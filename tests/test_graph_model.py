import pytest

from lineage.graph.model import (Confidence, Edge, EdgeKind, Provenance,
                                 column_id, file_id, min_confidence,
                                 program_id)


def test_edge_requires_provenance_and_confidence():
    # Property: no edge without provenance — construction without it fails.
    with pytest.raises(TypeError):
        Edge(src="a", dst="b", kind=EdgeKind.READS)  # type: ignore[call-arg]


def test_confidence_ordering():
    assert min_confidence(Confidence.CONFIRMED, Confidence.PARSED) == Confidence.PARSED
    assert min_confidence(Confidence.PARSED, Confidence.UNRESOLVED) == Confidence.UNRESOLVED
    assert min_confidence(Confidence.INFERRED, Confidence.INFERRED) == Confidence.INFERRED
    ranks = [Confidence.UNRESOLVED, Confidence.INFERRED, Confidence.PARSED,
             Confidence.CONFIRMED]
    assert sorted(ranks, key=lambda c: c.rank) == ranks


def test_node_ids_normalised():
    assert file_id("applib", "orders") == "file:APPLIB/ORDERS"
    assert file_id(None, "orders") == "file:*LIBL/ORDERS"
    assert file_id("L", "F", member="jan") == "file:L/F(JAN)"
    assert program_id("l", "p") == "program:L/P"
    assert column_id("l", "f", "c") == "column:L/F.C"


def test_edge_dedup_key_ignores_context():
    e1 = Edge(src="a", dst="b", kind=EdgeKind.READS,
              provenance=Provenance.XREF, confidence=Confidence.CONFIRMED,
              context={"x": 1})
    e2 = Edge(src="a", dst="b", kind=EdgeKind.READS,
              provenance=Provenance.XREF, confidence=Confidence.CONFIRMED,
              context={"y": 2})
    assert e1.key() == e2.key()


def test_all_persisted_edges_have_provenance(built):
    con, _ = built
    rows = con.execute(
        "SELECT count(*) FROM edges WHERE provenance IS NULL OR "
        "confidence IS NULL").fetchone()
    assert rows[0] == 0
    total = con.execute("SELECT count(*) FROM edges").fetchone()[0]
    assert total > 0
