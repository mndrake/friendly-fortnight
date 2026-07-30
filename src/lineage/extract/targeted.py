"""Targeted (slice-scoped) extraction.

Full extraction (``extraction_scope: full``, the default) pulls every
configured library/source file in full — the safe, simple default. On a
large estate that is prohibitively slow (measured on a real estate:
SYSCOLUMNS ~893k rows, SYSPARTITIONSTAT full scan ~64s, ~16k source
members). Targeted extraction instead:

1. Runs a cheap, broad "seed pass": ``DSPPGMREF *ALL`` per library (how
   writers are found — unavoidable) plus unfiltered SYSTABLES/SYSVIEWS/
   SYSVIEWDEP (all measured trivial).
2. Computes the *slice*: every node reached by a backward walk
   (:func:`lineage.graph.resolve.backward_lineage`) from each configured
   output seed over the coarse (phase-1) graph, plus the transitive callers
   of every slice program (a CL driver that calls a slice program carries
   OVRDBF context even though it never itself writes the output).
3. Iterates to closure (capped at 5 rounds): for every file newly in the
   slice, scope SYSCOLUMNS/SYSPARTITIONSTAT to it and pull its DSPFFD/DSPDBR
   individually (DSPDBR's based-on files extend the slice); for every
   program/file newly in the slice, retrieve the source member whose name
   matches it (member name == object name convention) and parse it in
   memory (never written to the database — the normal ``lineage parse``
   stage still runs over ``raw_source_members`` afterward) to discover new
   names: CL CALL/SBMJOB targets, OVRDBF TOFILE targets, CPYF from/to files,
   RUNSQLSTM members, RPG file references (incl. EXTNAME), RPG ``/COPY``
   members, and embedded-SQL tables read/written. New names extend the
   slice and the loop continues; it stops when a round adds nothing, or at
   the round cap.

Every slice addition is recorded in ``slice_objects`` (kind, library, name,
round, reason, source_ref) — the auditable record of *why* each object was
downloaded and, when discovered, *where* its source actually lives.

Two source-discovery mechanisms feed the member-fetch step:

* **objstat discovery** (:mod:`lineage.extract.objinfo`) — each slice
  program/file with a resolved library is asked, via
  ``QSYS2.OBJECT_STATISTICS``, where its source actually lives. This is
  authoritative and immune to member-name != object-name mismatches, so
  ``source_files`` in config becomes optional for targeted mode (still used,
  plus any discovered source files, for the fallback below).
* **Name-matching fallback** — for objects objstat could not place (no
  library to look up, no hit, or a probe failure) and for ``member``-kind
  entries (``/COPY``, ``RUNSQLSTM``, which are name-only), the object's name
  is matched against the enumerated members of the *dynamic* source-file
  list: configured ``source_files`` plus any ``SourceFileRef`` discovered via
  an objstat hit or a qualified ``/COPY`` directive.

``library_discovery: slice`` (the default) additionally lets per-file pulls
(DSPFFD/DSPDBR, scoped catalog SELECTs) follow the slice into libraries
outside the configured `libraries` scan list; ``library_discovery: none``
restores the strictly-configured restriction. Broad ``*ALL`` commands
(DSPPGMREF) never leave ``config.libraries`` in either mode — this module
never widens what full mode would pull; it only ever narrows.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, SourceFileRef
from .connection import HostSession
from .hostinfo import HostProfile
from .progress import NULL, Progress

MAX_ROUNDS = 5


def _add_prefixed(counts: dict[str, int], prefix: str, sub: dict[str, int]) -> None:
    for k, v in sub.items():
        counts[f"{prefix}.{k}"] = counts.get(f"{prefix}.{k}", 0) + v


class _Slice:
    """Slice membership, keyed by (kind, library, name).

    Two same-named objects in *different* libraries are distinct slice
    entries — each gets its own host pulls. A library-less entry (unqualified
    reference that could not be resolved) is a placeholder: a later ``add``
    of the same (kind, name) with a resolved library *upgrades* it in place
    (and reports a change, so the round loop knows new pulls are pending);
    conversely a library-less ``add`` is satisfied by any existing entry of
    that (kind, name). First round/reason wins on upgrades.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, Optional[str], str], dict] = {}

    def add(self, kind: str, library: Optional[str], name: str, round_: int,
            reason: str) -> bool:
        """Returns True when the slice changed (new entry or upgraded lib)."""
        if not name:
            return False
        name = name.strip().upper()
        lib = library.upper() if library else None
        if (kind, lib, name) in self._entries:
            return False
        same_name = [k for k in self._entries if k[0] == kind and k[2] == name]
        if lib is None:
            # Unqualified reference: any existing entry of this name covers it.
            if same_name:
                return False
        else:
            placeholder = (kind, None, name)
            if placeholder in self._entries:
                entry = self._entries.pop(placeholder)
                entry["library"] = lib
                self._entries[(kind, lib, name)] = entry
                return True
        self._entries[(kind, lib, name)] = {
            "library": lib, "round": round_, "reason": reason}
        return True

    def set_source_ref(self, kind: str, library: Optional[str], name: str,
                       source_ref: str) -> None:
        """Record where an entry's source was found (objstat discovery)."""
        lib = library.upper() if library else None
        key = (kind, lib, name.strip().upper())
        if key in self._entries:
            self._entries[key]["source_ref"] = source_ref

    def names(self) -> set[str]:
        return {name for _, _, name in self._entries}

    def all(self) -> list[tuple[str, Optional[str], str]]:
        return list(self._entries.keys())

    def of_kind(self, kind: str) -> set[tuple[Optional[str], str]]:
        return {(lib, name) for (k, lib, name) in self._entries if k == kind}

    def rows(self) -> list[tuple]:
        return [(kind, lib, name, e["round"], e["reason"], e.get("source_ref"))
                for (kind, lib, name), e in self._entries.items()]

    def __len__(self) -> int:
        return len(self._entries)


def _parse_node_id(node_id: str) -> Optional[tuple[str, Optional[str], str]]:
    """``program:LIB/NAME`` | ``file:LIB/NAME`` | ``file:LIB/NAME(MBR)`` ->
    (kind, library, name); member suffix is stripped (the base file is what
    drives host pulls). Returns None for node kinds the slice doesn't track
    (columns, views resolved as files are still 'file').
    """
    if ":" not in node_id:
        return None
    kind, rest = node_id.split(":", 1)
    if kind not in {"program", "file"}:
        return None
    if "/" not in rest:
        return None
    lib, name = rest.split("/", 1)
    name = name.split("(")[0]
    lib = None if lib.upper() == "*LIBL" else lib
    return kind, lib, name.upper()


def _resolve_unqualified(con, config: Config, name: str) -> Optional[str]:
    """Best-effort library resolution for an unqualified (*LIBL) name against
    the catalog already loaded (raw_systables) and the default liblist.
    Returns None when it cannot be resolved — the caller then keeps the
    object library-less (matched by name only for member retrieval).
    """
    name = name.upper()
    for lib in config.liblist(None):
        row = con.execute(
            "SELECT 1 FROM raw_systables WHERE upper(table_schema) = ? AND "
            "(upper(table_name) = ? OR upper(system_name) = ?) LIMIT 1",
            [lib.upper(), name, name]).fetchone()
        if row:
            return lib
    return None


def harvest_targeted(session: HostSession, con, config: Config,
                     profile: HostProfile | None = None,
                     progress: Progress | None = None) -> dict[str, int]:
    from ..db import insert_rows
    from ..graph.build import build_graph
    from ..graph.resolve import backward_lineage
    from . import catalog, objinfo, xref
    from .source import enumerate_members, retrieve_member

    p = progress or NULL
    counts: dict[str, int] = {}
    # Idempotency: a repeat call on the same store must not stack audit rows
    # (the CLI resets the whole raw layer first, but direct callers may not).
    con.execute("DELETE FROM slice_objects")

    # -- 1. Seed pass: cheap, broad -----------------------------------------
    p.phase("seed pass")
    _add_prefixed(counts, "catalog", catalog.harvest(
        session, con, config, profile,
        only={"SYSCOLUMNS": [], "SYSPARTITIONSTAT": []}, progress=progress))
    _add_prefixed(counts, "xref", xref.harvest_pgmref(session, con, config,
                                                      progress=progress))

    sl = _Slice()
    for seed in config.output_seeds:
        sl.add("file", seed.library, seed.file, 0, "seed")

    # -- 2. Slice computation: backward walk + transitive callers -----------
    p.phase("slice computation")
    g = build_graph(con, config, phase=1)
    for seed in config.output_seeds:
        for node_id in backward_lineage(g, seed.node_id):
            parsed = _parse_node_id(node_id)
            if parsed is None:
                continue
            kind, lib, name = parsed
            if lib is None:
                lib = _resolve_unqualified(con, config, name)
            sl.add(kind, lib, name, 0, "backward_walk")

    pgmref_calls = con.execute(
        "SELECT program_lib, program_name, object_lib, object_name "
        "FROM raw_dsppgmref WHERE upper(object_type) LIKE '%PGM%'").fetchall()
    changed = True
    while changed:
        changed = False
        pgm_names = {name for lib, name in sl.of_kind("program")}
        for plib, pname, olib, oname in pgmref_calls:
            if not oname or not pname:
                continue
            if oname.upper() in pgm_names and pname.upper() not in pgm_names:
                if sl.add("program", plib, pname, 0, "caller"):
                    changed = True
                    pgm_names.add(pname.upper())

    # -- 3. Iterative rounds: scoped host pulls + member fetch to closure ---
    member_cache: dict[str, list[dict]] = {}   # "LIB/FILE" -> enumerate_members()
    # (source_library, source_file, member) already retrieved this run.
    fetched_members: set[tuple[str, str, str]] = set()
    # (kind, library, name) -> objinfo.source_location() result, once looked
    # up (None = probed, no hit). Absent key = not probed yet.
    src_locations: dict[tuple[str, str, str], Optional[tuple[str, str, str]]] = {}
    n_members_retrieved = 0
    n_source_lines = 0
    rounds_run = 0
    seen_files: set[tuple[str, str]] = set()   # (lib, name) already round-processed
    # Targeted mode must only ever narrow full mode's pulls: broad commands
    # (DSPPGMREF *ALL) never leave config.libraries in either
    # library_discovery mode. Per-file pulls (DSPFFD/DSPDBR/scoped catalog)
    # follow the slice into libraries outside config.libraries when
    # library_discovery == "slice" (default); "none" keeps the file in the
    # slice (audit trail, member-name matching) but never pulls it — same as
    # full mode would (it would not touch that library either), surfacing as
    # an outside_scope gap at graph build.
    allowed_libs = {lib.upper() for lib in config.libraries}
    discover_libs = config.library_discovery == "slice"
    libraries_discovered: set[str] = set()

    # Dynamic source-file list the name-matching fallback searches: the
    # configured source_files plus any SourceFileRef discovered via an
    # objstat hit or a qualified /COPY directive.
    dynamic_source_files: list[SourceFileRef] = list(config.source_files)
    dynamic_keys = {(s.library.upper(), s.file.upper()) for s in dynamic_source_files}
    n_configured_source_files = len(dynamic_source_files)

    def _ensure_source_file(lib: str, file: str) -> None:
        key = (lib.upper(), file.upper())
        if key not in dynamic_keys:
            dynamic_keys.add(key)
            dynamic_source_files.append(SourceFileRef(library=lib, file=file))

    enumeration_failures: set[str] = set()

    def _enumerate_cached(lib: str, file: str) -> list[dict]:
        key = f"{lib.upper()}/{file.upper()}"
        if key not in member_cache:
            try:
                member_cache[key] = enumerate_members(
                    session, SourceFileRef(library=lib, file=file), profile)
            except Exception:  # noqa: BLE001
                # Discovered source files carry no operator guarantee (unlike
                # the configured list): a failed enumeration must not abort
                # the whole extraction. Objects whose only source lived here
                # surface as missing_source gaps at graph build.
                member_cache[key] = []
                enumeration_failures.add(key)
        return member_cache[key]

    def _retrieve_and_discover(lib: str, srcfile: str, member: str,
                               mtype: str | None, round_no: int) -> None:
        nonlocal n_members_retrieved, n_source_lines
        src_ref = SourceFileRef(library=lib, file=srcfile)
        try:
            lines, _strategy = retrieve_member(session, src_ref, member,
                                               config, profile)
        except Exception:  # noqa: BLE001
            # Same rationale as _enumerate_cached: a discovered member that
            # fails to read must not abort the extraction; the object shows
            # up as missing_source at graph build.
            enumeration_failures.add(f"{lib.upper()}/{srcfile.upper()}"
                                     f"({member.upper()})")
            return
        rows = [(lib, srcfile, member, mtype, seq, text) for seq, text in lines]
        n_members_retrieved += 1
        p.tick("members retrieved", n_members_retrieved)
        n_source_lines += insert_rows(
            con, "raw_source_members",
            ["library", "srcfile", "member", "member_type", "seq", "line_text"],
            rows)
        new_names, src_hints = _discover_names(lib, srcfile, member, mtype,
                                               lines, con, config)
        for ref in src_hints:
            _ensure_source_file(ref.library, ref.file)
        for kind, nlib, nname, reason in new_names:
            sl.add(kind, nlib, nname, round_no, reason)

    for round_no in range(1, MAX_ROUNDS + 1):
        p.phase(f"round {round_no}")
        progs_before = len(sl.of_kind("program"))
        files_before = len(sl.of_kind("file"))
        if discover_libs:
            new_files = [(lib, name) for lib, name in sl.of_kind("file")
                        if (lib, name) not in seen_files and lib]
            for lib, name in sl.of_kind("file"):
                if not lib:
                    seen_files.add((lib, name))
        else:
            new_files = [(lib, name) for lib, name in sl.of_kind("file")
                        if (lib, name) not in seen_files
                        and lib and lib.upper() in allowed_libs]
            # Files with no resolvable library, or in an unconfigured
            # library, can't/mustn't be pulled; they stay in the slice (for
            # member-name matching and the audit trail) but are marked
            # processed.
            for lib, name in sl.of_kind("file"):
                if not lib or lib.upper() not in allowed_libs:
                    seen_files.add((lib, name))
        for pair in new_files:
            seen_files.add(pair)
            if pair[0].upper() not in allowed_libs:
                libraries_discovered.add(pair[0].upper())

        did_anything = bool(new_files)
        if new_files:
            _add_prefixed(counts, "catalog", catalog.harvest(
                session, con, config, profile,
                only={"SYSCOLUMNS": new_files, "SYSPARTITIONSTAT": new_files},
                progress=progress))
            _add_prefixed(counts, "xref", xref.harvest_ffd(
                session, con, config, files=new_files, progress=progress))
            dbr_counts = xref.harvest_dbr(session, con, config,
                                          files=new_files, progress=progress)
            _add_prefixed(counts, "xref", dbr_counts)

            based_on = con.execute(
                "SELECT dep_lib, dep_file, based_lib, based_file FROM raw_dspdbr"
            ).fetchall()
            requested = {(lib.upper(), name.upper()) for lib, name in new_files}
            for dlib, dfile, blib, bfile in based_on:
                if not dfile or not bfile:
                    continue
                if ((dlib or "").upper(), (dfile or "").upper()) in requested:
                    if sl.add("file", blib, bfile, round_no, "dspdbr_based_on"):
                        did_anything = True

        # -- Source-location discovery: ask each not-yet-probed slice
        # program/file with a resolved library where its source actually
        # lives (authoritative — immune to member-name != object-name
        # mismatches). On a hit, fetch the *recorded* member (which may
        # differ from the object name) from its recorded source file. -------
        n_probed_this_round = 0
        for kind, obj_type in (("program", "*PGM"), ("file", "*FILE")):
            for lib, name in sl.of_kind(kind):
                if not lib:
                    continue
                key = (kind, lib, name)
                if key in src_locations:
                    continue
                loc = objinfo.source_location(session, lib, name, obj_type)
                src_locations[key] = loc
                n_probed_this_round += 1
                # Rate-only tick: entries discovered mid-round join the work
                # list live, so a fixed denominator would be a lie.
                p.tick("objstat lookups", len(src_locations))
                if loc is None:
                    continue
                srclib, srcfile, srcmbr = loc
                _ensure_source_file(srclib, srcfile)
                sl.set_source_ref(kind, lib, name,
                                  f"{srclib}/{srcfile}({srcmbr})")
                members = _enumerate_cached(srclib, srcfile)
                mtype = None
                for m in members:
                    if (m.get("member") or "").upper() == srcmbr.upper():
                        mtype = m.get("member_type")
                        break
                member_key = (srclib.upper(), srcfile.upper(), srcmbr.upper())
                if member_key not in fetched_members:
                    fetched_members.add(member_key)
                    _retrieve_and_discover(srclib, srcfile, srcmbr, mtype,
                                           round_no)
                    did_anything = True

        # -- Name-matching fallback: for slice objects objstat couldn't
        # place (no resolvable library, no hit, or a probe failure) and for
        # 'member'-kind entries (/COPY, RUNSQLSTM — name-only, never
        # objstat-probed) — match the object's name against the enumerated
        # members of the dynamic source-file list (configured + discovered).
        fallback_names: set[str] = set()
        for kind, lib, name in sl.all():
            if kind == "member":
                fallback_names.add(name)
            elif kind in ("program", "file"):
                if not lib:
                    # No library to probe with — name-matching is the only
                    # possible path.
                    fallback_names.add(name)
                elif (kind, lib, name) in src_locations \
                        and src_locations[(kind, lib, name)] is None:
                    # Probed and missed. An entry discovered mid-round (not
                    # yet probed) must NOT fall through here: it waits for
                    # next round's objstat pass, otherwise a same-named decoy
                    # member could be fetched alongside the recorded one.
                    fallback_names.add(name)
        if fallback_names:
            for src in dynamic_source_files:
                for m in _enumerate_cached(src.library, src.file):
                    mname = (m.get("member") or "").upper()
                    if mname not in fallback_names:
                        continue
                    member_key = (src.library.upper(), src.file.upper(), mname)
                    if member_key in fetched_members:
                        continue
                    fetched_members.add(member_key)
                    _retrieve_and_discover(src.library, src.file, mname,
                                           m.get("member_type"), round_no)
                    did_anything = True

        rounds_run = round_no
        p.note(f"round {round_no}: +{len(sl.of_kind('program')) - progs_before}"
               f" programs, +{len(sl.of_kind('file')) - files_before} files, "
               f"{n_probed_this_round} objstat probes, "
               f"{n_members_retrieved} members retrieved so far")
        if not did_anything:
            break

    insert_rows(con, "slice_objects",
                ["kind", "library", "name", "round", "reason", "source_ref"],
                sl.rows())

    counts["slice.rounds"] = rounds_run
    counts["slice.programs"] = len(sl.of_kind("program"))
    counts["slice.files"] = len(sl.of_kind("file"))
    counts["slice.members_retrieved"] = n_members_retrieved
    counts["slice.source_lines"] = n_source_lines
    counts["slice.total_objects"] = len(sl)
    counts["slice.source_files_discovered"] = (
        len(dynamic_source_files) - n_configured_source_files)
    counts["slice.libraries_discovered"] = len(libraries_discovered)
    if enumeration_failures:
        counts["slice.enumeration_failures"] = len(enumeration_failures)
    return counts


def _discover_names(library: str, srcfile: str, member: str,
                    member_type: str | None, lines: list[tuple[int, str]],
                    con, config: Config
                    ) -> tuple[list[tuple[str, Optional[str], str, str]],
                              list[SourceFileRef]]:
    """Parse one freshly fetched member in memory; return new slice entries
    as (kind, library, name, reason), plus any source files an explicit
    ``/COPY`` directive names (added to the dynamic source-file list the
    name-matching fallback searches). Never writes to the database — the
    normal ``lineage parse`` stage does that over ``raw_source_members``.
    """
    from ..parse.base import SourceMember
    m = SourceMember(library=library, srcfile=srcfile, member=member,
                     member_type=member_type,
                     lines=[text for _, text in lines])
    out: list[tuple[str, Optional[str], str, str]] = []
    src_hints: list[SourceFileRef] = []

    if m.is_cl():
        from ..parse.cl import parse as parse_cl
        prog = parse_cl(m)
        for call in prog.calls:
            if call.via in {"CALL", "SBMJOB"} and call.resolved and call.called_pgm:
                # Program libraries aren't in raw_systables (that's files/
                # tables); an unqualified CALL target's library is left None
                # here and filled in later if/when the same program is
                # already (or becomes) known with a library elsewhere.
                out.append(("program", call.called_lib, call.called_pgm,
                           "cl_call"))
            elif call.via == "CPYF":
                import json
                try:
                    info = json.loads(call.params[0])
                except (ValueError, IndexError, TypeError):
                    info = {}
                for role in ("from_file", "to_file"):
                    fname = info.get(role)
                    if not fname:
                        continue
                    lib = info.get(role.replace("file", "lib")) or \
                        _resolve_unqualified(con, config, fname)
                    out.append(("file", lib, fname, "cpyf"))
            elif call.via == "RUNSQLSTM":
                import json
                try:
                    info = json.loads(call.params[0])
                except (ValueError, IndexError, TypeError):
                    info = {}
                srcmbr = info.get("srcmbr")
                if srcmbr:
                    out.append(("member", None, srcmbr, "runsqlstm"))
        for ovr in prog.overrides:
            if ovr.resolved and ovr.to_file:
                lib = ovr.to_library or _resolve_unqualified(
                    con, config, ovr.to_file)
                out.append(("file", lib, ovr.to_file, "cl_override_target"))

    elif m.is_rpg():
        from ..parse.rpg import parse as parse_rpg
        prog = parse_rpg(m)
        for f in prog.files:
            fname = (f.extname or f.file)
            if fname:
                lib = _resolve_unqualified(con, config, fname)
                out.append(("file", lib, fname, "rpg_file"))
        for c in prog.copies:
            out.append(("member", None, c.member, "copy_member"))
            # Explicit lib/srcfile on the /COPY directive resolves in order:
            # this (added to the dynamic set) -> source files already
            # discovered -> configured source_files (both covered by the
            # dynamic set the fallback searches).
            if c.library and c.srcfile:
                src_hints.append(SourceFileRef(library=c.library, file=c.srcfile))
        for blk in prog.sql_blocks:
            out.extend(_sql_table_names(blk, con, config))

    elif member_type and member_type.upper() in {"SQL"}:
        from ..parse.embedded_sql import split_sql_script
        for stmt in split_sql_script(m.text):
            out.extend(_sql_table_names(stmt, con, config))

    return out, src_hints


def _sql_table_names(sql: str, con, config: Config
                     ) -> list[tuple[str, Optional[str], str, str]]:
    from ..parse.embedded_sql import analyze_statement
    a = analyze_statement(sql)
    out: list[tuple[str, Optional[str], str, str]] = []
    for tname in list(a.tables_read) + list(a.tables_written):
        if "/" in tname:
            lib, name = tname.split("/", 1)
        else:
            lib, name = _resolve_unqualified(con, config, tname), tname
        out.append(("file", lib, name, "sql_table"))
    return out
