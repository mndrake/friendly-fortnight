"""Graph node/edge model with provenance and confidence on every edge.

Property (tested): **no edge exists without provenance** — the dataclass makes
provenance/confidence mandatory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class NodeKind(str, Enum):
    PROGRAM = "program"
    FILE = "file"
    COLUMN = "column"
    MEMBER = "member"
    VIEW = "view"


class EdgeKind(str, Enum):
    READS = "reads"
    WRITES = "writes"
    OVERRIDES = "overrides"
    CALLS = "calls"
    DERIVES_FROM = "derives_from"
    DEFINES = "defines"


class Provenance(str, Enum):
    CATALOG = "catalog"
    XREF = "xref"
    SOURCE_CL = "source_cl"
    SOURCE_RPG = "source_rpg"
    SOURCE_SQL = "source_sql"
    DDS = "dds"


class Confidence(str, Enum):
    CONFIRMED = "confirmed"    # catalog/xref-backed, unambiguous
    PARSED = "parsed"          # derived from source parse, names constant
    INFERRED = "inferred"      # expanded via record formats / defaults
    UNRESOLVED = "unresolved"  # runtime-dependent; expression captured

    @property
    def rank(self) -> int:
        return _CONF_RANK[self]


_CONF_RANK = {
    Confidence.CONFIRMED: 3,
    Confidence.PARSED: 2,
    Confidence.INFERRED: 1,
    Confidence.UNRESOLVED: 0,
}


def min_confidence(a: Confidence, b: Confidence) -> Confidence:
    return a if a.rank <= b.rank else b


def file_id(library: Optional[str], name: str, member: Optional[str] = None) -> str:
    lib = (library or "*LIBL").upper()
    base = f"file:{lib}/{name.upper()}"
    if member:
        base += f"({member.upper()})"
    return base


def program_id(library: Optional[str], name: str) -> str:
    lib = (library or "*LIBL").upper()
    return f"program:{lib}/{name.upper()}"


def column_id(library: Optional[str], file: str, column: str) -> str:
    lib = (library or "*LIBL").upper()
    return f"column:{lib}/{file.upper()}.{column.upper()}"


@dataclass(frozen=True)
class Node:
    id: str
    kind: NodeKind
    library: Optional[str]
    name: str
    attrs: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    def row(self) -> tuple:
        return (self.id, self.kind.value, self.library, self.name,
                json.dumps(self.attrs, sort_keys=True))


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: EdgeKind
    provenance: Provenance
    confidence: Confidence
    context: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    def row(self) -> tuple:
        return (self.src, self.dst, self.kind.value, self.provenance.value,
                self.confidence.value, json.dumps(self.context, sort_keys=True))

    def key(self) -> tuple:
        """Dedup key: same relation regardless of context payload."""
        return (self.src, self.dst, self.kind, self.provenance, self.confidence)
