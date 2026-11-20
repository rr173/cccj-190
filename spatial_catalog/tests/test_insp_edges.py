"""Edge ordering and roster-shape tests: edges are ordered by
(source_id, part_index) in Unicode code point order; MultiLineString parts
are independent managed edges; direction mapping uses the topology
coordinate sequence, never neighbouring edges."""

import os

from spatial_catalog import iformat as F
from insp_helpers import (make_registry, make_topology, parse_cov_rows,
                          parse_managed_rows, run_coverage, upsert,
                          write_jsonl)

W = ("2026-11-01", "2026-11-02")   # single day


def test_unicode_edge_ordering(tmp_path):
    records = [
        {"object_id": "线-乙", "geometry":
            {"type": "LineString", "coordinates": [[0, 0], [10, 0]]}},
        {"object_id": "线-甲", "geometry":
            {"type": "LineString", "coordinates": [[100, 0], [110, 0]]}},
        {"object_id": "a-edge", "geometry":
            {"type": "LineString", "coordinates": [[200, 0], [210, 0]]}},
        {"object_id": "Ω-edge", "geometry":
            {"type": "LineString", "coordinates": [[300, 0], [310, 0]]}},
    ]
    topo = make_topology(tmp_path, records)
    edges = {("线-乙", 0): 10, ("线-甲", 0): 10, ("a-edge", 0): 10,
             ("Ω-edge", 0): 10}
    reg = make_registry(tmp_path, edges)
    insp = write_jsonl(tmp_path / "insp.jsonl", [])
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 1))
    managed = parse_managed_rows(os.path.join(out, F.MANAGED_NAME))
    got_order = [(sid, part) for sid, part, _l in managed]
    assert got_order == sorted(edges.keys())
    # coverage rows follow the same edge order
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    cov_order = []
    for row in before:
        key = (row[0], row[1])
        if not cov_order or cov_order[-1] != key:
            cov_order.append(key)
    assert cov_order == sorted(edges.keys())


def test_multipart_edges_are_independent(tmp_path):
    """Each part of a MultiLineString is its own managed edge with its own
    chainage; coverage on one part never leaks onto a sibling part."""
    records = [{
        "object_id": "multi",
        "geometry": {"type": "MultiLineString",
                     "coordinates": [[[0, 0], [40, 0]],
                                     [[100, 0], [160, 0]]]},
    }]
    topo = make_topology(tmp_path, records)
    edges = {("multi", 0): 40, ("multi", 1): 60}
    reg = make_registry(tmp_path, edges)
    revisions = [
        upsert("A", 1, "multi", 1, "forward", 0, 60,
               "2026-11-01", "2026-11-02", "2026-11-01", 0, "pass"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 1))
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    d1, d2 = F.parse_date("2026-11-01"), F.parse_date("2026-11-02")
    # part 0 is untouched (all gap); part 1 is fully pass
    assert ("multi", 0, d1, d2, 0, 40, "gap", "", 0) in before
    assert ("multi", 1, d1, d2, 0, 60, "pass", "A", 1) in before


def test_reverse_direction_maps_against_coordinate_sequence(tmp_path):
    """Reverse direction maps [lo,hi) to [L-hi, L-lo) along the edge's own
    coordinate sequence, independent of any other edge."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry":
                                     {"type": "LineString",
                                      "coordinates": [[0, 0], [100, 0]]}}])
    reg = make_registry(tmp_path, {("e1", 0): 100})
    revisions = [
        upsert("A", 1, "e1", 0, "reverse", 60, 90,
               "2026-11-01", "2026-11-02", "2026-11-01", 0, "defect"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 1))
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    d1, d2 = F.parse_date("2026-11-01"), F.parse_date("2026-11-02")
    # reverse [60,90) -> forward chainage [10,40)
    assert before == [
        ("e1", 0, d1, d2, 0, 10, "gap", "", 0),
        ("e1", 0, d1, d2, 10, 40, "defect", "A", 1),
        ("e1", 0, d1, d2, 40, 100, "gap", "", 0),
    ]


def test_part_index_numeric_ordering(tmp_path):
    """part_index sorts numerically, not lexicographically (part 2 before
    part 10)."""
    parts = [[[i * 1000, 0], [i * 1000 + 10, 0]] for i in range(12)]
    records = [{"object_id": "m",
                "geometry": {"type": "MultiLineString",
                             "coordinates": parts}}]
    topo = make_topology(tmp_path, records)
    edges = {("m", i): 10 for i in range(12)}
    reg = make_registry(tmp_path, edges)
    insp = write_jsonl(tmp_path / "insp.jsonl", [])
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 1))
    managed = parse_managed_rows(os.path.join(out, F.MANAGED_NAME))
    got_parts = [part for _sid, part, _l in managed]
    assert got_parts == list(range(12))
