"""Hand-computable topology scenarios.

Every degree, endpoint reference and component partition here is derived
by hand from integer coordinates, so correctness does not rely on any
library intuition.  The four required scenes are all present:

  * a three-edge chain;
  * a three-way fork;
  * a standalone closed self loop (zero-length part);
  * two lines that only cross in their interiors (must NOT connect).
"""

import math

import pytest

from spatial_catalog import tformat as tf
from spatial_catalog.topo import Selection, build_topology
from spatial_catalog.treader import Topology
from helpers import build_catalog, linestring


def _build(tmp_path, records, **kwargs):
    cat, _ = build_catalog(tmp_path, records, name="cat")
    out = str(tmp_path / "topo")
    kwargs.setdefault("cache_bytes", 1 << 16)   # forces external spills
    report = build_topology(cat, out, selection=Selection(),
                            force=True, **kwargs)
    return cat, out, report


def _rows(topo_dir):
    with Topology(topo_dir) as t:
        return list(t.iter_nodes()), list(t.iter_edges())


def test_three_edge_chain_degrees_and_partition(tmp_path):
    # chain a: (0,0)-(1,0), b: (1,0)-(2,0), c: (2,0)-(2,1)
    recs = [
        {"object_id": "a", "geometry": linestring([[0, 0], [1, 0]])},
        {"object_id": "b", "geometry": linestring([[1, 0], [2, 0]])},
        {"object_id": "c", "geometry": linestring([[2, 0], [2, 1]])},
    ]
    _cat, topo, report = _build(tmp_path, recs)
    nodes, edges = _rows(topo)
    # 4 distinct coordinates -> 4 nodes; 3 edges; 1 component
    assert report["nodes"] == 4 and report["edges"] == 3
    assert report["components"] == 1
    # node ordinals follow coordinate order
    coords = [(x, y) for _o, x, y, _d, _r, _c in nodes]
    assert coords == [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (2.0, 1.0)]
    # hand-checked degrees: 1, 2, 2, 1
    assert [n[3] for n in nodes] == [1, 2, 2, 1]
    # endpoint references
    deg1_refs = {nodes[0][0]: nodes[0][4], nodes[3][0]: nodes[3][4]}
    assert deg1_refs[0] == [(0, tf.END_FROM)]
    assert deg1_refs[3] == [(2, tf.END_TO)]
    # every edge points to the expected nodes
    assert [(e[3], e[4]) for e in edges] == [(0, 1), (1, 2), (2, 3)]
    # every node belongs to component C0
    assert all(n[5] == 0 for n in nodes)
    assert all(e[7] == 0 for e in edges)


def test_three_way_fork(tmp_path):
    # three edges sharing exactly the node (0,0)
    recs = [
        {"object_id": "f1", "geometry": linestring([[0, 0], [1, 0]])},
        {"object_id": "f2", "geometry": linestring([[0, 0], [0, 1]])},
        {"object_id": "f3", "geometry": linestring([[0, 0], [-1, 0]])},
    ]
    _cat, topo, report = _build(tmp_path, recs)
    nodes, edges = _rows(topo)
    # 4 nodes, 3 edges, one component
    assert (report["nodes"], report["edges"], report["components"]) == (4, 3, 1)
    hub = next(n for n in nodes if (n[1], n[2]) == (0.0, 0.0))
    # hub degree 3, one end of every edge
    assert hub[3] == 3
    assert sorted(hub[4]) == sorted([(0, tf.END_FROM),
                                     (1, tf.END_FROM),
                                     (2, tf.END_FROM)])
    # leaves each have degree 1
    leaves = [n for n in nodes if (n[1], n[2]) != (0.0, 0.0)]
    assert sorted(n[3] for n in leaves) == [1, 1, 1]


def test_standalone_self_loop(tmp_path):
    # one zero-length part: start == end, same node, degree counted twice
    recs = [{"object_id": "loop",
             "geometry": linestring([[3, 3], [3, 3]])}]
    _cat, topo, report = _build(tmp_path, recs)
    nodes, edges = _rows(topo)
    assert report["zero_length_parts"] == 1
    assert len(nodes) == 1 and len(edges) == 1
    assert nodes[0][3] == 2                      # two ends at the same node
    assert edges[0][3] == edges[0][4] == 0       # both ends reference node 0
    assert edges[0][6] == 0.0                    # planar length is zero
    assert nodes[0][5] == 0 and edges[0][7] == 0
    # the two incident tokens are the S and T side of the same edge
    assert nodes[0][4] == [(0, tf.END_FROM), (0, tf.END_TO)]


def test_interior_crossing_does_not_connect(tmp_path):
    # two segments cross at (1,1), which is interior to BOTH lines;
    # no shared endpoint coordinate, so they must stay two components.
    recs = [
        {"object_id": "h", "geometry": linestring([[0, 1], [2, 1]])},
        {"object_id": "v", "geometry": linestring([[1, 0], [1, 2]])},
    ]
    _cat, topo, report = _build(tmp_path, recs)
    nodes, edges = _rows(topo)
    assert (report["nodes"], report["edges"]) == (4, 2)
    assert report["components"] == 2
    comps = sorted({n[5] for n in nodes})
    assert comps == [0, 1]
    # each edge is entirely inside one component
    for e in edges:
        node_comps = {nodes[e[3]][5], nodes[e[4]][5]}
        assert node_comps == {e[7]}
    # degree-1 nodes only (no node was inserted at the crossing)
    assert sorted(n[3] for n in nodes) == [1, 1, 1, 1]


def test_endpoint_on_other_line_interior_is_not_a_connection(tmp_path):
    # b ends at (1,0), which lies in the interior of a (0,0)-(2,0);
    # the shared coordinate is an endpoint only for b, so no merge.
    recs = [
        {"object_id": "a", "geometry": linestring([[0, 0], [2, 0]])},
        {"object_id": "b", "geometry": linestring([[1, 0], [1, 1]])},
    ]
    _cat, topo, report = _build(tmp_path, recs)
    nodes, edges = _rows(topo)
    # (1,0) appears only as b's start; a's ends are (0,0),(2,0)
    assert report["nodes"] == 4
    assert report["components"] == 2
    node_at = {(n[1], n[2]): n for n in nodes}
    assert node_at[(1.0, 0.0)][3] == 1   # only b's end, no connection


def test_overlapping_and_repeated_lines_remain_independent(tmp_path):
    # two distinct sources with identical geometry: duplicate lines stay
    # two independent edges sharing the same two endpoint nodes.
    recs = [
        {"object_id": "dup-a", "geometry": linestring([[0, 0], [1, 1]])},
        {"object_id": "dup-b", "geometry": linestring([[0, 0], [1, 1]])},
    ]
    _cat, topo, report = _build(tmp_path, recs)
    nodes, edges = _rows(topo)
    assert report["edges"] == 2 and report["nodes"] == 2
    assert [n[3] for n in nodes] == [2, 2]   # each end holds both edges
    ids = [e[1] for e in edges]
    assert ids == ["dup-a", "dup-b"]         # code point order


def test_exact_vs_near_equal_coordinates(tmp_path):
    # one float64 ulp apart: numerically distinct, no snapping => separate
    # nodes (1e-16 would round to 1.0; the ulp at 1.0 is ~2.2204e-16).
    ulp = math.nextafter(1.0, math.inf) - 1.0
    recs = [
        {"object_id": "p", "geometry": linestring([[0, 0], [1, 0]])},
        {"object_id": "q",
         "geometry": linestring([[1.0 + ulp, 0], [2, 0]])},
    ]
    _cat, topo, report = _build(tmp_path, recs)
    nodes, _edges = _rows(topo)
    assert report["nodes"] == 4             # no tolerance merge
    xs = sorted(n[1] for n in nodes)
    assert 1.0 in xs and (1.0 + ulp) in xs
    assert 1.0 != 1.0 + ulp


def test_negative_zero_shares_node_with_positive_zero(tmp_path):
    recs = [
        {"object_id": "p", "geometry": linestring([[-0.0, -0.0], [1, 0]])},
        {"object_id": "q", "geometry": linestring([[0.0, 0.0], [0, 1]])},
    ]
    _cat, topo, report = _build(tmp_path, recs)
    nodes, _edges = _rows(topo)
    # the two zero representations are the same coordinate class
    zero_nodes = [n for n in nodes if n[1] == 0.0 and n[2] == 0.0]
    assert len(zero_nodes) == 1
    assert zero_nodes[0][3] == 2
    assert report["components"] == 1


def test_multipart_split_and_traceability(tmp_path):
    mls = {"type": "MultiLineString", "coordinates": [
        [[0, 0], [1, 0]], [[5, 5], [6, 5]], [[1, 0], [2, 0]]]}
    recs = [{"object_id": "ml", "geometry": mls}]
    _cat, topo, report = _build(tmp_path, recs)
    nodes, edges = _rows(topo)
    assert report["parts_parsed"] == 3
    # three edges all trace to the same source, parts 0,1,2
    assert [(e[1], e[2]) for e in edges] == [
        ("ml", 0), ("ml", 1), ("ml", 2)]
    # part 0 and part 2 meet at (1,0): two components overall
    assert report["components"] == 2
    with Topology(topo) as t:
        summary = t.validate_full(catalog=_cat)
    assert summary["ok"]


def test_reversed_coordinates_still_connect(tmp_path):
    # same two endpoints, opposite digitization direction
    recs = [
        {"object_id": "fwd", "geometry": linestring([[0, 0], [1, 0]])},
        {"object_id": "rev", "geometry": linestring([[1, 0], [0, 0]])},
    ]
    _cat, topo, _ = _build(tmp_path, recs)
    nodes, edges = _rows(topo)
    assert len(nodes) == 2 and len(edges) == 2
    # edges point in opposite directions between the same two nodes
    endpoint_pairs = [tuple(sorted(e[3:5])) for e in edges]
    assert endpoint_pairs == [(0, 1), (0, 1)]
    directions = {e[3:5] for e in edges}
    assert directions == {(0, 1), (1, 0)}


def test_isolated_line_is_its_own_component(tmp_path):
    recs = [
        {"object_id": "iso", "geometry": linestring([[9, 9], [9, 8]])},
        {"object_id": "other", "geometry": linestring([[0, 0], [1, 0]])},
    ]
    _cat, topo, report = _build(tmp_path, recs)
    assert report["components"] == 2
    with Topology(topo) as t:
        assert t.validate_full(catalog=False)["ok"]
