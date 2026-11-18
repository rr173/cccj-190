"""Nearest-neighbour tests: exact equidistant ties, id ordering, stop
boundary, bbox-near-but-far, duplicate bboxes, boundary distances."""

import math

import pytest

from spatial_catalog.brute import load_records, nearest_scan
from spatial_catalog.errors import QueryError
from helpers import (build_catalog, linestring, open_catalog, point, square)


def test_four_equidistant_points_id_order_and_stop(tmp_path):
    """Four points at distance exactly 1.0 from the origin (integer
    coordinates, exactly representable): the tie is mathematical.
    Result must be id-ordered, and k=2 must give the two smallest ids."""
    from spatial_catalog.datagen import gen_equidistant_points
    # ids deliberately not in any spatial order
    ids = ["delta", "alpha", "charlie", "bravo"]
    records = gen_equidistant_points(ids, center=(0, 0), radius=1)
    # a fifth, strictly farther point: must not disturb the tie group
    records.append({"object_id": "zed", "geometry": point(2, 0)})
    cat_dir, _ = build_catalog(tmp_path, records)
    with open_catalog(cat_dir) as cat:
        res = cat.nearest(0, 0, 4)
        assert [oid for oid, _ in res.results] == \
            ["alpha", "bravo", "charlie", "delta"]
        assert all(d == 1.0 for _, d in res.results)  # exact, not approx
        res2 = cat.nearest(0, 0, 2)
        assert [oid for oid, _ in res2.results] == ["alpha", "bravo"]
        # k larger than the tie group: zed follows at distance 2.0
        res5 = cat.nearest(0, 0, 5)
        assert [oid for oid, _ in res5.results] == \
            ["alpha", "bravo", "charlie", "delta", "zed"]
        assert res5.results[-1][1] == 2.0
        # k > n returns everything, still ordered
        assert len(cat.nearest(0, 0, 100).results) == 5
        # k = 0 is a normal, empty answer
        res0 = cat.nearest(0, 0, 0)
        assert res0.results == []
        assert res0.stats.nodes_visited == 0


def test_equidistant_unicode_ids_codepoint_order(tmp_path):
    from spatial_catalog.datagen import gen_equidistant_points
    ids = ["é", "z", "a", "Ω"]  # code points: a(0x61) z(0x7A) é(0xE9) Ω(0x3A9)
    records = gen_equidistant_points(ids, center=(5, 5), radius=3)
    cat_dir, _ = build_catalog(tmp_path, records)
    with open_catalog(cat_dir) as cat:
        res = cat.nearest(5, 5, 4)
        # Unicode code point order: a < z < é(0xE9) ... note Ω is 0x3A9
        assert [oid for oid, _ in res.results] == \
            sorted(ids, key=lambda s: [ord(c) for c in s])
        assert all(d == 3.0 for _, d in res.results)


def test_bbox_near_but_geometry_far(tmp_path):
    """Long diagonal line: its bbox corner is ~1.4 from the query point,
    but the line itself is ~707 away.  A naive bbox-distance kNN would
    wrongly prefer it over the point at (2,2)."""
    records = [
        {"object_id": "diagonal",
         "geometry": linestring([[1, 1000], [1000, 1]])},
        {"object_id": "near", "geometry": point(2, 2)},
    ]
    cat_dir, _ = build_catalog(tmp_path, records)
    with open_catalog(cat_dir) as cat:
        res = cat.nearest(0, 0, 1)
        assert res.results[0][0] == "near"
        assert res.results[0][1] == pytest.approx(math.hypot(2, 2))
        res2 = cat.nearest(0, 0, 2)
        assert res2.results[1][0] == "diagonal"
        assert res2.results[1][1] == pytest.approx(1001 / math.sqrt(2), rel=1e-9)


def test_duplicate_bboxes_tie_by_id(tmp_path):
    records = [
        {"object_id": "b", "geometry": point(5, 5)},
        {"object_id": "a", "geometry": point(5, 5)},
        {"object_id": "c", "geometry": point(5, 5)},
        {"object_id": "far", "geometry": point(50, 50)},
    ]
    cat_dir, _ = build_catalog(tmp_path, records)
    with open_catalog(cat_dir) as cat:
        res = cat.nearest(5, 5, 3)
        assert [oid for oid, _ in res.results] == ["a", "b", "c"]
        assert all(d == 0.0 for _, d in res.results)
        res1 = cat.nearest(5, 5, 1)
        assert res1.results == [("a", 0.0)]


def test_query_point_on_boundary(tmp_path):
    records = [
        {"object_id": "box", "geometry": square(0, 0, 10, 10)},
        {"object_id": "seg", "geometry": linestring([[20, 0], [20, 10]])},
        {"object_id": "pt", "geometry": point(30, 5)},
    ]
    cat_dir, _ = build_catalog(tmp_path, records)
    with open_catalog(cat_dir) as cat:
        # on the polygon's right edge
        res = cat.nearest(10, 5, 1)
        assert res.results[0] == ("box", 0.0)
        # exactly on the linestring; box edge x=10 is 10 away, pt is hypot(10,2)
        res = cat.nearest(20, 7, 3)
        assert res.results[0] == ("seg", 0.0)
        assert res.results[1] == ("box", 10.0)
        assert res.results[2][0] == "pt"
        assert res.results[2][1] == pytest.approx(math.hypot(10, 2))


def test_nearest_on_empty_catalog(tmp_path):
    cat_dir, _ = build_catalog(tmp_path, [])
    with open_catalog(cat_dir) as cat:
        res = cat.nearest(1, 2, 10)
        assert res.results == []
        assert res.stats.payloads_decoded == 0


def test_nearest_parameter_validation(tmp_path):
    cat_dir, _ = build_catalog(tmp_path, [{"object_id": "a", "geometry": point(0, 0)}])
    with open_catalog(cat_dir) as cat:
        with pytest.raises(QueryError):
            cat.nearest(math.nan, 0, 1)
        with pytest.raises(QueryError):
            cat.nearest(0, math.inf, 1)
        with pytest.raises(QueryError):
            cat.nearest(0, 0, -1)
        with pytest.raises(QueryError):
            cat.nearest(0, 0, 1.5)
        with pytest.raises(QueryError):
            cat.nearest(0, 0, True)


def test_nearest_matches_brute_on_mixed(tmp_path):
    from spatial_catalog import datagen
    records = datagen.gen_sparse_records(300, seed=13)
    cat_dir, _ = build_catalog(tmp_path, records)
    bf = load_records(tmp_path / "in.jsonl")
    with open_catalog(cat_dir) as cat:
        for (x, y, k) in [(0, 0, 1), (500, 500, 10), (1000, 0, 300),
                          (250.5, 749.25, 0), (333, 666, 301)]:
            got = list(cat.nearest(x, y, k).results)
            assert got == nearest_scan(bf, x, y, k)
