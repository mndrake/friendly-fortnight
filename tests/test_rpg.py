from conftest import make_member

from lineage.parse import rpg


def _parse3(text: str, name: str = "TESTPGM", mtype: str = "RPG"):
    return rpg.parse(make_member(name, mtype, text))


def test_rpg3_fspecs():
    p = _parse3(
        "     FCUSTLF1 IF  E                  DISK\n"
        "     FCUSTRPT O   E                  DISK\n"
        "     FWORKFIL UF  F     132          DISK\n"
    )
    by_name = {f.file: f for f in p.files}
    assert by_name["CUSTLF1"].usage == "input"
    assert by_name["CUSTLF1"].extname == "CUSTLF1"     # ext-described: name
    assert not by_name["CUSTLF1"].program_described
    assert by_name["CUSTRPT"].usage == "output"
    assert by_name["WORKFIL"].usage == "update"
    assert by_name["WORKFIL"].program_described        # 'F' in col 19
    assert by_name["WORKFIL"].extname is None


def test_rpg3_cspec_io_ops():
    p = _parse3(
        "     FORDERS  IF  E                  DISK\n"
        "     FORDSUM  O   E                  DISK\n"
        "     C                     READ ORDERS                   90\n"
        "     C           K01       CHAINORDERS                   91\n"
        "     C                     UPDATORDREC\n"
        "     C                     SETON                     LR\n"
    )
    ops = [(o.opcode, o.file, o.direction) for o in p.io_ops]
    assert ("READ", "ORDERS", "read") in ops
    assert ("CHAIN", "ORDERS", "read") in ops
    # UPDAT names a record format, two files declared -> dropped, not guessed
    assert not any(o.opcode == "UPDAT" for o in p.io_ops)


def test_rpg3_single_file_fallback():
    p = _parse3(
        "     FORDERS  UF  E                  DISK\n"
        "     C                     READ ORDERS                   90\n"
        "     C                     UPDATORDREC\n"
    )
    assert ("UPDAT", "ORDERS", "write") in [
        (o.opcode, o.file, o.direction) for o in p.io_ops]


def test_rpg3_copy_directive():
    p = _parse3("     /COPY QRPGSRC,STDHDR\n")
    assert len(p.copies) == 1
    c = p.copies[0]
    assert c.srcfile == "QRPGSRC"
    assert c.member == "STDHDR"


def test_rpg3_copy_with_library():
    p = _parse3("     /COPY APPLIB/QRPGSRC,STDHDR\n")
    c = p.copies[0]
    assert c.library == "APPLIB"
    assert c.srcfile == "QRPGSRC"
    assert c.member == "STDHDR"


def test_sqlrpg_block_extraction_and_hostvars():
    p = _parse3(
        "     C/EXEC SQL\n"
        "     C+ UPDATE ORDERS SET AMOUNT = :NEWAMT\n"
        "     C+   WHERE ORDNO = :ORD\n"
        "     C/END-EXEC\n",
        mtype="SQLRPG",
    )
    assert len(p.sql_blocks) == 1
    sql = p.sql_blocks[0]
    assert "UPDATE ORDERS" in sql
    assert ":NEWAMT" not in sql and "?" in sql   # host vars normalised
    assert "EXEC" not in sql.upper()


def test_rpg3_comments_ignored():
    p = _parse3(
        "     F* THIS IS A COMMENT\n"
        "     C* SO IS THIS\n"
        "     FORDERS  IF  E                  DISK\n"
    )
    assert len(p.files) == 1
    assert not p.io_ops


def test_rpg3_krename_continuation():
    p = _parse3(
        "     FCUSTLF1 IF  E                  DISK\n"
        "     F        CUSTREC                          KRENAMECREC1\n"
    )
    assert p.files[0].rename_rec == "CREC1"


def test_rpgle_free_format():
    p = _parse3(
        "       DCL-F ORDERS USAGE(*INPUT) EXTFILE('APPLIB/ORDERS');\n"
        "       DCL-F ORDSUM USAGE(*OUTPUT);\n"
        "       READ ORDERS;\n"
        "       WRITE ORDSUM;\n",
        mtype="RPGLE",
    )
    by_name = {f.file: f for f in p.files}
    assert by_name["ORDERS"].usage == "input"
    assert by_name["ORDSUM"].usage == "output"
    ops = {(o.opcode, o.file) for o in p.io_ops}
    assert ("READ", "ORDERS") in ops
    assert ("WRITE", "ORDSUM") in ops


def test_rpgle_fixed_fspec_extname():
    p = _parse3(
        "     FORDFILE   IF   E             DISK    EXTFILE('ORDERS')\n",
        mtype="RPGLE",
    )
    f = p.files[0]
    assert f.file == "ORDFILE"
    assert f.extname == "ORDERS"


def test_ispecs_ospecs_flags():
    p = _parse3(
        "     FLEGACY  IF  F     132          DISK\n"
        "     ILEGACY  AA  01\n"
        "     OLEGACY  E                OUT1\n"
    )
    assert p.has_ispecs
    assert p.has_ospecs


def test_referenced_fields_from_factor1_and_factor2():
    p = _parse3(
        "     FCUSTLF1 IF  E                  DISK\n"
        "     FORDERS  IF  E                  DISK\n"
        "     C           CUSTNO    CHAINCUSTLF1                  91\n"
    )
    # Factor1 (CUSTNO) is harvested; factor2 (CUSTLF1) is a declared file
    # name and excluded.
    assert p.referenced_fields == {"CUSTNO"}


def test_referenced_fields_exclude_indicators_and_figurative_constants():
    p = _parse3(
        "     FORDERS  IF  E                  DISK\n"
        "     C           N90       SETONAMOUNT                   91\n"
        "     C                     COMP      AMOUNT    *ZERO          91\n"
    )
    # Figurative constants (*ZERO) never surface as referenced fields; a
    # genuine identifier (AMOUNT) does.
    assert "AMOUNT" in p.referenced_fields
    assert not any(f.startswith("*") for f in p.referenced_fields)


def test_referenced_fields_result_area():
    p = _parse3(
        "     FORDERS  IF  E                  DISK\n"
        "     C                     ADD  1         TOTAMT\n"
    )
    assert "TOTAMT" in p.referenced_fields


def test_ospec_field_entry_harvested():
    # Field-entry area is idx 31:43 — built by column position, not guessed
    # spacing, to keep the fixed-format layout exact.
    def ospec_line(field: str) -> str:
        chars = [" "] * 48
        chars[5] = "O"
        for i, c in enumerate(field):
            chars[31 + i] = c
        return "".join(chars)

    p = _parse3(
        "     FCUSTRPT O   E                  DISK\n"
        + ospec_line("CUSTNO") + "\n"
        + ospec_line("AMOUNT") + "\n"
    )
    assert {"CUSTNO", "AMOUNT"} <= p.referenced_fields


def test_referenced_fields_free_format():
    p = _parse3(
        "       DCL-F ORDERS USAGE(*INPUT);\n"
        "       IF CUSTNO = 0;\n"
        "         EVAL AMOUNT = AMOUNT + 1;\n"
        "       ENDIF;\n",
        mtype="RPGLE",
    )
    assert {"CUSTNO", "AMOUNT"} <= p.referenced_fields
    # Declared file name and opcodes are not referenced fields.
    assert "ORDERS" not in p.referenced_fields
    assert "IF" not in p.referenced_fields
    assert "EVAL" not in p.referenced_fields


def test_referenced_fields_free_format_declaration_skipped():
    p = _parse3(
        "       DCL-S CUSTNO PACKED(9:2);\n",
        mtype="RPGLE",
    )
    # A DCL-* line introduces a name, it does not reference an existing
    # field — it must not contribute a referenced field.
    assert "CUSTNO" not in p.referenced_fields


def test_parse_all_fixture_estate_field_refs(parsed):
    rows = {r[0] for r in parsed.execute(
        "SELECT field_name FROM parsed_rpg_field_refs WHERE program = "
        "'APPLIB/RPT001'").fetchall()}
    assert "CUSTNO" in rows


def test_parse_all_fixture_estate(parsed):
    rows = parsed.execute(
        "SELECT program, file, usage FROM parsed_rpg_files "
        "WHERE program = 'APPLIB/RPT001' ORDER BY file").fetchall()
    assert rows == [
        ("APPLIB/RPT001", "CUSTLF1", "input"),
        ("APPLIB/RPT001", "CUSTRPT", "output"),
        ("APPLIB/RPT001", "ORDERS", "input"),
    ]
    blocks = parsed.execute(
        "SELECT program, raw_sql FROM _rpg_sql_blocks").fetchall()
    assert any("INSERT INTO ORDEXT" in sql for _, sql in blocks)


# --- O-spec / I-spec / C-spec move extraction (definitive field lineage) ------

def _fixed(col_chars: dict[int, str]) -> str:
    """Build a fixed-format line by exact column index."""
    n = max(col_chars) + 1
    chars = [" "] * n
    for i, c in col_chars.items():
        for j, ch in enumerate(c):
            if i + j >= len(chars):
                chars.extend(" " * (i + j - len(chars) + 1))
            chars[i + j] = ch
    return "".join(chars)


def test_ospec_fields_extracted_with_end_positions():
    p = _parse3(
        "     FLEGOUT  O   F     132          DISK\n"
        + _fixed({5: "O", 6: "LEGOUT", 14: "E", 31: "OUT1"}) + "\n"   # record
        + _fixed({5: "O", 31: "OFLD1", 41: "6"}) + "\n"
        + _fixed({5: "O", 31: "OFLD2", 40: "36"}) + "\n"
        + _fixed({5: "O", 31: "PAGE", 41: "4"}) + "\n"                # special
        + _fixed({5: "O", 31: "UDATE", 40: "44"}) + "\n"              # special
    )
    entries = [(o.file, o.field, o.end_pos) for o in p.ospec_fields]
    assert entries == [("LEGOUT", "OFLD1", 6), ("LEGOUT", "OFLD2", 36)]
    # The EXCPT name on the record line is not a field.
    assert not any(o.field == "OUT1" for o in p.ospec_fields)


def test_ispec_fields_extracted_with_positions():
    p = _parse3(
        "     FLEGACY  IF  F     132          DISK\n"
        + _fixed({5: "I", 6: "LEGACY", 14: "AA", 18: "01"}) + "\n"
        + _fixed({5: "I", 46: "1", 49: "6", 51: "0", 52: "FLD1"}) + "\n"
        + _fixed({5: "I", 46: "7", 48: "36", 52: "FLD2"}) + "\n"
    )
    entries = [(f.file, f.field, f.from_pos, f.to_pos) for f in p.ispec_fields]
    assert entries == [("LEGACY", "FLD1", 1, 6), ("LEGACY", "FLD2", 7, 36)]


def test_cspec_moves_field_sources_and_constants():
    p = _parse3(
        "     FORDERS  IF  E                  DISK\n"
        + _fixed({5: "C", 27: "MOVE", 32: "FLD1", 42: "WFLD1"}) + "\n"
        + _fixed({5: "C", 17: "AMT1", 27: "ADD", 32: "AMT2", 42: "TOTAL"}) + "\n"
        + _fixed({5: "C", 27: "Z-ADD", 32: "0", 42: "COUNT"}) + "\n"      # literal
        + _fixed({5: "C", 27: "MOVEL", 32: "'AB'", 42: "CODE"}) + "\n"    # literal
        + _fixed({5: "C", 27: "MOVE", 32: "ARR,3", 42: "OUT1"}) + "\n"    # index
    )
    moves = [(m.opcode, m.source, m.result) for m in p.moves]
    assert ("MOVE", "FLD1", "WFLD1") in moves
    # ADD contributes both factors as sources.
    assert ("ADD", "AMT1", "TOTAL") in moves and ("ADD", "AMT2", "TOTAL") in moves
    # Literal assignments keep a source=None row: assigned, no field source.
    assert ("Z-ADD", None, "COUNT") in moves
    assert ("MOVEL", None, "CODE") in moves
    # Array index reduces to the array name.
    assert ("MOVE", "ARR", "OUT1") in moves


def test_parse_all_persists_field_level_rows(parsed):
    ospec = parsed.execute(
        "SELECT file, field_name, end_pos FROM parsed_rpg_ospec_fields "
        "WHERE program = 'APPLIB/PGMDESC' ORDER BY end_pos").fetchall()
    assert ospec == [("LEGOUT", "OFLD1", 6), ("LEGOUT", "OFLD2", 36)]
    ispec = parsed.execute(
        "SELECT file, field_name FROM parsed_rpg_ispec_fields "
        "WHERE program = 'APPLIB/PGMDESC' ORDER BY field_name").fetchall()
    assert ispec == [("LEGACY", "FLD1"), ("LEGACY", "FLD2")]
    moves = parsed.execute(
        "SELECT source_field, result_field FROM parsed_rpg_moves "
        "WHERE program = 'APPLIB/PGMDESC' AND result_field = 'OFLD1'"
    ).fetchall()
    assert moves == [("WFLD1", "OFLD1")]


def test_time_and_clear_record_assignment_without_source():
    p = _parse3(
        "     FORDERS  IF  E                  DISK\n"
        + _fixed({5: "C", 27: "TIME", 42: "TSTAMP"}) + "\n"
        + _fixed({5: "C", 27: "CLEAR", 42: "WTOTAL"}) + "\n"
    )
    moves = [(m.opcode, m.source, m.result) for m in p.moves]
    assert ("TIME", None, "TSTAMP") in moves
    assert ("CLEAR", None, "WTOTAL") in moves
