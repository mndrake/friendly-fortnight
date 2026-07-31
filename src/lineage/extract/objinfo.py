"""Per-object source location lookup via ``QSYS2.OBJECT_STATISTICS``.

Every compiled IBM i object records where its source actually came from —
``SOURCE_LIBRARY``/``SOURCE_FILE``/``SOURCE_MEMBER`` — independent of any
naming convention. Targeted extraction (:mod:`lineage.extract.targeted`) asks
each slice program/file where its source lives instead of assuming the
member name matches the object name; this is authoritative, immune to
member-name != object-name mismatches, and keeps ``missing_source`` gaps
accurate.
"""
from __future__ import annotations

from typing import Optional

from .connection import HostSession

# QSYS2.OBJECT_STATISTICS object-type argument -> fixture-tag suffix.
_TAG_SUFFIX = {"*PGM": "pgm", "*FILE": "file"}


def _fetch(session: HostSession, tag: str, sql: str):
    if hasattr(session, "with_tag"):
        return session.with_tag(tag).query(sql)
    return session.query(sql)


def source_locations_bulk(session: HostSession, library: str, obj_type: str
                          ) -> Optional[dict[str, Optional[tuple[str, str, str]]]]:
    """Source locations for *every* object of ``obj_type`` in ``library``.

    One library-wide ``OBJECT_STATISTICS`` scan instead of one call per
    object: on a real estate a slice can hold a thousand-plus programs in a
    handful of libraries, and per-object probing pays a full JDBC round trip
    each — the scan answers them all at once. Returns
    ``{OBJNAME: (source_library, source_file, source_member) | None}`` —
    ``None`` values mean the object exists but has no usable recorded
    source. A name *absent* from the dict does not exist in the library at
    all (the scan is authoritative), so callers treat both the same as a
    per-object miss. Returns ``None`` (not ``{}``) when the scan itself
    fails, so callers can fall back to per-object probing.
    """
    library = library.upper()
    suffix = _TAG_SUFFIX.get(obj_type, obj_type.strip("*").lower())
    tag = f"objstat.{library}.{suffix}.bulk"
    sql = (
        "SELECT OBJNAME, SOURCE_LIBRARY, SOURCE_FILE, SOURCE_MEMBER "
        f"FROM TABLE(QSYS2.OBJECT_STATISTICS('{library}', '{obj_type}'))"
    )
    try:
        res = _fetch(session, tag, sql)
    except Exception:  # noqa: BLE001 - caller falls back to per-object probes
        return None
    out: dict[str, Optional[tuple[str, str, str]]] = {}
    for row in res.dicts():
        name = str(row.get("objname") or "").strip().upper()
        if not name:
            continue
        srclib = str(row.get("source_library") or "").strip()
        srcfile = str(row.get("source_file") or "").strip()
        srcmbr = str(row.get("source_member") or "").strip()
        out[name] = ((srclib, srcfile, srcmbr)
                     if srclib and srcfile and srcmbr else None)
    return out


def source_location(session: HostSession, library: str, name: str,
                    obj_type: str) -> Optional[tuple[str, str, str]]:
    """Where ``library/name``'s source actually lives.

    ``obj_type`` is ``*PGM`` or ``*FILE``. Returns
    ``(source_library, source_file, source_member)``, or ``None`` when the
    object has no recorded source (deleted source, a data-described file
    with no source-origin metadata, ...), any of the three values comes back
    blank, or the probe fails outright (older release, missing authority,
    fixture gap in tests) — callers fall back to name-matching in that case.
    """
    library = library.upper()
    name = name.upper()
    suffix = _TAG_SUFFIX.get(obj_type, obj_type.strip("*").lower())
    tag = f"objstat.{library}.{name}.{suffix}"
    sql = (
        "SELECT SOURCE_LIBRARY, SOURCE_FILE, SOURCE_MEMBER FROM TABLE("
        f"QSYS2.OBJECT_STATISTICS('{library}', '{obj_type}', '{name}'))"
    )
    try:
        res = _fetch(session, tag, sql)
    except Exception:  # noqa: BLE001 - degrade to name-matching on any failure
        return None
    if not res.rows:
        return None
    srclib, srcfile, srcmbr = res.rows[0][0], res.rows[0][1], res.rows[0][2]
    srclib = str(srclib).strip() if srclib is not None else ""
    srcfile = str(srcfile).strip() if srcfile is not None else ""
    srcmbr = str(srcmbr).strip() if srcmbr is not None else ""
    if not srclib or not srcfile or not srcmbr:
        return None
    return srclib, srcfile, srcmbr
