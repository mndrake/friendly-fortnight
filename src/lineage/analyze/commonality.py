"""Commonality analysis: outputs × sources incidence matrix and clustering.

Clustering starts simple (Jaccard similarity + greedy grouping, per the plan)
with the similarity function factored out so hierarchical clustering can be
slotted in later.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable


def build_matrix(con) -> dict[str, set[str]]:
    """output_id -> set of base source-file node ids (from output_lineage)."""
    con.execute("DELETE FROM commonality_matrix")
    rows = con.execute(
        "SELECT DISTINCT output_id, source_file FROM output_lineage "
        "WHERE source_file IS NOT NULL").fetchall()
    matrix: dict[str, set[str]] = defaultdict(set)
    for output_id, source_file in rows:
        matrix[output_id].add(source_file)
    from ..db import insert_rows
    insert_rows(con, "commonality_matrix",
                ["output_id", "source_id", "present"],
                [(o, s, True) for o, srcs in matrix.items() for s in sorted(srcs)])
    return dict(matrix)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def greedy_cluster(
    matrix: dict[str, set[str]],
    threshold: float = 0.5,
    similarity: Callable[[set[str], set[str]], float] = jaccard,
) -> list[list[str]]:
    """Greedy grouping: each output joins the first existing cluster whose
    representative (union of member sources) is at least ``threshold`` similar;
    otherwise it starts a new cluster. Deterministic given input ordering
    (outputs processed in sorted order).
    """
    clusters: list[tuple[list[str], set[str]]] = []
    for output in sorted(matrix):
        sources = matrix[output]
        placed = False
        for members, union in clusters:
            if similarity(sources, union) >= threshold:
                members.append(output)
                union |= sources
                placed = True
                break
        if not placed:
            clusters.append(([output], set(sources)))
    return [members for members, _ in clusters]


def candidate_products(matrix: dict[str, set[str]],
                       min_fanout: int = 2) -> list[dict]:
    """Foundational data-product candidates: source files shared by many
    outputs, ranked by fan-out. Each candidate is the shared source set of a
    cluster of outputs.
    """
    # Per-source fan-out.
    fanout: dict[str, set[str]] = defaultdict(set)
    for output, sources in matrix.items():
        for s in sources:
            fanout[s].add(output)

    clusters = greedy_cluster(matrix)
    candidates = []
    for members in clusters:
        if not members:
            continue
        shared = set.intersection(*(matrix[m] for m in members)) if members else set()
        all_sources = set.union(*(matrix[m] for m in members)) if members else set()
        n_outputs = len(members)
        if n_outputs < min_fanout and len(clusters) > 1:
            continue
        candidates.append({
            "outputs": sorted(members),
            "shared_sources": sorted(shared),
            "all_sources": sorted(all_sources),
            "fanout": n_outputs,
        })
    candidates.sort(key=lambda c: (-c["fanout"], -len(c["shared_sources"])))

    top_shared = sorted(fanout.items(), key=lambda kv: -len(kv[1]))
    return candidates + [{
        "outputs": sorted(outs), "shared_sources": [src],
        "all_sources": [src], "fanout": len(outs), "kind": "single_source",
    } for src, outs in top_shared if len(outs) >= min_fanout
        and not any(src in c["shared_sources"] for c in candidates)]


def analyze(con) -> dict[str, int]:
    matrix = build_matrix(con)
    candidates = candidate_products(matrix)
    # Persist candidates as JSON alongside the matrix (kept in a temp view via
    # export; the report reads candidates from here).
    import json
    con.execute("CREATE TABLE IF NOT EXISTS product_candidates "
                "(rank INTEGER, fanout INTEGER, payload VARCHAR)")
    con.execute("DELETE FROM product_candidates")
    for i, c in enumerate(candidates, start=1):
        con.execute("INSERT INTO product_candidates VALUES (?, ?, ?)",
                    [i, c["fanout"], json.dumps(c, sort_keys=True)])
    return {"outputs_in_matrix": len(matrix),
            "product_candidates": len(candidates)}
