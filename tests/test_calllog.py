"""Host-call log tests: JSONL records, error signatures, session wrapper."""
from __future__ import annotations

import json

from lineage.extract.calllog import HostCallLog, LoggingSession, _error_signature
from lineage.extract.connection import (FixtureHostSession, HostError,
                                        QueryResult)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _log(tmp_path, clock=None):
    return HostCallLog(tmp_path / "calls.jsonl", clock=clock or FakeClock())


def test_records_are_valid_jsonl_with_timestamp_and_duration(tmp_path):
    clock = FakeClock()
    log = HostCallLog(tmp_path / "calls.jsonl", clock=clock)
    log.record("sql", "SELECT 1\n  FROM X", "catalog.systables", 123.45)
    log.record("cl", "DSPFFD FILE(L/F)", None, 10.0,
               error=HostError("[CPF3064] Library L not found."))
    log.close()

    lines = [json.loads(ln) for ln in
             (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len(lines) == 2
    ok, err = lines
    assert ok["kind"] == "sql" and ok["ok"] is True
    assert ok["tag"] == "catalog.systables"
    assert ok["ms"] == 123.5 and "ts" in ok
    assert ok["text"] == "SELECT 1 FROM X"      # whitespace collapsed
    assert err["ok"] is False
    assert err["error"].startswith("[CPF3064]")


def test_summary_groups_error_signatures_and_ranks_slowest(tmp_path):
    log = _log(tmp_path)
    log.record("cl", "DSPFFD A", None, 5.0,
               error=HostError("[CPF3064] Library A not found."))
    log.record("cl", "DSPFFD B", None, 5.0,
               error=HostError("[CPF3064] Library B not found."))
    log.record("sql", "SELECT slow", "catalog.syscolumns", 64000.0)
    log.record("sql", "SELECT fast", "catalog.systables", 12.0)
    out = "\n".join(log.summary_lines())
    log.close()

    assert "host calls: 4 (2 errors)" in out
    assert "[2x] [CPF3064]" in out              # same signature, two libraries
    # slowest first, labeled by tag
    slow_idx = out.index("catalog.syscolumns")
    fast_idx = out.index("catalog.systables")
    assert slow_idx < fast_idx


def test_error_signature_falls_back_to_digitless_text():
    assert _error_signature("[SQL0204] X in Y type *FILE not found.") == "[SQL0204]"
    a = _error_signature("timeout after 30 seconds on row 12")
    b = _error_signature("timeout after 45 seconds on row 99")
    assert a == b


def test_logging_session_wraps_and_reraises(tmp_path):
    log = _log(tmp_path)
    inner = FixtureHostSession(responses={
        "t.ok": QueryResult(columns=["a"], rows=[(1,)])})
    session = LoggingSession(inner, log)

    res = session.with_tag("t.ok").query("SELECT 1")
    assert res.rows == [(1,)]

    try:
        session.with_tag("t.missing").query("SELECT 2")
        raised = False
    except HostError:
        raised = True
    assert raised                                # logging never swallows

    session.run_cl("DSPPGMREF PGM(L/*ALL)")
    assert inner.cl_log == ["DSPPGMREF PGM(L/*ALL)"]   # delegated
    assert session.sql_log == inner.sql_log            # __getattr__ passthrough
    log.close()

    lines = [json.loads(ln) for ln in log.path.read_text().splitlines()]
    assert [ln["ok"] for ln in lines] == [True, False, True]
    assert lines[0]["tag"] == "t.ok"
    assert lines[1]["tag"] == "t.missing"
    assert lines[2]["kind"] == "cl"


def test_progress_stamp_prefixes_lines():
    from lineage.extract.progress import Progress

    out: list[str] = []
    p = Progress(echo=out.append, clock=FakeClock(),
                 stamp=lambda: "12:34:56")
    p.phase("seed pass")
    p.note("hello")
    assert out == ["[12:34:56] == seed pass", "[12:34:56]   hello"]
