"""Validator tests: the validate command must catch truncation, bit flips,
tiling violations, diff/ledger drift, summary drift and ordering breakage
-- without rerunning the topology build."""

import json
import os

import pytest

from spatial_catalog import iformat as F
from spatial_catalog.errors import CorruptionError
from spatial_catalog.ireader import Coverage
from insp_helpers import (line, make_registry, make_topology, retract,
                          run_coverage, upsert, write_jsonl)

W = ("2026-11-01", "2026-11-04")


def _valid_artifact(tmp_path):
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 100)}])
    reg = make_registry(tmp_path, {("e1", 0): 100})
    revisions = [
        upsert("A", 1, "e1", 0, "forward", 10, 50,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass"),
        upsert("B", 2, "e1", 0, "reverse", 50, 70,
               "2026-11-02", "2026-11-04", "2026-11-02", 10, "defect"),
        upsert("A", 3, "e1", 0, "forward", 40, 80,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass"),
        retract("B", 4),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (2, 4))
    return out


def _rewrite_and_fix_manifest(out, name, transform):
    """Rewrite one delivered file via ``transform`` and update the manifest
    so the length/hash checks pass; the content checks must still catch
    the inconsistency."""
    path = os.path.join(out, name)
    data = open(path, "rb").read()
    open(path, "wb").write(transform(data))
    # re-pin the manifest to the tampered file
    mpath = os.path.join(out, F.MANIFEST_NAME)
    manifest = json.load(open(mpath))
    length, sha = F.sha256_file(path)
    manifest["files"][name]["length"] = length
    manifest["files"][name]["sha256"] = sha
    json.dump(manifest, open(mpath, "w"))


def test_valid_artifact_validates(tmp_path):
    out = _valid_artifact(tmp_path)
    with Coverage(out) as cov:
        result = cov.validate_full()
    assert result["ok"]
    assert result["status_change_total"] == 200
    assert result["evidence_only_total"] == 10


def test_truncated_file_caught(tmp_path):
    out = _valid_artifact(tmp_path)
    p = os.path.join(out, F.COVERAGE_BEFORE_NAME)
    os.truncate(p, os.path.getsize(p) - 4)
    with pytest.raises(CorruptionError):
        with Coverage(out) as cov:
            cov.validate_full()


def test_bit_flip_caught(tmp_path):
    out = _valid_artifact(tmp_path)
    p = os.path.join(out, F.DIFF_NAME)
    data = bytearray(open(p, "rb").read())
    data[-3] ^= 0xFF
    open(p, "wb").write(bytes(data))
    with pytest.raises(CorruptionError):
        with Coverage(out) as cov:
            cov.validate_full()


def test_tiling_violation_caught(tmp_path):
    """Drop a coverage row (leaving a chainage hole) and re-pin the
    manifest: the tiling check must catch the gap."""
    out = _valid_artifact(tmp_path)

    def drop_last_row(data):
        lines = data.split(b"\n")
        # remove the last data row (before the trailing empty string)
        assert lines[-1] == b""
        return b"\n".join(lines[:-2] + [b""])

    _rewrite_and_fix_manifest(out, F.COVERAGE_BEFORE_NAME, drop_last_row)
    # the manifest row count no longer matches either; fix it too so only
    # the tiling logic can catch it
    mpath = os.path.join(out, F.MANIFEST_NAME)
    manifest = json.load(open(mpath))
    manifest["counts"]["coverage_rows_before"] -= 1
    json.dump(manifest, open(mpath, "w"))
    with Coverage(out) as cov, pytest.raises(CorruptionError) as exc:
        cov.validate_full()
    assert "chainage" in str(exc.value) or "gap" in str(exc.value)


def test_diff_ledger_drift_caught(tmp_path):
    """Change a diff row's after-status and re-pin: the diff no longer
    matches the ledgers."""
    out = _valid_artifact(tmp_path)

    def tamper(data):
        # flip the first 'pass' after-status field to 'defect' in diff.tsv
        lines = data.split(b"\n")
        for i, ln in enumerate(lines):
            if ln and not ln.startswith(b"#"):
                parts = ln.split(b"\t")
                # after_status is field 9
                if parts[9] == b"pass":
                    parts[9] = b"defect"
                    lines[i] = b"\t".join(parts)
                    break
        return b"\n".join(lines)

    _rewrite_and_fix_manifest(out, F.DIFF_NAME, tamper)
    with Coverage(out) as cov, pytest.raises(CorruptionError):
        cov.validate_full()


def test_summary_drift_caught(tmp_path):
    out = _valid_artifact(tmp_path)
    # corrupt summary.json totals and re-pin
    spath = os.path.join(out, F.SUMMARY_NAME)
    summ = json.load(open(spath))
    summ["status_change_total"] = 999
    open(spath, "w").write(json.dumps(summ))
    _rewrite_and_fix_manifest(out, F.SUMMARY_NAME, lambda d: d)
    # also re-pin the summary file itself (it changed)
    mpath = os.path.join(out, F.MANIFEST_NAME)
    manifest = json.load(open(mpath))
    length, sha = F.sha256_file(spath)
    manifest["files"][F.SUMMARY_NAME]["length"] = length
    manifest["files"][F.SUMMARY_NAME]["sha256"] = sha
    json.dump(manifest, open(mpath, "w"))
    with Coverage(out) as cov, pytest.raises(CorruptionError):
        cov.validate_full()


def test_out_of_order_rows_caught(tmp_path):
    out = _valid_artifact(tmp_path)

    def swap_rows(data):
        lines = data.split(b"\n")
        # swap two adjacent data rows in coverage_before
        hdr, rows = lines[:2], lines[2:]
        if len(rows) >= 3 and rows[0] and rows[1]:
            rows[0], rows[1] = rows[1], rows[0]
        return b"\n".join(hdr + rows)

    _rewrite_and_fix_manifest(out, F.COVERAGE_BEFORE_NAME, swap_rows)
    with Coverage(out) as cov, pytest.raises(CorruptionError):
        cov.validate_full()


def test_validate_without_topology_or_registry(tmp_path):
    """Validation must not require the topology build or the registry to be
    reachable (they are optional re-identity checks)."""
    out = _valid_artifact(tmp_path)
    with Coverage(out) as cov:
        result = cov.validate_full(registry=False, topology=False)
    assert result["ok"]
    assert result["registry"] == "skipped"
    assert result["topology"] == "skipped"


def test_validate_unreachable_inputs_noted(tmp_path):
    """When the pinned registry/topology have moved away, validation notes
    them unreachable rather than silently passing."""
    out = _valid_artifact(tmp_path)
    # point the manifest at nonexistent paths
    mpath = os.path.join(out, F.MANIFEST_NAME)
    manifest = json.load(open(mpath))
    manifest["registry"]["path"] = "/nonexistent/reg.jsonl"
    manifest["topology"]["topology_path"] = "/nonexistent/topo"
    json.dump(manifest, open(mpath, "w"))
    with Coverage(out) as cov:
        result = cov.validate_full()
    assert result["ok"]
    assert result["registry"].startswith("unreachable")
    assert result["topology"].startswith("unreachable")
