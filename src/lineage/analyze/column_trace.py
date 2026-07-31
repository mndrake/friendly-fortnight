"""Column-level lineage extractor for a single selected output table.

Unlike :mod:`lineage.analyze.lineage` — which slices the graph backward for
every configured ``output_seed`` and flattens the result into ``output_lineage``
rows — this module answers a focused question: *for one table chosen at run
time, where does each of its columns come from?* It walks the column→column
``derives_from`` edges already assembled by the graph build
(``graph/build.py``: embedded SQL INSERT…SELECT / CTAS, catalog view
definitions, DDS field maps, RPG record-format expansion) and renders a
human-readable upstream tree per column, annotated with provenance and
confidence on every hop.

The target is assumed to be a **DDL (SQL ``CREATE TABLE``) table**, so its
column list is taken from the SQL catalog (``raw_syscolumns``), which is the
authoritative column set for a DDL table. Columns with no resolved upstream —
constants, defaults, host-variable-fed inserts, or anything the parsers could
not follow — are still listed, marked ``no resolved lineage`` (design
principle 4: everything unresolved is a first-class output).

Pure over the loaded graph + DuckDB connection; nothing here touches the host.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import networkx as nx

from ..graph.model import Confidence, EdgeKind, column_id, min_confidence

_MAX_DEPTH = 64


# --- target resolution -------------------------------------------------------

@dataclass(frozen=True)
class TargetColumn:
    sql_name: str
    sys_name: Optional[str]
    ordinal: Optional[int]


@dataclass(frozen=True)
class Target:
    library: str
    name: str            # canonical name used in node ids (system name)
    spec: str            # "LIB/NAME"
    table_type: Optional[str]
    node_id: str         # "file:LIB/NAME"
    columns: tuple[TargetColumn, ...]
    in_catalog: bool     # the table was found in raw_systables


def _split_table_arg(table_arg: str) -> tuple[str, str]:
    """Accept ``LIB/NAME`` or ``LIB.NAME`` (system or SQL naming)."""
    raw = table_arg.strip().replace(".", "/", 1) if "/" not in table_arg \
        else table_arg.strip()
    if "/" not in raw:
        raise ValueError(
            f"--table must be LIB/NAME or LIB.NAME, got {table_arg!r}")
    lib, name = raw.split("/", 1)
    lib, name = lib.strip().upper(), name.strip().upper()
    if not lib or not name:
        raise ValueError(
            f"--table must be LIB/NAME or LIB.NAME, got {table_arg!r}")
    return lib, name


def resolve_target(con, table_arg: str) -> Target:
    """Resolve ``LIB/NAME`` to its canonical file node and column list.

    The canonical name mirrors ``GraphBuilder._load_catalog`` (build.py), which
    keys file nodes on ``system_name or table_name`` — so a table addressed by
    either its SQL or system name resolves to the same node.
    """
    lib, name = _split_table_arg(table_arg)

    row = con.execute(
        "SELECT table_schema, table_name, system_name, table_type "
        "FROM raw_systables "
        "WHERE upper(table_schema) = ? "
        "AND (upper(table_name) = ? OR upper(system_name) = ?) LIMIT 1",
        [lib, name, name],
    ).fetchone()
    if row is not None:
        _, table_name, system_name, table_type = row
        canonical = (system_name or table_name or name).upper()
        in_catalog = True
    else:
        canonical, table_type, in_catalog = name, None, False

    spec = f"{lib}/{canonical}"

    columns: list[TargetColumn] = []
    cat = con.execute(
        "SELECT column_name, system_column, ordinal FROM raw_syscolumns "
        "WHERE upper(table_schema) = ? "
        "AND (upper(table_name) = ? OR upper(system_name) = ?) "
        "ORDER BY ordinal",
        [lib, name, name],
    ).fetchall()
    for column_name, system_column, ordinal in cat:
        columns.append(TargetColumn(
            sql_name=(column_name or system_column or "").upper(),
            sys_name=(system_column or "").upper() or None,
            ordinal=ordinal))

    # Fallbacks when the SQL catalog has no columns for this table (e.g. a
    # DDS PF pulled without SYSCOLUMNS): DSPFFD field set, then whatever
    # column nodes the graph itself carries.
    if not columns:
        for (field_name,) in con.execute(
                "SELECT DISTINCT field_name FROM raw_dspffd "
                "WHERE upper(file_lib) = ? AND upper(file_name) = ? "
                "ORDER BY field_name", [lib, canonical]).fetchall():
            if field_name:
                columns.append(TargetColumn(sql_name=field_name.upper(),
                                            sys_name=field_name.upper(),
                                            ordinal=None))

    return Target(library=lib, name=canonical, spec=spec,
                  table_type=table_type, node_id=f"file:{spec}",
                  columns=tuple(columns), in_catalog=in_catalog)


def _graph_columns_for(graph: nx.MultiDiGraph, spec: str) -> dict[str, str]:
    """Map ``COLUMN`` suffix -> full column node id for one file spec."""
    prefix = f"column:{spec}."
    out: dict[str, str] = {}
    for node in graph.nodes:
        if isinstance(node, str) and node.startswith(prefix):
            out[node[len(prefix):]] = node
    return out


# --- per-column upstream tree ------------------------------------------------

@dataclass
class TraceNode:
    node: str
    provenance: Optional[str] = None   # of the edge from parent (None at root)
    confidence: Optional[str] = None
    context: dict = field(default_factory=dict)
    children: list["TraceNode"] = field(default_factory=list)
    is_base: bool = False              # leaf column with no upstream
    truncated: bool = False            # cut for a cycle or depth limit


def _column_derives_edges(graph: nx.MultiDiGraph, node: str
                          ) -> list[tuple[str, dict]]:
    """Outgoing ``derives_from`` edges of ``node`` whose target is a column."""
    if not graph.has_node(node):
        return []
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, dict]] = []
    for _, dst, k in graph.out_edges(node, keys=True):
        data = graph.edges[node, dst, k]
        if data.get("kind") != EdgeKind.DERIVES_FROM.value:
            continue
        if not (isinstance(dst, str) and dst.startswith("column:")):
            continue
        # Same relation can appear at several provenance/confidence tags;
        # keep each distinct (target, provenance, confidence) once.
        key = (dst, data.get("provenance", ""), data.get("confidence", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append((dst, data))
    return out


def trace_column(graph: nx.MultiDiGraph, root: str,
                 max_depth: int = _MAX_DEPTH) -> TraceNode:
    """Build the upstream ``derives_from`` tree for one column node."""

    def build(node: str, edge: Optional[dict], visited: frozenset[str],
              depth: int) -> TraceNode:
        tn = TraceNode(
            node=node,
            provenance=(edge or {}).get("provenance"),
            confidence=(edge or {}).get("confidence"),
            context=dict((edge or {}).get("context") or {}),
        )
        if depth >= max_depth:
            tn.truncated = True
            return tn
        edges = _column_derives_edges(graph, node)
        if not edges:
            tn.is_base = True
            return tn
        for dst, data in sorted(
                edges, key=lambda e: (e[0], e[1].get("provenance", ""),
                                      e[1].get("confidence", ""))):
            if dst in visited:
                tn.children.append(TraceNode(
                    node=dst, provenance=data.get("provenance"),
                    confidence=data.get("confidence"),
                    context=dict(data.get("context") or {}), truncated=True))
                continue
            tn.children.append(
                build(dst, data, visited | {dst}, depth + 1))
        return tn

    return build(root, None, frozenset({root}), 0)


def _collect_bases(tn: TraceNode, path_conf: Confidence,
                   is_root: bool = True) -> dict[str, Confidence]:
    """Base column node -> best (highest) per-path minimum confidence."""
    if tn.provenance is not None:
        path_conf = min_confidence(path_conf, Confidence(tn.confidence))
    if not tn.children:
        if is_root:
            return {}  # the target column itself has no upstream
        return {tn.node: path_conf}
    out: dict[str, Confidence] = {}
    for child in tn.children:
        for node, conf in _collect_bases(child, path_conf, is_root=False).items():
            prev = out.get(node)
            if prev is None or conf.rank > prev.rank:
                out[node] = conf
    return out


# --- rendering ---------------------------------------------------------------

_CTX_KEYS = ("mechanism", "program", "renamed_from", "via", "overridden_file")


def _annotate(tn: TraceNode) -> str:
    ann = ""
    if tn.provenance:
        ann += f"  [{tn.provenance}/{tn.confidence}]"
    extras = [f"{k}={tn.context[k]}" for k in _CTX_KEYS if tn.context.get(k)]
    if extras:
        ann += "  (" + ", ".join(extras) + ")"
    if tn.truncated:
        ann += "  … (cycle/limit)"
    elif tn.is_base and tn.provenance is not None:
        ann += "  (base)"
    return ann


def _render_tree(tn: TraceNode, lines: list[str], depth: int) -> None:
    lines.append("    " * (depth + 1) + tn.node + _annotate(tn))
    for child in tn.children:
        _render_tree(child, lines, depth + 1)


def _min_over(confs) -> Optional[Confidence]:
    confs = list(confs)
    if not confs:
        return None
    acc = confs[0]
    for c in confs[1:]:
        acc = min_confidence(acc, c)
    return acc


def render_forest(target: Target, graph: nx.MultiDiGraph) -> str:
    """Render the full per-column upstream forest for the target table."""
    header = f"Column-level lineage: {target.spec}"
    lines = [header, "=" * len(header)]

    if not target.in_catalog:
        lines.append(
            f"(table not found in raw_systables — run `extract`/`build` for "
            f"{target.library}, or targeted extraction seeded on this table)")
    elif (target.table_type or "").upper() not in ("T", "P", ""):
        lines.append(
            f"(note: table_type={target.table_type!r}; this extractor assumes "
            "a DDL table — results still shown)")

    graph_cols = _graph_columns_for(graph, target.spec)

    columns = target.columns
    if not columns:
        # No SYSCOLUMNS/DSPFFD rows — fall back to whatever column nodes the
        # graph carries (e.g. a view whose columns are known only from its
        # definition).
        columns = tuple(TargetColumn(sql_name=c, sys_name=None, ordinal=None)
                        for c in sorted(graph_cols))
    if not columns:
        lines.append("")
        lines.append("No columns found for this table (no SYSCOLUMNS/DSPFFD "
                     "rows extracted, and no column nodes in the graph).")
        return "\n".join(lines) + "\n"

    for col in columns:
        # Match the catalog column to a graph column node by SQL then system
        # name; fall back to a synthetic id so the column still appears.
        root = None
        for cand in (col.sql_name, col.sys_name):
            if cand and cand in graph_cols:
                root = graph_cols[cand]
                break
        if root is None:
            root = column_id(target.library, target.name, col.sql_name)

        tree = trace_column(graph, root)
        bases = _collect_bases(tree, Confidence.CONFIRMED)

        lines.append("")
        if not bases:
            lines.append(f"{col.sql_name}  → no resolved lineage")
            continue
        overall = _min_over(bases.values())
        n = len(bases)
        lines.append(
            f"{col.sql_name}  → {n} base column{'s' if n != 1 else ''}, "
            f"min confidence {overall.value if overall else 'n/a'}")
        _render_tree(tree, lines, 0)

    return "\n".join(lines) + "\n"


def trace_table(con, graph: nx.MultiDiGraph, table_arg: str) -> str:
    """Resolve ``table_arg`` and render its column-lineage forest."""
    target = resolve_target(con, table_arg)
    return render_forest(target, graph)
