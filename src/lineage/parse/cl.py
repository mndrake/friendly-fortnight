"""CL parser.

Line-continuation-aware tokenizer for CL source. Extracts the statements that
carry lineage: OVRDBF/DLTOVR (file redirection), CPYF (file->file copy),
CALL/CALLPRC and SBMJOB CMD(CALL ...) (program calls), RUNSQLSTM (feeds the SQL
parser), and CRTDUPOBJ. CHGVAR/DCL are tracked well enough to constant-fold
object names; anything runtime-dependent is captured as an expression and
marked unresolved rather than guessed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from .base import SourceMember

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LABEL_RE = re.compile(r"^\s*[A-Z#$@][A-Z0-9#$@_]*:\s*", re.IGNORECASE)


@dataclass
class ClOverride:
    seq: int
    file: str
    to_file: Optional[str] = None
    to_library: Optional[str] = None
    to_member: Optional[str] = None
    scope: Optional[str] = None
    resolved: bool = True
    expr: Optional[str] = None


@dataclass
class ClCall:
    seq: int
    called_lib: Optional[str]
    called_pgm: Optional[str]
    via: str
    params: list[str] = field(default_factory=list)
    resolved: bool = True
    expr: Optional[str] = None


@dataclass
class ClStatement:
    seq: int
    command: str
    raw_text: str


@dataclass
class ClProgram:
    program_id: str
    statements: list[ClStatement] = field(default_factory=list)
    overrides: list[ClOverride] = field(default_factory=list)
    calls: list[ClCall] = field(default_factory=list)
    variables: dict[str, Optional[str]] = field(default_factory=dict)


def _strip_comments(text: str) -> str:
    return _COMMENT_RE.sub(" ", text)


def _join_continuations(lines: list[str]) -> list[str]:
    """Fold CL continuation lines. A '+' joins with the next line left-stripped;
    a '-' joins with the next line as-is."""
    out: list[str] = []
    acc: Optional[str] = None
    for ln in lines:
        piece = ln
        if acc is not None:
            # previous line requested continuation; how to join is stored in acc
            joiner, prev = acc  # type: ignore[misc]
            piece = prev + (ln.lstrip() if joiner == "+" else ln)
            acc = None
        stripped = piece.rstrip()
        if stripped.endswith("+"):
            acc = ("+", stripped[:-1].rstrip())  # type: ignore[assignment]
        elif stripped.endswith("-"):
            acc = ("-", stripped[:-1])  # type: ignore[assignment]
        else:
            out.append(piece)
    if acc is not None:
        out.append(acc[1])  # type: ignore[index]
    return out


def _iter_statements(member: SourceMember):
    """Yield (seq, statement_text) for each CL command, comments removed."""
    text = _strip_comments(member.text)
    logical = _join_continuations(text.splitlines())
    for i, stmt in enumerate(logical, start=1):
        s = _LABEL_RE.sub("", stmt).strip()
        if not s:
            continue
        yield i, s


def _verb(stmt: str) -> str:
    return stmt.split(None, 1)[0].upper() if stmt.strip() else ""


def extract_param(stmt: str, keyword: str) -> Optional[str]:
    """Return the raw text inside ``KEYWORD(...)`` with balanced parens."""
    pat = re.compile(rf"\b{re.escape(keyword)}\s*\(", re.IGNORECASE)
    m = pat.search(stmt)
    if not m:
        return None
    i = m.end()
    depth = 1
    start = i
    while i < len(stmt) and depth > 0:
        c = stmt[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return stmt[start:i].strip()
        i += 1
    return stmt[start:i].strip()


def _first_positional(stmt: str) -> Optional[str]:
    parts = stmt.split(None, 2)
    if len(parts) >= 2 and "(" not in parts[1]:
        return parts[1]
    return None


def _split_qualified(name: str) -> tuple[Optional[str], str]:
    name = name.strip().strip("'\"")
    if "/" in name:
        lib, obj = name.split("/", 1)
        lib = lib.strip()
        if lib.upper() in {"*LIBL", "*CURLIB", ""}:
            lib = None  # unqualified against liblist
        return lib, obj.strip()
    return None, name


def _is_var(token: str) -> bool:
    return token.strip().startswith("&")


def parse(member: SourceMember) -> ClProgram:
    prog = ClProgram(program_id=member.program_id)

    def resolve_name(raw: Optional[str]) -> tuple[Optional[str], bool, Optional[str]]:
        """Resolve an operand to a constant name. Returns (value, resolved, expr)."""
        if raw is None:
            return None, True, None
        tok = raw.strip()
        if not tok:
            return None, True, None
        if _is_var(tok):
            var = tok.split()[0].upper()
            val = prog.variables.get(var)
            if val is not None:
                return val, True, None
            return None, False, tok  # runtime-dependent
        # constant / literal
        return tok.strip("'\""), True, None

    for seq, stmt in _iter_statements(member):
        verb = _verb(stmt)
        prog.statements.append(ClStatement(seq=seq, command=verb, raw_text=stmt))

        if verb in {"DCL", "CHGVAR"}:
            _track_variable(prog, stmt, verb)

        elif verb == "OVRDBF":
            _parse_ovrdbf(prog, seq, stmt, resolve_name)

        elif verb == "DLTOVR":
            # captured as a statement; resolution simulates scope in graph.resolve
            pass

        elif verb == "CPYF":
            _parse_cpyf(prog, seq, stmt, resolve_name)

        elif verb in {"CALL", "CALLPRC"}:
            _parse_call(prog, seq, stmt, resolve_name, via="CALL")

        elif verb == "SBMJOB":
            _parse_sbmjob(prog, seq, stmt, resolve_name)

        elif verb == "RUNSQLSTM":
            _parse_runsqlstm(prog, seq, stmt, resolve_name)

        elif verb in {"CRTDUPOBJ"}:
            _parse_crtdupobj(prog, seq, stmt, resolve_name)

    return prog


def _track_variable(prog: ClProgram, stmt: str, verb: str) -> None:
    var = extract_param(stmt, "VAR")
    if not var:
        return
    var = var.strip().split()[0].upper()
    value = extract_param(stmt, "VALUE")
    if value is None:
        prog.variables[var] = None
        return
    value = value.strip()
    # Constant string literal.
    if value.startswith("'") and value.endswith("'"):
        prog.variables[var] = value.strip("'")
        return
    # Simple constant token with no operators / concatenation / var refs.
    if not any(op in value for op in ("*CAT", "*TCAT", "*BCAT", "&")) and " " not in value:
        prog.variables[var] = value.strip("'\"")
        return
    # Runtime-dependent (concatenation, other variables): leave unresolved.
    prog.variables[var] = None


def _parse_ovrdbf(prog, seq, stmt, resolve_name):
    file_raw = extract_param(stmt, "FILE") or _first_positional(stmt)
    tofile_raw = extract_param(stmt, "TOFILE")
    mbr_raw = extract_param(stmt, "MBR")
    scope = extract_param(stmt, "OVRSCOPE")

    fval, fres, _ = resolve_name(file_raw)
    tval, tres, texpr = resolve_name(tofile_raw)
    to_lib, to_file = (None, None)
    if tval:
        to_lib, to_file = _split_qualified(tval)
    mbr_val, _, _ = resolve_name(mbr_raw)
    resolved = fres and (tofile_raw is None or tres)
    prog.overrides.append(ClOverride(
        seq=seq,
        file=(fval or (file_raw or "")).upper() if fval or file_raw else "",
        to_file=to_file.upper() if to_file else None,
        to_library=to_lib.upper() if to_lib else None,
        to_member=mbr_val.upper() if mbr_val else None,
        scope=(scope or "").upper() or None,
        resolved=resolved,
        expr=None if resolved else (texpr or tofile_raw),
    ))


def _parse_cpyf(prog, seq, stmt, resolve_name):
    from_raw = extract_param(stmt, "FROMFILE")
    to_raw = extract_param(stmt, "TOFILE")
    fmtopt = extract_param(stmt, "FMTOPT")
    fval, fres, fexpr = resolve_name(from_raw)
    tval, tres, texpr = resolve_name(to_raw)
    from_lib, from_file = _split_qualified(fval) if fval else (None, None)
    to_lib, to_file = _split_qualified(tval) if tval else (None, None)
    # Model CPYF as a call-ish file->file edge; captured on the calls list with
    # via=CPYF so the graph builder can turn it into reads/writes edges.
    prog.calls.append(ClCall(
        seq=seq, called_lib=None, called_pgm=None, via="CPYF",
        params=[json.dumps({
            "from_lib": from_lib, "from_file": from_file,
            "to_lib": to_lib, "to_file": to_file,
            "fmtopt": fmtopt.strip() if fmtopt else None,
        })],
        resolved=fres and tres,
        expr=None if (fres and tres) else (fexpr or texpr),
    ))


def _parse_call(prog, seq, stmt, resolve_name, via):
    pgm_raw = extract_param(stmt, "PGM") or _first_positional(stmt)
    pval, pres, pexpr = resolve_name(pgm_raw)
    lib, pgm = _split_qualified(pval) if pval else (None, None)
    parm_raw = extract_param(stmt, "PARM")
    params = _split_parms(parm_raw) if parm_raw else []
    prog.calls.append(ClCall(
        seq=seq,
        called_lib=lib.upper() if lib else None,
        called_pgm=pgm.upper() if pgm else None,
        via=via, params=params, resolved=pres,
        expr=None if pres else (pexpr or pgm_raw),
    ))


def _parse_sbmjob(prog, seq, stmt, resolve_name):
    cmd = extract_param(stmt, "CMD")
    if not cmd:
        return
    inner_verb = _verb(cmd)
    if inner_verb in {"CALL", "CALLPRC"}:
        _parse_call(prog, seq, cmd, resolve_name, via="SBMJOB")


def _parse_runsqlstm(prog, seq, stmt, resolve_name):
    srcfile = extract_param(stmt, "SRCFILE")
    srcmbr = extract_param(stmt, "SRCMBR")
    sval, sres, _ = resolve_name(srcfile)
    mval, mres, _ = resolve_name(srcmbr)
    lib, sf = _split_qualified(sval) if sval else (None, None)
    prog.calls.append(ClCall(
        seq=seq,
        called_lib=lib.upper() if lib else None,
        called_pgm=(mval or sf or "").upper() or None,
        via="RUNSQLSTM",
        params=[json.dumps({"srcfile": sf, "srclib": lib, "srcmbr": mval})],
        resolved=sres and mres,
        expr=None,
    ))


def _parse_crtdupobj(prog, seq, stmt, resolve_name):
    obj = extract_param(stmt, "OBJ")
    to = extract_param(stmt, "NEWOBJ")
    from_lib_raw = extract_param(stmt, "FROMLIB")
    oval, ores, _ = resolve_name(obj)
    tval, tres, _ = resolve_name(to)
    prog.calls.append(ClCall(
        seq=seq, called_lib=None, called_pgm=None, via="CRTDUPOBJ",
        params=[json.dumps({"obj": oval, "newobj": tval,
                            "fromlib": (resolve_name(from_lib_raw)[0])})],
        resolved=ores and tres, expr=None,
    ))


def _split_parms(parm_raw: str) -> list[str]:
    # PARM operands are space-separated tokens, possibly quoted.
    tokens = re.findall(r"'[^']*'|\S+", parm_raw)
    return [t.strip("'") for t in tokens]


# --- Orchestration -----------------------------------------------------------

def parse_all(con) -> dict[str, int]:
    from ..db import insert_rows
    from .base import load_members

    stmt_rows, ovr_rows, call_rows = [], [], []
    n = 0
    for m in load_members(con):
        if not m.is_cl():
            continue
        n += 1
        prog = parse(m)
        for s in prog.statements:
            stmt_rows.append((prog.program_id, s.seq, s.command, s.raw_text))
        for o in prog.overrides:
            ovr_rows.append((prog.program_id, o.seq, o.file, o.to_file,
                             o.to_library, o.to_member, o.scope, o.resolved,
                             o.expr))
        for c in prog.calls:
            call_rows.append((prog.program_id, c.seq, c.called_lib,
                              c.called_pgm, c.via, json.dumps(c.params),
                              c.resolved, c.expr))
    insert_rows(con, "parsed_cl_statements",
                ["program", "seq", "command", "raw_text"], stmt_rows)
    insert_rows(con, "parsed_cl_overrides",
                ["program", "seq", "file", "to_file", "to_library",
                 "to_member", "scope", "resolved", "expr"], ovr_rows)
    insert_rows(con, "parsed_cl_calls",
                ["program", "seq", "called_lib", "called_pgm", "via",
                 "params", "resolved", "expr"], call_rows)
    return {"cl_programs": n, "cl_overrides": len(ovr_rows),
            "cl_calls": len(call_rows)}
