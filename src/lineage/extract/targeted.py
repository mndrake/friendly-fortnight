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
round, reason) — the auditable record of *why* each object was downloaded.
This module never widens what full mode would pull; it only ever narrows.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config
from .connection import HostSession
from .hostinfo import HostProfile

MAX_ROUNDS = 5


def _add_prefixed(counts: dict[str, int], prefix: str, sub: dict[str, int]) -> None:
    for k, v in sub.items():
        counts[f"{prefix}.{k}"] = counts.get(f"{prefix}.{k}", 0) + v


class _Slice:
    """Slice membership, keyed by (kind, name) — matching the member-name
    convention used for source retrieval. First round/reason wins; a later
    ``add`` with a resolved library fills in a library discovered as None
    earlier (e.g. an unqualified CL CALL target already in the slice from
    the backward walk with its library known).
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], dict] = {}

    def add(self, kind: str, library: Optional[str], name: str, round_: int,
            reason: str) -> bool:
        if not name:
            return False
        key = (kind, name.strip().upper())
        lib = library.upper() if library else None
        existing = self._entries.get(key)
        if existing is not None:
            if existing["library"] is None and lib is not None:
                existing["library"] = lib
            return False
        self._entries[key] = {"library": lib, "round": round_, "reason": reason}
        return True

    def names(self) -> set[str]:
        return {name for _, name in self._entries}

    def of_kind(self, kind: str) -> set[tuple[Optional[str], str]]:
        return {(e["library"], name) for (k, name), e in self._entries.items()
                if k == kind}

    def rows(self) -> list[tuple]:
        return [(kind, e["library"], name, e["round"], e["reason"])
                for (kind, name), e in self._entries.items()]

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
                     profile: HostProfile | None = None) -> dict[str, int]:
    from ..db import insert_rows
    from ..graph.build import build_graph
    from ..graph.resolve import backward_lineage
    from . import catalog, xref
    from .source import enumerate_members, retrieve_member

    counts: dict[str, int] = {}

    # -- 1. Seed pass: cheap, broad -----------------------------------------
    _add_prefixed(counts, "catalog", catalog.harvest(
        session, con, config, profile,
        only={"SYSCOLUMNS": [], "SYSPARTITIONSTAT": []}))
    _add_prefixed(counts, "xref", xref.harvest_pgmref(session, con, config))

    sl = _Slice()
    for seed in config.output_seeds:
        sl.add("file", seed.library, seed.file, 0, "seed")

    # -- 2. Slice computation: backward walk + transitive callers -----------
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
    member_cache: dict[str, list[dict]] = {}   # srcfile -> enumerate_members()
    fetched_members: set[str] = set()          # member names already retrieved
    n_members_retrieved = 0
    rounds_run = 0
    seen_files: set[tuple[str, str]] = set()   # (lib, name) already round-processed

    for round_no in range(1, MAX_ROUNDS + 1):
        new_files = [(lib, name) for lib, name in sl.of_kind("file")
                    if (lib, name) not in seen_files and lib]
        # Files with no resolvable library can't be scoped on the host; they
        # stay in the slice (for member-name matching) but are never pulled.
        for lib, name in sl.of_kind("file"):
            if not lib:
                seen_files.add((lib, name))
        for pair in new_files:
            seen_files.add(pair)

        did_anything = bool(new_files)
        if new_files:
            _add_prefixed(counts, "catalog", catalog.harvest(
                session, con, config, profile,
                only={"SYSCOLUMNS": new_files, "SYSPARTITIONSTAT": new_files}))
            _add_prefixed(counts, "xref", xref.harvest_ffd(
                session, con, config, files=new_files))
            dbr_counts = xref.harvest_dbr(session, con, config, files=new_files)
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

        # Member fetch/parse: any slice object (program/file/member) whose
        # name matches a not-yet-retrieved member of a configured source file.
        pending_names = sl.names() - fetched_members
        newly_fetched: list[tuple[str, str, str, str | None]] = []  # lib, srcfile, member, type
        if pending_names:
            for src in config.source_files:
                key = f"{src.library}/{src.file}"
                if key not in member_cache:
                    member_cache[key] = enumerate_members(session, src, profile)
                for m in member_cache[key]:
                    mname = (m.get("member") or "").upper()
                    if mname in pending_names and mname not in fetched_members:
                        newly_fetched.append(
                            (src.library, src.file, mname, m.get("member_type")))
                        fetched_members.add(mname)

        for lib, srcfile, member, mtype in newly_fetched:
            did_anything = True
            src_ref = _find_source_ref(config, lib, srcfile)
            lines, _strategy = retrieve_member(session, src_ref, member, config,
                                               profile)
            rows = [(lib, srcfile, member, mtype, seq, text) for seq, text in lines]
            n_members_retrieved += insert_rows(
                con, "raw_source_members",
                ["library", "srcfile", "member", "member_type", "seq", "line_text"],
                rows)
            new_names = _discover_names(lib, srcfile, member, mtype, lines, con,
                                        config)
            for kind, nlib, nname, reason in new_names:
                if sl.add(kind, nlib, nname, round_no, reason):
                    did_anything = True

        rounds_run = round_no
        if not did_anything:
            break

    insert_rows(con, "slice_objects",
                ["kind", "library", "name", "round", "reason"], sl.rows())

    counts["slice.rounds"] = rounds_run
    counts["slice.programs"] = len(sl.of_kind("program"))
    counts["slice.files"] = len(sl.of_kind("file"))
    counts["slice.members_retrieved"] = n_members_retrieved
    counts["slice.total_objects"] = len(sl)
    return counts


def _find_source_ref(config: Config, library: str, srcfile: str):
    for src in config.source_files:
        if src.library.upper() == library.upper() and src.file.upper() == srcfile.upper():
            return src
    # Fallback: shouldn't happen (srcfile came from config.source_files).
    from ..config import SourceFileRef
    return SourceFileRef(library=library, file=srcfile)


def _discover_names(library: str, srcfile: str, member: str,
                    member_type: str | None, lines: list[tuple[int, str]],
                    con, config: Config) -> list[tuple[str, Optional[str], str, str]]:
    """Parse one freshly fetched member in memory; return new slice entries
    as (kind, library, name, reason). Never writes to the database — the
    normal ``lineage parse`` stage does that over ``raw_source_members``.
    """
    from ..parse.base import SourceMember
    m = SourceMember(library=library, srcfile=srcfile, member=member,
                     member_type=member_type,
                     lines=[text for _, text in lines])
    out: list[tuple[str, Optional[str], str, str]] = []

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
        for blk in prog.sql_blocks:
            out.extend(_sql_table_names(blk, con, config))

    elif member_type and member_type.upper() in {"SQL"}:
        from ..parse.embedded_sql import split_sql_script
        for stmt in split_sql_script(m.text):
            out.extend(_sql_table_names(stmt, con, config))

    return out


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
