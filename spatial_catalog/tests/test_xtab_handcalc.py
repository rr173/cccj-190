"""Hand-computable horizontal/vertical line vs rectangle cases.

Every intersection length, source length and ratio here is derived by
hand from integer coordinates, so the test does not rely on any
geometry library intuition -- GEOS results are checked against numbers
computed in the test itself.
"""

import pytest

from spatial_catalog.xtab import build_crosstab
from spatial_catalog.xreader import XTable
from helpers import build_catalog, square, linestring


def _two_catalogs(tmp_path, line_recs, zone_recs, cache_bytes=1 << 20):
    line_dir, _ = build_catalog(tmp_path, line_recs, name="lines")
    zone_dir, _ = build_catalog(tmp_path, zone_recs, name="zones")
    out = str(tmp_path / "xtab")
    report = build_crosstab(line_dir, zone_dir, out,
                            cache_bytes=cache_bytes, force=True)
    return out, report


def _rows(xtab_dir):
    with XTable(xtab_dir) as xt:
        return list(xt.iter_rows())


def test_basic_horizontal_and_vertical_lengths(tmp_path):
    # Zone A = rectangle [2,0]x[6,4]; zone B = [8,0]x[12,4].
    zones = [
        {"object_id": "A", "geometry": square(2, 0, 6, 4)},
        {"object_id": "B", "geometry": square(8, 0, 12, 4)},
    ]
    lines = [
        # horizontal y=1, x 0..10, length 10:
        #   inside A: x in [2,6] -> 4; inside B: x in [8,10] -> 2
        {"object_id": "Lh", "geometry": linestring([[0, 1], [10, 1]])},
        # vertical x=5, y -5..5, length 10: inside A y in [0,4] -> 4
        {"object_id": "Lv", "geometry": linestring([[5, -5], [5, 5]])},
    ]
    xtab, report = _two_catalogs(tmp_path, lines, zones)
    rows = _rows(xtab)
    expect = [
        ("Lh", "A", 4.0, 10.0, 0.4),
        ("Lh", "B", 2.0, 10.0, 0.2),
        ("Lv", "A", 4.0, 10.0, 0.4),
    ]
    assert rows == expect
    assert report["positive_length_pairs"] == 3
    # Lv vs B is removed by bbox pruning (bboxes disjoint), so it never
    # reaches an exact intersection: exact == positive here.
    assert report["zero_length_pairs"] == 0
    assert report["exact_intersections"] == 3
    assert report["bbox_candidates"] == 3
    assert report["output_rows"] == 3


def test_point_contact_produces_no_row(tmp_path):
    # Line ends exactly on the corner (6,0): intersection is one Point,
    # length 0 -> no row, counted as a zero-length pair.
    zones = [{"object_id": "A", "geometry": square(2, 0, 6, 4)}]
    lines = [{"object_id": "touch",
              "geometry": linestring([[6, -5], [6, 0]])}]
    xtab, report = _two_catalogs(tmp_path, lines, zones)
    assert _rows(xtab) == []
    assert report["bbox_candidates"] == 1
    assert report["exact_intersections"] == 1
    assert report["zero_length_pairs"] == 1
    assert report["positive_length_pairs"] == 0


def test_edge_contact_positive_length_is_attributed(tmp_path):
    # A line coincident with the polygon's bottom edge over x in [2,6].
    # The intersection has positive 1-D length (4.0) and therefore DOES
    # count; only zero-length contacts are excluded.
    zones = [{"object_id": "A", "geometry": square(2, 0, 6, 4)}]
    lines = [{"object_id": "edge",
              "geometry": linestring([[0, 0], [8, 0]])}]
    xtab, report = _two_catalogs(tmp_path, lines, zones)
    rows = _rows(xtab)
    assert rows == [("edge", "A", 4.0, 8.0, 0.5)]


def test_line_crossing_zone_edge_partial(tmp_path):
    # Diagonal line from (0,0) to (10,10); zone A = [2,2]x[6,6].
    # It enters at (2,2) and leaves at (6,6): segment length
    # sqrt((6-2)^2+(6-2)^2) = sqrt(32).
    import math
    zones = [{"object_id": "A", "geometry": square(2, 2, 6, 6)}]
    lines = [{"object_id": "diag",
              "geometry": linestring([[0, 0], [10, 10]])}]
    xtab, _ = _two_catalogs(tmp_path, lines, zones)
    rows = _rows(xtab)
    assert len(rows) == 1
    sid, zid, ilen, slen, ratio = rows[0]
    assert (sid, zid) == ("diag", "A")
    assert slen == pytest.approx(math.sqrt(200.0))
    assert ilen == pytest.approx(math.sqrt(32.0))
    # ratio equals the float quotient exactly (not an approximation)
    assert ratio == ilen / slen


def test_hole_excludes_inner_length(tmp_path):
    # Zone square [0,0]x[10,10] with hole [3,3]x[7,7]; horizontal line
    # y=5 from x=0..10. Inside outer: 10; inside hole: 4 (excluded).
    hole = [[3, 3], [3, 7], [7, 7], [7, 3], [3, 3]]
    zones = [{"object_id": "ring",
              "geometry": square(0, 0, 10, 10, hole=hole)}]
    lines = [{"object_id": "L", "geometry": linestring([[0, 5], [10, 5]])}]
    xtab, _ = _two_catalogs(tmp_path, lines, zones)
    rows = _rows(xtab)
    assert len(rows) == 1
    assert rows[0][2] == pytest.approx(6.0)   # 10 - 4
    assert rows[0][3] == 10.0
    assert rows[0][4] == pytest.approx(0.6)


def test_overlapping_zones_counted_independently_no_conservation(tmp_path):
    # Two overlapping rectangles; the line segment inside the overlap is
    # attributed fully to BOTH zones (no apportionment).
    zones = [
        {"object_id": "A", "geometry": square(0, 0, 6, 4)},
        {"object_id": "B", "geometry": square(4, 0, 10, 4)},
    ]
    lines = [{"object_id": "L", "geometry": linestring([[0, 2], [10, 2]])}]
    xtab, report = _two_catalogs(tmp_path, lines, zones)
    rows = dict(((r[0], r[1]), r[2:]) for r in _rows(xtab))
    assert rows[("L", "A")] == (6.0, 10.0, 0.6)
    assert rows[("L", "B")] == (6.0, 10.0, 0.6)
    # summed attributed length (12) exceeds the line length (10): allowed
    assert report["positive_length_pairs"] == 2


def test_multiline_string_parts_sum(tmp_path):
    # MultiLineString: two horizontal pieces at y=1, x 0..1 and 9..11.
    # Zone A [2,0]-[6,4] hits nothing; zone B [8,0]-[12,4] hits x 9..11
    # -> length 2; source length = 1 + 2 = 3.
    zones = [
        {"object_id": "A", "geometry": square(2, 0, 6, 4)},
        {"object_id": "B", "geometry": square(8, 0, 12, 4)},
    ]
    mls = {"type": "MultiLineString",
           "coordinates": [[[0, 1], [1, 1]], [[9, 1], [11, 1]]]}
    lines = [{"object_id": "ML", "geometry": mls}]
    xtab, _ = _two_catalogs(tmp_path, lines, zones)
    rows = _rows(xtab)
    assert rows == [("ML", "B", 2.0, 3.0, 2.0 / 3.0)]


def test_multipolygon_zone(tmp_path):
    # Two disjoint squares as one MultiPolygon zone; a long line crosses
    # both -> one row whose length is the sum.
    zones = [{"object_id": "MP",
              "geometry": {"type": "MultiPolygon", "coordinates": [
                  square(0, 0, 2, 2)["coordinates"],
                  square(8, 0, 10, 2)["coordinates"]]}}]
    lines = [{"object_id": "L", "geometry": linestring([[-1, 1], [11, 1]])}]
    xtab, _ = _two_catalogs(tmp_path, lines, zones)
    rows = _rows(xtab)
    assert len(rows) == 1
    assert rows[0][:3] == ("L", "MP", 4.0)  # 2 + 2


def test_empty_cross_table_is_valid_and_header_only(tmp_path):
    xtab, report = _two_catalogs(tmp_path, [], [])
    assert report["output_rows"] == 0
    raw = open(xtab + "/rows.tsv", "rb").read()
    from spatial_catalog import xformat as xf
    assert raw == xf.HEADER_BYTES  # exactly the two header lines
    with XTable(xtab) as xt:
        assert list(xt.iter_rows()) == []
        assert xt.validate_full(line_catalog=False, zone_catalog=False) == {
            "ok": True, "rows": 0,
            "rows_sha256": xt.manifest["files"]["rows.tsv"]["sha256"],
            "input_identities": {"line": "skipped", "zone": "skipped"}}


def test_manifest_pins_counts_hashes_and_identities(tmp_path):
    zones = [{"object_id": "A", "geometry": square(0, 0, 10, 10)}]
    lines = [{"object_id": "L", "geometry": linestring([[0, 5], [10, 5]])}]
    xtab, report = _two_catalogs(tmp_path, lines, zones)
    with XTable(xtab) as xt:
        m = xt.manifest
        assert m["record_count"] == 1
        assert m["line_catalog"]["object_count"] == 1
        assert m["zone_catalog"]["object_count"] == 1
        assert m["files"]["rows.tsv"]["sha256"] == report["rows_tsv_sha256"]
        assert m["files"]["rows.tsv"]["length"] == report["rows_tsv_bytes"]
        # identity digests are stable content hashes
        assert len(m["line_catalog"]["identity_digest"]) == 64
        assert len(m["zone_catalog"]["identity_digest"]) == 64
        summary = xt.validate_full()
    assert summary["ok"] and summary["rows"] == 1
