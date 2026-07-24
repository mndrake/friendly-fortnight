"""Host session interface and implementations.

``HostSession`` is the single seam between the analyzer and the IBM i. The real
implementation (:class:`JdbcHostSession`) uses jaydebeapi + IBM Toolbox for
Java and executes host commands through ``QSYS2.QCMDEXC`` over the same JDBC
connection. Tests and offline analysis use :class:`FixtureHostSession`, which
serves pre-canned tabular data from local files.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from ..config import ConnectionConfig


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r)) for r in self.rows]


@runtime_checkable
class HostSession(Protocol):
    """A read-only session against the IBM i (or a stand-in)."""

    def query(self, sql: str, params: Sequence[Any] = ()) -> QueryResult:
        """Run a SELECT and return all rows. Chunking is the caller's concern."""
        ...

    def run_cl(self, command: str) -> None:
        """Execute a host CL command (via QCMDEXC on the real host)."""
        ...

    def close(self) -> None:
        ...


class HostError(RuntimeError):
    pass


class JdbcHostSession:
    """Real IBM i session over jaydebeapi. Import of jaydebeapi is deferred so
    the rest of the toolchain runs without a JVM.
    """

    def __init__(self, config: ConnectionConfig, jar_path: str | None = None,
                 max_retries: int = 4):
        self._config = config
        self._jar = jar_path or config.jar
        self._max_retries = max_retries
        self._conn = None
        self._connect()

    def _connect(self) -> None:
        try:
            import jaydebeapi  # noqa: PLC0415  (optional dependency)
        except ImportError as exc:  # pragma: no cover - env dependent
            raise HostError(
                "jaydebeapi is required for live host access; install the "
                "'host' extra: pip install db2-lineage[host]"
            ) from exc

        url = self._config.resolved_url()
        props = dict(self._config.properties)
        if self._config.user:
            props.setdefault("user", self._config.user)
        pw = self._config.resolved_password()
        if pw:
            props["password"] = pw

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                self._conn = jaydebeapi.connect(
                    self._config.driver_class, url, props, self._jar,
                )
                return
            except Exception as exc:  # pragma: no cover - env dependent
                last_exc = exc
                time.sleep(2 ** (attempt + 1))
        raise HostError(f"failed to connect to {url}: {last_exc}")

    def query(self, sql: str, params: Sequence[Any] = ()) -> QueryResult:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, list(params))
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = [tuple(r) for r in cur.fetchall()] if columns else []
            return QueryResult(columns=columns, rows=rows)
        finally:
            cur.close()

    def run_cl(self, command: str) -> None:
        # QCMDEXC runs the command in the connection's job. Length arg is
        # optional on modern releases; QSYS2.QCMDEXC(command) is the SQL form.
        cur = self._conn.cursor()
        try:
            cur.execute("CALL QSYS2.QCMDEXC(?)", [command])
        finally:
            cur.close()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


class FixtureHostSession:
    """Offline host stand-in.

    Serves query results and records ``run_cl`` invocations. Two data sources:

    * a directory of JSON files keyed by a caller-supplied *tag* (the extract
      modules pass a stable tag alongside each query), or
    * an in-memory ``responses`` dict mapping tag -> ``QueryResult``.

    ``run_cl`` is a no-op that appends to :attr:`cl_log` so tests can assert the
    commands that would have been issued.
    """

    def __init__(self, responses: dict[str, QueryResult] | None = None,
                 fixture_dir: str | Path | None = None):
        self._responses = responses or {}
        self._dir = Path(fixture_dir) if fixture_dir else None
        self.cl_log: list[str] = []
        self.sql_log: list[str] = []
        self._last_tag: str | None = None

    def with_tag(self, tag: str) -> "FixtureHostSession":
        """Return a shallow view whose next query resolves under ``tag``.

        Extract modules call ``session.with_tag('catalog.systables').query(...)``.
        """
        self._last_tag = tag
        return self

    def query(self, sql: str, params: Sequence[Any] = ()) -> QueryResult:
        self.sql_log.append(sql)
        tag = self._last_tag
        self._last_tag = None
        if tag is None:
            # Best-effort: allow direct keying by exact SQL for simple cases.
            tag = sql.strip()
        if tag in self._responses:
            return self._responses[tag]
        if self._dir is not None:
            path = self._dir / f"{tag}.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return QueryResult(columns=data["columns"],
                                   rows=[tuple(r) for r in data["rows"]])
        raise HostError(f"no fixture response for tag '{tag}'")

    def run_cl(self, command: str) -> None:
        self.cl_log.append(command)

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def open_session(config: ConnectionConfig, jar_path: str | None = None) -> HostSession:
    return JdbcHostSession(config, jar_path=jar_path)
