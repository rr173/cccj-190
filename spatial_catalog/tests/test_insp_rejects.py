"""Rejection tests: bad rows, unknown references, corrupt topology edge
tables, revision conflicts and out-of-range intervals must fail the build
without publishing a seemingly successful partial report."""

import json
import os

import pytest

from spatial_catalog import iformat as F
from spatial_catalog.errors import InspectionError
from insp_helpers import (line, make_registry, make_topology,
                          run_coverage, upsert, write_jsonl)

W = ("2026-11-01", "2026-11-04")


def _base(tmp_path, L=100):
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, L)}])
    reg = make_registry(tmp_path, {("e1", 0): L})
    return topo, reg


def _expect_fail(tmp_path, topo, reg, revisions, reason, window=W):
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, window, (1, 2))
    # nothing that looks like a committed report is published
    assert not os.path.exists(os.path.join(out, F.MANIFEST_NAME))
    report = json.load(open(os.path.join(out + ".failed",
                                         "build_report.json")))
    assert report["status"] == "failed"
    assert report["reason"] == reason
    return report


def test_reject_bad_json(tmp_path):
    topo, reg = _base(tmp_path)
    insp = tmp_path / "insp.jsonl"
    insp.write_text('{"inspection_id": "A", not json}\n')
    out = str(tmp_path / "out")
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [str(insp)], out, W, (1, 2))
    assert not os.path.exists(os.path.join(out, F.MANIFEST_NAME))


def test_reject_nan_literal(tmp_path):
    topo, reg = _base(tmp_path)
    insp = tmp_path / "insp.jsonl"
    insp.write_text('{"inspection_id": "A", "batch": NaN, "action": '
                    '"retract"}\n')
    out = str(tmp_path / "out")
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [str(insp)], out, W, (1, 2))


def test_reject_unknown_edge_reference(tmp_path):
    topo, reg = _base(tmp_path)
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "no-such-edge", 0, "forward", 0, 5,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass")],
        reason="unmanaged_edge")


def test_reject_unmanaged_edge(tmp_path):
    """An edge that exists in the topology but is not registered is not
    managed; observations on it are rejected."""
    topo = make_topology(tmp_path, [
        {"object_id": "e1", "geometry": line(0, 100)},
        {"object_id": "e2", "geometry": line(1000, 1100)},
    ])
    reg = make_registry(tmp_path, {("e1", 0): 100})   # e2 not registered
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "e2", 0, "forward", 0, 5,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass")],
        reason="unmanaged_edge")


def test_reject_interval_exceeds_chainage(tmp_path):
    topo, reg = _base(tmp_path, L=50)
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "e1", 0, "forward", 0, 60,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass")],
        reason="interval_out_of_range")


def test_reject_bad_interval(tmp_path):
    topo, reg = _base(tmp_path)
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "e1", 0, "forward", 30, 30,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass")],
        reason="bad_interval")


def test_reject_empty_date_interval(tmp_path):
    topo, reg = _base(tmp_path)
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "e1", 0, "forward", 0, 10,
               "2026-11-02", "2026-11-02", "2026-11-01", 0, "pass")],
        reason="bad_date_interval")


def test_reject_non_strict_date(tmp_path):
    topo, reg = _base(tmp_path)
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "e1", 0, "forward", 0, 10,
               "2026-11-1", "2026-11-04", "2026-11-01", 0, "pass")],
        reason="bad_date")


def test_reject_invalid_gregorian_date(tmp_path):
    topo, reg = _base(tmp_path)
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "e1", 0, "forward", 0, 10,
               "2026-02-30", "2026-11-04", "2026-11-01", 0, "pass")],
        reason="bad_date")


def test_reject_integer_overflow(tmp_path):
    topo, reg = _base(tmp_path)
    big = (1 << 63)   # exceeds signed 64-bit max
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", big, "pass")],
        reason="integer_overflow")


def test_reject_bad_priority_and_result(tmp_path):
    topo, reg = _base(tmp_path)
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", -1, "pass")],
        reason="bad_priority")
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "ok")],
        reason="bad_result")


def test_reject_bad_direction(tmp_path):
    topo, reg = _base(tmp_path)
    _expect_fail(tmp_path, topo, reg, [
        upsert("A", 1, "e1", 0, "sideways", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass")],
        reason="bad_direction")


def test_reject_unknown_action(tmp_path):
    topo, reg = _base(tmp_path)
    rev = {"inspection_id": "A", "batch": 1, "action": "modify"}
    _expect_fail(tmp_path, topo, reg, [rev], reason="bad_action")


def test_reject_duplicate_registry(tmp_path):
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 100)}])
    reg = write_jsonl(tmp_path / "reg.jsonl", [
        {"source_id": "e1", "part_index": 0, "chainage_end": 100},
        {"source_id": "e1", "part_index": 0, "chainage_end": 100},
    ])
    insp = write_jsonl(tmp_path / "insp.jsonl", [])
    out = str(tmp_path / "out")
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, W, (1, 2))
    report = json.load(open(os.path.join(out + ".failed",
                                         "build_report.json")))
    assert report["reason"] == "duplicate_registry"


def test_reject_registry_unknown_edge(tmp_path):
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 100)}])
    reg = write_jsonl(tmp_path / "reg.jsonl", [
        {"source_id": "ghost", "part_index": 0, "chainage_end": 100},
    ])
    insp = write_jsonl(tmp_path / "insp.jsonl", [])
    out = str(tmp_path / "out")
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, W, (1, 2))
    report = json.load(open(os.path.join(out + ".failed",
                                         "build_report.json")))
    assert report["reason"] == "registry_reference_rejected"


def test_reject_registry_zero_length_edge(tmp_path):
    """A zero-length geometry edge (a self-loop point) cannot be managed."""
    # a degenerate zero-length line: same start and end coordinate
    rec = {"object_id": "z", "geometry":
           {"type": "LineString", "coordinates": [[5, 5], [5, 5]]}}
    topo = make_topology(tmp_path, [rec])
    reg = write_jsonl(tmp_path / "reg.jsonl", [
        {"source_id": "z", "part_index": 0, "chainage_end": 10},
    ])
    insp = write_jsonl(tmp_path / "insp.jsonl", [])
    out = str(tmp_path / "out")
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, W, (1, 2))
    report = json.load(open(os.path.join(out + ".failed",
                                         "build_report.json")))
    assert report["reason"] == "registry_reference_rejected"


def test_reject_registry_bad_chainage(tmp_path):
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 100)}])
    reg = write_jsonl(tmp_path / "reg.jsonl", [
        {"source_id": "e1", "part_index": 0, "chainage_end": 0},
    ])
    insp = write_jsonl(tmp_path / "insp.jsonl", [])
    out = str(tmp_path / "out")
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, W, (1, 2))


def test_reject_corrupt_topology_edge_table(tmp_path):
    """A corrupted consumed topology edge table fails the build, never a
    partial report."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 100)}])
    # corrupt the edge table (flip bytes in the middle)
    edges_path = os.path.join(topo, "edges.tsv")
    data = bytearray(open(edges_path, "rb").read())
    mid = len(data) // 2
    data[mid] ^= 0xFF
    open(edges_path, "wb").write(bytes(data))
    reg = make_registry(tmp_path, {("e1", 0): 100})
    insp = write_jsonl(tmp_path / "insp.jsonl", [])
    out = str(tmp_path / "out")
    with pytest.raises(Exception):
        run_coverage(topo, reg, [insp], out, W, (1, 2))
    assert not os.path.exists(os.path.join(out, F.MANIFEST_NAME))


def test_reject_bad_window_and_cutoffs(tmp_path):
    topo, reg = _base(tmp_path)
    insp = write_jsonl(tmp_path / "insp.jsonl", [])
    out = str(tmp_path / "out")
    # empty window
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, ("2026-11-04", "2026-11-04"),
                     (1, 2))
    # before > after
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, W, (3, 2))
    # negative cutoff
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, W, (-1, 2))
