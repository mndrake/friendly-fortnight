"""Program tractability classification (design principle 3: don't fully
interpret RPG).

Each RPG program is classified as one of:

* ``sql_pure`` — all data access goes through embedded SQL; lineage comes from
  the SQL parser at column fidelity.
* ``ext_described_io`` — record-level I/O against externally described files;
  column mapping via DSPFFD record formats, record-level fidelity.
* ``program_described_or_complex`` — program-described files, O-specs/I-specs
  doing field remapping, S/36 markers, or heavy MOVE usage. Flagged for human
  review; contributes table-level edges only.

RPG III note: I-specs and O-specs are routine in RPG III even for externally
described files, so their mere presence is a *weak* signal; program-described
F-specs (format 'F' in col 19) are the strong signal.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .base import SourceMember
from .rpg import RpgProgram, parse as parse_rpg

SQL_PURE = "sql_pure"
EXT_IO = "ext_described_io"
COMPLEX = "program_described_or_complex"

_MOVE_RE = re.compile(r"^.{27}(MOVE[LA]?|MOVEA)\b", re.IGNORECASE)
_S36_RE = re.compile(r"^\s*//\s*(?:LOAD|RUN|FILE)\b", re.IGNORECASE)


@dataclass
class Classification:
    program: str
    program_class: str
    reasons: list[str] = field(default_factory=list)
    needs_review: bool = False


def classify(member: SourceMember, prog: RpgProgram | None = None) -> Classification:
    if prog is None:
        prog = parse_rpg(member)

    reasons: list[str] = []

    has_prog_described = any(f.program_described for f in prog.files)
    if has_prog_described:
        reasons.append("program_described_fspec")

    # S/36 environment markers (OCL-style // statements in the member).
    s36 = any(_S36_RE.match(ln) for ln in member.lines)
    if s36:
        reasons.append("s36_markers")

    # MOVE-heavy field remapping: count MOVE/MOVEL/MOVEA C-specs relative to
    # total C-specs.
    move_count = 0
    cspec_count = 0
    for ln in member.lines:
        if len(ln) > 5 and ln[5].upper() == "C" and not (len(ln) > 6 and ln[6] == "*"):
            cspec_count += 1
            if _MOVE_RE.match(ln):
                move_count += 1
    move_heavy = cspec_count >= 10 and move_count / max(cspec_count, 1) > 0.4
    if move_heavy:
        reasons.append("move_heavy")

    has_files = bool(prog.files)
    has_io = bool(prog.io_ops)
    has_sql = bool(prog.sql_blocks)
    dynamic_sql = any(
        re.match(r"^\s*(?:PREPARE|EXECUTE)\b", b, re.IGNORECASE)
        for b in prog.sql_blocks
    )
    if dynamic_sql:
        reasons.append("dynamic_sql")

    # O-specs indicate program-defined output formatting. In RPG III this is
    # common; it only tips the class when the file is program described.
    if prog.has_ospecs and has_prog_described:
        reasons.append("ospecs_on_program_described")

    if has_prog_described or s36 or move_heavy:
        cls = COMPLEX
    elif has_sql and not has_io and not has_files:
        cls = SQL_PURE
        reasons.append("all_access_via_sql")
    elif has_sql and (has_io or has_files):
        # Mixed: SQL plus record I/O on externally described files.
        cls = EXT_IO
        reasons.append("mixed_sql_and_record_io")
    elif has_files:
        cls = EXT_IO
        reasons.append("externally_described_record_io")
    else:
        cls = COMPLEX
        reasons.append("no_recognizable_io")

    needs_review = cls == COMPLEX or dynamic_sql
    return Classification(program=prog.program_id, program_class=cls,
                          reasons=reasons, needs_review=needs_review)


# --- Orchestration -----------------------------------------------------------

def classify_all(con) -> dict[str, int]:
    from ..db import insert_rows
    from .base import load_members

    rows = []
    counts = {SQL_PURE: 0, EXT_IO: 0, COMPLEX: 0}
    for m in load_members(con):
        if not m.is_rpg():
            continue
        c = classify(m)
        counts[c.program_class] += 1
        rows.append((c.program, c.program_class, json.dumps(c.reasons),
                     c.needs_review))
    insert_rows(con, "program_classification",
                ["program", "program_class", "reasons", "needs_review"], rows)
    return {f"class_{k}": v for k, v in counts.items()}
