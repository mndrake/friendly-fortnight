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
