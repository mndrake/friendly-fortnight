"""QSYS2 catalog pulls: tables, columns, views, view dependencies, partitions.

SELECT lists are built **adaptively** against the host's actual catalog shape
(from :mod:`lineage.extract.hostinfo`): every column we want is declared as an
ordered list of candidate names (TRs and releases rename/add columns), plus a
required flag. Missing optional columns are NULL-filled; a missing required
column fails loudly with the host's version in the message. When no profile is
available (old stores, some tests), the first candidate of each column is
used verbatim — the pre-probe behavior.

Results land verbatim in the raw store.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from ..db import insert_rows
from .connection import HostSession, QueryResult
from .hostinfo import HostProfile
from .progress import NULL, Progress


@dataclass(frozen=True)
class ColSpec:
    raw: str                       # raw-table column the value lands in
    candidates: tuple[str, ...]    # catalog column names, preference order
    required: bool = False

    def pick(self, available: set[str]) -> Optional[str]:
        if not available:
            return self.candidates[0]
        for cand in self.candidates:
            if cand in available:
                return cand
        return None


@dataclass(frozen=True)
class PullSpec:
    name: str
    raw_table: str
    catalog_view: str              # unqualified view name under QSYS2
    schema_filter: str             # column filtering by library
    cols: tuple[ColSpec, ...]

    def build_select(self, available: set[str], libs_in: Optional[str],
                     extra_where: Optional[str] = None
                     ) -> tuple[str, list[str]]:
        """Return (sql, missing_optional_raw_columns).

        Raises ``CatalogShapeError`` when a required column has no available
        candidate. ``extra_where``, when given, is AND-ed onto the WHERE
        clause (used to scope a pull to a chunked list of (schema, name)
        pairs — see :func:`pairs_filter`). ``libs_in`` of ``None`` omits the
        ``schema_filter IN (...)`` conjunct entirely — used for pairs pulls,
        where the (schema, name) pairs already pin the scope exactly, so
        libraries outside the configured scan list can still be reached.
        """
        parts: list[str] = []
        missing: list[str] = []
        for col in self.cols:
            picked = col.pick(available)
            if picked is None:
                if col.required:
                    raise CatalogShapeError(self, col, available)
                missing.append(col.raw)
                parts.append(f"CAST(NULL AS VARCHAR(1)) AS {col.raw}")
            else:
                parts.append(f"{picked} AS {col.raw}")
        where_clauses: list[str] = []
        if libs_in is not None:
            where_clauses.append(f"{self.schema_filter} IN ({libs_in})")
        if extra_where:
            where_clauses.append(f"({extra_where})")
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        sql = (f"SELECT {', '.join(parts)} FROM QSYS2.{self.catalog_view} "
               f"WHERE {where_sql}")
        return sql, missing


class CatalogShapeError(RuntimeError):
    def __init__(self, spec: PullSpec, col: ColSpec, available: set[str]):
        super().__init__(
            f"QSYS2.{spec.catalog_view} on this host has none of the "
            f"candidate columns {col.candidates} needed for '{col.raw}'. "
            f"Available columns: {sorted(available)}. Update the candidate "
            f"list in extract/catalog.py for this release.")
        self.spec = spec
        self.col = col


# Candidate lists reflect the documented QSYS2 catalog shapes; first entry is
# the current name. Optional columns NULL-fill on hosts that lack them.
PULLS: tuple[PullSpec, ...] = (
    PullSpec(
        name="systables", raw_table="raw_systables",
        catalog_view="SYSTABLES", schema_filter="TABLE_SCHEMA",
        cols=(
            ColSpec("table_schema", ("TABLE_SCHEMA",), required=True),
            ColSpec("table_name", ("TABLE_NAME",), required=True),
            ColSpec("system_name", ("SYSTEM_TABLE_NAME",)),
            ColSpec("table_type", ("TABLE_TYPE",), required=True),
            ColSpec("file_type", ("FILE_TYPE",)),
            # SYSTABLES has no row count; SYSPARTITIONSTAT carries it.
            ColSpec("row_count", ("NUMBER_ROWS", "CARD")),
            ColSpec("long_comment", ("LONG_COMMENT",)),
        ),
    ),
    PullSpec(
        name="syscolumns", raw_table="raw_syscolumns",
        catalog_view="SYSCOLUMNS", schema_filter="TABLE_SCHEMA",
        cols=(
            ColSpec("table_schema", ("TABLE_SCHEMA",), required=True),
            ColSpec("table_name", ("TABLE_NAME",), required=True),
            ColSpec("system_name", ("SYSTEM_TABLE_NAME",)),
            ColSpec("column_name", ("COLUMN_NAME",), required=True),
            ColSpec("system_column", ("SYSTEM_COLUMN_NAME",)),
            ColSpec("ordinal", ("ORDINAL_POSITION",), required=True),
            ColSpec("data_type", ("DATA_TYPE",)),
            ColSpec("length", ("LENGTH",)),
            ColSpec("numeric_scale", ("NUMERIC_SCALE",)),
            ColSpec("is_nullable", ("IS_NULLABLE",)),
            ColSpec("column_heading", ("COLUMN_HEADING",)),
        ),
    ),
    PullSpec(
        name="sysviews", raw_table="raw_sysviews",
        catalog_view="SYSVIEWS", schema_filter="TABLE_SCHEMA",
        cols=(
            ColSpec("table_schema", ("TABLE_SCHEMA",), required=True),
            ColSpec("table_name", ("TABLE_NAME",), required=True),
            ColSpec("system_name", ("SYSTEM_VIEW_NAME", "SYSTEM_TABLE_NAME")),
            ColSpec("view_definition", ("VIEW_DEFINITION",), required=True),
        ),
    ),
    PullSpec(
        name="sysviewdep", raw_table="raw_sysviewdep",
        catalog_view="SYSVIEWDEP", schema_filter="VIEW_SCHEMA",
        cols=(
            ColSpec("view_schema", ("VIEW_SCHEMA",), required=True),
            ColSpec("view_name", ("VIEW_NAME",), required=True),
            # Documented names are OBJECT_SCHEMA/OBJECT_NAME.
            ColSpec("object_schema", ("OBJECT_SCHEMA", "TABLE_SCHEMA"),
                    required=True),
            ColSpec("object_name", ("OBJECT_NAME", "TABLE_NAME"),
                    required=True),
            ColSpec("object_type", ("OBJECT_TYPE",)),
        ),
    ),
    PullSpec(
        name="syspartitionstat", raw_table="raw_syspartitionstat",
        catalog_view="SYSPARTITIONSTAT", schema_filter="TABLE_SCHEMA",
        cols=(
            ColSpec("table_schema", ("TABLE_SCHEMA",), required=True),
            ColSpec("table_name", ("TABLE_NAME",), required=True),
            ColSpec("system_name", ("SYSTEM_TABLE_NAME",)),
            # Documented member-name column is TABLE_PARTITION.
            ColSpec("partition_name", ("TABLE_PARTITION", "PARTITION_NAME"),
                    required=True),
            ColSpec("number_rows", ("NUMBER_ROWS",)),
            ColSpec("source_type", ("SOURCE_TYPE",)),
        ),
    ),
)

RAW_COLUMNS: dict[str, list[str]] = {
    "raw_systables": ["table_schema", "table_name", "system_name",
                      "table_type", "file_type", "row_count", "long_comment"],
    "raw_syscolumns": ["table_schema", "table_name", "system_name",
                       "column_name", "system_column", "ordinal", "data_type",
                       "length", "numeric_scale", "is_nullable",
                       "column_heading"],
    "raw_sysviews": ["table_schema", "table_name", "system_name",
                     "view_definition"],
    "raw_sysviewdep": ["view_schema", "view_name", "object_schema",
                       "object_name", "object_type"],
    "raw_syspartitionstat": ["table_schema", "table_name", "system_name",
                             "partition_name", "number_rows", "source_type"],
}


def _in_list(libs: Sequence[str]) -> str:
    return ", ".join("'" + lib.replace("'", "''") + "'" for lib in libs)


def _quote(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def pairs_filter(schema_col: str, name_col: str,
                 pairs: Sequence[tuple[str, str]], chunk: int = 500
                 ) -> list[str]:
    """Chunk ``(schema, name)`` pairs into ``OR``-of-``AND`` WHERE fragments.

    Each fragment looks like::

        ((SCHEMA_COL = 'LIB1' AND NAME_COL = 'NAME1') OR
         (SCHEMA_COL = 'LIB2' AND NAME_COL = 'NAME2') OR ...)

    Returns one fragment per ``chunk``-sized slice of ``pairs`` (so a caller
    issues one SELECT per fragment and concatenates the rows) — keeps any
    single ``IN``/``OR`` list from growing unbounded against large slices.
    """
    fragments: list[str] = []
    pairs = list(pairs)
    for i in range(0, len(pairs), chunk):
        batch = pairs[i:i + chunk]
        ors = " OR ".join(
            f"({schema_col} = {_quote(lib)} AND {name_col} = {_quote(name)})"
            for lib, name in batch)
        fragments.append(f"({ors})")
    return fragments


def _fetch(session: HostSession, tag: str, sql: str) -> QueryResult:
    if hasattr(session, "with_tag"):
        return session.with_tag(tag).query(sql)
    return session.query(sql)


def harvest(session: HostSession, con, config,
            profile: HostProfile | None = None,
            only: dict[str, Sequence[tuple[str, str]]] | None = None,
            progress: Progress | None = None
            ) -> dict[str, int]:
    """Pull the QSYS2 catalog views into the raw store.

    ``only``, when given, maps a ``catalog_view`` name (e.g. ``"SYSCOLUMNS"``)
    to a list of ``(library, name)`` pairs that pull is scoped to — used by
    targeted extraction to avoid a full-library SYSCOLUMNS/SYSPARTITIONSTAT
    scan. A view named in ``only`` with an *empty* list is skipped entirely
    (0 rows) rather than issuing an unfiltered pull; views not named in
    ``only`` behave exactly as a full-mode pull (today's behavior).
    """
    p = progress or NULL
    libs = _in_list(config.libraries)
    counts: dict[str, int] = {}
    for spec in PULLS:
        pairs: Optional[Sequence[tuple[str, str]]] = None
        if only is not None and spec.catalog_view in only:
            pairs = only[spec.catalog_view]
            if not pairs:
                counts[spec.raw_table] = 0
                continue
        available = profile.columns_of(spec.catalog_view) if profile else set()
        if pairs is not None:
            # Skip pairs whose rows are already in the raw table, and filter
            # returned rows to the requested pairs: repeat scoped calls across
            # targeted rounds stay idempotent, and a fixture session (which
            # serves the same canned response for every chunk) cannot smuggle
            # extra rows in. Raw column 0/1 are the schema/name columns for
            # every scopable view.
            lib_col, name_col = RAW_COLUMNS[spec.raw_table][:2]
            existing = {
                ((l or "").upper(), (n or "").upper())
                for l, n in con.execute(
                    f"SELECT DISTINCT {lib_col}, {name_col} "
                    f"FROM {spec.raw_table}").fetchall()
            }
            todo_set = {(l.upper(), n.upper()) for l, n in pairs} - existing
            todo = sorted(todo_set)
            if not todo:
                counts[spec.raw_table] = 0
                continue
            rows: list[tuple] = []
            missing: list[str] = []
            p.start(f"catalog {spec.name} ({len(todo)} pairs)")
            for frag in pairs_filter(spec.schema_filter, "TABLE_NAME", todo):
                # No TABLE_SCHEMA IN (...) conjunct here: the pairs already
                # pin exact (schema, name) scope, so a slice object in a
                # library outside config.libraries is still reachable
                # (library_discovery: slice — see extract/targeted.py).
                sql, missing = spec.build_select(available, None, extra_where=frag)
                res = _fetch(session, f"catalog.{spec.name}", sql)
                rows.extend(
                    tuple(r) for r in res.rows
                    if (str(r[0] or "").upper(), str(r[1] or "").upper())
                    in todo_set
                )
            if missing:
                counts[f"{spec.name}_missing_columns"] = len(missing)
            counts[spec.raw_table] = insert_rows(
                con, spec.raw_table, RAW_COLUMNS[spec.raw_table], rows)
            p.done(f"catalog {spec.name} ({len(todo)} pairs)",
                   rows=counts[spec.raw_table])
            continue
        sql, missing = spec.build_select(available, libs)
        if missing:
            counts[f"{spec.name}_missing_columns"] = len(missing)
        p.start(f"catalog {spec.name}")
        res = _fetch(session, f"catalog.{spec.name}", sql)
        counts[spec.raw_table] = insert_rows(
            con, spec.raw_table, RAW_COLUMNS[spec.raw_table],
            [tuple(r) for r in res.rows])
        p.done(f"catalog {spec.name}", rows=counts[spec.raw_table])
    return counts
