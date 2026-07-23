"""Resolution logic: library lists, OVRDBF simulation, LF flattening, and the
backward walk from output seeds.

All functions here are pure over in-memory structures so they are directly
unit-testable (property: override resolution is deterministic given a call
tree; LF flattening is idempotent).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import networkx as nx

from .model import (Confidence, Edge, EdgeKind, Provenance, file_id,
                    min_confidence)


# --- Library-list resolution -------------------------------------------------

@dataclass
class LiblistResolver:
    """Resolve unqualified object names against a configured library list.

    ``objects`` maps (library, name) -> True for every object known to exist
    (from the catalog). Ambiguity — the name exists in more than one liblist
    library — is reported, not silently resolved (design principle 4).
    """

    liblist: tuple[str, ...]
    objects: set[tuple[str, str]]
    ambiguities: list[dict] = field(default_factory=list)

    def resolve(self, name: str, context: str = "") -> tuple[Optional[str], bool]:
        """Return (library, ambiguous). Library is None when not found."""
        name = name.upper()
        hits = [lib for lib in self.liblist if (lib.upper(), name) in self.objects]
        if not hits:
            return None, False
        if len(hits) > 1:
            self.ambiguities.append({
                "name": name, "libraries": hits, "context": context,
            })
        return hits[0], len(hits) > 1


# --- OVRDBF simulation -------------------------------------------------------

@dataclass(frozen=True)
class Override:
    file: str                      # overridden (logical) name
    to_library: Optional[str]
    to_file: Optional[str]
    to_member: Optional[str]
    scope: str                     # *CALLLVL (default) or *JOB
    origin_program: str            # CL program that issued it
    seq: int
    resolved: bool = True
    expr: Optional[str] = None


@dataclass
class OverrideFrame:
    """Active overrides at one call level."""
    program: str
    overrides: dict[str, Override] = field(default_factory=dict)


class OverrideStack:
    """Simulates OVRDBF scoping down a call tree.

    ``*CALLLVL`` overrides live in the issuing program's frame and apply to it
    and everything it calls; they vanish when the frame pops. ``*JOB``
    overrides live in a job-level map until DLTOVR. Lookup: job-level first is
    NOT correct on the real system (call-level overrides at a nearer level take
    precedence); we search frames innermost-out, then job level.
    """

    def __init__(self) -> None:
        self.frames: list[OverrideFrame] = []
        self.job: dict[str, Override] = {}

    def push(self, program: str) -> None:
        self.frames.append(OverrideFrame(program=program))

    def pop(self) -> None:
        if self.frames:
            self.frames.pop()

    def apply(self, ovr: Override) -> None:
        if ovr.scope == "*JOB":
            self.job[ovr.file.upper()] = ovr
        else:
            if not self.frames:
                self.push("<entry>")
            self.frames[-1].overrides[ovr.file.upper()] = ovr

    def delete(self, file: Optional[str]) -> None:
        """DLTOVR FILE(name) or FILE(*ALL) at the current call level."""
        if not self.frames:
            return
        if file is None or file.upper() == "*ALL":
            self.frames[-1].overrides.clear()
        else:
            self.frames[-1].overrides.pop(file.upper(), None)

    def lookup(self, file: str) -> Optional[Override]:
        name = file.upper()
        for frame in reversed(self.frames):
            if name in frame.overrides:
                return frame.overrides[name]
        return self.job.get(name)

    def snapshot(self) -> dict[str, Override]:
        """Effective override map at the current level (innermost wins)."""
        eff: dict[str, Override] = dict(self.job)
        for frame in self.frames:
            eff.update(frame.overrides)
        return eff


@dataclass
class CallSite:
    program: str          # caller CL program id (lib/member)
    seq: int
    called: Optional[str] # called program name (None if unresolved)
    called_lib: Optional[str]
    via: str


def simulate_call_tree(
    entry: str,
    cl_events: dict[str, list],
    max_depth: int = 32,
) -> list[tuple[str, dict[str, Override], list[str]]]:
    """Walk a CL call tree from ``entry`` applying override scoping.

    ``cl_events[program]`` is the ordered list of parsed events for that CL
    program; each event is a tuple ``('ovrdbf', Override)``, ``('dltovr',
    file_or_None)`` or ``('call', CallSite)``.

    Returns, for every *called program* (including nested CL), the effective
    override map at the moment of the call and the call stack that led there.
    Deterministic: pure function of the inputs, depth-limited against cycles.
    """
    results: list[tuple[str, dict[str, Override], list[str]]] = []
    stack = OverrideStack()

    def walk(program: str, path: list[str]) -> None:
        if len(path) > max_depth or program in path:
            return
        stack.push(program)
        for event in cl_events.get(program, []):
            kind = event[0]
            if kind == "ovrdbf":
                stack.apply(event[1])
            elif kind == "dltovr":
                stack.delete(event[1])
            elif kind == "call":
                site: CallSite = event[1]
                if site.called is None:
                    continue
                callee = site.called
                results.append((callee, dict(stack.snapshot()), path + [program]))
                # Recurse if the callee is itself a CL program we parsed.
                if callee in cl_events:
                    walk(callee, path + [program])
                else:
                    # try qualified/unqualified variants
                    for key in cl_events:
                        if key.split("/")[-1] == callee.split("/")[-1]:
                            walk(key, path + [program])
                            break
        stack.pop()

    walk(entry, [])
    return results


# --- LF flattening -----------------------------------------------------------

def flatten_lf(graph: nx.MultiDiGraph, node: str,
               _seen: Optional[set] = None) -> set[str]:
    """Transitively resolve a file node to the base physical files it derives
    from, following ``derives_from`` edges. A node with no outgoing
    ``derives_from`` edges is its own base (idempotence: flattening a PF
    returns the PF).
    """
    if _seen is None:
        _seen = set()
    if node in _seen:
        return set()
    _seen.add(node)
    bases: set[str] = set()
    out = [
        (u, v) for u, v, k in graph.out_edges(node, keys=True)
        if graph.edges[u, v, k].get("kind") == EdgeKind.DERIVES_FROM.value
    ] if graph.has_node(node) else []
    if not out:
        return {node}
    for _, target in out:
        bases |= flatten_lf(graph, target, _seen)
    return bases


# --- Backward walk -----------------------------------------------------------

# Edge kinds that carry data *into* a node when walked backward.
_FLOW_KINDS = {EdgeKind.WRITES.value, EdgeKind.DERIVES_FROM.value,
               EdgeKind.READS.value, EdgeKind.DEFINES.value}


def backward_lineage(
    graph: nx.MultiDiGraph,
    seed: str,
    max_depth: int = 64,
) -> dict[str, tuple[int, Confidence]]:
    """From an output node, find every upstream node that feeds it.

    Data flows: ``program -writes-> file`` and ``file -derives_from-> base``
    and ``program -reads-> file`` (the program's outputs derive from its
    reads). Walking backward from the seed:

    * an incoming ``writes`` edge leads to the writing program;
    * from a program, its ``reads`` edges lead to input files;
    * a ``derives_from`` edge from the seed leads to underlying files;
    * ``calls`` edges lead from a program to its callers only for context —
      the caller does not itself feed data unless it also writes, so calls are
      not followed as data flow.

    Returns node -> (min path length, min confidence along the best path).
    """
    result: dict[str, tuple[int, Confidence]] = {}
    frontier: list[tuple[str, int, Confidence]] = [
        (seed, 0, Confidence.CONFIRMED)]
    while frontier:
        node, depth, conf = frontier.pop()
        if depth > max_depth:
            continue
        prev = result.get(node)
        if prev is not None and prev[0] <= depth and prev[1].rank >= conf.rank:
            continue
        if prev is None:
            result[node] = (depth, conf)
        else:
            # Multiple paths reach this node: report the shortest depth and
            # the best per-path minimum confidence (a well-evidenced path is
            # not degraded by a weaker parallel one).
            best = prev[1] if prev[1].rank >= conf.rank else conf
            result[node] = (min(prev[0], depth), best)
        if not graph.has_node(node):
            continue

        # 1. Who writes this node? (programs and CPYF file->file edges)
        for u, _, k in graph.in_edges(node, keys=True):
            data = graph.edges[u, node, k]
            if data.get("kind") == EdgeKind.WRITES.value:
                c = min_confidence(conf, Confidence(data.get("confidence")))
                frontier.append((u, depth + 1, c))

        # 2. What does this node derive from? (LF->PF, view->table, col->col)
        for _, v, k in graph.out_edges(node, keys=True):
            data = graph.edges[node, v, k]
            if data.get("kind") == EdgeKind.DERIVES_FROM.value:
                c = min_confidence(conf, Confidence(data.get("confidence")))
                frontier.append((v, depth + 1, c))

        # 3. From a program node, its reads are upstream data.
        if node.startswith("program:"):
            for _, v, k in graph.out_edges(node, keys=True):
                data = graph.edges[node, v, k]
                if data.get("kind") == EdgeKind.READS.value:
                    c = min_confidence(conf, Confidence(data.get("confidence")))
                    frontier.append((v, depth + 1, c))

    result.pop(seed, None)
    return result


def base_physical_files(
    graph: nx.MultiDiGraph,
    lineage: dict[str, tuple[int, Confidence]],
) -> dict[str, tuple[int, Confidence]]:
    """Filter a lineage result to file nodes that are base physical files:
    file/view nodes with no outgoing ``derives_from`` and not written by any
    program that itself has upstream reads... simplified: file nodes with no
    outgoing derives_from edges and no incoming writes edges (nothing in the
    scanned estate produces them), i.e. true sources of the estate.
    """
    out: dict[str, tuple[int, Confidence]] = {}
    for node, (depth, conf) in lineage.items():
        if not node.startswith(("file:", "view:")):
            continue
        has_derives = any(
            graph.edges[node, v, k].get("kind") == EdgeKind.DERIVES_FROM.value
            for _, v, k in graph.out_edges(node, keys=True)
        ) if graph.has_node(node) else False
        has_writer = any(
            graph.edges[u, node, k].get("kind") == EdgeKind.WRITES.value
            for u, _, k in graph.in_edges(node, keys=True)
        ) if graph.has_node(node) else False
        if not has_derives and not has_writer:
            out[node] = (depth, conf)
    return out
