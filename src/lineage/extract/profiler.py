"""Read-only host profiling: catalog/source volumes before committing to a
full extraction.

Every probe here is a `COUNT(*)`/`SUM(...)`/`GROUP BY` aggregate against
``QSYS2.OBJECT_STATISTICS`` or a catalog view — no ``run_cl``, no DDL, no
outfiles. The report answers three questions cheaply, ahead of the real
``extract`` run: how big is the object/catalog estate for the configured
libraries, how much source text would a full member pull download, and do
the configured output seeds actually look like the DDL tables the pipeline
assumes they are.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..config import Config
from .connection import HostSession

# Object types profiled per library via QSYS2.OBJECT_STATISTICS.
_OBJECT_TYPES = ("*PGM", "*FILE")

# Catalog views counted for the configured libraries; SYSVIEWDEP is keyed by
# VIEW_SCHEMA, the rest by TABLE_SCHEMA (see _schema_col).
_CATALOG_VIEWS = ("SYSTABLES", "SYSCOLUMNS", "SYSVIEWS", "SYSVIEWDEP",
                  "SYSPARTITIONSTAT")


@dataclass
class ProfileReport:
    # lib -> {"*PGM": {objattribute: count}, "*FILE": {objattribute: count}}
    library_objects: dict[str, dict[str, dict[str, int]]] = field(
        default_factory=dict)
    # catalog view name -> row count for the configured libraries
    catalog_rows: dict[str, int] = field(default_factory=dict)
    # "LIB/SRCFILE" -> {members, total_lines, total_bytes, top_members}
    source_volumes: dict[str, dict] = field(default_factory=dict)
    # seed id -> {library, file, objattribute, sql_object_type, classification}
    seed_classes: dict[str, dict] = field(default_factory=dict)
    # fixture tag -> elapsed seconds
    timings: dict[str, float] = field(default_factory=dict)
    # fixture tag -> error string, for probes that failed
    errors: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({
            "library_objects": self.library_objects,
            "catalog_rows": self.catalog_rows,
            "source_volumes": self.source_volumes,
            "seed_classes": self.seed_classes,
            "timings": self.timings,
            "errors": self.errors,
        }, indent=2, sort_keys=True)


def _fetch(session: HostSession, tag: str, sql: str):
    if hasattr(session, "with_tag"):
        return session.with_tag(tag).query(sql)
    return session.query(sql)


def _in_list(libs) -> str:
    return ", ".join("'" + lib.replace("'", "''") + "'" for lib in libs)


def _schema_col(view: str) -> str:
    return "VIEW_SCHEMA" if view == "SYSVIEWDEP" else "TABLE_SCHEMA"


def _classify_seed(objattribute: str, sql_object_type: str) -> str:
    """DDL-table-or-not classification for an output seed.

    The pipeline's premise is that output seeds are plain DDL tables;
    anything else (DDS PF/LF, SQL view, or missing) is flagged for review
    rather than assumed away.
    """
    if sql_object_type == "TABLE":
        return "ddl_table"
    if sql_object_type == "VIEW":
        return "view"
    if objattribute == "PF":
        return "dds_pf"
    if objattribute == "LF":
        return "lf"
    return "unknown"


def _run(report: ProfileReport, session: HostSession, tag: str, sql: str):
    """Run one probe, recording timing always and errors on failure.

    Returns the QueryResult on success, None on failure — callers branch on
    that rather than propagating, so one bad probe never aborts the rest.
    """
    start = time.perf_counter()
    try:
        return _fetch(session, tag, sql)
    except Exception as exc:  # noqa: BLE001 - graceful degradation by design
        report.errors[tag] = str(exc)
        return None
    finally:
        report.timings[tag] = time.perf_counter() - start


def profile_host(session: HostSession, config: Config) -> ProfileReport:
    """Measure catalog/source volumes on the target host. Read-only, and
    every probe degrades gracefully — a failure lands in ``errors`` rather
    than aborting the rest of the profile."""
    report = ProfileReport()

    # 1. Per-library object counts by type/attribute.
    for lib in config.libraries:
        for otype in _OBJECT_TYPES:
            suffix = otype.lstrip("*").lower()
            tag = f"profile.objects.{lib}.{suffix}"
            sql = (f"SELECT OBJATTRIBUTE, COUNT(*) FROM TABLE("
                   f"QSYS2.OBJECT_STATISTICS('{lib}', '{otype}')) "
                   f"GROUP BY OBJATTRIBUTE")
            res = _run(report, session, tag, sql)
            if res is not None:
                counts = {str(attr).strip(): int(cnt)
                          for attr, cnt in res.rows}
                report.library_objects.setdefault(lib, {})[otype] = counts

    # 2. Catalog row counts for the configured libraries.
    libs_in = _in_list(config.libraries)
    for view in _CATALOG_VIEWS:
        tag = f"profile.catalog.{view.lower()}"
        sql = (f"SELECT COUNT(*) FROM QSYS2.{view} "
               f"WHERE {_schema_col(view)} IN ({libs_in})")
        res = _run(report, session, tag, sql)
        if res is not None and res.rows:
            report.catalog_rows[view] = int(res.rows[0][0] or 0)

    # 3. Source file volumes and their heaviest members.
    for sf in config.source_files:
        key = f"{sf.library}/{sf.file}"
        tag = f"profile.source.{sf.library}.{sf.file}"
        sql = (f"SELECT COUNT(*), SUM(NUMBER_ROWS), SUM(DATA_SIZE) FROM "
               f"QSYS2.SYSPARTITIONSTAT WHERE TABLE_SCHEMA = '{sf.library}' "
               f"AND TABLE_NAME = '{sf.file}'")
        res = _run(report, session, tag, sql)
        if res is not None and res.rows:
            cnt, lines, size = res.rows[0]
            report.source_volumes[key] = {
                "members": int(cnt or 0),
                "total_lines": int(lines or 0),
                "total_bytes": int(size or 0),
                "top_members": [],
            }

        top_tag = f"{tag}.top"
        top_sql = (f"SELECT TABLE_PARTITION, NUMBER_ROWS FROM "
                   f"QSYS2.SYSPARTITIONSTAT WHERE TABLE_SCHEMA = "
                   f"'{sf.library}' AND TABLE_NAME = '{sf.file}' "
                   f"ORDER BY NUMBER_ROWS DESC FETCH FIRST 10 ROWS ONLY")
        res = _run(report, session, top_tag, top_sql)
        if res is not None:
            top = [{"member": str(member).strip(), "lines": int(n or 0)}
                   for member, n in res.rows]
            report.source_volumes.setdefault(key, {
                "members": 0, "total_lines": 0, "total_bytes": 0,
                "top_members": [],
            })["top_members"] = top

    # 4. Output seed classification — do they still look like DDL tables?
    for seed in config.output_seeds:
        tag = f"profile.seed.{seed.id}"
        sql = (f"SELECT OBJATTRIBUTE, SQL_OBJECT_TYPE FROM TABLE("
               f"QSYS2.OBJECT_STATISTICS('{seed.library}', '*FILE', "
               f"'{seed.file}'))")
        res = _run(report, session, tag, sql)
        if res is not None:
            if not res.rows:
                objattr, sql_type, classification = "", "", "missing"
            else:
                objattr = str(res.rows[0][0] or "").strip().upper()
                sql_type = str(res.rows[0][1] or "").strip().upper()
                classification = _classify_seed(objattr, sql_type)
            report.seed_classes[seed.id] = {
                "library": seed.library,
                "file": seed.file,
                "objattribute": objattr,
                "sql_object_type": sql_type,
                "classification": classification,
            }

    return report


def recommend(report: ProfileReport, config: Config) -> list[str]:
    """Heuristic, human-readable recommendations from a completed profile.

    Never raises and never touches the host; pure post-processing of the
    measured numbers.
    """
    lines: list[str] = []

    syscolumns_rows = report.catalog_rows.get("SYSCOLUMNS", 0)
    if syscolumns_rows > 50_000:
        lines.append(
            f"SYSCOLUMNS has {syscolumns_rows} rows across the configured "
            "libraries — scope SYSCOLUMNS/DSPFFD pulls to the lineage "
            "slice rather than the full library (targeted extraction).")

    total_lines = sum(v.get("total_lines", 0)
                      for v in report.source_volumes.values())
    total_members = sum(v.get("members", 0)
                        for v in report.source_volumes.values())
    if total_lines > 1_000_000 or total_members > 5_000:
        lines.append(
            f"Configured source files hold {total_members} members / "
            f"{total_lines} lines — targeted member retrieval (only the "
            "members lineage actually needs) is the largest download win.")

    for seed_id, info in report.seed_classes.items():
        classification = info.get("classification")
        if classification == "missing":
            lines.append(
                f"seed '{seed_id}' ({info.get('library')}/"
                f"{info.get('file')}) was not found on the host.")
        elif classification != "ddl_table":
            lines.append(
                f"seed '{seed_id}' classifies as '{classification}', not a "
                "plain DDL table — the DDL-only premise doesn't hold for "
                "it; review before scoping extraction around it.")

    for tag, secs in sorted(report.timings.items()):
        if secs > 5.0:
            note = ""
            if "syspartitionstat" in tag or tag.startswith("profile.source."):
                note = (" — per-member statistics are expensive on large "
                        "estates")
            lines.append(f"probe '{tag}' took {secs:.1f}s{note}.")

    for lib, by_type in report.library_objects.items():
        total_pgm = sum(by_type.get("*PGM", {}).values())
        if total_pgm > 2_000:
            lines.append(
                f"{lib} has {total_pgm} *PGM objects — the DSPPGMREF "
                "outfile pull will dominate extraction time; keep it "
                "per-library (one command per library) and consider "
                "running off-peak.")

    if not lines:
        lines.append(
            "Volumes look modest across the board — full-library "
            "extraction is fine at these volumes.")

    return lines
