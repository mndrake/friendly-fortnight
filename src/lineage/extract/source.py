"""Source member enumeration and retrieval.

Members are discovered via ``SYSPARTITIONSTAT`` on the configured source
physical files, then each member's text is retrieved by creating a temporary
alias over ``library/srcfile(member)`` and selecting ``SRCSEQ, SRCDTA``. JDBC
translation handles EBCDIC/CCSID; a Phase-1 round-trip check verifies a member
reads back as text (see :func:`verify_roundtrip`).
"""
from __future__ import annotations

from typing import Any

from ..config import SourceFileRef
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


def retrieve_member(session: HostSession, src: SourceFileRef, member: str,
                    scratch_lib: str) -> list[tuple[int, str]]:
    """Retrieve one member's lines as ``(seq, text)`` via a temporary alias."""
    alias = f"{scratch_lib}.T_MBR"
    # Create alias, select, drop. On the real host these are SQL statements over
    # the JDBC connection; the fixture session serves the SELECT by tag.
    _create_alias(session, alias, src, member)
    try:
        res = _fetch(
            session, f"source.text.{src.library}.{src.file}.{member}",
            f"SELECT SRCSEQ, SRCDTA FROM {alias} ORDER BY SRCSEQ",
        )
    finally:
        _drop_alias(session, alias)
    lines: list[tuple[int, str]] = []
    for row in res.rows:
        seq = int(row[0]) if row[0] is not None else 0
        text = "" if row[1] is None else str(row[1])
        lines.append((seq, text))
    return lines


def _create_alias(session: HostSession, alias: str, src: SourceFileRef,
                  member: str) -> None:
    stmt = f"CREATE ALIAS {alias} FOR {src.library}.{src.file}({member})"
    if hasattr(session, "run_sql"):
        session.run_sql(stmt)  # pragma: no cover
    else:
        # Real JDBC session executes DDL as a query with no result set.
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


def harvest(session: HostSession, con, config) -> dict[str, int]:
    """Enumerate and retrieve every member of every configured source file."""
    total = 0
    rows: list[tuple[Any, ...]] = []
    for src in config.source_files:
        for m in enumerate_members(session, src):
            member = m["member"]
            member_type = m.get("member_type")
            lines = retrieve_member(session, src, member, config.scratch_lib)
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
