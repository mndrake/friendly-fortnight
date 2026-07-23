"""Exports: CSV/Parquet for the matrices, JSON graph extract per output."""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from ..graph.resolve import backward_lineage

EXPORT_TABLES = ("output_lineage", "commonality_matrix", "complexity_scores",
                 "gaps", "nodes", "edges")


def export_tables(con, out_dir: str | Path, fmt: str = "csv") -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for table in EXPORT_TABLES:
        if fmt == "parquet":
            path = out_dir / f"{table}.parquet"
            con.execute(
                f"COPY (SELECT * FROM {table}) TO '{path}' (FORMAT PARQUET)")
        else:
            path = out_dir / f"{table}.csv"
            con.execute(
                f"COPY (SELECT * FROM {table}) TO '{path}' (HEADER, DELIMITER ',')")
        written.append(path)
    return written


def export_output_graphs(con, graph: nx.MultiDiGraph, config,
                         out_dir: str | Path) -> list[Path]:
    """Per-output JSON extract of the lineage subgraph."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for seed in config.output_seeds:
        node = seed.node_id
        payload: dict = {"output_id": seed.id, "seed": node,
                         "nodes": [], "edges": []}
        if graph.has_node(node):
            lineage = backward_lineage(graph, node)
            keep = set(lineage) | {node}
            for n in sorted(keep):
                if graph.has_node(n):
                    d = graph.nodes[n]
                    payload["nodes"].append({"id": n, **d})
            for u, v, k in graph.edges(keys=True):
                if u in keep and v in keep:
                    payload["edges"].append({"src": u, "dst": v,
                                             **graph.edges[u, v, k]})
        path = out_dir / f"lineage_{seed.id}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True),
                        encoding="utf-8")
        written.append(path)
    return written
