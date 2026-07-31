"""Interactive, self-contained HTML lineage viewer, one page per output seed.

WHY: ``summary.html`` (report/html.py) gives a static coverage table; it
answers "is this output resolved?" but not "show me the actual path" or
"where does this specific column come from?". An engineer scoping a
migration needs to *see* the table/program DAG feeding an output and drill
into column-level provenance without standing up any tooling — so each page
here is a single HTML file, inline CSS + inline vanilla JS, zero external
assets (same self-containment philosophy as ``report/html.py``).

Two sections per page:

1. A table/program-level DAG rendered as positioned SVG. The layout (layering
   + within-layer ordering) is computed in Python — networkx is already a
   dependency, and keeping layout logic in Python keeps the shipped JS
   trivial (pan/zoom/hover/click only, no client-side graph layout).
2. Column-level lineage for the seed's own table, reused from
   ``analyze.column_trace`` (the same engine behind `lineage trace-columns`),
   rendered as native ``<details>``/``<summary>`` trees — no JS required for
   that part, and unresolved columns are shown explicitly (design principle:
   "unresolved" is first-class, never silently dropped).
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import networkx as nx

from ..analyze.column_trace import (TargetColumn, TraceNode, resolve_target,
                                    trace_column)
from ..graph.model import column_id
from ..graph.resolve import backward_lineage

# --- layout constants ---------------------------------------------------------

_LAYER_W = 240.0
_ROW_H = 56.0
_MARGIN = 70.0
_NODE_H = 34.0
_BARYCENTER_PASSES = 3

_KIND_ORDER = ("file", "program")  # DAG keeps only these two node kinds

_CONF_COLOR = {
    "confirmed": "#157a3b",
    "parsed": "#2563eb",
    "inferred": "#b45309",
    "unresolved": "#b91c1c",
}
_CONF_LABEL = {
    "confirmed": "confirmed", "parsed": "parsed",
    "inferred": "inferred", "unresolved": "unresolved",
}
_EDGE_DASH = {
    "reads": "none",
    "writes": "11,4",
    "calls": "3,3",
    "derives_from": "1,4",
    "defines": "8,3,2,3",
}
_NODE_FILL = {"file": "#e0f2fe", "program": "#ede9fe"}
_NODE_STROKE = {"file": "#0369a1", "program": "#6d28d9"}

_CTX_KEYS = ("mechanism", "program", "renamed_from", "via", "overridden_file")


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def _attr(v) -> str:
    """Escape for use inside a double-quoted HTML attribute."""
    return html.escape(str(v), quote=True) if v is not None else ""


# --- table/program DAG: data gathering ----------------------------------------

@dataclass
class _DagNode:
    id: str
    kind: str
    library: Optional[str]
    name: str
    depth: int  # 0 = seed; larger = further upstream
    is_seed: bool
    label: str
    spec: str  # "LIB/NAME", matches the file-segment of column node ids
    w: float = 0.0
    h: float = _NODE_H
    x: float = 0.0
    y: float = 0.0


def _label_of(library: Optional[str], name: str) -> str:
    lib = library or "*LIBL"
    return f"{lib}/{name}"


def _build_subgraph(graph: nx.MultiDiGraph, seed_node: str
                    ) -> tuple[dict[str, _DagNode], list[dict]]:
    """Nodes/edges of ``backward_lineage(graph, seed_node) | {seed_node}``,
    restricted to file/program node kinds (column nodes belong to section 2).
    """
    nodes: dict[str, _DagNode] = {}

    def _maybe_add(node_id: str, depth: int, is_seed: bool) -> None:
        if not graph.has_node(node_id):
            return
        d = graph.nodes[node_id]
        kind = d.get("kind")
        if kind not in _KIND_ORDER:
            return
        library, name = d.get("library"), d.get("name") or node_id
        spec = _label_of(library, name)
        nodes[node_id] = _DagNode(
            id=node_id, kind=kind, library=library, name=name, depth=depth,
            is_seed=is_seed, label=spec, spec=spec,
            w=max(130.0, 8.6 * len(spec) + 40.0))

    _maybe_add(seed_node, 0, True)
    if graph.has_node(seed_node):
        lineage = backward_lineage(graph, seed_node)
        for node_id, (depth, _conf) in lineage.items():
            if node_id not in nodes:  # seed already added at depth 0
                _maybe_add(node_id, depth, False)

    edges: list[dict] = []
    kept = set(nodes)
    if kept:
        for u, v, k in graph.edges(keys=True):
            if u in kept and v in kept:
                d = graph.edges[u, v, k]
                edges.append({"src": u, "dst": v, "kind": d.get("kind"),
                             "provenance": d.get("provenance"),
                             "confidence": d.get("confidence")})
    return nodes, edges


# --- table/program DAG: layout -------------------------------------------------

def _layout(nodes: dict[str, _DagNode], edges: list[dict]) -> tuple[float, float]:
    """Assign (x, y) to every node in place. Returns (draw_width, draw_height).

    Layering: BFS distance from the seed over the *incoming lineage*
    direction — exactly the depth already computed by
    ``backward_lineage`` (seed depth 0, each hop +1) — so the seed lands in
    the rightmost layer and base sources in the leftmost. Within a layer,
    a few barycenter passes (mean y of each node's neighbors, any direction)
    reorder nodes to reduce edge crossings; this is the standard Sugiyama
    heuristic, good enough at the node counts this tool deals with.
    """
    if not nodes:
        return 0.0, 0.0

    max_depth = max(n.depth for n in nodes.values())
    layers: dict[int, list[str]] = defaultdict(list)
    for nid, n in nodes.items():
        layer = max_depth - n.depth  # 0 = leftmost (most upstream)
        layers[layer].append(nid)
    for ids in layers.values():
        ids.sort()  # deterministic initial order

    y: dict[str, float] = {}
    for ids in layers.values():
        for i, nid in enumerate(ids):
            y[nid] = i * _ROW_H

    adjacency: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adjacency[e["src"]].append(e["dst"])
        adjacency[e["dst"]].append(e["src"])

    for _pass in range(_BARYCENTER_PASSES):
        for layer in sorted(layers):
            ids = layers[layer]

            def _bary(nid: str) -> float:
                neigh = adjacency.get(nid) or []
                return sum(y[n] for n in neigh) / len(neigh) if neigh else y[nid]

            ordered = sorted(ids, key=lambda nid: (_bary(nid), nid))
            for i, nid in enumerate(ordered):
                y[nid] = i * _ROW_H
            layers[layer] = ordered

    max_rows = max(len(ids) for ids in layers.values())
    for layer, ids in layers.items():
        x = _MARGIN + layer * _LAYER_W
        for nid in ids:
            n = nodes[nid]
            n.x = x
            n.y = _MARGIN + y[nid]

    draw_w = _MARGIN * 2 + (max_depth) * _LAYER_W + 140.0
    draw_h = _MARGIN * 2 + max_rows * _ROW_H
    return draw_w, draw_h


# --- table/program DAG: SVG rendering ------------------------------------------

def _edge_path(x1: float, y1: float, w1: float, x2: float, y2: float, w2: float,
               offset: float) -> str:
    if x2 >= x1:
        p1x, p2x = x1 + w1 / 2, x2 - w2 / 2
    else:
        p1x, p2x = x1 - w1 / 2, x2 + w2 / 2
    dx = max(40.0, abs(p2x - p1x) * 0.5)
    c1x, c1y = p1x + dx, y1 + offset
    c2x, c2y = p2x - dx, y2 + offset
    return (f"M {p1x:.1f} {y1:.1f} C {c1x:.1f} {c1y:.1f} "
           f"{c2x:.1f} {c2y:.1f} {p2x:.1f} {y2:.1f}")


def _render_svg(nodes: dict[str, _DagNode], edges: list[dict],
                draw_w: float, draw_h: float) -> str:
    if not nodes:
        return ('<p class="note">No table/program lineage found for this '
               'seed (its node is not present in the built graph).</p>')

    incident: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        incident[e["src"]].append({"dir": "out", "other": e["dst"],
                                   "kind": e["kind"],
                                   "provenance": e["provenance"],
                                   "confidence": e["confidence"]})
        incident[e["dst"]].append({"dir": "in", "other": e["src"],
                                   "kind": e["kind"],
                                   "provenance": e["provenance"],
                                   "confidence": e["confidence"]})

    parts: list[str] = []
    parts.append(f'<svg id="dag-svg" viewBox="0 0 {draw_w:.0f} {draw_h:.0f}" '
                f'preserveAspectRatio="xMidYMid meet" width="100%" '
                f'height="100%">')
    parts.append("<defs>")
    for conf, color in _CONF_COLOR.items():
        parts.append(
            f'<marker id="arrow-{conf}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="7" markerHeight="7" orient="auto">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{color}"></path></marker>')
    parts.append("</defs>")

    # Edges first so nodes paint on top.
    parts.append('<g class="edges">')
    pair_seen: dict[tuple[str, str], int] = defaultdict(int)
    for e in edges:
        src, dst = nodes.get(e["src"]), nodes.get(e["dst"])
        if src is None or dst is None:
            continue
        idx = pair_seen[(e["src"], e["dst"])]
        pair_seen[(e["src"], e["dst"])] += 1
        offset = (idx - 0.0) * 10.0
        d = _edge_path(src.x, src.y, src.w, dst.x, dst.y, dst.w, offset)
        conf = e["confidence"] or "unresolved"
        color = _CONF_COLOR.get(conf, "#64748b")
        dash = _EDGE_DASH.get(e["kind"] or "", "none")
        dash_attr = "" if dash == "none" else f' stroke-dasharray="{dash}"'
        parts.append(
            f'<path class="edge" data-src="{_attr(e["src"])}" '
            f'data-dst="{_attr(e["dst"])}" data-kind="{_attr(e["kind"])}" '
            f'data-confidence="{_attr(conf)}" d="{d}" fill="none" '
            f'stroke="{color}"{dash_attr} stroke-width="2" '
            f'marker-end="url(#arrow-{conf if conf in _CONF_COLOR else "unresolved"})">'
            f'</path>')
    parts.append("</g>")

    parts.append('<g class="nodes">')
    for nid, n in nodes.items():
        rx = 12 if n.kind == "program" else 1
        cls = f"node node-{n.kind}" + (" seed" if n.is_seed else "")
        edges_json = _attr(json.dumps(incident.get(nid, [])))
        parts.append(
            f'<g class="{cls}" data-id="{_attr(nid)}" data-kind="{_attr(n.kind)}" '
            f'data-spec="{_attr(n.spec)}" data-label="{_attr(n.label)}" '
            f'data-edges="{edges_json}">'
            f'<rect x="{n.x - n.w / 2:.1f}" y="{n.y - n.h / 2:.1f}" '
            f'width="{n.w:.1f}" height="{n.h:.1f}" rx="{rx}" '
            f'fill="{_NODE_FILL[n.kind]}" stroke="{_NODE_STROKE[n.kind]}" '
            f'stroke-width="{3 if n.is_seed else 1.5}"></rect>'
            f'<text x="{n.x:.1f}" y="{n.y + 4:.1f}" text-anchor="middle">'
            f'{_esc(n.label)}</text></g>')
    parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


_LEGEND = """
<div class="legend">
  <div class="legend-group">
    <b>Shapes</b>
    <span><svg width="18" height="14"><rect x="1" y="1" width="16" height="12"
      fill="#e0f2fe" stroke="#0369a1"></rect></svg> file</span>
    <span><svg width="18" height="14"><rect x="1" y="1" width="16" height="12"
      rx="5" fill="#ede9fe" stroke="#6d28d9"></rect></svg> program</span>
    <span><svg width="18" height="14"><rect x="1" y="1" width="16" height="12"
      fill="#e0f2fe" stroke="#b45309" stroke-width="3"></rect></svg>
      seed (this output)</span>
  </div>
  <div class="legend-group">
    <b>Edge confidence</b>
    <span class="conf-confirmed">&#9644; confirmed</span>
    <span class="conf-parsed">&#9644; parsed</span>
    <span class="conf-inferred">&#9644; inferred</span>
    <span class="conf-unresolved">&#9644; unresolved</span>
  </div>
  <div class="legend-group">
    <b>Edge kind (dash pattern)</b>
    <span>reads: solid</span>
    <span>writes: long dash</span>
    <span>calls: dotted</span>
    <span>derives_from: fine dotted</span>
    <span>defines: dash-dot</span>
  </div>
  <div class="legend-group">
    <b>Interact</b>
    <span>drag to pan, wheel to zoom</span>
    <span>hover a node for details</span>
    <span>click a file to jump to its columns below</span>
  </div>
</div>
"""


# --- column lineage section -----------------------------------------------------

def _graph_columns_for(graph: nx.MultiDiGraph, spec: str) -> dict[str, str]:
    prefix = f"column:{spec}."
    out: dict[str, str] = {}
    for node in graph.nodes:
        if isinstance(node, str) and node.startswith(prefix):
            out[node[len(prefix):]] = node
    return out


def _file_spec_of(node_id: str) -> str:
    body = node_id[len("column:"):] if node_id.startswith("column:") else node_id
    return body.rsplit(".", 1)[0] if "." in body else body


def _short_label(node_id: str) -> str:
    return node_id


def _render_hop(tn: TraceNode) -> str:
    file_spec = _file_spec_of(tn.node)
    conf = tn.confidence or "unresolved"
    extras = [f"{k}={tn.context[k]}" for k in _CTX_KEYS if tn.context.get(k)]
    extra_txt = (f' <span class="ctx">({_esc(", ".join(extras))})</span>'
                if extras else "")
    trunc_txt = (' <span class="marker-truncated">(cycle/limit)</span>'
                if tn.truncated else "")
    summary = (
        f'<span class="node-name" data-file="{_attr(file_spec)}">'
        f'{_esc(_short_label(tn.node))}</span> '
        f'<span class="conf-{_esc(conf)}">via {_esc(tn.provenance)}, '
        f'{_esc(conf)}</span>{extra_txt}{trunc_txt}')
    if not tn.children:
        return (f'<div class="hop leaf" data-file="{_attr(file_spec)}">'
               f'{summary}</div>')
    inner = "".join(_render_hop(c) for c in tn.children)
    return (f'<details class="hop" data-file="{_attr(file_spec)}" open>'
           f'<summary>{summary}</summary>'
           f'<div class="hops">{inner}</div></details>')


def _render_output_column(col_name: str, root: str, graph: nx.MultiDiGraph
                          ) -> str:
    tree = trace_column(graph, root)
    file_spec = _file_spec_of(root)
    if not tree.children:
        return (
            f'<details class="column">'
            f'<summary><b data-file="{_attr(file_spec)}">{_esc(col_name)}'
            f'</b> <span class="marker-unresolved">no resolved lineage'
            f'</span></summary></details>')
    inner = "".join(_render_hop(c) for c in tree.children)
    return (
        f'<details class="column" open>'
        f'<summary><b data-file="{_attr(file_spec)}">{_esc(col_name)}'
        f'</b></summary><div class="hops">{inner}</div></details>')


def _column_lineage_section(con, graph: nx.MultiDiGraph, seed) -> str:
    table_arg = f"{seed.library}/{seed.file}"
    try:
        target = resolve_target(con, table_arg)
    except Exception as exc:  # noqa: BLE001 — never fail the whole page
        return (f'<h2 id="columns">Column lineage &mdash; {_esc(table_arg)}'
               f'</h2><p class="note">Column lineage unavailable: '
               f'{_esc(exc)}</p>')

    parts = [f'<h2 id="columns">Column lineage &mdash; {_esc(target.spec)}'
            f'</h2>']
    if not target.in_catalog:
        parts.append(
            '<p class="note">Table not found in the SQL catalog '
            '(raw_systables) — falling back to DSPFFD/graph column data, '
            'if any is available.</p>')

    graph_cols = _graph_columns_for(graph, target.spec)
    columns: tuple[TargetColumn, ...] = target.columns
    if not columns:
        columns = tuple(TargetColumn(sql_name=c, sys_name=None, ordinal=None)
                        for c in sorted(graph_cols))
    if not columns:
        parts.append('<p class="note">No columns found for this table (no '
                     'SYSCOLUMNS/DSPFFD rows, and no column nodes in the '
                     'graph).</p>')
        return "\n".join(parts)

    parts.append('<div class="columns">')
    for col in columns:
        root = None
        for cand in (col.sql_name, col.sys_name):
            if cand and cand in graph_cols:
                root = graph_cols[cand]
                break
        if root is None:
            root = column_id(target.library, target.name, col.sql_name)
        parts.append(_render_output_column(col.sql_name, root, graph))
    parts.append("</div>")
    return "\n".join(parts)


# --- page assembly ---------------------------------------------------------------

_CSS = """
body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
       margin: 1.5rem auto; max-width: 78rem; color: #1a1a2e; }
h1 { border-bottom: 2px solid #16324f; padding-bottom: .3rem; }
h2 { margin-top: 2rem; color: #16324f; }
a { color: #0369a1; }
.note { color: #475569; font-style: italic; }
.meta { color: #475569; font-size: .9rem; margin-bottom: 1rem; }
.dag-wrap { position: relative; border: 1px solid #cbd5e1; border-radius: 6px;
           background: #f8fafc; overflow: hidden; height: 70vh;
           min-height: 420px; }
#dag-svg { width: 100%; height: 100%; cursor: grab; display: block; }
#dag-svg.dragging { cursor: grabbing; }
.node rect { transition: opacity .1s; }
.node text { font-size: 11px; pointer-events: none; fill: #1a1a2e; }
.node.seed rect { stroke: #b45309; }
.node.node-dim { opacity: .25; }
.edge { opacity: .85; transition: opacity .1s, stroke-width .1s; }
.edge.edge-dim { opacity: .08; }
.edge.edge-active { opacity: 1; stroke-width: 3.5; }
.legend { display: flex; flex-wrap: wrap; gap: 1.2rem; margin-top: .6rem;
         padding: .6rem .8rem; border: 1px solid #e2e8f0; border-radius: 6px;
         background: #f8fafc; font-size: .82rem; }
.legend-group { display: flex; flex-direction: column; gap: .2rem;
               min-width: 9rem; }
.legend-group b { color: #16324f; margin-bottom: .1rem; }
.tooltip { position: fixed; display: none; z-index: 20; max-width: 26rem;
          background: #16324f; color: #f1f5f9; padding: .5rem .7rem;
          border-radius: 5px; font-size: .78rem; pointer-events: none;
          box-shadow: 0 4px 10px rgba(0,0,0,.25); }
.tooltip code { color: #e2e8f0; }
.tooltip ul { margin: .3rem 0 0; padding-left: 1.1rem; }
.conf-confirmed { color: #157a3b; font-weight: 600; }
.conf-parsed { color: #2563eb; font-weight: 600; }
.conf-inferred { color: #b45309; font-weight: 600; }
.conf-unresolved { color: #b91c1c; font-weight: 600; }
.marker-unresolved { color: #b91c1c; font-style: italic; }
.marker-truncated { color: #b45309; font-style: italic; }
.ctx { color: #64748b; font-size: .85em; }
.columns { margin-top: .6rem; }
details.column { border: 1px solid #e2e8f0; border-radius: 5px;
                margin: .35rem 0; padding: .3rem .6rem; background: #fff; }
details.column > summary { cursor: pointer; font-size: .95rem; }
.hops { margin-left: 1.3rem; border-left: 2px solid #e2e8f0;
       padding-left: .8rem; margin-top: .3rem; }
details.hop { margin: .25rem 0; }
details.hop > summary { cursor: pointer; }
.hop.leaf { padding: .15rem 0; }
.node-name { font-family: monospace; font-size: .85em; }
.node-name.flash { background: #fde68a; }
"""

_JS = """
(function () {
  var svg = document.getElementById('dag-svg');
  if (!svg) return;
  var vb = svg.getAttribute('viewBox').split(' ').map(Number);
  var box = { x: vb[0], y: vb[1], w: vb[2], h: vb[3] };
  var fullW = vb[2], fullH = vb[3];
  function apply() {
    svg.setAttribute('viewBox', box.x + ' ' + box.y + ' ' + box.w + ' ' + box.h);
  }
  svg.addEventListener('wheel', function (e) {
    e.preventDefault();
    var rect = svg.getBoundingClientRect();
    var scale = e.deltaY < 0 ? 0.88 : 1.14;
    var mx = box.x + (e.clientX - rect.left) / rect.width * box.w;
    var my = box.y + (e.clientY - rect.top) / rect.height * box.h;
    var newW = Math.max(fullW * 0.05, Math.min(fullW * 6, box.w * scale));
    var newH = box.h * (newW / box.w);
    box.x = mx - (mx - box.x) * (newW / box.w);
    box.y = my - (my - box.y) * (newH / box.h);
    box.w = newW; box.h = newH;
    apply();
  }, { passive: false });
  var dragging = false, lastX = 0, lastY = 0;
  svg.addEventListener('mousedown', function (e) {
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    svg.classList.add('dragging');
  });
  window.addEventListener('mousemove', function (e) {
    if (!dragging) return;
    var rect = svg.getBoundingClientRect();
    box.x -= (e.clientX - lastX) * box.w / rect.width;
    box.y -= (e.clientY - lastY) * box.h / rect.height;
    lastX = e.clientX; lastY = e.clientY;
    apply();
  });
  window.addEventListener('mouseup', function () {
    dragging = false; svg.classList.remove('dragging');
  });

  var nodes = svg.querySelectorAll('.node');
  var edges = svg.querySelectorAll('.edge');
  var tooltip = document.getElementById('dag-tooltip');

  function clearHighlight() {
    edges.forEach(function (p) {
      p.classList.remove('edge-active'); p.classList.remove('edge-dim');
    });
    nodes.forEach(function (n) { n.classList.remove('node-dim'); });
  }

  nodes.forEach(function (g) {
    var id = g.getAttribute('data-id');
    g.addEventListener('mouseenter', function () {
      var connected = { };
      connected[id] = true;
      edges.forEach(function (p) {
        var s = p.getAttribute('data-src'), d = p.getAttribute('data-dst');
        if (s === id || d === id) {
          p.classList.add('edge-active'); p.classList.remove('edge-dim');
          connected[s] = true; connected[d] = true;
        } else {
          p.classList.add('edge-dim'); p.classList.remove('edge-active');
        }
      });
      nodes.forEach(function (n) {
        if (!connected[n.getAttribute('data-id')]) n.classList.add('node-dim');
      });
      if (tooltip) {
        var detail;
        try { detail = JSON.parse(g.getAttribute('data-edges') || '[]'); }
        catch (e) { detail = []; }
        var out = '<b>' + g.getAttribute('data-label') + '</b><br>' +
          '<code>' + id + '</code>';
        if (detail.length) {
          out += '<ul>';
          detail.forEach(function (d) {
            out += '<li>' + d.dir + ' &#8596; ' + d.other + ' &mdash; ' +
              d.kind + ', ' + d.provenance + '/' + d.confidence + '</li>';
          });
          out += '</ul>';
        }
        tooltip.innerHTML = out;
        tooltip.style.display = 'block';
      }
    });
    g.addEventListener('mousemove', function (e) {
      if (!tooltip) return;
      tooltip.style.left = (e.clientX + 16) + 'px';
      tooltip.style.top = (e.clientY + 16) + 'px';
    });
    g.addEventListener('mouseleave', function () {
      clearHighlight();
      if (tooltip) tooltip.style.display = 'none';
    });
    g.addEventListener('click', function () {
      if (g.getAttribute('data-kind') !== 'file') return;
      var spec = g.getAttribute('data-spec');
      var target = document.querySelector(
        '.columns [data-file="' + spec.replace(/"/g, '\\\\"') + '"]');
      if (!target) return;
      var el = target;
      while (el) {
        if (el.tagName === 'DETAILS') el.open = true;
        el = el.parentElement;
      }
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.classList.add('flash');
      setTimeout(function () { target.classList.remove('flash'); }, 1500);
    });
  });
})();
"""

_PAGE_TMPL = """<style>{css}</style>
<h1>Lineage &mdash; {title}</h1>
<p class="meta">{meta}</p>
<h2>Table / program DAG</h2>
<p class="note">{dag_note}</p>
<div class="dag-wrap">{svg}</div>
{legend}
<div class="tooltip" id="dag-tooltip"></div>
{columns}
<script>{js}</script>
"""


def _render_page(con, graph: nx.MultiDiGraph, seed) -> str:
    nodes, edges = _build_subgraph(graph, seed.node_id)
    draw_w, draw_h = _layout(nodes, edges)
    svg = _render_svg(nodes, edges, draw_w, draw_h)
    columns_html = _column_lineage_section(con, graph, seed)
    title = f"{_esc(seed.id)} ({_esc(seed.library)}/{_esc(seed.file)})"
    meta = (f"{len(nodes)} table/program node(s), {len(edges)} edge(s) "
           f"upstream of <code>{_esc(seed.node_id)}</code>.")
    dag_note = "" if nodes else (
        f"Seed node <code>{_esc(seed.node_id)}</code> is not present in the "
        "built graph — nothing to draw.")
    legend = _LEGEND if nodes else ""
    return _PAGE_TMPL.format(css=_CSS, title=title, meta=meta,
                             dag_note=dag_note, svg=svg, legend=legend,
                             columns=columns_html, js=_JS)


def render_output_pages(con, graph: nx.MultiDiGraph, config,
                        out_dir: str | Path) -> list[Path]:
    """Render one self-contained ``lineage_<output_id>.html`` per configured
    output seed into ``out_dir`` (naming matches
    ``export.export_output_graphs``'s ``lineage_<id>.json``).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for seed in config.output_seeds:
        doc = _render_page(con, graph, seed)
        path = out_dir / f"lineage_{seed.id}.html"
        path.write_text(doc, encoding="utf-8")
        written.append(path)
    return written
