"""Outfile layout *resolution* tests: probe actual columns, resolve candidate
field names against them (Phase 2 — the fix for the live SQL0206 on
DSPPGMREF, see extract/xref.py module docstring and the plan at
there-are-older-dds-sleepy-octopus.md).

``test_xref_mapping.py`` covers the pure ``map_*`` functions (post-aliasing);
this file covers ``OutfileLayout.resolve()`` itself, the probe-then-select
flow in ``_select_outfile``, and an end-to-end "realistic host" harvest whose
outfile columns are the true IBM-documented WH* names — the shape that would
have caught the live bug (our old hardcoded ``WHNCNT`` field does not exist
on a real DSPPGMREF outfile).
"""
from __future__ import annotations

import pytest

from lineage import db as dbmod
from lineage.extract.connection import HostError, QueryResult
from lineage.extract.xref import (DBR_COLUMNS, DBR_LAYOUT, FFD_COLUMNS,
                                  FFD_LAYOUT, PGMREF_COLUMNS, PGMREF_LAYOUT,
                                  OutfileShapeError, harvest_dbr,
                                  harvest_ffd, harvest_pgmref)

# Full documented QADSPPGM/QWHDRPPR field list (see IBM "Detailed File Field
# Description for File QADSPPGM"). WHNCNT (our old invented ref-count field)
# is deliberately absent -- that absence is exactly what broke the live host.
_QWHDRPPR_FULL = ["WHLIB", "WHPNAM", "WHTEXT", "WHFNUM", "WHDTTM", "WHFNAM",
                  "WHLNAM", "WHSNAM", "WHRFNO", "WHFUSG", "WHRFNM", "WHRFSN",
                  "WHRFFN", "WHOBJT", "WHOTYP", "WHSYSN", "WHSPKG"]


# --- resolve() unit tests -----------------------------------------------------

def test_resolve_against_full_documented_pgmref_columns():
    select_list, missing = PGMREF_LAYOUT.resolve(_QWHDRPPR_FULL)
    assert "WHOTYP AS object_type" in select_list       # preferred over WHOBJT
    assert "WHFUSG AS usage_flag" in select_list
    assert "WHLNAM AS object_lib" in select_list
    assert "WHFNAM AS object_name" in select_list
    assert "WHLIB AS program_lib" in select_list
    assert "WHPNAM AS program_name" in select_list
    # ref_count has no documented candidate on this outfile -> NULL-filled,
    # and reported missing (it was our invented WHNCNT field).
    assert missing == ["ref_count"]
    assert "CAST(NULL AS VARCHAR(1)) AS ref_count" in select_list


def test_resolve_falls_back_to_whobjt_when_whotyp_absent():
    cols = [c for c in _QWHDRPPR_FULL if c != "WHOTYP"]
    assert "WHOBJT" in cols
    select_list, _missing = PGMREF_LAYOUT.resolve(cols)
    assert "WHOBJT AS object_type" in select_list
    assert "WHOTYP AS object_type" not in select_list


def test_resolve_identity_columns_unchanged_fixture_shape():
    """A source already exposing our raw names (the fixture estate) resolves
    with no candidate substitution — the identity rule."""
    select_list, missing = PGMREF_LAYOUT.resolve(PGMREF_LAYOUT.raw_columns)
    for raw in PGMREF_LAYOUT.raw_columns:
        assert f"{raw} AS {raw}" in select_list
    assert missing == []   # ref_count resolves via identity too, not NULL


def test_resolve_required_miss_raises_with_actual_columns_and_candidates():
    actual = ["WHPNAM", "WHLNAM", "WHFNAM", "WHOTYP", "WHFUSG"]  # no WHLIB
    with pytest.raises(OutfileShapeError) as exc_info:
        PGMREF_LAYOUT.resolve(actual)
    msg = str(exc_info.value)
    assert "WHLIB" in msg
    assert "program_lib" in msg
    assert str(sorted(actual)) in msg
    err = exc_info.value
    assert err.raw_column == "program_lib"
    assert "WHLIB" in err.candidates_tried
    assert err.actual_columns == tuple(actual)


def test_resolve_dbr_based_on_prefers_whrfi_whrli_over_whfile_whlib():
    cols = ["WHRELI", "WHREFI", "WHRLI", "WHRFI", "WHLIB", "WHFILE", "WHRTYP"]
    select_list, missing = DBR_LAYOUT.resolve(cols)
    assert "WHRELI AS dep_lib" in select_list
    assert "WHREFI AS dep_file" in select_list
    assert "WHRLI AS based_lib" in select_list
    assert "WHRFI AS based_file" in select_list
    assert "WHLIB AS based_lib" not in select_list
    assert "WHFILE AS based_file" not in select_list
    assert missing == []


def test_resolve_dbr_falls_back_to_whfile_whlib_when_whrfi_whrli_absent():
    cols = ["WHRELI", "WHREFI", "WHLIB", "WHFILE"]
    select_list, missing = DBR_LAYOUT.resolve(cols)
    assert "WHLIB AS based_lib" in select_list
    assert "WHFILE AS based_file" in select_list
    assert missing == ["dep_type"]


def test_resolve_ffd_field_name_prefers_whfldi_over_whflde():
    cols = ["WHLIB", "WHFILE", "WHNAME", "WHFLDI", "WHFLDE", "WHFLDT"]
    select_list, _missing = FFD_LAYOUT.resolve(cols)
    assert "WHFLDI AS field_name" in select_list
    assert "WHFLDE AS field_name" not in select_list


# --- Realistic-host end-to-end: true WH* outfile columns ----------------------

class _RealisticHostSession:
    """A minimal HostSession stand-in that, unlike FixtureHostSession, tells
    the probe select and the resolved data select apart and *validates* the
    data select only references columns the probe actually reported —
    exactly like a real DB2 for i SQL0206 would reject an unresolvable
    column. This is the shape that catches the live bug: the old hardcoded
    layout always emitted ``WHNCNT AS ref_count`` regardless of what the
    outfile actually has, which this stub would reject.
    """

    def __init__(self, probe: dict[str, QueryResult], data: dict[str, QueryResult]):
        self._probe = probe
        self._data = data
        self._last_tag: str | None = None
        self.cl_log: list[str] = []
        self.sql_log: list[str] = []

    def with_tag(self, tag: str) -> "_RealisticHostSession":
        self._last_tag = tag
        return self

    def query(self, sql: str, params=()) -> QueryResult:
        self.sql_log.append(sql)
        tag = self._last_tag
        self._last_tag = None
        if tag is None:
            tag = sql.strip()
        if "SELECT *" in sql:
            return self._probe[tag]
        probed = {c.upper() for c in self._probe[tag].columns}
        select_part = sql[len("SELECT "):sql.upper().index(" FROM ")]
        for piece in select_part.split(", "):
            src = piece.rsplit(" AS ", 1)[0].strip()
            if src.upper().startswith("CAST(NULL"):
                continue
            if src.upper() not in probed:
                raise HostError(
                    f"SQL0206: {src} not found in outfile for tag '{tag}' "
                    f"(actual columns: {sorted(probed)})")
        return self._data[tag]

    def run_cl(self, command: str) -> None:
        self.cl_log.append(command)

    def close(self) -> None:
        pass


def _realistic_session() -> _RealisticHostSession:
    probe = {
        "xref.dsppgmref": QueryResult(
            columns=_QWHDRPPR_FULL,
            rows=[tuple(None for _ in _QWHDRPPR_FULL)]),
        "xref.dspdbr": QueryResult(
            columns=["WHRELI", "WHREFI", "WHRLI", "WHRFI", "WHLIB", "WHFILE",
                     "WHRTYP"],
            rows=[(None,) * 7]),
        "xref.dspffd": QueryResult(
            columns=["WHLIB", "WHFILE", "WHNAME", "WHFLDI", "WHFLDE",
                     "WHFLDT", "WHFLDB", "WHFLDP", "WHFTXT", "WHFLDN"],
            rows=[(None,) * 10]),
    }
    data = {
        "xref.dsppgmref": QueryResult(
            columns=PGMREF_COLUMNS,
            rows=[("APPLIB", "RPT001", "APPLIB", "CUSTRPT", "F", "2", None)]),
        "xref.dspdbr": QueryResult(
            columns=DBR_COLUMNS,
            rows=[("APPLIB", "CUSTLF1", "APPLIB", "CUSTMAST", "D")]),
        "xref.dspffd": QueryResult(
            columns=FFD_COLUMNS,
            rows=[("APPLIB", "ORDERS", "ORDERSR", "AMOUNT", "P", 9, 0,
                   "Order amount", 3)]),
    }
    return _RealisticHostSession(probe, data)


def _synthetic_config():
    from lineage.config import from_dict
    return from_dict({
        "scratch_lib": "QTEMP",
        "libraries": ["APPLIB"],
        "output_seeds": [{"id": "X", "library": "APPLIB", "file": "CUSTRPT"}],
        "liblists": {"default": ["APPLIB"]},
    })


def test_realistic_host_pgmref_dbr_ffd_harvest_maps_correctly():
    session = _realistic_session()
    con = dbmod.connect(None)
    config = _synthetic_config()

    harvest_pgmref(session, con, config)
    harvest_dbr(session, con, config)
    harvest_ffd(session, con, config)

    pgmref_rows = con.execute(
        "SELECT program_lib, program_name, object_lib, object_name, "
        "object_type, usage_flag, ref_count FROM raw_dsppgmref").fetchall()
    assert pgmref_rows == [("APPLIB", "RPT001", "APPLIB", "CUSTRPT", "F",
                            "2", None)]

    dbr_rows = con.execute(
        "SELECT dep_lib, dep_file, based_lib, based_file, dep_type "
        "FROM raw_dspdbr").fetchall()
    assert dbr_rows == [("APPLIB", "CUSTLF1", "APPLIB", "CUSTMAST", "D")]

    ffd_rows = con.execute(
        "SELECT file_lib, file_name, record_format, field_name, field_type, "
        "field_length, field_scale, field_text, field_ordinal "
        "FROM raw_dspffd").fetchall()
    assert ffd_rows == [("APPLIB", "ORDERS", "ORDERSR", "AMOUNT", "P", 9, 0,
                         "Order amount", 3)]

    # The generated SQL actually reflects the corrected candidate order —
    # this is what would SQL0206 on the live host under the old hardcoded
    # layout (WHNCNT does not exist; based-on used WHFILE/WHLIB instead of
    # WHRFI/WHRLI).
    pgmref_sql = [s for s in session.sql_log if "SMOKEPR" not in s
                 and s.startswith("SELECT ") and "PGMREF" in s
                 and not s.startswith("SELECT *")]
    assert any("WHOTYP AS object_type" in s for s in pgmref_sql)
    assert any("CAST(NULL AS VARCHAR(1)) AS ref_count" in s
              for s in pgmref_sql)
    assert not any("WHNCNT" in s for s in pgmref_sql)

    dbr_sql = [s for s in session.sql_log if "DBR" in s
              and not s.startswith("SELECT *")]
    assert any("WHRFI AS based_file" in s for s in dbr_sql)
    assert any("WHRLI AS based_lib" in s for s in dbr_sql)

    con.close()


def test_required_whncnt_style_layout_fails_at_resolve_time():
    """A layout that (like the old hardcoded code) demands ``WHNCNT`` now
    fails client-side at resolve time against the true QWHDRPPR column set —
    the failure mode that previously surfaced only as a host SQL0206 is
    caught before any SQL is sent. (The old code's actual SQL0206 against
    the realistic host is exercised by `_RealisticHostSession`'s SQL-column
    validation in the harvest tests above, which rejects any generated
    select referencing an unprobed column.)"""
    from lineage.extract import xref as xref_mod

    broken = xref_mod.OutfileLayout(
        name="dsppgmref", model_file="QSYS/QADSPPGM",
        record_format="QWHDRPPR",
        fields=PGMREF_LAYOUT.fields[:-1] + (("ref_count", ("WHNCNT",), True),),
    )
    session = _realistic_session()
    with pytest.raises(OutfileShapeError, match="WHNCNT"):
        xref_mod._select_outfile(session, "QTEMP", "PGMREF", broken)


def test_full_mode_multi_library_probes_each_layout_once(con, session):
    """harvest() shares one resolution cache across its per-library loop:
    three libraries must still yield exactly three probes (one per outfile
    layout), not nine."""
    from lineage.config import from_dict
    from lineage.extract import xref as xref_mod

    config3 = from_dict({
        "scratch_lib": "QTEMP",
        "libraries": ["APPLIB", "LIBB", "LIBC"],
        "output_seeds": [{"id": "X", "library": "APPLIB", "file": "CUSTRPT"}],
        "liblists": {"default": ["APPLIB"]},
    })
    xref_mod.harvest(session, con, config3)
    probes = [q for q in session.sql_log if q.startswith("SELECT * FROM")]
    assert len(probes) == 3


# --- Probe-once caching over per-file mode ------------------------------------

def test_ffd_per_file_harvest_probes_once_per_call(session, con, config):
    """Per-file mode (targeted extraction) issues one DSPFFD+select per file;
    the outfile-shape probe must run once per harvest_ffd call, not once per
    file."""
    files = [("APPLIB", "CUSTMAST"), ("APPLIB", "ORDERS"),
             ("APPLIB", "ORDHIST")]
    harvest_ffd(session, con, config, files=files)

    probe_selects = [s for s in session.sql_log if s.startswith("SELECT *")]
    data_selects = [s for s in session.sql_log
                    if s.startswith("SELECT ") and not s.startswith("SELECT *")]
    assert len(probe_selects) == 1
    assert len(data_selects) == 3
