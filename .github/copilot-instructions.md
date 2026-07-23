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
- Libraries, output seeds, and library lists are **configuration, not
  discovery** (`config.yaml`, loaded by `lineage/config.py`). Never infer
  them from the host.
- Outfile record layouts (DSPPGMREF/DSPDBR/DSPFFD in `extract/xref.py`) are
  mapped **by field name, never by position**; a missing field must raise,
  not shift columns.

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
