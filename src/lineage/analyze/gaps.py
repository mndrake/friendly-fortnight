"""Coverage / gap report (design principle 4: everything unresolved is a
first-class output).

Aggregates the ``gaps`` table (populated during graph build) and computes the
coverage summary: for every output seed, exactly one of {resolved,
partially_resolved, unresolved} with reasons.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict


RESOLVED = "resolved"
PARTIAL = "partially_resolved"
UNRESOLVED = "unresolved"


def coverage(con, config) -> dict:
    """Classify each output seed and summarise gaps."""
    lineage_by_output: dict[str, list] = defaultdict(list)
    for output_id, source_file, min_conf in con.execute(
            "SELECT output_id, source_file, min_confidence "
            "FROM output_lineage WHERE source_column IS NULL").fetchall():
        lineage_by_output[output_id].append((source_file, min_conf))

    gap_rows = con.execute(
        "SELECT kind, object_id, detail, context FROM gaps").fetchall()
    gaps_by_kind = Counter(k for k, _, _, _ in gap_rows)

    # Gap objects touching an output's lineage make it partial.
    gap_objects = {obj for _, obj, _, _ in gap_rows}

    outputs = {}
    for seed in config.output_seeds:
        entries = lineage_by_output.get(seed.id, [])
        reasons: list[str] = []
        if not entries:
            status = UNRESOLVED
            reasons.append("no base physical files reached from seed")
        else:
            confidences = {conf for _, conf in entries}
            weak = {"inferred", "unresolved"} & confidences
            touched_gap = any(src in gap_objects for src, _ in entries)
            if weak or touched_gap:
                status = PARTIAL
                if weak:
                    reasons.append(
                        f"lineage includes {'/'.join(sorted(weak))} edges")
                if touched_gap:
                    reasons.append("lineage touches objects with open gaps")
            else:
                status = RESOLVED
        outputs[seed.id] = {
            "status": status,
            "reasons": reasons,
            "base_files": sorted({src for src, _ in entries}),
        }

    n_total = len(config.output_seeds)
    n_resolved = sum(1 for o in outputs.values() if o["status"] == RESOLVED)

    # Programs with no source member.
    missing_source = [obj for k, obj, _, _ in gap_rows if k == "missing_source"]
    outside_scope = [obj for k, obj, _, _ in gap_rows if k == "outside_scope"]

    return {
        "outputs": outputs,
        "summary": {
            "outputs_total": n_total,
            "outputs_resolved": n_resolved,
            "outputs_partial": sum(
                1 for o in outputs.values() if o["status"] == PARTIAL),
            "outputs_unresolved": sum(
                1 for o in outputs.values() if o["status"] == UNRESOLVED),
            "pct_resolved": round(100.0 * n_resolved / n_total, 1) if n_total else 0.0,
            "gaps_by_kind": dict(gaps_by_kind),
            "programs_missing_source": sorted(set(missing_source)),
            "files_outside_scope": sorted(set(outside_scope)),
        },
    }
