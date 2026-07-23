"""Outfile layout mapping tests — explicit column mapping, no positional
guesses (Phase 1 acceptance)."""
import pytest

from lineage.extract.connection import QueryResult
from lineage.extract.xref import (DBR_LAYOUT, FFD_LAYOUT, PGMREF_LAYOUT,
                                  map_dbr, map_ffd, map_pgmref,
                                  usage_directions)


def test_pgmref_mapping_by_name():
    res = QueryResult(
        columns=list(PGMREF_LAYOUT.raw_columns),
        rows=[("APPLIB  ", "RPT001  ", "APPLIB  ", "ORDERS  ", "F", "1", 2)],
    )
    rows = map_pgmref(res)
    assert rows == [("APPLIB", "RPT001", "APPLIB", "ORDERS", "F", "1", 2)]


def test_pgmref_missing_column_fails_loudly():
    res = QueryResult(columns=["program_lib", "program_name"], rows=[])
    with pytest.raises(ValueError, match="missing expected columns"):
        map_pgmref(res)


def test_pgmref_column_order_does_not_matter():
    cols = list(reversed(PGMREF_LAYOUT.raw_columns))
    row = dict(zip(PGMREF_LAYOUT.raw_columns,
                   ("L", "P", "OL", "ON", "F", "2", 1)))
    res = QueryResult(columns=cols, rows=[tuple(row[c] for c in cols)])
    assert map_pgmref(res) == [("L", "P", "OL", "ON", "F", "2", 1)]


def test_dbr_mapping():
    res = QueryResult(
        columns=list(DBR_LAYOUT.raw_columns),
        rows=[("APPLIB", "CUSTLF1", "APPLIB", "CUSTMAST", "D")],
    )
    assert map_dbr(res) == [("APPLIB", "CUSTLF1", "APPLIB", "CUSTMAST", "D")]


def test_ffd_mapping_coerces_numerics():
    res = QueryResult(
        columns=list(FFD_LAYOUT.raw_columns),
        rows=[("APPLIB", "ORDERS", "ORDREC", "AMOUNT", "P", "9", None,
               "Order amount", "3")],
    )
    rows = map_ffd(res)
    assert rows[0][5] == 9          # length coerced to int
    assert rows[0][6] is None       # scale None preserved
    assert rows[0][8] == 3


def test_usage_directions():
    assert usage_directions("1") == ("reads",)
    assert usage_directions("2") == ("writes",)
    assert usage_directions("3") == ("reads", "writes")
    assert usage_directions("4") == ("reads", "writes")
    # unknown/blank defaults to read (safe direction)
    assert usage_directions("") == ("reads",)
    assert usage_directions(None) == ("reads",)


def test_select_list_aliases_every_field():
    sql = PGMREF_LAYOUT.select_list()
    for fld, col in PGMREF_LAYOUT.fields:
        assert f"{fld} AS {col}" in sql
