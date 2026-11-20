"""Differential tests: random small scenarios compared against the
independent per-cell oracle (ioracle.py) -- point by point for states,
evidence and change totals, plus a separate check of the rectangle merging
and of input-split / read-order / cache-size independence."""

import json
import os
import random

import pytest

from spatial_catalog import datagen
from spatial_catalog import iformat as F
from spatial_catalog import ioracle as O
from insp_helpers import (make_registry, make_topology, parse_cov_rows,
                          parse_diff_rows, run_coverage, write_jsonl)


def _build(tmp_path, edges, records, revisions, window, cutoffs,
           cache_bytes=1 << 16, split=1, order=None, tag="out"):
    topo = make_topology(tmp_path, records, name="topo-%s" % tag)
    reg = make_registry(tmp_path, edges, name="reg-%s.jsonl" % tag)
    revs = list(revisions)
    if order == "reverse":
        revs = list(reversed(revs))
    elif order == "shuffle":
        random.Random(1234).shuffle(revs)
    files = []
    if split <= 1:
        files.append(write_jsonl(tmp_path / ("insp-%s.jsonl" % tag), revs))
    else:
        # split the same batch of revisions across several files
        buckets = [[] for _ in range(split)]
        for i, rev in enumerate(revs):
            buckets[i % split].append(rev)
        for k, bucket in enumerate(buckets):
            files.append(write_jsonl(
                tmp_path / ("insp-%s-%d.jsonl" % (tag, k)), bucket))
    out = str(tmp_path / tag)
    report = run_coverage(topo, reg, files, out, window, cutoffs,
                          cache_bytes=cache_bytes)
    return out, report


def _read_all(out):
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    after = parse_cov_rows(os.path.join(out, F.COVERAGE_AFTER_NAME))
    diff = parse_diff_rows(os.path.join(out, F.DIFF_NAME))
    summary = json.load(open(os.path.join(out, F.SUMMARY_NAME)))
    return before, after, diff, summary


def _unmerge(rows):
    """Expand coverage rows to a per-cell map (small grids only)."""
    grid = {}
    for (sid, part, d0, d1, c0, c1, status, iid, batch) in rows:
        for d in range(d0, d1):
            for c in range(c0, c1):
                grid[(sid, part, d, c)] = (status, iid, batch)
    return grid


def _oracle_grid_cells(edges, revisions, cutoff, from_ord, to_ord):
    grid = O.coverage_grid(edges, revisions, cutoff, from_ord, to_ord)
    cells = {}
    for (sid, part), cellmap in grid.items():
        for (d, c), val in cellmap.items():
            cells[(sid, part, d, c)] = val
    return cells


def _assert_maximally_merged(rows, edges):
    """The delivered rectangles must be in canonical (maximally merged)
    form: no chainage-adjacent equal-value pair inside a date run, and no
    two adjacent date runs with identical profiles."""
    by_edge = {}
    for row in rows:
        by_edge.setdefault((row[0], row[1]), []).append(row)
    for edge, erows in by_edge.items():
        # group into date runs
        runs = []
        cur = []
        for row in erows:
            d0, d1 = row[2], row[3]
            if cur and (cur[0][2], cur[0][3]) != (d0, d1):
                runs.append(cur)
                cur = []
            cur.append(row)
        if cur:
            runs.append(cur)
        # within a run: no adjacent equal-value chainage segments
        for run in runs:
            for a, b in zip(run, run[1:]):
                assert a[5] == b[4], "run not chainage-contiguous"
                assert (a[6], a[7], a[8]) != (b[6], b[7], b[8]), \
                    "adjacent equal-value segments were not merged"
        # across runs: adjacent runs must not have identical profiles
        for ra, rb in zip(runs, runs[1:]):
            pa = [(r[4], r[5], r[6], r[7], r[8]) for r in ra]
            pb = [(r[4], r[5], r[6], r[7], r[8]) for r in rb]
            assert pa != pb, "adjacent identical profiles were not merged"


@pytest.mark.parametrize("seed", range(30))
def test_random_scenario_vs_oracle(tmp_path, seed):
    rng = random.Random(1000 + seed)
    (edges, records, revisions, window, from_ord,
     to_ord) = datagen.gen_insp_random(rng, n_edges=4, max_L=10,
                                       window_days=5, n_ids=5, max_batch=5)
    cb = rng.randint(0, 3)
    ca = cb + rng.randint(0, 2)
    out, report = _build(tmp_path, edges, records, revisions, window,
                         (cb, ca))
    before, after, diff, summary = _read_all(out)

    # row-level exact comparison against the oracle
    exp_before = O.coverage_rows(edges, revisions, cb, from_ord, to_ord)
    exp_after = O.coverage_rows(edges, revisions, ca, from_ord, to_ord)
    exp_diff, exp_summary = O.diff_and_summary(edges, revisions, cb, ca,
                                               from_ord, to_ord)
    assert before == exp_before
    assert after == exp_after
    assert diff == exp_diff
    assert summary["status_changes"] == exp_summary["status_changes"]
    assert summary["evidence_only_changes"] == exp_summary["evidence_only"]

    # the delivered artifact must pass the independent validator (which
    # re-derives the diff from the two ledgers and checks the summary)
    from spatial_catalog.ireader import Coverage
    with Coverage(out) as cov:
        assert cov.validate_full(registry=False, topology=False)["ok"]

    # point-by-point (per-cell) comparison of states and evidence
    assert _unmerge(before) == _oracle_grid_cells(edges, revisions, cb,
                                                  from_ord, to_ord)
    assert _unmerge(after) == _oracle_grid_cells(edges, revisions, ca,
                                                 from_ord, to_ord)

    # change totals recomputed per cell match the delivered summary
    bcells = _oracle_grid_cells(edges, revisions, cb, from_ord, to_ord)
    acells = _oracle_grid_cells(edges, revisions, ca, from_ord, to_ord)
    st = ev = 0
    for key, bval in bcells.items():
        aval = acells[key]
        if bval == aval:
            continue
        if bval[0] != aval[0]:
            st += 1
        else:
            ev += 1
    assert summary["status_change_total"] == st
    assert summary["evidence_only_total"] == ev

    # rectangle merging is canonical
    _assert_maximally_merged(before, edges)
    _assert_maximally_merged(after, edges)


def test_split_order_cache_independence(tmp_path):
    """The same batch of revisions, split differently across files, read in
    a different order, or processed with a different cache size, must not
    change the table rows or the business totals."""
    rng = random.Random(777)
    (edges, records, revisions, window, from_ord,
     to_ord) = datagen.gen_insp_random(rng, n_edges=5, max_L=12,
                                       window_days=6, n_ids=8, max_batch=6)
    cutoffs = (2, 5)
    variants = [
        dict(split=1, order=None, cache_bytes=1 << 16, tag="base"),
        dict(split=3, order=None, cache_bytes=1 << 16, tag="split"),
        dict(split=1, order="shuffle", cache_bytes=1 << 16, tag="shuffled"),
        dict(split=1, order="reverse", cache_bytes=1 << 16, tag="reversed"),
        dict(split=1, order=None, cache_bytes=4 << 20, tag="bigcache"),
    ]
    results = []
    for v in variants:
        out, report = _build(tmp_path, edges, records, revisions, window,
                             cutoffs, cache_bytes=v["cache_bytes"],
                             split=v["split"], order=v["order"],
                             tag=v["tag"])
        before, after, diff, summary = _read_all(out)
        results.append((before, after, diff, summary))
    base = results[0]
    for got in results[1:]:
        assert got[0] == base[0], "coverage_before changed"
        assert got[1] == base[1], "coverage_after changed"
        assert got[2] == base[2], "diff changed"
        assert got[3]["status_changes"] == base[3]["status_changes"]
        assert got[3]["evidence_only_changes"] == \
            base[3]["evidence_only_changes"]


def test_evidence_only_swap_is_not_a_status_change(tmp_path):
    """A region that only changes evidence (same status) must be reported
    as an evidence-only replacement, never as a status change."""
    edges = {("e1", 0): 10}
    records = [{"object_id": "e1", "geometry":
                {"type": "LineString", "coordinates": [[0, 0], [10, 0]]}}]
    revisions = [
        {"inspection_id": "A", "batch": 1, "action": "upsert",
         "source_id": "e1", "part_index": 0, "direction": "forward",
         "lo": 0, "hi": 10, "start": "2026-11-01", "end": "2026-11-04",
         "checked_on": "2026-11-01", "priority": 0, "result": "pass"},
        {"inspection_id": "A", "batch": 2, "action": "upsert",
         "source_id": "e1", "part_index": 0, "direction": "forward",
         "lo": 0, "hi": 10, "start": "2026-11-01", "end": "2026-11-04",
         "checked_on": "2026-11-02", "priority": 0, "result": "pass"},
    ]
    window = ("2026-11-01", "2026-11-04")
    out, _ = _build(tmp_path, edges, records, revisions, window, (1, 2))
    _b, _a, diff, summary = _read_all(out)
    assert summary["status_change_total"] == 0
    assert summary["evidence_only_total"] == 30   # 10 units x 3 days
    assert summary["evidence_only_changes"] == {"pass>pass": 30}
    # the diff row carries both evidence keys but the same status
    assert len(diff) == 1
    assert diff[0][6] == diff[0][9] == "pass"
    assert (diff[0][7], diff[0][8]) == ("A", 1)
    assert (diff[0][10], diff[0][11]) == ("A", 2)
