"""Extraction-into-DuckDB and graph-build integration tests (no host)."""


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
