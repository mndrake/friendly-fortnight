"""Graph assembly.

Builds the nodes/edges tables from the raw + parsed layers, reconciling the
two evidence layers (design principle 1): system cross-reference data is
authoritative for compiled references, but source-parsed evidence supersedes
it where OVRDBF redirection or dynamic names apply. Every edge carries
provenance and confidence.

Phases:
* phase 1 — catalog + xref only (coarse, table-level);
* phase 2 — + DDS field lineage, CL calls/CPYF, override resolution, liblist;
* phase 3 — + RPG file refs, embedded SQL (column level), classification merge.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Optional

import networkx as nx

from .model import (Confidence, Edge, EdgeKind, Node, NodeKind, Provenance,
                    column_id, file_id, program_id)
from .resolve import (CallSite, LiblistResolver, Override, simulate_call_tree)


def _col_file_spec(col_node: str) -> str:
    """``column:LIB/FILE.COL`` -> ``LIB/FILE``."""
    body = col_node.split(":", 1)[1]
    return body.rsplit(".", 1)[0]


#: RPG special words that resolve at run time, not from any file — a field
#: assigned from these is a runtime value (extract date/time stamps), and
#: "no resolved lineage" is the CORRECT answer, worth explaining.
_RPG_RUNTIME_VALUES = {"UDATE", "UDAY", "UMONTH", "UYEAR", "UDATE0",
                       "TIME", "TIMDAT"}


class GraphBuilder:
    def __init__(self, con, config, phase: int = 3):
        self.con = con
        self.config = config
        self.phase = phase
        self._dbr_base: dict[tuple[str, str], tuple[str, str]] = {}
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._edge_keys: set = set()
        self.gaps: list[tuple[str, str, str, str]] = []  # kind, object, detail, context
        self._catalog_objects: set[tuple[str, str]] = set()
        self.resolver: Optional[LiblistResolver] = None
        # program name (upper) -> declared file name (upper) -> resolved
        # read-direction targets [(lib, file)], populated by _edges_from_rpg.
        # Reused by the column-usage pass so it doesn't re-simulate overrides.
        self._rpg_read_targets: dict[tuple[str, str], list[tuple[Optional[str], str]]] = \
            defaultdict(list)
        # CPYF events captured while building CL edges, replayed by the
        # column-usage pass (no re-parsing / re-resolving liblist).
        self._cpyf_events: list[dict] = []
        self._ffd_cache: Optional[dict[tuple[str, str], set[str]]] = None
        # Long-SQL-name <-> system-name canonicalization, built from the
        # catalog before any edges are minted. A DDL member writes
        # EDS_BROKER_BARGAIN_EVENING while DSPPGMREF/DSPFFD/RPG know the
        # same table as BROAST — without canonical node names the graph
        # splits into two disconnected tables and lineage never meets.
        self._canon_file: dict[tuple[str, str], str] = {}
        self._canon_col: dict[tuple[str, str, str], str] = {}

    # -- helpers -------------------------------------------------------------

    def add_node(self, node: Node) -> str:
        existing = self.nodes.get(node.id)
        if existing is None or (not existing.attrs and node.attrs):
            self.nodes[node.id] = node
        return node.id

    def add_file_node(self, library: Optional[str], name: str,
                      member: Optional[str] = None, **attrs) -> str:
        if library:
            name = self._canon_file.get(
                (library.upper(), name.strip().upper()), name)
        nid = file_id(library, name, member)
        self.add_node(Node(id=nid, kind=NodeKind.FILE, library=library,
                           name=name.upper(), attrs=attrs))
        if member:
            # A member-qualified node derives from its base file so lineage
            # stays connected (multi-member trap).
            base = self.add_file_node(library, name)
            self.add_edge(Edge(src=nid, dst=base, kind=EdgeKind.DERIVES_FROM,
                               provenance=Provenance.CATALOG,
                               confidence=Confidence.CONFIRMED,
                               context={"member_of": base}))
        return nid

    def add_program_node(self, library: Optional[str], name: str, **attrs) -> str:
        nid = program_id(library, name)
        self.add_node(Node(id=nid, kind=NodeKind.PROGRAM, library=library,
                           name=name.upper(), attrs=attrs))
        return nid

    def add_edge(self, edge: Edge) -> None:
        k = edge.key()
        if k in self._edge_keys:
            return
        self._edge_keys.add(k)
        self.edges.append(edge)

    def _col_id(self, library: Optional[str], table: str, col: str) -> str:
        """column_id with canonical (system) table and column names."""
        lib_u = (library or "").upper()
        table_u = (table or "").strip().upper()
        if library:
            table_u = self._canon_file.get((lib_u, table_u), table_u)
        col_u = (col or "").strip().upper()
        col_u = self._canon_col.get((lib_u, table_u, col_u), col_u)
        return column_id(library, table_u, col_u)

    def gap(self, kind: str, object_id: str, detail: str, context: dict | None = None) -> None:
        self.gaps.append((kind, object_id, detail,
                          json.dumps(context or {}, sort_keys=True)))

    def _resolve_lib(self, name: str, context: str) -> Optional[str]:
        if self.resolver is None:
            return None
        lib, ambiguous = self.resolver.resolve(name, context)
        if ambiguous:
            self.gap("ambiguous_liblist", f"file:*LIBL/{name.upper()}",
                     f"'{name}' found in multiple liblist libraries",
                     {"context": context})
        return lib

    # -- build steps ---------------------------------------------------------

    def build(self) -> nx.MultiDiGraph:
        self._load_catalog()
        self._edges_from_xref()
        self._edges_from_viewdep()
        self._view_column_lineage()
        if self.phase >= 2:
            self._edges_from_dds()
            self._lf_column_passthrough()
            self._overrides = self._simulate_overrides()
            self._edges_from_cl()
            self._apply_overrides_to_xref()
        else:
            self._overrides = {}
        if self.phase >= 3:
            self._edges_from_rpg()
            self._edges_from_sql()
            self._column_usage_edges()
            self._record_format_column_expansion()
        self._detect_missing_source()
        self._persist()
        return self.to_networkx()

    # -- catalog -------------------------------------------------------------

    def _load_catalog(self) -> None:
        rows = self.con.execute(
            "SELECT table_schema, table_name, system_name, table_type "
            "FROM raw_systables").fetchall()
        # Canonical-name maps first — node creation below and every edge
        # pass afterwards mint ids through them.
        for schema, name, sysname, _ttype in rows:
            if not schema or not (name and sysname):
                continue
            lib_u = str(schema).strip().upper()
            canon = str(sysname or name).strip().upper()
            for label in (name, sysname):
                self._canon_file[(lib_u, str(label).strip().upper())] = canon
        for schema, name, sysname, colname, syscol in self.con.execute(
                "SELECT table_schema, table_name, system_name, column_name, "
                "system_column FROM raw_syscolumns").fetchall():
            if not schema:
                continue
            lib_u = str(schema).strip().upper()
            canon = str(sysname or name or "").strip().upper()
            if name and sysname:
                for label in (name, sysname):
                    self._canon_file[(lib_u, str(label).strip().upper())] = canon
            ccanon = str(syscol or colname or "").strip().upper()
            if canon and ccanon and colname and syscol:
                for clabel in (colname, syscol):
                    self._canon_col[
                        (lib_u, canon, str(clabel).strip().upper())] = ccanon
        for schema, name, sysname, ttype in rows:
            schema_s = (schema or "").strip()
            for label in {name, sysname} - {None}:
                self._catalog_objects.add(
                    (schema_s.upper(), str(label).strip().upper()))
            kind_attr = {"table_type": ttype}
            self.add_file_node(schema_s, str(sysname or name).strip(),
                               **kind_attr)
        # raw_syscolumns is the same SQL catalog under another view: a store
        # whose extract scope-pulled SYSCOLUMNS but not SYSTABLES (older
        # runs) must still let the liblist resolver place unqualified
        # references to those tables — identity hinging on SYSTABLES alone
        # turned every such reference into an outside_scope gap.
        for schema, name, sysname in self.con.execute(
                "SELECT DISTINCT table_schema, table_name, system_name "
                "FROM raw_syscolumns").fetchall():
            if not schema:
                continue
            for label in {name, sysname} - {None}:
                self._catalog_objects.add(
                    (schema.strip().upper(), str(label).strip().upper()))
        self.resolver = LiblistResolver(
            liblist=self.config.liblist(None),
            objects=self._catalog_objects,
        )

    # -- xref ----------------------------------------------------------------

    def _edges_from_xref(self) -> None:
        from ..extract.xref import usage_directions

        rows = self.con.execute(
            "SELECT program_lib, program_name, object_lib, object_name, "
            "object_type, usage_flag FROM raw_dsppgmref").fetchall()
        for plib, pname, olib, oname, otype, usage in rows:
            if not pname or not oname:
                continue
            otype = (otype or "").upper().lstrip("*")
            pid = self.add_program_node(plib, pname)
            if otype in {"PGM", "PGM."}:
                qid = self.add_program_node(olib, oname)
                self.add_edge(Edge(src=pid, dst=qid, kind=EdgeKind.CALLS,
                                   provenance=Provenance.XREF,
                                   confidence=Confidence.CONFIRMED))
                continue
            if otype and otype not in {"FILE", "F"}:
                continue
            fid = self.add_file_node(olib, oname)
            for direction in usage_directions(usage):
                self.add_edge(Edge(
                    src=pid, dst=fid,
                    kind=EdgeKind.READS if direction == "reads" else EdgeKind.WRITES,
                    provenance=Provenance.XREF,
                    confidence=Confidence.CONFIRMED,
                ))

        rows = self.con.execute(
            "SELECT dep_lib, dep_file, based_lib, based_file FROM raw_dspdbr"
        ).fetchall()
        for dlib, dfile, blib, bfile in rows:
            if not dfile or not bfile:
                continue
            did = self.add_file_node(dlib, dfile)
            bid = self.add_file_node(blib, bfile)
            if did != bid:
                self.add_edge(Edge(src=did, dst=bid,
                                   kind=EdgeKind.DERIVES_FROM,
                                   provenance=Provenance.XREF,
                                   confidence=Confidence.CONFIRMED))

    def _edges_from_viewdep(self) -> None:
        rows = self.con.execute(
            "SELECT view_schema, view_name, object_schema, object_name "
            "FROM raw_sysviewdep").fetchall()
        for vschema, vname, oschema, oname in rows:
            vid = self.add_file_node(vschema, vname, table_type="V")
            oid = self.add_file_node(oschema, oname)
            if vid != oid:
                self.add_edge(Edge(src=vid, dst=oid,
                                   kind=EdgeKind.DERIVES_FROM,
                                   provenance=Provenance.CATALOG,
                                   confidence=Confidence.CONFIRMED))

    def _view_column_lineage(self) -> None:
        """Column lineage for SQL views from their catalog definitions."""
        from ..parse.embedded_sql import analyze_statement

        rows = self.con.execute(
            "SELECT table_schema, table_name, view_definition FROM raw_sysviews"
        ).fetchall()
        for schema, name, definition in rows:
            if not definition:
                continue
            a = analyze_statement(definition)
            if a.parse_error or not a.column_lineage:
                if a.parse_error:
                    self.gap("parse_error", f"file:{schema}/{name}",
                             "view definition failed to parse",
                             {"error": a.parse_error})
                continue
            for item in a.column_lineage:
                tgt = item["target"]
                if tgt == "*":
                    continue
                tgt_id = self._col_id(schema, name, tgt.split(".")[-1])
                for src in item["sources"]:
                    src_id = self._column_ref_to_id(src)
                    if src_id is None:
                        continue
                    self.add_edge(Edge(src=tgt_id, dst=src_id,
                                       kind=EdgeKind.DERIVES_FROM,
                                       provenance=Provenance.SOURCE_SQL,
                                       confidence=Confidence.PARSED))

    def _column_ref_to_id(self, ref: str) -> Optional[str]:
        """'LIB/TABLE.COL' | 'TABLE.COL' | 'COL' -> column node id (liblist)."""
        if "." not in ref:
            return None
        table_part, col = ref.rsplit(".", 1)
        if col == "*":
            return None
        if "/" in table_part:
            lib, table = table_part.split("/", 1)
        else:
            lib = self._resolve_lib(table_part, f"column ref {ref}")
            table = table_part
        return self._col_id(lib, table, col)

    # -- DDS -----------------------------------------------------------------

    def _edges_from_dds(self) -> None:
        files = self.con.execute(
            "SELECT library, file, dds_type, record_format, based_on, is_join "
            "FROM parsed_dds_files").fetchall()
        for library, fname, dds_type, recfmt, based_on_json, is_join in files:
            if dds_type != "LF":
                continue
            lf_id = self.add_file_node(library, fname, table_type="L")
            for base in json.loads(based_on_json or "[]"):
                blib, bname = self._split_qualified(base)
                if blib is None:
                    blib = self._resolve_lib(bname, f"PFILE of {library}/{fname}")
                pf_id = self.add_file_node(blib, bname)
                self.add_edge(Edge(src=lf_id, dst=pf_id,
                                   kind=EdgeKind.DERIVES_FROM,
                                   provenance=Provenance.DDS,
                                   confidence=Confidence.PARSED))

        fields = self.con.execute(
            "SELECT library, file, record_format, field_name, renamed_from, "
            "ref_field, ref_file, concat_fields FROM parsed_dds_fields"
        ).fetchall()
        for library, fname, recfmt, field_name, renamed, ref_field, ref_file, concat_json in fields:
            lf_col = self._col_id(library, fname, field_name)
            concat = json.loads(concat_json or "[]")
            sources: list[tuple[str, str]] = []  # (pf_name, pf_field)
            if concat and ref_file:
                sources = [(ref_file, c) for c in concat]
            elif concat:
                sources = [(None, c) for c in concat]
            elif ref_field and ref_file:
                sources = [(ref_file, ref_field)]
            elif ref_field:
                sources = [(None, ref_field)]
            for pf_name, pf_field in sources:
                if pf_name is None:
                    self.gap("unresolved_dynamic_name", lf_col,
                             "LF field has no resolvable base file (join LF "
                             "without JREF?)",
                             {"field": field_name, "file": f"{library}/{fname}"})
                    continue
                plib, pname = self._split_qualified(pf_name)
                if plib is None:
                    plib = self._resolve_lib(pname, f"DDS field {lf_col}")
                self.add_edge(Edge(src=lf_col,
                                   dst=self._col_id(plib, pname, pf_field),
                                   kind=EdgeKind.DERIVES_FROM,
                                   provenance=Provenance.DDS,
                                   confidence=Confidence.PARSED,
                                   context={"renamed_from": renamed} if renamed else {}))

    def _lf_column_passthrough(self) -> None:
        """Column passthrough for logicals whose DDS source wasn't parsed.

        Parsed DDS is the best evidence for LF -> PF column lineage (it
        handles renames, JREFs, concatenations). But a live estate retrieves
        source for the slice, not for every dependent logical — a trace then
        stops at the LF as if it were a base file, which is exactly wrong
        for a physical-table-focused analysis. DSPDBR already proves the
        file-level dependency and the catalog knows both field sets, and a
        non-join logical exposes its base file's fields under the same
        names — so same-named columns pass through at inferred confidence.
        Columns that already have parsed evidence (DDS field maps, view
        definitions) are skipped — parsed beats synthesized — but the gate
        is per COLUMN, not per file: a partially parsed DDS (some fields
        renamed/REFed, others missed) must not leave the unparsed fields
        stranded at the logical as if it were a base file.
        """
        # Column node ids that already carry derives evidence from their
        # own definition — don't second-guess those specific columns.
        has_parsed_col: set[str] = set()
        for e in self.edges:
            if e.kind == EdgeKind.DERIVES_FROM and e.src.startswith("column:"):
                has_parsed_col.add(e.src)

        ffd = self._dspffd_fields()
        for dlib, dfile, blib, bfile in self.con.execute(
                "SELECT DISTINCT dep_lib, dep_file, based_lib, based_file "
                "FROM raw_dspdbr").fetchall():
            if not (dlib and dfile and blib and bfile):
                continue
            dep_id = self.add_file_node(dlib, dfile)
            base_id = self.add_file_node(blib, bfile)
            if dep_id == base_id:
                continue
            dep_spec = dep_id.split(":", 1)[1]
            dkey = tuple(dep_spec.split("/", 1))
            bkey = tuple(base_id.split(":", 1)[1].split("/", 1))
            common = ffd.get(dkey, set()) & ffd.get(bkey, set())
            for f in sorted(common):
                src = self._col_id(dkey[0], dkey[1], f)
                if src in has_parsed_col:
                    continue
                self.add_edge(Edge(
                    src=src,
                    dst=self._col_id(bkey[0], bkey[1], f),
                    kind=EdgeKind.DERIVES_FROM,
                    provenance=Provenance.XREF,
                    confidence=Confidence.INFERRED,
                    context={"mechanism": "lf_field_passthrough"}))

    @staticmethod
    def _split_qualified(name: str) -> tuple[Optional[str], str]:
        name = (name or "").strip().upper()
        if "/" in name:
            lib, obj = name.split("/", 1)
            return (lib or None), obj
        return None, name

    # -- CL: calls, CPYF, overrides ------------------------------------------

    def _cl_events(self) -> dict[str, list]:
        """Ordered event streams per CL program for the override simulation."""
        events: dict[str, list] = defaultdict(list)
        ovr = self.con.execute(
            "SELECT program, seq, file, to_file, to_library, to_member, "
            "scope, resolved, expr FROM parsed_cl_overrides").fetchall()
        merged: dict[str, list[tuple[int, tuple]]] = defaultdict(list)
        for program, seq, f, tf, tl, tm, scope, resolved, expr in ovr:
            o = Override(file=f or "", to_library=tl, to_file=tf, to_member=tm,
                         scope=(scope or "*CALLLVL"), origin_program=program,
                         seq=seq, resolved=bool(resolved), expr=expr)
            merged[program].append((seq, ("ovrdbf", o)))

        dlt = self.con.execute(
            "SELECT program, seq, raw_text FROM parsed_cl_statements "
            "WHERE command = 'DLTOVR'").fetchall()
        from ..parse.cl import extract_param
        for program, seq, raw in dlt:
            f = extract_param(raw, "FILE")
            if f is None:
                parts = raw.split(None, 2)
                f = parts[1] if len(parts) > 1 else None
            merged[program].append((seq, ("dltovr", f)))

        calls = self.con.execute(
            "SELECT program, seq, called_lib, called_pgm, via, resolved, expr "
            "FROM parsed_cl_calls WHERE via IN ('CALL', 'SBMJOB')").fetchall()
        for program, seq, clib, cpgm, via, resolved, expr in calls:
            if not resolved or cpgm is None:
                self.gap("unresolved_dynamic_name", f"program:{program}",
                         "CALL target is runtime-dependent",
                         {"expr": expr, "seq": seq})
                continue
            callee_key = self._program_key(clib, cpgm)
            merged[program].append((seq, ("call", CallSite(
                program=program, seq=seq, called=callee_key,
                called_lib=clib, via=via))))

        for program, evs in merged.items():
            events[program] = [e for _, e in sorted(evs, key=lambda x: x[0])]
        return dict(events)

    def _program_key(self, lib: Optional[str], name: str) -> str:
        """Key used to look up a parsed CL member for a called program."""
        if lib:
            return f"{lib}/{name}"
        # Unqualified: match by member name against parsed CL programs.
        return name

    def _simulate_overrides(self) -> dict[str, list[tuple[dict, list[str]]]]:
        """Run the call-tree simulation from every entry-point CL program.

        Returns: program name (unqualified) -> list of (override_map, stack)
        observed at its call sites.
        """
        events = self._cl_events()
        if not events:
            return {}

        called: set[str] = set()
        for evs in events.values():
            for kind, payload in evs:
                if kind == "call":
                    called.add(payload.called.split("/")[-1])
        entries = [p for p in events
                   if p.split("/")[-1] not in called]

        per_program: dict[str, list[tuple[dict, list[str]]]] = defaultdict(list)
        for entry in entries or list(events):
            for callee, ovr_map, stack in simulate_call_tree(entry, events):
                name = callee.split("/")[-1].upper()
                per_program[name].append((ovr_map, stack))

        # Surface unresolved overrides as gaps.
        for program, evs in events.items():
            for kind, payload in evs:
                if kind == "ovrdbf" and not payload.resolved:
                    self.gap("unresolved_dynamic_name",
                             f"program:{program}",
                             "OVRDBF TOFILE is runtime-dependent",
                             {"expr": payload.expr, "file": payload.file})
        return dict(per_program)

    def _edges_from_cl(self) -> None:
        # Call edges (parsed CL supplements xref).
        calls = self.con.execute(
            "SELECT program, called_lib, called_pgm, via, params, resolved "
            "FROM parsed_cl_calls").fetchall()
        for program, clib, cpgm, via, params_json, resolved in calls:
            plib, pname = self._split_qualified(program)
            pid = self.add_program_node(plib, pname)
            if via in {"CALL", "SBMJOB"} and cpgm:
                if clib is None:
                    clib_r = self._resolve_lib(cpgm, f"CALL from {program}")
                else:
                    clib_r = clib
                qid = self.add_program_node(clib_r, cpgm)
                self.add_edge(Edge(src=pid, dst=qid, kind=EdgeKind.CALLS,
                                   provenance=Provenance.SOURCE_CL,
                                   confidence=Confidence.PARSED if resolved
                                   else Confidence.UNRESOLVED,
                                   context={"via": via}))
            elif via == "CPYF":
                try:
                    info = json.loads(json.loads(params_json)[0])
                except (ValueError, IndexError, TypeError):
                    continue
                ffile, tfile = info.get("from_file"), info.get("to_file")
                if not ffile or not tfile:
                    self.gap("unresolved_dynamic_name", f"program:{program}",
                             "CPYF with runtime-dependent file name", info)
                    continue
                flib = info.get("from_lib") or self._resolve_lib(
                    ffile, f"CPYF in {program}")
                tlib = info.get("to_lib") or self._resolve_lib(
                    tfile, f"CPYF in {program}")
                fid = self.add_file_node(flib, ffile)
                tid = self.add_file_node(tlib, tfile)
                # Data flows FROMFILE -> TOFILE: target derives from source.
                self.add_edge(Edge(src=tid, dst=fid,
                                   kind=EdgeKind.DERIVES_FROM,
                                   provenance=Provenance.SOURCE_CL,
                                   confidence=Confidence.PARSED,
                                   context={"via": "CPYF", "program": program}))
                # The CL program also reads/writes those files.
                self.add_edge(Edge(src=pid, dst=fid, kind=EdgeKind.READS,
                                   provenance=Provenance.SOURCE_CL,
                                   confidence=Confidence.PARSED))
                self.add_edge(Edge(src=pid, dst=tid, kind=EdgeKind.WRITES,
                                   provenance=Provenance.SOURCE_CL,
                                   confidence=Confidence.PARSED))
                # Replayed by the column-usage pass (phase 3): which FROMFILE
                # columns the CL program actually reads.
                self._cpyf_events.append({
                    "pid": pid, "from_lib": flib, "from_file": ffile,
                    "to_lib": tlib, "to_file": tfile,
                    "fmtopt": info.get("fmtopt"),
                })

    def _apply_overrides_to_xref(self) -> None:
        """Redirect compiled (xref) file references through observed OVRDBFs.

        Phase-2 behaviour: a called program's DSPPGMREF references are
        interpreted under the overrides active at its call sites, even when no
        RPG source is available. Superseded xref edges are dropped only when
        every call site overrides.
        """
        if not self._overrides:
            return
        new_edges: list[Edge] = []
        superseded: set[tuple[str, str]] = set()
        for e in list(self.edges):
            if not (e.provenance == Provenance.XREF
                    and e.kind in {EdgeKind.READS, EdgeKind.WRITES}
                    and e.src.startswith("program:")
                    and e.dst.startswith("file:")):
                continue
            pgm = e.src.split("/")[-1].upper()
            fname = e.dst.split(":", 1)[1].split("/")[-1].split("(")[0].upper()
            targets, all_ovr = self._apply_override(pgm, fname)
            if not targets:
                continue
            for lib, tf, mbr, ctx in targets:
                fid = self.add_file_node(lib, tf, mbr)
                new_edges.append(Edge(
                    src=e.src, dst=fid, kind=e.kind,
                    provenance=Provenance.SOURCE_CL,
                    confidence=Confidence.PARSED,
                    context={**ctx, "overridden_file": fname}))
            if all_ovr:
                superseded.add((pgm, fname))
        for e in new_edges:
            self.add_edge(e)
        if superseded:
            self._drop_superseded_xref_edges(superseded)

    # -- RPG -----------------------------------------------------------------

    def _apply_override(self, program_name: str, file_name: str
                        ) -> tuple[list[tuple[Optional[str], str, Optional[str], dict]], bool]:
        """Resolve a program's file reference through observed call-site
        overrides.

        Returns ``(targets, all_sites_overridden)`` where each target is
        (library, file, member, context). ``all_sites_overridden`` is True
        only when *every* observed call site had the override active — only
        then may the compiled (xref) reference be superseded; when a program
        is called both with and without the override, both resolutions are
        real and both edges are kept.
        """
        sites = self._overrides.get(program_name.upper(), [])
        targets = []
        n_overridden = 0
        for ovr_map, stack in sites:
            o = ovr_map.get(file_name.upper())
            if o is None or not o.resolved or not o.to_file:
                continue
            n_overridden += 1
            lib = o.to_library or self._resolve_lib(
                o.to_file, f"OVRDBF in {o.origin_program}")
            targets.append((lib, o.to_file, o.to_member,
                            {"override_origin": o.origin_program,
                             "call_stack": stack}))
        all_overridden = bool(sites) and n_overridden == len(sites)
        return targets, all_overridden

    def _edges_from_rpg(self) -> None:
        rows = self.con.execute(
            "SELECT program, file, usage, extname FROM parsed_rpg_files"
        ).fetchall()
        superseded: set[tuple[str, str]] = set()  # (pgm name, file name)
        for program, fname, usage, extname in rows:
            plib, pname = self._split_qualified(program)
            pid = self.add_program_node(plib, pname)
            ext = (extname or fname).upper()
            directions = {"input": ["reads"], "output": ["writes"],
                          "update": ["reads", "writes"],
                          "combined": ["reads", "writes"]}.get(usage, ["reads"])

            targets, all_ovr = self._apply_override(pname, fname)
            if not targets:
                targets, all_ovr = self._apply_override(pname, ext)
            override_edges = list(targets)
            if all_ovr:
                # Every observed call site redirects this file: the compiled
                # (xref) reference is superseded by the override target(s).
                superseded.add((pname.upper(), fname.upper()))
                superseded.add((pname.upper(), ext))
            if not targets or not all_ovr:
                # Default resolution applies (also alongside partial overrides).
                lib = self._resolve_lib(ext, f"RPG F-spec in {program}")
                if lib is None:
                    self.gap("outside_scope", f"file:*LIBL/{ext}",
                             f"file referenced by {program} not found in any "
                             "configured library", {"program": program})
                targets = override_edges + [(lib, ext, None, {})]

            if "reads" in directions:
                key = (pname.upper(), fname.upper())
                self._rpg_read_targets[key].extend(
                    (lib, tf) for lib, tf, mbr, ctx in targets)

            for lib, tf, mbr, ctx in targets:
                is_override = "override_origin" in ctx
                prov = Provenance.SOURCE_CL if is_override else Provenance.SOURCE_RPG
                fid = self.add_file_node(lib, tf, mbr)
                for d in directions:
                    self.add_edge(Edge(
                        src=pid, dst=fid,
                        kind=EdgeKind.READS if d == "reads" else EdgeKind.WRITES,
                        provenance=prov, confidence=Confidence.PARSED,
                        context={**ctx, "declared_file": fname}))

        if superseded:
            self._drop_superseded_xref_edges(superseded)

    def _drop_superseded_xref_edges(self, superseded: set[tuple[str, str]]) -> None:
        """Remove compiled-reference edges that OVRDBF redirection supersedes
        (known trap: DSPPGMREF shows compiled references only).
        """
        kept: list[Edge] = []
        for e in self.edges:
            drop = False
            if (e.provenance == Provenance.XREF
                    and e.kind in {EdgeKind.READS, EdgeKind.WRITES}
                    and e.src.startswith("program:") and e.dst.startswith("file:")):
                pgm = e.src.split("/")[-1].upper()
                fname = e.dst.split(":", 1)[1].split("/")[-1].split("(")[0].upper()
                if (pgm, fname) in superseded:
                    drop = True
            if drop:
                self._edge_keys.discard(e.key())
            else:
                kept.append(e)
        self.edges = kept

    # -- SQL -----------------------------------------------------------------

    def _edges_from_sql(self) -> None:
        rows = self.con.execute(
            "SELECT program, stmt_type, tables_read, tables_written, "
            "column_lineage, parse_error, raw_sql FROM parsed_sql_statements"
        ).fetchall()
        for program, stmt_type, tr_json, tw_json, cl_json, perr, raw_sql in rows:
            base_program = program.split("#")[0]
            plib, pname = self._split_qualified(base_program)
            pid = self.add_program_node(plib, pname)

            # parse_error with stmt_type PARSE_ERROR = nothing salvaged;
            # parse_error with a real stmt_type = the name-level DDL fallback
            # in parse/embedded_sql.py — its table facts are regex-derived,
            # so they flow through at INFERRED confidence, gap still logged.
            partial = bool(perr) and stmt_type != "PARSE_ERROR"
            if perr and not partial:
                self.gap("parse_error", f"program:{base_program}",
                         f"SQL statement failed to parse: {perr[:200]}",
                         {"sql": (raw_sql or "")[:500]})
                continue
            if partial:
                self.gap("parse_error", f"program:{base_program}",
                         "SQL statement partially parsed (name-level DDL "
                         f"fallback): {perr[:200]}",
                         {"sql": (raw_sql or "")[:500]})
            if stmt_type == "DYNAMIC":
                self.gap("unresolved_dynamic_name", f"program:{base_program}",
                         "dynamic SQL (PREPARE/EXECUTE IMMEDIATE)",
                         {"sql": (raw_sql or "")[:500]})
                continue

            conf = Confidence.INFERRED if partial else Confidence.PARSED
            for tname in json.loads(tr_json or "[]"):
                fid = self._sql_table_node(tname, base_program)
                self.add_edge(Edge(src=pid, dst=fid, kind=EdgeKind.READS,
                                   provenance=Provenance.SOURCE_SQL,
                                   confidence=conf))
            for tname in json.loads(tw_json or "[]"):
                fid = self._sql_table_node(tname, base_program)
                self.add_edge(Edge(src=pid, dst=fid, kind=EdgeKind.WRITES,
                                   provenance=Provenance.SOURCE_SQL,
                                   confidence=conf))

            for item in json.loads(cl_json or "[]"):
                tgt = item.get("target", "")
                if not tgt or tgt == "*" or "." not in tgt:
                    continue
                tgt_id = self._column_ref_to_id(tgt)
                if tgt_id is None:
                    continue
                for src in item.get("sources", []):
                    src_id = self._column_ref_to_id(src)
                    if src_id is None:
                        continue
                    self.add_edge(Edge(src=tgt_id, dst=src_id,
                                       kind=EdgeKind.DERIVES_FROM,
                                       provenance=Provenance.SOURCE_SQL,
                                       confidence=conf,
                                       context={"program": base_program}))

    def _sql_table_node(self, tname: str, program: str) -> str:
        lib, name = self._split_qualified(tname)
        if lib is None:
            lib = self._resolve_lib(name, f"SQL in {program}")
            if lib is None:
                self.gap("outside_scope", f"file:*LIBL/{name}",
                         f"table referenced by {program} not found in any "
                         "configured library", {"program": program})
        return self.add_file_node(lib, name)

    # -- column usage (program -reads-> column) -------------------------------

    def _dspffd_fields(self) -> dict[tuple[str, str], set[str]]:
        """(LIB, FILE) -> set of field names. Cached — used by both the
        record-format expansion and the column-usage passes.

        Sources, unioned: raw_dspffd (DSPFFD outfiles), and raw_syscolumns —
        the SQL catalog knows every database file's fields under the same
        10-char internal names DSPFFD reports (SYSTEM_COLUMN_NAME), so a
        catalog-covered file needs no per-file DSPFFD host call at all.
        Targeted extraction relies on this to skip those commands; the union
        keeps full and targeted builds identical.
        """
        if self._ffd_cache is None:
            ffd: dict[tuple[str, str], set[str]] = defaultdict(set)
            for lib, fname, field_name in self.con.execute(
                    "SELECT file_lib, file_name, field_name FROM raw_dspffd"
            ).fetchall():
                if lib and fname and field_name:
                    ffd[(lib.upper(), fname.upper())].add(field_name.upper())
            for schema, name, sysname, col, syscol in self.con.execute(
                    "SELECT table_schema, table_name, system_name, "
                    "column_name, system_column FROM raw_syscolumns"
            ).fetchall():
                field = str(syscol or col or "").strip().upper()
                lib_u = (schema or "").strip().upper()
                if not field or not lib_u:
                    continue
                for label in {name, sysname} - {None}:
                    ffd[(lib_u, str(label).strip().upper())].add(field)
            self._ffd_cache = ffd
        return self._ffd_cache

    def _column_usage_edges(self) -> None:
        """Program -reads-> column edges (design: "Column usage from CL and
        RPG parsing"): which source columns a program actually touches,
        complementing (not replacing) the target-mapping heuristics below.
        """
        self._rpg_field_usage_edges()
        self._sql_column_usage_edges()
        self._cpyf_column_usage_edges()

    def _rpg_field_usage_edges(self) -> None:
        ffd = self._dspffd_fields()
        refs: dict[str, set[str]] = defaultdict(set)
        for program, field_name in self.con.execute(
                "SELECT program, field_name FROM parsed_rpg_field_refs"
        ).fetchall():
            refs[program].add((field_name or "").upper())

        rows = self.con.execute(
            "SELECT program, file, usage FROM parsed_rpg_files "
            "WHERE usage IN ('input', 'update', 'combined')").fetchall()
        for program, fname, usage in rows:
            plib, pname = self._split_qualified(program)
            pid = self.add_program_node(plib, pname)
            targets = self._rpg_read_targets.get(
                (pname.upper(), (fname or "").upper()), [])
            program_refs = refs.get(program, set())
            for lib, tf in targets:
                fields = ffd.get(((lib or "").upper(), (tf or "").upper()), set())
                if not fields:
                    continue
                # Field references are harvested program-wide: RPG III
                # same-named fields across read files share one variable, so
                # a referenced name present in this file's format means this
                # file's column genuinely feeds that variable (parsed) — even
                # when the reference was coded against another file. The
                # record's remaining fields are still loaded by every READ/
                # CHAIN, so they keep the inferred record-level fallback
                # rather than being suppressed by the intersection.
                used = fields & program_refs
                for f in sorted(used):
                    self.add_edge(Edge(
                        src=pid, dst=self._col_id(lib, tf, f),
                        kind=EdgeKind.READS, provenance=Provenance.SOURCE_RPG,
                        confidence=Confidence.PARSED,
                        context={"mechanism": "field_reference"}))
                for f in sorted(fields - used):
                    self.add_edge(Edge(
                        src=pid, dst=self._col_id(lib, tf, f),
                        kind=EdgeKind.READS, provenance=Provenance.SOURCE_RPG,
                        confidence=Confidence.INFERRED,
                        context={"mechanism": "record_io_all_fields"}))

    def _sql_column_usage_edges(self) -> None:
        rows = self.con.execute(
            "SELECT program, stmt_type, tables_read, columns_used, parse_error "
            "FROM parsed_sql_statements").fetchall()
        for program, stmt_type, tr_json, cu_json, perr in rows:
            # Partial DDL-fallback statements (parse_error set, real
            # stmt_type) keep their columns_used: those come from a fully
            # parsed inner SELECT, only the outer DDL shell failed.
            partial = bool(perr) and stmt_type != "PARSE_ERROR"
            if (perr and not partial) or stmt_type == "DYNAMIC":
                continue
            base_program = program.split("#")[0]
            plib, pname = self._split_qualified(base_program)
            pid = self.add_program_node(plib, pname)
            read_tables = set(json.loads(tr_json or "[]"))
            conf = Confidence.INFERRED if partial else Confidence.PARSED
            for ref in json.loads(cu_json or "[]"):
                if "." not in ref:
                    continue
                table_part = ref.rsplit(".", 1)[0]
                # Only columns of tables this statement actually reads — a
                # target-only table (INSERT/UPDATE/MERGE) is not "used".
                if table_part not in read_tables:
                    continue
                col_id = self._column_ref_to_id(ref)
                if col_id is None:
                    continue
                self.add_edge(Edge(src=pid, dst=col_id, kind=EdgeKind.READS,
                                   provenance=Provenance.SOURCE_SQL,
                                   confidence=conf,
                                   context={"mechanism": "sql_reference"}))

    def _cpyf_column_usage_edges(self) -> None:
        ffd = self._dspffd_fields()
        for ev in self._cpyf_events:
            from_fields = ffd.get(
                ((ev["from_lib"] or "").upper(), (ev["from_file"] or "").upper()),
                set())
            to_fields = ffd.get(
                ((ev["to_lib"] or "").upper(), (ev["to_file"] or "").upper()),
                set())
            used = from_fields & to_fields
            if not used:
                continue
            fmtopt = (ev.get("fmtopt") or "").upper()
            is_map = "*MAP" in fmtopt
            confidence = Confidence.PARSED if is_map else Confidence.INFERRED
            mechanism = "cpyf_map" if is_map else "cpyf_layout"
            for f in sorted(used):
                self.add_edge(Edge(
                    src=ev["pid"],
                    dst=self._col_id(ev["from_lib"], ev["from_file"], f),
                    kind=EdgeKind.READS, provenance=Provenance.SOURCE_CL,
                    confidence=confidence, context={"mechanism": mechanism}))

    # -- record-format column expansion --------------------------------------

    def _record_format_column_expansion(self) -> None:
        """Field-level source resolution for RPG record I/O.

        The blanket rule "same-named fields between read and written files
        map column-to-column" fans an output's sources out to every
        reference file the program binds — unusable for an analyst. The
        parsed evidence pins it down per output field:

        * **Output field set** — the file's O-specs when present (the
          definitive list of what the program writes, byte-buffer files
          included); otherwise the catalog/DSPFFD field set of an
          externally described file. A program-described file without
          O-specs stays opaque, as before.
        * **C-spec assignments** (``parsed_rpg_moves``) — a field assigned
          by MOVE/Z-ADD/arithmetic derives from the move chain's terminal
          fields (``cspec_move_chain``, renames included), NOT from a
          same-named input field; a field assigned only constants derives
          from nothing (pruned, not guessed).
        * **Unassigned fields** flow straight through record I/O from the
          read files that carry them (RPG populates same-named fields of
          externally described records on READ): ``record_read_through`` /
          ``ospec_field``. A program-described *read* file contributes its
          I-spec fields instead of catalog columns.

        Confidence: ``parsed`` when the source field lives in exactly one
        read file — or in several files that DSPDBR proves are views of one
        base physical (reading a logical and its base is ONE source, marked
        ``same_base``); ``inferred`` (with ``ambiguous_files``) only when
        genuinely distinct base files carry it — the honest residue of
        static analysis.
        """
        prog_described: dict[str, set[str]] = defaultdict(set)
        rpg_programs: set[str] = set()
        for program, fname, extname, pdesc in self.con.execute(
                "SELECT program, file, extname, program_described "
                "FROM parsed_rpg_files").fetchall():
            short = str(program).split("/")[-1].upper()
            rpg_programs.add(short)
            if pdesc:
                for nm in (fname, extname):
                    if nm:
                        prog_described[short].add(str(nm).strip().upper())
        if not rpg_programs:
            return

        ospec: dict[tuple[str, str], set[str]] = defaultdict(set)
        for program, fname, fld in self.con.execute(
                "SELECT program, file, field_name "
                "FROM parsed_rpg_ospec_fields").fetchall():
            ospec[(str(program).split("/")[-1].upper(),
                   str(fname).upper())].add(str(fld).upper())
        ispec: dict[tuple[str, str], set[str]] = defaultdict(set)
        for program, fname, fld in self.con.execute(
                "SELECT program, file, field_name "
                "FROM parsed_rpg_ispec_fields").fetchall():
            ispec[(str(program).split("/")[-1].upper(),
                   str(fname).upper())].add(str(fld).upper())
        # short program -> result field -> source fields (empty set = only
        # constant assignments seen: the field is overwritten, sources
        # unknown).
        moves: dict[str, dict[str, set[str]]] = defaultdict(dict)
        for program, src, res in self.con.execute(
                "SELECT program, source_field, result_field "
                "FROM parsed_rpg_moves").fetchall():
            d = moves[str(program).split("/")[-1].upper()] \
                .setdefault(str(res).upper(), set())
            if src:
                d.add(str(src).upper())

        ffd = self._dspffd_fields()

        # LF -> base-PF map from DSPDBR, for ambiguity collapse: a field
        # found in two read files that are views of the same physical is
        # one source, not two.
        dbr_base: dict[tuple[str, str], tuple[str, str]] = {}
        for dlib, dfile, blib, bfile in self.con.execute(
                "SELECT DISTINCT dep_lib, dep_file, based_lib, based_file "
                "FROM raw_dspdbr").fetchall():
            if dlib and dfile and blib and bfile:
                dbr_base[(str(dlib).strip().upper(),
                          str(dfile).strip().upper())] = \
                    (str(blib).strip().upper(), str(bfile).strip().upper())
        self._dbr_base = dbr_base

        # program -> read/write file node ids, from edges built so far
        # (override-resolved). Program-described files stay in — the O-spec
        # and I-spec evidence is exactly what makes them transparent.
        reads: dict[str, set[str]] = defaultdict(set)
        writes: dict[str, set[str]] = defaultdict(set)
        for e in self.edges:
            if not e.src.startswith("program:"):
                continue
            pname = e.src.split(":", 1)[1]
            if pname.split("/")[-1].upper() not in rpg_programs:
                continue
            if e.kind == EdgeKind.READS and e.dst.startswith("file:"):
                reads[pname].add(e.dst)
            elif e.kind == EdgeKind.WRITES and e.dst.startswith("file:"):
                writes[pname].add(e.dst)

        def fields_of(file_node: str) -> tuple[Optional[tuple[str, str]], set[str]]:
            spec = file_node.split(":", 1)[1].split("(")[0]
            if "/" not in spec:
                return None, set()
            lib, name = spec.split("/", 1)
            return (lib, name), ffd.get((lib, name), set())

        for pname in set(reads) | set(writes):
            short = pname.split("/")[-1].upper()
            pdesc = prog_described.get(short, set())
            pmoves = moves.get(short, {})

            read_specs: list[tuple[tuple[str, str], set[str]]] = []
            for r in reads.get(pname, ()):
                rkey, rfields = fields_of(r)
                if rkey is None:
                    continue
                if rkey[1] in pdesc:
                    # Byte buffer on the read side: only I-spec fields are
                    # populated — catalog columns of the underlying file
                    # are NOT program variables here.
                    rfields = ispec.get((short, rkey[1]), set())
                else:
                    rfields = rfields | ispec.get((short, rkey[1]), set())
                if rfields:
                    read_specs.append((rkey, rfields))

            for w in writes.get(pname, ()):
                wkey, wfields = fields_of(w)
                if wkey is None:
                    continue
                ofl = ospec.get((short, wkey[1]))
                if ofl:
                    # O-specs are authoritative. Name-match against the
                    # file's real columns when the catalog knows them; for
                    # an uncatalogued file the O-spec names ARE the layout.
                    out_fields = ({f for f in ofl if f in wfields}
                                  if wfields else set(ofl))
                    for f in sorted(ofl - out_fields):
                        self.gap(
                            "ospec_field_unmapped",
                            self._col_id(wkey[0], wkey[1], f),
                            f"O-spec field {f} of {pname} has no same-named "
                            f"column in {wkey[0]}/{wkey[1]} (position-level "
                            "mapping not attempted)",
                            {"program": pname})
                    mech_direct = "ospec_field"
                elif wkey[1] in pdesc:
                    continue  # byte buffer with no O-specs: stays opaque
                elif wfields:
                    out_fields = wfields
                    mech_direct = "record_read_through"
                else:
                    continue
                for f in sorted(out_fields):
                    self._emit_field_sources(
                        pname, wkey, f, pmoves, read_specs, mech_direct,
                        gap_when_untraced=bool(ofl))

    def _emit_field_sources(self, pname: str, wkey: tuple[str, str], f: str,
                            pmoves: dict[str, set[str]],
                            read_specs: list[tuple[tuple[str, str], set[str]]],
                            mech_direct: str,
                            gap_when_untraced: bool) -> None:
        """Derives edges for one output field of one written file.

        An assigned field (present in ``pmoves``) resolves through the move
        chain: walk source fields until one belongs to a read file, carrying
        the intermediate variables as ``via``. An unassigned field resolves
        by direct record read-through. Both attribute per terminal field:
        one owning base file = parsed (read files that DSPDBR maps to the
        same physical count as one, marked ``same_base``), several distinct
        bases = inferred + ambiguous_files. Untraceable fields leave a gap
        that says WHY (runtime value, literals, dead-end work variables) so
        "no resolved lineage" is explained, not bare.
        """
        collected: list[tuple[str, tuple[str, ...],
                              list[tuple[str, str]]]] = []
        dead_ends: set[str] = set()

        def owners(tok: str) -> list[tuple[str, str]]:
            return [rkey for rkey, rfields in read_specs
                    if tok in rfields and rkey != wkey]

        assigned = f in pmoves
        if assigned:
            seen = {f}
            stack: list[tuple[str, tuple[str, ...]]] = [(f, ())]
            while stack:
                tok, path = stack.pop()
                if tok != f:
                    hits = owners(tok)
                    if hits:
                        collected.append((tok, path, hits))
                        continue  # reached a file-backed field: stop here
                srcs = pmoves.get(tok)
                if srcs is None and tok != f:
                    dead_ends.add(tok)
                    continue
                for s in sorted(srcs or ()):
                    if s not in seen and len(path) < 8:
                        seen.add(s)
                        stack.append((s, path + (tok,)))
        else:
            hits = owners(f)
            if hits:
                collected.append((f, (), hits))

        if not collected:
            if assigned or gap_when_untraced:
                runtime = sorted(dead_ends & _RPG_RUNTIME_VALUES)
                work = sorted(dead_ends - _RPG_RUNTIME_VALUES)
                if runtime:
                    reason, detail = "runtime_value", (
                        "assigned from runtime value(s) "
                        + ", ".join(runtime))
                elif work:
                    reason, detail = "work_variables", (
                        "assignment chain dead-ends at work variable(s) "
                        + ", ".join(work[:6]))
                elif assigned:
                    reason, detail = "constant", (
                        "assigned but no field source (literals, constants, "
                        "or runtime opcodes like TIME)")
                else:
                    reason, detail = "unowned", \
                        "not assigned and not present in any read file"
                self.gap(
                    "rpg_untraced_output_field",
                    self._col_id(wkey[0], wkey[1], f),
                    f"{pname} writes {f}: {detail}",
                    {"program": pname, "reason": reason})
            return

        def ultimate_base(key: tuple[str, str]) -> tuple[str, str]:
            k = key
            for _ in range(4):
                nxt = self._dbr_base.get(k)
                if nxt is None or nxt == k:
                    break
                k = nxt
            return k

        for tok, path, hits in collected:
            bases = {ultimate_base(k) for k in hits}
            conf = (Confidence.PARSED if len(bases) == 1
                    else Confidence.INFERRED)
            ctx_base = {"program": pname,
                        "mechanism": ("cspec_move_chain" if path
                                      else mech_direct)}
            via = path[1:]
            if via:
                ctx_base["via"] = " <- ".join(via)
            if len(bases) > 1:
                ctx_base["ambiguous_files"] = len(hits)
            elif len(hits) > 1:
                ctx_base["same_base"] = "/".join(next(iter(bases)))
            for rkey in hits:
                self.add_edge(Edge(
                    src=self._col_id(wkey[0], wkey[1], f),
                    dst=self._col_id(rkey[0], rkey[1], tok),
                    kind=EdgeKind.DERIVES_FROM,
                    provenance=Provenance.SOURCE_RPG,
                    confidence=conf,
                    context=dict(ctx_base)))

    # -- source coverage -----------------------------------------------------

    def _detect_missing_source(self) -> None:
        """Programs referenced in xref with no retrieved source member."""
        members = {
            r[0].upper() for r in self.con.execute(
                "SELECT DISTINCT member FROM raw_source_members").fetchall()
        }
        programs = self.con.execute(
            "SELECT DISTINCT program_lib, program_name FROM raw_dsppgmref"
        ).fetchall()
        for plib, pname in programs:
            if pname and pname.upper() not in members:
                self.gap("missing_source", program_id(plib, pname),
                         "compiled object has no retrieved source member; "
                         "lineage limited to compiled references",
                         {"library": plib})

    # -- persistence ---------------------------------------------------------

    def _persist(self) -> None:
        from ..db import insert_rows, reset_layer
        reset_layer(self.con, "graph")
        self.con.execute("DELETE FROM gaps")
        insert_rows(self.con, "nodes",
                    ["id", "kind", "library", "name", "attrs"],
                    [n.row() for n in self.nodes.values()])
        insert_rows(self.con, "edges",
                    ["src", "dst", "kind", "provenance", "confidence",
                     "context"],
                    [e.row() for e in self.edges])
        insert_rows(self.con, "gaps",
                    ["kind", "object_id", "detail", "context"], self.gaps)

    def to_networkx(self) -> nx.MultiDiGraph:
        g = nx.MultiDiGraph()
        for n in self.nodes.values():
            g.add_node(n.id, kind=n.kind.value, library=n.library, name=n.name)
        for e in self.edges:
            g.add_edge(e.src, e.dst, kind=e.kind.value,
                       provenance=e.provenance.value,
                       confidence=e.confidence.value,
                       context=e.context)
        return g


def load_graph(con) -> nx.MultiDiGraph:
    """Rebuild the networkx graph from the persisted nodes/edges tables."""
    g = nx.MultiDiGraph()
    for nid, kind, library, name, attrs in con.execute(
            "SELECT id, kind, library, name, attrs FROM nodes").fetchall():
        g.add_node(nid, kind=kind, library=library, name=name)
    for src, dst, kind, prov, conf, context in con.execute(
            "SELECT src, dst, kind, provenance, confidence, context "
            "FROM edges").fetchall():
        g.add_edge(src, dst, kind=kind, provenance=prov, confidence=conf,
                   context=json.loads(context or "{}"))
    return g


def build_graph(con, config, phase: int = 3) -> nx.MultiDiGraph:
    return GraphBuilder(con, config, phase=phase).build()
