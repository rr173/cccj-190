"""Randomised differential tests: indexed queries vs an independent
sequential full scan (brute.py) that shares no tree/pruning code."""

import random

import pytest

from spatial_catalog import datagen
from spatial_catalog.brute import load_records, nearest_scan, window_scan
from helpers import build_catalog, open_catalog

SCENES = [
    ("sparse", lambda seed: datagen.gen_sparse_records(1200, seed=seed)),
    ("mixed", lambda seed: datagen.gen_mixed_records(900, seed=seed)),
    ("overlap", lambda seed: datagen.gen_overlap_records(500, seed=seed)),
]


@pytest.mark.parametrize("scene_name,gen", SCENES, ids=[s for s, _ in SCENES])
@pytest.mark.parametrize("seed", [101, 202])
def test_differential_window_and_nearest(tmp_path, scene_name, gen, seed):
    records = gen(seed)
    # small cache on purpose: build must spill, queries must still be exact
    cat_dir, report = build_catalog(tmp_path, records, cache_bytes=1 << 20)
    bf = load_records(tmp_path / "in.jsonl")
    assert report["object_count"] == len(bf)
    rng = random.Random(seed * 7 + 1)
    extent = 1000.0 if scene_name != "mixed" else 2000.0

    with open_catalog(cat_dir) as cat:
        total_pages = cat.page_count
        n = len(bf)
        for _ in range(40):
            cx = rng.uniform(-50, extent)
            cy = rng.uniform(-50, extent)
            w = rng.choice([0.0, rng.uniform(0.5, 5), rng.uniform(5, 200)])
            h = rng.choice([0.0, rng.uniform(0.5, 5), rng.uniform(5, 200)])
            rect = (cx, cy, cx + w, cy + h)
            res = cat.window(*rect)
            got = sorted(res)
            expect = window_scan(bf, *rect)
            assert got == expect, "window %r mismatch" % (rect,)
            # stats are real and bounded by a full scan
            assert 1 <= res.stats.nodes_visited <= total_pages
            assert res.stats.payloads_decoded <= n
            assert res.stats.exact_predicates == res.stats.payloads_decoded
            assert res.stats.payloads_decoded >= len(expect)

        for _ in range(40):
            x = rng.uniform(-50, extent)
            y = rng.uniform(-50, extent)
            k = rng.choice([0, 1, 2, 5, 25, len(bf), len(bf) + 10])
            res = cat.nearest(x, y, k)
            got = list(res.results)
            expect = nearest_scan(bf, x, y, k)
            assert got == expect, "nearest (%r,%r) k=%r mismatch" % (x, y, k)
            assert res.stats.nodes_visited <= total_pages
            assert res.stats.payloads_decoded <= n
            assert res.stats.exact_predicates == res.stats.payloads_decoded
            if k > 0 and n > 0:
                assert res.stats.nodes_visited >= 1


def test_differential_duplicate_bbox_scene(tmp_path):
    """Many geometries share identical bboxes (same square, many ids)."""
    from helpers import square
    records = [{"object_id": "dup-%03d" % i,
                "geometry": square(10, 10, 20, 20)} for i in range(50)]
    records += [{"object_id": "loner", "geometry": {"type": "Point",
                                                    "coordinates": [500, 500]}}]
    cat_dir, _ = build_catalog(tmp_path, records)
    bf = load_records(tmp_path / "in.jsonl")
    with open_catalog(cat_dir) as cat:
        for k in (1, 7, 50, 51, 60):
            got = list(cat.nearest(15, 15, k).results)
            assert got == nearest_scan(bf, 15, 15, k)
        got = sorted(cat.window(15, 15, 15, 15))
        assert got == window_scan(bf, 15, 15, 15, 15)
