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
