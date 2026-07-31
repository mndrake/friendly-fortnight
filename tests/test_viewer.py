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
