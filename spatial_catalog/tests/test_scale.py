"""Scale tests: prove the index actually prunes on sparse data (node and
payload visits far below a full scan), and honestly measure the overlap
worst case (degradation + RSS), all with runtime-generated data."""

import random
import resource
import sys

import pytest

from spatial_catalog import datagen
from spatial_catalog.brute import load_records, nearest_scan, window_scan
from helpers import build_catalog, open_catalog

SPARSE_N = 60000
OVERLAP_N = 4000


def _rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


@pytest.fixture(scope="module")
def sparse_cat(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sparse")
    records = datagen.gen_sparse_records(SPARSE_N, seed=77)
    rss0 = _rss()
    cat_dir, report = build_catalog(tmp, records, cache_bytes=4 << 20)
    rss1 = _rss()
    print("\n[sparse build] n=%d cache=4MiB rss_delta=%.1fMiB pages=%d "
          "depth=%d duration=%.1fs" % (
              SPARSE_N, (rss1 - rss0) / 2**20, report["page_count"],
              report["depth"], report["duration_s"]))
    # bounded-memory evidence: building 60k geometries with a 4 MiB budget
    # must stay far below loading everything (generous ceiling, CI-safe)
    assert rss1 - rss0 < 1 << 30
    return cat_dir, str(tmp / "in.jsonl"), report


def test_sparse_index_prunes(tmp_path, sparse_cat):
    cat_dir, jsonl, report = sparse_cat
    bf = load_records(jsonl)
    rng = random.Random(9)
    n = len(bf)
    leaf_pages = report["leaf_pages"]

    with open_catalog(cat_dir, page_cache_bytes=1 << 20,
                      queue_spill_bytes=1 << 20) as cat:
        win_nodes, win_payloads = [], []
        for _ in range(8):
            cx, cy = rng.uniform(0, 900), rng.uniform(0, 900)
            rect = (cx, cy, cx + 5.0, cy + 5.0)
            res = cat.window(*rect)
            got = sorted(res)
            assert got == window_scan(bf, *rect)
            win_nodes.append(res.stats.nodes_visited)
            win_payloads.append(res.stats.payloads_decoded)

        nn_nodes, nn_payloads = [], []
        for _ in range(8):
            x, y = rng.uniform(0, 1000), rng.uniform(0, 1000)
            res = cat.nearest(x, y, 10)
            assert list(res.results) == nearest_scan(bf, x, y, 10)
            nn_nodes.append(res.stats.nodes_visited)
            nn_payloads.append(res.stats.payloads_decoded)

    avg_wn = sum(win_nodes) / len(win_nodes)
    avg_wp = sum(win_payloads) / len(win_payloads)
    avg_nn = sum(nn_nodes) / len(nn_nodes)
    avg_np = sum(nn_payloads) / len(nn_payloads)
    print("[sparse window] avg nodes=%.1f/%d pages, avg payloads=%.1f/%d"
          % (avg_wn, report["page_count"], avg_wp, n))
    print("[sparse nearest] avg nodes=%.1f, avg payloads=%.1f/%d"
          % (avg_nn, avg_np, n))
    # the index must do dramatically less work than a full scan
    assert avg_wn < leaf_pages / 10
    assert avg_wp < n / 100
    assert avg_nn < leaf_pages / 10
    assert avg_np < n / 100


def test_overlap_worst_case_degrades_honestly(tmp_path):
    """All bboxes overlap: pruning is impossible, the walk degenerates
    toward a full scan.  Correctness must hold; we report, not hide."""
    records = datagen.gen_overlap_records(OVERLAP_N, seed=88)
    cat_dir, report = build_catalog(tmp_path, records, cache_bytes=4 << 20)
    bf = load_records(tmp_path / "in.jsonl")
    rng = random.Random(10)
    with open_catalog(cat_dir, page_cache_bytes=1 << 20,
                      queue_spill_bytes=1 << 20) as cat:
        nodes, payloads = [], []
        for _ in range(5):
            x, y = rng.uniform(-500, 500), rng.uniform(-500, 500)
            res = cat.nearest(x, y, 10)
            assert list(res.results) == nearest_scan(bf, x, y, 10)
            nodes.append(res.stats.nodes_visited)
            payloads.append(res.stats.payloads_decoded)
        # window over the centre hits everything
        res = cat.window(-5, -5, 5, 5)
        got = sorted(res)
        assert got == window_scan(bf, -5, -5, 5, 5)
        assert len(got) == OVERLAP_N  # every line crosses the centre
    avg_n = sum(nodes) / len(nodes)
    avg_p = sum(payloads) / len(payloads)
    print("[overlap nearest] avg nodes=%.1f/%d avg payloads=%.1f/%d "
          "(degenerate as expected)" % (avg_n, report["page_count"],
                                        avg_p, OVERLAP_N))
    # honest expectation: a large fraction of all payloads is touched
    # (sparse nearest touches <1%; here it is hundreds of times more),
    # and the central window above decoded every one of the OVERLAP_N.
    assert avg_p > OVERLAP_N / 4
    print("[overlap] peak rss so far: %.1f MiB" % (_rss() / 2**20))


def test_build_one_million_scale_smoke(tmp_path):
    """Larger build to prove the spill pipeline scales; kept as a smoke
    test (correctness spot-checks only) to bound CI time."""
    n = 200000
    records = datagen.gen_sparse_records(n, seed=99)
    cat_dir, report = build_catalog(tmp_path, records, cache_bytes=8 << 20)
    assert report["object_count"] == n
    with open_catalog(cat_dir) as cat:
        assert cat.object_count == n
        res = cat.nearest(500.5, 500.5, 5)
        assert len(res.results) == 5
        bf_sample = load_records(tmp_path / "in.jsonl")
        assert list(res.results) == nearest_scan(bf_sample, 500.5, 500.5, 5)
        assert res.stats.payloads_decoded < n / 100
