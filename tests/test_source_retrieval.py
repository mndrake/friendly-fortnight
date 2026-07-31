"""Source-retrieval strategy tests: ifs_read (stateless) vs alias fallback."""
import pytest

from lineage.config import ConfigError, SourceFileRef, from_dict
from lineage.extract.connection import (FixtureHostSession, HostError,
                                        QueryResult)
from lineage.extract.source import (harvest, ifs_member_path,
                                    retrieve_member, retrieve_member_alias,
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


def test_auto_mode_resolves_to_ifs_read_without_profile(session):
    _, strategy = retrieve_member(session, SourceFileRef("APPLIB", "QRPGSRC"),
                                  "RPT001", _cfg("auto"))
    assert strategy == "ifs_read"


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
    # The bare FixtureHostSession has no fixture response for CREATE/DROP
    # ALIAS tags, so both create attempts raise HostError (caught, not
    # swallowed) and the retry-once path runs in full: CREATE, DROP,
    # CREATE, then the tagged SELECT succeeds, then a final DROP in the
    # finally block.
    assert len(creates) == 2
    assert len(drops) == 2
    assert [q.split(None, 2)[0] + " " + q.split(None, 2)[1]
            for q in session.sql_log[:5]] == [
        "CREATE ALIAS", "DROP ALIAS", "CREATE ALIAS", "SELECT SRCSEQ,",
        "DROP ALIAS",
    ]
    # Alias lives in the configured scratch library, and is always dropped.
    assert all("SCRATCHX.T_MBR" in q for q in creates + drops)


def test_dispatch_honours_config(session):
    _, strategy = retrieve_member(session, SourceFileRef("APPLIB", "QRPGSRC"),
                                  "RPT001", _cfg("ifs_read"))
    assert strategy == "ifs_read"
    assert not any("ALIAS" in q for q in session.sql_log)
    _, strategy = retrieve_member(session, SourceFileRef("APPLIB", "QRPGSRC"),
                                  "RPT001", _cfg("alias"))
    assert strategy == "alias"
    assert any("CREATE ALIAS" in q for q in session.sql_log)


class _IfsBlindSession:
    """Stub host where IFS_READ silently returns nothing (its real failure
    mode for data-PF members / CCSID 65535) but record-level SQL works."""

    def __init__(self):
        self.sql_log = []

    def query(self, sql, params=()):
        from lineage.extract.connection import QueryResult
        self.sql_log.append(sql)
        if "IFS_READ" in sql:
            return QueryResult(columns=["LINE_NUMBER", "LINE"], rows=[])
        if "SRCSEQ" in sql:
            return QueryResult(columns=["SRCSEQ", "SRCDTA"],
                               rows=[(1, "CREATE TABLE BROAST (X INT)")])
        return QueryResult(columns=[], rows=[])

    def run_cl(self, command):  # pragma: no cover - unused
        pass

    def close(self):  # pragma: no cover - unused
        pass


def test_ifs_read_empty_falls_back_to_alias():
    session = _IfsBlindSession()
    lines, strategy = retrieve_member(
        session, SourceFileRef("TNTACCSRC", "QDDLSRC"), "BROAST",
        _cfg("ifs_read"))
    assert strategy == "alias_fallback"
    assert lines == [(1, "CREATE TABLE BROAST (X INT)")]
    assert any("IFS_READ" in q for q in session.sql_log)
    assert any("CREATE ALIAS" in q for q in session.sql_log)


def test_genuinely_empty_member_does_not_loop():
    class _AllEmpty(_IfsBlindSession):
        def query(self, sql, params=()):
            from lineage.extract.connection import QueryResult
            self.sql_log.append(sql)
            return QueryResult(columns=["LINE_NUMBER", "LINE"], rows=[])

    session = _AllEmpty()
    lines, strategy = retrieve_member(
        session, SourceFileRef("APPLIB", "QRPGSRC"), "EMPTYMBR",
        _cfg("ifs_read"))
    assert lines == []
    assert strategy == "ifs_read"


def test_both_strategies_return_identical_text(session):
    src = SourceFileRef("APPLIB", "QCLSRC")
    via_ifs = retrieve_member_ifs(session, src, "CLDRIVER")
    via_alias = retrieve_member_alias(session, src, "CLDRIVER",
                                      scratch_lib="QTEMP")
    assert [t for _, t in via_ifs] == [t for _, t in via_alias]


# --- Alias retry / rich-error diagnostics (create-error swallowing fix) ---


class _CreateFailsOnceSession:
    """CREATE ALIAS fails on the first attempt, succeeds on the retry; the
    SELECT then succeeds normally."""

    def __init__(self):
        self.sql_log = []
        self._create_calls = 0

    def query(self, sql, params=()):
        self.sql_log.append(sql)
        if sql.startswith("CREATE ALIAS"):
            self._create_calls += 1
            if self._create_calls == 1:
                raise RuntimeError("SQL0204 T_MBR in QTEMP not found")
            return QueryResult(columns=[], rows=[])
        if sql.startswith("DROP ALIAS"):
            return QueryResult(columns=[], rows=[])
        if "SRCSEQ" in sql:
            return QueryResult(columns=["SRCSEQ", "SRCDTA"],
                               rows=[(1, "CREATE TABLE BROAST (X INT)")])
        return QueryResult(columns=[], rows=[])

    def run_cl(self, command):  # pragma: no cover - unused
        pass

    def close(self):  # pragma: no cover - unused
        pass


def test_create_fails_once_retry_succeeds_then_select_succeeds():
    session = _CreateFailsOnceSession()
    lines = retrieve_member_alias(session, SourceFileRef("TNTACCSRC", "QDDLSRC"),
                                  "BROAST", scratch_lib="SCRATCHX")
    assert lines == [(1, "CREATE TABLE BROAST (X INT)")]
    assert session.sql_log[0].startswith("CREATE ALIAS")
    assert session.sql_log[1].startswith("DROP ALIAS")
    assert session.sql_log[2].startswith("CREATE ALIAS")
    assert session.sql_log[3].startswith("SELECT")


class _AllFailSession:
    """Both CREATE ALIAS attempts fail, and the SELECT also fails — the
    scenario that produced the inscrutable SQL0204 on the live host."""

    def __init__(self):
        self.sql_log = []
        self._create_calls = 0

    def query(self, sql, params=()):
        self.sql_log.append(sql)
        if sql.startswith("CREATE ALIAS"):
            self._create_calls += 1
            raise RuntimeError(f"SQL0204 CREATE ALIAS failure #{self._create_calls}")
        if sql.startswith("DROP ALIAS"):
            return QueryResult(columns=[], rows=[])
        if "SRCSEQ" in sql:
            raise RuntimeError("SQL0204 T_MBR in QTEMP not found")
        return QueryResult(columns=[], rows=[])

    def run_cl(self, command):  # pragma: no cover - unused
        pass

    def close(self):  # pragma: no cover - unused
        pass


def test_both_creates_and_select_fail_raises_rich_host_error():
    session = _AllFailSession()
    with pytest.raises(HostError) as excinfo:
        retrieve_member_alias(session, SourceFileRef("TNTACCSRC", "QDDLSRC"),
                              "BROAST", scratch_lib="SCRATCHX")
    msg = str(excinfo.value)
    assert "TNTACCSRC/QDDLSRC(BROAST)" in msg
    assert "SQL0204 T_MBR in QTEMP not found" in msg  # the SELECT error
    assert "SQL0204 CREATE ALIAS failure #1" in msg  # the FIRST create error
    assert "JOBLOG_INFO" in msg
    # The DROP in the finally still ran despite the raise.
    assert any(q.startswith("DROP ALIAS") for q in session.sql_log)


class _CreateOkSelectFailsSession:
    """CREATE ALIAS succeeds outright; the SELECT still fails for some other
    reason (e.g. authority, transient host issue)."""

    def __init__(self):
        self.sql_log = []

    def query(self, sql, params=()):
        self.sql_log.append(sql)
        if sql.startswith("CREATE ALIAS") or sql.startswith("DROP ALIAS"):
            return QueryResult(columns=[], rows=[])
        if "SRCSEQ" in sql:
            raise RuntimeError("SQL0501 cursor not open")
        return QueryResult(columns=[], rows=[])

    def run_cl(self, command):  # pragma: no cover - unused
        pass

    def close(self):  # pragma: no cover - unused
        pass


def test_create_succeeds_select_fails_reports_create_success():
    session = _CreateOkSelectFailsSession()
    with pytest.raises(HostError) as excinfo:
        retrieve_member_alias(session, SourceFileRef("TNTACCSRC", "QDDLSRC"),
                              "BROAST", scratch_lib="SCRATCHX")
    msg = str(excinfo.value)
    assert "create reported success" in msg
    assert "SQL0501 cursor not open" in msg
    # Only one create attempt: no failure to trigger a retry.
    creates = [q for q in session.sql_log if q.startswith("CREATE ALIAS")]
    assert len(creates) == 1


# --- harvest() resilience: one bad member must not kill the run ---


class _HarvestOneBadMemberSession:
    """Enumerates two members; the first's alias create fails on both
    attempts and its SELECT also fails, the second retrieves cleanly."""

    def __init__(self):
        self.sql_log = []
        self._create_calls = 0
        self._select_calls = 0

    def with_tag(self, tag):
        return self

    def query(self, sql, params=()):
        self.sql_log.append(sql)
        if "SYSPARTITIONSTAT" in sql:
            return QueryResult(columns=["member", "member_type"],
                               rows=[("BADMBR", "RPGLE"), ("GOODMBR", "RPGLE")])
        if sql.startswith("CREATE ALIAS"):
            self._create_calls += 1
            if self._create_calls <= 2:  # both attempts for BADMBR fail
                raise RuntimeError("SQL0204 T_MBR in QTEMP not found")
            return QueryResult(columns=[], rows=[])
        if sql.startswith("DROP ALIAS"):
            return QueryResult(columns=[], rows=[])
        if "SRCSEQ" in sql:
            self._select_calls += 1
            if self._select_calls == 1:  # BADMBR's SELECT also fails
                raise RuntimeError("SQL0204 T_MBR in QTEMP not found")
            return QueryResult(columns=["SRCSEQ", "SRCDTA"],
                               rows=[(1, "GOOD LINE")])
        return QueryResult(columns=[], rows=[])

    def run_cl(self, command):  # pragma: no cover - unused
        pass

    def close(self):  # pragma: no cover - unused
        pass


def test_harvest_counts_one_bad_member_and_keeps_the_good_one(con):
    session = _HarvestOneBadMemberSession()
    config = from_dict({
        "scratch_lib": "SCRATCHX",
        "libraries": ["TNTACCSRC"],
        "output_seeds": [{"id": "X", "library": "TNTACCSRC", "file": "OUT"}],
        "source_retrieval": "alias",
        "source_files": [{"library": "TNTACCSRC", "file": "QDDLSRC"}],
    })
    counts = harvest(session, con, config)
    assert counts["member_retrieval_failures"] == 1
    assert counts["raw_source_members"] == 1
    rows = con.execute(
        "SELECT member, line_text FROM raw_source_members").fetchall()
    assert rows == [("GOODMBR", "GOOD LINE")]


# --- Live JDBC label case: aliases come back uppercased -----------------------


class _UppercaseLabelSession:
    """Live-host shape: DB2 folds unquoted `AS member` aliases to uppercase,
    so result columns arrive as MEMBER/MEMBER_TYPE — unlike fixtures."""

    def __init__(self):
        self.sql_log = []

    def query(self, sql, params=()):
        self.sql_log.append(sql)
        if "SYSPARTITIONSTAT" in sql:
            return QueryResult(columns=["MEMBER", "MEMBER_TYPE"],
                               rows=[("BROAST", "SQL"), ("  ", None)])
        return QueryResult(columns=[], rows=[])

    def run_cl(self, command):  # pragma: no cover - unused
        pass

    def close(self):  # pragma: no cover - unused
        pass


def test_enumerate_members_handles_uppercase_jdbc_labels():
    """The live-host bug: `.get("member")` returned None because the driver
    labeled the column MEMBER, yielding an empty member name and a
    CREATE ALIAS ...FILE() SQL0104. Keys are now case-normalised, and rows
    with blank member names are dropped."""
    from lineage.extract.source import enumerate_members

    members = enumerate_members(_UppercaseLabelSession(), SRC)
    assert members == [{"member": "BROAST", "member_type": "SQL"}]


# --- Adaptive strategy demotion (auto mode only) -------------------------------

def _ifs_dead_session(members: list[str]) -> FixtureHostSession:
    """Every IFS_READ returns zero rows; every alias read succeeds — the
    systematic-fallback estate (data-PF source files / CCSID 65535)."""
    responses = {
        "source.members.APPLIB.QCLSRC": QueryResult(
            columns=["member", "member_type"],
            rows=[(m, "CLP") for m in members]),
    }

    class _Session(FixtureHostSession):
        def query(self, sql, params=()):
            if "IFS_READ" in sql:
                self.sql_log.append(sql)
                self._last_tag = None
                return QueryResult(columns=["LINE_NUMBER", "LINE"], rows=[])
            if "SRCSEQ" in sql:
                self.sql_log.append(sql)
                self._last_tag = None
                return QueryResult(columns=["SRCSEQ", "SRCDTA"],
                                   rows=[(1, "PGM"), (2, "ENDPGM")])
            if sql.startswith(("CREATE ALIAS", "DROP ALIAS")):
                self.sql_log.append(sql)
                self._last_tag = None
                return QueryResult(columns=[], rows=[])
            return super().query(sql)

    return _Session(responses=responses)


def test_auto_mode_demotes_after_consecutive_fallbacks():
    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.extract import source as source_mod

    members = [f"M{i:02d}" for i in range(1, 11)]   # 10 members
    session = _ifs_dead_session(members)
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["APPLIB"],
        "source_files": [{"library": "APPLIB", "file": "QCLSRC"}],
        "output_seeds": [{"id": "X", "library": "APPLIB", "file": "OUT1"}],
        "liblists": {"default": ["APPLIB"]},
    })
    con = dbmod.connect(None)
    counts = source_mod.harvest(session, con, config)

    assert counts["source_strategy_demoted"] == 1
    ifs_queries = [q for q in session.sql_log if "IFS_READ" in q]
    # Exactly DEMOTE_AFTER_FALLBACKS wasted probes, then straight to alias.
    assert len(ifs_queries) == source_mod.DEMOTE_AFTER_FALLBACKS
    # Every member still retrieved.
    assert dbmod.table_count(con, "raw_source_members") == len(members) * 2
    con.close()


def test_ifs_success_resets_demotion_counter():
    from lineage.config import from_dict
    from lineage.extract import source as source_mod
    from lineage.extract.source import RetrievalStats, retrieve_member
    from lineage.config import SourceFileRef

    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["APPLIB"],
        "source_files": [{"library": "APPLIB", "file": "QCLSRC"}],
        "output_seeds": [{"id": "X", "library": "APPLIB", "file": "OUT1"}],
        "liblists": {"default": ["APPLIB"]},
    })
    stats = RetrievalStats(consecutive_fallbacks=source_mod.DEMOTE_AFTER_FALLBACKS - 1)
    src = SourceFileRef(library="APPLIB", file="QCLSRC")
    session = FixtureHostSession(responses={
        "source.text.APPLIB.QCLSRC.GOODMBR": QueryResult(
            columns=["LINE_NUMBER", "LINE"], rows=[(1, "PGM")]),
    })
    lines, strategy = retrieve_member(session, src, "GOODMBR", config,
                                      stats=stats)
    assert strategy == "ifs_read"
    assert stats.consecutive_fallbacks == 0     # reset, no demotion
    assert not stats.demoted


def test_explicit_ifs_read_mode_never_demotes():
    import dataclasses

    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.extract import source as source_mod

    members = [f"M{i:02d}" for i in range(1, 11)]
    session = _ifs_dead_session(members)
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["APPLIB"],
        "source_files": [{"library": "APPLIB", "file": "QCLSRC"}],
        "output_seeds": [{"id": "X", "library": "APPLIB", "file": "OUT1"}],
        "liblists": {"default": ["APPLIB"]},
        "source_retrieval": "ifs_read",
    })
    con = dbmod.connect(None)
    counts = source_mod.harvest(session, con, config)

    assert "source_strategy_demoted" not in counts
    ifs_queries = [q for q in session.sql_log if "IFS_READ" in q]
    assert len(ifs_queries) == len(members)   # explicit mode: obeyed verbatim
    con.close()
