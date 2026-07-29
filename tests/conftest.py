"""Shared fixtures: a synthetic RPG III estate served without any host.

The estate (library APPLIB):

* PFs: CUSTMAST, ORDERS, ORDHIST, ORDARC, CUSTRPT, ORDEXT, ORDSUM
* LF:  CUSTLF1 over CUSTMAST (CNAME renames CUSTNAME)
* View: CUSTVIEW over CUSTMAST
* CL:  CLDRIVER (OVRDBF ORDERS->ORDHIST around CALL RPT001; CPYF to ORDARC),
       CLDYN (CHGVAR-built override target: unresolvable)
* RPG III: RPT001 (reads CUSTLF1+ORDERS, writes CUSTRPT),
       RPT002 (reads ORDERS, writes ORDSUM), PGMDESC (program described),
       SQLEXT (SQLRPG: INSERT INTO ORDEXT ... SELECT FROM ORDERS)
* GHOST: appears in DSPPGMREF but has no source member (missing-source gap)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).parent / "fixtures"

from lineage import db as dbmod  # noqa: E402
from lineage.config import from_dict  # noqa: E402
from lineage.extract.connection import FixtureHostSession, QueryResult  # noqa: E402

LIB = "APPLIB"


@pytest.fixture()
def config():
    return from_dict({
        "scratch_lib": "QTEMP",
        "libraries": [LIB],
        "source_files": [
            {"library": LIB, "file": "QCLSRC"},
            {"library": LIB, "file": "QRPGSRC"},
            {"library": LIB, "file": "QDDSSRC"},
        ],
        "output_seeds": [
            {"id": "CUST_MONTHLY_RPT", "library": LIB, "file": "CUSTRPT"},
            {"id": "ORDER_EXTRACT", "library": LIB, "file": "ORDEXT"},
            {"id": "ORDER_SUMMARY", "library": LIB, "file": "ORDSUM"},
            {"id": "MISSING_OUT", "library": LIB, "file": "NOWHERE"},
        ],
        "liblists": {"default": [LIB]},
    })


def _source_members() -> list[tuple[str, str, str]]:
    """(srcfile, member, member_type) discovered from fixture files."""
    out = []
    for srcdir in sorted((FIXTURES / "source").iterdir()):
        for f in sorted(srcdir.iterdir()):
            member, mtype = f.name.rsplit(".", 1)
            out.append((srcdir.name, member, mtype))
    return out


def _member_lines(srcfile: str, member: str, mtype: str) -> list[str]:
    path = FIXTURES / "source" / srcfile / f"{member}.{mtype}"
    return path.read_text(encoding="utf-8").splitlines()


_PF = ["CUSTMAST", "ORDERS", "ORDHIST", "ORDARC", "CUSTRPT", "ORDEXT", "ORDSUM"]

_FFD: dict[str, list[str]] = {
    "CUSTMAST": ["CUSTNO", "CUSTNAME", "REGION"],
    "ORDERS": ["ORDNO", "CUSTNO", "AMOUNT", "ORDDATE"],
    "ORDHIST": ["ORDNO", "CUSTNO", "AMOUNT", "ORDDATE"],
    "ORDARC": ["ORDNO", "CUSTNO", "AMOUNT", "ORDDATE"],
    "CUSTRPT": ["CUSTNO", "CNAME", "AMOUNT"],
    "ORDEXT": ["ORDNO", "AMT"],
    "ORDSUM": ["ORDNO", "AMOUNT"],
    "CUSTLF1": ["CUSTNO", "CNAME"],
}

# DSPPGMREF: (program, object, type, usage) — usage 1=input 2=output 3=both.
_PGMREF = [
    ("RPT001", "CUSTLF1", "F", "1"),
    ("RPT001", "ORDERS", "F", "1"),
    ("RPT001", "CUSTRPT", "F", "2"),
    ("RPT002", "ORDERS", "F", "1"),
    ("RPT002", "ORDSUM", "F", "2"),
    ("SQLEXT", "ORDERS", "F", "1"),
    ("SQLEXT", "ORDEXT", "F", "2"),
    ("CLDRIVER", "RPT001", "PGM", ""),
    ("CLDRIVER", "SQLEXT", "PGM", ""),
    ("CLDRIVER", "ORDERS", "F", "1"),
    ("CLDRIVER", "ORDARC", "F", "2"),
    ("CLDYN", "RPT002", "PGM", ""),
    ("GHOST", "ORDERS", "F", "1"),
]


# Catalog shapes served by the fixture host: the columns each QSYS2 view
# "has" on this imaginary IBM i 7.4 box. Drives the capability probe.
_CATALOG_SHAPES: dict[str, list[str]] = {
    "SYSTABLES": ["TABLE_SCHEMA", "TABLE_NAME", "SYSTEM_TABLE_NAME",
                  "TABLE_TYPE", "FILE_TYPE", "NUMBER_ROWS", "LONG_COMMENT"],
    "SYSCOLUMNS": ["TABLE_SCHEMA", "TABLE_NAME", "SYSTEM_TABLE_NAME",
                   "COLUMN_NAME", "SYSTEM_COLUMN_NAME", "ORDINAL_POSITION",
                   "DATA_TYPE", "LENGTH", "NUMERIC_SCALE", "IS_NULLABLE",
                   "COLUMN_HEADING"],
    "SYSVIEWS": ["TABLE_SCHEMA", "TABLE_NAME", "SYSTEM_VIEW_NAME",
                 "VIEW_DEFINITION"],
    "SYSVIEWDEP": ["VIEW_SCHEMA", "VIEW_NAME", "OBJECT_SCHEMA",
                   "OBJECT_NAME", "OBJECT_TYPE"],
    "SYSPARTITIONSTAT": ["TABLE_SCHEMA", "TABLE_NAME", "SYSTEM_TABLE_NAME",
                         "TABLE_PARTITION", "NUMBER_ROWS", "SOURCE_TYPE"],
    "SYSROUTINES": ["ROUTINE_SCHEMA", "ROUTINE_NAME"],
}


def build_session() -> FixtureHostSession:
    responses: dict[str, QueryResult] = {}

    responses["probe.env"] = QueryResult(
        columns=["OS_VERSION", "OS_RELEASE"], rows=[("7", "4")])
    responses["probe.catalog_columns"] = QueryResult(
        columns=["TABLE_NAME", "COLUMN_NAME"],
        rows=[(view, col) for view, cols in _CATALOG_SHAPES.items()
              for col in cols])
    responses["probe.ifs_read"] = QueryResult(columns=["COUNT"], rows=[(1,)])

    responses["catalog.systables"] = QueryResult(
        columns=["TABLE_SCHEMA", "TABLE_NAME", "SYSTEM_TABLE_NAME",
                 "TABLE_TYPE", "FILE_TYPE", "CARD", "LONG_COMMENT"],
        rows=[(LIB, n, n, "P", "D", 100, None) for n in _PF]
        + [(LIB, "CUSTLF1", "CUSTLF1", "L", "D", None, None),
           (LIB, "CUSTVIEW", "CUSTVIEW", "V", "D", None, None)],
    )
    responses["catalog.syscolumns"] = QueryResult(
        columns=["TABLE_SCHEMA", "TABLE_NAME", "SYSTEM_TABLE_NAME",
                 "COLUMN_NAME", "SYSTEM_COLUMN_NAME", "ORDINAL_POSITION",
                 "DATA_TYPE", "LENGTH", "NUMERIC_SCALE", "IS_NULLABLE",
                 "COLUMN_HEADING"],
        rows=[(LIB, t, t, c, c, i + 1, "DECIMAL", 9, 0, "N", c)
              for t, cols in _FFD.items() for i, c in enumerate(cols)],
    )
    responses["catalog.sysviews"] = QueryResult(
        columns=["TABLE_SCHEMA", "TABLE_NAME", "SYSTEM_VIEW_NAME",
                 "VIEW_DEFINITION"],
        rows=[(LIB, "CUSTVIEW", "CUSTVIEW",
               "SELECT CUSTNO, CUSTNAME AS CNAME FROM APPLIB.CUSTMAST "
               "WHERE REGION = 'A'")],
    )
    responses["catalog.sysviewdep"] = QueryResult(
        columns=["VIEW_SCHEMA", "VIEW_NAME", "TABLE_SCHEMA", "TABLE_NAME",
                 "OBJECT_TYPE"],
        rows=[(LIB, "CUSTVIEW", LIB, "CUSTMAST", "TABLE")],
    )
    responses["catalog.syspartitionstat"] = QueryResult(
        columns=["TABLE_SCHEMA", "TABLE_NAME", "SYSTEM_TABLE_NAME",
                 "PARTITION_NAME", "NUMBER_ROWS", "SOURCE_TYPE"],
        rows=[(LIB, t, t, t, 100, None) for t in _PF],
    )

    responses["xref.dsppgmref"] = QueryResult(
        columns=["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
        rows=[(LIB, pgm, LIB, obj, otype, usage, 1)
              for pgm, obj, otype, usage in _PGMREF],
    )
    responses["xref.dspdbr"] = QueryResult(
        columns=["dep_lib", "dep_file", "based_lib", "based_file", "dep_type"],
        rows=[(LIB, "CUSTLF1", LIB, "CUSTMAST", "D")],
    )
    responses["xref.dspffd"] = QueryResult(
        columns=["file_lib", "file_name", "record_format", "field_name",
                 "field_type", "field_length", "field_scale", "field_text",
                 "field_ordinal"],
        rows=[(LIB, t, t + "R", c, "P", 9, 0, c, i + 1)
              for t, cols in _FFD.items() for i, c in enumerate(cols)],
    )

    for srcfile in {"QCLSRC", "QRPGSRC", "QDDSSRC"}:
        members = [(m, t) for sf, m, t in _source_members() if sf == srcfile]
        responses[f"source.members.{LIB}.{srcfile}"] = QueryResult(
            columns=["member", "member_type"], rows=members)
        for member, mtype in members:
            lines = _member_lines(srcfile, member, mtype)
            responses[f"source.text.{LIB}.{srcfile}.{member}"] = QueryResult(
                columns=["SRCSEQ", "SRCDTA"],
                rows=[(i + 1, ln) for i, ln in enumerate(lines)])

    return FixtureHostSession(responses=responses)


@pytest.fixture()
def session():
    return build_session()


@pytest.fixture()
def con():
    c = dbmod.connect(None)
    yield c
    c.close()


@pytest.fixture()
def extracted(con, session, config):
    """DuckDB store populated with the full fixture estate (raw layer)."""
    from lineage.extract import catalog, hostinfo, source, xref
    profile = hostinfo.probe(session)
    profile.save(con)
    catalog.harvest(session, con, config, profile)
    xref.harvest(session, con, config)
    source.harvest(session, con, config, profile)
    return con


@pytest.fixture()
def parsed(extracted):
    """Raw + parsed layers populated."""
    from lineage.parse import cl, classify, dds, embedded_sql, rpg
    dds.parse_all(extracted)
    cl.parse_all(extracted)
    rpg.parse_all(extracted)
    embedded_sql.parse_all(extracted)
    classify.classify_all(extracted)
    return extracted


@pytest.fixture()
def built(parsed, config):
    """Graph built at full fidelity; returns (con, graph)."""
    from lineage.graph.build import build_graph
    g = build_graph(parsed, config, phase=3)
    return parsed, g


def make_member(name: str, mtype: str, text: str, library: str = LIB,
                srcfile: str = "QTEST"):
    """Helper for parser unit tests: build a SourceMember from literal text."""
    from lineage.parse.base import SourceMember
    return SourceMember(library=library, srcfile=srcfile, member=name,
                        member_type=mtype, lines=text.splitlines())
