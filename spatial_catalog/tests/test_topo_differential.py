"""Differential tests: production topology vs the independent naive oracle.

The oracle (topo_oracle.py) re-derives nodes, degrees, incident references,
edges and components with a plain dict coordinate map and explicit-stack
graph walk, sharing no code with the external-sort / disk union-find
build.  Random small graphs cover multi-part order, reversed coordinates,
duplicate edges, isolated lines, exact vs near-equal coordinates and
Unicode source ids.
"""

import json
import random

import pytest

from spatial_catalog.builder import build as build_catalog
from spatial_catalog.topo import Selection, build_topology
from spatial_catalog.topo_oracle import expected_network
from spatial_catalog.treader import Topology
from helpers import write_records


def _build_from_records(tmp_path, records, cache_bytes=1 << 16,
                        selection=None, exclude_non_lines=False):
    src = write_records(tmp_path / "in.jsonl", records)
    cat = str(tmp_path / "cat")
    build_catalog(src, cat, cache_bytes=cache_bytes, force=True)
    out = str(tmp_path / "topo")
    report = build_topology(
        cat, out, selection=selection or Selection(),
        exclude_non_lines=exclude_non_lines, cache_bytes=cache_bytes,
        max_spill_bytes=1 << 26, force=True)
    return cat, out, report


def _artifact_tables(topo_dir):
    with Topology(topo_dir) as t:
        return list(t.iter_nodes()), list(t.iter_edges())


def _assert_matches_oracle(records, topo_dir, selected=None):
    expected = expected_network(records, selected=selected)
    nodes, edges = _artifact_tables(topo_dir)
    assert len(nodes) == len(expected["nodes"])
    for got, exp in zip(nodes, expected["nodes"]):
        assert got == exp, "node mismatch: %r != %r" % (got, exp)
    assert len(edges) == len(expected["edges"])
    for got, exp in zip(edges, expected["edges"]):
        e_ord, oid, part, fo, to, coords, length, comp = got
        ee_ord, eoid, epart, efo, eto, ecoords, elength = exp
        assert (e_ord, oid, part, fo, to) == (ee_ord, eoid, epart, efo, eto)
        assert coords == ecoords
        assert length == elength
        assert comp == expected["nodes"][fo][5]
    return expected


def test_random_graphs_differential(tmp_path):
    rng = random.Random(101)
    for trial in range(12):
        records = []
        # a small integer coordinate pool guarantees exact end sharing
        pool = [(x, y) for x in range(6) for y in range(6)]
        n_lines = rng.randint(1, 14)
        for i in range(n_lines):
            n_pts = rng.randint(2, 4)
            coords = [pool[rng.randrange(len(pool))] for _ in range(n_pts)]
            records.append({"object_id": "r%02d-%02d" % (trial, i),
                            "geometry": {"type": "LineString",
                                         "coordinates": coords}})
        # reversed copies and duplicate sources
        if rng.random() < 0.5:
            src = records[rng.randrange(len(records))]
            records.append({"object_id": src["object_id"] + "-rev",
                            "geometry": {"type": "LineString",
                                         "coordinates":
                                             list(reversed(src["geometry"]["coordinates"]))}})
        if rng.random() < 0.4:
            dup = dict(records[rng.randrange(len(records))])
            dup = {"object_id": dup["object_id"] + "-dup",
                   "geometry": dup["geometry"]}
            records.append(dup)
        d = tmp_path / ("t%d" % trial)
        d.mkdir()
        _cat, topo, report = _build_from_records(d, records)
        exp = _assert_matches_oracle(records, topo)
        assert report["components"] == exp["components"]
        with Topology(topo) as t:
            assert t.validate_full(catalog=_cat)["ok"]


def test_multipart_order_and_traceability_differential(tmp_path):
    rng = random.Random(102)
    records = []
    for i in range(8):
        n_parts = rng.randint(1, 4)
        parts = []
        for _ in range(n_parts):
            coords = [(rng.randint(0, 5), rng.randint(0, 5))
                      for _ in range(rng.randint(2, 3))]
            parts.append(coords)
        records.append({"object_id": "ml-%02d" % i,
                        "geometry": {"type": "MultiLineString",
                                     "coordinates": parts}})
    _cat, topo, report = _build_from_records(tmp_path, records)
    exp = _assert_matches_oracle(records, topo)
    assert report["parts_parsed"] == exp["parts"]
    # every part index 0..n-1 of every source appears exactly once
    with Topology(topo) as t:
        idents = sorted((e[1], e[2]) for e in t.iter_edges())
    expected_idents = sorted((r["object_id"], p)
                             for r in records
                             for p in range(len(r["geometry"]["coordinates"])))
    assert idents == expected_idents


def test_unicode_source_ids_differential(tmp_path):
    records = [
        {"object_id": "线-甲", "geometry": {"type": "LineString",
                                          "coordinates": [[0, 0], [1, 0]]}},
        {"object_id": "线-乙", "geometry": {"type": "LineString",
                                          "coordinates": [[1, 0], [2, 0]]}},
        {"object_id": "Ω-edge", "geometry": {"type": "LineString",
                                             "coordinates": [[2, 0], [2, 1]]}},
        {"object_id": "å-end", "geometry": {"type": "LineString",
                                            "coordinates": [[9, 9], [2, 1]]}},
    ]
    random.Random(103).shuffle(records)
    _cat, topo, _ = _build_from_records(tmp_path, records)
    exp = _assert_matches_oracle(records, topo)
    with Topology(topo) as t:
        ids = [e[1] for e in t.iter_edges()]
    # Unicode code point order: Latin < Greek < CJK
    assert ids == sorted(ids)
    assert ids[0] == "å-end"
    assert exp["components"] == 1


def test_selection_filter_differential(tmp_path):
    records = [
        {"object_id": "keep-1", "geometry": {"type": "LineString",
                                             "coordinates": [[0, 0], [1, 0]]}},
        {"object_id": "keep-2", "geometry": {"type": "LineString",
                                             "coordinates": [[1, 0], [2, 0]]}},
        {"object_id": "drop-1", "geometry": {"type": "LineString",
                                             "coordinates": [[5, 5], [6, 5]]}},
    ]
    src = write_records(tmp_path / "in.jsonl", records)
    cat = str(tmp_path / "cat")
    build_catalog(src, cat, cache_bytes=1 << 16, force=True)
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("# selection\nkeep-1\nkeep-2\n")
    sel = Selection(ids_file=str(ids_file))
    out = str(tmp_path / "topo")
    report = build_topology(cat, out, selection=sel, cache_bytes=1 << 16,
                            max_spill_bytes=1 << 26, force=True)
    assert report["records_excluded_filter"] == 1
    _assert_matches_oracle(records, out, selected={"keep-1", "keep-2"})
    with Topology(out) as t:
        assert t.validate_full(catalog=cat)["ok"]


def test_prefix_selection_differential(tmp_path):
    records = [
        {"object_id": "net-a", "geometry": {"type": "LineString",
                                            "coordinates": [[0, 0], [1, 0]]}},
        {"object_id": "net-b", "geometry": {"type": "LineString",
                                            "coordinates": [[1, 0], [1, 1]]}},
        {"object_id": "other", "geometry": {"type": "LineString",
                                            "coordinates": [[7, 7], [8, 7]]}},
    ]
    _cat, topo, report = _build_from_records(
        tmp_path, records,
        selection=Selection(id_prefixes=("net-",)))
    assert report["records_excluded_filter"] == 1
    _assert_matches_oracle(records, topo, selected={"net-a", "net-b"})


def test_empty_selection_is_valid_empty_network(tmp_path):
    records = [
        {"object_id": "a", "geometry": {"type": "LineString",
                                        "coordinates": [[0, 0], [1, 0]]}},
    ]
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("# nothing selected\n\n")
    _cat, topo, report = _build_from_records(
        tmp_path, records, selection=Selection(ids_file=str(ids_file)))
    assert (report["nodes"], report["edges"], report["components"]) == (0, 0, 0)
    with Topology(topo) as t:
        summary = t.validate_full(catalog=_cat)
    assert summary["ok"] and summary["nodes"] == 0
    exp = expected_network(records, selected=set())
    nodes, edges = _artifact_tables(topo)
    assert nodes == exp["nodes"] == []
    assert edges == exp["edges"] == []
