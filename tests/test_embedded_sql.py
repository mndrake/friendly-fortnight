import json

from lineage.parse.embedded_sql import (analyze_statement, split_sql_script)


def test_select_lineage():
    a = analyze_statement(
        "SELECT O.ORDNO, C.CUSTNAME AS BUYER FROM APPLIB.ORDERS O "
        "JOIN APPLIB.CUSTMAST C ON O.CUSTNO = C.CUSTNO")
    assert a.stmt_type == "SELECT"
    assert set(a.tables_read) == {"APPLIB/ORDERS", "APPLIB/CUSTMAST"}
    by_target = {i["target"]: i["sources"] for i in a.column_lineage}
    assert by_target["ORDNO"] == ["APPLIB/ORDERS.ORDNO"]
    assert by_target["BUYER"] == ["APPLIB/CUSTMAST.CUSTNAME"]


def test_insert_select_positional_mapping():
    a = analyze_statement(
        "INSERT INTO ORDEXT (ORDNO, AMT) "
        "SELECT ORDNO, AMOUNT FROM ORDERS WHERE AMOUNT > 0")
    assert a.stmt_type == "INSERT"
    assert a.tables_written == ["ORDEXT"]
    assert a.tables_read == ["ORDERS"]
    by_target = {i["target"]: i["sources"] for i in a.column_lineage}
    assert by_target["ORDEXT.ORDNO"] == ["ORDERS.ORDNO"]
    assert by_target["ORDEXT.AMT"] == ["ORDERS.AMOUNT"]


def test_insert_values_no_table_sources():
    a = analyze_statement("INSERT INTO ORDEXT (ORDNO, AMT) VALUES (?, ?)")
    assert a.tables_written == ["ORDEXT"]
    by_target = {i["target"]: i["sources"] for i in a.column_lineage}
    assert by_target["ORDEXT.ORDNO"] == []


def test_update_set_lineage():
    a = analyze_statement("UPDATE ORDERS SET AMOUNT = AMOUNT * 2 WHERE ORDNO = ?")
    assert a.stmt_type == "UPDATE"
    assert a.tables_written == ["ORDERS"]
    by_target = {i["target"]: i["sources"] for i in a.column_lineage}
    assert "ORDERS.AMOUNT" in by_target


def test_cte_not_treated_as_table():
    a = analyze_statement(
        "WITH BIG AS (SELECT ORDNO, AMOUNT FROM ORDERS WHERE AMOUNT > 100) "
        "SELECT ORDNO FROM BIG")
    assert a.tables_read == ["ORDERS"]


def test_cursor_select_unwrapped():
    a = analyze_statement(
        "DECLARE C1 CURSOR FOR SELECT ORDNO FROM ORDERS")
    assert a.stmt_type == "CURSOR_SELECT"
    assert a.tables_read == ["ORDERS"]


def test_dynamic_sql_flagged():
    a = analyze_statement("EXECUTE IMMEDIATE ?")
    assert a.stmt_type == "DYNAMIC"
    assert a.is_dynamic
    a = analyze_statement("PREPARE S1 FROM ?")
    assert a.is_dynamic


def test_noise_statements_skipped():
    for sql in ("OPEN C1", "CLOSE C1", "FETCH C1 INTO ?", "COMMIT",
                "WHENEVER SQLERROR CONTINUE", "SET OPTION COMMIT = *NONE"):
        assert analyze_statement(sql).stmt_type == "NOISE"


def test_parse_error_recorded_not_fatal():
    a = analyze_statement("SELECT FROM WHERE GROUP HAVING NONSENSE ((")
    assert a.stmt_type == "PARSE_ERROR"
    assert a.parse_error


def test_db2isms_stripped():
    a = analyze_statement(
        "SELECT ORDNO FROM ORDERS WITH UR OPTIMIZE FOR 10 ROWS "
        "FOR FETCH ONLY")
    assert a.stmt_type in {"SELECT", "CURSOR_SELECT"}
    assert a.tables_read == ["ORDERS"]


def test_merge_lineage():
    a = analyze_statement(
        "MERGE INTO ORDSUM T USING ORDERS S ON T.ORDNO = S.ORDNO "
        "WHEN MATCHED THEN UPDATE SET T.AMOUNT = S.AMOUNT "
        "WHEN NOT MATCHED THEN INSERT (ORDNO, AMOUNT) "
        "VALUES (S.ORDNO, S.AMOUNT)")
    assert a.stmt_type == "MERGE"
    assert a.tables_written == ["ORDSUM"]
    assert "ORDERS" in a.tables_read


def test_create_view_lineage():
    a = analyze_statement(
        "CREATE VIEW V1 AS SELECT ORDNO, AMOUNT FROM APPLIB.ORDERS")
    assert a.stmt_type == "CREATE_VIEW"
    assert a.tables_written == ["V1"]
    assert a.tables_read == ["APPLIB/ORDERS"]


def test_split_sql_script():
    stmts = split_sql_script(
        "CREATE VIEW A AS SELECT 1 FROM X;\n"
        "-- a comment; with a semicolon\n"
        "INSERT INTO B SELECT 'a;b' FROM C;\n")
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE VIEW A")
    assert "a;b" in stmts[1]


def test_columns_used_includes_where_and_join_not_just_select_list():
    a = analyze_statement(
        "SELECT O.ORDNO FROM APPLIB.ORDERS O "
        "JOIN APPLIB.CUSTMAST C ON O.CUSTNO = C.CUSTNO "
        "WHERE O.AMOUNT > 0")
    assert set(a.columns_used) == {
        "APPLIB/ORDERS.ORDNO", "APPLIB/ORDERS.CUSTNO",
        "APPLIB/CUSTMAST.CUSTNO", "APPLIB/ORDERS.AMOUNT"}
    # The select list alone (column_lineage) does not carry the WHERE/JOIN
    # columns — columns_used is a strict superset for this statement.
    select_list_sources = {s for i in a.column_lineage for s in i["sources"]}
    assert select_list_sources < set(a.columns_used)


def test_columns_used_insert_select_covers_where_only_column():
    a = analyze_statement(
        "INSERT INTO ORDEXT (ORDNO, AMT) "
        "SELECT ORDNO, AMOUNT FROM ORDERS WHERE AMOUNT > 0")
    assert set(a.columns_used) == {"ORDERS.ORDNO", "ORDERS.AMOUNT"}


def test_columns_used_update_single_table_default():
    a = analyze_statement("UPDATE ORDERS SET AMOUNT = AMOUNT * 2 WHERE ORDNO = ?")
    assert set(a.columns_used) == {"ORDERS.AMOUNT", "ORDERS.ORDNO"}


def test_parse_all_fixture_estate_columns_used(parsed):
    rows = parsed.execute(
        "SELECT columns_used FROM parsed_sql_statements WHERE program = "
        "'APPLIB/SQLEXT'").fetchall()
    used = json.loads(rows[0][0])
    assert set(used) == {"ORDERS.ORDNO", "ORDERS.AMOUNT"}


def test_parse_all_fixture_estate(parsed):
    rows = parsed.execute(
        "SELECT program, stmt_type, tables_read, tables_written "
        "FROM parsed_sql_statements").fetchall()
    sqlext = [r for r in rows if r[0] == "APPLIB/SQLEXT"]
    assert len(sqlext) == 1
    _, stype, tr, tw = sqlext[0]
    assert stype == "INSERT"
    assert json.loads(tr) == ["ORDERS"]
    assert json.loads(tw) == ["ORDEXT"]


# --- DB2 for i DDL: system naming, column clauses, and the salvage fallback ---

def test_system_naming_slash_create_table():
    """LIB/FILE system naming was sqlglot's biggest live failure — every DDL
    member using it fell back to a bare Command ('contains unsupported
    syntax') and lost its table entirely."""
    a = analyze_statement("CREATE TABLE MYLIB/CUSTRPT (A CHAR(10) NOT NULL)")
    assert a.stmt_type == "CREATE_TABLE"
    assert a.tables_written == ["MYLIB/CUSTRPT"]
    assert a.parse_error is None


def test_system_naming_slash_dml():
    a = analyze_statement(
        "INSERT INTO MYLIB/ORDEXT SELECT ORDNO FROM MYLIB/ORDERS")
    assert a.tables_written == ["MYLIB/ORDEXT"]
    assert a.tables_read == ["MYLIB/ORDERS"]
    assert a.parse_error is None


def test_division_is_not_mistaken_for_system_naming():
    a = analyze_statement("SELECT AMOUNT / QTY FROM ORDERS")
    assert a.stmt_type == "SELECT"
    assert a.tables_read == ["ORDERS"]
    assert set(a.columns_used) == {"ORDERS.AMOUNT", "ORDERS.QTY"}


def test_db2_ddl_column_clauses_parse_cleanly():
    a = analyze_statement(
        "CREATE TABLE MYLIB.CUSTRPT (\n"
        "  CUSTNAME FOR COLUMN CUSTNM CHAR(30) CCSID 37 NOT NULL WITH DEFAULT,\n"
        "  AMT DECIMAL(11,2) NOT NULL WITH DEFAULT 0\n"
        ") RCDFMT CUSTRPTR")
    assert a.stmt_type == "CREATE_TABLE"
    assert a.tables_written == ["MYLIB/CUSTRPT"]
    assert a.parse_error is None


def test_ctas_system_naming_with_data():
    a = analyze_statement(
        "CREATE TABLE MYLIB/SUMTAB AS (SELECT ORDNO, SUM(AMOUNT) AS TOT "
        "FROM MYLIB/ORDERS GROUP BY ORDNO) WITH DATA")
    assert a.stmt_type == "CREATE_TABLE"
    assert a.tables_written == ["MYLIB/SUMTAB"]
    assert a.tables_read == ["MYLIB/ORDERS"]
    assert a.parse_error is None


def test_unparseable_ddl_salvages_created_table():
    """DB2-isms the stripper doesn't know (tablespace IN clause here) must
    not lose the created table: name-level fallback, parse_error kept."""
    a = analyze_statement("CREATE TABLE MYLIB.T1 (A INT) IN MYLIB.TS1")
    assert a.stmt_type == "CREATE_TABLE"
    assert a.tables_written == ["MYLIB/T1"]
    assert a.parse_error


def test_unparseable_ctas_salvages_inner_select():
    a = analyze_statement(
        "CREATE TABLE MYLIB.SUM2 AS (SELECT A FROM MYLIB.ORD2) "
        "WITH DATA IN MYLIB.TS1")
    assert a.stmt_type == "CREATE_TABLE"
    assert a.tables_written == ["MYLIB/SUM2"]
    assert a.tables_read == ["MYLIB/ORD2"]
    assert a.parse_error   # honest: the outer DDL never fully parsed


def test_ddl_noise_statements_skipped():
    for sql in ("LABEL ON TABLE MYLIB.CUSTRPT IS 'Customer report'",
                "LABEL ON COLUMN MYLIB.CUSTRPT (CUSTNAME IS 'Name')",
                "COMMENT ON TABLE MYLIB.CUSTRPT IS 'X'",
                "GRANT SELECT ON MYLIB.T1 TO PUBLIC",
                "REVOKE ALL ON MYLIB.T1 FROM PUBLIC",
                "SET PATH = MYLIB",
                "SET CURRENT SCHEMA = MYLIB"):
        assert analyze_statement(sql).stmt_type == "NOISE", sql


def test_sqlglot_command_warning_is_silenced():
    import logging
    assert logging.getLogger("sqlglot").getEffectiveLevel() >= logging.ERROR


def test_partial_ddl_reaches_graph_at_inferred_confidence():
    """A partially parsed CREATE TABLE (parse_error set, real stmt_type) must
    still put its WRITES edge in the graph — at inferred confidence — with a
    gap recorded, instead of being discarded."""
    import json as jsonmod

    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.graph.build import build_graph
    from lineage.parse.embedded_sql import analyze_statement as _an

    con = dbmod.connect(None)
    a = _an("CREATE TABLE TESTLIB.WEIRD (A INT) IN TESTLIB.TS1")
    con.execute(
        "INSERT INTO parsed_sql_statements (program, seq, stmt_type, ast_json,"
        " tables_read, tables_written, column_lineage, columns_used,"
        " parse_error, raw_sql) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ["TESTLIB/DDLPGM", 1, a.stmt_type, None,
         jsonmod.dumps(a.tables_read), jsonmod.dumps(a.tables_written),
         jsonmod.dumps(a.column_lineage), jsonmod.dumps(a.columns_used),
         a.parse_error, "CREATE TABLE ..."])
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["TESTLIB"],
        "output_seeds": [{"id": "X", "library": "TESTLIB", "file": "WEIRD"}],
        "liblists": {"default": ["TESTLIB"]},
    })
    g = build_graph(con, config, phase=3)
    edges = [(u, v, d) for u, v, d in g.edges(data=True)
             if d.get("kind") == "writes"]
    assert any(v == "file:TESTLIB/WEIRD" and d.get("confidence") == "inferred"
               for _u, v, d in edges)
    gaps = con.execute(
        "SELECT detail FROM gaps WHERE kind = 'parse_error'").fetchall()
    assert any("partially parsed" in (d or "") for (d,) in gaps)
    con.close()


# --- Standalone SQL script members (SQLTABL et al.) ----------------------------

def test_sql_member_type_detection():
    """SQLTABL-style stamps are standalone SQL; embedded-SQL host languages
    are not (their SQL arrives via the language parser's blocks)."""
    from lineage.parse.base import SourceMember

    def m(mtype):
        return SourceMember(library="L", srcfile="S", member="M",
                            member_type=mtype, lines=[])

    for yes in ("SQL", "SQLTABL", "SQLVIEW", "SQLPRC", "SQLIDX", "TABLE",
                "VIEW", "sqltabl", "SQLSEQ", "SQLXYZ"):
        assert m(yes).is_sql(), yes
    for no in ("SQLRPGLE", "SQLRPG", "SQLCBLLE", "CLP", "RPGLE", "PF", "LF",
               "TXT", "", None):
        assert not m(no).is_sql(), no


def test_parse_all_ingests_standalone_sql_members():
    """A SQLTABL member in raw_source_members must land in
    parsed_sql_statements with a program key tracing back to the member —
    the live BROAST bug was exactly this pass not existing."""
    from lineage import db as dbmod
    from lineage.db import insert_rows
    from lineage.parse import embedded_sql

    con = dbmod.connect(None)
    ddl = [
        "CREATE TABLE TNTACCDTA.BROAST (CACINM DECIMAL(9,2), CASSET CHAR(10))",
        "LABEL ON TABLE TNTACCDTA.BROAST IS 'Broker assets'",
        ("INSERT INTO TNTACCDTA.BROAST (CACINM, CASSET) "
         "SELECT ACINM, ASSET FROM TNTACCDTA.BARGAIN"),
    ]
    text = ";\n".join(ddl) + ";"
    insert_rows(con, "raw_source_members",
                ["library", "srcfile", "member", "member_type", "seq",
                 "line_text"],
                [("TNTACCSRC", "QDDLSRC", "BROAST", "SQLTABL", i + 1, ln)
                 for i, ln in enumerate(text.splitlines())])
    counts = embedded_sql.parse_all(con)
    assert counts["sql_statements"] >= 2   # CREATE + INSERT (LABEL is noise)

    rows = con.execute(
        "SELECT program, stmt_type, tables_written FROM parsed_sql_statements"
    ).fetchall()
    assert all(p == "TNTACCSRC/BROAST#SQLMBR:QDDLSRC" for p, _, _ in rows)
    types = {t for _, t, _ in rows}
    assert "CREATE_TABLE" in types and "INSERT" in types
    assert all("TNTACCDTA/BROAST" in tw for _, t, tw in rows
               if t in ("CREATE_TABLE", "INSERT"))
    con.close()


def test_runsqlstm_member_not_double_parsed():
    """A member reached via RUNSQLSTM keeps its CL-attributed rows only —
    the standalone pass must not duplicate it."""
    import json as jsonmod

    from lineage import db as dbmod
    from lineage.db import insert_rows
    from lineage.parse import embedded_sql

    con = dbmod.connect(None)
    insert_rows(con, "raw_source_members",
                ["library", "srcfile", "member", "member_type", "seq",
                 "line_text"],
                [("APPLIB", "QSQLSRC", "MKTBL", "SQLTABL", 1,
                  "CREATE TABLE APPLIB.T9 (A INT)")])
    insert_rows(con, "parsed_cl_calls",
                ["program", "seq", "called_lib", "called_pgm", "via",
                 "params", "resolved"],
                [("APPLIB/DRIVER", 1, None, None, "RUNSQLSTM",
                  jsonmod.dumps([jsonmod.dumps({"srcmbr": "MKTBL"})]), True)])
    embedded_sql.parse_all(con)
    rows = con.execute(
        "SELECT program FROM parsed_sql_statements").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "APPLIB/DRIVER#RUNSQLSTM:MKTBL"
    con.close()


def test_standalone_sql_member_produces_column_derives_edges():
    """End to end (parse -> phase-3 build): a BROAST-like standalone member
    whose INSERT...SELECT feeds the DDL table must yield derives_from edges
    from the output's columns to the source table's columns."""
    from lineage import db as dbmod
    from lineage.config import from_dict
    from lineage.db import insert_rows
    from lineage.graph.build import build_graph
    from lineage.parse import embedded_sql

    con = dbmod.connect(None)
    text = ("CREATE TABLE TNTACCDTA.BROAST (CACINM DECIMAL(9,2));\n"
            "INSERT INTO TNTACCDTA.BROAST (CACINM) "
            "SELECT ACINM FROM TNTACCDTA.BARGAIN;")
    insert_rows(con, "raw_source_members",
                ["library", "srcfile", "member", "member_type", "seq",
                 "line_text"],
                [("TNTACCSRC", "QDDLSRC", "BROAST", "SQLTABL", i + 1, ln)
                 for i, ln in enumerate(text.splitlines())])
    embedded_sql.parse_all(con)
    config = from_dict({
        "scratch_lib": "QTEMP", "libraries": ["TNTACCDTA"],
        "output_seeds": [{"id": "B", "library": "TNTACCDTA",
                          "file": "BROAST"}],
        "liblists": {"default": ["TNTACCDTA"]},
    })
    g = build_graph(con, config, phase=3)
    derives = [(u, v) for u, v, d in g.edges(data=True)
               if d.get("kind") == "derives_from"
               and u.startswith("column:TNTACCDTA/BROAST.")]
    assert ("column:TNTACCDTA/BROAST.CACINM",
            "column:TNTACCDTA/BARGAIN.ACINM") in derives
    con.close()
