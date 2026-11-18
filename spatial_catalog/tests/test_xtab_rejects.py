"""Invalid inputs, GEOS failures, budget exhaustion and publication
safety.  Every failure must be loud, carry locatable evidence, never
publish a partial cross-table, and never destroy an existing valid one.
"""

import json
import os

import pytest
from shapely.geometry import LineString

from spatial_catalog import xformat as xf
from spatial_catalog.builder import build
from spatial_catalog.errors import (CrossTableError, NotACatalogError,
                                    SpillBudgetExceeded)
from spatial_catalog.xtab import (build_crosstab, validate_role_geometries,
                                  validate_role_geometry)
from spatial_catalog.xreader import XTable
from helpers import write_records, linestring, square, point


def _cats(tmp_path, line_recs, zone_recs, **kw):
    base = str(tmp_path)
    ldir = os.path.join(base, "linedata")
    zdir = os.path.join(base, "zonedata")
    os.makedirs(ldir, exist_ok=True)
    os.makedirs(zdir, exist_ok=True)
    lsrc = write_records(os.path.join(ldir, "lines.jsonl"), line_recs)
    zsrc = write_records(os.path.join(zdir, "zones.jsonl"), zone_recs)
    line_cat, zone_cat = os.path.join(base, "l.cat"), os.path.join(base, "z.cat")
    build(lsrc, line_cat, cache_bytes=1 << 20, force=True)
    build(zsrc, zone_cat, cache_bytes=1 << 20, force=True)
    return line_cat, zone_cat


LINES = [{"object_id": "L1", "geometry": linestring([[0, 0], [10, 0]])}]
ZONES = [{"object_id": "Z1", "geometry": square(0, -2, 10, 2)}]


def test_wrong_line_type_rejected_with_evidence(tmp_path):
    # a point lives where a line is required
    line_cat, zone_cat = _cats(
        tmp_path,
        [{"object_id": "p", "geometry": point(1, 1)},
         {"object_id": "L2", "geometry": linestring([[0, 0], [1, 0]])}],
        ZONES)
    out = str(tmp_path / "xt")
    with pytest.raises(CrossTableError) as ei:
        build_crosstab(line_cat, zone_cat, out, force=True)
    assert ei.value.reason == "invalid_line_input"
    failed = out + ".failed"
    assert not os.path.exists(out)
    with pytest.raises(NotACatalogError):
        XTable(out)
    evidence = [json.loads(l) for l in
                open(os.path.join(failed, "xtab_rejects.jsonl"), encoding="utf-8")]
    assert evidence[0]["reason"] == "wrong_geometry_type"
    assert evidence[0]["object_id"] is not None
    assert evidence[0]["geometry_type"] == "Point"
    report = json.load(open(os.path.join(failed, "build_report.json")))
    assert report["status"] == "failed"
    assert not os.path.exists(os.path.join(failed, xf.MANIFEST_NAME))


def test_wrong_zone_type_rejected(tmp_path):
    line_cat, zone_cat = _cats(
        tmp_path, LINES,
        [{"object_id": "p", "geometry": point(0, 0)},
         {"object_id": "Z2", "geometry": square(0, 0, 1, 1)}])
    out = str(tmp_path / "xt")
    with pytest.raises(CrossTableError) as ei:
        build_crosstab(line_cat, zone_cat, out, force=True)
    assert ei.value.reason == "invalid_zone_input"
    evidence = [json.loads(l) for l in
                open(os.path.join(out + ".failed", "xtab_rejects.jsonl"),
                     encoding="utf-8")]
    assert any(e["reason"] == "wrong_geometry_type" and
               e["geometry_type"] == "Point" for e in evidence)


def test_line_catalog_rejects_polygon_role(tmp_path):
    # swap roles: polygons in the line catalog slot and lines in the zone
    # slot -- whichever stream is validated first must reject loudly
    line_cat, zone_cat = _cats(tmp_path, ZONES, LINES)
    with pytest.raises(CrossTableError) as ei:
        build_crosstab(line_cat, zone_cat, str(tmp_path / "xt"), force=True)
    assert ei.value.reason in ("invalid_line_input", "invalid_zone_input")


def test_zero_length_line_rejected(tmp_path):
    # zero-length LineString is invalid in GEOS, so the base build already
    # rejects it; validate_role_geometry must still reject a synthetic one.
    bad = LineString([(0, 0), (0, 0)])
    ev = validate_role_geometry("zl", bad, "line",
                                frozenset(("LineString", "MultiLineString")))
    assert ev is not None
    assert ev["reason"] == "invalid_geometry"


def test_role_validation_unit_branches(tmp_path):
    # direct branch coverage of the pure validation helper
    from shapely.geometry import Point, Polygon
    line_types = frozenset(("LineString", "MultiLineString"))
    zone_types = frozenset(("Polygon", "MultiPolygon"))
    # wrong type in each role
    ev = validate_role_geometry("p", Point(0, 0), "line", line_types, 0)
    assert ev["reason"] == "wrong_geometry_type"
    ev = validate_role_geometry("l", LineString([(0, 0), (1, 1)]),
                                "zone", zone_types, 1)
    assert ev["reason"] == "wrong_geometry_type"
    # a valid geometry passes both applicable roles
    good_line = LineString([(0, 0), (1, 1)])
    assert validate_role_geometry("ok", good_line, "line", line_types, 2) is None
    good_zone = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    assert validate_role_geometry("okz", good_zone, "zone", zone_types, 3) is None
    # streaming wrapper collects every violation
    violations = validate_role_geometries(
        [("a", good_line), ("b", Point(0, 0))], "line", line_types)
    assert [v["object_id"] for v in violations] == ["b"]


def test_input_catalogs_are_not_modified(tmp_path):
    line_cat, zone_cat = _cats(tmp_path, LINES, ZONES)
    import hashlib

    def digests(d):
        out = {}
        for name in os.listdir(d):
            out[name] = hashlib.sha256(
                open(os.path.join(d, name), "rb").read()).hexdigest()
        return out

    d0l, d0z = digests(line_cat), digests(zone_cat)
    build_crosstab(line_cat, zone_cat, str(tmp_path / "xt"), force=True)
    assert digests(line_cat) == d0l
    assert digests(zone_cat) == d0z


def test_geos_overlay_failure_aborts_with_pair_evidence(tmp_path, monkeypatch):
    line_cat, zone_cat = _cats(tmp_path, LINES, ZONES)
    from shapely.errors import GEOSException

    def boom(self, other):
        raise GEOSException("simulated overlay topology error")

    monkeypatch.setattr(LineString, "intersection", boom)
    out = str(tmp_path / "xt")
    with pytest.raises(CrossTableError) as ei:
        build_crosstab(line_cat, zone_cat, out, force=True)
    assert ei.value.reason == "overlay_failure"
    evidence = json.loads(
        open(os.path.join(out + ".failed", "xtab_rejects.jsonl"),
             encoding="utf-8").readline())
    assert evidence["source_id"] == "L1"
    assert evidence["zone_id"] == "Z1"
    assert "simulated overlay topology error" in evidence["detail"]
    assert not os.path.exists(out)


def test_spill_budget_exhaustion_fails_without_publishing(tmp_path):
    # full-overlap scene with a deliberately tiny disk budget; the many
    # pair records must spill and exceed it rather than be truncated
    from spatial_catalog import datagen
    lines, zones = datagen.gen_xtab_full_overlap(300, 12, seed=3)
    lsrc = write_records(tmp_path / "l.jsonl", lines)
    zsrc = write_records(tmp_path / "z.jsonl", zones)
    lc, zc = str(tmp_path / "l.cat"), str(tmp_path / "z.cat")
    build(lsrc, lc, cache_bytes=1 << 20, force=True)
    build(zsrc, zc, cache_bytes=1 << 20, force=True)
    out = str(tmp_path / "xt")
    with pytest.raises(SpillBudgetExceeded):
        build_crosstab(lc, zc, out, cache_bytes=1 << 16,
                       max_spill_bytes=1 << 16, force=True)
    assert not os.path.exists(out)
    failed = out + ".failed"
    report = json.load(open(os.path.join(failed, "build_report.json")))
    assert report["reason"] == "spill_budget_exceeded"
    # no manifest ever lands in the failed directory
    assert not os.path.exists(os.path.join(failed, xf.MANIFEST_NAME))


def test_existing_valid_output_is_protected_on_failure(tmp_path):
    # build a good cross-table first
    line_cat, zone_cat = _cats(tmp_path, LINES, ZONES)
    out = str(tmp_path / "xt")
    build_crosstab(line_cat, zone_cat, out, force=True)
    good_rows = open(os.path.join(out, "rows.tsv"), "rb").read()
    # now a failing rebuild (wrong roles) must not touch the good output
    other = tmp_path / "other"
    other.mkdir()
    bad_line, bad_zone = _cats(other, ZONES, LINES)
    with pytest.raises(CrossTableError):
        build_crosstab(bad_line, bad_zone, out, force=True)
    assert os.path.exists(os.path.join(out, xf.MANIFEST_NAME))
    assert open(os.path.join(out, "rows.tsv"), "rb").read() == good_rows
    with XTable(out) as xt:
        assert xt.validate_full(line_catalog=False, zone_catalog=False)["ok"]


def test_disk_error_during_build_aborts_cleanly(tmp_path, monkeypatch):
    line_cat, zone_cat = _cats(tmp_path, LINES, ZONES)
    out = str(tmp_path / "xt")
    import spatial_catalog.xtab as xtab_mod
    real_open = xtab_mod.open

    def flaky_open(path, *a, **kw):
        mode = a[0] if a else kw.get("mode", "r")
        if isinstance(path, str) and path.endswith("rows.tsv") and "w" in mode:
            raise OSError("simulated disk full")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(xtab_mod, "open", flaky_open)
    with pytest.raises(CrossTableError) as ei:
        build_crosstab(line_cat, zone_cat, out, force=True)
    assert ei.value.reason == "io_error"
    assert not os.path.exists(out)
    # failed directory holds a report, never a manifest
    assert os.path.isdir(out + ".failed")
    assert not os.path.exists(os.path.join(out + ".failed", xf.MANIFEST_NAME))


def test_consumer_abort_cleans_temp_files(tmp_path):
    # abort enumeration mid-stream: the generator must close run files and
    # the temp workspace must not leak
    line_cat, zone_cat = _cats(tmp_path, LINES, ZONES)
    from spatial_catalog.catalog import Catalog

    orig = Catalog.bbox_window_entries

    def aborted(self, bbox, stats=None):
        for item in orig(self, bbox, stats):
            raise KeyboardInterrupt()
            yield item  # pragma: no cover

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Catalog, "bbox_window_entries", aborted)
    out = str(tmp_path / "xt")
    # a consumer abort propagates the interrupt after full failure
    # bookkeeping (no partial publish, temp workspace removed)
    with pytest.raises(KeyboardInterrupt):
        build_crosstab(line_cat, zone_cat, out, force=True)
    monkeypatch.undo()
    # no build directory (temp workspace removed on every exit path)
    leftovers = [p for p in os.listdir(str(tmp_path))
                 if (".build-%d" % os.getpid()) in p or ".old-" in p]
    assert leftovers == []
    assert not os.path.exists(out)
    # the aborted build is recorded as a failed directory, never a table
    assert os.path.isdir(out + ".failed")
    assert not os.path.exists(os.path.join(out + ".failed", xf.MANIFEST_NAME))


def test_not_a_catalog_inputs_fail_loudly(tmp_path):
    junk = tmp_path / "junk"
    junk.mkdir()
    line_cat, zone_cat = _cats(tmp_path, LINES, ZONES)
    with pytest.raises(CrossTableError):
        build_crosstab(str(junk), zone_cat, str(tmp_path / "xt1"), force=True)
    with pytest.raises(CrossTableError):
        build_crosstab(line_cat, str(junk), str(tmp_path / "xt2"), force=True)


def test_tsv_unsafe_id_rejected(tmp_path):
    # base catalogs accept arbitrary non-empty UTF-8 ids; the cross-table
    # row grammar additionally forbids tab/CR/LF in ids.
    rec = {"object_id": "bad\tid",
           "geometry": linestring([[0, 0], [1, 0]])}
    lsrc = write_records(tmp_path / "l.jsonl", [rec])
    zsrc = write_records(tmp_path / "z.jsonl", ZONES)
    lc, zc = str(tmp_path / "l.cat"), str(tmp_path / "z.cat")
    build(lsrc, lc, cache_bytes=1 << 20, force=True)
    build(zsrc, zc, cache_bytes=1 << 20, force=True)
    with pytest.raises(CrossTableError) as ei:
        build_crosstab(lc, zc, str(tmp_path / "xt"), force=True)
    assert ei.value.reason == "id_not_tsv_safe"
