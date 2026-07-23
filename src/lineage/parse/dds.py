"""DDS parser for physical and logical files.

Parses the fixed-column DDS statement area (the 80-char SRCDTA content):

    col  6 (idx 5)  form type — 'A' for DDS
    col  7 (idx 6)  '*' => comment line
    col 17 (idx16)  name type — R record, K key, S/O select/omit, J join, blank field
    cols 19-28      name
    col 38 (idx37)  usage (I/O/B/N/H)
    cols 45-80      functions/keywords (PFILE, JFILE, RENAME, CONCAT, REFFLD, JREF, ...)

Logical files (PFILE/JFILE present) yield field-level ``derives_from`` edges
from LF fields to the PF fields they reference; join logicals honour JREF to
pick the right physical file, and RENAME/CONCAT are carried on the field.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .base import SourceMember

_FORM_IDX = 5
_COMMENT_IDX = 6
_NAMETYPE_IDX = 16
_NAME_START = 18
_NAME_END = 28
_USAGE_IDX = 37
_FUNC_START = 44

_KEYWORD_RE = re.compile(r"([A-Z][A-Z0-9]*)\s*(\([^)]*\))?", re.IGNORECASE)


@dataclass
class DdsField:
    name: str
    usage: Optional[str] = None
    renamed_from: Optional[str] = None
    ref_field: Optional[str] = None
    ref_file: Optional[str] = None
    concat_fields: list[str] = field(default_factory=list)


@dataclass
class DdsRecordFormat:
    name: str
    fields: list[DdsField] = field(default_factory=list)


@dataclass
class DdsFile:
    library: str
    file: str
    dds_type: str  # 'PF' or 'LF'
    based_on: list[str] = field(default_factory=list)  # PFILE/JFILE targets
    is_join: bool = False
    records: list[DdsRecordFormat] = field(default_factory=list)


def _cell(line: str, start: int, end: int) -> str:
    return line[start:end].strip() if len(line) > start else ""


def _char(line: str, idx: int) -> str:
    return line[idx] if len(line) > idx else " "


def _parse_keywords(text: str) -> dict[str, list[str]]:
    """Return keyword -> list of operand tokens. CONCAT/JFILE keep order."""
    out: dict[str, list[str]] = {}
    for m in _KEYWORD_RE.finditer(text):
        kw = m.group(1).upper()
        args_raw = m.group(2) or ""
        args = args_raw.strip("()").replace(",", " ").split()
        out[kw] = args
    return out


def parse(member: SourceMember) -> Optional[DdsFile]:
    """Parse a DDS member. Returns ``None`` if it contains no DDS statements."""
    file = DdsFile(library=member.library, file=member.member, dds_type="PF")
    current_rec: Optional[DdsRecordFormat] = None
    current_field: Optional[DdsField] = None
    saw_dds = False

    # Accumulate function-area text per entry to handle keyword continuation
    # (unbalanced parens continue onto following DDS lines).
    pending_func = ""
    pending_target: Optional[str] = None  # 'file', 'record', 'field'

    def flush() -> None:
        nonlocal pending_func, pending_target
        if not pending_func.strip() or pending_target is None:
            pending_func = ""
            pending_target = None
            return
        kws = _parse_keywords(pending_func)
        if pending_target == "file" or pending_target == "record":
            if "PFILE" in kws:
                file.dds_type = "LF"
                file.based_on.extend(kws["PFILE"])
            if "JFILE" in kws:
                file.dds_type = "LF"
                file.is_join = True
                file.based_on.extend(kws["JFILE"])
        if pending_target == "field" and current_field is not None:
            if "RENAME" in kws and kws["RENAME"]:
                current_field.renamed_from = kws["RENAME"][0]
            if "REFFLD" in kws and kws["REFFLD"]:
                current_field.ref_field = kws["REFFLD"][0]
            if "CONCAT" in kws:
                current_field.concat_fields = list(kws["CONCAT"])
            if "JREF" in kws and kws["JREF"]:
                current_field.ref_file = kws["JREF"][0]
        pending_func = ""
        pending_target = None

    for raw in member.lines:
        if _char(raw, _FORM_IDX).upper() != "A":
            continue
        if _char(raw, _COMMENT_IDX) == "*":
            continue
        saw_dds = True
        name_type = _char(raw, _NAMETYPE_IDX).upper().strip()
        name = _cell(raw, _NAME_START, _NAME_END)
        func = raw[_FUNC_START:].rstrip() if len(raw) > _FUNC_START else ""

        is_new_entry = bool(name) or name_type in {"R", "J", "K", "S", "O"}
        if is_new_entry:
            flush()

        if name_type == "R":
            current_rec = DdsRecordFormat(name=name)
            file.records.append(current_rec)
            current_field = None
            pending_target = "record"
        elif name_type in {"K", "S", "O", "J"}:
            # Key / select / omit / join spec: not a data field. Keywords on
            # these lines (e.g. JFILE lives at file level) target the file.
            pending_target = "file" if name_type == "J" else "record"
        elif name and name_type == "":
            # Data field within the current record format.
            if current_rec is None:
                current_rec = DdsRecordFormat(name=member.member)
                file.records.append(current_rec)
            usage = _char(raw, _USAGE_IDX).upper().strip() or None
            current_field = DdsField(name=name, usage=usage)
            # For a plain LF field, the field name is also the PF field name
            # unless RENAME says otherwise.
            current_rec.fields.append(current_field)
            pending_target = "field"
        else:
            # Continuation or file-level keyword line (no name).
            if pending_target is None:
                pending_target = "record" if current_rec else "file"

        if func:
            pending_func = (pending_func + " " + func) if pending_func else func

    flush()

    if not saw_dds:
        return None

    # Resolve LF field references. For a non-join LF, a field derives from the
    # like-named PF field (post-RENAME) in the single based-on PF.
    if file.dds_type == "LF" and not file.is_join and len(file.based_on) == 1:
        pf = file.based_on[0]
        for rec in file.records:
            for fld in rec.fields:
                if fld.ref_file is None:
                    fld.ref_file = pf
                if fld.ref_field is None:
                    fld.ref_field = fld.renamed_from or fld.name
    elif file.dds_type == "LF" and file.is_join:
        for rec in file.records:
            for fld in rec.fields:
                if fld.ref_field is None:
                    fld.ref_field = fld.renamed_from or fld.name
                # ref_file stays as JREF gave it (may be None -> ambiguous).

    return file


# --- Orchestration -----------------------------------------------------------

def parse_all(con) -> dict[str, int]:
    """Parse all DDS members in the raw store into the parsed_dds_* tables."""
    import json

    from ..db import insert_rows
    from .base import load_members

    file_rows = []
    field_rows = []
    n_files = 0
    for m in load_members(con):
        if not m.is_dds():
            continue
        parsed = parse(m)
        if parsed is None:
            continue
        n_files += 1
        for rec in parsed.records:
            file_rows.append((
                parsed.library, parsed.file, parsed.dds_type, rec.name,
                json.dumps(parsed.based_on), parsed.is_join,
            ))
            for fld in rec.fields:
                field_rows.append((
                    parsed.library, parsed.file, rec.name, fld.name,
                    fld.renamed_from, fld.ref_field, fld.ref_file,
                    json.dumps(fld.concat_fields), fld.usage,
                ))
    insert_rows(con, "parsed_dds_files",
                ["library", "file", "dds_type", "record_format", "based_on",
                 "is_join"], file_rows)
    insert_rows(con, "parsed_dds_fields",
                ["library", "file", "record_format", "field_name",
                 "renamed_from", "ref_field", "ref_file", "concat_fields",
                 "usage"], field_rows)
    return {"dds_files": n_files, "dds_fields": len(field_rows)}
