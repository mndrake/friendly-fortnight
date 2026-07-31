"""CLI-level validation tests."""
from __future__ import annotations

from typer.testing import CliRunner

from lineage.cli import app

runner = CliRunner()


def test_extract_full_without_source_files_raises(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    duckdb_path = tmp_path / "lineage.duckdb"
    cfg_path.write_text(
        "scratch_lib: QTEMP\n"
        "libraries: [APPLIB]\n"
        "output_seeds:\n"
        "  - id: X\n"
        "    library: APPLIB\n"
        "    file: OUT\n"
        "storage:\n"
        f"  duckdb: {duckdb_path}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["extract", "--config", str(cfg_path)])
    assert result.exit_code != 0
    assert "full extraction requires source_files" in result.output


def test_extract_targeted_without_source_files_does_not_raise_validation(tmp_path):
    """The full-mode source_files guard must not fire for targeted scope
    (verified up to the point host connection would be attempted -- no
    fixture host is wired up here, so this only proves the validation itself
    is scope-conditional)."""
    cfg_path = tmp_path / "config.yaml"
    duckdb_path = tmp_path / "lineage.duckdb"
    cfg_path.write_text(
        "scratch_lib: QTEMP\n"
        "libraries: [APPLIB]\n"
        "output_seeds:\n"
        "  - id: X\n"
        "    library: APPLIB\n"
        "    file: OUT\n"
        "storage:\n"
        f"  duckdb: {duckdb_path}\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["extract", "--config", str(cfg_path), "--scope", "targeted"])
    assert "full extraction requires source_files" not in result.output


def test_sql_errors_groups_and_exports(tmp_path):
    import json as jsonmod

    from lineage import db as dbmod

    cfg_path = tmp_path / "config.yaml"
    duckdb_path = tmp_path / "lineage.duckdb"
    cfg_path.write_text(
        "scratch_lib: QTEMP\n"
        "libraries: [APPLIB]\n"
        "output_seeds:\n"
        "  - id: X\n"
        "    library: APPLIB\n"
        "    file: OUT\n"
        "storage:\n"
        f"  duckdb: {duckdb_path}\n",
        encoding="utf-8",
    )
    con = dbmod.connect(str(duckdb_path))
    stmts = [
        # Two instances of the same systemic signature (parser gap shape).
        ("APPLIB/PGM1", 1, "PARSE_ERROR",
         "Invalid expression / Unexpected token. Line 1, Col: 8.",
         "WEIRD DB2 STATEMENT ONE"),
        ("APPLIB/PGM2", 1, "PARSE_ERROR",
         "Invalid expression / Unexpected token. Line 3, Col: 12.",
         "WEIRD DB2 STATEMENT TWO"),
        # A partially parsed DDL fallback.
        ("APPLIB/DDLPGM", 1, "CREATE_TABLE",
         "unsupported syntax (sqlglot fell back to Command)",
         "CREATE TABLE APPLIB.T1 (A INT) IN APPLIB.TS1"),
    ]
    for prog, seq, stype, err, raw in stmts:
        con.execute(
            "INSERT INTO parsed_sql_statements (program, seq, stmt_type, "
            "ast_json, tables_read, tables_written, column_lineage, "
            "columns_used, parse_error, raw_sql) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [prog, seq, stype, None, jsonmod.dumps([]), jsonmod.dumps([]),
             jsonmod.dumps([]), jsonmod.dumps([]), err, raw])
    con.close()

    out_csv = tmp_path / "errors.csv"
    result = runner.invoke(app, ["sql-errors", "--config", str(cfg_path),
                                 "--out", str(out_csv)])
    assert result.exit_code == 0, result.output
    assert "3 statements carry a parse error" in result.output
    assert "1 partially parsed" in result.output
    assert "2 failed outright" in result.output
    # Line/Col normalized away -> both PARSE_ERRORs share one signature.
    assert "[   2x] Invalid expression / Unexpected token. Line _, Col _" \
        in result.output
    assert "WEIRD DB2 STATEMENT ONE" in result.output
    csv_text = out_csv.read_text(encoding="utf-8")
    assert csv_text.count("\n") == 4   # header + 3 rows
    assert "CREATE TABLE APPLIB.T1" in csv_text


def test_sql_errors_empty_store(tmp_path):
    from lineage import db as dbmod

    cfg_path = tmp_path / "config.yaml"
    duckdb_path = tmp_path / "lineage.duckdb"
    cfg_path.write_text(
        "scratch_lib: QTEMP\nlibraries: [APPLIB]\n"
        "output_seeds:\n  - id: X\n    library: APPLIB\n    file: OUT\n"
        "storage:\n"
        f"  duckdb: {duckdb_path}\n",
        encoding="utf-8",
    )
    dbmod.connect(str(duckdb_path)).close()
    result = runner.invoke(app, ["sql-errors", "--config", str(cfg_path)])
    assert result.exit_code == 0
    assert "no SQL parse errors recorded" in result.output
