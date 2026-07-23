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
