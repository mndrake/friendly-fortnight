"""Tests for the single-table column-lineage extractor (analyze.column_trace)."""
import json

from lineage.analyze import column_trace


# --- against the synthetic fixture estate ------------------------------------

def test_ordext_columns_trace_to_orders_via_sql(built):
    """ORDEXT is populated by SQLEXT's embedded
    `INSERT INTO ORDEXT (ORDNO, AMT) SELECT ORDNO, AMOUNT FROM ORDERS`, so each
    ORDEXT column resolves to its ORDERS source column at source_sql/parsed.
    """
    con, g = built
    target = column_trace.resolve_target(con, "APPLIB/ORDEXT")
    # DDL-style column enumeration comes from the SQL catalog (raw_syscolumns).
    assert {c.sql_name for c in target.columns} == {"ORDNO", "AMT"}
    assert target.node_id == "file:APPLIB/ORDEXT"

    text = column_trace.render_forest(target, g)
    assert "column:APPLIB/ORDERS.ORDNO" in text
    assert "column:APPLIB/ORDERS.AMOUNT" in text
    assert "source_sql" in text
    assert "(base)" in text


def test_ordext_ordno_tree_structure(built):
    con, g = built
    root = "column:APPLIB/ORDEXT.ORDNO"
    tree = column_trace.trace_column(g, root)
    assert tree.node == root
    assert tree.provenance is None            # root has no incoming edge
    assert not tree.is_base                   # it has an upstream
    children = {c.node for c in tree.children}
    assert "column:APPLIB/ORDERS.ORDNO" in children
    src = next(c for c in tree.children
               if c.node == "column:APPLIB/ORDERS.ORDNO")
    assert src.provenance == "source_sql"
    assert src.is_base                        # ORDERS.ORDNO has no upstream


def test_custview_rename_traces_to_custmast(built):
    """CUSTVIEW.CNAME maps to CUSTMAST.CUSTNAME through the view definition
    (SELECT CUSTNO, CUSTNAME AS CNAME ...). CUSTVIEW has no SYSCOLUMNS rows in
    the fixture, so columns fall back to the graph's own column nodes."""
    con, g = built
    text = column_trace.trace_table(con, g, "APPLIB/CUSTVIEW")
    assert "column:APPLIB/CUSTMAST.CUSTNAME" in text
    # It is a view, not a DDL table — the extractor says so but still traces.
    assert "table_type" in text


def test_unknown_table_reports_clearly(built):
    con, g = built
    text = column_trace.trace_table(con, g, "APPLIB/NOSUCHTBL")
    assert "not found in raw_systables" in text


def test_table_arg_accepts_sql_naming(built):
    con, _ = built
    dot = column_trace.resolve_target(con, "APPLIB.ORDEXT")
    slash = column_trace.resolve_target(con, "APPLIB/ORDEXT")
    assert dot.spec == slash.spec == "APPLIB/ORDEXT"


# --- a genuine DDL table, built from hand-inserted rows -----------------------

def test_ddl_table_columns_trace_to_base(con, config):
    """A DDL (CREATE TABLE) table loaded by INSERT…SELECT: its columns trace
    back to the base table's columns, and a column with no source is reported
    as 'no resolved lineage'. Demonstrates the DDL (not DDS) case end to end.
    """
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    insert_rows(con, "raw_systables",
                ["table_schema", "table_name", "system_name", "table_type"],
                [("APPLIB", "DDLT", "DDLT", "T"),      # DDL table (type T)
                 ("APPLIB", "BASET", "BASET", "T")])
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal"],
                [("APPLIB", "DDLT", "DDLT", "COL1", "COL1", 1),
                 ("APPLIB", "DDLT", "DDLT", "COL2", "COL2", 2),
                 ("APPLIB", "DDLT", "DDLT", "COL3", "COL3", 3)])  # no source
    insert_rows(con, "parsed_sql_statements",
                ["program", "seq", "stmt_type", "ast_json", "tables_read",
                 "tables_written", "column_lineage", "columns_used",
                 "parse_error", "raw_sql"],
                [("APPLIB/LOADPGM", 1, "INSERT", None,
                  json.dumps(["APPLIB/BASET"]), json.dumps(["APPLIB/DDLT"]),
                  json.dumps([
                      {"target": "APPLIB/DDLT.COL1",
                       "sources": ["APPLIB/BASET.SRCA"]},
                      {"target": "APPLIB/DDLT.COL2",
                       "sources": ["APPLIB/BASET.SRCB"]},
                  ]),
                  json.dumps([]), None,
                  "INSERT INTO DDLT SELECT * FROM BASET")])

    g = build_graph(con, config, phase=3)
    target = column_trace.resolve_target(con, "APPLIB/DDLT")
    assert target.table_type == "T"
    assert [c.sql_name for c in target.columns] == ["COL1", "COL2", "COL3"]

    text = column_trace.render_forest(target, g)
    assert "column:APPLIB/BASET.SRCA" in text
    assert "column:APPLIB/BASET.SRCB" in text
    # COL3 has no upstream mapping -> surfaced, not dropped.
    assert "COL3  → no resolved lineage" in text


def test_ddl_table_confidence_and_provenance_in_summary(con, config):
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    insert_rows(con, "raw_systables",
                ["table_schema", "table_name", "system_name", "table_type"],
                [("APPLIB", "DDLT", "DDLT", "T")])
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal"],
                [("APPLIB", "DDLT", "DDLT", "COL1", "COL1", 1)])
    insert_rows(con, "parsed_sql_statements",
                ["program", "seq", "stmt_type", "ast_json", "tables_read",
                 "tables_written", "column_lineage", "columns_used",
                 "parse_error", "raw_sql"],
                [("APPLIB/LOADPGM", 1, "INSERT", None,
                  json.dumps(["APPLIB/BASET"]), json.dumps(["APPLIB/DDLT"]),
                  json.dumps([{"target": "APPLIB/DDLT.COL1",
                               "sources": ["APPLIB/BASET.SRCA"]}]),
                  json.dumps([]), None, "INSERT INTO DDLT SELECT * FROM BASET")])

    g = build_graph(con, config, phase=3)
    text = column_trace.trace_table(con, g, "APPLIB/DDLT")
    assert "COL1  → 1 base column, min confidence parsed" in text


def test_resolve_target_falls_back_to_syscolumns_identity():
    """A store whose extract pulled SYSCOLUMNS but not SYSTABLES for the
    table (older runs) must still resolve identity — raw_syscolumns is the
    same SQL catalog."""
    from lineage import db as dbmod
    from lineage.analyze.column_trace import resolve_target
    from lineage.db import insert_rows

    con = dbmod.connect(None)
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"],
                [("TNTACCDTA", "EDS_BROKER_BARGAIN_EVENING", "BROAST",
                  "CACINM", "CACINM", 1, "DECIMAL", 9, 2, "N", "x")])
    t = resolve_target(con, "TNTACCDTA/BROAST")
    assert t.in_catalog          # found via syscolumns, not systables
    assert t.name == "BROAST"    # canonical system name
    assert t.spec == "TNTACCDTA/BROAST"
    assert [c.sql_name for c in t.columns] == ["CACINM"]
    con.close()


def test_diagnose_table_full_picture(built, config, tmp_path):
    """diagnose_table over the fixture store: identity, writers, statements,
    column edges, classification, slice audit, gap profile — all present."""
    from lineage.analyze.diagnose import diagnose_table

    con, _graph = built
    out = "\n".join(diagnose_table(con, "APPLIB/CUSTRPT"))
    assert "identity: APPLIB/CUSTRPT" in out
    assert "file:APPLIB/CUSTRPT" in out
    assert "writes: program:APPLIB/RPT001" in out
    assert "column nodes:" in out
    assert "gap profile" in out


def test_diagnose_table_absent_everything():
    """A table the store knows nothing about must produce a readable report,
    not an exception."""
    from lineage import db as dbmod
    from lineage.analyze.diagnose import diagnose_table

    con = dbmod.connect(None)
    out = "\n".join(diagnose_table(con, "NOLIB/NOTABLE"))
    assert "in_catalog=False" in out
    assert "(none — the graph has no file node for this table)" in out
    assert "(not in slice_objects)" in out
    con.close()


def test_trace_column_linear_on_dense_diamond_mesh():
    """20 layers of 6 nodes, each deriving from every node of the next layer:
    ~6^20 paths but only 121 nodes. The walk must finish instantly (node-
    linear), mark revisits as truncated stubs, and collect each base once."""
    import time

    import networkx as nx

    from lineage.analyze.column_trace import _collect_bases, trace_column
    from lineage.graph.model import Confidence

    g = nx.MultiDiGraph()
    layers = [[f"column:L/T{d}.F{i}" for i in range(6)] for d in range(21)]
    for d in range(20):
        for u in layers[d]:
            for v in layers[d + 1]:
                g.add_edge(u, v, kind="derives_from", provenance="dds",
                           confidence="parsed", context={})
    t0 = time.monotonic()
    tree = trace_column(g, layers[0][0])
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0                      # was effectively unbounded
    bases = _collect_bases(tree, Confidence.CONFIRMED)
    # Bases are the last layer's nodes, each exactly once, no stub names.
    assert set(bases) <= set(layers[20])
    assert all(not b.startswith("(+") for b in bases)


# --- default-view de-noising: collapse of structural relay hops ---------------

def _mesh(*edges):
    import networkx as nx

    g = nx.MultiDiGraph()
    for src, dst, conf in edges:
        g.add_edge(src, dst, kind="derives_from", provenance="xref",
                   confidence=conf, context={"mechanism": "same_name_field"})
    return g


def test_collapse_splices_relay_and_keeps_weakest_confidence():
    """OUT.C1 <- OUT#.C1 <- BASE.C1: the hash-logical relay is spliced out;
    the promoted BASE hop names the relay and carries the weaker confidence
    of the two collapsed hops (requirements 1 and 5)."""
    from lineage.analyze.column_trace import collapse_intermediates, trace_column

    g = _mesh(("column:L/OUT.C1", "column:L/OUT#.C1", "inferred"),
              ("column:L/OUT#.C1", "column:L/BASE.C1", "parsed"))
    tree = collapse_intermediates(trace_column(g, "column:L/OUT.C1"),
                                  {"L/OUT#"})
    (child,) = tree.children
    assert child.node == "column:L/BASE.C1"
    assert child.context["collapsed_via"] == "L/OUT#.C1"
    assert child.confidence == "inferred"
    assert child.context["mechanism"] == "same_name_field"  # metadata kept


def test_collapse_chains_via_through_stacked_relays():
    """OUT <- A# <- B# <- BASE: both relays collapse, the via chain lists
    them in traversal order."""
    from lineage.analyze.column_trace import collapse_intermediates, trace_column

    g = _mesh(("column:L/OUT.C", "column:L/A#.C", "inferred"),
              ("column:L/A#.C", "column:L/B#.C", "inferred"),
              ("column:L/B#.C", "column:L/BASE.C", "parsed"))
    tree = collapse_intermediates(trace_column(g, "column:L/OUT.C"),
                                  {"L/A#", "L/B#"})
    (child,) = tree.children
    assert child.node == "column:L/BASE.C"
    assert child.context["collapsed_via"] == "L/A#.C, L/B#.C"


def test_collapse_keeps_terminal_logical_source():
    """A logical with no upstream of its own is the best-known source —
    it must stay visible, not vanish (requirement 2)."""
    from lineage.analyze.column_trace import collapse_intermediates, trace_column

    g = _mesh(("column:L/OUT.C2", "column:L/LONE#.C2", "inferred"))
    tree = collapse_intermediates(trace_column(g, "column:L/OUT.C2"),
                                  {"L/LONE#"})
    (child,) = tree.children
    assert child.node == "column:L/LONE#.C2"


def test_collapse_merges_duplicate_paths_and_drops_self_loop():
    """OUT.C reaches A.C directly and again through relay X# (which also
    loops back to OUT.C): the loop echo is dropped, the two A routes merge
    into one node marked paths_merged, best confidence wins (req 4)."""
    from lineage.analyze.column_trace import collapse_intermediates, trace_column

    g = _mesh(("column:L/OUT.C", "column:L/A.C", "parsed"),
              ("column:L/OUT.C", "column:L/X#.C", "inferred"),
              ("column:L/X#.C", "column:L/A.C", "parsed"),
              ("column:L/X#.C", "column:L/OUT.C", "inferred"))
    tree = collapse_intermediates(trace_column(g, "column:L/OUT.C"),
                                  {"L/X#"})
    (child,) = tree.children
    assert child.node == "column:L/A.C"
    assert child.context["paths_merged"] == 2
    assert child.confidence == "parsed"       # the direct route's confidence
    assert not child.truncated


def test_trace_table_collapses_by_default_raw_keeps_hops(built):
    """Against the fixture estate: CUSTRPT.CUSTNO flows through the logical
    CUSTLF1 — collapsed by default (CUSTMAST promoted, relay annotated),
    fully expanded again with raw=True (requirement 3)."""
    con, g = built
    default = column_trace.trace_table(con, g, "APPLIB/CUSTRPT")
    assert "column:APPLIB/CUSTMAST.CUSTNO" in default
    assert "collapsed_via=APPLIB/CUSTLF1.CUSTNO" in default
    assert "column:APPLIB/CUSTLF1.CUSTNO" not in default

    raw = column_trace.trace_table(con, g, "APPLIB/CUSTRPT", raw=True)
    assert "column:APPLIB/CUSTLF1.CUSTNO" in raw
    assert "collapsed_via" not in raw


def test_trace_column_caps_fan_in_with_stub():
    import networkx as nx

    from lineage.analyze.column_trace import _MAX_CHILDREN, trace_column

    g = nx.MultiDiGraph()
    root = "column:L/T.ROOT"
    for i in range(_MAX_CHILDREN + 15):
        g.add_edge(root, f"column:L/S.F{i:03d}", kind="derives_from",
                   provenance="dds", confidence="parsed", context={})
    tree = trace_column(g, root)
    assert len(tree.children) == _MAX_CHILDREN + 1
    assert tree.children[-1].node == "(+15 more upstream edges)"
    assert tree.children[-1].truncated
