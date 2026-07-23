from conftest import make_member

from lineage.parse import dds


def _parse(text: str, mtype: str = "PF", name: str = "TESTF"):
    return dds.parse(make_member(name, mtype, text))


def test_pf_fields():
    f = _parse(
        "     A          R CUSTREC\n"
        "     A            CUSTNO         6P 0\n"
        "     A            CUSTNAME      30A\n"
        "     A          K CUSTNO\n"
    )
    assert f.dds_type == "PF"
    assert [r.name for r in f.records] == ["CUSTREC"]
    assert [fl.name for fl in f.records[0].fields] == ["CUSTNO", "CUSTNAME"]


def test_comments_ignored():
    f = _parse(
        "     A* HEADER COMMENT\n"
        "     A          R REC1\n"
        "     A            FLD1          10A\n"
    )
    assert len(f.records[0].fields) == 1


def test_simple_lf_rename():
    f = _parse(
        "     A          R CUSTREC                   PFILE(CUSTMAST)\n"
        "     A            CUSTNO\n"
        "     A            CNAME                     RENAME(CUSTNAME)\n"
        "     A          K CUSTNO\n",
        mtype="LF", name="CUSTLF1",
    )
    assert f.dds_type == "LF"
    assert f.based_on == ["CUSTMAST"]
    assert not f.is_join
    by_name = {fl.name: fl for fl in f.records[0].fields}
    # Non-renamed field maps to the like-named PF field.
    assert by_name["CUSTNO"].ref_field == "CUSTNO"
    assert by_name["CUSTNO"].ref_file == "CUSTMAST"
    # RENAME maps back to the original PF field.
    assert by_name["CNAME"].renamed_from == "CUSTNAME"
    assert by_name["CNAME"].ref_field == "CUSTNAME"
    assert by_name["CNAME"].ref_file == "CUSTMAST"


def test_join_lf_jfile_and_jref():
    f = _parse(
        "     A          R JREC                      JFILE(ORDERS CUSTMAST)\n"
        "     A          J                           JOIN(ORDERS CUSTMAST)\n"
        "     A                                      JFLD(CUSTNO CUSTNO)\n"
        "     A            ORDNO                     JREF(ORDERS)\n"
        "     A            CUSTNAME                  JREF(CUSTMAST)\n",
        mtype="LF", name="ORDCUST",
    )
    assert f.is_join
    assert f.based_on == ["ORDERS", "CUSTMAST"]
    by_name = {fl.name: fl for fl in f.records[0].fields}
    assert by_name["ORDNO"].ref_file == "ORDERS"
    assert by_name["CUSTNAME"].ref_file == "CUSTMAST"


def test_join_lf_duplicate_names_post_rename():
    # Two like-named PF fields disambiguated by RENAME + JREF (known trap).
    f = _parse(
        "     A          R JREC                      JFILE(ORDERS ORDHIST)\n"
        "     A            AMOUNT                    JREF(ORDERS)\n"
        "     A            HAMOUNT                   RENAME(AMOUNT) +\n"
        "     A                                      JREF(ORDHIST)\n",
        mtype="LF", name="ORDBOTH",
    )
    by_name = {fl.name: fl for fl in f.records[0].fields}
    assert by_name["AMOUNT"].ref_file == "ORDERS"
    assert by_name["HAMOUNT"].renamed_from == "AMOUNT"
    assert by_name["HAMOUNT"].ref_field == "AMOUNT"
    assert by_name["HAMOUNT"].ref_file == "ORDHIST"


def test_concat_fields():
    f = _parse(
        "     A          R CREC                      PFILE(CUSTMAST)\n"
        "     A            FULLNM                    CONCAT(FIRST LAST)\n",
        mtype="LF", name="CONCLF",
    )
    fld = f.records[0].fields[0]
    assert fld.concat_fields == ["FIRST", "LAST"]


def test_non_dds_member_returns_none():
    assert _parse("just some text\nnot dds at all\n") is None


def test_parse_all_writes_field_edges(parsed):
    rows = parsed.execute(
        "SELECT field_name, ref_field, ref_file FROM parsed_dds_fields "
        "WHERE file = 'CUSTLF1' ORDER BY field_name").fetchall()
    assert ("CNAME", "CUSTNAME", "CUSTMAST") in rows
    assert ("CUSTNO", "CUSTNO", "CUSTMAST") in rows
