"""Source-retrieval strategy tests: ifs_read (stateless) vs alias fallback."""
import pytest

from lineage.config import ConfigError, SourceFileRef, from_dict
from lineage.extract.source import (ifs_member_path, retrieve_member,
                                    retrieve_member_alias,
                                    retrieve_member_ifs)


def _cfg(mode: str):
    return from_dict({
        "scratch_lib": "SCRATCHX",
        "libraries": ["APPLIB"],
        "output_seeds": [{"id": "X", "library": "APPLIB", "file": "OUT"}],
        "source_retrieval": mode,
    })


SRC = SourceFileRef(library="TNTACCSRC", file="QDDLSRC")


def test_ifs_member_path():
    assert ifs_member_path(SRC, "broast") == \
        "/QSYS.LIB/TNTACCSRC.LIB/QDDLSRC.FILE/BROAST.MBR"


def test_default_mode_is_ifs_read():
    cfg = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["APPLIB"],
        "output_seeds": [{"id": "X", "library": "APPLIB", "file": "OUT"}],
    })
    assert cfg.source_retrieval == "ifs_read"


def test_invalid_mode_rejected():
    with pytest.raises(ConfigError, match="source_retrieval"):
        _cfg("carrier_pigeon")


def test_ifs_read_issues_no_ddl(session):
    lines = retrieve_member_ifs(session, SourceFileRef("APPLIB", "QRPGSRC"),
                                "RPT001")
    assert lines and lines[0][0] == 1
    assert any("IFS_READ" in q for q in session.sql_log)
    assert not any("CREATE ALIAS" in q or "DROP ALIAS" in q
                   for q in session.sql_log)


def test_alias_strategy_creates_and_drops(session):
    lines = retrieve_member_alias(session, SourceFileRef("APPLIB", "QRPGSRC"),
                                  "RPT001", scratch_lib="SCRATCHX")
    assert lines
    creates = [q for q in session.sql_log if q.startswith("CREATE ALIAS")]
    drops = [q for q in session.sql_log if q.startswith("DROP ALIAS")]
    assert creates and drops
    # Alias lives in the configured scratch library, and is always dropped.
    assert all("SCRATCHX.T_MBR" in q for q in creates + drops)
    assert len(drops) >= len(creates)


def test_dispatch_honours_config(session):
    retrieve_member(session, SourceFileRef("APPLIB", "QRPGSRC"), "RPT001",
                    _cfg("ifs_read"))
    assert not any("ALIAS" in q for q in session.sql_log)
    retrieve_member(session, SourceFileRef("APPLIB", "QRPGSRC"), "RPT001",
                    _cfg("alias"))
    assert any("CREATE ALIAS" in q for q in session.sql_log)


def test_both_strategies_return_identical_text(session):
    src = SourceFileRef("APPLIB", "QCLSRC")
    via_ifs = retrieve_member_ifs(session, src, "CLDRIVER")
    via_alias = retrieve_member_alias(session, src, "CLDRIVER",
                                      scratch_lib="QTEMP")
    assert [t for _, t in via_ifs] == [t for _, t in via_alias]
