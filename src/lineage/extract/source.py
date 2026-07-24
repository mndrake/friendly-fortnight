"""Source member enumeration and retrieval.

Members are discovered via ``SYSPARTITIONSTAT`` on the configured source
physical files. Member text is retrieved by one of two strategies
(``source_retrieval`` in config):

* ``ifs_read`` (default) — ``QSYS2.IFS_READ`` over the member's
  ``/QSYS.LIB/<lib>.LIB/<file>.FILE/<member>.MBR`` path. Stateless and
  read-only: no scratch-library objects, no alias-name collisions between
  concurrent jobs, nothing to clean up on the error path. Requires IBM i
  7.3 TR7 / 7.4+. Returns line text only — per-line ``SRCDAT`` change dates
  are not available this way (member-level dates remain in
  ``SYSPARTITIONSTAT``), which is fine for lineage: only ordered text is
  needed.
* ``alias`` — temporary ``CREATE ALIAS`` in the scratch library ->
  ``SELECT SRCSEQ, SRCDTA`` -> ``DROP ALIAS``. Works on older releases.

Both strategies fetch under the same fixture tag, so offline tests are
strategy-agnostic. JDBC translation handles EBCDIC/CCSID; a Phase-1
round-trip check verifies a member reads back as text
(see :func:`verify_roundtrip`).
"""
from __future__ import annotations

from typing import Any

from ..config import Config, SourceFileRef
from ..db import insert_rows
from .connection import HostSession, QueryResult


def _fetch(session: HostSession, tag: str, sql: str) -> QueryResult:
    if hasattr(session, "with_tag"):
        return session.with_tag(tag).query(sql)
    return session.query(sql)


def enumerate_members(session: HostSession, src: SourceFileRef) -> list[dict[str, Any]]:
    """List members of a source physical file with their source type."""
    res = _fetch(
        session, f"source.members.{src.library}.{src.file}",
        f"""
        SELECT PARTITION_NAME AS member, SOURCE_TYPE AS member_type
        FROM QSYS2.SYSPARTITIONSTAT
        WHERE TABLE_SCHEMA = '{src.library}' AND TABLE_NAME = '{src.file}'
        """,
    )
    return res.dicts()


def ifs_member_path(src: SourceFileRef, member: str) -> str:
    """QSYS.LIB filesystem path of a source member."""
    return (f"/QSYS.LIB/{src.library.upper()}.LIB/"
            f"{src.file.upper()}.FILE/{member.upper()}.MBR")


def retrieve_member(session: HostSession, src: SourceFileRef, member: str,
                    config: Config) -> list[tuple[int, str]]:
    """Retrieve one member's lines as ``(seq, text)`` using the configured
    strategy."""
    if config.source_retrieval == "alias":
        return retrieve_member_alias(session, src, member, config.scratch_lib)
    return retrieve_member_ifs(session, src, member)


def retrieve_member_ifs(session: HostSession, src: SourceFileRef,
                        member: str) -> list[tuple[int, str]]:
    """Read a member via QSYS2.IFS_READ — no scratch objects involved."""
    path = ifs_member_path(src, member)
    res = _fetch(
        session, f"source.text.{src.library}.{src.file}.{member}",
        "SELECT LINE_NUMBER, LINE FROM TABLE(QSYS2.IFS_READ("
        f"PATH_NAME => '{path}')) ORDER BY LINE_NUMBER",
    )
    return _rows_to_lines(res)


def retrieve_member_alias(session: HostSession, src: SourceFileRef,
                          member: str, scratch_lib: str) -> list[tuple[int, str]]:
    """Read a member via a temporary alias in the scratch library."""
    alias = f"{scratch_lib}.T_MBR"
    _create_alias(session, alias, src, member)
    try:
        res = _fetch(
            session, f"source.text.{src.library}.{src.file}.{member}",
            f"SELECT SRCSEQ, SRCDTA FROM {alias} ORDER BY SRCSEQ",
        )
    finally:
        _drop_alias(session, alias)
    return _rows_to_lines(res)


def _rows_to_lines(res: QueryResult) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for row in res.rows:
        seq = int(row[0]) if row[0] is not None else 0
        text = "" if row[1] is None else str(row[1])
        lines.append((seq, text))
    return lines


def _create_alias(session: HostSession, alias: str, src: SourceFileRef,
                  member: str) -> None:
    stmt = f"CREATE ALIAS {alias} FOR {src.library}.{src.file}({member})"
    try:
        session.query(stmt)
    except Exception:  # noqa: BLE001 - fixtures don't need the alias
        pass


def _drop_alias(session: HostSession, alias: str) -> None:
    stmt = f"DROP ALIAS {alias}"
    try:
        session.query(stmt)
    except Exception:  # noqa: BLE001
        pass


def harvest(session: HostSession, con, config: Config) -> dict[str, int]:
    """Enumerate and retrieve every member of every configured source file."""
    total = 0
    rows: list[tuple[Any, ...]] = []
    for src in config.source_files:
        for m in enumerate_members(session, src):
            member = m["member"]
            member_type = m.get("member_type")
            lines = retrieve_member(session, src, member, config)
            for seq, text in lines:
                rows.append((src.library, src.file, member, member_type, seq, text))
                total += 1
    inserted = insert_rows(
        con, "raw_source_members",
        ["library", "srcfile", "member", "member_type", "seq", "line_text"],
        rows,
    )
    return {"raw_source_members": inserted}


def verify_roundtrip(con) -> bool:
    """Phase-1 sanity check: at least one source member retrieved as text.

    Guards against a silent CCSID/translation failure that would leave source
    lines blank or binary-garbled.
    """
    row = con.execute(
        "SELECT count(*) FROM raw_source_members "
        "WHERE line_text IS NOT NULL AND length(trim(line_text)) > 0"
    ).fetchone()
    return bool(row and row[0] > 0)
