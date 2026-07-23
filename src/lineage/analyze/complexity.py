"""Path complexity scoring per output.

Scores each output's lineage by path depth, count of complex programs en
route, override depth, and unresolved-edge count, then buckets:
*replicate_as_view* / *moderate* / *full_reengineering*.
"""
from __future__ import annotations

import json

import networkx as nx

from ..graph.resolve import backward_lineage

BUCKET_VIEW = "replicate_as_view"
BUCKET_MODERATE = "moderate"
BUCKET_FULL = "full_reengineering"


def bucket_for(path_depth: int, complex_pgms: int, override_depth: int,
               unresolved: int) -> str:
    if unresolved > 0 or complex_pgms > 1:
        return BUCKET_FULL
    if complex_pgms == 1 or override_depth > 0 or path_depth > 3:
        return BUCKET_MODERATE
    return BUCKET_VIEW


def score(con, graph: nx.MultiDiGraph, config) -> dict[str, int]:
    from ..db import insert_rows

    complex_programs = {
        r[0].split("/")[-1].upper() for r in con.execute(
            "SELECT program FROM program_classification "
            "WHERE program_class = 'program_described_or_complex'").fetchall()
    }

    con.execute("DELETE FROM complexity_scores")
    rows = []
    for seed in config.output_seeds:
        node = seed.node_id
        if not graph.has_node(node):
            rows.append((seed.id, 0, 0, 0, 0, BUCKET_FULL))
            continue
        lineage = backward_lineage(graph, node)
        path_depth = max((d for d, _ in lineage.values()), default=0)
        pgm_nodes = [n for n in lineage if n.startswith("program:")]
        n_complex = sum(
            1 for n in pgm_nodes
            if n.split(":", 1)[1].split("/")[-1] in complex_programs)
        # Override depth: edges within the lineage that were produced by
        # OVRDBF redirection (context carries the override origin).
        override_depth = 0
        unresolved = 0
        lineage_nodes = set(lineage) | {node}
        for u, v, k in graph.edges(keys=True):
            if u not in lineage_nodes and v not in lineage_nodes:
                continue
            data = graph.edges[u, v, k]
            if data.get("confidence") == "unresolved":
                unresolved += 1
            ctx = data.get("context") or {}
            if "override_origin" in ctx or "overridden_file" in ctx:
                override_depth += 1
        b = bucket_for(path_depth, n_complex, override_depth, unresolved)
        rows.append((seed.id, path_depth, n_complex, override_depth,
                     unresolved, b))
    insert_rows(con, "complexity_scores",
                ["output_id", "path_depth", "complex_pgms", "override_depth",
                 "unresolved", "bucket"], rows)
    return {"scored_outputs": len(rows)}
