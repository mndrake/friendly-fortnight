"""Timestamped host-call log for troubleshooting and bottleneck analysis.

A targeted extract interleaves thousands of SQL queries and CL commands over
hours; when exceptions or slowdowns appear mid-run, progress lines alone
can't answer "which call, when, how long, failing with what". Every host
call goes through :class:`LoggingSession` (a transparent wrapper around any
``HostSession``) and is appended to a JSONL file — one object per call with
an ISO timestamp, elapsed milliseconds, the extract layer's fixture tag
(``catalog.syscolumns``, ``objstat.LIB.NAME.pgm``, ...), the statement text
(whitespace-collapsed, truncated), and the error's first line when it
failed. Exceptions are recorded and **re-raised** — logging never changes
behavior.

JSONL so the store's own engine can analyze it, e.g. the bottleneck query::

    SELECT tag, count(*) AS calls, sum(ms)/1000 AS secs
    FROM read_json_auto('data/logs/host-calls-*.jsonl')
    GROUP BY tag ORDER BY secs DESC LIMIT 15;

:class:`HostCallLog` also keeps cheap in-memory aggregates (call/error
counts, slowest calls, error signatures keyed by message id) so the CLI can
print a summary at the end of the run without re-reading the file.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

_TEXT_LIMIT = 400
_SLOWEST_KEEP = 10
_SLOWEST_PRUNE_AT = 400

# IBM i message ids ([CPF3064], [SQL0204], [MCH1234]) are the natural error
# signature; messages without one collapse by digit-stripping so counts
# group ("Library X not found" repeated across libraries).
_MSGID_RE = re.compile(r"\[(?:CPF|SQL|MCH|CPD|CPI)\w+\]")


def _error_signature(msg: str) -> str:
    m = _MSGID_RE.search(msg)
    if m:
        return m.group(0)
    return re.sub(r"\d+", "N", msg)[:80]


class HostCallLog:
    """Append-only JSONL log of host calls plus in-memory summary state."""

    def __init__(self, path: str | Path,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.now):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self.clock = clock
        self._now = now
        self.n_calls = 0
        self.n_errors = 0
        self.total_ms = 0.0
        self._slowest: list[tuple[float, str, str]] = []  # (ms, kind, label)
        self._error_sigs: dict[str, int] = {}

    def record(self, kind: str, text: str, tag: Optional[str], ms: float,
               error: Exception | None = None) -> None:
        self.n_calls += 1
        self.total_ms += ms
        flat = " ".join((text or "").split())
        entry: dict[str, Any] = {
            "ts": self._now().isoformat(timespec="milliseconds"),
            "kind": kind, "ms": round(ms, 1), "tag": tag,
            "ok": error is None, "text": flat[:_TEXT_LIMIT],
        }
        if error is not None:
            self.n_errors += 1
            txt = str(error).strip()
            first = txt.splitlines()[0][:300] if txt else repr(error)
            entry["error"] = first
            sig = _error_signature(first)
            self._error_sigs[sig] = self._error_sigs.get(sig, 0) + 1
        self._fh.write(json.dumps(entry) + "\n")
        self._slowest.append((ms, kind, tag or flat[:80]))
        if len(self._slowest) > _SLOWEST_PRUNE_AT:
            self._slowest.sort(reverse=True)
            del self._slowest[_SLOWEST_KEEP:]

    def summary_lines(self) -> list[str]:
        lines = [
            f"host calls: {self.n_calls} ({self.n_errors} errors), "
            f"total host time {self.total_ms / 1000:.0f}s — log: {self.path}"
        ]
        top = sorted(self._slowest, reverse=True)[:5]
        if top:
            lines.append("slowest host calls:")
            for ms, kind, label in top:
                lines.append(f"  {ms / 1000:7.1f}s  {kind:3}  {label}")
        if self._error_sigs:
            lines.append("error signatures:")
            for sig, cnt in sorted(self._error_sigs.items(),
                                   key=lambda kv: (-kv[1], kv[0]))[:5]:
                lines.append(f"  [{cnt}x] {sig}")
        return lines

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass


class LoggingSession:
    """Transparent HostSession wrapper: times and logs every call.

    ``with_tag`` always exists on the wrapper, so live JDBC sessions get
    their calls logged under the same tags fixtures use — the tag is kept
    for the *next* call's log line and forwarded to the inner session when
    it supports tags itself. Everything else (``cl_log``, ``sql_log``,
    ``database_version``, ...) delegates to the wrapped session untouched.
    """

    def __init__(self, inner, log: HostCallLog):
        self._inner = inner
        self.log = log
        self._tag: Optional[str] = None

    def with_tag(self, tag: str) -> "LoggingSession":
        self._tag = tag
        if hasattr(self._inner, "with_tag"):
            self._inner.with_tag(tag)
        return self

    def query(self, sql: str, params: Sequence[Any] = ()):
        tag, self._tag = self._tag, None
        t0 = self.log.clock()
        try:
            res = self._inner.query(sql, params)
        except Exception as exc:
            self.log.record("sql", sql, tag, (self.log.clock() - t0) * 1000,
                            error=exc)
            raise
        self.log.record("sql", sql, tag, (self.log.clock() - t0) * 1000)
        return res

    def run_cl(self, command: str) -> None:
        tag, self._tag = self._tag, None
        t0 = self.log.clock()
        try:
            self._inner.run_cl(command)
        except Exception as exc:
            self.log.record("cl", command, tag,
                            (self.log.clock() - t0) * 1000, error=exc)
            raise
        self.log.record("cl", command, tag, (self.log.clock() - t0) * 1000)

    def close(self) -> None:
        self._inner.close()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)
