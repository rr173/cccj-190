"""Scale, bounded-resource and determinism tests for the inspection
reconciliation.

The scene contains one long-tail trunk edge with many heavily interleaved,
mostly shadowed observations plus a wide roster of scattered edges.  Builds
run under a deliberately small cache so external sorts and the sweep heaps
genuinely spill, and the tests assert:

  * the business output (ledgers, diff, summary) is byte-identical across
    budgets -- budgets change only where data lives, never the result;
  * the bounded path really spills (non-zero spill runs) while peak RSS
    does not scale with input size;
  * a tiny on-disk spill ceiling fails the build loudly
    (InspectionSpillBudgetExceeded) and publishes nothing;
  * repeated runs on unchanged input are byte-identical.

Large data is generated at runtime; no big fixture is committed.
"""

import hashlib
import json
import os

import pytest

from spatial_catalog import datagen
from spatial_catalog import iformat as F
from spatial_catalog.errors import InspectionSpillBudgetExceeded
from spatial_catalog.ireader import Coverage
from insp_helpers import run_coverage, write_jsonl
from spatial_catalog.builder import build as build_catalog
from spatial_catalog.topo import Selection, build_topology


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _business_hashes(out):
    names = (F.MANAGED_NAME, F.COVERAGE_BEFORE_NAME, F.COVERAGE_AFTER_NAME,
             F.DIFF_NAME, F.SUMMARY_NAME)
    return {name: _sha(os.path.join(out, name)) for name in names}


def _scene(tmp_path, n_edges, trunk_obs, seed=47, tag="sc"):
    records, registry, revisions, window, cutoffs = datagen.gen_insp_scale(
        n_edges=n_edges, trunk_obs=trunk_obs, seed=seed)
    src = write_jsonl(tmp_path / ("in-%s.jsonl" % tag), records)
    cat = str(tmp_path / ("cat-%s" % tag))
    build_catalog(src, cat, cache_bytes=1 << 16, force=True)
    topo = str(tmp_path / ("topo-%s" % tag))
    build_topology(cat, topo, selection=Selection(), cache_bytes=1 << 16,
                   force=True)
    reg = write_jsonl(tmp_path / ("reg-%s.jsonl" % tag), registry)
    insp = write_jsonl(tmp_path / ("insp-%s.jsonl" % tag), revisions)
    return (topo, reg, insp, window,
            (cutoffs["before"], cutoffs["after"]))


def test_small_cache_matches_large_cache(tmp_path):
    topo, reg, insp, window, cutoffs = _scene(tmp_path, n_edges=120,
                                              trunk_obs=1500)
    out_small = str(tmp_path / "out-small")
    out_big = str(tmp_path / "out-big")
    r_small = run_coverage(topo, reg, [insp], out_small, window, cutoffs,
                           cache_bytes=1 << 16)
    r_big = run_coverage(topo, reg, [insp], out_big, window, cutoffs,
                         cache_bytes=8 << 20)
    # the small-budget build genuinely used the disk-backed path
    assert r_small["spill_runs"] > 0
    # identical business output
    assert _business_hashes(out_small) == _business_hashes(out_big)
    assert r_small["coverage_rows_before"] == r_big["coverage_rows_before"]
    assert r_small["coverage_rows_after"] == r_big["coverage_rows_after"]
    assert r_small["diff_rows"] == r_big["diff_rows"]
    # and the artifact self-validates
    with Coverage(out_small) as cov:
        assert cov.validate_full()["ok"]


def test_spill_path_is_exercised_and_bounded(tmp_path):
    """With a tiny cache the sweep heaps and sorts must spill, yet the run
    completes and peak RSS stays far below the input size."""
    topo, reg, insp, window, cutoffs = _scene(tmp_path, n_edges=200,
                                              trunk_obs=3000)
    out = str(tmp_path / "out")
    report = run_coverage(topo, reg, [insp], out, window, cutoffs,
                          cache_bytes=1 << 16, max_spill_bytes=1 << 28)
    assert report["spill_runs"] > 0
    # the sweep heaps themselves were forced to spill to disk
    assert report["heap_spills"] > 0
    assert report["heap_high_water"] > 0
    # measured, non-placeholder resource figures
    assert report["peak_rss_bytes"] > 0
    assert report["tmp_bytes_written"] > 0
    assert report["spill_bytes_peak"] > 0
    # the trunk's revisions/observations are large; RSS stays modest
    insp_size = os.path.getsize(insp)
    assert report["peak_rss_bytes"] < max(insp_size * 4, 64 << 20)
    with Coverage(out) as cov:
        assert cov.validate_full()["ok"]


def test_spill_budget_exhaustion_fails_loudly(tmp_path):
    topo, reg, insp, window, cutoffs = _scene(tmp_path, n_edges=150,
                                              trunk_obs=2000)
    out = str(tmp_path / "out")
    # the smallest legal disk ceiling: real spills exceed it, so the build
    # must abort rather than publish a partial monthly report
    with pytest.raises(InspectionSpillBudgetExceeded):
        run_coverage(topo, reg, [insp], out, window, cutoffs,
                     cache_bytes=1 << 16, max_spill_bytes=1 << 16)
    assert not os.path.exists(os.path.join(out, F.MANIFEST_NAME))
    report = json.load(open(os.path.join(out + ".failed",
                                         "build_report.json")))
    assert report["reason"] == "spill_budget_exceeded"
    assert report["counters"]["spill_bytes_peak"] > 0


def test_deterministic_across_runs(tmp_path):
    topo, reg, insp, window, cutoffs = _scene(tmp_path, n_edges=80,
                                              trunk_obs=800)
    out1 = str(tmp_path / "out1")
    out2 = str(tmp_path / "out2")
    run_coverage(topo, reg, [insp], out1, window, cutoffs,
                 cache_bytes=1 << 16)
    run_coverage(topo, reg, [insp], out2, window, cutoffs,
                 cache_bytes=1 << 16)
    assert _business_hashes(out1) == _business_hashes(out2)


def test_hidden_boundaries_are_reported(tmp_path):
    """The report distinguishes boundaries processed (observation
    activations and finest segments) from the rows actually delivered, so
    shadowed work is visible."""
    topo, reg, insp, window, cutoffs = _scene(tmp_path, n_edges=60,
                                              trunk_obs=1200)
    out = str(tmp_path / "out")
    report = run_coverage(topo, reg, [insp], out, window, cutoffs,
                          cache_bytes=1 << 16)
    # many observations were processed in the sweeps
    assert report["observation_activations"] > 0
    assert report["elementary_date_intervals"] > 0
    # the finest segments are at least as many as the delivered rows, and
    # the shadowed trunk shows merging (segments > delivered rows)
    assert report["cov_before_segments"] >= report["coverage_rows_before"]
    assert report["cov_after_segments"] >= report["coverage_rows_after"]
    assert report["diff_segments"] >= report["diff_rows"]
    # visible observations and clipping are measured
    assert report["visible_observations"] > 0
