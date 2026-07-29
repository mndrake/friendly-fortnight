"""Targeted (slice-scoped) extraction tests.

Covers slice computation over the shared fixture estate (conftest.py),
equivalence with full extraction, per-file DSP command shape, the pairs_filter
chunking helper, and closure/iteration/cap behavior over small synthetic
estates built directly in this file (a CL call chain not present in the
fixture estate).
"""
from __future__ import annotations

from lineage import db as dbmod
from lineage.config import from_dict
from lineage.extract.connection import FixtureHostSession, QueryResult

LIB = "APPLIB"


def _slice_rows(con):
    return {(kind, lib, name): (round_, reason) for kind, lib, name, round_, reason
            in con.execute(
                "SELECT kind, library, name, round, reason FROM slice_objects"
            ).fetchall()}


# --- Slice computation over the fixture estate -------------------------------

def test_slice_excludes_pgmdesc_and_ghost(targeted_extracted):
    sl = _slice_rows(targeted_extracted)
    names = {name for _, _, name in sl}
    assert "PGMDESC" not in names
    assert "GHOST" not in names


def test_slice_includes_callers_and_transitive_files(targeted_extracted):
    sl = _slice_rows(targeted_extracted)
    programs = {name for kind, _, name in sl if kind == "program"}
    files = {name for kind, _, name in sl if kind == "file"}
    assert programs == {"RPT001", "RPT002", "SQLEXT", "CLDRIVER", "CLDYN"}
    assert files >= {"CUSTRPT", "ORDEXT", "ORDSUM", "ORDERS", "ORDHIST",
                     "CUSTLF1", "CUSTMAST", "ORDARC"}
    # Callers of slice programs, not writers/readers themselves.
    assert sl[("program", LIB, "CLDRIVER")][1] == "caller"
    assert sl[("program", LIB, "CLDYN")][1] == "caller"


def test_slice_reasons_trace_discovery_mechanism(targeted_extracted):
    sl = _slice_rows(targeted_extracted)
    assert sl[("file", LIB, "CUSTMAST")][1] == "dspdbr_based_on"
    assert sl[("file", LIB, "ORDARC")][1] == "cpyf"
    assert sl[("file", LIB, "ORDHIST")][1] == "cl_override_target"
    for seed_file in ("CUSTRPT", "ORDEXT", "ORDSUM"):
        assert sl[("file", LIB, seed_file)][1] == "seed"
    for backward in ("ORDERS", "CUSTLF1"):
        assert sl[("file", LIB, backward)][1] == "backward_walk"
    for pgm in ("RPT001", "RPT002", "SQLEXT"):
        assert sl[("program", LIB, pgm)][1] == "backward_walk"


def test_slice_objects_rounds_are_ordered(targeted_extracted):
    sl = _slice_rows(targeted_extracted)
    # Seed/backward-walk/caller entries land in round 0; discoveries made
    # from parsing sliced source members land in later rounds.
    assert sl[("file", LIB, "CUSTRPT")][0] == 0
    assert sl[("program", LIB, "RPT001")][0] == 0
    assert sl[("file", LIB, "ORDARC")][0] >= 1
    assert sl[("file", LIB, "CUSTMAST")][0] >= 1


def test_targeted_retrieves_no_pgmdesc_source(targeted_extracted):
    members = {r[0] for r in targeted_extracted.execute(
        "SELECT DISTINCT member FROM raw_source_members").fetchall()}
    assert "PGMDESC" not in members
    # But every slice program/file with a source member is present.
    assert {"RPT001", "RPT002", "SQLEXT", "CLDRIVER", "CLDYN",
           "CUSTMAST", "ORDERS", "CUSTLF1", "ORDHIST"} <= members


# --- Per-file DSP command shape -----------------------------------------------

def test_targeted_issues_per_file_dsp_commands(targeted_extracted, session):
    cl_log = session.cl_log
    assert any("DSPFFD FILE(APPLIB/ORDERS)" in c for c in cl_log)
    assert any("DSPDBR FILE(APPLIB/ORDERS)" in c for c in cl_log)
    # Never a whole-library *ALL DSPFFD/DSPDBR in targeted mode.
    assert not any("DSPFFD FILE(APPLIB/*ALL)" in c for c in cl_log)
    assert not any("DSPDBR FILE(APPLIB/*ALL)" in c for c in cl_log)
    # DSPPGMREF remains the one broad per-library pull, even in targeted mode.
    assert any("DSPPGMREF PGM(APPLIB/*ALL)" in c for c in cl_log)


def test_full_mode_still_issues_all(extracted, session):
    cl_log = session.cl_log
    assert any("DSPFFD FILE(APPLIB/*ALL)" in c for c in cl_log)
    assert any("DSPDBR FILE(APPLIB/*ALL)" in c for c in cl_log)


# --- Equivalence: full vs targeted output_lineage -----------------------------

def test_full_and_targeted_output_lineage_match(config):
    """Golden guarantee: targeted extraction must never change the answer."""
    import conftest as conftest_mod
    from lineage.analyze.lineage import compute_output_lineage
    from lineage.extract import catalog, hostinfo, source, targeted, xref
    from lineage.graph.build import build_graph
    from lineage.parse import cl, classify, dds, embedded_sql, rpg

    def run_pipeline(mode: str):
        session = conftest_mod.build_session()
        con = dbmod.connect(None)
        profile = hostinfo.probe(session)
        profile.save(con)
        if mode == "full":
            catalog.harvest(session, con, config, profile)
            xref.harvest(session, con, config)
            source.harvest(session, con, config, profile)
        else:
            targeted.harvest_targeted(session, con, config, profile)
        dds.parse_all(con)
        cl.parse_all(con)
        rpg.parse_all(con)
        embedded_sql.parse_all(con)
        classify.classify_all(con)
        g = build_graph(con, config, phase=3)
        compute_output_lineage(con, g, config)
        rows = con.execute(
            "SELECT output_id, source_file, source_column, path_len, "
            "min_confidence FROM output_lineage ORDER BY 1, 2, 3, 4, 5"
        ).fetchall()
        con.close()
        return rows

    full_rows = run_pipeline("full")
    targeted_rows = run_pipeline("targeted")
    assert full_rows  # sanity: the fixture estate actually resolves something
    assert full_rows == targeted_rows


# --- pairs_filter chunking -----------------------------------------------------

def test_pairs_filter_chunks_over_500():
    from lineage.extract.catalog import pairs_filter

    pairs = [(f"LIB{i}", f"NAME{i}") for i in range(1200)]
    fragments = pairs_filter("TABLE_SCHEMA", "TABLE_NAME", pairs, chunk=500)
    assert len(fragments) == 3
    assert all(f.startswith("(") and f.endswith(")") for f in fragments)
    assert "LIB0" in fragments[0] and "NAME0" in fragments[0]
    assert "LIB1199" in fragments[2]


def test_pairs_filter_single_chunk_under_limit():
    from lineage.extract.catalog import pairs_filter

    pairs = [("LIB", "A"), ("LIB", "B")]
    fragments = pairs_filter("TABLE_SCHEMA", "TABLE_NAME", pairs, chunk=500)
    assert len(fragments) == 1


# --- Full-mode regression: multi-library command order ------------------------

def test_full_mode_command_order_per_library(con, session):
    """Full-mode xref.harvest must interleave PGMREF -> FFD -> DBR per
    library (the original pre-refactor order), not group commands by verb."""
    from lineage.extract import xref

    config2 = from_dict({
        "scratch_lib": "QTEMP",
        "libraries": [LIB, "APPLIB2"],
        "output_seeds": [{"id": "X", "library": LIB, "file": "CUSTRPT"}],
        "liblists": {"default": [LIB]},
    })
    xref.harvest(session, con, config2)
    verbs = [(c.split()[0], c.split("(")[1].split("/")[0])
             for c in session.cl_log]
    assert verbs == [
        ("DSPPGMREF", "APPLIB"), ("DSPFFD", "APPLIB"), ("DSPDBR", "APPLIB"),
        ("DSPPGMREF", "APPLIB2"), ("DSPFFD", "APPLIB2"), ("DSPDBR", "APPLIB2"),
    ]


# --- Slice identity: same name, different libraries ----------------------------

def test_slice_distinguishes_same_name_across_libraries():
    from lineage.extract.targeted import _Slice

    sl = _Slice()
    assert sl.add("file", "LIB1", "DUPNAME", 0, "seed")
    assert sl.add("file", "LIB2", "DUPNAME", 1, "cpyf")
    assert sl.of_kind("file") == {("LIB1", "DUPNAME"), ("LIB2", "DUPNAME")}
    assert len(sl) == 2


def test_slice_placeholder_upgrade_reports_change():
    from lineage.extract.targeted import _Slice

    sl = _Slice()
    assert sl.add("program", None, "PGMX", 0, "cl_call")
    # Resolving the library later is a change (its pulls are now pending)...
    assert sl.add("program", "LIB1", "PGMX", 1, "backward_walk")
    assert sl.of_kind("program") == {("LIB1", "PGMX")}
    # ...but the original round/reason are preserved.
    assert dict(zip(("kind", "lib", "name", "round", "reason"),
                    sl.rows()[0]))["reason"] == "cl_call"
    # A later unqualified mention is satisfied by the existing entry.
    assert not sl.add("program", None, "PGMX", 2, "cl_call")


# --- Targeted mode never touches unconfigured libraries ------------------------

def _otherlib_responses() -> dict[str, QueryResult]:
    responses: dict[str, QueryResult] = {}
    for tag in ("catalog.systables", "catalog.sysviews", "catalog.sysviewdep",
               "catalog.syscolumns", "catalog.syspartitionstat"):
        responses[tag] = QueryResult(columns=["a"], rows=[])
    responses["xref.dsppgmref"] = QueryResult(
        columns=["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
        rows=[("TESTLIB", "WRITER", "TESTLIB", "OUT1", "F", "2", 1)],
    )
    responses["xref.dspffd"] = QueryResult(
        columns=["file_lib", "file_name", "record_format", "field_name",
                 "field_type", "field_length", "field_scale", "field_text",
                 "field_ordinal"], rows=[])
    responses["xref.dspdbr"] = QueryResult(
        columns=["dep_lib", "dep_file", "based_lib", "based_file", "dep_type"],
        rows=[])
    # objstat: WRITER/OUT1/EXTFILE all resolve empty -- this estate relies
    # purely on the name-matching fallback and CPYF discovery.
    for tag in ("objstat.TESTLIB.WRITER.pgm", "objstat.TESTLIB.OUT1.file",
               "objstat.OTHERLIB.EXTFILE.file"):
        responses[tag] = QueryResult(
            columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"],
            rows=[])
    responses["source.members.TESTLIB.QCLSRC"] = QueryResult(
        columns=["member", "member_type"], rows=[("WRITER", "CLP")])
    responses["source.text.TESTLIB.QCLSRC.WRITER"] = QueryResult(
        columns=["SRCSEQ", "SRCDTA"],
        rows=[(1, "PGM"),
              (2, "CPYF FROMFILE(OTHERLIB/EXTFILE) TOFILE(TESTLIB/OUT1)"),
              (3, "ENDPGM")])
    return responses


def test_targeted_never_pulls_outside_configured_libraries():
    """With library_discovery: none, a CPYF referencing OTHERLIB (not in
    config.libraries) must not cause DSPFFD/DSPDBR against OTHERLIB -- full
    mode would never touch it either. The file still enters the slice for
    the audit trail."""
    from lineage.extract import targeted

    session = FixtureHostSession(responses=_otherlib_responses())
    con = dbmod.connect(None)
    counts = targeted.harvest_targeted(
        session, con, _synthetic_config(library_discovery="none"))

    assert not any("OTHERLIB" in c for c in session.cl_log)
    sl = _slice_rows(con)
    assert ("file", "OTHERLIB", "EXTFILE") in sl   # audited, never pulled
    con.close()


def test_targeted_slice_discovery_pulls_otherlib_per_file():
    """Default library_discovery: slice follows the slice into OTHERLIB with
    per-file pulls only -- never a broad *ALL or DSPPGMREF for it."""
    from lineage.extract import targeted

    session = FixtureHostSession(responses=_otherlib_responses())
    con = dbmod.connect(None)
    counts = targeted.harvest_targeted(session, con, _synthetic_config())

    assert any("DSPFFD FILE(OTHERLIB/EXTFILE)" in c for c in session.cl_log)
    assert any("DSPDBR FILE(OTHERLIB/EXTFILE)" in c for c in session.cl_log)
    assert not any("OTHERLIB/*ALL" in c for c in session.cl_log)
    assert not any(c.startswith("DSPPGMREF") and "OTHERLIB" in c
                  for c in session.cl_log)
    sl = _slice_rows(con)
    assert ("file", "OTHERLIB", "EXTFILE") in sl
    assert counts["slice.libraries_discovered"] >= 1
    con.close()


# --- Idempotency of the scoped pulls without an external reset ------------------

def test_harvest_targeted_twice_does_not_duplicate(config, session):
    import conftest as conftest_mod
    from lineage.extract import hostinfo, targeted

    con = dbmod.connect(None)
    profile = hostinfo.probe(session)
    targeted.harvest_targeted(session, con, config, profile)
    tables = ("raw_syscolumns", "raw_syspartitionstat", "raw_dspffd",
              "raw_dspdbr", "slice_objects")
    before = {t: dbmod.table_count(con, t) for t in tables}
    # Fresh session (fixture tag state), same store, no raw-layer reset.
    session2 = conftest_mod.build_session()
    targeted.harvest_targeted(session2, con, config, profile)
    after = {t: dbmod.table_count(con, t) for t in tables}
    assert after == before
    con.close()


# --- Synthetic estate: iteration/closure and the round cap --------------------

def _cl_member(*lines: str) -> str:
    return "\n".join(lines)


def _synthetic_session(chain: list[str],
                       extra_responses: dict[str, QueryResult] | None = None
                       ) -> FixtureHostSession:
    """A CL call chain WRITER -> P1 -> P2 -> ... ; WRITER writes OUT1 via a
    compiled xref reference (so it's the backward-walk seed writer); every
    other hop in the chain is discoverable *only* by parsing CL source --
    exactly the "revealed by a fetched member" case targeted extraction must
    iterate to reach. No objstat.* tags are seeded by default, so
    objinfo.source_location() always misses (missing fixture tag -> caught
    exception -> None) and every member is found via name-matching;
    ``extra_responses`` lets a caller override/add specific tags (e.g. an
    explicit empty objstat response) without needing to rebuild the estate.
    """
    responses: dict[str, QueryResult] = {}
    empty_cols = ["A"]
    for tag in ("catalog.systables", "catalog.sysviews", "catalog.sysviewdep",
               "catalog.syscolumns", "catalog.syspartitionstat"):
        responses[tag] = QueryResult(columns=empty_cols, rows=[])

    responses["xref.dsppgmref"] = QueryResult(
        columns=["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
        rows=[("TESTLIB", "WRITER", "TESTLIB", "OUT1", "F", "2", 1)],
    )
    responses["xref.dspffd"] = QueryResult(
        columns=["file_lib", "file_name", "record_format", "field_name",
                 "field_type", "field_length", "field_scale", "field_text",
                 "field_ordinal"],
        rows=[],
    )
    responses["xref.dspdbr"] = QueryResult(
        columns=["dep_lib", "dep_file", "based_lib", "based_file", "dep_type"],
        rows=[],
    )

    members = []
    for i, name in enumerate(chain):
        nxt = chain[i + 1] if i + 1 < len(chain) else None
        text = _cl_member(
            "PGM",
            f"CALL PGM(TESTLIB/{nxt})" if nxt else "NOOP:",
            "ENDPGM",
        )
        members.append((name, text))

    responses["source.members.TESTLIB.QCLSRC"] = QueryResult(
        columns=["member", "member_type"],
        rows=[(name, "CLP") for name, _ in members],
    )
    for name, text in members:
        lines = text.splitlines()
        responses[f"source.text.TESTLIB.QCLSRC.{name}"] = QueryResult(
            columns=["SRCSEQ", "SRCDTA"],
            rows=[(i + 1, ln) for i, ln in enumerate(lines)],
        )

    responses.update(extra_responses or {})
    return FixtureHostSession(responses=responses)


def _synthetic_config(library_discovery: str = "slice"):
    return from_dict({
        "scratch_lib": "QTEMP",
        "libraries": ["TESTLIB"],
        "source_files": [{"library": "TESTLIB", "file": "QCLSRC"}],
        "output_seeds": [{"id": "OUT", "library": "TESTLIB", "file": "OUT1"}],
        "liblists": {"default": ["TESTLIB"]},
        "library_discovery": library_discovery,
    })


def test_iteration_discovers_call_across_rounds():
    """WRITER's member (fetched round 1) reveals CALL HELPER; HELPER is only
    fetched in round 2. Closure terminates well under the round cap."""
    from lineage.extract import targeted

    con = dbmod.connect(None)
    session = _synthetic_session(["WRITER", "HELPER"])
    config = _synthetic_config()
    counts = targeted.harvest_targeted(session, con, config)

    sl = _slice_rows(con)
    assert ("program", "TESTLIB", "HELPER") in sl
    round_added, reason = sl[("program", "TESTLIB", "HELPER")]
    assert reason == "cl_call"
    assert round_added == 1   # discovered while parsing WRITER's round-1 fetch

    members = {r[0] for r in con.execute(
        "SELECT DISTINCT member FROM raw_source_members").fetchall()}
    assert {"WRITER", "HELPER"} <= members
    assert counts["slice.rounds"] > 1   # genuine iteration, not just the seed pass
    con.close()


def test_cycle_and_chain_cap_is_honored(monkeypatch):
    """A long CL call chain that would need more rounds than the cap allows:
    only the first ``MAX_ROUNDS`` hops are discovered."""
    from lineage.extract import targeted

    monkeypatch.setattr(targeted, "MAX_ROUNDS", 2)
    chain = ["WRITER", "P1", "P2", "P3", "P4", "P5"]
    con = dbmod.connect(None)
    session = _synthetic_session(chain)
    config = _synthetic_config()
    counts = targeted.harvest_targeted(session, con, config)

    sl = _slice_rows(con)
    programs = {name for kind, _, name in sl if kind == "program"}
    # WRITER (round 0) -> P1 (round 1, from WRITER's fetch) -> P2 (round 2,
    # from P1's fetch). The cap stops the loop there.
    assert {"WRITER", "P1", "P2"} <= programs
    assert "P3" not in programs
    assert "P4" not in programs
    assert counts["slice.rounds"] == 2
    con.close()


def test_cycle_does_not_loop_forever():
    """A direct call cycle (A calls B, B calls A) must not hang or grow the
    slice unboundedly — closure recognizes both names are already sliced."""
    from lineage.extract import targeted

    responses: dict[str, QueryResult] = {}
    for tag in ("catalog.systables", "catalog.sysviews", "catalog.sysviewdep",
               "catalog.syscolumns", "catalog.syspartitionstat"):
        responses[tag] = QueryResult(columns=["a"], rows=[])
    responses["xref.dsppgmref"] = QueryResult(
        columns=["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
        rows=[("TESTLIB", "A", "TESTLIB", "OUT1", "F", "2", 1)],
    )
    responses["xref.dspffd"] = QueryResult(
        columns=["file_lib", "file_name", "record_format", "field_name",
                 "field_type", "field_length", "field_scale", "field_text",
                 "field_ordinal"],
        rows=[],
    )
    responses["xref.dspdbr"] = QueryResult(
        columns=["dep_lib", "dep_file", "based_lib", "based_file", "dep_type"],
        rows=[],
    )
    responses["source.members.TESTLIB.QCLSRC"] = QueryResult(
        columns=["member", "member_type"], rows=[("A", "CLP"), ("B", "CLP")])
    responses["source.text.TESTLIB.QCLSRC.A"] = QueryResult(
        columns=["SRCSEQ", "SRCDTA"],
        rows=[(1, "PGM"), (2, "CALL PGM(TESTLIB/B)"), (3, "ENDPGM")])
    responses["source.text.TESTLIB.QCLSRC.B"] = QueryResult(
        columns=["SRCSEQ", "SRCDTA"],
        rows=[(1, "PGM"), (2, "CALL PGM(TESTLIB/A)"), (3, "ENDPGM")])
    session = FixtureHostSession(responses=responses)
    con = dbmod.connect(None)
    config = _synthetic_config()

    counts = targeted.harvest_targeted(session, con, config)
    sl = _slice_rows(con)
    programs = {name for kind, _, name in sl if kind == "program"}
    assert programs == {"A", "B"}
    assert counts["slice.rounds"] < targeted.MAX_ROUNDS
    con.close()


# --- objstat-driven discovery: member name != object name ---------------------

def test_objstat_discovers_member_name_mismatch():
    """WRITER's *recorded* source member is WRITERSRC, not WRITER -- objstat
    discovery must fetch WRITERSRC even though plain name-matching (member
    name == object name) would never find it."""
    from lineage.extract import targeted

    responses: dict[str, QueryResult] = {}
    for tag in ("catalog.systables", "catalog.sysviews", "catalog.sysviewdep",
               "catalog.syscolumns", "catalog.syspartitionstat"):
        responses[tag] = QueryResult(columns=["a"], rows=[])
    responses["xref.dsppgmref"] = QueryResult(
        columns=["program_lib", "program_name", "object_lib", "object_name",
                 "object_type", "usage_flag", "ref_count"],
        rows=[("TESTLIB", "WRITER", "TESTLIB", "OUT1", "F", "2", 1)],
    )
    responses["xref.dspffd"] = QueryResult(
        columns=["file_lib", "file_name", "record_format", "field_name",
                 "field_type", "field_length", "field_scale", "field_text",
                 "field_ordinal"], rows=[])
    responses["xref.dspdbr"] = QueryResult(
        columns=["dep_lib", "dep_file", "based_lib", "based_file", "dep_type"],
        rows=[])
    responses["objstat.TESTLIB.WRITER.pgm"] = QueryResult(
        columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"],
        rows=[("TESTLIB", "QCLSRC", "WRITERSRC")])
    responses["objstat.TESTLIB.OUT1.file"] = QueryResult(
        columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"], rows=[])
    # Enumeration lists the *recorded* member name, WRITERSRC -- WRITER
    # itself is never a member in this estate.
    responses["source.members.TESTLIB.QCLSRC"] = QueryResult(
        columns=["member", "member_type"], rows=[("WRITERSRC", "CLP")])
    responses["source.text.TESTLIB.QCLSRC.WRITERSRC"] = QueryResult(
        columns=["SRCSEQ", "SRCDTA"],
        rows=[(1, "PGM"), (2, "CALL PGM(TESTLIB/HELPER)"), (3, "ENDPGM")])
    session = FixtureHostSession(responses=responses)
    con = dbmod.connect(None)
    targeted.harvest_targeted(session, con, _synthetic_config())

    rows = con.execute(
        "SELECT library, srcfile, member FROM raw_source_members").fetchall()
    assert ("TESTLIB", "QCLSRC", "WRITERSRC") in rows
    assert not any(r[2] == "WRITER" for r in rows)
    sl = _slice_rows(con)
    assert ("program", "TESTLIB", "WRITER") in sl
    src_ref = con.execute(
        "SELECT source_ref FROM slice_objects WHERE kind='program' AND "
        "name='WRITER'").fetchone()[0]
    assert src_ref == "TESTLIB/QCLSRC(WRITERSRC)"
    con.close()


def test_objstat_empty_response_degrades_to_name_matching():
    """objstat returning zero rows (not an exception) must still fall back
    to plain name-matching for that object, with source_ref left NULL."""
    from lineage.extract import targeted

    empty_objstat = QueryResult(
        columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"], rows=[])
    session = _synthetic_session(["WRITER"], extra_responses={
        "objstat.TESTLIB.WRITER.pgm": empty_objstat,
        "objstat.TESTLIB.OUT1.file": empty_objstat,
    })
    con = dbmod.connect(None)
    targeted.harvest_targeted(session, con, _synthetic_config())

    members = {r[0] for r in con.execute(
        "SELECT DISTINCT member FROM raw_source_members").fetchall()}
    assert "WRITER" in members
    src_ref = con.execute(
        "SELECT source_ref FROM slice_objects WHERE kind='program' AND "
        "name='WRITER'").fetchone()[0]
    assert src_ref is None
    con.close()


# --- source_files becomes optional for targeted mode --------------------------

def test_targeted_discovers_source_files_without_config(session, config):
    """With no source_files configured at all, targeted extraction still
    retrieves every slice member purely via objstat discovery."""
    import dataclasses

    from lineage.extract import hostinfo, targeted

    cfg = dataclasses.replace(config, source_files=())
    con = dbmod.connect(None)
    profile = hostinfo.probe(session)
    counts = targeted.harvest_targeted(session, con, cfg, profile)

    assert counts["slice.source_files_discovered"] > 0
    members = {r[0] for r in con.execute(
        "SELECT DISTINCT member FROM raw_source_members").fetchall()}
    assert {"RPT001", "RPT002", "SQLEXT", "CUSTMAST", "ORDERS"} <= members
    con.close()
