# DB2 for i Lineage Analyzer

Determines, for each DB2 for i "output" file/table consumed by users, the
transitive set of **base physical files and columns** that feed it — traced
through CL programs, **RPG III** programs (RPGLE/SQLRPGLE also supported), DDS
logical files, and SQL views. The commonality analysis across outputs
identifies candidate foundational data products for Snowflake, so consumers
can be migrated off DB2 and the CL/RPG jobs retired.

## Design principles

1. **Two evidence layers, reconciled.** System cross-reference data
   (DSPPGMREF, DSPDBR, catalogs) is authoritative for compiled references but
   blind to OVRDBF redirection, dynamic SQL, and library-list resolution.
   Source parsing fills those gaps. Every lineage edge carries a provenance
   (`catalog|xref|source_cl|source_rpg|source_sql|dds`) and confidence
   (`confirmed|parsed|inferred|unresolved`) tag; source-parsed override edges
   supersede compiled references only when *every* observed call site carries
   the override.
2. **Extract once, analyze locally.** All host data lands in local
   DuckDB/Parquet on first pull. Parsers and graph builds never touch the LPAR.
3. **Don't fully interpret RPG.** Programs are classified by tractability
   (`sql_pure` / `ext_described_io` / `program_described_or_complex`) and
   lineage is extracted at the fidelity each class allows. The rest is flagged
   for human review, not guessed.
4. **Everything unresolved is a first-class output.** CHGVAR-built dynamic
   names, missing source members, out-of-scope references, and library-list
   ambiguity are reported with captured expressions, never silently dropped.

## Quick start (no IBM i needed)

Environments are managed with [uv](https://docs.astral.sh/uv/); the committed
`uv.lock` pins the full dependency set and `.python-version` pins Python 3.11.

```sh
uv sync                                       # creates .venv, installs project + dev deps
uv run pytest                                 # full suite, no host required

# Run the whole pipeline against the synthetic fixture estate:
uv run python scripts/make_fixture_host.py data/fixture-host
uv run lineage run --config config.fixture.yaml --fixture-dir data/fixture-host
open data/report/summary.html
```

For live-host extraction include the `host` extra (jaydebeapi/JPype):
`uv sync --extra host`. Without uv, `pip install -e . --group dev` still works.

## Against a live system

1. Place `jt400.jar` under `drivers/` and copy `config.example.yaml` to
   `config.yaml` (gitignored); fill in connection, scanned libraries, source
   files, output seeds, and assumed library lists. These are **configuration,
   not discovery**.
2. Ensure the configured `scratch_lib` exists and is writable — the
   DSPPGMREF/DSPDBR/DSPFFD command outfiles land there. Access is otherwise
   read-only; host commands run via `QSYS2.QCMDEXC` over the same JDBC
   connection. Source member text is read with `QSYS2.IFS_READ` by default
   (no temporary objects at all; needs IBM i 7.3 TR7 / 7.4+). IFS_READ
   reports a failed open as zero rows rather than an error — members of
   DDS/externally-described data PFs and CCSID-65535 source columns do this —
   so empty members automatically retry through the CREATE ALIAS path;
   set `source_retrieval: alias` to use aliases exclusively (older releases).
3. Probe the host first: `uv run lineage probe` detects the DB2/OS version
   (via JDBC `DatabaseMetaData` and `SYSIBMADM.ENV_SYS_INFO`), introspects
   which columns each QSYS2 catalog view actually has on that release/TR, and
   reports whether `QSYS2.IFS_READ` exists. Every catalog SELECT is then
   built from the host's real column set — synonym candidates cover renames
   across releases, missing optional columns are NULL-filled, and a missing
   required column fails with an explicit message instead of a generic
   column-not-found error. `uv run python scripts/smoke_host.py config.yaml`
   additionally checks QCMDEXC and a source-member CCSID round-trip; for the
   DSPPGMREF outfile it probes the outfile's actual columns, resolves our
   candidate field-name layout against them, and prints the per-field
   mapping (candidate columns used, any NULL-filled optionals) — a required
   field with no match fails the smoke run with the outfile's actual column
   list. `uv run lineage profile` measures
   catalog/source volumes and bottlenecks read-only (`QSYS2.OBJECT_STATISTICS`
   and catalog-view aggregates only, no CL, no DDL) and writes
   `data/profile_report.json` with recommendations on whether targeted
   extraction is needed before scoping a full pull.
4. On a large estate, set `extraction_scope: targeted` in `config.yaml` (or
   pass `lineage extract --scope targeted`) once the profile confirms the
   output seeds are DDL-only. Targeted extraction still runs `DSPPGMREF
   *ALL` per library (the one broad pull — it's how writers are found), but
   then iterates to closure over just the backward slice of the configured
   `output_seeds` plus their transitive callers: scoped SYSCOLUMNS/
   SYSPARTITIONSTAT, per-file DSPFFD/DSPDBR, and only the source members
   that slice actually needs (capped at 5 rounds). Every object pulled is
   recorded in the `slice_objects` table with the round, reason it entered
   the slice, and (when discovered) `source_ref` — where its source was
   found — so a targeted run is auditable. `extraction_scope: full` (the
   default) is unaffected either way.

   Targeted mode asks each slice program/file where its source actually
   lives (`QSYS2.OBJECT_STATISTICS`) instead of assuming the member name
   matches the object name — authoritative, and immune to member-name !=
   object-name mismatches. This means `source_files` becomes **optional**
   for targeted runs (still required, and validated at `extract` time, for
   `extraction_scope: full`); it's only used as a name-matching fallback for
   objects objstat can't place and for `/COPY`/`RUNSQLSTM` members. Separately,
   `library_discovery: slice` (the default) lets per-file pulls follow the
   slice into libraries outside the configured `libraries` scan list — set
   `library_discovery: none` to restore the strictly-configured restriction
   (slice objects outside `libraries` are still recorded for the audit trail,
   just never pulled). Broad `*ALL` commands never leave `libraries` in
   either mode.
5. Run the stages (each is re-runnable):

```sh
uv run lineage extract          # catalogs, DSPPGMREF/DSPDBR/DSPFFD, source members
uv run lineage parse            # DDS -> CL -> RPG -> embedded SQL -> classification
uv run lineage build --phase 3  # graph assembly (1 = xref only, 2 = +CL/DDS)
uv run lineage analyze          # per-output lineage, commonality, complexity
uv run lineage report           # CSV/Parquet/JSON exports + summary.html
```

### Tracing one table's columns

To trace the column-level lineage of a single selected output table — without
adding it to `output_seeds` — run, after `build` (phase 3):

```sh
uv run lineage trace-columns --config config.yaml --table APPLIB/MYTABLE
```

This assumes the target is a **DDL (SQL `CREATE TABLE`) table**: its column
list is read from the SQL catalog (`raw_syscolumns`), and each column is walked
backward along column `derives_from` edges to its base-table columns, rendered
as a human-readable tree with the provenance/confidence of every hop. Columns
with no resolvable upstream (constants, host-variable-fed inserts, unparsable
SQL) are listed as `no resolved lineage` rather than dropped. Pass `--out FILE`
to also save the tree. The table must already be extracted and built; for a
table outside the current scan, seed a targeted extraction on it first.

Set the password via `LINEAGE_DB_PASSWORD` rather than storing it in
`config.yaml`.

## Pipeline

| Stage | Modules | Output (DuckDB) |
|---|---|---|
| extract | `lineage.extract.{connection,catalog,xref,source}` | `raw_*` tables |
| parse | `lineage.parse.{dds,cl,rpg,embedded_sql,classify}` | `parsed_*`, `program_classification` |
| build | `lineage.graph.{model,build,resolve}` | `nodes`, `edges`, `gaps` |
| analyze | `lineage.analyze.{lineage,commonality,complexity,gaps}` | `output_lineage`, `commonality_matrix`, `complexity_scores` |
| report | `lineage.report.{export,html}` | `data/report/*` |

Key behaviors:

- **Outfile layouts are mapped by field name, never position**
  (`extract/xref.py`); a mismatched release fails loudly.
- **OVRDBF scoping is simulated** down the CL call tree (`*CALLLVL` frames,
  `*JOB` map, `DLTOVR`), and the effective override map at each call site
  redirects the called program's file references. A program called both with
  and without an override keeps both resolutions.
- **RPG III columns**: F-spec name 7–14, file format E/F col 19 (program
  described ⇒ complex), C-spec opcode 28–32 (`UPDAT`/`DELET`/`EXCPT`
  spellings), `C/EXEC SQL` + `C+` continuations, `/COPY QRPGSRC,MBR`.
  Member types `RPGLE`/`SQLRPGLE` switch to RPG IV/free-format rules.
- **Column lineage** comes from SQL (sqlglot; DB2-isms like `WITH UR`
  stripped first), DDS field mappings (RENAME/CONCAT/JREF), and — for
  `ext_described_io` programs — same-named-field record expansion tagged
  `inferred`.
- **Column usage** (`output_lineage.relation = 'used'`, distinct from the
  `'derives'` rows above): which base-file columns a program actually reads,
  not just which columns map into an output. Sourced from RPG C-spec
  factor1/factor2/result and O-spec field-entry tokens intersected against a
  read file's DSPFFD fields (`parsed`; falls back to every field of the file
  when nothing intersects, `inferred`), from every column referenced anywhere
  in a SQL statement — not just the select list — and from CPYF
  FROMFILE∩TOFILE field-name overlap (`parsed` under `FMTOPT(*MAP)`, else
  `inferred`). This feeds an outputs × source-columns view of the
  commonality matrix, sharpening data-product candidate sizing. Full
  source→target field-flow mapping (MOVE/EVAL dataflow, I-spec renames,
  O-spec output boundaries) is deliberately out of scope — it needs RPG logic
  interpretation the rest of this tool avoids; usage analysis is the
  lower-lift alternative and a documented future enhancement.
- **Complexity buckets** per output: `replicate_as_view` / `moderate` /
  `full_reengineering` from path depth, complex programs en route, override
  edges, and unresolved edges.

## Testing

No live-host dependency: parsers and graph logic are tested against a
synthetic RPG III estate under `tests/fixtures/source/` served through
`FixtureHostSession`. Golden-file tests pin expected `output_lineage` rows;
property tests cover override-scope determinism, LF-flattening idempotence,
and the no-edge-without-provenance invariant. The one live-host script
(`scripts/smoke_host.py`) is excluded from CI.
