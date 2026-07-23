"""RPG structural parser — RPG III first, RPG IV (RPGLE) as a secondary layout.

The estate is assumed to be **RPG III** (member types RPG / RPG38 / RPT /
SQLRPG). RPG III specifics honoured here:

* F-spec: file name in cols 7-14, file type (I/O/U/C) col 15, file format col
  19 (``E`` externally described / ``F`` program described). There is no
  EXTNAME keyword — an externally described file's external name *is* the file
  name, and redirection happens through OVRDBF at run time.
* F-spec continuation lines carry ``K`` options (``KRENAME`` record-format
  rename); captured by keyword scan rather than strict columns.
* C-spec: opcode in cols 28-32, factor 2 in cols 33-42. RPG III opcode
  spellings: ``UPDAT``, ``DELET``, ``EXCPT``, ``REDPE``.
* ``/COPY`` argument syntax ``[lib/]srcfile,member``.
* Embedded SQL (SQLRPG): ``C/EXEC SQL`` … ``C+`` continuations … ``C/END-EXEC``.

Member types RPGLE / SQLRPGLE switch to RPG IV columns (opcode 26-35) and
free-format ``DCL-F`` / ``EXTFILE`` handling.

Structural only — RPG logic is never interpreted (design principle 3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .base import SourceMember

# Opcode -> direction. Positioning ops (SETLL/SETGT) count as reads.
# Includes both RPG III (UPDAT/DELET/EXCPT/REDPE) and RPG IV spellings.
_READ_OPS = {"CHAIN", "READ", "READE", "READP", "READPE", "REDPE", "READC",
             "SETLL", "SETGT"}
_WRITE_OPS = {"WRITE", "UPDAT", "UPDATE", "DELET", "DELETE", "EXCPT",
              "EXCEPT", "EXFMT"}

_USAGE_FROM_FTYPE = {"I": "input", "O": "output", "U": "update", "C": "combined"}
_USAGE_FROM_KW = {
    "*INPUT": "input", "*OUTPUT": "output", "*UPDATE": "update",
    "*DELETE": "update",
}

# RPG III member types use the RPG III column layout.
RPG3_TYPES = {"RPG", "RPG38", "RPT", "SQLRPG"}

_COPY_RE = re.compile(r"^.{5}\s?/(?:COPY|INCLUDE)\s+(\S+)", re.IGNORECASE)
_COPY_FREE_RE = re.compile(r"^\s*/(?:COPY|INCLUDE)\s+(\S+)", re.IGNORECASE)
_DCLF_RE = re.compile(r"^\s*DCL-F\s+([A-Z0-9#$@_]+)(.*?);?\s*$", re.IGNORECASE)
_KW_RE = re.compile(r"([A-Z][A-Z0-9]*)\s*\(([^)]*)\)", re.IGNORECASE)
_FREE_OP_RE = re.compile(r"^\s*([A-Z]+)\b", re.IGNORECASE)
# F-spec continuation option: K RENAME (RPG III) — external format in 19-28,
# new name after RENAME. Scanned loosely: "K" then "RENAME" then the new name.
_KRENAME_RE = re.compile(r"\bKRENAME\s*([A-Z0-9#$@_]*)", re.IGNORECASE)


@dataclass
class RpgFile:
    file: str
    usage: str
    extname: Optional[str] = None   # None => program described
    rename_rec: Optional[str] = None
    declared_via: str = "fspec"
    program_described: bool = False


@dataclass
class RpgIoOp:
    seq: int
    opcode: str
    file: str
    direction: str


@dataclass
class RpgCopy:
    """A /COPY reference: optional library, source file, member."""
    library: Optional[str]
    srcfile: Optional[str]
    member: str


@dataclass
class RpgProgram:
    program_id: str
    files: list[RpgFile] = field(default_factory=list)
    io_ops: list[RpgIoOp] = field(default_factory=list)
    copies: list[RpgCopy] = field(default_factory=list)
    sql_blocks: list[str] = field(default_factory=list)
    has_ospecs: bool = False
    has_ispecs: bool = False


def _kw(text: str) -> dict[str, str]:
    return {m.group(1).upper(): m.group(2).strip() for m in _KW_RE.finditer(text)}


def _form_type(line: str) -> str:
    return (line[5] if len(line) > 5 else " ").upper()


def _is_comment(line: str, rpg3: bool) -> bool:
    # Fixed-format comment: '*' in col 7 (idx 6).
    if len(line) > 6 and line[6] == "*":
        return True
    s = line.strip()
    if not rpg3 and (s.startswith("//")):
        return True
    return False


def _parse_copy_arg(arg: str) -> RpgCopy:
    """RPG III form: [lib/]srcfile,member — or bare member name."""
    arg = arg.strip().upper()
    lib = None
    rest = arg
    if "/" in rest:
        lib, rest = rest.split("/", 1)
    if "," in rest:
        srcfile, member = rest.split(",", 1)
        return RpgCopy(library=lib or None, srcfile=srcfile or None,
                       member=member.strip())
    return RpgCopy(library=lib or None, srcfile=None, member=rest.strip())


def parse(member: SourceMember) -> RpgProgram:
    rpg3 = member.type_upper in RPG3_TYPES or member.type_upper == ""
    prog = RpgProgram(program_id=member.program_id)
    lines = member.lines
    i = 0
    seq = 0
    known_files: set[str] = set()
    last_file: Optional[RpgFile] = None

    while i < len(lines):
        line = lines[i].rstrip("\n")

        # /COPY — '/' in col 7 for fixed format; free-format at any indent.
        mcopy = _COPY_RE.match(line) or _COPY_FREE_RE.match(line)
        if mcopy:
            prog.copies.append(_parse_copy_arg(mcopy.group(1)))
            i += 1
            continue

        # Embedded SQL block (C/EXEC SQL in fixed format, EXEC SQL in free).
        if re.search(r"[/\s]EXEC\s+SQL\b", line, re.IGNORECASE):
            block, consumed = _collect_sql(lines, i, rpg3=rpg3)
            if block:
                prog.sql_blocks.append(_normalize_hostvars(block))
            i += consumed
            continue

        if _is_comment(line, rpg3):
            i += 1
            continue

        ftype = _form_type(line)
        stripped = line.strip()

        if not rpg3:
            # Free-format DCL-F (RPGLE only).
            mdcl = _DCLF_RE.match(stripped)
            if mdcl and ftype not in {"C", "I", "O", "P", "D", "F"}:
                fname = mdcl.group(1).upper()
                kws = _kw(mdcl.group(2))
                usage = _usage_from_keywords(kws)
                extname = _extname_from_keywords(kws) or fname
                rename = None
                if "RENAME" in kws:
                    rename = kws["RENAME"].split(":")[0].strip().upper() or None
                f = RpgFile(file=fname, usage=usage, extname=extname,
                            rename_rec=rename, declared_via="dclf")
                prog.files.append(f)
                known_files.add(fname)
                last_file = f
                i += 1
                continue

        if ftype == "F":
            fspec = _parse_fspec(line, rpg3=rpg3)
            if fspec is not None:
                prog.files.append(fspec)
                known_files.add(fspec.file)
                last_file = fspec
            else:
                # Continuation line — check for KRENAME applying to last file.
                mk = _KRENAME_RE.search(line)
                if mk and last_file is not None and mk.group(1):
                    last_file.rename_rec = mk.group(1).upper()
            i += 1
            continue

        if ftype == "I":
            prog.has_ispecs = True
            i += 1
            continue
        if ftype == "O":
            prog.has_ospecs = True
            i += 1
            continue

        if ftype == "C":
            op = _parse_cspec_io(line, seq + 1, known_files, rpg3=rpg3)
            if op is not None:
                seq += 1
                prog.io_ops.append(op)
            i += 1
            continue

        if not rpg3:
            op = _parse_free_io(stripped, seq + 1, known_files)
            if op is not None:
                seq += 1
                prog.io_ops.append(op)
        i += 1

    return prog


def _parse_fspec(line: str, rpg3: bool) -> Optional[RpgFile]:
    if rpg3:
        # RPG III: name cols 7-14 (idx 6:14), type col 15 (idx 14),
        # format col 19 (idx 18): E externally described / F program described.
        name = line[6:14].strip().upper() if len(line) > 6 else ""
        if not name:
            return None
        ftype = (line[14] if len(line) > 14 else " ").upper()
        fmt = (line[18] if len(line) > 18 else " ").upper()
        usage = _USAGE_FROM_FTYPE.get(ftype, "input")
        is_ext = fmt == "E"
        return RpgFile(
            file=name, usage=usage,
            extname=name if is_ext else None,
            rename_rec=None, declared_via="fspec",
            program_described=not is_ext,
        )
    # RPG IV fixed: name cols 7-16 (idx 6:16), type col 17 (idx 16),
    # externally described col 22 (idx 21) == 'E', keywords from col 44.
    name = line[6:16].strip().upper() if len(line) > 6 else ""
    if not name:
        return None
    ftype = (line[16] if len(line) > 16 else " ").upper()
    usage = _USAGE_FROM_FTYPE.get(ftype, "input")
    kws = _kw(line[43:].strip() if len(line) > 43 else "")
    extname = _extname_from_keywords(kws)
    is_ext = (len(line) > 21 and line[21].upper() == "E")
    if extname is None and is_ext:
        extname = name
    rename = None
    if "RENAME" in kws:
        rename = kws["RENAME"].split(":")[0].strip().upper() or None
    return RpgFile(file=name, usage=usage, extname=extname, rename_rec=rename,
                   declared_via="fspec", program_described=not is_ext and extname is None)


def _usage_from_keywords(kws: dict[str, str]) -> str:
    if "USAGE" in kws:
        tokens = [t.strip().upper() for t in kws["USAGE"].split(":")]
        mapped = {_USAGE_FROM_KW.get(t) for t in tokens} - {None}
        if mapped == {"input"}:
            return "input"
        if mapped == {"output"}:
            return "output"
        if mapped:
            return "update" if "update" in mapped or len(mapped) > 1 else next(iter(mapped))
    return "input"


def _extname_from_keywords(kws: dict[str, str]) -> Optional[str]:
    for kw in ("EXTFILE", "EXTDESC", "EXTNAME"):
        if kw in kws:
            val = kws[kw].split(":")[0].strip().strip("'\"").upper()
            if val and val != "*EXTDESC":
                return val
    return None


def _parse_cspec_io(line: str, seq: int, known_files: set[str],
                    rpg3: bool) -> Optional[RpgIoOp]:
    if rpg3:
        # RPG III: opcode cols 28-32 (idx 27:32), factor 2 cols 33-42 (idx 32:42).
        opcode_area = line[27:32].strip().upper() if len(line) > 27 else ""
        factor2 = line[32:42].strip().upper() if len(line) > 32 else ""
    else:
        # RPG IV: opcode cols 26-35 (idx 25:35), factor 2 cols 36-49.
        opcode_area = line[25:35].strip().upper() if len(line) > 25 else ""
        factor2 = line[35:49].strip().upper() if len(line) > 35 else ""
    opcode = opcode_area.split()[0] if opcode_area else ""
    # Strip operation extenders like (E) or (N) on RPG IV.
    opcode = opcode.split("(")[0]
    if opcode not in _READ_OPS and opcode not in _WRITE_OPS:
        return None
    file = factor2.split()[0] if factor2 else ""
    if file not in known_files:
        # Factor 2 may name a record format (or be blank for WRITE recfmt).
        # Fall back to the sole declared file when unambiguous.
        if len(known_files) == 1:
            file = next(iter(known_files))
        else:
            return None
    direction = "read" if opcode in _READ_OPS else "write"
    return RpgIoOp(seq=seq, opcode=opcode, file=file, direction=direction)


def _parse_free_io(stripped: str, seq: int, known_files: set[str]) -> Optional[RpgIoOp]:
    m = _FREE_OP_RE.match(stripped)
    if not m:
        return None
    opcode = m.group(1).upper()
    if opcode not in _READ_OPS and opcode not in _WRITE_OPS:
        return None
    rest = stripped[m.end():].strip().rstrip(";")
    target = rest.split()[0].upper().strip("()") if rest else ""
    file = target if target in known_files else (
        next(iter(known_files)) if len(known_files) == 1 else target)
    if file not in known_files:
        return None
    direction = "read" if opcode in _READ_OPS else "write"
    return RpgIoOp(seq=seq, opcode=opcode, file=file, direction=direction)


def _collect_sql(lines: list[str], start: int, rpg3: bool) -> tuple[str, int]:
    """Collect an EXEC SQL block starting at ``start``.

    Fixed format (RPG III SQLRPG and fixed RPGLE): ``C/EXEC SQL`` then ``C+``
    continuations until ``C/END-EXEC``. Free format: until a line ending ';'.
    Returns (sql_text, lines_consumed).
    """
    first = lines[start]
    fixed = bool(re.match(r"^.{5}[A-Za-z ]?/", first)) or "/EXEC" in first.upper()
    buf: list[str] = []
    i = start
    n = len(lines)
    ended = False
    while i < n:
        ln = lines[i]
        if re.search(r"\bEND-EXEC\b", ln, re.IGNORECASE):
            i += 1
            ended = True
            break
        buf.append(ln)
        if not fixed and re.search(r";\s*$", ln.rstrip()):
            i += 1
            ended = True
            break
        i += 1
    if not ended:
        i = n
    consumed = i - start

    cleaned: list[str] = []
    for ln in buf:
        # Strip the fixed-format control columns: seq/form 'C' at idx 5 plus
        # the '/'-directive or '+'-continuation marker at idx 6.
        if len(ln) > 6 and ln[5].upper() in {"C", " "} and ln[6] in {"/", "+"}:
            ln = ln[7:]
        elif len(ln) > 6 and ln[5].upper() == "C":
            ln = ln[6:]
        cleaned.append(ln)
    text = " ".join(l.strip() for l in cleaned)
    text = re.sub(r"\bEXEC\s+SQL\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bEND-EXEC\b", " ", text, flags=re.IGNORECASE)
    return text.strip().rstrip(";").strip(), max(consumed, 1)


def _normalize_hostvars(sql: str) -> str:
    # Replace :HOSTVAR (and :struct.field) with parameter markers.
    return re.sub(r":[A-Za-z_][A-Za-z0-9_.]*", "?", sql)


# --- Orchestration -----------------------------------------------------------

def parse_all(con) -> dict[str, int]:
    from ..db import insert_rows
    from .base import load_members

    file_rows, io_rows = [], []
    n = 0
    sql_pending: list[tuple[str, str]] = []  # (program_id, sql) for embedded_sql
    for m in load_members(con):
        if not m.is_rpg():
            continue
        n += 1
        prog = parse(m)
        for f in prog.files:
            file_rows.append((prog.program_id, f.file, f.usage, f.extname,
                              f.rename_rec, f.declared_via))
        for op in prog.io_ops:
            io_rows.append((prog.program_id, op.seq, op.opcode, op.file,
                            op.direction))
        for blk in prog.sql_blocks:
            sql_pending.append((prog.program_id, blk))
    insert_rows(con, "parsed_rpg_files",
                ["program", "file", "usage", "extname", "rename_rec",
                 "declared_via"], file_rows)
    insert_rows(con, "parsed_rpg_io_ops",
                ["program", "seq", "opcode", "file", "direction"], io_rows)
    # Stash embedded SQL blocks for the SQL parser to consume.
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _rpg_sql_blocks "
                "(program VARCHAR, seq INTEGER, raw_sql VARCHAR)")
    con.execute("DELETE FROM _rpg_sql_blocks")
    for idx, (pid, blk) in enumerate(sql_pending):
        con.execute("INSERT INTO _rpg_sql_blocks VALUES (?, ?, ?)",
                    [pid, idx, blk])
    return {"rpg_programs": n, "rpg_files": len(file_rows),
            "rpg_io_ops": len(io_rows), "rpg_sql_blocks": len(sql_pending)}
