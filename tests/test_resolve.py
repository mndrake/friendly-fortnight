import networkx as nx

from lineage.graph.model import Confidence, EdgeKind
from lineage.graph.resolve import (CallSite, LiblistResolver, Override,
                                   OverrideStack, backward_lineage,
                                   flatten_lf, simulate_call_tree)


def _ovr(file="ORDERS", to="ORDHIST", scope="*CALLLVL", origin="P1",
         resolved=True):
    return Override(file=file, to_library="APPLIB", to_file=to,
                    to_member=None, scope=scope, origin_program=origin,
                    seq=1, resolved=resolved)


class TestOverrideStack:
    def test_calllvl_pops_with_frame(self):
        s = OverrideStack()
        s.push("A")
        s.apply(_ovr())
        assert s.lookup("ORDERS") is not None
        s.pop()
        assert s.lookup("ORDERS") is None

    def test_job_scope_survives_pop(self):
        s = OverrideStack()
        s.push("A")
        s.apply(_ovr(scope="*JOB"))
        s.pop()
        assert s.lookup("ORDERS") is not None

    def test_inner_frame_wins(self):
        s = OverrideStack()
        s.push("A")
        s.apply(_ovr(to="OUTER"))
        s.push("B")
        s.apply(_ovr(to="INNER"))
        assert s.lookup("ORDERS").to_file == "INNER"
        s.pop()
        assert s.lookup("ORDERS").to_file == "OUTER"

    def test_dltovr_current_level(self):
        s = OverrideStack()
        s.push("A")
        s.apply(_ovr())
        s.delete("ORDERS")
        assert s.lookup("ORDERS") is None

    def test_dltovr_all(self):
        s = OverrideStack()
        s.push("A")
        s.apply(_ovr())
        s.apply(_ovr(file="CUSTMAST", to="X"))
        s.delete("*ALL")
        assert s.lookup("ORDERS") is None
        assert s.lookup("CUSTMAST") is None


def _events():
    return {
        "APPLIB/CLDRIVER": [
            ("ovrdbf", _ovr(origin="APPLIB/CLDRIVER")),
            ("call", CallSite(program="APPLIB/CLDRIVER", seq=2,
                              called="RPT001", called_lib=None, via="CALL")),
            ("dltovr", "ORDERS"),
            ("call", CallSite(program="APPLIB/CLDRIVER", seq=4,
                              called="SQLEXT", called_lib=None, via="CALL")),
        ],
    }


class TestSimulateCallTree:
    def test_override_visible_at_first_call_only(self):
        results = simulate_call_tree("APPLIB/CLDRIVER", _events())
        by_callee = {callee: ovr for callee, ovr, _ in results}
        assert "ORDERS" in by_callee["RPT001"]
        assert "ORDERS" not in by_callee["SQLEXT"]

    def test_deterministic(self):
        r1 = simulate_call_tree("APPLIB/CLDRIVER", _events())
        r2 = simulate_call_tree("APPLIB/CLDRIVER", _events())
        assert [(c, sorted(o), s) for c, o, s in r1] == \
               [(c, sorted(o), s) for c, o, s in r2]

    def test_nested_cl_inherits_override(self):
        events = {
            "L1": [
                ("ovrdbf", _ovr(origin="L1")),
                ("call", CallSite(program="L1", seq=2, called="L2",
                                  called_lib=None, via="CALL")),
            ],
            "L2": [
                ("call", CallSite(program="L2", seq=1, called="RPTX",
                                  called_lib=None, via="CALL")),
            ],
        }
        results = simulate_call_tree("L1", events)
        by_callee = {c: o for c, o, _ in results}
        assert "ORDERS" in by_callee["RPTX"]  # override crosses CL levels

    def test_cycle_terminates(self):
        events = {
            "A": [("call", CallSite(program="A", seq=1, called="B",
                                    called_lib=None, via="CALL"))],
            "B": [("call", CallSite(program="B", seq=1, called="A",
                                    called_lib=None, via="CALL"))],
        }
        results = simulate_call_tree("A", events)
        assert len(results) < 10  # terminates


class TestLiblistResolver:
    def test_first_match_wins(self):
        r = LiblistResolver(liblist=("LIBA", "LIBB"),
                            objects={("LIBB", "F1"), ("LIBA", "F2")})
        assert r.resolve("F1") == ("LIBB", False)
        assert r.resolve("F2") == ("LIBA", False)

    def test_ambiguity_flagged(self):
        r = LiblistResolver(liblist=("LIBA", "LIBB"),
                            objects={("LIBA", "F1"), ("LIBB", "F1")})
        lib, ambiguous = r.resolve("F1", context="test")
        assert lib == "LIBA"       # first in liblist order
        assert ambiguous
        assert r.ambiguities[0]["libraries"] == ["LIBA", "LIBB"]

    def test_not_found(self):
        r = LiblistResolver(liblist=("LIBA",), objects=set())
        assert r.resolve("NOPE") == (None, False)


def _mkgraph(edges):
    g = nx.MultiDiGraph()
    for src, dst, kind, conf in edges:
        g.add_edge(src, dst, kind=kind, provenance="xref", confidence=conf)
    return g


class TestFlattenLf:
    def test_pf_is_its_own_base(self):
        g = _mkgraph([])
        g.add_node("file:L/PF")
        assert flatten_lf(g, "file:L/PF") == {"file:L/PF"}

    def test_lf_flattens_to_pf(self):
        g = _mkgraph([("file:L/LF", "file:L/PF", "derives_from", "confirmed")])
        assert flatten_lf(g, "file:L/LF") == {"file:L/PF"}

    def test_flattening_idempotent(self):
        g = _mkgraph([
            ("file:L/LF2", "file:L/LF1", "derives_from", "confirmed"),
            ("file:L/LF1", "file:L/PF", "derives_from", "confirmed"),
        ])
        once = flatten_lf(g, "file:L/LF2")
        again = set()
        for n in once:
            again |= flatten_lf(g, n)
        assert once == again == {"file:L/PF"}

    def test_join_lf_flattens_to_all_pfs(self):
        g = _mkgraph([
            ("file:L/JLF", "file:L/PF1", "derives_from", "confirmed"),
            ("file:L/JLF", "file:L/PF2", "derives_from", "confirmed"),
        ])
        assert flatten_lf(g, "file:L/JLF") == {"file:L/PF1", "file:L/PF2"}

    def test_cycle_terminates(self):
        g = _mkgraph([
            ("file:L/A", "file:L/B", "derives_from", "confirmed"),
            ("file:L/B", "file:L/A", "derives_from", "confirmed"),
        ])
        flatten_lf(g, "file:L/A")  # must not recurse forever


class TestBackwardLineage:
    def test_walks_writer_then_reads(self):
        g = _mkgraph([
            ("program:L/P", "file:L/OUT", "writes", "confirmed"),
            ("program:L/P", "file:L/IN", "reads", "confirmed"),
        ])
        result = backward_lineage(g, "file:L/OUT")
        assert "program:L/P" in result
        assert "file:L/IN" in result

    def test_min_confidence_propagates(self):
        g = _mkgraph([
            ("program:L/P", "file:L/OUT", "writes", "confirmed"),
            ("program:L/P", "file:L/IN", "reads", "inferred"),
            ("file:L/IN", "file:L/BASE", "derives_from", "confirmed"),
        ])
        result = backward_lineage(g, "file:L/OUT")
        assert result["file:L/BASE"][1] == Confidence.INFERRED

    def test_calls_not_followed_as_data(self):
        g = _mkgraph([
            ("program:L/P", "file:L/OUT", "writes", "confirmed"),
            ("program:L/CALLER", "program:L/P", "calls", "confirmed"),
            ("program:L/CALLER", "file:L/OTHER", "reads", "confirmed"),
        ])
        result = backward_lineage(g, "file:L/OUT")
        assert "file:L/OTHER" not in result
