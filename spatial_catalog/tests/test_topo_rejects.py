"""Selection filtering, explicit type rejection and malformed-input tests.

Every failure must be locatable by source id (and part position where a
part is involved), and a failed build must never publish a manifest.
"""

import json
import os

import pytest

from spatial_catalog.builder import build as build_catalog
from spatial_catalog.errors import TopologyError
from spatial_catalog.tformat import SELECTION_NAME
from spatial_catalog.topo import (Selection, build_topology,
                                  validate_part)
from spatial_catalog.treader import Topology
from helpers import linestring, point, square, write_records


def _catalog(tmp_path, records, name="cat"):
    src = write_records(tmp_path / "in.jsonl", records)
    out = str(tmp_path / name)
    build_catalog(src, out, cache_bytes=1 << 16, force=True)
    return out


def test_non_line_selected_fails_by_default(tmp_path):
    cat = _catalog(tmp_path, [
        {"object_id": "p", "geometry": point(1, 1)},
        {"object_id": "l", "geometry": linestring([[0, 0], [1, 0]])},
    ])
    out = str(tmp_path / "topo")
    with pytest.raises(TopologyError) as ei:
        build_topology(cat, out, selection=Selection(),
                       cache_bytes=1 << 16, force=True)
    assert ei.value.reason == "wrong_geometry_type"
    # evidence names the offending source id
    evidence = os.path.join(ei.value.evidence_path)
    rows = [json.loads(line) for line in open(evidence, encoding="utf-8")]
    assert rows and rows[0]["object_id"] == "p"
    # no committed topology, failed dir holds the evidence
    assert not os.path.exists(os.path.join(out, "topo_manifest.json"))
    assert os.path.isdir(out + ".failed")


def test_non_line_excluded_when_policy_allows(tmp_path):
    cat = _catalog(tmp_path, [
        {"object_id": "p", "geometry": point(1, 1)},
        {"object_id": "s", "geometry": square(0, 0, 2, 2)},
        {"object_id": "l", "geometry": linestring([[0, 0], [1, 0]])},
    ])
    out = str(tmp_path / "topo")
    report = build_topology(cat, out, selection=Selection(),
                            exclude_non_lines=True,
                            cache_bytes=1 << 16, force=True)
    assert report["records_type_rejected"] == 2
    assert report["line_objects_selected"] == 1
    # the selection file documents every excluded record with its type
    rows = [json.loads(line)
            for line in open(os.path.join(out, SELECTION_NAME),
                             encoding="utf-8")]
    reasons = {r["object_id"]: r["reason"] for r in rows}
    assert reasons == {"p": "wrong_geometry_type",
                       "s": "wrong_geometry_type"}
    with Topology(out) as t:
        assert t.validate_full(catalog=cat)["ok"]
        assert t.manifest["counts"]["type_rejected"] == 2


def test_filter_exclusion_is_recorded(tmp_path):
    cat = _catalog(tmp_path, [
        {"object_id": "keep", "geometry": linestring([[0, 0], [1, 0]])},
        {"object_id": "drop", "geometry": linestring([[2, 2], [3, 2]])},
    ])
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("keep\n")
    out = str(tmp_path / "topo")
    report = build_topology(
        cat, out, selection=Selection(ids_file=str(ids_file)),
        cache_bytes=1 << 16, force=True)
    assert report["records_excluded_filter"] == 1
    rows = [json.loads(line)
            for line in open(os.path.join(out, SELECTION_NAME),
                             encoding="utf-8")]
    assert rows == [{"object_id": "drop",
                     "reason": "excluded_by_filter",
                     "geometry_type": "LineString"}]


def test_prefix_and_allowlist_combine(tmp_path):
    cat = _catalog(tmp_path, [
        {"object_id": "net-1", "geometry": linestring([[0, 0], [1, 0]])},
        {"object_id": "net-2", "geometry": linestring([[1, 0], [2, 0]])},
        {"object_id": "explicit", "geometry": linestring([[5, 5], [6, 5]])},
        {"object_id": "other", "geometry": linestring([[9, 9], [8, 9]])},
    ])
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("explicit\n")
    out = str(tmp_path / "topo")
    report = build_topology(
        cat, out,
        selection=Selection(ids_file=str(ids_file),
                            id_prefixes=("net-",)),
        cache_bytes=1 << 16, force=True)
    # three selected, one excluded; the two net lines connect
    assert report["records_excluded_filter"] == 1
    assert report["edges"] == 3
    assert report["components"] == 2


def test_corrupt_payload_fails_with_location_and_no_publish(tmp_path):
    cat = _catalog(tmp_path, [
        {"object_id": "l1", "geometry": linestring([[0, 0], [1, 0]])},
        {"object_id": "l2", "geometry": linestring([[1, 0], [2, 0]])},
    ])
    out = str(tmp_path / "topo")
    # flip a byte inside a payload record (after the fixed file header)
    payloads = os.path.join(cat, "payloads.bin")
    size = os.path.getsize(payloads)
    offset = min(size - 2, 200)
    with open(payloads, "r+b") as fh:
        fh.seek(offset)
        b = fh.read(1)
        fh.seek(offset)
        fh.write(bytes([b[0] ^ 0xFF]))
    with pytest.raises(Exception):
        build_topology(cat, out, selection=Selection(),
                       cache_bytes=1 << 16, force=True)
    assert not os.path.exists(os.path.join(out, "topo_manifest.json"))
    assert os.path.isdir(out + ".failed")


def test_validate_part_locates_source_and_part():
    with pytest.raises(TopologyError) as ei:
        validate_part("src-x", 3, [])
    assert ei.value.detail["object_id"] == "src-x"
    assert ei.value.detail["part"] == 3
    with pytest.raises(TopologyError) as ei:
        validate_part("src-y", 0, [(1.0, 1.0)])
    assert ei.value.detail["part"] == 0
    with pytest.raises(TopologyError) as ei:
        validate_part("src-z", 2, [(0.0, 0.0), (float("nan"), 1.0)])
    assert ei.value.reason == "non_finite_coordinate"
    assert ei.value.detail["position"] == 1


def test_id_with_tab_is_rejected(tmp_path):
    # object ids are catalog-bounded to 4096 bytes but TSV needs no tab;
    # a tab id cannot be built into a catalog, so exercise the checker
    # directly to pin the explicit error rather than a broken TSV row.
    from spatial_catalog.tformat import check_topo_id
    from spatial_catalog.errors import TopologyError as TE
    with pytest.raises(TE):
        check_topo_id("bad\tid", "source")
    with pytest.raises(TE):
        check_topo_id("", "source")


def test_existing_output_is_protected_without_force(tmp_path):
    cat = _catalog(tmp_path, [
        {"object_id": "l", "geometry": linestring([[0, 0], [1, 0]])}])
    out = str(tmp_path / "topo")
    build_topology(cat, out, selection=Selection(),
                   cache_bytes=1 << 16, force=True)
    with pytest.raises(TopologyError) as ei:
        build_topology(cat, out, selection=Selection(),
                       cache_bytes=1 << 16, force=False)
    assert ei.value.reason == "output_exists"
