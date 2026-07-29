"""Cross-reference harvest: DSPPGMREF / DSPDBR / DSPFFD outfiles.

These ``DSP*`` commands write to database outfiles whose record layouts are
IBM-defined model files (``QADSPPGM``/``QWHDRPPR``, ``QADSPDBR``/``QWHDRDBR``,
``QADSPFFD``/``QWHDRFFD``). We map those layouts by **field name, never by
position**: each map below lists the outfile fields we select and the raw
column they land in. If a target release renames or omits a field, the SELECT
fails loudly rather than silently shifting columns — verify against
``DSPFFD FILE(QSYS/QADSPPGM)`` on the target system.

The mapping functions (``map_*``) are pure and unit-tested against fixture
rows; orchestration (``harvest``) is the only part that touches the host.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .connection import HostSession, QueryResult


@dataclass(frozen=True)
class OutfileLayout:
    """One outfile field layout: model file/format and the field->column map."""

    name: str
    model_file: str      # e.g. QSYS/QADSPPGM
    record_format: str   # e.g. QWHDRPPR
    # ordered: outfile field name -> our raw column name
    fields: tuple[tuple[str, str], ...]

    @property
    def outfile_fields(self) -> tuple[str, ...]:
        return tuple(f for f, _ in self.fields)

    @property
    def raw_columns(self) -> tuple[str, ...]:
        return tuple(c for _, c in self.fields)

    def select_list(self) -> str:
        # Alias each outfile field to its raw column so downstream code reads
        # by our stable names regardless of the model field name.
        return ", ".join(f"{fld} AS {col}" for fld, col in self.fields)


# --- DSPPGMREF: program -> referenced object (compiled references) -----------
PGMREF_LAYOUT = OutfileLayout(
    name="dsppgmref",
    model_file="QSYS/QADSPPGM",
    record_format="QWHDRPPR",
    fields=(
        ("WHLIB", "program_lib"),
        ("WHPNAM", "program_name"),
        ("WHLNAM", "object_lib"),
        ("WHFNAM", "object_name"),
        ("WHOTYP", "object_type"),
        ("WHFUSG", "usage_flag"),
        ("WHNCNT", "ref_count"),
    ),
)

# --- DSPDBR: dependent (logical) -> based-on (physical) ----------------------
DBR_LAYOUT = OutfileLayout(
    name="dspdbr",
    model_file="QSYS/QADSPDBR",
    record_format="QWHDRDBR",
    fields=(
        ("WHRELI", "dep_lib"),
        ("WHREFI", "dep_file"),
        ("WHLIB", "based_lib"),
        ("WHFILE", "based_file"),
        ("WHRTYP", "dep_type"),
    ),
)

# --- DSPFFD: field descriptions per file/record format -----------------------
FFD_LAYOUT = OutfileLayout(
    name="dspffd",
    model_file="QSYS/QADSPFFD",
    record_format="QWHDRFFD",
    fields=(
        ("WHLIB", "file_lib"),
        ("WHFILE", "file_name"),
        ("WHNAME", "record_format"),
        ("WHFLDE", "field_name"),
        ("WHFLDT", "field_type"),
        ("WHFLDB", "field_length"),
        ("WHFLDP", "field_scale"),
        ("WHFTXT", "field_text"),
        ("WHFLDN", "field_ordinal"),
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
    """Validate that the query returned our raw columns, return list of dicts."""
    missing = [c for c in layout.raw_columns if c not in result.columns]
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
                   scratch_lib: str | None = None) -> dict[str, int]:
    """DSPPGMREF PGM(lib/*ALL) per configured library — full-scope, always."""
    from ..db import insert_rows

    scratch = scratch_lib or config.scratch_lib
    counts = {"raw_dsppgmref": 0}
    for lib in config.libraries:
        pgm_of = f"{scratch}/PGMREF"
        session.run_cl(
            f"DSPPGMREF PGM({lib}/*ALL) OUTPUT(*OUTFILE) OUTFILE({pgm_of})"
        )
        res = _select_outfile(session, scratch, "PGMREF", PGMREF_LAYOUT)
        counts["raw_dsppgmref"] += insert_rows(
            con, "raw_dsppgmref", PGMREF_COLUMNS, map_pgmref(res))
    return counts


def harvest_ffd(session: HostSession, con, config,
                files: "Sequence[tuple[str, str]] | None" = None,
                scratch_lib: str | None = None) -> dict[str, int]:
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
    """
    from ..db import insert_rows

    scratch = scratch_lib or config.scratch_lib
    counts = {"raw_dspffd": 0}
    if files is None:
        for lib in config.libraries:
            ffd_of = f"{scratch}/FFD"
            session.run_cl(
                f"DSPFFD FILE({lib}/*ALL) OUTPUT(*OUTFILE) OUTFILE({ffd_of})"
            )
            res = _select_outfile(session, scratch, "FFD", FFD_LAYOUT)
            counts["raw_dspffd"] += insert_rows(
                con, "raw_dspffd", FFD_COLUMNS, map_ffd(res))
        return counts

    already = _harvested_pairs(con, "raw_dspffd", "file_lib", "file_name")
    for lib, file in files:
        key = (lib.upper(), file.upper())
        if key in already:
            continue
        ffd_of = f"{scratch}/FFD"
        session.run_cl(
            f"DSPFFD FILE({lib}/{file}) OUTPUT(*OUTFILE) OUTFILE({ffd_of})"
        )
        res = _select_outfile(session, scratch, "FFD", FFD_LAYOUT)
        rows = [r for r in map_ffd(res)
                if ((r[0] or "").upper(), (r[1] or "").upper()) == key]
        counts["raw_dspffd"] += insert_rows(con, "raw_dspffd", FFD_COLUMNS, rows)
        already.add(key)
    return counts


def harvest_dbr(session: HostSession, con, config,
                files: "Sequence[tuple[str, str]] | None" = None,
                scratch_lib: str | None = None) -> dict[str, int]:
    """DSPDBR outfile harvest — dependent (logical) -> based-on (physical).

    Same ``files=None`` vs. per-file scoping and dedup/filter rules as
    :func:`harvest_ffd`, filtered on the *dependent* (library, file).
    """
    from ..db import insert_rows

    scratch = scratch_lib or config.scratch_lib
    counts = {"raw_dspdbr": 0}
    if files is None:
        for lib in config.libraries:
            dbr_of = f"{scratch}/DBR"
            session.run_cl(
                f"DSPDBR FILE({lib}/*ALL) OUTPUT(*OUTFILE) OUTFILE({dbr_of})"
            )
            res = _select_outfile(session, scratch, "DBR", DBR_LAYOUT)
            counts["raw_dspdbr"] += insert_rows(
                con, "raw_dspdbr", DBR_COLUMNS, map_dbr(res))
        return counts

    already = _harvested_pairs(con, "raw_dspdbr", "dep_lib", "dep_file")
    for lib, file in files:
        key = (lib.upper(), file.upper())
        if key in already:
            continue
        dbr_of = f"{scratch}/DBR"
        session.run_cl(
            f"DSPDBR FILE({lib}/{file}) OUTPUT(*OUTFILE) OUTFILE({dbr_of})"
        )
        res = _select_outfile(session, scratch, "DBR", DBR_LAYOUT)
        rows = [r for r in map_dbr(res)
                if ((r[0] or "").upper(), (r[1] or "").upper()) == key]
        counts["raw_dspdbr"] += insert_rows(con, "raw_dspdbr", DBR_COLUMNS, rows)
        already.add(key)
    return counts


def _harvested_pairs(con, table: str, lib_col: str, name_col: str
                     ) -> set[tuple[str, str]]:
    rows = con.execute(
        f"SELECT DISTINCT {lib_col}, {name_col} FROM {table}").fetchall()
    return {((lib or "").upper(), (name or "").upper()) for lib, name in rows}


def harvest(session: HostSession, con, config, scratch_lib: str | None = None) -> dict[str, int]:
    """Full-mode wrapper: DSPPGMREF/DSPFFD/DSPDBR *ALL per configured library.

    Returns a dict of raw table -> rows inserted. This (and the three
    per-command functions above) are the only functions here that issue host
    commands; everything else is pure. Existing callers/tests target this
    full-mode entry point unchanged; targeted extraction
    (:mod:`lineage.extract.targeted`) calls the three sub-functions directly
    with a ``files`` scope.
    """
    counts: dict[str, int] = {}
    counts.update(harvest_pgmref(session, con, config, scratch_lib=scratch_lib))
    counts.update(harvest_ffd(session, con, config, scratch_lib=scratch_lib))
    counts.update(harvest_dbr(session, con, config, scratch_lib=scratch_lib))
    return counts


def _select_outfile(session: HostSession, scratch: str, name: str,
                    layout: OutfileLayout) -> QueryResult:
    sql = f"SELECT {layout.select_list()} FROM {scratch}.{name}"
    tag = f"xref.{layout.name}"
    # FixtureHostSession honours with_tag; real sessions ignore it.
    if hasattr(session, "with_tag"):
        return session.with_tag(tag).query(sql)
    return session.query(sql)
