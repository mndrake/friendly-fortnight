"""One-shot "why doesn't this table resolve" diagnostics.

Debugging column lineage on a live estate means asking the same questions
every time: does the table have catalog identity, which graph nodes carry
its name, who writes it, which parsed SQL statements touch it, do column
nodes and derives_from edges exist, are the writers classified for
record-format expansion, and what do the slice audit and gap profile say.
``lineage diagnose --table LIB/NAME`` answers all of them in one output so
a single paste shows the full picture instead of a query-at-a-time loop.

Read-only over the store; nothing here touches the host.
"""
from __future__ import annotations

from .column_trace import resolve_target


def _section(lines: list[str], title: str) -> None:
    lines.append("")
    lines.append(f"-- {title} " + "-" * max(0, 60 - len(title)))


def diagnose_table(con, table_arg: str, limit: int = 12) -> list[str]:
    lines: list[str] = []
    t = resolve_target(con, table_arg)
    lines.append(f"identity: {t.spec}  node={t.node_id}  "
                 f"in_catalog={t.in_catalog}  type={t.table_type or '?'}  "
                 f"columns={len(t.columns)}")

    # Every name this table is known by (canonical + long SQL names).
    names = {t.name.upper()}
    for (long_name,) in con.execute(
            "SELECT DISTINCT trim(table_name) FROM raw_systables "
            "WHERE upper(trim(system_name)) = ? "
            "UNION SELECT DISTINCT trim(table_name) FROM raw_syscolumns "
            "WHERE upper(trim(system_name)) = ?",
            [t.name.upper(), t.name.upper()]).fetchall():
        if long_name:
            names.add(str(long_name).upper())
    lines.append(f"known names: {', '.join(sorted(names))}")

    _section(lines, "file nodes carrying these names")
    node_ids: list[str] = []
    for nm in sorted(names):
        for (nid,) in con.execute(
                "SELECT id FROM nodes WHERE id LIKE ?",
                [f"file:%/{nm}"]).fetchall():
            if nid not in node_ids:
                node_ids.append(nid)
    if not node_ids:
        lines.append("  (none — the graph has no file node for this table)")
    for nid in node_ids:
        n_writes, n_reads = con.execute(
            "SELECT sum(CASE WHEN kind='writes' THEN 1 ELSE 0 END), "
            "sum(CASE WHEN kind='reads' THEN 1 ELSE 0 END) "
            "FROM edges WHERE dst = ?", [nid]).fetchone()
        lines.append(f"  {nid}  writers={n_writes or 0}  readers={n_reads or 0}")
        for src, prov, conf in con.execute(
                "SELECT src, provenance, confidence FROM edges "
                "WHERE dst = ? AND kind = 'writes' LIMIT ?",
                [nid, limit]).fetchall():
            lines.append(f"    <- writes: {src} ({prov}, {conf})")

    _section(lines, "parsed SQL statements mentioning the table")
    conds = " OR ".join(["upper(raw_sql) LIKE ?"] * len(names))
    params = [f"%{nm}%" for nm in sorted(names)]
    (n_stmts,) = con.execute(
        f"SELECT count(*) FROM parsed_sql_statements WHERE {conds}",
        params).fetchone()
    lines.append(f"  total: {n_stmts}")
    for program, stype, perr, sql in con.execute(
            f"SELECT program, stmt_type, parse_error IS NOT NULL, "
            f"substr(raw_sql, 1, 90) FROM parsed_sql_statements "
            f"WHERE {conds} LIMIT ?", params + [limit]).fetchall():
        err = " [parse_error]" if perr else ""
        lines.append(f"  {program} {stype}{err}: {' '.join(str(sql).split())}")

    _section(lines, "column nodes / derives_from edges")
    (n_cols,) = con.execute(
        "SELECT count(*) FROM nodes WHERE id LIKE ?",
        [f"column:{t.spec}.%"]).fetchone()
    (n_derives,) = con.execute(
        "SELECT count(*) FROM edges WHERE kind = 'derives_from' "
        "AND src LIKE ?", [f"column:{t.spec}.%"]).fetchone()
    (n_into,) = con.execute(
        "SELECT count(*) FROM edges WHERE kind = 'derives_from' "
        "AND dst LIKE ?", [f"column:{t.spec}.%"]).fetchone()
    lines.append(f"  column nodes: {n_cols}   derives out: {n_derives}   "
                 f"derives in: {n_into}")
    for src, dst in con.execute(
            "SELECT src, dst FROM edges WHERE kind = 'derives_from' "
            "AND src LIKE ? LIMIT 5", [f"column:{t.spec}.%"]).fetchall():
        lines.append(f"  {src} -> {dst}")

    _section(lines, "writer program classification")
    writers = {src.split(":", 1)[1] for nid in node_ids
               for (src,) in con.execute(
                   "SELECT src FROM edges WHERE dst = ? AND kind = 'writes'",
                   [nid]).fetchall() if src.startswith("program:")}
    if not writers:
        lines.append("  (no writer programs in the graph)")
    for w in sorted(writers)[:limit]:
        row = con.execute(
            "SELECT program_class FROM program_classification "
            "WHERE upper(program) = ? OR upper(program) LIKE ?",
            [w.upper(), f"%/{w.split('/')[-1].upper()}"]).fetchone()
        lines.append(f"  {w}: {row[0] if row else 'unclassified'}")

    _section(lines, "slice audit for these names")
    rows = con.execute(
        "SELECT kind, library, name, round, reason, source_ref "
        "FROM slice_objects WHERE upper(name) IN ({})".format(
            ", ".join("?" * len(names))), sorted(names)).fetchall()
    if not rows:
        lines.append("  (not in slice_objects)")
    for kind, lib, nm, rnd, reason, ref in rows[:limit]:
        lines.append(f"  {kind} {lib}/{nm} round={rnd} reason={reason}"
                     + (f" source={ref}" if ref else ""))

    _section(lines, "gap profile (whole store)")
    for kind, cnt in con.execute(
            "SELECT kind, count(*) FROM gaps GROUP BY 1 "
            "ORDER BY 2 DESC").fetchall():
        lines.append(f"  {kind}: {cnt}")
    _section(lines, f"gaps mentioning {t.name}")
    gconds = " OR ".join(["upper(object_id) LIKE ? OR upper(detail) LIKE ?"]
                         * len(names))
    gparams: list[str] = []
    for nm in sorted(names):
        gparams += [f"%{nm}%", f"%{nm}%"]
    grows = con.execute(
        f"SELECT kind, object_id, substr(detail, 1, 90) FROM gaps "
        f"WHERE {gconds} LIMIT ?", gparams + [limit]).fetchall()
    if not grows:
        lines.append("  (none)")
    for kind, obj, detail in grows:
        lines.append(f"  {kind} {obj}: {detail}")
    return lines
