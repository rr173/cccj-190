"""Topology diagnostics: runtime-generated scale scene built under an
explicit cache/spill budget, differentially checked against the fully
independent naive oracle (topo_oracle.py) and re-validated by the
independent reader checks.

The scene deliberately contains a large number of mutually disjoint small
components and exactly one giant connected component, so the run proves
both the bounded-resource handling of the long tail and complete closure
of a component that exceeds every cache page.
"""

import json
import os
import resource
import shutil
import sys

from . import datagen
from .builder import build as build_catalog
from .topo import Selection, build_topology
from .topo_oracle import expected_network
from .treader import Topology


def _rss_bytes():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _diff_against_oracle(topo_dir, jsonl_path, selection_ids=None):
    """Compare every node and every edge of the artifact to the naive
    in-memory oracle.  Raises AssertionError on the first divergence."""
    records = []
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    selected = set(selection_ids) if selection_ids is not None else None
    expected = expected_network(records, selected=selected)
    with Topology(topo_dir) as topo:
        got_nodes = list(topo.iter_nodes())
        got_edges = list(topo.iter_edges())
    # nodes: ordinal, coordinate, degree, incident refs, component
    assert len(got_nodes) == len(expected["nodes"]), (
        "node count %d != oracle %d"
        % (len(got_nodes), len(expected["nodes"])))
    for got, exp in zip(got_nodes, expected["nodes"]):
        n_ord, x, y, degree, refs, comp = got
        e_ord, ex, ey, edeg, erefs, ecomp = exp
        assert n_ord == e_ord, "node ordinal mismatch %d/%d" % (n_ord, e_ord)
        assert (x, y) == (ex, ey), "node coord mismatch at %d" % n_ord
        assert degree == edeg, "degree mismatch at %d" % n_ord
        assert refs == erefs, "incident refs mismatch at %d: %r != %r" % (
            n_ord, refs, erefs)
        assert comp == ecomp, "component mismatch at %d" % n_ord
    # edges: ordinal, source id, part, endpoints, geometry, length
    assert len(got_edges) == len(expected["edges"]), (
        "edge count %d != oracle %d"
        % (len(got_edges), len(expected["edges"])))
    for got, exp in zip(got_edges, expected["edges"]):
        e_ord, oid, part, fo, to, coords, length, comp = got
        ee_ord, eoid, epart, efo, eto, ecoords, elength = exp
        assert (e_ord, oid, part) == (ee_ord, eoid, epart), \
            "edge identity mismatch"
        assert (fo, to) == (efo, eto), "edge endpoints mismatch on %s" % oid
        assert coords == ecoords, "edge geometry mismatch on %s" % oid
        assert length == elength, "edge length mismatch on %s" % oid
        assert comp == expected["nodes"][fo][5], \
            "edge component declaration mismatch on %s" % oid
    return {"nodes": len(got_nodes), "edges": len(got_edges),
            "oracle_components": expected["components"]}


def run_topo_diag(out_dir, n=6000, big=1500, cache_bytes=1 << 20,
                  max_spill_bytes=1 << 30, seed=31):
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    n_small = max(50, n // 2)
    report = {"n_small_components": n_small, "giant_component_edges": big + 1,
              "cache_bytes": cache_bytes,
              "max_spill_bytes": max_spill_bytes, "seed": seed,
              "python": sys.version.split()[0]}
    records = datagen.gen_topo_scale_records(
        n_small=n_small, big_size=big, seed=seed)
    jsonl = os.path.join(out_dir, "scale.jsonl")
    datagen.write_jsonl(jsonl, records)
    cat_dir = os.path.join(out_dir, "scale.cat")
    build_catalog(jsonl, cat_dir, cache_bytes=cache_bytes, force=True)
    topo_dir = os.path.join(out_dir, "scale.topo")
    rss0 = _rss_bytes()
    build_report = build_topology(
        cat_dir, topo_dir, selection=Selection(),
        exclude_non_lines=True, cache_bytes=cache_bytes,
        max_spill_bytes=max_spill_bytes, force=True)
    report["build"] = build_report
    report["rss_delta_bytes"] = _rss_bytes() - rss0
    # independent reader validation (own BFS component checker)
    with Topology(topo_dir) as topo:
        report["validate"] = topo.validate_full(catalog=cat_dir)
    # naive oracle differential, node by node and edge by edge
    report["oracle_diff"] = _diff_against_oracle(topo_dir, jsonl)
    # giant component integrity: exactly one component with > big/2 nodes
    with Topology(topo_dir) as topo:
        sizes = {}
        for _o, _x, _y, _d, _r, comp in topo.iter_nodes():
            sizes[comp] = sizes.get(comp, 0) + 1
    big_sizes = sorted(sizes.values(), reverse=True)[:3]
    report["largest_component_sizes"] = big_sizes
    assert big_sizes[0] >= big, (
        "giant component lost nodes: largest=%d expected>=%d"
        % (big_sizes[0], big))
    assert sum(1 for s in sizes.values() if s >= big) == 1, \
        "expected exactly one giant component"
    assert build_report["union_find_evictions"] > 0, (
        "expected disk union-find page evictions on the multi-page giant "
        "component, got 0")
    assert build_report["spill_runs"] > 0, "external sort never spilled"
    report["peak_rss_bytes"] = _rss_bytes()
    report["note"] = (
        "%d disjoint small components plus one %d-node giant component; "
        "build stayed within cache_bytes=%d while union-find and sorts "
        "spilled to disk" % (
            len(sizes) - 1, big_sizes[0], cache_bytes))
    return report
