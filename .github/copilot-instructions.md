# Copilot instructions — DB2 for i Lineage Analyzer

Python 3.11 toolchain that traces DB2 for i output files/tables back to the
base physical files and columns that feed them, through CL programs, RPG III
source, DDS logical files, and SQL views. Results drive commonality analysis
to propose foundational data products for a Snowflake migration.

## Environment and commands

- Environments are managed with **uv**. Use `uv sync` to set up,
  `uv run pytest` for tests, `uv run lineage <command>` for the CLI.
  Never `pip install` into the repo environment; add dependencies to
  `pyproject.toml` and run `uv lock`.
- Dev-only tools go in `[dependency-groups].dev`; live-host-only dependencies
  (jaydebeapi/JPype) belong in the `host` extra.
- Full pipeline without any IBM i:
  `uv run python scripts/make_fixture_host.py data/fixture-host` then
  `uv run lineage run --config config.fixture.yaml --fixture-dir data/fixture-host`.

## Architecture (strict layering)

Pipeline stages, each writing one DuckDB layer (schema in
`src/lineage/schema/schema.sql`, layer resets in `src/lineage/db.py`):

1. `lineage.extract.*` → `raw_*` tables — verbatim host pulls, never
   interpreted.
2. `lineage.parse.*` → `parsed_*`, `program_classification` — pure parsers
   over `SourceMember` (`parse/base.py`); orchestration `parse_all(con)`
   functions read raw and write parsed.
3. `lineage.graph.*` → `nodes`, `edges`, `gaps` — assembly and resolution.
4. `lineage.analyze.*` → `output_lineage`, `commonality_matrix`,
   `complexity_scores` — plus the coverage report.
5. `lineage.report.*` — CSV/Parquet/JSON exports and a self-contained HTML
   summary (inline CSS, no external assets).

Rules that must hold:

- **Only `lineage.extract.*` touches the host**, and only through the
  `HostSession` protocol (`extract/connection.py`). Parsers, graph, analysis,
  and reports read DuckDB only. Host access is read-only; host commands go
  through `QSYS2.QCMDEXC`; outfiles land in the configured scratch library.
  Source member text retrieval defaults to `source_retrieval: auto` — the
  probe picks IFS_READ when the host has it, alias otherwise. IFS_READ
  signals a failed open as zero rows + a job-log warning, not an SQL error
  (data-PF members, CCSID 65535), so an empty result auto-falls-back to the
  `alias` strategy (CREATE/DROP ALIAS in scratch_lib) per member; `alias`
  mode uses aliases exclusively for pre-7.3-TR7 releases. Both fetch under
  the same fixture tag.
- Libraries, output seeds, and library lists are **configuration, not
  discovery** (`config.yaml`, loaded by `lineage/config.py`). Never infer
  them from the host. Source *locations* are the one exception: targeted
  mode asks each slice program/file where its source actually lives
  (`QSYS2.OBJECT_STATISTICS`, `extract/objinfo.py`) rather than assuming the
  member name matches the object name, so `source_files` is optional for
  targeted mode (still required, validated at `extract` time, for
  `extraction_scope: full`).
- Outfile record layouts (DSPPGMREF/DSPDBR/DSPFFD in `extract/xref.py`) are
  mapped **by field name, never by position**, the same adaptive way QSYS2
  catalog columns are (see below): each field declares an ordered candidate
  list of model-file column names (e.g. DSPFFD's field name is `WHFLDI` on
  some releases, `WHFLDE` on others; DSPPGMREF has no ref-count field at
  all) plus a required flag, resolved against the outfile's *actual*
  columns probed at run time (`SELECT * ... FETCH FIRST 1 ROWS ONLY`) —
  never a hardcoded, unverified list. Missing optional fields NULL-fill; a
  missing required field raises `OutfileShapeError` naming the outfile and
  listing its actual columns, rather than shifting columns.
- QSYS2 catalog SELECTs are **capability-driven**, never hardcoded: the host
  probe (`extract/hostinfo.py`, run at the start of `extract` and by
  `lineage probe`) records the DB2/OS version (JDBC `DatabaseMetaData` +
  `SYSIBMADM.ENV_SYS_INFO`), introspects each QSYS2 view's actual columns
  from `QSYS2.SYSCOLUMNS`, and checks for `QSYS2.IFS_READ`. Catalog pulls in
  `extract/catalog.py` declare per-column candidate-name lists (synonyms
  across releases/TRs) with required flags — optional misses NULL-fill,
  required misses raise `CatalogShapeError`. When adding a catalog column,
  add it as a candidate list, not a bare name (e.g. member name is
  `TABLE_PARTITION`, view deps use `OBJECT_SCHEMA`/`OBJECT_NAME`, SYSTABLES
  has no row-count column).
- Profiling (`extract/profiler.py`, `lineage profile`) is strictly read-only
  aggregates — `COUNT`/`SUM`/`GROUP BY` over `QSYS2.OBJECT_STATISTICS` and
  catalog views only. Keep it free of `run_cl` and DDL; it runs ahead of
  `extract` to size the estate, not to change anything on the host.
- `extraction_scope: targeted` (`extract/targeted.py`, `lineage extract
  --scope targeted`) must only ever *narrow* what full mode would pull —
  never issue a host command or SELECT that full mode wouldn't. It computes
  the backward slice of the configured `output_seeds` (plus their
  transitive callers) and iterates scoped catalog/xref/source pulls to
  closure; every object it pulls is recorded in `slice_objects` (kind,
  library, name, round, reason, source_ref) as the auditable record of why
  and (when discovered via objstat) where. `full` remains the default;
  `xref.harvest`/`catalog.harvest` full-mode behavior must stay
  byte-identical to before targeted mode existed. Per-file pulls
  (DSPFFD/DSPDBR, scoped catalog SELECTs) follow the slice into libraries
  outside the configured `libraries` when `library_discovery: slice` (the
  default); `library_discovery: none` restores the strictly-configured
  restriction. Broad `*ALL` commands (DSPPGMREF) never leave `libraries` in
  either mode — the design principle "configuration, not discovery" still
  governs which libraries the broad scan touches; only per-object source
  *location* is discovered.

## Lineage semantics

- Every edge carries `provenance` (`catalog|xref|source_cl|source_rpg|`
  `source_sql|dds`) and `confidence` (`confirmed|parsed|inferred|unresolved`)
  — both are mandatory dataclass fields on `graph/model.py:Edge`. Never add
  an edge without them.
- Two evidence layers: xref/catalog data is authoritative for compiled
  references; source-parsed evidence fills gaps (OVRDBF, dynamic SQL,
  liblist). An OVRDBF override **supersedes a compiled xref edge only when
  every observed call site carries the override**; mixed call sites keep both
  resolutions (`graph/build.py`).
- Anything runtime-dependent (CHGVAR-built names, PREPARE/EXECUTE IMMEDIATE)
  is recorded in `gaps` with the captured expression and confidence
  `unresolved` — never guessed, never silently dropped. Gap kinds are
  distinct: `unresolved_dynamic_name` ≠ `outside_scope` ≠ `missing_source`.
- Do not interpret RPG logic. Programs are classified
  (`sql_pure` / `ext_described_io` / `program_described_or_complex` in
  `parse/classify.py`) and lineage fidelity follows the class; complex
  programs contribute table-level edges only. Same-name field expansion for
  externally described I/O is tagged `inferred`, never `parsed`.
- Column usage (program actually reads column X, vs. "column X maps into
  output Y") is a separate relation from field-flow mapping: `EdgeKind.READS`
  edges from `program:*` to `column:*` (provenance `source_rpg` /
  `source_sql` / `source_cl`), built by `graph/build.py::_column_usage_edges`
  from pure identifier-token harvesting (RPG C/O-spec, every SQL column
  reference, CPYF FROMFILE∩TOFILE) — never RPG dataflow interpretation.
  `confidence=parsed` only when a real field name was seen in the source;
  `inferred` is the record-level fallback (every field of a file the program
  reads). RPG field references are program-wide, not per-file: RPG III
  same-named fields across read files share one variable, so a referenced
  name yields a `parsed` edge on *every* read file whose format carries it
  (each file's column genuinely feeds the shared variable), and the
  remaining fields of each read file always keep the `inferred` fallback —
  a partial intersection must never suppress them. `output_lineage.relation`
  distinguishes the two views: `'derives'` (the pre-existing target-mapping
  rows) vs `'used'` (read en route to an output, not necessarily mapped into
  it) — `gaps.py::coverage` and the file-level commonality matrix stay scoped
  to `'derives'` so usage evidence never flips a resolved/partial status.

## RPG dialect

The estate is **RPG III** (member types RPG/RPG38/RPT/SQLRPG) — this is the
default parsing mode in `parse/rpg.py`:

- F-spec: file name cols 7–14, file type col 15, file format col 19
  (`E` externally described, `F` program described ⇒ complex). No EXTNAME
  keyword — redirection happens via OVRDBF at run time.
- C-spec: opcode cols 28–32, factor 2 cols 33–42; RPG III spellings
  `UPDAT`, `DELET`, `EXCPT`, `REDPE`.
- Embedded SQL: `C/EXEC SQL` … `C+` continuations … `C/END-EXEC`;
  `/COPY` syntax is `[lib/]srcfile,member`.
- Member types RPGLE/SQLRPGLE switch to RPG IV fixed-format columns and
  free-format `DCL-F`.

SQL parsing uses sqlglot's **generic dialect** (there is no `db2` dialect);
DB2-isms (`WITH UR`, `OPTIMIZE FOR n ROWS`, `FOR FETCH ONLY`, …) are stripped
first in `parse/embedded_sql.py`. SQL parse failures are recorded per
statement in `parsed_sql_statements.parse_error` and must never abort a run.

## Testing

- **No live-host dependency in tests.** The synthetic RPG III estate lives in
  `tests/fixtures/source/` (fixed-column source members — preserve column
  alignment exactly when editing) and is served via `FixtureHostSession`
  wired up in `tests/conftest.py` (fixtures: `config`, `session`, `con`,
  `extracted`, `parsed`, `built`).
- Parser unit tests build members with `conftest.make_member`; golden
  end-to-end expectations are pinned in `tests/test_end_to_end.py`.
- Invariants covered by tests that must keep holding: no edge without
  provenance/confidence; LF flattening idempotent; override simulation
  deterministic; every output seed lands in exactly one of
  {resolved, partially_resolved, unresolved}.
- `scripts/smoke_host.py` is the only live-host code path and is excluded
  from CI.

## Conventions

- Node ids: `file:LIB/NAME`, `file:LIB/NAME(MBR)`, `program:LIB/NAME`,
  `column:LIB/FILE.FIELD`; unqualified library renders as `*LIBL`. Always
  build them via the helpers in `graph/model.py`.
- DuckDB SQL + pyarrow only — no pandas.
- Object names are uppercased at parse/build boundaries; source text is kept
  verbatim in `raw_source_members`.
- Secrets: passwords come from `LINEAGE_DB_PASSWORD`, never committed config.
  `config.yaml` is gitignored; `config.example.yaml` documents the shape.
