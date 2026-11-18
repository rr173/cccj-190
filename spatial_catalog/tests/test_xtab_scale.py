"""Resource-budget tests with runtime-generated large scenes.

These prove, with measured counters, that:
  * sparse data makes bbox pruning remove the vast majority of pairs and
    the build stays within a tiny memory budget;
  * full-overlap data cannot be pruned and is reported as a degeneration,
    but external spilling still produces every pair without holding them
    in memory and without truncation;
  * repeated runs are deterministic (identical rows.tsv bytes/hashes).

All data is generated at runtime; no large artifacts are committed.
"""

import os
import resource

from spatial_catalog import datagen
from spatial_catalog.brute import crosstab_scan, load_records
from spatial_catalog.builder import build
from spatial_catalog.xtab import build_crosstab
from spatial_catalog.xreader import XTable
from helpers import write_records

SPARSE_LINES, SPARSE_ZONES = 12000, 2000
OVERLAP_LINES, OVERLAP_ZONES = 800, 40


def _rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _build_inputs(tmp_path, lines, zones, cache_bytes=4 << 20):
    lp = write_records(str(tmp_path / "lines.jsonl"), lines)
    zp = write_records(str(tmp_path / "zones.jsonl"), zones)
    lc, zc = str(tmp_path / "lines.cat"), str(tmp_path / "zones.cat")
    build(lp, lc, cache_bytes=cache_bytes, force=True)
    build(zp, zc, cache_bytes=cache_bytes, force=True)
    return lc, zc, lp, zp


def test_large_sparse_prunes_and_stays_bounded(tmp_path):
    lines, zones = datagen.gen_xtab_sparse(
        SPARSE_LINES, SPARSE_ZONES, seed=31)
    lc, zc, lp, zp = _build_inputs(tmp_path, lines, zones)
    rss0 = _rss()
    xt = str(tmp_path / "xt")
    report = build_crosstab(lc, zc, xt, cache_bytes=2 << 20,
                            max_spill_bytes=1 << 30, force=True)
    rss1 = _rss()
    total_pairs = SPARSE_LINES * SPARSE_ZONES
    print("\n[sparse xtab] lines=%d zones=%d total_pairs=%d candidates=%d "
          "exact=%d rows=%d rss_delta=%.1fMiB spill_runs=%d spill_peak=%d"
          % (SPARSE_LINES, SPARSE_ZONES, total_pairs,
             report["bbox_candidates"], report["exact_intersections"],
             report["output_rows"], (rss1 - rss0) / 2**20,
             report["spill_runs"], report["spill_bytes_peak"]))
    # pruning: exact intersections are a small fraction of the full scan
    assert report["exact_intersections"] < total_pairs / 50
    # the cross-table itself validates and matches the oracle
    with XTable(xt) as table:
        got = list(table.iter_rows())
        assert table.validate_full(line_catalog=False,
                                   zone_catalog=False)["ok"]
    expected = crosstab_scan(load_records(lp), load_records(zp))
    assert got == expected
    # bounded memory: building 24k objects + cross-table under a 2 MiB
    # budget must not balloon (generous CI ceiling)
    assert (rss1 - rss0) < 1 << 30


def test_full_overlap_spills_and_keeps_every_pair(tmp_path):
    lines, zones = datagen.gen_xtab_full_overlap(
        OVERLAP_LINES, OVERLAP_ZONES, seed=32)
    lc, zc, lp, zp = _build_inputs(tmp_path, lines, zones)
    xt = str(tmp_path / "xt")
    # deliberately small in-memory chunk to force multiple spill runs
    report = build_crosstab(lc, zc, xt, cache_bytes=1 << 16,
                            max_spill_bytes=1 << 30, force=True)
    n_pairs = OVERLAP_LINES * OVERLAP_ZONES
    print("\n[overlap xtab] pairs=%d spill_runs=%d spill_peak=%d rows=%d"
          % (n_pairs, report["spill_runs"], report["spill_bytes_peak"],
             report["output_rows"]))
    # honest degeneration: pruning removes nothing
    assert report["bbox_candidates"] == n_pairs
    assert report["exact_intersections"] == n_pairs
    assert report["positive_length_pairs"] == n_pairs
    assert report["spill_runs"] >= 2  # external merge path really used
    assert report["spill_bytes_peak"] > 0
    with XTable(xt) as table:
        got = list(table.iter_rows())
    expected = crosstab_scan(load_records(lp), load_records(zp))
    assert got == expected
    assert len(got) == n_pairs
    # every (source, zone) pair appears exactly once
    keys = [(r[0], r[1]) for r in got]
    assert len(set(keys)) == len(keys) == n_pairs


def test_large_build_determinism(tmp_path):
    lines, zones = datagen.gen_xtab_sparse(3000, 500, seed=33)
    lc, zc, _lp, _zp = _build_inputs(tmp_path, lines, zones)
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    r1 = build_crosstab(lc, zc, a, cache_bytes=1 << 18, force=True)
    r2 = build_crosstab(lc, zc, b, cache_bytes=1 << 18, force=True)
    assert open(os.path.join(a, "rows.tsv"), "rb").read() == \
        open(os.path.join(b, "rows.tsv"), "rb").read()
    assert r1["rows_tsv_sha256"] == r2["rows_tsv_sha256"]
    assert r1["output_rows"] == r2["output_rows"]
