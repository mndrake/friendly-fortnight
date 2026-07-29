"""Host capability probe and adaptive catalog SELECT tests."""
import pytest

from lineage.extract.catalog import (PULLS, CatalogShapeError, PullSpec,
                                     ColSpec)
from lineage.extract.connection import FixtureHostSession, QueryResult
from lineage.extract.hostinfo import HostProfile, probe
from lineage.extract.source import resolve_strategy


class TestProbe:
    def test_full_probe_from_fixture(self, session):
        prof = probe(session)
        assert prof.version_label == "IBM i 7.4"
        assert prof.product_version == "07.04.0000 V7R4m0"
        assert prof.has_ifs_read
        assert "TABLE_PARTITION" in prof.columns_of("SYSPARTITIONSTAT")
        assert "OBJECT_SCHEMA" in prof.columns_of("SYSVIEWDEP")

    def test_probe_degrades_gracefully(self):
        # A host where every probe query fails still yields a usable profile.
        empty = FixtureHostSession(responses={})
        prof = probe(empty)
        assert prof.version_label == "07.04.0000 V7R4m0"  # JDBC metadata only
        assert prof.catalog_columns == {}
        assert not prof.has_ifs_read

    def test_save_load_roundtrip(self, con, session):
        prof = probe(session)
        prof.save(con)
        loaded = HostProfile.load(con)
        assert loaded.version_label == prof.version_label
        assert loaded.has_ifs_read == prof.has_ifs_read
        assert loaded.catalog_columns == prof.catalog_columns


def _spec():
    return PullSpec(
        name="t", raw_table="raw_t", catalog_view="SYSTEST",
        schema_filter="TABLE_SCHEMA",
        cols=(
            ColSpec("a", ("A_NEW", "A_OLD"), required=True),
            ColSpec("b", ("B_ONLY",)),
        ))


class TestAdaptiveSelect:
    def test_synonym_fallback(self):
        sql, missing = _spec().build_select({"A_OLD", "B_ONLY"}, "'L'")
        assert "A_OLD AS a" in sql
        assert not missing

    def test_missing_optional_null_filled(self):
        sql, missing = _spec().build_select({"A_NEW"}, "'L'")
        assert "CAST(NULL AS VARCHAR(1)) AS b" in sql
        assert missing == ["b"]

    def test_missing_required_fails_loudly(self):
        with pytest.raises(CatalogShapeError, match="A_NEW"):
            _spec().build_select({"B_ONLY"}, "'L'")

    def test_no_profile_uses_first_candidates(self):
        sql, missing = _spec().build_select(set(), "'L'")
        assert "A_NEW AS a" in sql and "B_ONLY AS b" in sql
        assert not missing

    def test_shipped_specs_use_documented_names(self):
        by_name = {s.name: s for s in PULLS}
        part = by_name["syspartitionstat"]
        member_col = next(c for c in part.cols if c.raw == "partition_name")
        assert member_col.candidates[0] == "TABLE_PARTITION"
        dep = by_name["sysviewdep"]
        obj = next(c for c in dep.cols if c.raw == "object_schema")
        assert obj.candidates[0] == "OBJECT_SCHEMA"
        tables = by_name["systables"]
        rowcount = next(c for c in tables.cols if c.raw == "row_count")
        assert not rowcount.required  # SYSTABLES has no row count


class TestStrategyResolution:
    def _cfg(self, mode):
        from lineage.config import from_dict
        return from_dict({
            "scratch_lib": "QTEMP", "libraries": ["APPLIB"],
            "output_seeds": [{"id": "X", "library": "APPLIB", "file": "F"}],
            "source_retrieval": mode,
        })

    def test_auto_with_ifs_read(self):
        prof = HostProfile(has_ifs_read=True)
        assert resolve_strategy(self._cfg("auto"), prof) == "ifs_read"

    def test_auto_without_ifs_read(self):
        prof = HostProfile(has_ifs_read=False)
        assert resolve_strategy(self._cfg("auto"), prof) == "alias"

    def test_auto_without_profile_is_optimistic(self):
        assert resolve_strategy(self._cfg("auto"), None) == "ifs_read"

    def test_explicit_modes_ignore_profile(self):
        prof = HostProfile(has_ifs_read=False)
        assert resolve_strategy(self._cfg("ifs_read"), prof) == "ifs_read"
        prof = HostProfile(has_ifs_read=True)
        assert resolve_strategy(self._cfg("alias"), prof) == "alias"

    def test_default_mode_is_auto(self):
        from lineage.config import from_dict
        cfg = from_dict({
            "scratch_lib": "QTEMP", "libraries": ["APPLIB"],
            "output_seeds": [{"id": "X", "library": "APPLIB", "file": "F"}],
        })
        assert cfg.source_retrieval == "auto"
