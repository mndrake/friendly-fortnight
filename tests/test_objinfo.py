"""Tests for lineage.extract.objinfo.source_location."""
from __future__ import annotations

from lineage.extract.connection import FixtureHostSession, QueryResult
from lineage.extract.objinfo import source_location


def test_source_location_hit():
    session = FixtureHostSession(responses={
        "objstat.APPLIB.RPT001.pgm": QueryResult(
            columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"],
            rows=[("APPLIB", "QRPGSRC", "RPT001")]),
    })
    assert source_location(session, "APPLIB", "RPT001", "*PGM") == (
        "APPLIB", "QRPGSRC", "RPT001")


def test_source_location_lowercases_tag_type_and_strips_star():
    """Tag is objstat.<LIB>.<NAME>.<pgm|file> -- lowercase, no leading *."""
    session = FixtureHostSession(responses={
        "objstat.APPLIB.CUSTMAST.file": QueryResult(
            columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"],
            rows=[("APPLIB", "QDDSSRC", "CUSTMAST")]),
    })
    assert source_location(session, "applib", "custmast", "*FILE") == (
        "APPLIB", "QDDSSRC", "CUSTMAST")


def test_source_location_empty_rows_returns_none():
    session = FixtureHostSession(responses={
        "objstat.APPLIB.GHOST.pgm": QueryResult(
            columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"],
            rows=[]),
    })
    assert source_location(session, "APPLIB", "GHOST", "*PGM") is None


def test_source_location_blank_values_returns_none():
    session = FixtureHostSession(responses={
        "objstat.APPLIB.NOWHERE.file": QueryResult(
            columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"],
            rows=[(None, "  ", "MBR")]),
    })
    assert source_location(session, "APPLIB", "NOWHERE", "*FILE") is None


def test_source_location_missing_fixture_tag_degrades_to_none():
    """A probe failure (here: no fixture response at all) must not raise --
    callers fall back to name-matching."""
    session = FixtureHostSession(responses={})
    assert source_location(session, "APPLIB", "RPT001", "*PGM") is None


def test_source_location_strips_whitespace():
    session = FixtureHostSession(responses={
        "objstat.APPLIB.RPT001.pgm": QueryResult(
            columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"],
            rows=[(" APPLIB ", " QRPGSRC ", " RPT001 ")]),
    })
    assert source_location(session, "APPLIB", "RPT001", "*PGM") == (
        "APPLIB", "QRPGSRC", "RPT001")


# --- Bulk (library-wide) scan --------------------------------------------------

def test_bulk_scan_maps_every_object():
    from lineage.extract.objinfo import source_locations_bulk

    session = FixtureHostSession(responses={
        "objstat.APPLIB.pgm.bulk": QueryResult(
            # Uppercase labels, as a live JDBC result reports them.
            columns=["OBJNAME", "SOURCE_LIBRARY", "SOURCE_FILE",
                     "SOURCE_MEMBER"],
            rows=[("RPT001", "APPLIB", "QRPGSRC", "RPT001"),
                  ("RPT002", "APPLIB", "QRPGSRC", "RPT002SRC"),
                  ("NOSRC", None, "  ", None)]),
    })
    locs = source_locations_bulk(session, "applib", "*PGM")
    assert locs == {
        "RPT001": ("APPLIB", "QRPGSRC", "RPT001"),
        "RPT002": ("APPLIB", "QRPGSRC", "RPT002SRC"),
        "NOSRC": None,          # exists, but no usable recorded source
    }
    assert "GHOST" not in locs   # absent == not in the library at all
    # Exactly one host query: the whole-library scan.
    assert len(session.sql_log) == 1
    assert "OBJECT_STATISTICS('APPLIB', '*PGM')" in session.sql_log[0]


def test_bulk_scan_failure_returns_none_not_empty():
    from lineage.extract.objinfo import source_locations_bulk

    session = FixtureHostSession(responses={})   # no fixture tag -> HostError
    assert source_locations_bulk(session, "APPLIB", "*PGM") is None
