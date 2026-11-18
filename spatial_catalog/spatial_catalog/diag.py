"""Diagnostics: build generated datasets, run query suites, and report
real traversal statistics (node pages visited, payloads decoded, exact
predicates) against full-scan cost, plus peak RSS.  This is the honest
performance evidence: sparse data must show a large win, the overlap
worst case is reported as-is (degradation included)."""

import os
import random
import resource
import shutil
import sys

from . import datagen
from .brute import load_records, nearest_scan, window_scan
from .builder import build
from .catalog import Catalog


def _rss_bytes():
    # ru_maxrss is KiB on Linux, bytes on macOS; Linux is the target.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _suite(cat, records, rng, n_window, n_nearest, extent):
    rows = []
    n = len(records)
    for i in range(n_window):
        cx = rng.uniform(0, extent)
        cy = rng.uniform(0, extent)
        w = rng.uniform(0.001, 0.02) * extent
        rect = (cx, cy, cx + w, cy + w)
        res = cat.window(*rect)
        ids = sorted(res)
        expect = window_scan(records, *rect)
        assert ids == expect, "window mismatch at %r" % (rect,)
        rows.append({"kind": "window", "nodes": res.stats.nodes_visited,
                     "payloads": res.stats.payloads_decoded,
                     "predicates": res.stats.exact_predicates,
                     "hits": len(ids)})
    for i in range(n_nearest):
        x = rng.uniform(0, extent)
        y = rng.uniform(0, extent)
        k = rng.choice([1, 5, 20])
        res = cat.nearest(x, y, k)
        expect = nearest_scan(records, x, y, k)
        got = [(oid, d) for oid, d in res.results]
        assert got == expect, "nearest mismatch at (%r,%r)" % (x, y)
        rows.append({"kind": "nearest", "nodes": res.stats.nodes_visited,
                     "payloads": res.stats.payloads_decoded,
                     "predicates": res.stats.exact_predicates,
                     "pq_spills": res.stats.pq_spills,
                     "pq_high_water": res.stats.pq_high_water,
                     "hits": len(got)})
    return rows


def _summarise(rows):
    out = {}
    for row in rows:
        agg = out.setdefault(row["kind"], {"queries": 0, "nodes": 0,
                                           "payloads": 0, "predicates": 0})
        agg["queries"] += 1
        agg["nodes"] += row["nodes"]
        agg["payloads"] += row["payloads"]
        agg["predicates"] += row["predicates"]
    for agg in out.values():
        q = agg["queries"]
        agg["avg_nodes"] = round(agg.pop("nodes") / q, 1)
        agg["avg_payloads"] = round(agg.pop("payloads") / q, 1)
        agg["avg_predicates"] = round(agg.pop("predicates") / q, 1)
    return out


def run_diag(out_dir, n=100000, cache_bytes=8 << 20, seed=7):
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    report = {"n": n, "cache_bytes": cache_bytes, "seed": seed,
              "python": sys.version.split()[0]}

    # ---- sparse dataset -------------------------------------------------
    sparse_jsonl = os.path.join(out_dir, "sparse.jsonl")
    datagen.write_jsonl(sparse_jsonl, datagen.gen_sparse_records(n, seed=seed))
    rss0 = _rss_bytes()
    brep = build(sparse_jsonl, os.path.join(out_dir, "sparse.cat"),
                 cache_bytes=cache_bytes, force=True)
    report["sparse_build"] = brep
    report["sparse_build_rss_bytes"] = _rss_bytes()
    # NOTE: ru_maxrss is a monotonic process peak; data generation before
    # the build may already have set it, so this delta can read as ~0.
    # test_scale.py measures a real build-phase delta in a fresh process
    # (60k geometries, 4 MiB budget -> ~10 MiB delta).
    report["sparse_build_rss_delta_bytes"] = _rss_bytes() - rss0

    records = load_records(sparse_jsonl)
    rng = random.Random(seed + 1)
    with Catalog(os.path.join(out_dir, "sparse.cat"),
                 page_cache_bytes=cache_bytes,
                 queue_spill_bytes=cache_bytes) as cat:
        rows = _suite(cat, records, rng, n_window=30, n_nearest=30,
                      extent=1000.0)
        report["sparse"] = {
            "objects": cat.object_count,
            "pages": cat.page_count,
            "leaf_pages": cat.manifest["leaf_pages"],
            "full_scan_payloads": n,
            "queries": _summarise(rows),
        }

    # ---- overlap worst case --------------------------------------------
    m = max(2000, n // 5)
    overlap_jsonl = os.path.join(out_dir, "overlap.jsonl")
    datagen.write_jsonl(overlap_jsonl, datagen.gen_overlap_records(m, seed=seed))
    brep2 = build(overlap_jsonl, os.path.join(out_dir, "overlap.cat"),
                  cache_bytes=cache_bytes, force=True)
    report["overlap_build"] = brep2
    orecords = load_records(overlap_jsonl)
    rng = random.Random(seed + 2)
    with Catalog(os.path.join(out_dir, "overlap.cat"),
                 page_cache_bytes=cache_bytes,
                 queue_spill_bytes=cache_bytes) as cat:
        rows = _suite(cat, orecords, rng, n_window=10, n_nearest=10,
                      extent=1000.0)
        report["overlap"] = {
            "objects": cat.object_count,
            "pages": cat.page_count,
            "leaf_pages": cat.manifest["leaf_pages"],
            "full_scan_payloads": m,
            "queries": _summarise(rows),
            "note": "all bboxes overlap: degradation to near-full-scan is "
                    "expected and reported, not hidden",
        }
    report["peak_rss_bytes"] = _rss_bytes()
    return report
