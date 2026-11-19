"""Independent naive topology oracle (test-only).

This module re-derives the entire expected network from raw JSONL records
using the simplest possible in-memory approach -- a plain dict coordinate
map and plain graph traversal -- and deliberately shares NO code with the
production build:

* no external sort, no disk union-find, no tformat/tunion/topo imports;
* node identity is a Python dict keyed by the exact coordinate tuple;
* components are found by an explicit stack walk over an adjacency dict.

It exists only so differential tests can compare the disk-bounded
production artifact, node by node and edge by edge, against a second,
independently reasoned answer on small samples.
"""

import json
import math


def load_jsonl_records(path):
    records = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _norm(x, y):
    # mirror the production -0.0 normalisation, written independently
    if x == 0.0:
        x = 0.0
    if y == 0.0:
        y = 0.0
    return (float(x), float(y))


def _length(coords):
    total = 0.0
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        total += math.hypot(x1 - x0, y1 - y0)
    return total


def expected_network(records, selected=None):
    """Naive reference topology.

    Parameters
    ----------
    records : list of {"object_id", "geometry"} dicts (raw JSONL).
    selected : optional set/None; when given, only object_ids in it are
               processed (the others are counted as filter exclusions).

    Returns a dict with the exact tables a build should produce, using the
    same stable ordering rules: coordinates sorted lexicographically,
    edges sorted by (source_id, part_index) code point order.
    """
    line_types = {"LineString", "MultiLineString"}
    edges = []                    # (source_id, part, coords)
    scanned = 0
    excluded = 0
    type_rejected = 0
    for rec in records:
        oid = rec["object_id"]
        geom = rec["geometry"]
        scanned += 1
        if selected is not None and oid not in selected:
            excluded += 1
            continue
        if geom["type"] not in line_types:
            type_rejected += 1
            continue
        if geom["type"] == "LineString":
            parts = [geom["coordinates"]]
        else:
            parts = geom["coordinates"]
        for idx, coords in enumerate(parts):
            edges.append((oid, idx, [_norm(x, y) for x, y in coords]))

    edges.sort(key=lambda e: (e[0], e[1]))

    # naive exact-coordinate node map
    coord_to_node = {}
    for _oid, _part, coords in edges:
        for xy in (coords[0], coords[-1]):
            if xy not in coord_to_node:
                coord_to_node[xy] = len(coord_to_node)
    ordered_coords = sorted(coord_to_node)
    # node ordinals must follow coordinate order, so remap
    ord_by_coord = {xy: i for i, xy in enumerate(ordered_coords)}

    degree = {i: 0 for i in range(len(ordered_coords))}
    refs = {i: [] for i in range(len(ordered_coords))}
    adjacency = {i: set() for i in range(len(ordered_coords))}
    edge_table = []
    for e_ord, (oid, part, coords) in enumerate(edges):
        fo = ord_by_coord[coords[0]]
        to = ord_by_coord[coords[-1]]
        edge_table.append((e_ord, oid, part, fo, to, coords,
                           _length(coords)))
        degree[fo] += 1
        degree[to] += 1
        refs[fo].append((e_ord, 0))
        refs[to].append((e_ord, 1))
        adjacency[fo].add(to)
        adjacency[to].add(fo)
    for i in refs:
        refs[i].sort()

    # naive explicit-stack component walk
    seen = set()
    component_of = {}
    components = 0
    for start in range(len(ordered_coords)):
        if start in seen:
            continue
        components += 1
        stack = [start]
        members = []
        seen.add(start)
        while stack:
            cur = stack.pop()
            members.append(cur)
            for nxt in adjacency[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        min_member = min(members)
        for member in members:
            component_of[member] = min_member

    node_table = []
    for i, xy in enumerate(ordered_coords):
        node_table.append((i, xy[0], xy[1], degree[i], refs[i],
                           component_of[i]))
    return {
        "scanned": scanned,
        "excluded": excluded,
        "type_rejected": type_rejected,
        "parts": len(edges),
        "nodes": node_table,
        "edges": edge_table,
        "components": components,
        "zero_length_parts": sum(
            1 for e in edge_table if _length(e[5]) == 0.0),
    }
