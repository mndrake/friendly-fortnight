#!/usr/bin/env python3
"""Standalone host profiler — no repo dependencies, safe to copy anywhere.

Measures catalog/source volumes and likely extraction bottlenecks on a
DB2 for i system using READ-ONLY aggregate queries (no QCMDEXC, no outfiles,
no object creation). Prints a human summary, then a JSON blob between paste
markers to report the results back for analysis.

Two ways to run:

1. In a notebook that already has a jaydebeapi connection ``conn``:
   paste this file into a cell (or ``from profile_host_standalone import
   profile, render``), edit the CONFIG block, then::

       report = profile(conn)
       print(render(report))

2. From a shell (needs jaydebeapi + jt400.jar)::

       export LINEAGE_DB_HOST=MYLPAR LINEAGE_DB_USER=ME \
              LINEAGE_DB_PASSWORD=... LINEAGE_JT400_JAR=drivers/jt400.jar
       python scripts/profile_host_standalone.py

Delete this file freely — nothing in the repo imports it.
"""
import json
import os
import time

# ── CONFIG: edit for the target system ──────────────────────────────────────
LIBRARIES = ["APPLIB"]                        # libraries to be scanned
SOURCE_FILES = [                              # (library, source file)
    ("APPLIB", "QCLSRC"),
    ("APPLIB", "QRPGSRC"),
    ("APPLIB", "QDDSSRC"),
    ("APPLIB", "QDDLSRC"),
]
SEEDS = [                                     # (label, library, file/table)
    ("EXAMPLE_OUT", "APPLIB", "SOMETABLE"),
]
TOP_MEMBERS = 10
# ────────────────────────────────────────────────────────────────────────────


def _q(conn, sql):
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()


def _probe(conn, report, tag, sql):
    """Run one read-only query; record rows, timing, and any error."""
    t0 = time.perf_counter()
    try:
        rows = _q(conn, sql)
        return rows
    except Exception as exc:  # noqa: BLE001 - report, never abort
        report["errors"][tag] = str(exc)[:300]
        return []
    finally:
        report["timings_s"][tag] = round(time.perf_counter() - t0, 3)


def profile(conn):
    r = {
        "version": {}, "library_objects": {}, "catalog_rows": {},
        "source_volumes": {}, "seeds": {}, "ifs_read": None,
        "timings_s": {}, "errors": {},
    }

    rows = _probe(conn, r, "env",
                  "SELECT OS_VERSION, OS_RELEASE FROM SYSIBMADM.ENV_SYS_INFO")
    if rows:
        r["version"] = {"os_version": str(rows[0][0]).strip(),
                        "os_release": str(rows[0][1]).strip()}

    for lib in LIBRARIES:
        by_type = {}
        for otype in ("*PGM", "*FILE"):
            tag = f"objects.{lib}.{otype[1:].lower()}"
            rows = _probe(conn, r, tag,
                          "SELECT OBJATTRIBUTE, COUNT(*) FROM TABLE("
                          f"QSYS2.OBJECT_STATISTICS('{lib}', '{otype}')) "
                          "GROUP BY OBJATTRIBUTE")
            by_type[otype] = {str(a or "").strip() or "?": int(n)
                              for a, n in rows}
        r["library_objects"][lib] = by_type

    libs_in = ", ".join(f"'{lib}'" for lib in LIBRARIES)
    for view, schema_col in (("SYSTABLES", "TABLE_SCHEMA"),
                             ("SYSCOLUMNS", "TABLE_SCHEMA"),
                             ("SYSVIEWS", "TABLE_SCHEMA"),
                             ("SYSVIEWDEP", "VIEW_SCHEMA"),
                             ("SYSPARTITIONSTAT", "TABLE_SCHEMA")):
        tag = f"catalog.{view.lower()}"
        rows = _probe(conn, r, tag,
                      f"SELECT COUNT(*) FROM QSYS2.{view} "
                      f"WHERE {schema_col} IN ({libs_in})")
        if rows:
            r["catalog_rows"][view] = int(rows[0][0])

    for lib, sf in SOURCE_FILES:
        tag = f"source.{lib}.{sf}"
        rows = _probe(conn, r, tag,
                      "SELECT COUNT(*), SUM(NUMBER_ROWS), SUM(DATA_SIZE) "
                      "FROM QSYS2.SYSPARTITIONSTAT "
                      f"WHERE TABLE_SCHEMA = '{lib}' AND TABLE_NAME = '{sf}'")
        vol = {"members": 0, "total_lines": 0, "total_bytes": 0,
               "top_members": []}
        if rows and rows[0][0]:
            vol["members"] = int(rows[0][0] or 0)
            vol["total_lines"] = int(rows[0][1] or 0)
            vol["total_bytes"] = int(rows[0][2] or 0)
        top = _probe(conn, r, tag + ".top",
                     "SELECT TABLE_PARTITION, NUMBER_ROWS "
                     "FROM QSYS2.SYSPARTITIONSTAT "
                     f"WHERE TABLE_SCHEMA = '{lib}' AND TABLE_NAME = '{sf}' "
                     f"ORDER BY NUMBER_ROWS DESC "
                     f"FETCH FIRST {TOP_MEMBERS} ROWS ONLY")
        vol["top_members"] = [{"member": str(m).strip(), "lines": int(n or 0)}
                              for m, n in top]
        r["source_volumes"][f"{lib}/{sf}"] = vol

    for label, lib, name in SEEDS:
        tag = f"seed.{label}"
        rows = _probe(conn, r, tag,
                      "SELECT OBJATTRIBUTE, SQL_OBJECT_TYPE FROM TABLE("
                      f"QSYS2.OBJECT_STATISTICS('{lib}', '*FILE', '{name}'))")
        if not rows:
            cls = "missing"
            attr = sqltype = ""
        else:
            attr = str(rows[0][0] or "").strip().upper()
            sqltype = str(rows[0][1] or "").strip().upper()
            if sqltype == "TABLE":
                cls = "ddl_table"
            elif sqltype == "VIEW":
                cls = "view"
            elif attr == "PF":
                cls = "dds_pf"
            elif attr == "LF":
                cls = "lf"
            else:
                cls = "unknown"
        r["seeds"][label] = {"library": lib, "file": name,
                             "objattribute": attr, "sql_object_type": sqltype,
                             "classification": cls}

    rows = _probe(conn, r, "ifs_read",
                  "SELECT COUNT(*) FROM QSYS2.SYSROUTINES WHERE "
                  "ROUTINE_SCHEMA = 'QSYS2' AND ROUTINE_NAME = 'IFS_READ'")
    r["ifs_read"] = bool(rows and rows[0][0])
    return r


def render(report):
    lines = ["", "== host profile summary =="]
    v = report["version"]
    if v:
        lines.append(f"IBM i {v.get('os_version')}.{v.get('os_release')}  "
                     f"(IFS_READ={'yes' if report['ifs_read'] else 'no'})")
    for lib, by_type in report["library_objects"].items():
        for otype, counts in by_type.items():
            detail = ", ".join(f"{k}={n}" for k, n in sorted(counts.items()))
            lines.append(f"{lib} {otype}: {sum(counts.values())} ({detail})")
    for view, n in report["catalog_rows"].items():
        lines.append(f"QSYS2.{view} rows in scope: {n}")
    for key, vol in report["source_volumes"].items():
        lines.append(f"{key}: {vol['members']} members, "
                     f"{vol['total_lines']} lines, {vol['total_bytes']} bytes")
    for label, s in report["seeds"].items():
        lines.append(f"seed {label} ({s['library']}/{s['file']}): "
                     f"{s['classification']}")
    slow = sorted(report["timings_s"].items(), key=lambda kv: -kv[1])[:5]
    lines.append("slowest probes: " +
                 ", ".join(f"{t}={s}s" for t, s in slow))
    if report["errors"]:
        lines.append(f"probe errors: {len(report['errors'])} "
                     f"({', '.join(sorted(report['errors']))})")
    lines += ["", "=== COPY EVERYTHING BELOW AND PASTE IT BACK ===",
              json.dumps(report, sort_keys=True),
              "=== END ==="]
    return "\n".join(lines)


def _connect_from_env():
    import jaydebeapi
    host = os.environ["LINEAGE_DB_HOST"]
    return jaydebeapi.connect(
        "com.ibm.as400.access.AS400JDBCDriver",
        f"jdbc:as400://{host}",
        {"user": os.environ["LINEAGE_DB_USER"],
         "password": os.environ["LINEAGE_DB_PASSWORD"],
         "prompt": "false"},
        os.environ.get("LINEAGE_JT400_JAR", "drivers/jt400.jar"),
    )


if __name__ == "__main__":
    connection = _connect_from_env()
    try:
        print(render(profile(connection)))
    finally:
        connection.close()
