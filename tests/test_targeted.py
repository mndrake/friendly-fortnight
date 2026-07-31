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
            "min_confidence, relation FROM output_lineage "
            "ORDER BY 1, 2, 3, 4, 5, 6"
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

def _syscolumns_rows(*files: tuple[str, str]) -> QueryResult:
    """Raw-shaped SYSCOLUMNS response giving each (lib, file) one column —
    enough for the catalog-presence check that gates per-file DSP commands
    (a real database file always has catalog columns on the live host)."""
    return QueryResult(
        columns=["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"],
        rows=[(lib, f, f, "FLD1", "FLD1", 1, "DECIMAL", 9, 0, "N", "FLD1")
              for lib, f in files])


def _otherlib_responses() -> dict[str, QueryResult]:
    responses: dict[str, QueryResult] = {}
    for tag in ("catalog.systables", "catalog.sysviews", "catalog.sysviewdep",
               "catalog.syspartitionstat"):
        responses[tag] = QueryResult(columns=["a"], rows=[])
    responses["catalog.syscolumns"] = _syscolumns_rows(
        ("TESTLIB", "OUT1"), ("OTHERLIB", "EXTFILE"))
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


# --- Robustness: failures on discovered source locations must not abort -------

def test_discovered_enumeration_failure_degrades_gracefully():
    """objstat points WRITER's source at a file the host cannot enumerate or
    read (no fixture responses at all) — the run must complete, count the
    failure, and leave WRITER as a missing-source case rather than crash."""
    from lineage.extract import targeted

    session = _synthetic_session(["WRITER"], extra_responses={
        "objstat.TESTLIB.WRITER.pgm": QueryResult(
            columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"],
            rows=[("TESTLIB", "QRPGSRC2", "WRITERSRC")]),
    })
    con = dbmod.connect(None)
    counts = targeted.harvest_targeted(session, con, _synthetic_config())

    assert counts.get("slice.enumeration_failures", 0) >= 1
    members = {r[0] for r in con.execute(
        "SELECT DISTINCT member FROM raw_source_members").fetchall()}
    assert "WRITERSRC" not in members   # unreadable, skipped — not fatal
    con.close()


def test_mid_round_discovery_waits_for_objstat_no_decoy_fetch():
    """A program discovered mid-round (HELPER, via WRITER's CALL) must be
    objstat-probed before name-matching: its recorded member is HELPERSRC,
    and a same-named decoy member HELPER exists in the same source file. Only
    the recorded member may be fetched."""
    from lineage.extract import targeted

    helper_src_text = ["PGM", "ENDPGM"]
    decoy_text = ["PGM", "/* DECOY - MUST NOT BE FETCHED */", "ENDPGM"]
    session = _synthetic_session(["WRITER"], extra_responses={
        # WRITER's member reveals the call to HELPER.
        "source.text.TESTLIB.QCLSRC.WRITER": QueryResult(
            columns=["SRCSEQ", "SRCDTA"],
            rows=[(1, "PGM"), (2, "CALL PGM(TESTLIB/HELPER)"), (3, "ENDPGM")]),
        # Enumeration lists both the decoy and the recorded member.
        "source.members.TESTLIB.QCLSRC": QueryResult(
            columns=["member", "member_type"],
            rows=[("WRITER", "CLP"), ("HELPER", "CLP"), ("HELPERSRC", "CLP")]),
        "source.text.TESTLIB.QCLSRC.HELPER": QueryResult(
            columns=["SRCSEQ", "SRCDTA"],
            rows=[(i + 1, ln) for i, ln in enumerate(decoy_text)]),
        "source.text.TESTLIB.QCLSRC.HELPERSRC": QueryResult(
            columns=["SRCSEQ", "SRCDTA"],
            rows=[(i + 1, ln) for i, ln in enumerate(helper_src_text)]),
        "objstat.TESTLIB.HELPER.pgm": QueryResult(
            columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"],
            rows=[("TESTLIB", "QCLSRC", "HELPERSRC")]),
    })
    con = dbmod.connect(None)
    targeted.harvest_targeted(session, con, _synthetic_config())

    members = {r[0] for r in con.execute(
        "SELECT DISTINCT member FROM raw_source_members").fetchall()}
    assert "HELPERSRC" in members       # the recorded member
    assert "HELPER" not in members      # the decoy stayed unfetched
    src_ref = con.execute(
        "SELECT source_ref FROM slice_objects WHERE kind='program' AND "
        "name='HELPER'").fetchone()[0]
    assert src_ref == "TESTLIB/QCLSRC(HELPERSRC)"
    con.close()


# --- Per-file failure tolerance: phantom libraries must not kill a run --------

class _PhantomLibSession(FixtureHostSession):
    """Fixture session whose CL commands fail (CPF3064) for given libraries —
    the live-host failure mode where slice discovery names a library that
    does not actually exist on the system."""

    def __init__(self, responses, fail_libs):
        super().__init__(responses=responses)
        self._fail_libs = set(fail_libs)

    def run_cl(self, command: str) -> None:
        super().run_cl(command)
        for lib in self._fail_libs:
            if f"({lib}/" in command:
                from lineage.extract.connection import HostError
                raise HostError(f"[CPF3064] Library {lib} not found.")


def _ffd_result(rows) -> QueryResult:
    return QueryResult(
        columns=["file_lib", "file_name", "record_format", "field_name",
                 "field_type", "field_length", "field_scale", "field_text",
                 "field_ordinal"], rows=rows)


def test_perfile_ffd_skips_phantom_library_and_caches_it():
    """One CPF3064 marks the library dead: its remaining files are counted
    as failures without further host calls, and good files still harvest."""
    from lineage.extract import xref

    session = _PhantomLibSession(
        {"xref.dspffd": _ffd_result([
            ("GOODLIB", "F1", "REC", "FLD1", "A", "10", None, "t", "1"),
            ("GOODLIB", "F4", "REC", "FLD4", "A", "10", None, "t", "1")])},
        fail_libs={"IMA91G001"})
    con = dbmod.connect(None)
    files = [("GOODLIB", "F1"), ("IMA91G001", "F2"),
             ("IMA91G001", "F3"), ("GOODLIB", "F4")]
    counts = xref.harvest_ffd(session, con, _synthetic_config(), files=files)

    assert counts["raw_dspffd"] == 2
    assert counts["raw_dspffd_failures"] == 2
    # The dead-library cache: exactly one attempted command against the
    # phantom library, not one per file.
    assert sum(1 for c in session.cl_log if "IMA91G001" in c) == 1
    got = set(con.execute(
        "SELECT file_lib, file_name FROM raw_dspffd").fetchall())
    assert got == {("GOODLIB", "F1"), ("GOODLIB", "F4")}
    con.close()


def test_perfile_dbr_generic_failure_does_not_condemn_the_library():
    """A non-CPF3064 per-file failure is skipped and counted, but the same
    library's other files are still attempted."""
    from lineage.extract import xref
    from lineage.extract.connection import HostError

    class _OneBadFile(FixtureHostSession):
        def run_cl(self, command: str) -> None:
            super().run_cl(command)
            if "(LIB1/BADF)" in command:
                raise HostError("[CPF9860] Some other per-file error.")

    session = _OneBadFile(responses={"xref.dspdbr": QueryResult(
        columns=["dep_lib", "dep_file", "based_lib", "based_file", "dep_type"],
        rows=[("LIB1", "GOODF", "LIB1", "BASE", "D")])})
    con = dbmod.connect(None)
    counts = xref.harvest_dbr(session, con, _synthetic_config(),
                              files=[("LIB1", "BADF"), ("LIB1", "GOODF")])

    assert counts["raw_dspdbr_failures"] == 1
    assert counts["raw_dspdbr"] == 1
    assert any("(LIB1/GOODF)" in c for c in session.cl_log)
    con.close()


def test_targeted_survives_phantom_discovered_library():
    """End-to-end regression for the live CPF3064 crash: a slice-discovered
    library that doesn't exist must not abort harvest_targeted — failures
    are counted, the audit trail keeps the file, and the run completes."""
    from lineage.extract import targeted

    session = _PhantomLibSession(_otherlib_responses(),
                                 fail_libs={"OTHERLIB"})
    con = dbmod.connect(None)
    counts = targeted.harvest_targeted(session, con, _synthetic_config())

    assert counts.get("xref.raw_dspffd_failures", 0) >= 1
    assert counts.get("xref.raw_dspdbr_failures", 0) >= 1
    sl = _slice_rows(con)
    assert ("file", "OTHERLIB", "EXTFILE") in sl   # audited despite failure
    con.close()


# --- Progress reporting -------------------------------------------------------

def test_targeted_emits_progress_phases(config, session):
    from lineage.extract import hostinfo, targeted
    from lineage.extract.progress import Progress

    con = dbmod.connect(None)
    profile = hostinfo.probe(session)
    out: list[str] = []
    targeted.harvest_targeted(session, con, config, profile,
                              progress=Progress(echo=out.append))
    text = "\n".join(out)
    assert "== seed pass" in text
    assert "== slice computation" in text
    assert "== round 1" in text
    assert "DSPPGMREF APPLIB/*ALL" in text
    assert any(ln.strip().startswith("round 1:") for ln in out)
    con.close()


def test_targeted_without_progress_is_unchanged(config, session):
    """The progress parameter defaults to a no-op — same counts either way."""
    import conftest as conftest_mod
    from lineage.extract import hostinfo, targeted
    from lineage.extract.progress import Progress

    con1 = dbmod.connect(None)
    profile = hostinfo.probe(session)
    c1 = targeted.harvest_targeted(session, con1, config, profile)
    con2 = dbmod.connect(None)
    session2 = conftest_mod.build_session()
    c2 = targeted.harvest_targeted(session2, con2, config,
                                   hostinfo.probe(session2),
                                   progress=Progress(echo=lambda s: None))
    assert c1 == c2
    con1.close()
    con2.close()


def test_full_mode_progress_lines(config, session, con):
    from lineage.extract import hostinfo, source, xref
    from lineage.extract.progress import Progress

    out: list[str] = []
    prog = Progress(echo=out.append)
    profile = hostinfo.probe(session)
    xref.harvest(session, con, config, progress=prog)
    source.harvest(session, con, config, profile, progress=prog)
    text = "\n".join(out)
    assert "DSPPGMREF APPLIB/*ALL ..." in text
    assert "DSPFFD APPLIB/*ALL: done in" in text
    assert "source APPLIB/QCLSRC: done in" in text


# --- objstat batching: one library scan instead of a call per object ----------

def test_objstat_bulk_scan_replaces_per_object_probes():
    """25 slice programs in one library (>= OBJSTAT_BULK_THRESHOLD) must be
    resolved by a single library-wide OBJECT_STATISTICS scan — the live run
    was paying one JDBC round trip per object, a thousand-plus calls."""
    from lineage.extract import targeted

    caller_rows = [("TESTLIB", "WRITER", "TESTLIB", "OUT1", "F", "2", 1)]
    caller_rows += [("TESTLIB", f"P{i:02d}", "TESTLIB", "WRITER", "PGM", "1", 1)
                    for i in range(1, 25)]   # 24 callers + WRITER = 25 programs
    bulk_rows = [("WRITER", "TESTLIB", "QCLSRC", "WRITER")]
    bulk_rows += [(f"P{i:02d}", None, None, None) for i in range(1, 25)]
    session = _synthetic_session(["WRITER"], extra_responses={
        "xref.dsppgmref": QueryResult(
            columns=["program_lib", "program_name", "object_lib",
                     "object_name", "object_type", "usage_flag", "ref_count"],
            rows=caller_rows),
        "objstat.TESTLIB.pgm.bulk": QueryResult(
            columns=["OBJNAME", "SOURCE_LIBRARY", "SOURCE_FILE",
                     "SOURCE_MEMBER"],
            rows=bulk_rows),
    })
    con = dbmod.connect(None)
    counts = targeted.harvest_targeted(session, con, _synthetic_config())

    bulk_queries = [q for q in session.sql_log
                    if "OBJECT_STATISTICS('TESTLIB', '*PGM')" in q]
    per_object_pgm = [q for q in session.sql_log
                      if "'*PGM', '" in q]
    assert len(bulk_queries) == 1        # one scan, cached across rounds
    assert per_object_pgm == []          # no per-object program probes at all
    assert counts["slice.objstat_bulk_scans"] >= 1
    assert counts["slice.objstat_lookups"] >= 25
    # The scan's hit still drives member retrieval as before.
    members = {r[0] for r in con.execute(
        "SELECT DISTINCT member FROM raw_source_members").fetchall()}
    assert "WRITER" in members
    src_ref = con.execute(
        "SELECT source_ref FROM slice_objects WHERE kind='program' AND "
        "name='WRITER'").fetchone()[0]
    assert src_ref == "TESTLIB/QCLSRC(WRITER)"
    con.close()


def test_objstat_small_slice_keeps_per_object_probes(monkeypatch):
    """Below the threshold the per-object probe (cheap, targeted) is kept —
    a full scan of a huge library for two objects would be the regression."""
    from lineage.extract import targeted

    session = _synthetic_session(["WRITER"])
    con = dbmod.connect(None)
    counts = targeted.harvest_targeted(session, con, _synthetic_config())

    assert not any(".bulk" in q or "'*PGM')" in q for q in session.sql_log
                   if "OBJECT_STATISTICS" in q)
    assert "slice.objstat_bulk_scans" not in counts
    con.close()


def test_objstat_bulk_scan_failure_falls_back_to_per_object(monkeypatch):
    """A failed library scan (no fixture tag) must degrade to per-object
    probing, not lose objstat discovery entirely."""
    from lineage.extract import targeted

    monkeypatch.setattr(targeted, "OBJSTAT_BULK_THRESHOLD", 1)
    session = _synthetic_session(["WRITER"], extra_responses={
        # No .bulk tag anywhere -> bulk scan raises -> cached as failed.
        "objstat.TESTLIB.WRITER.pgm": QueryResult(
            columns=["SOURCE_LIBRARY", "SOURCE_FILE", "SOURCE_MEMBER"],
            rows=[("TESTLIB", "QCLSRC", "WRITER")]),
    })
    con = dbmod.connect(None)
    targeted.harvest_targeted(session, con, _synthetic_config())

    src_ref = con.execute(
        "SELECT source_ref FROM slice_objects WHERE kind='program' AND "
        "name='WRITER'").fetchone()[0]
    assert src_ref == "TESTLIB/QCLSRC(WRITER)"   # per-object probe still won
    con.close()


# --- Regression: library-less slice entries must not crash the objstat pass ---

def test_libraryless_entry_does_not_crash_objstat_grouping():
    """WRITER's CL calls an unqualified HELPER: the slice then holds
    (None, 'HELPER') next to ('TESTLIB', 'WRITER'), and the objstat
    grouping pass sorts that mix — a plain sorted() raised TypeError
    ('<' between NoneType and str) and killed a 3-hour live run."""
    from lineage.extract import targeted

    session = _synthetic_session(["WRITER"], extra_responses={
        "source.text.TESTLIB.QCLSRC.WRITER": QueryResult(
            columns=["SRCSEQ", "SRCDTA"],
            rows=[(1, "PGM"), (2, "CALL PGM(HELPER)"), (3, "ENDPGM")]),
    })
    con = dbmod.connect(None)
    counts = targeted.harvest_targeted(session, con, _synthetic_config())

    sl = _slice_rows(con)
    assert ("program", None, "HELPER") in sl    # placeholder survived intact
    assert counts["slice.rounds"] >= 2          # the pass after discovery ran
    con.close()


# --- Resume: a second pass over the same store refetches nothing --------------

def test_targeted_resume_reuses_members_without_host_fetches(config, session):
    import conftest as conftest_mod
    from lineage.extract import hostinfo, targeted

    con = dbmod.connect(None)
    profile = hostinfo.probe(session)
    first = targeted.harvest_targeted(session, con, config, profile)
    assert first["slice.members_retrieved"] > 0

    session2 = conftest_mod.build_session()
    second = targeted.harvest_targeted(session2, con, config, profile)
    # Same slice, zero member fetches: every member came from the store.
    assert second["slice.members_retrieved"] == 0
    assert second["slice.members_reused"] == first["slice.members_retrieved"]
    assert not any("SRCSEQ" in q or "IFS_READ" in q for q in session2.sql_log)
    # No re-issued DSPPGMREF either — the library's rows are already present.
    assert not any(c.startswith("DSPPGMREF") for c in session2.cl_log)
    con.close()


def test_unscoped_catalog_pull_is_idempotent(config, session):
    import conftest as conftest_mod
    from lineage import db as dbmod2
    from lineage.extract import catalog, hostinfo

    con = dbmod.connect(None)
    profile = hostinfo.probe(session)
    catalog.harvest(session, con, config, profile)
    before = dbmod2.table_count(con, "raw_systables")
    session2 = conftest_mod.build_session()
    second = catalog.harvest(session2, con, config, profile)
    assert dbmod2.table_count(con, "raw_systables") == before
    assert second["raw_systables"] == 0
    con.close()


# --- Catalog-presence gate: doomed per-file DSP commands are never issued -----

def test_targeted_skips_files_absent_from_catalog():
    """A slice file with no catalog columns (device file, phantom library,
    deleted object) gets no DSPFFD/DSPDBR at all — the live estate was
    paying a failed host call per pair (CPF3064/CPF3010 spam)."""
    from lineage.extract import targeted

    session = _synthetic_session(["WRITER"])   # syscolumns fixture is empty
    con = dbmod.connect(None)
    counts = targeted.harvest_targeted(session, con, _synthetic_config())

    assert not any(c.startswith(("DSPFFD", "DSPDBR")) for c in session.cl_log)
    assert counts["slice.nondb_files_skipped"] >= 1
    sl = _slice_rows(con)
    assert ("file", "TESTLIB", "OUT1") in sl   # still audited, still sliced
    con.close()


def test_scoped_catalog_pull_matches_system_names():
    """DSPPGMREF/RPG reference DDL tables by 10-char system names; the
    scoped SYSCOLUMNS pull must match SYSTEM_TABLE_NAME too, or every
    long-named DDL table silently loses its columns."""
    from lineage import db as dbmod2
    from lineage.extract import catalog

    session = FixtureHostSession(responses={
        "catalog.syscolumns": QueryResult(
            columns=["table_schema", "table_name", "system_name",
                     "column_name", "system_column", "ordinal", "data_type",
                     "length", "numeric_scale", "is_nullable",
                     "column_heading"],
            rows=[("APPLIB", "CUSTOMER_REPORT", "CUSTRP0001", "CUSTNO",
                   "CUSTNO", 1, "DECIMAL", 9, 0, "N", "CUSTNO")]),
    })
    con = dbmod.connect(None)
    config = _synthetic_config()
    only = {"SYSCOLUMNS": [("APPLIB", "CUSTRP0001")],
            "SYSPARTITIONSTAT": [], "SYSTABLES": [], "SYSVIEWS": [],
            "SYSVIEWDEP": []}
    counts = catalog.harvest(session, con, config, only=only)

    assert counts["raw_syscolumns"] == 1     # matched via system name
    assert any("SYSTEM_TABLE_NAME = 'CUSTRP0001'" in q
               for q in session.sql_log)
    # Idempotent under the system name too: the second call must recognise
    # the stored long-name rows as covering the system-named pair.
    session2 = FixtureHostSession(responses=session._responses)
    counts2 = catalog.harvest(session2, con, config, only=only)
    assert counts2["raw_syscolumns"] == 0
    assert dbmod2.table_count(con, "raw_syscolumns") == 1
    con.close()


def test_rounds_scope_pull_systables_for_slice_pairs():
    """Output tables live in data libraries outside config.libraries; the
    rounds must pull their SYSTABLES rows scoped by pair, or the table has
    no catalog identity (live symptom: column-trace 'Table not found in
    raw_systables' for a DDL output)."""
    from lineage.extract import targeted

    session = _synthetic_session(["WRITER"])   # seed systables pull is empty
    con = dbmod.connect(None)
    targeted.harvest_targeted(session, con, _synthetic_config())

    assert any("QSYS2.SYSTABLES" in q and "OUT1" in q
               for q in session.sql_log), \
        "no pair-scoped SYSTABLES pull was issued for the slice"
    assert any("QSYS2.SYSVIEWS" in q and "OUT1" in q
               for q in session.sql_log)
    con.close()


def test_scoped_systables_pull_inserts_discovered_table():
    from lineage.extract import catalog

    session = FixtureHostSession(responses={
        "catalog.systables": QueryResult(
            columns=["table_schema", "table_name", "system_name",
                     "table_type", "file_type", "row_count", "long_comment"],
            rows=[("TNTACCDTA", "BROAST", "BROAST", "T", "D", 100, None)]),
    })
    con = dbmod.connect(None)
    counts = catalog.harvest(
        session, con, _synthetic_config(),
        only={"SYSTABLES": [("TNTACCDTA", "BROAST")], "SYSCOLUMNS": [],
              "SYSPARTITIONSTAT": [], "SYSVIEWS": [], "SYSVIEWDEP": []})
    assert counts["raw_systables"] == 1
    row = con.execute(
        "SELECT table_schema, table_name FROM raw_systables").fetchone()
    assert row == ("TNTACCDTA", "BROAST")
    con.close()


# --- Long SQL names: CL commands must use 10-char system names -----------------

def test_dsp_commands_use_system_name_for_long_sql_names():
    """A slice file known by its long SQL name (from parsed DDL) must be
    DSPFFD/DSPDBR'd by its 10-char system name — the long name fails with
    CPF0006 'wrong length' (live: DSPDBR TNTIN1DTA/DIVIDEND_EXCHANGE_...)."""
    from lineage.extract import targeted

    responses = _otherlib_responses()
    responses["catalog.syscolumns"] = _syscolumns_rows(("TESTLIB", "OUT1"))
    responses["catalog.syscolumns"] = QueryResult(
        columns=["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"],
        rows=[("TESTLIB", "OUT1", "OUT1", "F", "F", 1,
               "DECIMAL", 9, 0, "N", "F"),
              ("TESTLIB", "DIVIDEND_EXCHANGE_RATE_PSEUDO", "DIVEXRP",
               "F2", "F2", 1, "DECIMAL", 9, 0, "N", "F2")])
    session = FixtureHostSession(responses=responses)
    con = dbmod.connect(None)
    import dataclasses

    from lineage.config import OutputSeed
    cfg = _synthetic_config()
    cfg = dataclasses.replace(cfg, output_seeds=tuple(
        list(cfg.output_seeds)
        + [OutputSeed(id="LONGT", library="TESTLIB",
                      file="DIVIDEND_EXCHANGE_RATE_PSEUDO")]))
    targeted.harvest_targeted(session, con, cfg)

    assert any("DSPFFD FILE(TESTLIB/DIVEXRP)" in c for c in session.cl_log)
    assert not any("DIVIDEND_EXCHANGE" in c for c in session.cl_log)
    con.close()


def test_long_name_without_system_name_is_skipped_not_attempted():
    from lineage.extract import targeted

    responses = _otherlib_responses()
    responses["catalog.syscolumns"] = QueryResult(
        columns=["table_schema", "table_name", "system_name", "column_name",
                 "system_column", "ordinal", "data_type", "length",
                 "numeric_scale", "is_nullable", "column_heading"],
        rows=[("TESTLIB", "OUT1", "OUT1", "F", "F", 1,
               "DECIMAL", 9, 0, "N", "F"),
              ("TESTLIB", "AN_UNMAPPED_LONG_TABLE_NAME", None,
               "F2", "F2", 1, "DECIMAL", 9, 0, "N", "F2")])
    session = FixtureHostSession(responses=responses)
    con = dbmod.connect(None)
    import dataclasses

    from lineage.config import OutputSeed
    cfg = _synthetic_config()
    cfg = dataclasses.replace(cfg, output_seeds=tuple(
        list(cfg.output_seeds)
        + [OutputSeed(id="LONGU", library="TESTLIB",
                      file="AN_UNMAPPED_LONG_TABLE_NAME")]))
    counts = targeted.harvest_targeted(session, con, cfg)

    assert not any("AN_UNMAPPED" in c for c in session.cl_log)
    assert counts["slice.unaddressable_files_skipped"] >= 1
    con.close()


def test_perfile_failure_notes_are_throttled():
    """Hundreds of identical per-file failures must not flood the console:
    a few notes, then one suppression line — the host log has the rest."""
    from lineage.extract import xref
    from lineage.extract.connection import HostError
    from lineage.extract.progress import Progress

    class _AllFail(FixtureHostSession):
        def run_cl(self, command: str) -> None:
            super().run_cl(command)
            raise HostError("[CPF0006] Errors occurred in command.")

    session = _AllFail(responses={})
    con = dbmod.connect(None)
    out: list[str] = []
    files = [("LIB1", f"F{i:03d}") for i in range(10)]
    counts = xref.harvest_dbr(session, con, _synthetic_config(), files=files,
                              progress=Progress(echo=out.append))
    assert counts["raw_dspdbr_failures"] == 10
    failure_notes = [ln for ln in out if "failed" in ln]
    assert len(failure_notes) == xref._MAX_FAILURE_NOTES
    assert sum("suppressed" in ln for ln in out) == 1
    con.close()


# --- Incomplete catalog self-description must not kill system-name matching ----

def _profile_without_systables_sysname():
    from lineage.extract.hostinfo import HostProfile

    return HostProfile(catalog_columns={
        "SYSTABLES": {"TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE"},
        "SYSCOLUMNS": {"TABLE_SCHEMA", "TABLE_NAME", "SYSTEM_TABLE_NAME",
                       "COLUMN_NAME", "SYSTEM_COLUMN_NAME",
                       "ORDINAL_POSITION"},
    })


def test_scoped_pull_speculates_system_name_despite_profile():
    """The live probe under-reported SYSTABLES' columns; the scoped pull must
    still try SYSTEM_TABLE_NAME (documented) — and genuinely SELECT it, so
    the row filter can accept system-name matches."""
    from lineage.extract import catalog

    session = FixtureHostSession(responses={
        "catalog.systables": QueryResult(
            columns=["table_schema", "table_name", "system_name",
                     "table_type", "file_type", "row_count", "long_comment"],
            rows=[("TNTACCDTA", "EDS_BROKER_BARGAIN_EVENING", "BROAST",
                   "T", "D", 1, None)]),
    })
    con = dbmod.connect(None)
    counts = catalog.harvest(
        session, con, _synthetic_config(),
        profile=_profile_without_systables_sysname(),
        only={"SYSTABLES": [("TNTACCDTA", "BROAST")], "SYSCOLUMNS": [],
              "SYSPARTITIONSTAT": [], "SYSVIEWS": [], "SYSVIEWDEP": []})

    assert counts["raw_systables"] == 1
    sql = next(q for q in session.sql_log if "SYSTABLES" in q)
    assert "SYSTEM_TABLE_NAME = 'BROAST'" in sql       # speculative OR-clause
    assert "SYSTEM_TABLE_NAME AS system_name" in sql   # ...and SELECTed
    con.close()


def test_speculative_system_name_retries_without_on_host_rejection():
    """If the host genuinely lacks the column (SQL0206), the pull retries
    without it instead of failing the extraction."""
    from lineage.extract import catalog
    from lineage.extract.connection import HostError

    class _RejectsSysname(FixtureHostSession):
        def query(self, sql, params=()):
            if "SYSTEM_TABLE_NAME" in sql:
                self.sql_log.append(sql)
                self._last_tag = None
                raise HostError("[SQL0206] SYSTEM_TABLE_NAME not valid.")
            return super().query(sql)

    session = _RejectsSysname(responses={
        "catalog.systables": QueryResult(
            columns=["table_schema", "table_name", "system_name",
                     "table_type", "file_type", "row_count", "long_comment"],
            rows=[("TNTACCDTA", "BROAST", None, "T", "D", 1, None)]),
    })
    con = dbmod.connect(None)
    counts = catalog.harvest(
        session, con, _synthetic_config(),
        profile=_profile_without_systables_sysname(),
        only={"SYSTABLES": [("TNTACCDTA", "BROAST")], "SYSCOLUMNS": [],
              "SYSPARTITIONSTAT": [], "SYSVIEWS": [], "SYSVIEWDEP": []})

    assert counts["raw_systables"] == 1    # matched by plain TABLE_NAME
    assert any("SYSTEM_TABLE_NAME" in q for q in session.sql_log)   # tried
    assert any("SYSTEM_TABLE_NAME" not in q and "SYSTABLES" in q
               for q in session.sql_log)                            # retried
    con.close()
