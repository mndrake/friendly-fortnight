import pytest

from lineage.graph.model import (Confidence, Edge, EdgeKind, Provenance,
                                 column_id, file_id, min_confidence,
                                 program_id)


def test_edge_requires_provenance_and_confidence():
    # Property: no edge without provenance — construction without it fails.
    with pytest.raises(TypeError):
        Edge(src="a", dst="b", kind=EdgeKind.READS)  # type: ignore[call-arg]


def test_confidence_ordering():
    assert min_confidence(Confidence.CONFIRMED, Confidence.PARSED) == Confidence.PARSED
    assert min_confidence(Confidence.PARSED, Confidence.UNRESOLVED) == Confidence.UNRESOLVED
    assert min_confidence(Confidence.INFERRED, Confidence.INFERRED) == Confidence.INFERRED
    ranks = [Confidence.UNRESOLVED, Confidence.INFERRED, Confidence.PARSED,
             Confidence.CONFIRMED]
    assert sorted(ranks, key=lambda c: c.rank) == ranks


def test_node_ids_normalised():
    assert file_id("applib", "orders") == "file:APPLIB/ORDERS"
    assert file_id(None, "orders") == "file:*LIBL/ORDERS"
    assert file_id("L", "F", member="jan") == "file:L/F(JAN)"
    assert program_id("l", "p") == "program:L/P"
    assert column_id("l", "f", "c") == "column:L/F.C"


def test_edge_dedup_key_ignores_context():
    e1 = Edge(src="a", dst="b", kind=EdgeKind.READS,
              provenance=Provenance.XREF, confidence=Confidence.CONFIRMED,
              context={"x": 1})
    e2 = Edge(src="a", dst="b", kind=EdgeKind.READS,
              provenance=Provenance.XREF, confidence=Confidence.CONFIRMED,
              context={"y": 2})
    assert e1.key() == e2.key()


def test_all_persisted_edges_have_provenance(built):
    con, _ = built
    rows = con.execute(
        "SELECT count(*) FROM edges WHERE provenance IS NULL OR "
        "confidence IS NULL").fetchone()
    assert rows[0] == 0
    total = con.execute("SELECT count(*) FROM edges").fetchone()[0]
    assert total > 0


# --- Canonical names: long SQL name and system name are ONE node ---------------

def test_long_and_system_names_resolve_to_one_node():
    """A DDL member writes EDS_BROKER_BARGAIN_EVENING; DSPPGMREF/RPG know the
    same table as BROAST. Without canonicalization the graph held two
    disconnected tables and trace-columns found no lineage (live estate)."""
    import json as jsonmod

    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    con = dbmod.connect(None)
    insert_rows(con, "raw_systables",
                ["table_schema", "table_name", "system_name", "table_type",
                 "file_type", "row_count", "long_comment"],
                [("DTALIB", "EDS_BROKER_BARGAIN_EVENING", "BROAST", "T",
                  "D", 1, None)])
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"],
                [("DTALIB", "EDS_BROKER_BARGAIN_EVENING", "BROAST",
                  "RECORD_TYPE_CODE", "CACINM", 1, "CHAR", 1, None, "N", "x"),
                 ("DTALIB", "SOURCE_TXN_TABLE", "SRCTXN",
                  "TYPE_CODE", "ATYPE", 1, "CHAR", 1, None, "N", "x")])
    # A parsed statement written entirely in long SQL names.
    con.execute(
        "INSERT INTO parsed_sql_statements (program, seq, stmt_type, "
        "ast_json, tables_read, tables_written, column_lineage, columns_used, "
        "parse_error, raw_sql) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ["TNTACCSRC/BROAST#SQLMBR:QDDLSRC", 0, "INSERT", None,
         jsonmod.dumps(["DTALIB/SOURCE_TXN_TABLE"]),
         jsonmod.dumps(["DTALIB/EDS_BROKER_BARGAIN_EVENING"]),
         jsonmod.dumps([{"target":
                         "DTALIB/EDS_BROKER_BARGAIN_EVENING.RECORD_TYPE_CODE",
                         "sources": ["DTALIB/SOURCE_TXN_TABLE.TYPE_CODE"]}]),
         jsonmod.dumps(["DTALIB/SOURCE_TXN_TABLE.TYPE_CODE"]), None,
         "INSERT INTO ..."])
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["DTALIB"],
        "output_seeds": [{"id": "B", "library": "DTALIB", "file": "BROAST"}],
        "liblists": {"default": ["DTALIB"]},
    })
    g = build_graph(con, config, phase=3)

    # One node under the system name; the long name minted nothing.
    assert g.has_node("file:DTALIB/BROAST")
    assert not g.has_node("file:DTALIB/EDS_BROKER_BARGAIN_EVENING")
    writes = [(u, v) for u, v, d in g.edges(data=True)
              if d.get("kind") == "writes"]
    assert ("program:TNTACCSRC/BROAST", "file:DTALIB/BROAST") in writes
    # Column lineage canonicalized at both ends: system column names, on
    # system-named tables.
    derives = [(u, v) for u, v, d in g.edges(data=True)
               if d.get("kind") == "derives_from"]
    assert ("column:DTALIB/BROAST.CACINM",
            "column:DTALIB/SRCTXN.ATYPE") in derives
    con.close()


def test_record_expansion_gates_per_file_not_per_program():
    """One program-described work-file F-spec must no longer disqualify the
    whole program (live XBRD case): externally described files still expand
    by same-named fields; the byte-buffer file contributes nothing."""
    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    con = dbmod.connect(None)
    col_rows = []
    for tbl in ("OUTF", "INF", "WRKF"):
        for i, c in enumerate(("F1", "F2")):
            col_rows.append(("APPX", tbl, tbl, c, c, i + 1, "CHAR", 10,
                             None, "N", c))
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"], col_rows)
    insert_rows(con, "raw_dsppgmref",
                ["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
                [("APPX", "MIXPGM", "APPX", "INF", "F", "1", 1),
                 ("APPX", "MIXPGM", "APPX", "OUTF", "F", "2", 1),
                 ("APPX", "MIXPGM", "APPX", "WRKF", "F", "2", 1)])
    insert_rows(con, "parsed_rpg_files",
                ["program", "file", "usage", "extname", "rename_rec",
                 "declared_via", "program_described"],
                [("APPX/MIXPGM", "INF", "input", None, None, "fspec", False),
                 ("APPX/MIXPGM", "OUTF", "output", None, None, "fspec", False),
                 ("APPX/MIXPGM", "WRKF", "output", None, None, "fspec", True)])
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["APPX"],
        "output_seeds": [{"id": "O", "library": "APPX", "file": "OUTF"}],
        "liblists": {"default": ["APPX"]},
    })
    g = build_graph(con, config, phase=3)
    derives = [(u, v) for u, v, d in g.edges(data=True)
               if d.get("kind") == "derives_from"]
    # Externally described pair expands despite the program-described sibling
    # (and despite MIXPGM having no classification row at all).
    assert ("column:APPX/OUTF.F1", "column:APPX/INF.F1") in derives
    assert ("column:APPX/OUTF.F2", "column:APPX/INF.F2") in derives
    # The byte-buffer file stays out of name matching entirely.
    assert not any("WRKF" in u or "WRKF" in v for u, v in derives)
    con.close()


def test_lf_passthrough_bridges_unparsed_logicals():
    """A logical with no parsed DDS (source never retrieved) must still pass
    its columns through to the base physical via DSPDBR + shared catalog
    fields — a trace stopping at EITFDTA# as 'base' was exactly wrong."""
    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    con = dbmod.connect(None)
    cols = []
    for tbl in ("EITFDTA", "EITFDTA#"):
        for i, c in enumerate(("YETFDL", "METFDL")):
            cols.append(("DTAL", tbl, tbl, c, c, i + 1, "CHAR", 8, None,
                         "N", c))
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"], cols)
    insert_rows(con, "raw_dspdbr",
                ["dep_lib", "dep_file", "based_lib", "based_file",
                 "dep_type"],
                [("DTAL", "EITFDTA#", "DTAL", "EITFDTA", "D")])
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["DTAL"],
        "output_seeds": [{"id": "X", "library": "DTAL", "file": "EITFDTA#"}],
        "liblists": {"default": ["DTAL"]},
    })
    g = build_graph(con, config, phase=3)
    derives = [(u, v, d) for u, v, d in g.edges(data=True)
               if d.get("kind") == "derives_from"]
    assert any(u == "column:DTAL/EITFDTA#.YETFDL"
               and v == "column:DTAL/EITFDTA.YETFDL"
               and d.get("context", {}).get("mechanism") ==
               "lf_field_passthrough"
               for u, v, d in derives)
    con.close()


def test_lf_passthrough_defers_to_parsed_dds():
    """A logical whose DDS was parsed (rename-aware evidence) must NOT get
    synthesized same-name passthrough edges layered on top."""
    import json as jsonmod

    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    con = dbmod.connect(None)
    cols = []
    for tbl in ("BASEPF", "RENLF"):
        cols.append(("DTAL", tbl, tbl, "CNAME", "CNAME", 1, "CHAR", 8,
                     None, "N", "x"))
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"], cols)
    insert_rows(con, "raw_dspdbr",
                ["dep_lib", "dep_file", "based_lib", "based_file",
                 "dep_type"],
                [("DTAL", "RENLF", "DTAL", "BASEPF", "D")])
    insert_rows(con, "parsed_dds_files",
                ["library", "file", "dds_type", "record_format", "based_on",
                 "is_join"],
                [("DTAL", "RENLF", "LF", "RENLFR",
                  jsonmod.dumps(["BASEPF"]), False)])
    insert_rows(con, "parsed_dds_fields",
                ["library", "file", "record_format", "field_name",
                 "renamed_from", "ref_field", "ref_file", "concat_fields",
                 "usage"],
                [("DTAL", "RENLF", "RENLFR", "CNAME", "CUSTNAME",
                  "CUSTNAME", "BASEPF", None, "B")])
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["DTAL"],
        "output_seeds": [{"id": "X", "library": "DTAL", "file": "RENLF"}],
        "liblists": {"default": ["DTAL"]},
    })
    g = build_graph(con, config, phase=3)
    mechs = {d.get("context", {}).get("mechanism")
             for u, v, d in g.edges(data=True)
             if d.get("kind") == "derives_from"
             and u.startswith("column:DTAL/RENLF.")}
    assert "lf_field_passthrough" not in mechs   # parsed DDS won
    con.close()


def test_move_chain_resolves_program_described_output():
    """Fully program-described I/O (byte buffers to DSPFFD) resolves at
    parsed confidence through O-specs + I-specs + the C-spec move chain:
    OFLD1 <- MOVEL WFLD1 <- MOVE FLD1 <- READ LEGACY. Catalog columns of a
    program-described *read* file are not program variables — only the
    I-spec fields count."""
    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    con = dbmod.connect(None)
    cols = []
    for tbl, names in (("LEGACY", ("FLD1", "FLD2", "EXTRA")),
                       ("LEGOUT", ("OFLD1", "OFLD2"))):
        for i, c in enumerate(names):
            cols.append(("APPX", tbl, tbl, c, c, i + 1, "CHAR", 10, None,
                         "N", c))
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"], cols)
    insert_rows(con, "raw_dsppgmref",
                ["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
                [("APPX", "PDPGM", "APPX", "LEGACY", "F", "1", 1),
                 ("APPX", "PDPGM", "APPX", "LEGOUT", "F", "2", 1)])
    insert_rows(con, "parsed_rpg_files",
                ["program", "file", "usage", "extname", "rename_rec",
                 "declared_via", "program_described"],
                [("APPX/PDPGM", "LEGACY", "input", None, None, "fspec", True),
                 ("APPX/PDPGM", "LEGOUT", "output", None, None, "fspec", True)])
    insert_rows(con, "parsed_rpg_ospec_fields",
                ["program", "file", "field_name", "end_pos"],
                [("APPX/PDPGM", "LEGOUT", "OFLD1", 6),
                 ("APPX/PDPGM", "LEGOUT", "OFLD2", 36)])
    insert_rows(con, "parsed_rpg_ispec_fields",
                ["program", "file", "field_name", "ext_name", "from_pos",
                 "to_pos"],
                [("APPX/PDPGM", "LEGACY", "FLD1", None, 1, 6),
                 ("APPX/PDPGM", "LEGACY", "FLD2", None, 7, 36)])
    insert_rows(con, "parsed_rpg_moves",
                ["program", "seq", "opcode", "source_field", "result_field"],
                [("APPX/PDPGM", 1, "MOVE", "FLD1", "WFLD1"),
                 ("APPX/PDPGM", 2, "MOVEL", "WFLD1", "OFLD1"),
                 ("APPX/PDPGM", 3, "MOVEL", "FLD2", "OFLD2")])
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["APPX"],
        "output_seeds": [{"id": "O", "library": "APPX", "file": "LEGOUT"}],
        "liblists": {"default": ["APPX"]},
    })
    g = build_graph(con, config, phase=3)
    derives = {(u, v): d for u, v, d in g.edges(data=True)
               if d.get("kind") == "derives_from"}
    d1 = derives[("column:APPX/LEGOUT.OFLD1", "column:APPX/LEGACY.FLD1")]
    assert d1["confidence"] == "parsed"
    assert d1["context"]["mechanism"] == "cspec_move_chain"
    assert d1["context"]["via"] == "WFLD1"
    d2 = derives[("column:APPX/LEGOUT.OFLD2", "column:APPX/LEGACY.FLD2")]
    assert d2["confidence"] == "parsed"
    assert "via" not in d2["context"]          # single-hop move, no relay
    # EXTRA is a catalog column of the byte-buffer read file, not an I-spec
    # field: it must not appear as a source of anything.
    assert not any("EXTRA" in v for _, v in derives)
    con.close()


def test_ospec_limits_output_fields_and_prunes_constant_fed():
    """With O-specs present, only O-spec fields get lineage: a same-named
    column the program never outputs gets no edge, a constant-fed field is
    pruned with an explicit gap, and an O-spec field with no matching
    output column gaps as unmapped."""
    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    con = dbmod.connect(None)
    cols = []
    for tbl in ("OUTF", "INF"):
        for i, c in enumerate(("F1", "F2", "F3")):
            cols.append(("APPX", tbl, tbl, c, c, i + 1, "CHAR", 10, None,
                         "N", c))
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"], cols)
    insert_rows(con, "raw_dsppgmref",
                ["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
                [("APPX", "XPGM", "APPX", "INF", "F", "1", 1),
                 ("APPX", "XPGM", "APPX", "OUTF", "F", "2", 1)])
    insert_rows(con, "parsed_rpg_files",
                ["program", "file", "usage", "extname", "rename_rec",
                 "declared_via", "program_described"],
                [("APPX/XPGM", "INF", "input", None, None, "fspec", False),
                 ("APPX/XPGM", "OUTF", "output", None, None, "fspec", False)])
    insert_rows(con, "parsed_rpg_ospec_fields",
                ["program", "file", "field_name", "end_pos"],
                [("APPX/XPGM", "OUTF", "F1", 10),
                 ("APPX/XPGM", "OUTF", "F2", 20),
                 ("APPX/XPGM", "OUTF", "BADFLD", 30)])
    insert_rows(con, "parsed_rpg_moves",
                ["program", "seq", "opcode", "source_field", "result_field"],
                [("APPX/XPGM", 1, "Z-ADD", None, "F2")])   # constant-fed
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["APPX"],
        "output_seeds": [{"id": "O", "library": "APPX", "file": "OUTF"}],
        "liblists": {"default": ["APPX"]},
    })
    g = build_graph(con, config, phase=3)
    derives = {(u, v): d for u, v, d in g.edges(data=True)
               if d.get("kind") == "derives_from"}
    d1 = derives[("column:APPX/OUTF.F1", "column:APPX/INF.F1")]
    assert d1["confidence"] == "parsed"
    assert d1["context"]["mechanism"] == "ospec_field"
    # F2 is assigned a literal: no same-name guess, an explicit gap instead.
    assert ("column:APPX/OUTF.F2", "column:APPX/INF.F2") not in derives
    # F3 exists in both files but is not in the O-specs: never written.
    assert ("column:APPX/OUTF.F3", "column:APPX/INF.F3") not in derives
    gaps = {(k, o) for k, o in con.execute(
        "SELECT kind, object_id FROM gaps").fetchall()}
    assert ("rpg_untraced_output_field", "column:APPX/OUTF.F2") in gaps
    assert ("ospec_field_unmapped", "column:APPX/OUTF.BADFLD") in gaps
    con.close()


def test_unique_read_through_promoted_ambiguous_stays_inferred():
    """Without O-specs (externally described write), an unassigned output
    field in exactly one read file is definitive (parsed); one present in
    several read files stays inferred, marked with the ambiguity count."""
    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    con = dbmod.connect(None)
    cols = []
    for tbl, names in (("OUTF", ("SOLO", "BOTH")),
                       ("INA", ("SOLO", "BOTH")), ("INB", ("BOTH",))):
        for i, c in enumerate(names):
            cols.append(("APPX", tbl, tbl, c, c, i + 1, "CHAR", 10, None,
                         "N", c))
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"], cols)
    insert_rows(con, "raw_dsppgmref",
                ["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
                [("APPX", "RPGM", "APPX", "INA", "F", "1", 1),
                 ("APPX", "RPGM", "APPX", "INB", "F", "1", 1),
                 ("APPX", "RPGM", "APPX", "OUTF", "F", "2", 1)])
    insert_rows(con, "parsed_rpg_files",
                ["program", "file", "usage", "extname", "rename_rec",
                 "declared_via", "program_described"],
                [("APPX/RPGM", "INA", "input", None, None, "fspec", False),
                 ("APPX/RPGM", "INB", "input", None, None, "fspec", False),
                 ("APPX/RPGM", "OUTF", "output", None, None, "fspec", False)])
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["APPX"],
        "output_seeds": [{"id": "O", "library": "APPX", "file": "OUTF"}],
        "liblists": {"default": ["APPX"]},
    })
    g = build_graph(con, config, phase=3)
    derives = {(u, v): d for u, v, d in g.edges(data=True)
               if d.get("kind") == "derives_from"}
    solo = derives[("column:APPX/OUTF.SOLO", "column:APPX/INA.SOLO")]
    assert solo["confidence"] == "parsed"
    assert solo["context"]["mechanism"] == "record_read_through"
    both_a = derives[("column:APPX/OUTF.BOTH", "column:APPX/INA.BOTH")]
    both_b = derives[("column:APPX/OUTF.BOTH", "column:APPX/INB.BOTH")]
    for d in (both_a, both_b):
        assert d["confidence"] == "inferred"
        assert d["context"]["ambiguous_files"] == 2
    con.close()


def test_lf_passthrough_gate_is_per_column():
    """A partially parsed DDS (rename evidence for one field) must not
    suppress passthrough for the OTHER fields — they'd strand at the
    logical as if it were a base file (live ASSET# case)."""
    import json as jsonmod

    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    con = dbmod.connect(None)
    cols = []
    for tbl in ("BASEPF", "PARTLF"):
        for i, c in enumerate(("CNAME", "QCONSC")):
            cols.append(("DTAL", tbl, tbl, c, c, i + 1, "CHAR", 8, None,
                         "N", "x"))
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"], cols)
    insert_rows(con, "raw_dspdbr",
                ["dep_lib", "dep_file", "based_lib", "based_file",
                 "dep_type"],
                [("DTAL", "PARTLF", "DTAL", "BASEPF", "D")])
    insert_rows(con, "parsed_dds_files",
                ["library", "file", "dds_type", "record_format", "based_on",
                 "is_join"],
                [("DTAL", "PARTLF", "LF", "PARTLFR",
                  jsonmod.dumps(["BASEPF"]), False)])
    # Parsed DDS evidence exists for CNAME only.
    insert_rows(con, "parsed_dds_fields",
                ["library", "file", "record_format", "field_name",
                 "renamed_from", "ref_field", "ref_file", "concat_fields",
                 "usage"],
                [("DTAL", "PARTLF", "PARTLFR", "CNAME", "CUSTNAME",
                  "CUSTNAME", "BASEPF", None, "B")])
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["DTAL"],
        "output_seeds": [{"id": "X", "library": "DTAL", "file": "PARTLF"}],
        "liblists": {"default": ["DTAL"]},
    })
    g = build_graph(con, config, phase=3)
    passthrough = {(u, v) for u, v, d in g.edges(data=True)
                   if d.get("kind") == "derives_from"
                   and d.get("context", {}).get("mechanism")
                   == "lf_field_passthrough"}
    # The unparsed column bridges to the base...
    assert ("column:DTAL/PARTLF.QCONSC",
            "column:DTAL/BASEPF.QCONSC") in passthrough
    # ...while the parsed-DDS column is left to its parsed evidence.
    assert ("column:DTAL/PARTLF.CNAME",
            "column:DTAL/BASEPF.CNAME") not in passthrough
    con.close()


def test_same_base_read_files_resolve_parsed():
    """A field found in two read files that DSPDBR proves are views of one
    physical (LF + its base) is ONE source: parsed, marked same_base — not
    a fake ambiguity."""
    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    con = dbmod.connect(None)
    cols = []
    for tbl in ("OUTF", "BASEPF", "BASELF"):
        cols.append(("APPX", tbl, tbl, "AMT", "AMT", 1, "CHAR", 10, None,
                     "N", "x"))
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"], cols)
    insert_rows(con, "raw_dspdbr",
                ["dep_lib", "dep_file", "based_lib", "based_file",
                 "dep_type"],
                [("APPX", "BASELF", "APPX", "BASEPF", "D")])
    insert_rows(con, "raw_dsppgmref",
                ["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
                [("APPX", "RPGM2", "APPX", "BASEPF", "F", "1", 1),
                 ("APPX", "RPGM2", "APPX", "BASELF", "F", "1", 1),
                 ("APPX", "RPGM2", "APPX", "OUTF", "F", "2", 1)])
    insert_rows(con, "parsed_rpg_files",
                ["program", "file", "usage", "extname", "rename_rec",
                 "declared_via", "program_described"],
                [("APPX/RPGM2", "BASEPF", "input", None, None, "fspec", False),
                 ("APPX/RPGM2", "BASELF", "input", None, None, "fspec", False),
                 ("APPX/RPGM2", "OUTF", "output", None, None, "fspec", False)])
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["APPX"],
        "output_seeds": [{"id": "O", "library": "APPX", "file": "OUTF"}],
        "liblists": {"default": ["APPX"]},
    })
    g = build_graph(con, config, phase=3)
    derives = {(u, v): d for u, v, d in g.edges(data=True)
               if d.get("kind") == "derives_from"
               and u == "column:APPX/OUTF.AMT"}
    assert len(derives) == 2                     # both routes shown...
    for d in derives.values():                   # ...but as ONE source
        assert d["confidence"] == "parsed"
        assert d["context"]["same_base"] == "APPX/BASEPF"
        assert "ambiguous_files" not in d["context"]
    con.close()


def test_untraced_output_field_gaps_explain_why():
    """Constant-fed, runtime-value, and dead-end-work-variable fields each
    leave a gap that says WHY there is no lineage."""
    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    con = dbmod.connect(None)
    cols = []
    for tbl, names in (("OUTF", ("CNT", "RUNYR", "DEAD")), ("INF", ("F1",))):
        for i, c in enumerate(names):
            cols.append(("APPX", tbl, tbl, c, c, i + 1, "CHAR", 10, None,
                         "N", "x"))
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"], cols)
    insert_rows(con, "raw_dsppgmref",
                ["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
                [("APPX", "GPGM", "APPX", "INF", "F", "1", 1),
                 ("APPX", "GPGM", "APPX", "OUTF", "F", "2", 1)])
    insert_rows(con, "parsed_rpg_files",
                ["program", "file", "usage", "extname", "rename_rec",
                 "declared_via", "program_described"],
                [("APPX/GPGM", "INF", "input", None, None, "fspec", False),
                 ("APPX/GPGM", "OUTF", "output", None, None, "fspec", False)])
    insert_rows(con, "parsed_rpg_moves",
                ["program", "seq", "opcode", "source_field", "result_field"],
                [("APPX/GPGM", 1, "Z-ADD", None, "CNT"),        # constant
                 ("APPX/GPGM", 2, "MOVE", "UYEAR", "RUNYR"),    # runtime
                 ("APPX/GPGM", 3, "MOVE", "WORKVAR", "DEAD")])  # dead-end
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["APPX"],
        "output_seeds": [{"id": "O", "library": "APPX", "file": "OUTF"}],
        "liblists": {"default": ["APPX"]},
    })
    build_graph(con, config, phase=3)
    gaps = {obj: (json_ctx, detail) for obj, detail, json_ctx in con.execute(
        "SELECT object_id, detail, context FROM gaps "
        "WHERE kind = 'rpg_untraced_output_field'").fetchall()}
    assert "runtime value(s) UYEAR" in gaps["column:APPX/OUTF.RUNYR"][1]
    assert "work variable(s) WORKVAR" in gaps["column:APPX/OUTF.DEAD"][1]
    assert "no field source" in gaps["column:APPX/OUTF.CNT"][1]
    con.close()
