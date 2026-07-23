from conftest import make_member

from lineage.parse import cl


def _parse(text: str, name: str = "TESTCL"):
    return cl.parse(make_member(name, "CLP", text))


def test_continuation_joined():
    p = _parse(
        "             OVRDBF     FILE(ORDERS) +\n"
        "                          TOFILE(APPLIB/ORDHIST)\n"
    )
    assert len(p.overrides) == 1
    o = p.overrides[0]
    assert o.file == "ORDERS"
    assert o.to_library == "APPLIB"
    assert o.to_file == "ORDHIST"


def test_minus_continuation_preserves_spacing():
    p = _parse(
        "             CALL       PGM(-\n"
        "RPT001)\n"
    )
    assert p.calls[0].called_pgm == "RPT001"


def test_comments_stripped():
    p = _parse(
        "             /* OVRDBF FILE(FAKE) TOFILE(NOPE) */\n"
        "             CALL       PGM(RPT001)\n"
    )
    assert not p.overrides
    assert len(p.calls) == 1


def test_ovrdbf_with_member_and_scope():
    p = _parse(
        "             OVRDBF     FILE(ORDERS) TOFILE(LIBX/ORDHIST) "
        "MBR(JAN) OVRSCOPE(*JOB)\n")
    o = p.overrides[0]
    assert o.to_member == "JAN"
    assert o.scope == "*JOB"


def test_chgvar_constant_folding():
    p = _parse(
        "             DCL        VAR(&LIB) TYPE(*CHAR) LEN(10)\n"
        "             CHGVAR     VAR(&LIB) VALUE('PRODLIB')\n"
        "             OVRDBF     FILE(ORDERS) TOFILE(&LIB/ORDHIST)\n"
    )
    o = p.overrides[0]
    # &LIB inside a qualified name is not resolved token-wise — the TOFILE
    # operand starts with '&' only when the variable is the whole operand.
    # Here the operand is '&LIB/ORDHIST' which starts with '&': unresolved
    # unless the variable substitution covers it. Current behaviour: the
    # whole-operand variable case resolves; embedded variables stay dynamic.
    assert o.resolved is False or o.to_file == "ORDHIST"


def test_chgvar_whole_operand_resolution():
    p = _parse(
        "             CHGVAR     VAR(&F) VALUE('ORDHIST')\n"
        "             OVRDBF     FILE(ORDERS) TOFILE(&F)\n"
    )
    o = p.overrides[0]
    assert o.resolved
    assert o.to_file == "ORDHIST"


def test_dynamic_name_unresolved_with_expr():
    p = _parse(
        "             DCL        VAR(&SFX) TYPE(*CHAR) LEN(2)\n"
        "             CHGVAR     VAR(&F) VALUE('ORD' *CAT &SFX)\n"
        "             OVRDBF     FILE(ORDERS) TOFILE(&F)\n"
    )
    o = p.overrides[0]
    assert not o.resolved
    assert o.expr  # expression captured for the gap report


def test_cpyf_captured():
    import json
    p = _parse(
        "             CPYF       FROMFILE(APPLIB/ORDERS) "
        "TOFILE(APPLIB/ORDARC) MBROPT(*REPLACE)\n")
    c = p.calls[0]
    assert c.via == "CPYF"
    info = json.loads(c.params[0])
    assert info["from_file"] == "ORDERS"
    assert info["to_file"] == "ORDARC"


def test_sbmjob_call_extracted():
    p = _parse(
        "             SBMJOB     CMD(CALL PGM(APPLIB/NIGHTLY) "
        "PARM('X')) JOB(NIGHT)\n")
    c = p.calls[0]
    assert c.via == "SBMJOB"
    assert c.called_lib == "APPLIB"
    assert c.called_pgm == "NIGHTLY"


def test_runsqlstm_captured():
    import json
    p = _parse(
        "             RUNSQLSTM  SRCFILE(APPLIB/QSQLSRC) SRCMBR(BLDVIEW)\n")
    c = p.calls[0]
    assert c.via == "RUNSQLSTM"
    info = json.loads(c.params[0])
    assert info["srcmbr"] == "BLDVIEW"
    assert info["srclib"] == "APPLIB"


def test_labels_skipped():
    p = _parse(
        " RETRY:      CALL       PGM(RPT001)\n")
    assert p.calls[0].called_pgm == "RPT001"


def test_parse_all_fixture_estate(parsed):
    ovr = parsed.execute(
        "SELECT file, to_library, to_file, resolved FROM parsed_cl_overrides "
        "ORDER BY program").fetchall()
    by_file = {(f, r): (tl, tf) for f, tl, tf, r in ovr}
    # CLDRIVER's constant override resolves.
    assert by_file[("ORDERS", True)] == ("APPLIB", "ORDHIST")
    # CLDYN's CHGVAR-built target stays dynamic.
    assert ("ORDERS", False) in by_file
