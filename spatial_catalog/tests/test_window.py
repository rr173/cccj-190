"""Window query tests: closed rectangles, boundary touch, holes,
degenerate rectangles, and real traversal statistics."""

import math

import pytest

from spatial_catalog.brute import load_records, window_scan
from spatial_catalog.errors import QueryError
from helpers import build_catalog, open_catalog, point, square, linestring, write_records


def test_hole_bbox_hit_but_no_intersection(tmp_path):
    """Polygon with a hole; query rect sits entirely inside the hole:
    the bbox matches (payload IS decoded) but exact intersects is False."""
    donut = square(0, 0, 10, 10, hole=[[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]])
    records = [
        {"object_id": "donut", "geometry": donut},
        {"object_id": "far", "geometry": point(100, 100)},
    ]
    cat_dir, _ = build_catalog(tmp_path, records)
    with open_catalog(cat_dir) as cat:
        res = cat.window(4.5, 4.5, 5.5, 5.5)
        ids = list(res)
        assert ids == []
        # the walk really reached the leaf and decoded the payload
        assert res.stats.nodes_visited >= 1
        assert res.stats.payloads_decoded == 1
        assert res.stats.exact_predicates == 1
        # brute force agrees
        records_bf = load_records(tmp_path / "in.jsonl")
        assert window_scan(records_bf, 4.5, 4.5, 5.5, 5.5) == []


def test_boundary_touch_counts_as_intersection(tmp_path):
    donut = square(0, 0, 10, 10, hole=[[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]])
    records = [{"object_id": "donut", "geometry": donut}]
    cat_dir, _ = build_catalog(tmp_path, records)
    with open_catalog(cat_dir) as cat:
        # rect touches the polygon's right edge x=10 exactly
        assert sorted(cat.window(10, 0, 12, 10)) == ["donut"]
        # rect touches a single corner point (10, 10)
        assert sorted(cat.window(10, 10, 20, 20)) == ["donut"]
        # rect just barely misses
        assert list(cat.window(10.0001, 0, 12, 10)) == []


def test_degenerate_rectangles(tmp_path):
    records = [
        {"object_id": "vline", "geometry": linestring([[5, 0], [5, 10]])},
        {"object_id": "pt", "geometry": point(5, 5)},
        {"object_id": "box", "geometry": square(20, 20, 30, 30)},
    ]
    cat_dir, _ = build_catalog(tmp_path, records)
    with open_catalog(cat_dir) as cat:
        # zero-width rectangle (vertical segment) crossing the line
        assert sorted(cat.window(5, 2, 5, 8)) == ["pt", "vline"]
        # zero-area rectangle (a point) exactly on the line
        assert sorted(cat.window(5, 3, 5, 3)) == ["vline"]
        # zero-area rectangle off everything
        assert list(cat.window(7, 7, 7, 7)) == []
        # very thin rectangle
        assert sorted(cat.window(4.999, 0, 5.001, 10)) == ["pt", "vline"]


def test_window_on_empty_catalog(tmp_path):
    cat_dir, report = build_catalog(tmp_path, [])
    assert report["object_count"] == 0
    with open_catalog(cat_dir) as cat:
        res = cat.window(-10, -10, 10, 10)
        assert list(res) == []
        assert res.stats.nodes_visited == 1  # the empty root page
        assert res.stats.payloads_decoded == 0


def test_window_parameter_validation(tmp_path):
    cat_dir, _ = build_catalog(tmp_path, [{"object_id": "a", "geometry": point(0, 0)}])
    with open_catalog(cat_dir) as cat:
        with pytest.raises(QueryError):
            cat.window(5, 0, 1, 1)          # minx > maxx
        with pytest.raises(QueryError):
            cat.window(0, 0, 1, math.nan)   # non-finite
        with pytest.raises(QueryError):
            cat.window(0, 0, math.inf, 1)
        with pytest.raises(QueryError):
            cat.window(0, 0, "x", 1)        # not a number


def test_window_matches_brute_on_mixed(tmp_path):
    from spatial_catalog import datagen
    records = datagen.gen_sparse_records(300, seed=11)
    cat_dir, _ = build_catalog(tmp_path, records)
    bf = load_records(tmp_path / "in.jsonl")
    with open_catalog(cat_dir) as cat:
        for rect in [(0, 0, 50, 50), (100, 100, 500, 500), (0, 0, 1000, 1000),
                     (250, 250, 250, 250), (999, 999, 1001, 1001)]:
            got = sorted(cat.window(*rect))
            assert got == window_scan(bf, *rect)
