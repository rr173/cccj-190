"""Invalid input must be rejected with explicit evidence, and a failed
build must leave a directory that cannot be queried."""

import json
import os

import pytest

from spatial_catalog import geojson
from spatial_catalog.builder import build
from spatial_catalog.catalog import Catalog
from spatial_catalog.errors import BuildError, NotACatalogError


def run_build_expect_failure(tmp_path, lines, **kwargs):
    src = tmp_path / "in.jsonl"
    with open(src, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line if line.endswith("\n") else line + "\n")
    out = str(tmp_path / "cat")
    with pytest.raises(BuildError) as excinfo:
        build(str(src), out, **kwargs)
    # failed build is not queryable: no committed catalog directory
    assert not os.path.exists(out)
    with pytest.raises(NotACatalogError):
        Catalog(out)
    # evidence exists and is machine-readable
    failed = out + ".failed"
    rejects_path = os.path.join(failed, "rejects.jsonl")
    assert os.path.isfile(rejects_path)
    rejects = [json.loads(l) for l in open(rejects_path, encoding="utf-8")]
    report = json.load(open(os.path.join(failed, "build_report.json")))
    assert report["status"] == "failed"
    assert not os.path.exists(os.path.join(failed, "manifest.json"))
    # even the failed directory itself refuses to open as a catalog
    with pytest.raises(NotACatalogError):
        Catalog(failed)
    return rejects, excinfo.value


def rec(object_id, geom):
    return json.dumps({"object_id": object_id, "geometry": geom})


PT = {"type": "Point", "coordinates": [1.0, 2.0]}


def test_reject_nan_literal(tmp_path):
    rejects, _ = run_build_expect_failure(
        tmp_path, ['{"object_id": "a", "geometry": {"type": "Point",'
                   ' "coordinates": [NaN, 1.0]}}'])
    assert rejects[0]["reason"] == geojson.R_NON_FINITE
    assert rejects[0]["line"] == 1


def test_reject_infinity_literal(tmp_path):
    rejects, _ = run_build_expect_failure(
        tmp_path, ['{"object_id": "a", "geometry": {"type": "Point",'
                   ' "coordinates": [1.0, -Infinity]}}'])
    assert rejects[0]["reason"] == geojson.R_NON_FINITE


def test_reject_huge_exponent_coordinate(tmp_path):
    # 1e400 parses to inf in float
    rejects, _ = run_build_expect_failure(
        tmp_path, [rec("a", {"type": "Point", "coordinates": [1e400, 0.0]})])
    assert rejects[0]["reason"] == geojson.R_NON_FINITE


def test_reject_empty_point(tmp_path):
    rejects, _ = run_build_expect_failure(
        tmp_path, [rec("a", {"type": "Point", "coordinates": []})])
    assert rejects[0]["reason"] in (geojson.R_NOT_2D, geojson.R_EMPTY)


def test_reject_empty_polygon(tmp_path):
    rejects, _ = run_build_expect_failure(
        tmp_path, [rec("a", {"type": "Polygon", "coordinates": []})])
    assert rejects[0]["reason"] == geojson.R_EMPTY


def test_reject_duplicate_id(tmp_path):
    rejects, _ = run_build_expect_failure(
        tmp_path, [rec("dup", PT), rec("ok", PT), rec("dup", PT)])
    assert rejects[0]["reason"] == geojson.R_DUP_ID
    assert rejects[0]["object_id"] == "dup"
    assert rejects[0]["lines"] == [1, 3]


def test_reject_bowtie_polygon(tmp_path):
    bowtie = {"type": "Polygon", "coordinates": [
        [[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]}
    rejects, _ = run_build_expect_failure(tmp_path, [rec("bow", bowtie)])
    assert rejects[0]["reason"] == geojson.R_INVALID


def test_reject_unclosed_ring(tmp_path):
    open_ring = {"type": "Polygon", "coordinates": [
        [[0, 0], [1, 0], [1, 1], [0, 1]]]}
    rejects, _ = run_build_expect_failure(tmp_path, [rec("o", open_ring)])
    assert rejects[0]["reason"] == geojson.R_BAD_GEOM


def test_reject_geometry_collection(tmp_path):
    gc = {"type": "GeometryCollection",
          "geometries": [{"type": "Point", "coordinates": [0, 0]}]}
    rejects, _ = run_build_expect_failure(tmp_path, [rec("g", gc)])
    assert rejects[0]["reason"] == geojson.R_UNSUPPORTED_TYPE


def test_reject_3d_position(tmp_path):
    rejects, _ = run_build_expect_failure(
        tmp_path, [rec("a", {"type": "Point", "coordinates": [1.0, 2.0, 3.0]})])
    assert rejects[0]["reason"] == geojson.R_NOT_2D


def test_reject_oversize_payload(tmp_path):
    big = {"type": "LineString",
           "coordinates": [[float(i), float(i % 7)] for i in range(200)]}
    rejects, _ = run_build_expect_failure(
        tmp_path, [rec("big", big)], max_payload_bytes=128)
    assert rejects[0]["reason"] == geojson.R_PAYLOAD_TOO_LARGE


def test_reject_missing_id(tmp_path):
    rejects, _ = run_build_expect_failure(
        tmp_path, [json.dumps({"geometry": PT})])
    assert rejects[0]["reason"] == geojson.R_MISSING_ID


def test_reject_empty_id(tmp_path):
    rejects, _ = run_build_expect_failure(tmp_path, [rec("", PT)])
    assert rejects[0]["reason"] == geojson.R_BAD_ID


def test_reject_non_string_id(tmp_path):
    rejects, _ = run_build_expect_failure(tmp_path, [rec(123, PT)])
    assert rejects[0]["reason"] == geojson.R_BAD_ID


def test_reject_coordinate_out_of_bound(tmp_path):
    rejects, _ = run_build_expect_failure(
        tmp_path, [rec("a", {"type": "Point", "coordinates": [1e16, 0.0]})],
        max_coord=1e15)
    assert rejects[0]["reason"] == geojson.R_COORD_RANGE


def test_reject_short_linestring(tmp_path):
    rejects, _ = run_build_expect_failure(
        tmp_path, [rec("a", {"type": "LineString", "coordinates": [[0, 0]]})])
    assert rejects[0]["reason"] == geojson.R_BAD_GEOM


def test_reject_not_json(tmp_path):
    rejects, _ = run_build_expect_failure(tmp_path, ["{not json"])
    assert rejects[0]["reason"] == geojson.R_NOT_JSON


def test_reject_multiple_evidence_rows(tmp_path):
    rejects, _ = run_build_expect_failure(
        tmp_path,
        [rec("ok1", PT),
         rec("bad-empty", {"type": "Point", "coordinates": []}),
         rec("ok2", PT),
         '{"object_id": "bad-nan", "geometry": '
         '{"type": "Point", "coordinates": [NaN, 0]}}'])
    assert len(rejects) == 2
    assert [r["line"] for r in rejects] == [2, 4]
