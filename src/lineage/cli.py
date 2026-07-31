"""Typer CLI: extract / parse / build / analyze / report.

Each command is runnable end-to-end at whatever fidelity exists so far; later
stages tolerate missing earlier layers (they just produce less).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from . import db as dbmod
from .config import Config, load_config

app = typer.Typer(help="DB2 for i lineage analyzer", no_args_is_help=True)

_CONFIG_OPT = typer.Option("config.yaml", "--config", "-c",
                           help="Path to config.yaml")


def _load(config_path: str) -> Config:
    return load_config(config_path)


def _con(cfg: Config):
    return dbmod.connect(cfg.duckdb_path)


def _echo_counts(counts: dict) -> None:
    for k, v in counts.items():
        typer.echo(f"  {k}: {v}")


@app.command()
def extract(config: str = _CONFIG_OPT,
            fixture_dir: Optional[str] = typer.Option(
                None, help="Serve host responses from a fixture directory "
                           "instead of connecting to the LPAR"),
            scope: Optional[str] = typer.Option(
                None, help="Extraction scope: full|targeted "
                           "(default: config's extraction_scope)"),
            resume: bool = typer.Option(
                False, "--resume",
                help="Keep the existing raw layer instead of resetting it: "
                     "already-harvested libraries, (library, file) pairs, "
                     "and source members are skipped, so a crashed or "
                     "killed targeted run continues where it left off. "
                     "Targeted scope only.")) -> None:
    """Pull catalogs, cross-references, and source members into the raw store."""
    cfg = _load(config)
    con = _con(cfg)
    from .config import EXTRACTION_SCOPES
    resolved_scope = (scope or cfg.extraction_scope).lower()
    if resolved_scope not in EXTRACTION_SCOPES:
        raise typer.BadParameter(
            f"--scope must be one of {EXTRACTION_SCOPES}, got '{resolved_scope}'")
    if resume and resolved_scope != "targeted":
        raise typer.BadParameter(
            "--resume requires targeted scope (full-mode source retrieval "
            "has no per-member skip and would duplicate rows)")
    if resolved_scope == "full" and not cfg.source_files:
        raise typer.BadParameter(
            "full extraction requires source_files; targeted mode can "
            "discover them")
    if fixture_dir:
        from .extract.connection import FixtureHostSession
        session = FixtureHostSession(fixture_dir=fixture_dir)
    else:
        from .extract.connection import open_session
        session = open_session(cfg.connection)
    # Live progress goes to stderr so the stdout count summary stays clean
    # and scriptable; on a multi-minute host run this is the only sign of
    # life until the final counts.
    from .extract.progress import Progress
    prog = Progress(echo=lambda s: typer.echo(s, err=True))
    try:
        from .extract import catalog, hostinfo, source, targeted, xref
        if resume:
            typer.echo("resume: keeping existing raw layer — "
                       "already-harvested work will be skipped")
        else:
            dbmod.reset_layer(con, "raw")
        profile = hostinfo.probe(session)
        profile.save(con)
        typer.echo(f"host: {profile.version_label} "
                   f"(IFS_READ={'yes' if profile.has_ifs_read else 'no'}, "
                   f"source strategy="
                   f"{source.resolve_strategy(cfg, profile)})")
        typer.echo(f"scope: {resolved_scope}")
        if resolved_scope == "targeted":
            counts = targeted.harvest_targeted(session, con, cfg, profile,
                                               progress=prog)
            _echo_counts(counts)
        else:
            prog.phase("catalog")
            catalog_counts = catalog.harvest(session, con, cfg, profile,
                                             progress=prog)
            prog.phase("xref")
            xref_counts = xref.harvest(session, con, cfg, progress=prog)
            prog.phase("source")
            source_counts = source.harvest(session, con, cfg, profile,
                                           progress=prog)
            typer.echo("catalog:")
            _echo_counts(catalog_counts)
            typer.echo("xref:")
            _echo_counts(xref_counts)
            typer.echo("source:")
            _echo_counts(source_counts)
        if not source.verify_roundtrip(con):
            typer.secho(
                "WARNING: no non-blank source lines retrieved — possible "
                "CCSID/translation problem", fg=typer.colors.YELLOW)
    finally:
        prog.close()
        session.close()
        con.close()


@app.command()
def profile(config: str = _CONFIG_OPT,
           fixture_dir: Optional[str] = typer.Option(
               None, help="Serve host responses from a fixture directory "
                          "instead of connecting to the LPAR"),
           out: str = typer.Option("data/profile_report.json",
                                   help="Where to write the JSON report")
           ) -> None:
    """Measure catalog/source volumes read-only, ahead of a full extract."""
    cfg = _load(config)
    if fixture_dir:
        from .extract.connection import FixtureHostSession
        session = FixtureHostSession(fixture_dir=fixture_dir)
    else:
        from .extract.connection import open_session
        session = open_session(cfg.connection)
    try:
        from .extract.profiler import profile_host, recommend
        report = profile_host(session, cfg)

        typer.echo("library objects:")
        for lib, by_type in sorted(report.library_objects.items()):
            for otype, counts in sorted(by_type.items()):
                total = sum(counts.values())
                typer.echo(f"  {lib} {otype}: {total} "
                           f"({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})")

        typer.echo("catalog rows:")
        for view, count in sorted(report.catalog_rows.items()):
            typer.echo(f"  {view}: {count}")

        typer.echo("source volumes:")
        for key, vol in sorted(report.source_volumes.items()):
            typer.echo(f"  {key}: {vol['members']} members, "
                       f"{vol['total_lines']} lines, "
                       f"{vol['total_bytes']} bytes")

        typer.echo("output seeds:")
        for seed_id, info in sorted(report.seed_classes.items()):
            line = (f"  {seed_id}: {info['library']}/{info['file']} -> "
                   f"{info['classification']}")
            if info["classification"] != "ddl_table":
                typer.secho(line, fg=typer.colors.YELLOW)
            else:
                typer.echo(line)

        slow = sorted(report.timings.items(), key=lambda kv: kv[1],
                     reverse=True)[:5]
        if slow:
            typer.echo("slowest probes:")
            for tag, secs in slow:
                typer.echo(f"  {tag}: {secs:.2f}s")

        if report.errors:
            typer.secho("probe errors:", fg=typer.colors.YELLOW)
            for tag, msg in sorted(report.errors.items()):
                typer.secho(f"  {tag}: {msg}", fg=typer.colors.YELLOW)

        typer.echo("recommendations:")
        for line in recommend(report, cfg):
            typer.echo(f"- {line}")

        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report.to_json(), encoding="utf-8")
        typer.echo(f"report written to {out_path}")
    finally:
        session.close()


@app.command()
def probe(config: str = _CONFIG_OPT) -> None:
    """Detect the host's DB2/OS version and catalog capabilities."""
    cfg = _load(config)
    con = _con(cfg)
    from .extract import hostinfo, source
    from .extract.connection import open_session
    session = open_session(cfg.connection)
    try:
        prof = hostinfo.probe(session)
        prof.save(con)
        typer.echo(f"  version: {prof.version_label}")
        if prof.product_version:
            typer.echo(f"  jdbc product version: {prof.product_version}")
        typer.echo(f"  QSYS2.IFS_READ available: {prof.has_ifs_read}")
        typer.echo(f"  source retrieval (mode={cfg.source_retrieval}): "
                   f"{source.resolve_strategy(cfg, prof)}")
        for view in sorted(prof.catalog_columns):
            typer.echo(f"  QSYS2.{view}: "
                       f"{len(prof.catalog_columns[view])} columns")
        if not prof.catalog_columns:
            typer.secho("  WARNING: catalog introspection returned nothing — "
                        "SELECTs will use documented default column names",
                        fg=typer.colors.YELLOW)
    finally:
        session.close()
        con.close()


@app.command()
def parse(config: str = _CONFIG_OPT) -> None:
    """Parse retrieved source members (DDS, CL, RPG, embedded SQL)."""
    cfg = _load(config)
    con = _con(cfg)
    try:
        from .parse import cl, classify, dds, embedded_sql, rpg
        dbmod.reset_layer(con, "parsed")
        typer.echo("dds:")
        _echo_counts(dds.parse_all(con))
        typer.echo("cl:")
        _echo_counts(cl.parse_all(con))
        typer.echo("rpg:")
        _echo_counts(rpg.parse_all(con))
        typer.echo("sql:")
        _echo_counts(embedded_sql.parse_all(con))
        typer.echo("classification:")
        _echo_counts(classify.classify_all(con))
    finally:
        con.close()


@app.command()
def build(config: str = _CONFIG_OPT,
          phase: int = typer.Option(3, help="1=xref/catalog only, "
                                            "2=+CL/DDS, 3=full")) -> None:
    """Assemble the lineage graph from raw + parsed layers."""
    cfg = _load(config)
    con = _con(cfg)
    try:
        from .graph.build import build_graph
        g = build_graph(con, cfg, phase=phase)
        typer.echo(f"  nodes: {g.number_of_nodes()}")
        typer.echo(f"  edges: {g.number_of_edges()}")
        n_gaps = dbmod.table_count(con, "gaps")
        typer.echo(f"  gaps: {n_gaps}")
    finally:
        con.close()


@app.command()
def analyze(config: str = _CONFIG_OPT) -> None:
    """Compute per-output lineage, commonality, and complexity."""
    cfg = _load(config)
    con = _con(cfg)
    try:
        from .analyze import commonality, complexity, lineage
        from .graph.build import load_graph
        g = load_graph(con)
        typer.echo("lineage:")
        _echo_counts(lineage.compute_output_lineage(con, g, cfg))
        typer.echo("commonality:")
        _echo_counts(commonality.analyze(con))
        typer.echo("complexity:")
        _echo_counts(complexity.score(con, g, cfg))
    finally:
        con.close()


@app.command()
def trace_columns(config: str = _CONFIG_OPT,
                  table: str = typer.Option(
                      ..., "--table", "-t",
                      help="Target table as LIB/NAME (a DDL table). Its "
                           "column lineage is traced from the built graph."),
                  out: Optional[str] = typer.Option(
                      None, help="Also write the rendered tree to this "
                                 "file")) -> None:
    """Trace column-level lineage of one selected output table as a tree.

    Reads the already-built graph, so run `build` (phase 3) first. The target's
    columns come from the SQL catalog (raw_syscolumns); each is traced backward
    along column derives_from edges to its base-table columns.
    """
    cfg = _load(config)
    con = _con(cfg)
    try:
        from .analyze import column_trace
        from .graph.build import load_graph
        g = load_graph(con)
        try:
            target = column_trace.resolve_target(con, table)
        except ValueError as exc:
            raise typer.BadParameter(str(exc))
        text = column_trace.render_forest(target, g)
        typer.echo(text)
        if out:
            out_path = Path(out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            typer.echo(f"  written to {out_path}")
    finally:
        con.close()


@app.command()
def report(config: str = _CONFIG_OPT,
           out: str = typer.Option("data/report", help="Output directory"),
           fmt: str = typer.Option("csv", help="Table export format: csv|parquet")) -> None:
    """Export tables and render the HTML summary report."""
    cfg = _load(config)
    con = _con(cfg)
    try:
        from .analyze.gaps import coverage
        from .graph.build import load_graph
        from .report import export, html as html_report
        cov = coverage(con, cfg)
        out_dir = Path(out)
        written = export.export_tables(con, out_dir, fmt=fmt)
        g = load_graph(con)
        written += export.export_output_graphs(con, g, cfg, out_dir / "graphs")
        (out_dir / "coverage.json").write_text(
            json.dumps(cov, indent=2, sort_keys=True), encoding="utf-8")
        html_path = html_report.render(con, cov, out_dir / "summary.html")
        typer.echo(f"  tables: {len(written)} files")
        typer.echo(f"  coverage: {out_dir / 'coverage.json'}")
        typer.echo(f"  html: {html_path}")
        s = cov["summary"]
        typer.echo(
            f"  outputs resolved {s['outputs_resolved']}/{s['outputs_total']} "
            f"({s['pct_resolved']}%), partial {s['outputs_partial']}, "
            f"unresolved {s['outputs_unresolved']}")
    finally:
        con.close()


@app.command()
def run(config: str = _CONFIG_OPT,
        fixture_dir: Optional[str] = typer.Option(None),
        phase: int = typer.Option(3),
        scope: Optional[str] = typer.Option(
            None, help="Extraction scope: full|targeted "
                       "(default: config's extraction_scope)")) -> None:
    """extract → parse → build → analyze → report, end to end."""
    extract(config=config, fixture_dir=fixture_dir, scope=scope, resume=False)
    parse(config=config)
    build(config=config, phase=phase)
    analyze(config=config)
    report(config=config, out="data/report", fmt="csv")


if __name__ == "__main__":
    app()
