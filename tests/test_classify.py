from conftest import make_member

from lineage.parse.classify import (COMPLEX, EXT_IO, SQL_PURE, classify)


def test_ext_described_io():
    m = make_member("RPT", "RPG",
                    "     FORDERS  IF  E                  DISK\n"
                    "     FORDSUM  O   E                  DISK\n"
                    "     C                     READ ORDERS                   90\n")
    c = classify(m)
    assert c.program_class == EXT_IO
    assert not c.needs_review


def test_program_described_is_complex():
    m = make_member("LEG", "RPG",
                    "     FLEGACY  IF  F     132          DISK\n"
                    "     C                     READ LEGACY                   90\n")
    c = classify(m)
    assert c.program_class == COMPLEX
    assert "program_described_fspec" in c.reasons
    assert c.needs_review


def test_sql_pure():
    m = make_member("SQLONLY", "SQLRPG",
                    "     C/EXEC SQL\n"
                    "     C+ SELECT COUNT(*) INTO :N FROM ORDERS\n"
                    "     C/END-EXEC\n")
    c = classify(m)
    assert c.program_class == SQL_PURE


def test_mixed_sql_and_io_is_ext_io():
    m = make_member("MIX", "SQLRPG",
                    "     FORDSUM  O   E                  DISK\n"
                    "     C/EXEC SQL\n"
                    "     C+ SELECT AMOUNT INTO :A FROM ORDERS\n"
                    "     C/END-EXEC\n")
    c = classify(m)
    assert c.program_class == EXT_IO
    assert "mixed_sql_and_record_io" in c.reasons


def test_dynamic_sql_needs_review():
    m = make_member("DYN", "SQLRPG",
                    "     C/EXEC SQL\n"
                    "     C+ PREPARE S1 FROM :STMT\n"
                    "     C/END-EXEC\n")
    c = classify(m)
    assert c.needs_review
    assert "dynamic_sql" in c.reasons


def test_move_heavy_is_complex():
    lines = ["     FORDERS  IF  E                  DISK\n"]
    for i in range(8):
        lines.append(f"     C                     MOVE FLD{i}      WF{i}\n")
    lines += ["     C                     READ ORDERS                   90\n",
              "     C                     SETON                     LR\n",
              "     C                     SETON                     LR\n"]
    m = make_member("MOVY", "RPG", "".join(lines))
    c = classify(m)
    assert c.program_class == COMPLEX
    assert "move_heavy" in c.reasons


def test_fixture_estate_classification(parsed):
    rows = dict(parsed.execute(
        "SELECT program, program_class FROM program_classification").fetchall())
    assert rows["APPLIB/RPT001"] == EXT_IO
    assert rows["APPLIB/RPT002"] == EXT_IO
    assert rows["APPLIB/SQLEXT"] == SQL_PURE
    assert rows["APPLIB/PGMDESC"] == COMPLEX
