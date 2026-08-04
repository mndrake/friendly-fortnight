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
_FREE_OP_RE = re.compile(r"^\s*([A-Z][A-Z0-9-]*)\b", re.IGNORECASE)

# Referenced-field token harvesting (design: "Column usage from CL and RPG
# parsing"). Pure token collection — no opcode semantics. A candidate token
# is a plain identifier; figurative constants/indicators (*BLANK, *INxx, ...),
# quoted literals and pure numbers are excluded by construction.
_FIELD_TOKEN_RE = re.compile(r"^[A-Z#$@][A-Z0-9#$@_]*$")
_STAR_KEYWORD_RE = re.compile(r"\*[A-Z0-9]+", re.IGNORECASE)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
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
class RpgOField:
    """One O-spec field entry: the program writes ``field`` to ``file``."""
    file: str
    field: str
    end_pos: Optional[int]


@dataclass
class RpgIField:
    """One I-spec field entry: reading ``file`` populates ``field``."""
    file: str
    field: str
    ext_name: Optional[str]     # external name when renaming (ext-described)
    from_pos: Optional[int]
    to_pos: Optional[int]


@dataclass
class RpgMove:
    """One data-moving C-spec: ``result`` is assigned from ``source``.

    ``source`` is ``None`` for a constant/literal assignment — recording
    that the field IS assigned (so the graph build won't claim it flows
    straight through from an input record) even though no field feeds it.
    """
    seq: int
    opcode: str
    source: Optional[str]
    result: str


@dataclass
class RpgProgram:
    program_id: str
    files: list[RpgFile] = field(default_factory=list)
    io_ops: list[RpgIoOp] = field(default_factory=list)
    copies: list[RpgCopy] = field(default_factory=list)
    sql_blocks: list[str] = field(default_factory=list)
    has_ospecs: bool = False
    has_ispecs: bool = False
    ospec_fields: list[RpgOField] = field(default_factory=list)
    ispec_fields: list[RpgIField] = field(default_factory=list)
    moves: list[RpgMove] = field(default_factory=list)
    # Identifier tokens seen in C-spec factor1/factor2/result and O-spec
    # field-entry areas — candidate "referenced fields" for column usage.
    # Not filtered against a file's real field set here (that intersection
    # happens in the graph build, once DSPFFD is available); this is
    # deliberately loose ("intersection filters everything").
    referenced_fields: set[str] = field(default_factory=set)


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


def _area_tokens(area: str, exclude: set[str]) -> set[str]:
    """Clean identifier tokens out of a fixed-column factor/result area.

    Whitespace-split, then each candidate is upper-cased and kept only if it
    looks like a plain identifier (``_FIELD_TOKEN_RE``) and is not in
    ``exclude`` (declared file names). Figurative constants (``*BLANK``),
    quoted literals and pure numeric literals never match the identifier
    pattern, so they drop out without special-casing.
    """
    out: set[str] = set()
    for raw in area.split():
        tok = raw.strip("()").upper()
        if not tok or not _FIELD_TOKEN_RE.match(tok) or tok in exclude:
            continue
        out.add(tok)
    return out


def _harvest_cspec_fields(line: str, known_files: set[str], rpg3: bool) -> set[str]:
    """Factor1/factor2/result identifier tokens for one fixed-format C-spec
    line (RPG III or RPG IV column layout)."""
    if rpg3:
        areas = (line[17:27] if len(line) > 17 else "",
                 line[32:42] if len(line) > 32 else "",
                 line[42:48] if len(line) > 42 else "")
    else:
        areas = (line[11:25] if len(line) > 11 else "",
                 line[35:49] if len(line) > 35 else "",
                 line[49:63] if len(line) > 49 else "")
    out: set[str] = set()
    for area in areas:
        out |= _area_tokens(area, known_files)
    return out


def _harvest_free_fields(stmt: str, known_files: set[str]) -> set[str]:
    """Identifier tokens for a free-format statement, minus the leading
    opcode and declared file names. Declarations (``DCL-*``) are skipped —
    they introduce names, they do not reference existing fields."""
    m = _FREE_OP_RE.match(stmt)
    opcode = m.group(1).upper() if m else ""
    if not opcode or opcode.startswith("DCL"):
        return set()
    text = _STRING_LITERAL_RE.sub(" ", stmt)
    text = _STAR_KEYWORD_RE.sub(" ", text)
    exclude = known_files | {opcode}
    out: set[str] = set()
    for raw in re.findall(r"[A-Za-z#$@][A-Za-z0-9#$@_]*", text):
        tok = raw.upper()
        if not _FIELD_TOKEN_RE.match(tok) or tok in exclude:
            continue
        out.add(tok)
    return out


# Output-spec special words that are not program fields (page counters and
# the runtime date family). Figurative constants (*DATE etc.) never match the
# identifier pattern in the first place.
_O_SPECIAL = {"PAGE", "PAGE1", "PAGE2", "PAGE3", "PAGE4", "PAGE5", "PAGE6",
              "PAGE7", "UDATE", "UDAY", "UMONTH", "UYEAR"}

# Data-moving opcodes -> which factors feed the result field. This is the
# assignment vocabulary of RPG III calculations; opcodes that only set
# indicators or position files (SETLL, LOKUP, ...) move no data and are
# deliberately absent. ``MVR`` (remainder of a previous DIV) is recorded
# against factor-less sources — the result is assigned, provenance unknown.
_MOVE_SOURCE_FACTORS: dict[str, tuple[int, ...]] = {
    "MOVE": (2,), "MOVEL": (2,), "MOVEA": (2,),
    "Z-ADD": (2,), "Z-SUB": (2,), "SQRT": (2,), "XFOOT": (2,),
    "XLATE": (2,), "SUBST": (2,),
    "ADD": (1, 2), "SUB": (1, 2), "MULT": (1, 2), "DIV": (1, 2),
    "CAT": (1, 2),
    # Assigned, but from no file field: DIV remainder, system clock, reset.
    "MVR": (), "TIME": (), "CLEAR": (),
}


def _factor_token(area: str) -> Optional[str]:
    """The field identifier in a factor/result area, or None for literals,
    figurative constants, and blanks. Array indexes (``ARR,3``) reduce to
    the array name."""
    tok = area.strip().split()[0].split(",")[0].upper() if area.strip() else ""
    return tok if tok and _FIELD_TOKEN_RE.match(tok) else None


def _int_or_none(area: str) -> Optional[int]:
    s = area.strip()
    return int(s) if s.isdigit() else None


def _parse_cspec_move(line: str, seq: int, rpg3: bool) -> list[RpgMove]:
    """Data-moving C-spec as (source -> result) rows, one per source field.

    A constant-only assignment yields a single ``source=None`` row so the
    result still counts as "assigned" downstream.
    """
    if rpg3:
        f1a, opa = line[17:27], line[27:32]
        f2a, resa = line[32:42], line[42:48]
    else:
        f1a, opa = line[11:25], line[25:35]
        f2a, resa = line[35:49], line[49:63]
    opcode = (opa.strip().upper().split() or [""])[0].split("(")[0]
    factors = _MOVE_SOURCE_FACTORS.get(opcode)
    if factors is None:
        return []
    result = _factor_token(resa)
    if result is None:
        return []
    sources = [t for t in
               (_factor_token(f1a) if 1 in factors else None,
                _factor_token(f2a) if 2 in factors else None) if t]
    if not sources:
        return [RpgMove(seq=seq, opcode=opcode, source=None, result=result)]
    return [RpgMove(seq=seq, opcode=opcode, source=s, result=result)
            for s in sources]


def _ospec_entry(line: str, rpg3: bool, cur_file: Optional[str]
                 ) -> tuple[Optional[str], Optional[RpgOField]]:
    """(new current O-file, field entry) for one O-spec line.

    Record identification lines carry the file name (cols 7-14 RPG III /
    7-16 RPG IV) and set the current file; field description lines have a
    blank name area and a field name at 32-37 (RPG III) / 30-43 (RPG IV).
    Constants (blank field area) and output special words (PAGE/UDATE
    family) are not fields. An EXCPT name sits in the field columns *of a
    record line*, so it never reaches the field branch.
    """
    name = (line[6:14] if rpg3 else line[6:16]).strip().upper() \
        if len(line) > 6 else ""
    if name:
        return name, None
    if rpg3:
        fld = line[31:37].strip().upper() if len(line) > 31 else ""
        end = line[39:43] if len(line) > 39 else ""
    else:
        fld = line[29:43].strip().upper() if len(line) > 29 else ""
        end = line[46:51] if len(line) > 46 else ""
    if (not cur_file or not fld or fld in _O_SPECIAL
            or not _FIELD_TOKEN_RE.match(fld)):
        return cur_file, None
    return cur_file, RpgOField(file=cur_file, field=fld,
                               end_pos=_int_or_none(end))


def _ispec_entry(line: str, rpg3: bool, cur_file: Optional[str]
                 ) -> tuple[Optional[str], Optional[RpgIField]]:
    """(new current I-file, field entry) for one I-spec line.

    Record identification lines name the file in cols 7-14; field
    description lines have a blank name area and the field name at 53-58
    (RPG III) / 49-62 (RPG IV), with from/to record positions at 44-47 /
    48-51 (RPG III). Data-structure subfield lines parse the same way and
    attribute to the DS name — harmless, since no read file matches it.
    """
    name = line[6:14].strip().upper() if len(line) > 6 else ""
    if name and _FIELD_TOKEN_RE.match(name):
        return name, None
    if rpg3:
        fld = line[52:58].strip().upper() if len(line) > 52 else ""
        from_pos = _int_or_none(line[43:47]) if len(line) > 43 else None
        to_pos = _int_or_none(line[47:51]) if len(line) > 47 else None
    else:
        fld = line[48:62].strip().upper() if len(line) > 48 else ""
        from_pos = to_pos = None
    if not cur_file or not fld or not _FIELD_TOKEN_RE.match(fld):
        return cur_file, None
    ext = line[20:30].strip().upper() if len(line) > 20 else ""
    ext_name = ext if ext and _FIELD_TOKEN_RE.match(ext) else None
    return cur_file, RpgIField(file=cur_file, field=fld, ext_name=ext_name,
                               from_pos=from_pos, to_pos=to_pos)


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
    move_seq = 0
    known_files: set[str] = set()
    last_file: Optional[RpgFile] = None
    cur_ofile: Optional[str] = None
    cur_ifile: Optional[str] = None

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
            cur_ifile, ifld = _ispec_entry(line, rpg3, cur_ifile)
            if ifld is not None:
                prog.ispec_fields.append(ifld)
            i += 1
            continue
        if ftype == "O":
            prog.has_ospecs = True
            # Field-entry area: idx 31:43 (loose — the DSPFFD intersection
            # in the graph build filters out anything that isn't a real
            # field, so record format names etc. are harmless surplus).
            prog.referenced_fields |= _area_tokens(
                line[31:43] if len(line) > 31 else "", known_files)
            cur_ofile, ofld = _ospec_entry(line, rpg3, cur_ofile)
            if ofld is not None:
                prog.ospec_fields.append(ofld)
            i += 1
            continue

        if ftype == "C":
            prog.referenced_fields |= _harvest_cspec_fields(
                line, known_files, rpg3=rpg3)
            op = _parse_cspec_io(line, seq + 1, known_files, rpg3=rpg3)
            if op is not None:
                seq += 1
                prog.io_ops.append(op)
            for mv in _parse_cspec_move(line, move_seq + 1, rpg3=rpg3):
                move_seq = mv.seq
                prog.moves.append(mv)
            i += 1
            continue

        if not rpg3:
            prog.referenced_fields |= _harvest_free_fields(stripped, known_files)
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

    file_rows, io_rows, field_ref_rows = [], [], []
    ospec_rows, ispec_rows, move_rows = [], [], []
    n = 0
    sql_pending: list[tuple[str, str]] = []  # (program_id, sql) for embedded_sql
    for m in load_members(con):
        if not m.is_rpg():
            continue
        n += 1
        prog = parse(m)
        for f in prog.files:
            file_rows.append((prog.program_id, f.file, f.usage, f.extname,
                              f.rename_rec, f.declared_via,
                              bool(f.program_described)))
        for op in prog.io_ops:
            io_rows.append((prog.program_id, op.seq, op.opcode, op.file,
                            op.direction))
        for blk in prog.sql_blocks:
            sql_pending.append((prog.program_id, blk))
        for fld in sorted(prog.referenced_fields):
            field_ref_rows.append((prog.program_id, fld))
        for of in prog.ospec_fields:
            ospec_rows.append((prog.program_id, of.file, of.field, of.end_pos))
        for if_ in prog.ispec_fields:
            ispec_rows.append((prog.program_id, if_.file, if_.field,
                               if_.ext_name, if_.from_pos, if_.to_pos))
        for mv in prog.moves:
            move_rows.append((prog.program_id, mv.seq, mv.opcode, mv.source,
                              mv.result))
    insert_rows(con, "parsed_rpg_files",
                ["program", "file", "usage", "extname", "rename_rec",
                 "declared_via", "program_described"], file_rows)
    insert_rows(con, "parsed_rpg_io_ops",
                ["program", "seq", "opcode", "file", "direction"], io_rows)
    insert_rows(con, "parsed_rpg_field_refs",
                ["program", "field_name"], field_ref_rows)
    insert_rows(con, "parsed_rpg_ospec_fields",
                ["program", "file", "field_name", "end_pos"], ospec_rows)
    insert_rows(con, "parsed_rpg_ispec_fields",
                ["program", "file", "field_name", "ext_name", "from_pos",
                 "to_pos"], ispec_rows)
    insert_rows(con, "parsed_rpg_moves",
                ["program", "seq", "opcode", "source_field", "result_field"],
                move_rows)
    # Stash embedded SQL blocks for the SQL parser to consume.
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _rpg_sql_blocks "
                "(program VARCHAR, seq INTEGER, raw_sql VARCHAR)")
    con.execute("DELETE FROM _rpg_sql_blocks")
    for idx, (pid, blk) in enumerate(sql_pending):
        con.execute("INSERT INTO _rpg_sql_blocks VALUES (?, ?, ?)",
                    [pid, idx, blk])
    return {"rpg_programs": n, "rpg_files": len(file_rows),
            "rpg_io_ops": len(io_rows), "rpg_sql_blocks": len(sql_pending),
            "rpg_field_refs": len(field_ref_rows),
            "rpg_ospec_fields": len(ospec_rows),
            "rpg_ispec_fields": len(ispec_rows),
            "rpg_moves": len(move_rows)}
