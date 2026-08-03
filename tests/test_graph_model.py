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
