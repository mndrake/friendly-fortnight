"""QSYS2 catalog pulls: tables, columns, views, view dependencies, partitions.

All reads are plain SELECTs against the QSYS2 catalog views, filtered to the
configured libraries. Results land verbatim in the raw store.
"""
from __future__ import annotations

from typing import Any, Sequence

from ..db import insert_rows
from .connection import HostSession, QueryResult


def _in_list(libs: Sequence[str]) -> str:
    return ", ".join("'" + lib.replace("'", "''") + "'" for lib in libs)


def _fetch(session: HostSession, tag: str, sql: str) -> QueryResult:
    if hasattr(session, "with_tag"):
        return session.with_tag(tag).query(sql)
    return session.query(sql)


def harvest(session: HostSession, con, config) -> dict[str, int]:
    libs = _in_list(config.libraries)
    counts: dict[str, int] = {}

    systables = _fetch(session, "catalog.systables", f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, SYSTEM_TABLE_NAME, TABLE_TYPE,
               FILE_TYPE, CARD, LONG_COMMENT
        FROM QSYS2.SYSTABLES
        WHERE TABLE_SCHEMA IN ({libs})
    """)
    counts["raw_systables"] = insert_rows(
        con, "raw_systables",
        ["table_schema", "table_name", "system_name", "table_type",
         "file_type", "row_count", "long_comment"],
        [tuple(r) for r in systables.rows],
    )

    syscolumns = _fetch(session, "catalog.syscolumns", f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, SYSTEM_TABLE_NAME, COLUMN_NAME,
               SYSTEM_COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, LENGTH,
               NUMERIC_SCALE, IS_NULLABLE, COLUMN_HEADING
        FROM QSYS2.SYSCOLUMNS
        WHERE TABLE_SCHEMA IN ({libs})
    """)
    counts["raw_syscolumns"] = insert_rows(
        con, "raw_syscolumns",
        ["table_schema", "table_name", "system_name", "column_name",
         "system_column", "ordinal", "data_type", "length", "numeric_scale",
         "is_nullable", "column_heading"],
        [tuple(r) for r in syscolumns.rows],
    )

    sysviews = _fetch(session, "catalog.sysviews", f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, SYSTEM_VIEW_NAME, VIEW_DEFINITION
        FROM QSYS2.SYSVIEWS
        WHERE TABLE_SCHEMA IN ({libs})
    """)
    counts["raw_sysviews"] = insert_rows(
        con, "raw_sysviews",
        ["table_schema", "table_name", "system_name", "view_definition"],
        [tuple(r) for r in sysviews.rows],
    )

    sysviewdep = _fetch(session, "catalog.sysviewdep", f"""
        SELECT VIEW_SCHEMA, VIEW_NAME, TABLE_SCHEMA, TABLE_NAME, OBJECT_TYPE
        FROM QSYS2.SYSVIEWDEP
        WHERE VIEW_SCHEMA IN ({libs})
    """)
    counts["raw_sysviewdep"] = insert_rows(
        con, "raw_sysviewdep",
        ["view_schema", "view_name", "object_schema", "object_name",
         "object_type"],
        [tuple(r) for r in sysviewdep.rows],
    )

    partstat = _fetch(session, "catalog.syspartitionstat", f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, SYSTEM_TABLE_NAME, PARTITION_NAME,
               NUMBER_ROWS, SOURCE_TYPE
        FROM QSYS2.SYSPARTITIONSTAT
        WHERE TABLE_SCHEMA IN ({libs})
    """)
    counts["raw_syspartitionstat"] = insert_rows(
        con, "raw_syspartitionstat",
        ["table_schema", "table_name", "system_name", "partition_name",
         "number_rows", "source_type"],
        [tuple(r) for r in partstat.rows],
    )

    return counts
