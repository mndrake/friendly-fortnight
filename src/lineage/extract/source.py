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
from .connection import HostError, HostSession, QueryResult
from .hostinfo import HostProfile
from .progress import NULL, Progress


def _fetch(session: HostSession, tag: str, sql: str) -> QueryResult:
    if hasattr(session, "with_tag"):
        return session.with_tag(tag).query(sql)
    return session.query(sql)


def enumerate_members(session: HostSession, src: SourceFileRef,
                      profile: "HostProfile | None" = None) -> list[dict[str, Any]]:
    """List members of a source physical file with their source type.

    The member-name column is ``TABLE_PARTITION`` in the documented
    SYSPARTITIONSTAT shape; the host profile decides when an alternate name
    applies.
    """
    member_col, type_col = "TABLE_PARTITION", "SOURCE_TYPE"
    if profile is not None:
        available = profile.columns_of("SYSPARTITIONSTAT")
        if available:
            for cand in ("TABLE_PARTITION", "PARTITION_NAME"):
                if cand in available:
                    member_col = cand
                    break
            if "SOURCE_TYPE" not in available:
                type_col = "CAST(NULL AS VARCHAR(10))"
    res = _fetch(
        session, f"source.members.{src.library}.{src.file}",
        f"""
        SELECT {member_col} AS member, {type_col} AS member_type
        FROM QSYS2.SYSPARTITIONSTAT
        WHERE TABLE_SCHEMA = '{src.library}' AND TABLE_NAME = '{src.file}'
        """,
    )
    # An empty member name would build nonsense downstream (an alias like
    # FILE() is an SQL0104) — drop such rows rather than propagate them.
    return [m for m in res.dicts() if (m.get("member") or "").strip()]


def ifs_member_path(src: SourceFileRef, member: str) -> str:
    """QSYS.LIB filesystem path of a source member."""
    return (f"/QSYS.LIB/{src.library.upper()}.LIB/"
            f"{src.file.upper()}.FILE/{member.upper()}.MBR")


def resolve_strategy(config: Config,
                     profile: HostProfile | None = None) -> str:
    """Resolve the configured retrieval mode to a concrete strategy.

    ``auto`` uses IFS_READ when the host profile confirms QSYS2.IFS_READ
    exists, else the alias path; without a profile it optimistically tries
    IFS_READ (the per-member fallback still protects each read).
    """
    mode = config.source_retrieval
    if mode != "auto":
        return mode
    if profile is not None and not profile.has_ifs_read:
        return "alias"
    return "ifs_read"


def retrieve_member(session: HostSession, src: SourceFileRef, member: str,
                    config: Config,
                    profile: HostProfile | None = None
                    ) -> tuple[list[tuple[int, str]], str]:
    """Retrieve one member's lines as ``(seq, text)``.

    Returns ``(lines, strategy_used)``. In ``ifs_read`` mode an empty result
    or an SQL error triggers a per-member fallback to the alias strategy:
    IFS_READ reports a failed open as *zero rows plus a job-log warning*, not
    an SQL error — which happens for members of DDS/externally described data
    PFs (text-mode QSYS.LIB access only supports source PFs and single-field
    program-described PFs) and for SRCDTA CCSID 65535 (no conversion) — and
    raises outright on releases without the function. The alias path is plain
    record-level SQL and works for all of these.
    """
    strategy = resolve_strategy(config, profile)
    if strategy == "alias":
        return retrieve_member_alias(session, src, member,
                                     config.scratch_lib), "alias"
    try:
        lines = retrieve_member_ifs(session, src, member)
    except Exception:  # noqa: BLE001 - e.g. IFS_READ absent on this release
        lines = []
    if lines:
        return lines, "ifs_read"
    fallback = retrieve_member_alias(session, src, member, config.scratch_lib)
    if fallback:
        return fallback, "alias_fallback"
    return lines, "ifs_read"


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
    """Read a member via a temporary alias in the scratch library.

    The create is retried once (drop-then-recreate, defensive against a
    leftover alias in the same job) before giving up on it; the first
    create error is kept for diagnostics if the retry also fails. If the
    SELECT itself then fails, the two failures are combined into one rich
    ``HostError`` — the swallowed-CREATE-error bug that produced an
    inscrutable downstream SQL0204 on a live IBM i 7.5 run must not repeat.
    """
    alias = f"{scratch_lib}.T_MBR"
    create_error = _create_alias(session, alias, src, member)
    if create_error is not None:
        _drop_alias(session, alias)
        retry_error = _create_alias(session, alias, src, member)
        if retry_error is None:
            create_error = None
    try:
        res = _fetch(
            session, f"source.text.{src.library}.{src.file}.{member}",
            f"SELECT SRCSEQ, SRCDTA FROM {alias} ORDER BY SRCSEQ",
        )
    except Exception as exc:  # noqa: BLE001 - re-raised below with full context
        ref = f"{src.library}/{src.file}({member})"
        create_msg = (str(create_error) if create_error is not None
                      else "create reported success")
        raise HostError(
            f"failed to retrieve member {ref} via alias: SELECT failed "
            f"({exc}); CREATE ALIAS failed ({create_msg}); run "
            "SELECT * FROM TABLE(QSYS2.JOBLOG_INFO('*')) immediately after "
            "for host-side detail"
        ) from exc
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
                  member: str) -> Exception | None:
    """Issue the CREATE ALIAS; return the exception (or ``None``) instead of
    swallowing it, so callers can diagnose and/or retry."""
    stmt = f"CREATE ALIAS {alias} FOR {src.library}.{src.file}({member})"
    try:
        session.query(stmt)
    except Exception as exc:  # noqa: BLE001 - caller decides how to react
        return exc
    return None


def _drop_alias(session: HostSession, alias: str) -> None:
    stmt = f"DROP ALIAS {alias}"
    try:
        session.query(stmt)
    except Exception:  # noqa: BLE001
        pass


def harvest(session: HostSession, con, config: Config,
            profile: HostProfile | None = None,
            progress: Progress | None = None) -> dict[str, int]:
    """Enumerate and retrieve every member of every configured source file.

    One bad member (e.g. an alias that genuinely cannot be created/read on
    this release) must never crash a run over thousands of members — its
    retrieval failure is counted and the harvest continues.
    """
    p = progress or NULL
    rows: list[tuple[Any, ...]] = []
    fallbacks = 0
    retrieval_failures = 0
    for src in config.source_files:
        members = enumerate_members(session, src, profile)
        label = f"source {src.library}/{src.file}"
        p.start(label)
        file_fallbacks = file_failures = 0
        for i, m in enumerate(members, start=1):
            member = m["member"]
            member_type = m.get("member_type")
            try:
                lines, strategy = retrieve_member(session, src, member,
                                                  config, profile)
            except Exception:  # noqa: BLE001 - counted, not fatal
                retrieval_failures += 1
                file_failures += 1
                p.tick(f"members {src.library}/{src.file}", i, len(members))
                continue
            if strategy == "alias_fallback":
                fallbacks += 1
                file_fallbacks += 1
            for seq, text in lines:
                rows.append((src.library, src.file, member, member_type, seq, text))
            p.tick(f"members {src.library}/{src.file}", i, len(members))
        p.done(label, members=len(members), fallbacks=file_fallbacks,
               failures=file_failures)
    inserted = insert_rows(
        con, "raw_source_members",
        ["library", "srcfile", "member", "member_type", "seq", "line_text"],
        rows,
    )
    counts = {"raw_source_members": inserted}
    if fallbacks:
        # Members IFS_READ could not open in text mode (data-PF source files
        # or CCSID 65535); the alias strategy served them instead.
        counts["ifs_read_fallbacks"] = fallbacks
    if retrieval_failures:
        counts["member_retrieval_failures"] = retrieval_failures
    return counts


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
