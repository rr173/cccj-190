"""Differential tests: the production cross-table vs the independent
naive O(L*Z) double-scan oracle in brute.py.

The oracle shares no production enumeration/pruning/merge code -- it only
calls Shapely single-geometry intersection on every pair.  Every row and
every numeric value must match, and diagnostics must prove pruning on
sparse data while honestly reporting the full-overlap degeneration.
"""

import os
import random

import pytest

from spatial_catalog import datagen
from spatial_catalog.brute import crosstab_scan, load_records
from spatial_catalog.builder import build
from spatial_catalog.xtab import build_crosstab
from spatial_catalog.xreader import XTable
from helpers import write_records


def _build_pair_catalogs(tmp_path, line_recs, zone_recs, cache_bytes=1 << 20):
    line_jsonl = write_records(tmp_path / "lines.jsonl", line_recs)
    zone_jsonl = write_records(tmp_path / "zones.jsonl", zone_recs)
    line_cat = str(tmp_path / "lines.cat")
    zone_cat = str(tmp_path / "zones.cat")
    build(line_jsonl, line_cat, cache_bytes=cache_bytes, force=True)
    build(zone_jsonl, zone_cat, cache_bytes=cache_bytes, force=True)
    return line_cat, zone_cat, line_jsonl, zone_jsonl


def _run_and_diff(tmp_path, line_recs, zone_recs, cache_bytes=1 << 20,
                  max_spill_bytes=1 << 30):
    line_cat, zone_cat, line_jsonl, zone_jsonl = _build_pair_catalogs(
        tmp_path, line_recs, zone_recs, cache_bytes)
    xtab = str(tmp_path / "xtab")
    report = build_crosstab(line_cat, zone_cat, xtab,
                            cache_bytes=cache_bytes,
                            max_spill_bytes=max_spill_bytes, force=True)
    with XTable(xtab) as xt:
        got = list(xt.iter_rows())
    expected = crosstab_scan(load_records(line_jsonl), load_records(zone_jsonl))
    assert got == expected  # exact row-by-row, value-by-value equality
    return report, got, expected


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_sparse_random_scene_matches_oracle(tmp_path, seed):
    lines, zones = datagen.gen_xtab_sparse(300, 120, seed=seed)
    report, got, expected = _run_and_diff(tmp_path, lines, zones)
    # sparse data: bbox pruning must cut exact intersections well below
    # the O(L*Z) brute cost
    total = len(lines) * len(zones)
    assert report["exact_intersections"] < total / 5
    assert report["bbox_candidates"] == report["exact_intersections"]
    assert report["output_rows"] == len(expected)
    # the measured counters really were measured, not hard-coded
    assert report["line_objects"] == len(lines)
    assert report["zone_objects"] == len(zones)
    assert report["positive_length_pairs"] + report["zero_length_pairs"] \
        == report["exact_intersections"]


def test_unicode_id_ordering_matches_code_point_order(tmp_path):
    lines, zones = datagen.gen_xtab_unicode_ids()
    _report, got, expected = _run_and_diff(tmp_path, lines, zones)
    # expected is sorted by the oracle with plain Python (code point) order
    keys = [(r[0], r[1]) for r in got]
    assert keys == sorted(keys)
    assert keys == [(r[0], r[1]) for r in expected]
    # explicit code-point relations that UTF-8 byte sorting must preserve
    # (verified independently with Python's own string ordering):
    # ASCII (<0x80) first, then Latin-1 å (U+00E5), Greek Ω (U+03A9),
    # CJK (U+4E00+); within ASCII, 'a' < 'i'.
    chain = ["latin", "line-乙", "åline", "Ωmega", "线-1"]
    assert sorted(chain) == ["latin", "line-乙", "åline", "Ωmega", "线-1"]


def test_full_overlap_emits_every_pair_and_degrades_honestly(tmp_path):
    lines, zones = datagen.gen_xtab_full_overlap(60, 8, seed=5)
    report, got, expected = _run_and_diff(tmp_path, lines, zones,
                                          cache_bytes=1 << 18)
    n_pairs = len(lines) * len(zones)
    # no pruning possible: every pair is an exact intersection and a row
    assert report["bbox_candidates"] == n_pairs
    assert report["exact_intersections"] == n_pairs
    assert report["positive_length_pairs"] == n_pairs
    assert report["output_rows"] == n_pairs
    assert len(got) == len(expected) == n_pairs
    # the tiny cache forces real spill runs
    assert report["spill_runs"] >= 1
    assert report["spill_bytes_peak"] > 0


def test_one_line_hits_many_zones(tmp_path):
    rng = random.Random(42)
    zones = []

    def rect(x0, y0, x1, y1):
        return {"type": "Polygon",
                "coordinates": [[[x0, y0], [x1, y0], [x1, y1],
                                 [x0, y1], [x0, y0]]]}

    for j in range(60):
        cx, cy = rng.uniform(0, 100), rng.uniform(0, 100)
        r = rng.uniform(1, 3)
        zones.append({"object_id": "z%03d" % j,
                      "geometry": rect(cx, cy, cx + r, cy + r)})
    # one long line sweeping the whole domain
    lines = [{"object_id": "sweep",
              "geometry": {"type": "LineString",
                           "coordinates": [[-5, -5], [50, 105], [105, -5]]}}]
    _report, got, expected = _run_and_diff(tmp_path, lines, zones)
    assert len(got) == len(expected)
    assert all(r[0] == "sweep" for r in got)
    # zone ids within the single source are sorted
    zids = [r[1] for r in got]
    assert zids == sorted(zids)


def test_narrow_sliver_intersection(tmp_path):
    # A line grazing a polygon: tiny but strictly positive length inside.
    zones = [{"object_id": "sliver-zone",
              "geometry": datagen._square(0.0, 0.0, 1.0, 1e-9)}]
    lines = [{"object_id": "grazing",
              "geometry": {"type": "LineString",
                           "coordinates": [[-1.0, 5e-10], [2.0, 5e-10]]}}]
    _report, got, expected = _run_and_diff(tmp_path, lines, zones)
    assert got == expected
    assert len(got) == 1
    assert got[0][2] > 0.0


def test_multi_component_and_hole_mix(tmp_path):
    lines, zones = datagen.gen_xtab_sparse(150, 80, seed=99)
    # force multi-part coverage: generator already injects MultiLineString
    # (i % 6 == 4), MultiPolygon (i % 7 == 3) and holes (i % 5 == 2)
    assert any(l["geometry"]["type"] == "MultiLineString" for l in lines)
    assert any(z["geometry"]["type"] == "MultiPolygon" for z in zones)
    _report, got, expected = _run_and_diff(tmp_path, lines, zones)
    assert got == expected


def test_empty_line_catalog(tmp_path):
    _l, zones = datagen.gen_xtab_sparse(1, 5, seed=7)
    report, got, expected = _run_and_diff(tmp_path, [], zones)
    assert got == expected == []
    assert report["output_rows"] == 0
    assert report["bbox_candidates"] == 0


def test_empty_zone_catalog(tmp_path):
    lines, _z = datagen.gen_xtab_sparse(5, 1, seed=8)
    report, got, expected = _run_and_diff(tmp_path, lines, [])
    assert got == expected == []
    assert report["output_rows"] == 0
    assert report["exact_intersections"] == 0


def test_counter_invariants(tmp_path):
    lines, zones = datagen.gen_xtab_sparse(200, 90, seed=51)
    report, got, expected = _run_and_diff(tmp_path, lines, zones)
    # every exact intersection is classified once
    assert report["positive_length_pairs"] + report["zero_length_pairs"] \
        == report["exact_intersections"]
    # rows + deduped duplicates == positive pairs
    assert report["output_rows"] + report["duplicate_pairs_dropped"] \
        == report["positive_length_pairs"]
    # bbox candidates are a superset of exact work and a subset of the
    # full cartesian product
    total = len(lines) * len(zones)
    assert report["exact_intersections"] <= report["bbox_candidates"] <= total
    assert report["output_rows"] == len(expected) == len(got)


def test_deterministic_rebuild_identical(tmp_path):
    lines, zones = datagen.gen_xtab_sparse(120, 60, seed=17)
    line_cat, zone_cat, _, _ = _build_pair_catalogs(tmp_path, lines, zones)
    out1 = str(tmp_path / "xt1")
    out2 = str(tmp_path / "xt2")
    r1 = build_crosstab(line_cat, zone_cat, out1,
                        cache_bytes=1 << 18, force=True)
    r2 = build_crosstab(line_cat, zone_cat, out2,
                        cache_bytes=1 << 18, force=True)
    rows1 = open(os.path.join(out1, "rows.tsv"), "rb").read()
    rows2 = open(os.path.join(out2, "rows.tsv"), "rb").read()
    assert rows1 == rows2                      # byte-identical logical rows
    assert r1["rows_tsv_sha256"] == r2["rows_tsv_sha256"]
    # manifest digests are independent of the volatile created_utc field
    m1, m2 = XTable(out1).manifest, XTable(out2).manifest
    assert m1["line_catalog"] == m2["line_catalog"]
    assert m1["zone_catalog"] == m2["zone_catalog"]
    assert m1["files"] == m2["files"]
