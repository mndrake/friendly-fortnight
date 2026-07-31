"""Cross-reference harvest: DSPPGMREF / DSPDBR / DSPFFD outfiles.

These ``DSP*`` commands write to database outfiles whose record layouts are
IBM-defined model files (``QADSPPGM``/``QWHDRPPR``, ``QADSPDBR``/``QWHDRDBR``,
``QADSPFFD``/``QWHDRFFD``). We map those layouts by **field name, never by
position**, the same adaptive way ``extract/catalog.py`` maps QSYS2 catalog
views: each field we want declares an ordered list of candidate model-file
column names (a target release may rename or drop one — e.g. field name is
``WHFLDI``/``WHFLDE`` depending on release, and DSPPGMREF has no ``WHNCNT``
ref-count field at all) plus a required flag. The candidate list is resolved
against the outfile's *actual* columns, probed at run time
(:func:`_select_outfile`) — never a hardcoded, unverified list. A required
field with no matching candidate fails loudly (:class:`OutfileShapeError`)
rather than silently shifting columns.

The mapping functions (``map_*``) are pure and unit-tested against fixture
rows; orchestration (``harvest``) is the only part that touches the host.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .connection import HostSession, QueryResult
from .progress import NULL, Progress


@dataclass(frozen=True)
class OutfileLayout:
    """One outfile record layout: model file/format and its field candidates.

    ``fields`` entries are ``(raw_column, candidates, required)``: ``raw_column``
    is the stable name downstream code reads (matches ``map_*``/``*_COLUMNS``),
    ``candidates`` is an ordered tuple of model-file column names to try. The
    field's own ``raw_column`` is implicitly tried last, after every declared
    candidate — the identity rule that lets a source already exposing our raw
    names (:class:`~.connection.FixtureHostSession`) resolve unchanged.
    """

    name: str
    model_file: str      # e.g. QSYS/QADSPPGM
    record_format: str   # e.g. QWHDRPPR
    fields: tuple[tuple[str, tuple[str, ...], bool], ...]

    @property
    def raw_columns(self) -> tuple[str, ...]:
        return tuple(raw for raw, _cands, _req in self.fields)

    def resolve(self, actual_columns: Sequence[str]) -> tuple[str, list[str]]:
        """Resolve this layout against an outfile's actual columns.

        Returns ``(select_list_sql, missing_optional_raw_columns)``. Matching
        is case-insensitive. Missing optional fields NULL-fill
        (``CAST(NULL AS VARCHAR(1)) AS <raw>``); a missing required field
        raises :class:`OutfileShapeError`.
        """
        available = {c.upper(): c for c in actual_columns}
        parts: list[str] = []
        missing: list[str] = []
        for raw, candidates, required in self.fields:
            tried = list(candidates) + [raw]  # identity rule: implicit last try
            picked = next((available[c.upper()] for c in tried
                          if c.upper() in available), None)
            if picked is None:
                if required:
                    raise OutfileShapeError(self, raw, tried, actual_columns)
                missing.append(raw)
                parts.append(f"CAST(NULL AS VARCHAR(1)) AS {raw}")
            else:
                parts.append(f"{picked} AS {raw}")
        return ", ".join(parts), missing


class OutfileShapeError(RuntimeError):
    """A required outfile field has no matching column on this release."""

    def __init__(self, layout: OutfileLayout, raw_column: str,
                candidates_tried: Sequence[str], actual_columns: Sequence[str]):
        super().__init__(
            f"{layout.model_file} ({layout.record_format}) outfile for "
            f"'{layout.name}' has none of the candidate columns "
            f"{tuple(candidates_tried)} needed for '{raw_column}'. Actual "
            f"columns: {sorted(actual_columns)}. Update the candidate list "
            f"in extract/xref.py for this release."
        )
        self.layout = layout
        self.raw_column = raw_column
        self.candidates_tried = tuple(candidates_tried)
        self.actual_columns = tuple(actual_columns)


# --- DSPPGMREF: program -> referenced object (compiled references) -----------
# QSYS/QADSPPGM (record format QWHDRPPR) documented fields: WHLIB, WHPNAM,
# WHTEXT, WHFNUM, WHDTTM, WHFNAM, WHLNAM, WHSNAM, WHRFNO, WHFUSG, WHRFNM,
# WHRFSN, WHRFFN, WHOBJT (1-char object type), WHOTYP (10-char object type),
# WHSYSN, WHSPKG. There is no ref-count field (WHNCNT was invented).
PGMREF_LAYOUT = OutfileLayout(
    name="dsppgmref",
    model_file="QSYS/QADSPPGM",
    record_format="QWHDRPPR",
    fields=(
        ("program_lib", ("WHLIB",), True),
        ("program_name", ("WHPNAM",), True),
        ("object_lib", ("WHLNAM",), True),
        ("object_name", ("WHFNAM",), True),
        ("object_type", ("WHOTYP", "WHOBJT"), True),
        ("usage_flag", ("WHFUSG",), True),
        # Not a documented QWHDRPPR field; kept for raw-schema compat, always
        # NULL-filled on a real host.
        ("ref_count", ("WHNCNT",), False),
    ),
)

# --- DSPDBR: dependent (logical) -> based-on (physical) ----------------------
# QSYS/QADSPDBR (record format QWHDRDBR): the file the command was run
# against (based-on) is WHRFI/WHRLI; the dependent (logical) file is
# WHREFI/WHRELI.
DBR_LAYOUT = OutfileLayout(
    name="dspdbr",
    model_file="QSYS/QADSPDBR",
    record_format="QWHDRDBR",
    fields=(
        ("dep_lib", ("WHRELI",), True),
        ("dep_file", ("WHREFI",), True),
        ("based_lib", ("WHRLI", "WHLIB"), True),
        ("based_file", ("WHRFI", "WHFILE"), True),
        ("dep_type", ("WHRTYP", "WHTYPE"), False),
    ),
)

# --- DSPFFD: field descriptions per file/record format -----------------------
# QSYS/QADSPFFD (record format QWHDRFFD): field name exists as both WHFLDI
# (internal) and WHFLDE (external); WHFLDN ordinal is undocumented, so it and
# the rest of the descriptive fields are optional/candidate-based.
FFD_LAYOUT = OutfileLayout(
    name="dspffd",
    model_file="QSYS/QADSPFFD",
    record_format="QWHDRFFD",
    fields=(
        ("file_lib", ("WHLIB",), True),
        ("file_name", ("WHFILE",), True),
        ("record_format", ("WHNAME",), True),
        ("field_name", ("WHFLDI", "WHFLDE"), True),
        ("field_type", ("WHFLDT",), False),
        ("field_length", ("WHFLDB",), False),
        ("field_scale", ("WHFLDP", "WHFLDD"), False),
        ("field_text", ("WHFTXT",), False),
        ("field_ordinal", ("WHFLDN", "WHFOBO"), False),
    ),
)


# --- Usage flag interpretation ----------------------------------------------
# DSPPGMREF WHFUSG file-usage codes. Values are release-documented; we map to
# read/write direction sets. Unknown/blank -> ('reads',) as the safe default
# (a referenced file we cannot prove is written is treated as an input).
_USAGE_MAP: dict[str, tuple[str, ...]] = {
    "1": ("reads",),                 # input
    "2": ("writes",),                # output
    "3": ("reads", "writes"),        # both
    "4": ("reads", "writes"),        # update (read + write)
    "8": ("reads",),                 # references (constraint) — treat as read
}


def usage_directions(flag: str | None) -> tuple[str, ...]:
    if flag is None:
        return ("reads",)
    return _USAGE_MAP.get(str(flag).strip(), ("reads",))


def _row_map(result: QueryResult, layout: OutfileLayout) -> list[dict[str, Any]]:
    """Validate that the query returned our raw columns, return list of dicts.

    Comparison is case-insensitive: the live JDBC driver reports our
    ``AS raw_column`` aliases folded to uppercase.
    """
    returned = {c.lower() for c in result.columns}
    missing = [c for c in layout.raw_columns if c.lower() not in returned]
    if missing:
        raise ValueError(
            f"{layout.name}: outfile query is missing expected columns {missing}; "
            f"verify {layout.model_file} layout on the target release"
        )
    return result.dicts()


def map_pgmref(result: QueryResult) -> list[tuple[Any, ...]]:
    rows = _row_map(result, PGMREF_LAYOUT)
    out = []
    for r in rows:
        out.append((
            _s(r["program_lib"]), _s(r["program_name"]),
            _s(r["object_lib"]), _s(r["object_name"]),
            _s(r["object_type"]), _s(r["usage_flag"]),
            _to_int(r["ref_count"]),
        ))
    return out


def map_dbr(result: QueryResult) -> list[tuple[Any, ...]]:
    rows = _row_map(result, DBR_LAYOUT)
    out = []
    for r in rows:
        out.append((
            _s(r["dep_lib"]), _s(r["dep_file"]),
            _s(r["based_lib"]), _s(r["based_file"]),
            _s(r["dep_type"]),
        ))
    return out


def map_ffd(result: QueryResult) -> list[tuple[Any, ...]]:
    rows = _row_map(result, FFD_LAYOUT)
    out = []
    for r in rows:
        out.append((
            _s(r["file_lib"]), _s(r["file_name"]), _s(r["record_format"]),
            _s(r["field_name"]), _s(r["field_type"]),
            _to_int(r["field_length"]), _to_int(r["field_scale"]),
            _s(r["field_text"]), _to_int(r["field_ordinal"]),
        ))
    return out


def _s(v: Any) -> str | None:
    if v is None:
        return None
    return str(v).strip()


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# --- Orchestration (host-touching) ------------------------------------------

PGMREF_COLUMNS = ["program_lib", "program_name", "object_lib", "object_name",
                  "object_type", "usage_flag", "ref_count"]
FFD_COLUMNS = ["file_lib", "file_name", "record_format", "field_name",
               "field_type", "field_length", "field_scale", "field_text",
               "field_ordinal"]
DBR_COLUMNS = ["dep_lib", "dep_file", "based_lib", "based_file", "dep_type"]


def harvest_pgmref(session: HostSession, con, config,
                   scratch_lib: str | None = None,
                   libraries: "Sequence[str] | None" = None,
                   resolution_cache: dict[str, str] | None = None,
                   progress: Progress | None = None
                   ) -> dict[str, int]:
    """DSPPGMREF PGM(lib/*ALL) per library — full-scope, always.

    ``libraries`` defaults to the configured scan list; :func:`harvest` passes
    one library at a time to preserve the per-library command interleaving.
    """
    from ..db import insert_rows

    p = progress or NULL
    scratch = scratch_lib or config.scratch_lib
    counts = {"raw_dsppgmref": 0}
    if resolution_cache is None:
        resolution_cache = {}
    # Libraries whose rows are already present are skipped — the same
    # idempotency rule as the per-file FFD/DBR pair-skipping, and what makes
    # `lineage extract --resume` cheap. (A library with zero programs leaves
    # no rows and is re-run on resume; DSPPGMREF over an empty library is
    # fast and harmless.)
    done_libs = {(r[0] or "").upper() for r in con.execute(
        "SELECT DISTINCT program_lib FROM raw_dsppgmref").fetchall()}
    for lib in (config.libraries if libraries is None else libraries):
        if lib.upper() in done_libs:
            continue
        pgm_of = f"{scratch}/PGMREF"
        p.start(f"DSPPGMREF {lib}/*ALL")
        session.run_cl(
            f"DSPPGMREF PGM({lib}/*ALL) OUTPUT(*OUTFILE) OUTFILE({pgm_of})"
        )
        res = _select_outfile(session, scratch, "PGMREF", PGMREF_LAYOUT,
                              resolution_cache)
        inserted = insert_rows(
            con, "raw_dsppgmref", PGMREF_COLUMNS, map_pgmref(res))
        counts["raw_dsppgmref"] += inserted
        p.done(f"DSPPGMREF {lib}/*ALL", rows=inserted)
    return counts


def _is_missing_library(exc: Exception) -> bool:
    """A per-file DSP command failed because the *library* does not exist.

    CPF3064 ("Library &1 not found") reaches us wrapped in a JDBC
    SQLException whose text carries the message id; CPF9810 is the same
    condition from other commands. When a whole library is dead, every file
    under it will fail identically — the caller caches the library and skips
    its remaining files instead of paying one failed host call each.
    """
    msg = str(exc).upper()
    return "CPF3064" in msg or "CPF9810" in msg


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)


def harvest_ffd(session: HostSession, con, config,
                files: "Sequence[tuple[str, str]] | None" = None,
                scratch_lib: str | None = None,
                libraries: "Sequence[str] | None" = None,
                resolution_cache: dict[str, str] | None = None,
                progress: Progress | None = None
                ) -> dict[str, int]:
    """DSPFFD outfile harvest.

    ``files=None`` (default) reproduces today's behavior: one
    ``DSPFFD FILE(lib/*ALL)`` per configured library. A ``files`` iterable of
    ``(library, file)`` pairs instead issues one ``DSPFFD FILE(lib/file)`` per
    pair — used by targeted extraction to scope the pull to a slice.

    Per-file mode filters the mapped rows down to the requested (library,
    file) before insert: the real host's outfile only contains that one file,
    but :class:`FixtureHostSession` serves the same canned response for every
    select, so without filtering this would insert duplicates. Pairs whose
    (library, file) already have rows in ``raw_dspffd`` are skipped, so
    repeat calls across rounds don't duplicate either.

    A per-file command failure (a slice-discovered library that doesn't
    actually exist — CPF3064 — or a file the profile can't describe) is
    counted and skipped, never fatal: the slice is *discovered* from parsed
    source and compiled references, so it can name phantom objects, and one
    of them must not kill a long extraction. Dead libraries are cached so a
    phantom library costs one failed host call, not one per file under it.
    The affected file surfaces downstream as a missing-columns gap.
    """
    from ..db import insert_rows

    p = progress or NULL
    scratch = scratch_lib or config.scratch_lib
    counts = {"raw_dspffd": 0}
    if resolution_cache is None:
        resolution_cache = {}
    if files is None:
        for lib in (config.libraries if libraries is None else libraries):
            ffd_of = f"{scratch}/FFD"
            p.start(f"DSPFFD {lib}/*ALL")
            session.run_cl(
                f"DSPFFD FILE({lib}/*ALL) OUTPUT(*OUTFILE) OUTFILE({ffd_of})"
            )
            res = _select_outfile(session, scratch, "FFD", FFD_LAYOUT,
                                  resolution_cache)
            inserted = insert_rows(
                con, "raw_dspffd", FFD_COLUMNS, map_ffd(res))
            counts["raw_dspffd"] += inserted
            p.done(f"DSPFFD {lib}/*ALL", rows=inserted)
        return counts

    already = _harvested_pairs(con, "raw_dspffd", "file_lib", "file_name")
    dead_libs: set[str] = set()
    failures = 0
    total = len(files)
    for i, (lib, file) in enumerate(files, start=1):
        p.tick("DSPFFD per-file", i, total)
        key = (lib.upper(), file.upper())
        if key in already:
            continue
        if key[0] in dead_libs:
            failures += 1
            continue
        ffd_of = f"{scratch}/FFD"
        try:
            session.run_cl(
                f"DSPFFD FILE({lib}/{file}) OUTPUT(*OUTFILE) OUTFILE({ffd_of})"
            )
            res = _select_outfile(session, scratch, "FFD", FFD_LAYOUT,
                                  resolution_cache)
        except OutfileShapeError:
            raise   # a layout mismatch is systemic, not per-file — fail loudly
        except Exception as exc:  # noqa: BLE001 - counted, never fatal
            failures += 1
            if _is_missing_library(exc):
                dead_libs.add(key[0])
                p.note(f"DSPFFD {lib}/{file} failed ({_first_line(exc)}) — "
                       f"library {lib} marked dead, skipping its other files")
            else:
                p.note(f"DSPFFD {lib}/{file} failed ({_first_line(exc)}) — "
                       "skipped")
            continue
        rows = [r for r in map_ffd(res)
                if ((r[0] or "").upper(), (r[1] or "").upper()) == key]
        counts["raw_dspffd"] += insert_rows(con, "raw_dspffd", FFD_COLUMNS, rows)
        already.add(key)
    if failures:
        counts["raw_dspffd_failures"] = failures
    return counts


def harvest_dbr(session: HostSession, con, config,
                files: "Sequence[tuple[str, str]] | None" = None,
                scratch_lib: str | None = None,
                libraries: "Sequence[str] | None" = None,
                resolution_cache: dict[str, str] | None = None,
                progress: Progress | None = None
                ) -> dict[str, int]:
    """DSPDBR outfile harvest — dependent (logical) -> based-on (physical).

    Same ``files=None`` vs. per-file scoping, dedup/filter rules, and
    per-file failure tolerance (dead-library cache included) as
    :func:`harvest_ffd`, filtered on the *dependent* (library, file).
    """
    from ..db import insert_rows

    p = progress or NULL
    scratch = scratch_lib or config.scratch_lib
    counts = {"raw_dspdbr": 0}
    if resolution_cache is None:
        resolution_cache = {}
    if files is None:
        for lib in (config.libraries if libraries is None else libraries):
            dbr_of = f"{scratch}/DBR"
            p.start(f"DSPDBR {lib}/*ALL")
            session.run_cl(
                f"DSPDBR FILE({lib}/*ALL) OUTPUT(*OUTFILE) OUTFILE({dbr_of})"
            )
            res = _select_outfile(session, scratch, "DBR", DBR_LAYOUT,
                                  resolution_cache)
            inserted = insert_rows(
                con, "raw_dspdbr", DBR_COLUMNS, map_dbr(res))
            counts["raw_dspdbr"] += inserted
            p.done(f"DSPDBR {lib}/*ALL", rows=inserted)
        return counts

    already = _harvested_pairs(con, "raw_dspdbr", "dep_lib", "dep_file")
    dead_libs: set[str] = set()
    failures = 0
    total = len(files)
    for i, (lib, file) in enumerate(files, start=1):
        p.tick("DSPDBR per-file", i, total)
        key = (lib.upper(), file.upper())
        if key in already:
            continue
        if key[0] in dead_libs:
            failures += 1
            continue
        dbr_of = f"{scratch}/DBR"
        try:
            session.run_cl(
                f"DSPDBR FILE({lib}/{file}) OUTPUT(*OUTFILE) OUTFILE({dbr_of})"
            )
            res = _select_outfile(session, scratch, "DBR", DBR_LAYOUT,
                                  resolution_cache)
        except OutfileShapeError:
            raise   # a layout mismatch is systemic, not per-file — fail loudly
        except Exception as exc:  # noqa: BLE001 - counted, never fatal
            failures += 1
            if _is_missing_library(exc):
                dead_libs.add(key[0])
                p.note(f"DSPDBR {lib}/{file} failed ({_first_line(exc)}) — "
                       f"library {lib} marked dead, skipping its other files")
            else:
                p.note(f"DSPDBR {lib}/{file} failed ({_first_line(exc)}) — "
                       "skipped")
            continue
        rows = [r for r in map_dbr(res)
                if ((r[0] or "").upper(), (r[1] or "").upper()) == key]
        counts["raw_dspdbr"] += insert_rows(con, "raw_dspdbr", DBR_COLUMNS, rows)
        already.add(key)
    if failures:
        counts["raw_dspdbr_failures"] = failures
    return counts


def _harvested_pairs(con, table: str, lib_col: str, name_col: str
                     ) -> set[tuple[str, str]]:
    rows = con.execute(
        f"SELECT DISTINCT {lib_col}, {name_col} FROM {table}").fetchall()
    return {((lib or "").upper(), (name or "").upper()) for lib, name in rows}


def harvest(session: HostSession, con, config, scratch_lib: str | None = None,
            progress: Progress | None = None) -> dict[str, int]:
    """Full-mode wrapper: DSPPGMREF/DSPFFD/DSPDBR *ALL per configured library.

    Returns a dict of raw table -> rows inserted. This (and the three
    per-command functions above) are the only functions here that issue host
    commands; everything else is pure. Existing callers/tests target this
    full-mode entry point unchanged; targeted extraction
    (:mod:`lineage.extract.targeted`) calls the three sub-functions directly
    with a ``files`` scope.
    """
    counts = {"raw_dsppgmref": 0, "raw_dspffd": 0, "raw_dspdbr": 0}
    # One layout-resolution cache for the whole run: the outfile shapes are
    # host-defined and identical across libraries, so each layout is probed
    # once, not once per library.
    cache: dict[str, str] = {}
    # Per library, PGMREF -> FFD -> DBR — the exact command order of the
    # original single-function harvest, so full mode stays byte-identical
    # on multi-library configs.
    for lib in config.libraries:
        for sub in (
            harvest_pgmref(session, con, config, scratch_lib=scratch_lib,
                           libraries=[lib], resolution_cache=cache,
                           progress=progress),
            harvest_ffd(session, con, config, scratch_lib=scratch_lib,
                        libraries=[lib], resolution_cache=cache,
                        progress=progress),
            harvest_dbr(session, con, config, scratch_lib=scratch_lib,
                        libraries=[lib], resolution_cache=cache,
                        progress=progress),
        ):
            for k, v in sub.items():
                counts[k] = counts.get(k, 0) + v
    return counts


def _select_outfile(session: HostSession, scratch: str, name: str,
                    layout: OutfileLayout,
                    resolution_cache: dict[str, str] | None = None
                    ) -> QueryResult:
    """Probe the outfile's actual columns, resolve the layout, then select.

    The probe (``SELECT * ... FETCH FIRST 1 ROWS ONLY``) and the data select
    both go through the same fixture tag (``with_tag`` applied to each query
    individually — :class:`~.connection.FixtureHostSession` consumes the tag
    once per call). ``resolution_cache``, when given, is keyed by
    ``layout.name`` and shared across a whole ``harvest_*`` call so per-file
    mode (many DSPFFD/DSPDBR invocations) probes once, not once per file.
    """
    tag = f"xref.{layout.name}"

    def _query(sql: str) -> QueryResult:
        # FixtureHostSession honours with_tag; real sessions ignore it.
        if hasattr(session, "with_tag"):
            return session.with_tag(tag).query(sql)
        return session.query(sql)

    if resolution_cache is not None and layout.name in resolution_cache:
        select_list = resolution_cache[layout.name]
    else:
        probe = _query(f"SELECT * FROM {scratch}.{name} FETCH FIRST 1 ROWS ONLY")
        select_list, _missing = layout.resolve(probe.columns)
        if resolution_cache is not None:
            resolution_cache[layout.name] = select_list

    return _query(f"SELECT {select_list} FROM {scratch}.{name}")
