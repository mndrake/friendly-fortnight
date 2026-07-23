"""Embedded SQL analysis via sqlglot.

Consumes SQL text extracted from SQLRPG(LE) members (``_rpg_sql_blocks``) and
RUNSQLSTM source members, and derives per-statement:

* table read/write sets (``lib/table`` strings, unqualified when no schema);
* column-level lineage for SELECT / INSERT-SELECT / CREATE VIEW, with explicit
  handling for INSERT VALUES, UPDATE SET, and MERGE;
* dynamic-SQL markers (PREPARE / EXECUTE IMMEDIATE) recorded as unresolved.

Parse failures are recorded per statement and are never fatal (plan: "Parse
failures recorded per statement, never fatal").
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

import sqlglot
from sqlglot import exp


def _pick_dialect() -> str | None:
    """Prefer a real db2 dialect if this sqlglot has one; otherwise the
    generic dialect (DB2 for i SQL is close to standard SQL once DB2-isms
    are stripped by :func:`_strip_db2isms`).
    """
    try:
        sqlglot.parse_one("SELECT 1", read="db2")
        return "db2"
    except Exception:  # noqa: BLE001 - Unknown dialect
        return None


DIALECT = _pick_dialect()

# DB2 for i clauses the generic dialect chokes on; all are lineage-neutral.
_DB2ISM_RES = [
    re.compile(r"\bWITH\s+(?:UR|CS|RS|RR)\b", re.IGNORECASE),
    re.compile(r"\bFOR\s+(?:READ|FETCH)\s+ONLY\b", re.IGNORECASE),
    re.compile(r"\bOPTIMIZE\s+FOR\s+\d+\s+ROWS?\b", re.IGNORECASE),
    re.compile(r"\bWITH\s+NC\b", re.IGNORECASE),
    re.compile(r"\bSKIP\s+LOCKED\s+DATA\b", re.IGNORECASE),
]


def _strip_db2isms(sql: str) -> str:
    for rx in _DB2ISM_RES:
        sql = rx.sub(" ", sql)
    return sql

# Statements that carry no lineage but appear in embedded SQL constantly.
_NOISE_RE = re.compile(
    r"^\s*(?:DECLARE\s+\w+\s+CURSOR|OPEN\b|CLOSE\b|FETCH\b|COMMIT\b|"
    r"ROLLBACK\b|WHENEVER\b|SET\s+OPTION\b|INCLUDE\b|BEGIN\s+DECLARE|"
    r"END\s+DECLARE)",
    re.IGNORECASE,
)
_DYNAMIC_RE = re.compile(r"^\s*(?:PREPARE|EXECUTE\s+IMMEDIATE|EXECUTE)\b",
                         re.IGNORECASE)
_CURSOR_SELECT_RE = re.compile(
    r"^\s*DECLARE\s+\w+\s+(?:\w+\s+)*CURSOR\s+(?:WITH\s+\w+\s+)*FOR\s+(.*)$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class SqlAnalysis:
    stmt_type: str
    tables_read: list[str] = field(default_factory=list)
    tables_written: list[str] = field(default_factory=list)
    column_lineage: list[dict] = field(default_factory=list)  # {target, sources}
    ast_json: Optional[str] = None
    parse_error: Optional[str] = None
    is_dynamic: bool = False


def _table_name(t: exp.Table) -> str:
    if t.db:
        return f"{t.db.upper()}/{t.name.upper()}"
    return t.name.upper()


def _collect_cte_names(root: exp.Expression) -> set[str]:
    return {cte.alias_or_name.upper() for cte in root.find_all(exp.CTE)}


def _source_columns(expr: exp.Expression, alias_map: dict[str, str],
                    default_tables: list[str]) -> list[str]:
    """Column references inside ``expr`` as ``table.column`` strings."""
    out = []
    for col in expr.find_all(exp.Column):
        tbl = col.table.upper() if col.table else None
        if tbl and tbl in alias_map:
            tbl = alias_map[tbl]
        if tbl is None and len(default_tables) == 1:
            tbl = default_tables[0]
        colname = col.name.upper()
        out.append(f"{tbl}.{colname}" if tbl else colname)
    return out


def _alias_map(root: exp.Expression, cte_names: set[str]) -> dict[str, str]:
    amap: dict[str, str] = {}
    for t in root.find_all(exp.Table):
        name = _table_name(t)
        if t.name.upper() in cte_names:
            continue
        alias = t.alias
        if alias:
            amap[alias.upper()] = name
        amap.setdefault(t.name.upper(), name)
    return amap


def analyze_statement(sql: str) -> SqlAnalysis:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        return SqlAnalysis(stmt_type="EMPTY")

    if _DYNAMIC_RE.match(sql):
        return SqlAnalysis(stmt_type="DYNAMIC", is_dynamic=True,
                           parse_error=None)

    # DECLARE CURSOR FOR <select>: analyze the inner select.
    mcur = _CURSOR_SELECT_RE.match(sql)
    if mcur:
        inner = analyze_statement(mcur.group(1))
        inner.stmt_type = "CURSOR_SELECT"
        return inner

    if _NOISE_RE.match(sql):
        return SqlAnalysis(stmt_type="NOISE")

    try:
        tree = sqlglot.parse_one(_strip_db2isms(sql), read=DIALECT)
    except Exception as excinfo:  # noqa: BLE001 - recorded, never fatal
        return SqlAnalysis(stmt_type="PARSE_ERROR", parse_error=str(excinfo))

    return _analyze_tree(tree)


def _analyze_tree(tree: exp.Expression) -> SqlAnalysis:
    cte_names = _collect_cte_names(tree)
    amap = _alias_map(tree, cte_names)

    def real_tables(node: exp.Expression) -> list[str]:
        names = []
        for t in node.find_all(exp.Table):
            if t.name.upper() in cte_names:
                continue
            nm = _table_name(t)
            if nm not in names:
                names.append(nm)
        return names

    analysis = SqlAnalysis(stmt_type=type(tree).__name__.upper())
    try:
        analysis.ast_json = tree.sql(dialect=DIALECT)
    except Exception:  # noqa: BLE001
        analysis.ast_json = None

    if isinstance(tree, exp.Select):
        analysis.stmt_type = "SELECT"
        analysis.tables_read = real_tables(tree)
        analysis.column_lineage = _select_lineage(tree, amap, analysis.tables_read)

    elif isinstance(tree, exp.Insert):
        analysis.stmt_type = "INSERT"
        target = tree.this
        tname = None
        if isinstance(target, exp.Schema):
            tbl = target.this
            tname = _table_name(tbl) if isinstance(tbl, exp.Table) else None
            insert_cols = [c.name.upper() for c in target.expressions
                           if isinstance(c, (exp.Column, exp.Identifier))]
        elif isinstance(target, exp.Table):
            tname = _table_name(target)
            insert_cols = []
        else:
            insert_cols = []
        if tname:
            analysis.tables_written = [tname]
        select = tree.find(exp.Select)
        if select is not None:
            analysis.tables_read = real_tables(select)
            sel_lineage = _select_lineage(select, _alias_map(select, cte_names),
                                          analysis.tables_read)
            # Map select outputs to insert column list positionally.
            if insert_cols and len(insert_cols) == len(sel_lineage):
                for tgt_col, item in zip(insert_cols, sel_lineage):
                    analysis.column_lineage.append({
                        "target": f"{tname}.{tgt_col}",
                        "sources": item["sources"],
                    })
            else:
                for item in sel_lineage:
                    analysis.column_lineage.append({
                        "target": f"{tname}.{item['target']}" if tname else item["target"],
                        "sources": item["sources"],
                    })
        # INSERT ... VALUES: no table sources; host-variable-fed columns.
        elif insert_cols and tname:
            for c in insert_cols:
                analysis.column_lineage.append(
                    {"target": f"{tname}.{c}", "sources": []})

    elif isinstance(tree, exp.Update):
        analysis.stmt_type = "UPDATE"
        target = tree.this
        tname = _table_name(target) if isinstance(target, exp.Table) else None
        if tname:
            analysis.tables_written = [tname]
        reads = [t for t in real_tables(tree) if t != tname]
        analysis.tables_read = reads
        for setexpr in tree.expressions:
            if isinstance(setexpr, exp.EQ):
                tgt = setexpr.this
                tgt_name = tgt.name.upper() if isinstance(tgt, exp.Column) else str(tgt)
                sources = _source_columns(setexpr.expression, amap,
                                          reads or ([tname] if tname else []))
                analysis.column_lineage.append({
                    "target": f"{tname}.{tgt_name}" if tname else tgt_name,
                    "sources": sources,
                })

    elif isinstance(tree, exp.Delete):
        analysis.stmt_type = "DELETE"
        target = tree.this
        if isinstance(target, exp.Table):
            analysis.tables_written = [_table_name(target)]

    elif isinstance(tree, exp.Merge):
        analysis.stmt_type = "MERGE"
        target = tree.this
        tname = None
        if isinstance(target, (exp.Table, exp.Alias)):
            tbl = target if isinstance(target, exp.Table) else target.this
            if isinstance(tbl, exp.Table):
                tname = _table_name(tbl)
        if tname:
            analysis.tables_written = [tname]
        using = tree.args.get("using")
        if using is not None:
            analysis.tables_read = real_tables(using)
            # USING may be a subquery: its inner selects count as reads too.
        for when in tree.find_all(exp.When):
            then = when.args.get("then")
            if then is None:
                continue
            for setexpr in then.find_all(exp.EQ):
                tgt = setexpr.this
                if not isinstance(tgt, exp.Column):
                    continue
                sources = _source_columns(setexpr.expression, amap,
                                          analysis.tables_read)
                analysis.column_lineage.append({
                    "target": f"{tname}.{tgt.name.upper()}" if tname else tgt.name.upper(),
                    "sources": sources,
                })

    elif isinstance(tree, exp.Create):
        kind = (tree.kind or "").upper()
        analysis.stmt_type = f"CREATE_{kind}" if kind else "CREATE"
        target = tree.this
        tname = None
        if isinstance(target, exp.Schema) and isinstance(target.this, exp.Table):
            tname = _table_name(target.this)
        elif isinstance(target, exp.Table):
            tname = _table_name(target)
        if tname:
            analysis.tables_written = [tname]
        select = tree.find(exp.Select)
        if select is not None:
            analysis.tables_read = [t for t in real_tables(select) if t != tname]
            sel_lineage = _select_lineage(select, _alias_map(select, cte_names),
                                          analysis.tables_read)
            for item in sel_lineage:
                analysis.column_lineage.append({
                    "target": f"{tname}.{item['target']}" if tname else item["target"],
                    "sources": item["sources"],
                })

    else:
        # Fallback: any tables found count as reads.
        analysis.tables_read = real_tables(tree)

    return analysis


def _select_lineage(select: exp.Select, amap: dict[str, str],
                    tables_read: list[str]) -> list[dict]:
    """Per output column of ``select``: name and contributing source columns."""
    out = []
    for proj in select.expressions:
        if isinstance(proj, exp.Alias):
            target = proj.alias.upper()
            sources = _source_columns(proj.this, amap, tables_read)
        elif isinstance(proj, exp.Column):
            target = proj.name.upper()
            sources = _source_columns(proj, amap, tables_read)
        elif isinstance(proj, exp.Star):
            target = "*"
            sources = [f"{t}.*" for t in tables_read]
        else:
            target = proj.sql().upper()[:64]
            sources = _source_columns(proj, amap, tables_read)
        out.append({"target": target, "sources": sources})
    return out


def split_sql_script(text: str) -> list[str]:
    """Split a RUNSQLSTM-style script into statements on ';' outside quotes."""
    stmts, buf, in_str = [], [], False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "'":
            in_str = not in_str
            buf.append(c)
        elif c == ";" and not in_str:
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
        elif c == "-" and not in_str and i + 1 < len(text) and text[i + 1] == "-":
            # line comment
            while i < len(text) and text[i] != "\n":
                i += 1
            buf.append("\n")
        else:
            buf.append(c)
        i += 1
    s = "".join(buf).strip()
    if s:
        stmts.append(s)
    return stmts


# --- Orchestration -----------------------------------------------------------

def parse_all(con) -> dict[str, int]:
    """Analyze all pending embedded SQL blocks and RUNSQLSTM members."""
    from ..db import insert_rows
    from .base import load_members

    rows = []
    n_err = 0

    # 1. Embedded SQL blocks stashed by the RPG parser.
    try:
        blocks = con.execute(
            "SELECT program, seq, raw_sql FROM _rpg_sql_blocks ORDER BY program, seq"
        ).fetchall()
    except Exception:  # noqa: BLE001 - temp table absent if rpg step skipped
        blocks = []
    for program, seq, raw_sql in blocks:
        a = analyze_statement(raw_sql)
        if a.stmt_type in {"EMPTY", "NOISE"}:
            continue
        if a.parse_error:
            n_err += 1
        rows.append((program, seq, a.stmt_type, a.ast_json,
                     json.dumps(a.tables_read), json.dumps(a.tables_written),
                     json.dumps(a.column_lineage), a.parse_error, raw_sql))

    # 2. RUNSQLSTM source members referenced from CL.
    members = {m.member.upper(): m for m in load_members(con)}
    runsql = con.execute(
        "SELECT program, seq, params FROM parsed_cl_calls WHERE via = 'RUNSQLSTM'"
    ).fetchall()
    for cl_program, seq, params_json in runsql:
        try:
            info = json.loads(json.loads(params_json)[0])
        except (ValueError, IndexError, TypeError):
            continue
        mbr = (info.get("srcmbr") or "").upper()
        target = members.get(mbr)
        if target is None:
            continue
        for j, stmt in enumerate(split_sql_script(target.text)):
            a = analyze_statement(stmt)
            if a.stmt_type in {"EMPTY", "NOISE"}:
                continue
            if a.parse_error:
                n_err += 1
            rows.append((f"{cl_program}#RUNSQLSTM:{mbr}", seq * 1000 + j,
                         a.stmt_type, a.ast_json,
                         json.dumps(a.tables_read), json.dumps(a.tables_written),
                         json.dumps(a.column_lineage), a.parse_error, stmt))

    insert_rows(con, "parsed_sql_statements",
                ["program", "seq", "stmt_type", "ast_json", "tables_read",
                 "tables_written", "column_lineage", "parse_error", "raw_sql"],
                rows)
    return {"sql_statements": len(rows), "sql_parse_errors": n_err}
