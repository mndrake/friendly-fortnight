"""Extraction-into-DuckDB and graph-build integration tests (no host)."""
import json


def test_extract_populates_raw_layer(extracted):
    counts = {t: extracted.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("raw_systables", "raw_dsppgmref", "raw_dspdbr",
                        "raw_dspffd", "raw_source_members")}
    assert counts["raw_systables"] == 9
    assert counts["raw_dsppgmref"] == 13
    assert counts["raw_dspdbr"] == 1
    assert counts["raw_dspffd"] > 0
    assert counts["raw_source_members"] > 0


def test_source_roundtrip_check(extracted):
    from lineage.extract.source import verify_roundtrip
    assert verify_roundtrip(extracted)


def test_dsp_commands_issued(con, session, config):
    from lineage.extract import xref
    xref.harvest(session, con, config)
    assert any("DSPPGMREF PGM(APPLIB/*ALL)" in c for c in session.cl_log)
    assert any("DSPFFD FILE(APPLIB/*ALL)" in c for c in session.cl_log)
    assert any("DSPDBR FILE(APPLIB/*ALL)" in c for c in session.cl_log)


def test_phase1_graph_table_level(parsed, config):
    """Phase 1 build: xref/catalog only — coarse lineage still works."""
    from lineage.graph.build import build_graph
    from lineage.graph.resolve import backward_lineage, base_physical_files
    g = build_graph(parsed, config, phase=1)
    lineage = backward_lineage(g, "file:APPLIB/CUSTRPT")
    bases = base_physical_files(g, lineage)
    # Without CL parsing the compiled reference to ORDERS stands, and the
    # DSPDBR relation still flattens CUSTLF1 -> CUSTMAST.
    assert "file:APPLIB/CUSTMAST" in bases
    assert "file:APPLIB/ORDERS" in bases
    assert "file:APPLIB/ORDHIST" not in bases


def test_full_graph_overrides_supersede(built):
    con, g = built
    # RPT001's compiled read of ORDERS must be gone; ORDHIST read present.
    edges = con.execute(
        "SELECT dst, kind, provenance FROM edges WHERE "
        "src = 'program:APPLIB/RPT001' AND kind = 'reads'").fetchall()
    dsts = {d for d, _, _ in edges}
    assert "file:APPLIB/ORDHIST" in dsts
    assert "file:APPLIB/ORDERS" not in dsts
    prov = {d: p for d, _, p in edges}
    assert prov["file:APPLIB/ORDHIST"] == "source_cl"


def test_partial_override_keeps_both_edges(con, config):
    """A program called with AND without an override keeps both resolutions."""
    import json
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    # Minimal synthetic estate straight into the tables.
    insert_rows(con, "raw_systables",
                ["table_schema", "table_name", "system_name", "table_type"],
                [("APPLIB", "ORDERS", "ORDERS", "P"),
                 ("APPLIB", "ORDHIST", "ORDHIST", "P"),
                 ("APPLIB", "OUT1", "OUT1", "P")])
    insert_rows(con, "raw_dsppgmref",
                ["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag"],
                [("APPLIB", "RPTX", "APPLIB", "ORDERS", "F", "1"),
                 ("APPLIB", "RPTX", "APPLIB", "OUT1", "F", "2")])
    # Two callers: one overrides, one doesn't.
    insert_rows(con, "parsed_cl_overrides",
                ["program", "seq", "file", "to_file", "to_library",
                 "to_member", "scope", "resolved", "expr"],
                [("APPLIB/CLA", 1, "ORDERS", "ORDHIST", "APPLIB", None,
                  "*CALLLVL", True, None)])
    insert_rows(con, "parsed_cl_calls",
                ["program", "seq", "called_lib", "called_pgm", "via",
                 "params", "resolved", "expr"],
                [("APPLIB/CLA", 2, None, "RPTX", "CALL", json.dumps([]), True, None),
                 ("APPLIB/CLB", 1, None, "RPTX", "CALL", json.dumps([]), True, None)])
    g = build_graph(con, config, phase=2)
    reads = {v for u, v, k in g.edges(keys=True)
             if u == "program:APPLIB/RPTX"
             and g.edges[u, v, k]["kind"] == "reads"}
    assert "file:APPLIB/ORDHIST" in reads   # overridden path
    assert "file:APPLIB/ORDERS" in reads    # non-overridden path kept


def test_member_override_creates_member_node(con, config):
    import json
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    insert_rows(con, "raw_systables",
                ["table_schema", "table_name", "system_name", "table_type"],
                [("APPLIB", "SALES", "SALES", "P")])
    insert_rows(con, "raw_dsppgmref",
                ["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag"],
                [("APPLIB", "RPTM", "APPLIB", "SALES", "F", "1")])
    insert_rows(con, "parsed_cl_overrides",
                ["program", "seq", "file", "to_file", "to_library",
                 "to_member", "scope", "resolved", "expr"],
                [("APPLIB/CLM", 1, "SALES", "SALES", "APPLIB", "JAN",
                  "*CALLLVL", True, None)])
    insert_rows(con, "parsed_cl_calls",
                ["program", "seq", "called_lib", "called_pgm", "via",
                 "params", "resolved", "expr"],
                [("APPLIB/CLM", 2, None, "RPTM", "CALL", json.dumps([]),
                  True, None)])
    g = build_graph(con, config, phase=2)
    # Member-qualified node exists and derives from its base file.
    assert g.has_node("file:APPLIB/SALES(JAN)")
    kinds = {g.edges[u, v, k]["kind"]
             for u, v, k in g.out_edges("file:APPLIB/SALES(JAN)", keys=True)}
    assert "derives_from" in kinds


def test_cpyf_produces_file_to_file_edge(built):
    con, _ = built
    rows = con.execute(
        "SELECT src, dst FROM edges WHERE kind = 'derives_from' AND "
        "src = 'file:APPLIB/ORDARC'").fetchall()
    assert ("file:APPLIB/ORDARC", "file:APPLIB/ORDERS") in rows


def test_view_column_lineage_from_catalog(built):
    con, _ = built
    rows = con.execute(
        "SELECT src, dst FROM edges WHERE kind = 'derives_from' AND "
        "src LIKE 'column:APPLIB/CUSTVIEW%'").fetchall()
    assert ("column:APPLIB/CUSTVIEW.CNAME",
            "column:APPLIB/CUSTMAST.CUSTNAME") in rows


# --- Column usage from CL and RPG parsing ------------------------------------

def test_rpg_field_reference_usage_is_parsed(built):
    """RPT001's CHAIN CUSTNO CUSTLF1 names CUSTNO explicitly: the resulting
    usage edge is 'parsed', not the record-level fallback."""
    con, _ = built
    rows = con.execute(
        "SELECT dst, confidence, context FROM edges WHERE "
        "src = 'program:APPLIB/RPT001' AND kind = 'reads' AND "
        "dst LIKE 'column:%'").fetchall()
    by_dst = {d: (c, json.loads(ctx)) for d, c, ctx in rows}
    assert by_dst["column:APPLIB/ORDHIST.CUSTNO"][0] == "parsed"
    assert by_dst["column:APPLIB/ORDHIST.CUSTNO"][1]["mechanism"] == "field_reference"
    # The record's remaining fields keep the inferred record-level fallback
    # (READ loads the whole record) — a partial intersection must not
    # suppress them.
    assert by_dst["column:APPLIB/ORDHIST.AMOUNT"][0] == "inferred"
    assert by_dst["column:APPLIB/ORDHIST.AMOUNT"][1]["mechanism"] == \
        "record_io_all_fields"
    ordhist_cols = {d for d in by_dst if d.startswith("column:APPLIB/ORDHIST.")}
    assert ordhist_cols == {f"column:APPLIB/ORDHIST.{f}"
                            for f in ("CUSTNO", "ORDNO", "AMOUNT", "ORDDATE")}


def test_override_resolved_target_gets_usage_edges(built):
    """CLDRIVER overrides ORDERS -> ORDHIST around CALL RPT001: usage edges
    must land on ORDHIST columns, never the superseded ORDERS ones."""
    con, _ = built
    dsts = {d for d, in con.execute(
        "SELECT dst FROM edges WHERE src = 'program:APPLIB/RPT001' AND "
        "kind = 'reads' AND dst LIKE 'column:%'").fetchall()}
    assert any(d.startswith("column:APPLIB/ORDHIST.") for d in dsts)
    assert not any(d.startswith("column:APPLIB/ORDERS.") for d in dsts)


def test_rpg_record_io_all_fields_fallback_is_inferred(built):
    """RPT002 has no field-level evidence for ORDERS (WRITEORDSREC names a
    record format, not a field): falls back to every ORDERS field, marked
    inferred, not parsed."""
    con, _ = built
    rows = con.execute(
        "SELECT dst, confidence, context FROM edges WHERE "
        "src = 'program:APPLIB/RPT002' AND kind = 'reads' AND "
        "dst LIKE 'column:%'").fetchall()
    assert rows  # sanity: some usage edges exist
    for dst, conf, ctx in rows:
        assert conf == "inferred"
        assert json.loads(ctx)["mechanism"] == "record_io_all_fields"
    assert {d for d, _, _ in rows} == {
        "column:APPLIB/ORDERS.ORDNO", "column:APPLIB/ORDERS.CUSTNO",
        "column:APPLIB/ORDERS.AMOUNT", "column:APPLIB/ORDERS.ORDDATE"}


def test_sql_usage_edges_include_where_only_columns(built):
    """SQLEXT's WHERE AMOUNT > 0 references AMOUNT even though it is also in
    the select list; the usage edge exists regardless, distinct from the
    select-list-only column_lineage derives_from edges."""
    con, _ = built
    dsts = {d for d, in con.execute(
        "SELECT dst FROM edges WHERE src = 'program:APPLIB/SQLEXT' AND "
        "kind = 'reads' AND provenance = 'source_sql' AND "
        "dst LIKE 'column:%'").fetchall()}
    assert dsts == {"column:APPLIB/ORDERS.ORDNO", "column:APPLIB/ORDERS.AMOUNT"}


def test_cpyf_column_usage_default_is_inferred(built):
    """CLDRIVER's CPYF FROMFILE(ORDERS) TOFILE(ORDARC) carries no FMTOPT:
    the field-name intersection usage edges are 'inferred', not 'parsed'."""
    con, _ = built
    rows = con.execute(
        "SELECT dst, confidence, context FROM edges WHERE "
        "src = 'program:APPLIB/CLDRIVER' AND kind = 'reads' AND "
        "provenance = 'source_cl' AND dst LIKE 'column:APPLIB/ORDERS.%'"
    ).fetchall()
    assert rows
    for dst, conf, ctx in rows:
        assert conf == "inferred"
        assert json.loads(ctx)["mechanism"] == "cpyf_layout"


def test_cpyf_fmtopt_map_raises_confidence_to_parsed(con, config):
    """A CPYF with FMTOPT(*MAP) is confident field-name evidence: the usage
    edges on the FROMFILE columns are 'parsed', mechanism 'cpyf_map'."""
    import json as jsonmod
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    insert_rows(con, "raw_systables",
                ["table_schema", "table_name", "system_name", "table_type"],
                [("APPLIB", "SRCF", "SRCF", "P"),
                 ("APPLIB", "TGTF", "TGTF", "P")])
    insert_rows(con, "raw_dspffd",
                ["file_lib", "file_name", "record_format", "field_name"],
                [("APPLIB", "SRCF", "SRCFR", "FLDA"),
                 ("APPLIB", "SRCF", "SRCFR", "FLDB"),
                 ("APPLIB", "TGTF", "TGTFR", "FLDA"),
                 ("APPLIB", "TGTF", "TGTFR", "FLDB")])
    insert_rows(con, "parsed_cl_calls",
                ["program", "seq", "called_lib", "called_pgm", "via",
                 "params", "resolved", "expr"],
                [("APPLIB/CLMAP", 1, None, None, "CPYF",
                  jsonmod.dumps([jsonmod.dumps({
                      "from_lib": "APPLIB", "from_file": "SRCF",
                      "to_lib": "APPLIB", "to_file": "TGTF",
                      "fmtopt": "*MAP *DROP"})]),
                  True, None)])
    g = build_graph(con, config, phase=3)
    rows = con.execute(
        "SELECT dst, confidence, context FROM edges WHERE "
        "src = 'program:APPLIB/CLMAP' AND kind = 'reads' AND "
        "dst LIKE 'column:APPLIB/SRCF.%'").fetchall()
    assert {d for d, _, _ in rows} == {
        "column:APPLIB/SRCF.FLDA", "column:APPLIB/SRCF.FLDB"}
    for _, conf, ctx in rows:
        assert conf == "parsed"
        assert json.loads(ctx)["mechanism"] == "cpyf_map"
