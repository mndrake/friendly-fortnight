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
