"""Per-output backward transitive closure (Phase 4, step 1).

For each configured output seed, walk backward through the graph to the base
physical files (and columns where available), carrying the per-path minimum
confidence. Results land in ``output_lineage``.
"""
from __future__ import annotations

import networkx as nx

from ..graph.model import Confidence
from ..graph.resolve import backward_lineage, base_physical_files


def compute_output_lineage(con, graph: nx.MultiDiGraph, config) -> dict[str, int]:
    from ..db import insert_rows

    con.execute("DELETE FROM output_lineage")
    rows = []
    seeds_resolved = 0
    for seed in config.output_seeds:
        seed_node = seed.node_id
        if not graph.has_node(seed_node):
            # Seed not in graph at all: full gap, recorded by gaps report.
            con.execute(
                "INSERT INTO gaps (kind, object_id, detail, context) VALUES "
                "(?, ?, ?, ?)",
                ["no_lineage", seed_node,
                 f"output seed {seed.id} not present in graph "
                 "(not extracted or outside scanned libraries)", "{}"],
            )
            continue
        lineage = backward_lineage(graph, seed_node)
        bases = base_physical_files(graph, lineage)
        if bases:
            seeds_resolved += 1
        for node, (depth, conf) in sorted(bases.items()):
            rows.append((seed.id, node, None, depth, conf.value))
        base_files = {b.split(":", 1)[1].split("(")[0] for b in bases}

        # Column-level: walk backward from the seed file's own column nodes
        # (column derives_from column edges) and keep columns that land on a
        # base physical file.
        seed_spec = seed_node.split(":", 1)[1]
        seed_cols = [n for n in graph.nodes
                     if n.startswith(f"column:{seed_spec}.")]
        col_hits: dict[str, tuple[int, Confidence]] = {}
        for col in seed_cols:
            for node, (depth, conf) in backward_lineage(graph, col).items():
                if not node.startswith("column:"):
                    continue
                spec = node.split(":", 1)[1]
                file_part = spec.rsplit(".", 1)[0]
                if file_part not in base_files:
                    continue
                prev = col_hits.get(node)
                if prev is None or depth < prev[0]:
                    col_hits[node] = (depth, conf)
        for node, (depth, conf) in sorted(col_hits.items()):
            file_part = node.split(":", 1)[1].rsplit(".", 1)[0]
            rows.append((seed.id, f"file:{file_part}", node, depth,
                         conf.value))
    insert_rows(con, "output_lineage",
                ["output_id", "source_file", "source_column", "path_len",
                 "min_confidence"], rows)
    return {"output_lineage_rows": len(rows), "seeds_resolved": seeds_resolved}
