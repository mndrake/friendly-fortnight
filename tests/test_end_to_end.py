"""Golden-file tests: fixture estate -> expected lineage, commonality,
complexity, and gaps (Phase 1-4 acceptance criteria)."""
import json

import pytest


@pytest.fixture()
def analyzed(built, config):
    con, g = built
    from lineage.analyze import commonality, complexity, lineage
    lineage.compute_output_lineage(con, g, config)
    commonality.analyze(con)
    complexity.score(con, g, config)
    return con, g


def _bases(con, output_id):
    return {r[0] for r in con.execute(
        "SELECT DISTINCT source_file FROM output_lineage "
        "WHERE output_id = ? AND source_column IS NULL "
        "AND relation = 'derives'",
        [output_id]).fetchall()}


def _used_columns(con, output_id):
    return {r[0] for r in con.execute(
        "SELECT DISTINCT source_column FROM output_lineage "
        "WHERE output_id = ? AND relation = 'used'",
        [output_id]).fetchall()}


class TestTableLevelLineage:
    def test_custrpt_traced_through_override_and_lf(self, analyzed):
        con, _ = analyzed
        # RPT001 reads CUSTLF1 (flattens to CUSTMAST) and ORDERS which the
        # calling CL overrides to ORDHIST. ORDERS itself must NOT appear:
        # the compiled reference was superseded (Phase 2 acceptance).
        assert _bases(con, "CUST_MONTHLY_RPT") == {
            "file:APPLIB/CUSTMAST", "file:APPLIB/ORDHIST"}

    def test_ordext_via_embedded_sql(self, analyzed):
        con, _ = analyzed
        assert _bases(con, "ORDER_EXTRACT") == {"file:APPLIB/ORDERS"}

    def test_ordsum_default_resolution_when_override_dynamic(self, analyzed):
        con, _ = analyzed
        # CLDYN's override target is CHGVAR-built (unresolvable), so RPT002
        # keeps its compiled ORDERS reference and the gap is reported.
        assert _bases(con, "ORDER_SUMMARY") == {"file:APPLIB/ORDERS"}

    def test_missing_seed_is_unresolved(self, analyzed):
        con, _ = analyzed
        assert _bases(con, "MISSING_OUT") == set()


class TestColumnLevelLineage:
    def test_ordext_columns_from_sql(self, analyzed):
        con, _ = analyzed
        rows = {r[0] for r in con.execute(
            "SELECT source_column FROM output_lineage WHERE output_id = "
            "'ORDER_EXTRACT' AND source_column IS NOT NULL "
            "AND relation = 'derives'").fetchall()}
        assert "column:APPLIB/ORDERS.ORDNO" in rows
        assert "column:APPLIB/ORDERS.AMOUNT" in rows

    def test_custrpt_cname_traces_to_custname_through_rename(self, analyzed):
        con, _ = analyzed
        rows = {r[0] for r in con.execute(
            "SELECT source_column FROM output_lineage WHERE output_id = "
            "'CUST_MONTHLY_RPT' AND source_column IS NOT NULL "
            "AND relation = 'derives'").fetchall()}
        # CUSTRPT.CNAME -> CUSTLF1.CNAME (inferred) -> CUSTMAST.CUSTNAME (DDS
        # RENAME): the join-through-rename Phase 3 acceptance case.
        assert "column:APPLIB/CUSTMAST.CUSTNAME" in rows
        assert "column:APPLIB/ORDHIST.AMOUNT" in rows

    def test_inferred_confidence_marked(self, analyzed):
        con, _ = analyzed
        conf = {r[0] for r in con.execute(
            "SELECT DISTINCT min_confidence FROM output_lineage WHERE "
            "output_id = 'CUST_MONTHLY_RPT' AND source_column IS NOT NULL "
            "AND relation = 'derives'"
        ).fetchall()}
        assert "inferred" in conf


class TestColumnUsage:
    """Column usage from CL and RPG parsing: program -reads-> column,
    relation='used' — "read en route", not "maps into the output"."""

    def test_ordext_where_clause_column_used(self, analyzed):
        con, _ = analyzed
        # SQLEXT's INSERT ... SELECT ORDNO, AMOUNT FROM ORDERS WHERE
        # AMOUNT > 0: both select-list and WHERE-only columns are usage.
        used = _used_columns(con, "ORDER_EXTRACT")
        assert used == {"column:APPLIB/ORDERS.ORDNO",
                        "column:APPLIB/ORDERS.AMOUNT"}
        confs = {r[0] for r in con.execute(
            "SELECT DISTINCT min_confidence FROM output_lineage WHERE "
            "output_id = 'ORDER_EXTRACT' AND relation = 'used'").fetchall()}
        assert confs == {"parsed"}

    def test_custrpt_chain_field_reference(self, analyzed):
        con, _ = analyzed
        # RPT001's CHAIN CUSTNO CUSTLF1 references CUSTNO by name. CUSTLF1
        # itself is not a base physical file (it derives from CUSTMAST), so
        # no usage row lands there; ORDHIST is the base file RPT001 actually
        # reads (via CLDRIVER's OVRDBF ORDERS->ORDHIST) and does carry the
        # parsed field reference.
        used = _used_columns(con, "CUST_MONTHLY_RPT")
        assert used == {"column:APPLIB/ORDHIST.CUSTNO"}
        row = con.execute(
            "SELECT min_confidence FROM output_lineage WHERE output_id = "
            "'CUST_MONTHLY_RPT' AND relation = 'used'").fetchone()
        assert row[0] == "parsed"

    def test_ordsum_record_io_all_fields_fallback(self, analyzed):
        con, _ = analyzed
        # RPT002 reads ORDERS with no field-level evidence found in its
        # C-specs (WRITEORDSREC references a record format, not a field) ->
        # falls back to every ORDERS field, confidence inferred.
        used = _used_columns(con, "ORDER_SUMMARY")
        assert used == {
            "column:APPLIB/ORDERS.ORDNO", "column:APPLIB/ORDERS.CUSTNO",
            "column:APPLIB/ORDERS.AMOUNT", "column:APPLIB/ORDERS.ORDDATE"}
        confs = {r[0] for r in con.execute(
            "SELECT DISTINCT min_confidence FROM output_lineage WHERE "
            "output_id = 'ORDER_SUMMARY' AND relation = 'used'").fetchall()}
        assert confs == {"inferred"}

    def test_used_rows_never_flip_coverage_status(self, analyzed, config):
        """Usage rows are additive evidence, not lineage: they must not
        change an output's resolved/partial/unresolved status (gaps.py
        filters its confidence scan to relation='derives')."""
        con, _ = analyzed
        from lineage.analyze.gaps import coverage
        cov = coverage(con, config)
        assert cov["outputs"]["ORDER_EXTRACT"]["status"] == "resolved"

    def test_commonality_matrix_has_column_rows(self, analyzed):
        con, _ = analyzed
        rows = {r[0] for r in con.execute(
            "SELECT source_id FROM commonality_matrix WHERE output_id = "
            "'ORDER_EXTRACT' AND source_id LIKE 'column:%'").fetchall()}
        assert rows == {"column:APPLIB/ORDERS.ORDNO",
                        "column:APPLIB/ORDERS.AMOUNT"}

    def test_candidate_products_unaffected_by_columns(self, analyzed):
        """candidate_products/greedy_cluster still operate on the file-level
        matrix only — column rows must not change fan-out clustering."""
        con, _ = analyzed
        rows = con.execute(
            "SELECT fanout, payload FROM product_candidates ORDER BY rank"
        ).fetchall()
        top = json.loads(rows[0][1])
        assert top["shared_sources"] == ["file:APPLIB/ORDERS"]


class TestGaps:
    def test_dynamic_override_reported(self, built):
        con, _ = built
        rows = con.execute(
            "SELECT object_id, context FROM gaps WHERE kind = "
            "'unresolved_dynamic_name'").fetchall()
        assert any("CLDYN" in obj for obj, _ in rows)
        # The captured expression is surfaced, not dropped.
        assert any("&F" in (ctx or "") for _, ctx in rows)

    def test_missing_source_reported(self, built):
        con, _ = built
        rows = {r[0] for r in con.execute(
            "SELECT object_id FROM gaps WHERE kind = 'missing_source'"
        ).fetchall()}
        assert "program:APPLIB/GHOST" in rows

    def test_outside_scope_distinct_from_dynamic(self, built):
        con, _ = built
        kinds = {r[0] for r in con.execute(
            "SELECT DISTINCT kind FROM gaps").fetchall()}
        # Phase 2 acceptance: distinguishes unresolved-dynamic-name from
        # outside-scanned-scope.
        assert "unresolved_dynamic_name" in kinds
        assert "outside_scope" in kinds
        outside = {r[0] for r in con.execute(
            "SELECT object_id FROM gaps WHERE kind = 'outside_scope'"
        ).fetchall()}
        assert any("LEGACY" in o for o in outside)


class TestCoverage:
    def test_every_output_exactly_one_status(self, analyzed, config):
        con, _ = analyzed
        from lineage.analyze.gaps import coverage
        cov = coverage(con, config)
        assert set(cov["outputs"]) == {s.id for s in config.output_seeds}
        for oid, o in cov["outputs"].items():
            assert o["status"] in {"resolved", "partially_resolved",
                                   "unresolved"}
        assert cov["outputs"]["MISSING_OUT"]["status"] == "unresolved"
        assert cov["outputs"]["MISSING_OUT"]["reasons"]
        s = cov["summary"]
        assert s["outputs_total"] == 4
        assert s["outputs_resolved"] + s["outputs_partial"] + \
            s["outputs_unresolved"] == 4


class TestCommonality:
    def test_shared_source_produces_candidate(self, analyzed):
        con, _ = analyzed
        rows = con.execute(
            "SELECT fanout, payload FROM product_candidates ORDER BY rank"
        ).fetchall()
        assert rows
        top = json.loads(rows[0][1])
        # ORDER_EXTRACT and ORDER_SUMMARY both feed off ORDERS.
        assert top["fanout"] == 2
        assert top["shared_sources"] == ["file:APPLIB/ORDERS"]
        assert set(top["outputs"]) == {"ORDER_EXTRACT", "ORDER_SUMMARY"}


class TestComplexity:
    def test_buckets(self, analyzed):
        con, _ = analyzed
        buckets = dict(con.execute(
            "SELECT output_id, bucket FROM complexity_scores").fetchall())
        # Straight SQL extract: no overrides, shallow -> view candidate.
        assert buckets["ORDER_EXTRACT"] == "replicate_as_view"
        # Override en route -> at least moderate.
        assert buckets["CUST_MONTHLY_RPT"] in {"moderate",
                                               "full_reengineering"}
        assert buckets["MISSING_OUT"] == "full_reengineering"

    def test_override_depth_counted(self, analyzed):
        con, _ = analyzed
        row = con.execute(
            "SELECT override_depth FROM complexity_scores WHERE "
            "output_id = 'CUST_MONTHLY_RPT'").fetchone()
        assert row[0] >= 1


class TestReport:
    def test_exports_and_html(self, analyzed, config, tmp_path):
        con, g = analyzed
        from lineage.analyze.gaps import coverage
        from lineage.report import export, html as html_report
        cov = coverage(con, config)
        files = export.export_tables(con, tmp_path, fmt="csv")
        assert all(f.exists() for f in files)
        graphs = export.export_output_graphs(con, g, config, tmp_path / "g")
        assert len(graphs) == 4
        payload = json.loads((tmp_path / "g" / "lineage_ORDER_EXTRACT.json")
                             .read_text())
        assert payload["seed"] == "file:APPLIB/ORDEXT"
        assert any(n["id"] == "file:APPLIB/ORDERS" for n in payload["nodes"])
        out = html_report.render(con, cov, tmp_path / "summary.html")
        text = out.read_text()
        assert "CUST_MONTHLY_RPT" in text
        assert "replicate_as_view" in text
        assert "Cols used" in text
        assert "Top used source columns" in text
        assert "APPLIB/ORDERS.AMOUNT" in text

    def test_parquet_export(self, analyzed, tmp_path):
        con, _ = analyzed
        from lineage.report import export
        files = export.export_tables(con, tmp_path, fmt="parquet")
        assert all(f.suffix == ".parquet" and f.exists() for f in files)
