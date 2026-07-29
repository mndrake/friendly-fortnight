"""Read-only host profiling tests (lineage.extract.profiler)."""
import json
import re

import pytest

from lineage.extract.connection import FixtureHostSession, QueryResult
from lineage.extract.profiler import ProfileReport, profile_host, recommend

_WRITE_RE = re.compile(r"\b(CREATE|DROP|INSERT|DELETE|UPDATE)\b", re.IGNORECASE)


class TestProfileHost:
    def test_full_profile_over_fixture_estate(self, session, config):
        report = profile_host(session, config)

        assert report.catalog_rows == {
            "SYSTABLES": 9, "SYSCOLUMNS": 24, "SYSVIEWS": 1,
            "SYSVIEWDEP": 1, "SYSPARTITIONSTAT": 16,
        }

        assert report.library_objects["APPLIB"]["*PGM"] == {
            "CLP": 2, "RPG": 3, "SQLRPG": 1}
        assert report.library_objects["APPLIB"]["*FILE"] == {
            "PF": 8, "LF": 1}

        vol = report.source_volumes["APPLIB/QCLSRC"]
        assert vol["members"] == 2
        assert vol["total_lines"] == 19
        assert vol["total_bytes"] == 2000
        assert vol["top_members"] == [
            {"member": "CLDRIVER", "lines": 12},
            {"member": "CLDYN", "lines": 7},
        ]
        total_members = sum(v["members"] for v in report.source_volumes.values())
        total_lines = sum(v["total_lines"] for v in report.source_volumes.values())
        assert total_members == 2 + 4 + 7
        assert total_lines == 19 + 40 + 40

        assert report.seed_classes["CUST_MONTHLY_RPT"]["classification"] == "ddl_table"
        assert report.seed_classes["ORDER_EXTRACT"]["classification"] == "ddl_table"
        assert report.seed_classes["ORDER_SUMMARY"]["classification"] == "ddl_table"
        assert report.seed_classes["MISSING_OUT"]["classification"] == "missing"

        assert report.timings  # every probe recorded, even successful ones
        assert all(t >= 0 for t in report.timings.values())
        assert report.errors == {}

    def test_read_only_guard(self, session, config):
        profile_host(session, config)
        assert session.cl_log == []
        for sql in session.sql_log:
            assert not _WRITE_RE.search(sql), sql

    def test_json_round_trip(self, session, config):
        report = profile_host(session, config)
        data = json.loads(report.to_json())
        assert set(data.keys()) == {
            "library_objects", "catalog_rows", "source_volumes",
            "seed_classes", "timings", "errors",
        }

    def test_graceful_degradation_on_empty_host(self, config):
        empty = FixtureHostSession(responses={})
        report = profile_host(empty, config)  # must not raise
        assert isinstance(report, ProfileReport)
        assert report.errors  # every probe failed and was recorded
        assert report.catalog_rows == {}
        assert report.library_objects == {}
        assert report.seed_classes == {}


class TestSeedClassification:
    def _report_for(self, seed_row, config):
        responses = {
            "profile.seed.X": QueryResult(
                columns=["OBJATTRIBUTE", "SQL_OBJECT_TYPE"], rows=[seed_row]),
        }
        session = FixtureHostSession(responses=responses)
        from lineage.config import from_dict
        cfg = from_dict({
            "scratch_lib": "QTEMP", "libraries": ["APPLIB"],
            "output_seeds": [{"id": "X", "library": "APPLIB", "file": "F"}],
        })
        return profile_host(session, cfg)

    def test_dds_pf(self, config):
        report = self._report_for(("PF", ""), config)
        assert report.seed_classes["X"]["classification"] == "dds_pf"

    def test_lf(self, config):
        report = self._report_for(("LF", ""), config)
        assert report.seed_classes["X"]["classification"] == "lf"

    def test_view_over_pf(self, config):
        report = self._report_for(("PF", "VIEW"), config)
        assert report.seed_classes["X"]["classification"] == "view"

    def test_view_over_lf(self, config):
        report = self._report_for(("LF", "VIEW"), config)
        assert report.seed_classes["X"]["classification"] == "view"

    def test_unknown(self, config):
        report = self._report_for(("SOMETHINGELSE", ""), config)
        assert report.seed_classes["X"]["classification"] == "unknown"


class TestRecommend:
    def _cfg(self):
        from lineage.config import from_dict
        return from_dict({
            "scratch_lib": "QTEMP", "libraries": ["APPLIB"],
            "output_seeds": [{"id": "X", "library": "APPLIB", "file": "F"}],
        })

    def test_big_syscolumns_triggers_scoping(self):
        report = ProfileReport(catalog_rows={"SYSCOLUMNS": 60_000})
        lines = recommend(report, self._cfg())
        assert any("SYSCOLUMNS" in line for line in lines)

    def test_big_source_volume_triggers_member_retrieval(self):
        report = ProfileReport(source_volumes={
            "APPLIB/QRPGSRC": {"members": 6_000, "total_lines": 10,
                              "total_bytes": 0, "top_members": []},
        })
        lines = recommend(report, self._cfg())
        assert any("targeted member retrieval" in line for line in lines)

        report2 = ProfileReport(source_volumes={
            "APPLIB/QRPGSRC": {"members": 1, "total_lines": 2_000_000,
                              "total_bytes": 0, "top_members": []},
        })
        lines2 = recommend(report2, self._cfg())
        assert any("targeted member retrieval" in line for line in lines2)

    def test_non_ddl_seed_named(self):
        report = ProfileReport(seed_classes={
            "X": {"library": "APPLIB", "file": "F", "objattribute": "PF",
                 "sql_object_type": "", "classification": "dds_pf"},
        })
        lines = recommend(report, self._cfg())
        assert any("'X'" in line and "dds_pf" in line for line in lines)

    def test_missing_seed_called_out(self):
        report = ProfileReport(seed_classes={
            "X": {"library": "APPLIB", "file": "F", "objattribute": "",
                 "sql_object_type": "", "classification": "missing"},
        })
        lines = recommend(report, self._cfg())
        assert any("'X'" in line and "not found" in line for line in lines)

    def test_slow_timing_named(self):
        report = ProfileReport(timings={"profile.catalog.syscolumns": 6.5})
        lines = recommend(report, self._cfg())
        assert any("profile.catalog.syscolumns" in line for line in lines)

    def test_slow_syspartitionstat_mentions_per_member_cost(self):
        report = ProfileReport(
            timings={"profile.source.APPLIB.QRPGSRC": 9.0})
        lines = recommend(report, self._cfg())
        matches = [l for l in lines if "profile.source.APPLIB.QRPGSRC" in l]
        assert matches
        assert "per-member" in matches[0]

    def test_large_pgm_count_flags_dsppgmref(self):
        report = ProfileReport(library_objects={
            "APPLIB": {"*PGM": {"RPG": 3_000}}})
        lines = recommend(report, self._cfg())
        assert any("DSPPGMREF" in line for line in lines)

    def test_modest_report_yields_all_fine_line(self):
        report = ProfileReport(
            catalog_rows={"SYSCOLUMNS": 10},
            source_volumes={"APPLIB/QRPGSRC": {
                "members": 5, "total_lines": 100, "total_bytes": 0,
                "top_members": []}},
            seed_classes={"X": {
                "library": "APPLIB", "file": "F", "objattribute": "PF",
                "sql_object_type": "TABLE", "classification": "ddl_table"}},
            timings={"profile.catalog.syscolumns": 0.1},
            library_objects={"APPLIB": {"*PGM": {"RPG": 5}}},
        )
        lines = recommend(report, self._cfg())
        assert lines == ["Volumes look modest across the board — "
                         "full-library extraction is fine at these volumes."]
