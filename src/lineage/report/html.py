"""Single-file HTML summary report.

Covers: coverage, top shared source files/columns, candidate products ranked
by fan-out, gap list, classification counts. Self-contained (inline CSS, no
external assets).
"""
from __future__ import annotations

import html
import json
from pathlib import Path

_CSS = """
body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
       margin: 2rem auto; max-width: 70rem; color: #1a1a2e; }
h1 { border-bottom: 2px solid #16324f; padding-bottom: .3rem; }
h2 { margin-top: 2rem; color: #16324f; }
table { border-collapse: collapse; width: 100%; margin: .8rem 0; }
th, td { border: 1px solid #cbd5e1; padding: .35rem .6rem; text-align: left;
         font-size: .9rem; vertical-align: top; }
th { background: #eef2f7; }
.status-resolved { color: #157a3b; font-weight: 600; }
.status-partially_resolved { color: #b45309; font-weight: 600; }
.status-unresolved { color: #b91c1c; font-weight: 600; }
.bucket-replicate_as_view { color: #157a3b; }
.bucket-moderate { color: #b45309; }
.bucket-full_reengineering { color: #b91c1c; }
code { background: #f1f5f9; padding: 0 .25rem; border-radius: 3px;
       font-size: .85em; }
.kpi { display: inline-block; margin-right: 2rem; }
.kpi b { font-size: 1.6rem; display: block; }
"""


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def render(con, coverage: dict, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    summary = coverage["summary"]
    outputs = coverage["outputs"]

    parts: list[str] = []
    parts.append(f"<style>{_CSS}</style>")
    parts.append("<h1>DB2 for i Lineage — Summary</h1>")

    # KPIs
    parts.append("<div>")
    for label, key in (("Outputs", "outputs_total"),
                       ("Resolved", "outputs_resolved"),
                       ("Partial", "outputs_partial"),
                       ("Unresolved", "outputs_unresolved"),
                       ("% resolved", "pct_resolved")):
        parts.append(f'<span class="kpi"><b>{_esc(summary[key])}</b>{label}</span>')
    parts.append("</div>")

    # Coverage per output
    cols_used = dict(con.execute(
        "SELECT output_id, count(DISTINCT source_column) FROM output_lineage "
        "WHERE relation = 'used' GROUP BY output_id").fetchall())
    parts.append("<h2>Output coverage</h2><table><tr><th>Output</th>"
                 "<th>Status</th><th>Base physical files</th>"
                 "<th>Cols used</th><th>Reasons</th><th>Lineage view</th></tr>")
    for oid in sorted(outputs):
        o = outputs[oid]
        files = "<br>".join(f"<code>{_esc(f)}</code>" for f in o["base_files"]) or "—"
        page = f"lineage_{oid}.html"
        parts.append(
            f"<tr><td>{_esc(oid)}</td>"
            f"<td class='status-{o['status']}'>{_esc(o['status'])}</td>"
            f"<td>{files}</td><td>{cols_used.get(oid, 0)}</td>"
            f"<td>{_esc('; '.join(o['reasons']) or '')}</td>"
            f"<td><a href='{_esc(page)}'>view</a></td></tr>")
    parts.append("</table>")

    # Complexity buckets
    rows = con.execute(
        "SELECT output_id, path_depth, complex_pgms, override_depth, "
        "unresolved, bucket FROM complexity_scores ORDER BY output_id"
    ).fetchall()
    if rows:
        parts.append("<h2>Complexity</h2><table><tr><th>Output</th>"
                     "<th>Path depth</th><th>Complex pgms</th>"
                     "<th>Override edges</th><th>Unresolved</th>"
                     "<th>Bucket</th></tr>")
        for oid, pd, cp, od, un, bucket in rows:
            parts.append(
                f"<tr><td>{_esc(oid)}</td><td>{pd}</td><td>{cp}</td>"
                f"<td>{od}</td><td>{un}</td>"
                f"<td class='bucket-{bucket}'>{_esc(bucket)}</td></tr>")
        parts.append("</table>")

    # Candidate products
    try:
        cands = con.execute(
            "SELECT rank, fanout, payload FROM product_candidates "
            "ORDER BY rank").fetchall()
    except Exception:  # noqa: BLE001
        cands = []
    if cands:
        parts.append("<h2>Foundational data-product candidates "
                     "(ranked by fan-out)</h2><table><tr><th>#</th>"
                     "<th>Fan-out</th><th>Outputs</th><th>Shared sources</th></tr>")
        for rank, fanout, payload in cands:
            c = json.loads(payload)
            parts.append(
                f"<tr><td>{rank}</td><td>{fanout}</td>"
                f"<td>{_esc(', '.join(c['outputs']))}</td>"
                f"<td>{'<br>'.join('<code>' + _esc(s) + '</code>' for s in c['shared_sources']) or '—'}</td></tr>")
        parts.append("</table>")

    # Top shared source files
    top = con.execute(
        "SELECT source_id, count(DISTINCT output_id) AS n FROM "
        "commonality_matrix GROUP BY source_id ORDER BY n DESC, source_id "
        "LIMIT 20").fetchall()
    if top:
        parts.append("<h2>Top shared source files</h2><table>"
                     "<tr><th>Source</th><th># outputs</th></tr>")
        for src, n in top:
            parts.append(f"<tr><td><code>{_esc(src)}</code></td><td>{n}</td></tr>")
        parts.append("</table>")

    # Top used source columns
    top_cols = con.execute(
        "SELECT source_id, count(DISTINCT output_id) AS n FROM "
        "commonality_matrix WHERE source_id LIKE 'column:%' "
        "GROUP BY source_id ORDER BY n DESC, source_id LIMIT 20").fetchall()
    if top_cols:
        parts.append("<h2>Top used source columns</h2><table>"
                     "<tr><th>Column</th><th># outputs</th></tr>")
        for src, n in top_cols:
            parts.append(f"<tr><td><code>{_esc(src)}</code></td><td>{n}</td></tr>")
        parts.append("</table>")

    # Classification counts
    cls = con.execute(
        "SELECT program_class, count(*) FROM program_classification "
        "GROUP BY program_class ORDER BY 1").fetchall()
    if cls:
        parts.append("<h2>Program classification</h2><table>"
                     "<tr><th>Class</th><th>Count</th></tr>")
        for c, n in cls:
            parts.append(f"<tr><td>{_esc(c)}</td><td>{n}</td></tr>")
        parts.append("</table>")

    # Gaps
    gap_rows = con.execute(
        "SELECT kind, object_id, detail FROM gaps ORDER BY kind, object_id"
    ).fetchall()
    parts.append(f"<h2>Gaps ({len(gap_rows)})</h2>")
    if gap_rows:
        parts.append("<table><tr><th>Kind</th><th>Object</th><th>Detail</th></tr>")
        for kind, obj, detail in gap_rows:
            parts.append(f"<tr><td>{_esc(kind)}</td>"
                         f"<td><code>{_esc(obj)}</code></td>"
                         f"<td>{_esc(detail)}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p>No gaps recorded.</p>")

    out_path.write_text("\n".join(parts), encoding="utf-8")
    return out_path
