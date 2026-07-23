"""DuckDB store: connection management and schema application.

The store is intentionally thin. Callers use plain SQL against a DuckDB
connection; helpers here cover schema application and a couple of ergonomic
patterns (replace-a-table-from-rows) used across the extract/parse modules.
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

SCHEMA_RESOURCE = ("lineage.schema", "schema.sql")

# The set of tables truncated by ``reset_layer`` for each pipeline stage, so a
# re-run of a stage does not leave stale rows behind.
LAYER_TABLES: dict[str, tuple[str, ...]] = {
    "raw": (
        "raw_systables", "raw_syscolumns", "raw_sysviews", "raw_sysviewdep",
        "raw_syspartitionstat", "raw_dsppgmref", "raw_dspdbr", "raw_dspffd",
        "raw_source_members",
    ),
    "parsed": (
        "parsed_cl_statements", "parsed_cl_overrides", "parsed_cl_calls",
        "parsed_dds_files", "parsed_dds_fields", "parsed_rpg_files",
        "parsed_rpg_io_ops", "parsed_sql_statements", "program_classification",
    ),
    "graph": ("nodes", "edges"),
    "analysis": (
        "output_lineage", "commonality_matrix", "complexity_scores", "gaps",
    ),
}


def schema_sql() -> str:
    pkg, name = SCHEMA_RESOURCE
    return resources.files(pkg).joinpath(name).read_text(encoding="utf-8")


def connect(path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open (or create) the DuckDB store and ensure the schema exists.

    ``path`` of ``None`` or ``":memory:"`` opens an in-memory database, used by
    tests.
    """
    if path is None:
        target = ":memory:"
    else:
        target = str(path)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(target)
    apply_schema(con)
    return con


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(schema_sql())


def reset_layer(con: duckdb.DuckDBPyConnection, layer: str) -> None:
    if layer not in LAYER_TABLES:
        raise ValueError(f"unknown layer '{layer}'")
    for table in LAYER_TABLES[layer]:
        con.execute(f"DELETE FROM {table}")


def insert_rows(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> int:
    """Bulk-insert rows into ``table`` for the given ``columns``.

    Returns the number of rows inserted. Uses DuckDB's executemany.
    """
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(columns))
    collist = ", ".join(columns)
    con.executemany(
        f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",
        rows,
    )
    return len(rows)


def table_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
