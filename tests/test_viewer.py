"""Tests for the interactive per-output HTML lineage viewer (report/viewer.py)."""
from __future__ import annotations

import json
import re

from lineage.config import from_dict
from lineage.report import viewer

LIB = "APPLIB"

_SEED_IDS = ("CUST_MONTHLY_RPT", "ORDER_EXTRACT", "ORDER_SUMMARY", "MISSING_OUT")


def _pages(built, config, tmp_path):
    con, g = built
    return {p.stem: p for p in viewer.render_output_pages(con, g, config, tmp_path)}


def test_one_page_per_seed(built, config, tmp_path):
    pages = _pages(built, config, tmp_path)
    assert set(pages) == {f"lineage_{sid}" for sid in _SEED_IDS}
    for path in pages.values():
        assert path.exists()
        assert path.name.startswith("lineage_") and path.suffix == ".html"


def test_page_contains_svg_seed_table_and_known_upstream_file(built, config,
                                                               tmp_path):
    pages = _pages(built, config, tmp_path)

    custrpt = pages["lineage_CUST_MONTHLY_RPT"].read_text(encoding="utf-8")
    assert "<svg" in custrpt
    assert "CUSTRPT" in custrpt          # the seed table itself
    # RPT001 reads CUSTLF1 (-> CUSTMAST via DSPDBR) and, after CL override
    # resolution, ORDHIST rather than the compiled ORDERS reference — see
    # tests/test_end_to_end.py::test_custrpt_traced_through_override_and_lf.
    assert "CUSTMAST" in custrpt
    assert "ORDHIST" in custrpt

    ordext = pages["lineage_ORDER_EXTRACT"].read_text(encoding="utf-8")
    assert "<svg" in ordext
    assert "ORDEXT" in ordext
    assert "ORDERS" in ordext            # SQLEXT's INSERT...SELECT FROM ORDERS


def test_confidence_colors_and_legend_present(built, config, tmp_path):
    pages = _pages(built, config, tmp_path)
    text = pages["lineage_CUST_MONTHLY_RPT"].read_text(encoding="utf-8")
    for cls in ("conf-confirmed", "conf-parsed", "conf-inferred",
               "conf-unresolved"):
        assert cls in text
    assert "legend" in text.lower()
    assert "confirmed" in text and "inferred" in text


_XMLNS_RE = re.compile(r'xmlns(:\w+)?="https?://[^"]*"')


def test_pages_are_self_contained_no_external_references(built, config,
                                                          tmp_path):
    """Self-containment guard: no external http(s):// reference anywhere,
    except an SVG xmlns namespace declaration (which is inert markup, not a
    fetched resource)."""
    pages = _pages(built, config, tmp_path)
    for path in pages.values():
        text = path.read_text(encoding="utf-8")
        stripped = _XMLNS_RE.sub("", text)
        assert "http://" not in stripped
        assert "https://" not in stripped


def test_column_section_lists_column_with_nested_upstream_hop(built, config,
                                                               tmp_path):
    pages = _pages(built, config, tmp_path)
    text = pages["lineage_CUST_MONTHLY_RPT"].read_text(encoding="utf-8")
    assert 'class="columns"' in text
    # CUSTNO -> CUSTLF1.CUSTNO (first hop) -> CUSTMAST.CUSTNO (nested hop).
    assert "column:APPLIB/CUSTLF1.CUSTNO" in text
    assert "column:APPLIB/CUSTMAST.CUSTNO" in text
    # Nesting: the CUSTMAST hop must appear inside CUSTLF1's <div class="hops">.
    idx_custlf1 = text.index('data-file="APPLIB/CUSTLF1"')
    idx_custmast = text.index("column:APPLIB/CUSTMAST.CUSTNO")
    assert idx_custlf1 < idx_custmast


def test_unresolved_column_shows_no_resolved_lineage_marker(con, tmp_path):
    """A DDL table with one column that has no derives_from edge at all must
    still be listed, explicitly marked unresolved (design principle 4)."""
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph

    insert_rows(con, "raw_systables",
                ["table_schema", "table_name", "system_name", "table_type"],
                [(LIB, "DDLT", "DDLT", "T"), (LIB, "BASET", "BASET", "T")])
    insert_rows(con, "raw_syscolumns",
                ["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal"],
                [(LIB, "DDLT", "DDLT", "COL1", "COL1", 1),
                 (LIB, "DDLT", "DDLT", "COL2", "COL2", 2)])   # COL2: no source
    insert_rows(con, "parsed_sql_statements",
                ["program", "seq", "stmt_type", "ast_json", "tables_read",
                 "tables_written", "column_lineage", "columns_used",
                 "parse_error", "raw_sql"],
                [(f"{LIB}/LOADPGM", 1, "INSERT", None,
                  json.dumps([f"{LIB}/BASET"]), json.dumps([f"{LIB}/DDLT"]),
                  json.dumps([{"target": f"{LIB}/DDLT.COL1",
                              "sources": [f"{LIB}/BASET.SRCA"]}]),
                  json.dumps([]), None,
                  "INSERT INTO DDLT SELECT * FROM BASET")])

    ddl_config = from_dict({
        "scratch_lib": "QTEMP",
        "libraries": [LIB],
        "source_files": [{"library": LIB, "file": "QRPGSRC"}],
        "output_seeds": [{"id": "DDLT_OUT", "library": LIB, "file": "DDLT"}],
        "liblists": {"default": [LIB]},
    })
    g = build_graph(con, ddl_config, phase=3)

    written = viewer.render_output_pages(con, g, ddl_config, tmp_path)
    text = written[0].read_text(encoding="utf-8")
    assert "COL1" in text and "COL2" in text
    assert "column:APPLIB/BASET.SRCA" in text  # COL1's resolved upstream
    assert "no resolved lineage" in text        # COL2, marked explicitly


def test_page_renders_for_seed_absent_from_graph(built, tmp_path):
    """A configured output seed whose file was never observed in the estate
    (no writer, not in the catalog) must still produce a valid page, not an
    exception."""
    con, g = built
    bogus_config = from_dict({
        "scratch_lib": "QTEMP",
        "libraries": [LIB],
        "source_files": [{"library": LIB, "file": "QRPGSRC"}],
        "output_seeds": [
            {"id": "GHOST_OUT", "library": LIB, "file": "TOTALLYNOTREAL"},
        ],
        "liblists": {"default": [LIB]},
    })
    written = viewer.render_output_pages(con, g, bogus_config, tmp_path)
    assert len(written) == 1
    text = written[0].read_text(encoding="utf-8")
    assert "<style>" in text and "</script>" in text
    # No table/program DAG data for a node the graph never saw.
    assert "not present in the built graph" in text


# --- DAG pruning: data-flow spine, not the whole cone --------------------------

def test_dag_omits_call_edges_and_call_only_programs(built, config):
    """CLDYN only *calls* into the slice (no reads/writes) — with call edges
    dropped it must vanish from the DAG; the data-flow files stay."""
    from lineage.report import viewer

    con, graph = built
    page = viewer._render_page(con, graph, config.output_seeds[0])
    svg = page.split('class="dag-wrap"')[1].split("</svg>")[0]
    assert 'data-kind="calls"' not in svg
    assert "CLDYN" not in svg            # call-only driver pruned
    assert "CUSTMAST" in svg             # data flow intact
    assert "call edges" in page          # omission is stated, not silent


def test_dag_depth_cut_marks_truncated_nodes(built, config, monkeypatch):
    """With a tiny node budget the DAG cuts at a shallow depth, says so in
    the meta line, and dash-marks nodes whose upstream continues."""
    from lineage.report import viewer

    monkeypatch.setattr(viewer, "_MAX_DAG_NODES", 2)
    con, graph = built
    page = viewer._render_page(con, graph, config.output_seeds[0])
    assert "nodes deeper than" in page
    assert "node-truncated" in page
    assert "Complete subgraph" in page


def test_dag_merges_parallel_edges(built, config):
    """No two rendered edge paths share (src, dst, kind) — parallel evidence
    is merged into one edge with a count."""
    from lineage.report import viewer

    con, graph = built
    nodes, edges = viewer._build_subgraph(graph, config.output_seeds[0].node_id)
    shown_nodes, shown_edges, _stats = viewer._prune(nodes, edges)
    keys = [(e["src"], e["dst"], e["kind"]) for e in shown_edges]
    assert len(keys) == len(set(keys))


# --- Physical-table flow: the default data-lineage view ------------------------

def test_physical_view_contracts_programs_and_logical_files(built, config):
    """The primary view answers 'which physical tables feed this output' —
    RPT001 (program) and CUSTLF1 (logical file) must be contracted into the
    arrows, with the program riding along as the edge's 'via'."""
    from lineage.report import viewer

    con, graph = built
    page = viewer._render_page(con, graph, config.output_seeds[0])
    phys_svg = page.split('class="dag-wrap"')[1].split("</svg>")[0]
    assert "CUSTRPT" in phys_svg         # the output itself
    assert "CUSTMAST" in phys_svg        # physical base
    assert "ORDHIST" in phys_svg         # physical base (post-override)
    assert "CUSTLF1" not in phys_svg     # logical file: contracted away
    assert "RPT001" not in phys_svg.replace("via APPLIB/RPT001", "")
    assert "via APPLIB/RPT001" in phys_svg   # ...but visible on the arrows
    assert 'data-kind="flow"' in phys_svg
    assert "Physical table flow" in page
    # The detailed spine is still there, one click away.
    assert "Detailed table / program graph" in page
