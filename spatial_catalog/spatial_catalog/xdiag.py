"""Cross-table diagnostics: runtime-generated sparse and full-overlap
scenes, built under an explicit cache/spill budget, with every counter
measured from the actual run and a differential check against the
independent brute-force oracle for every row and value."""

import os
import resource
import shutil
import sys

from . import datagen
from .brute import crosstab_scan, load_records
from .builder import build
from .xtab import build_crosstab
from .xreader import XTable


def _rss_bytes():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _read_xtab_rows(xtab_dir):
    with XTable(xtab_dir) as xt:
        return list(xt.iter_rows())


def run_xtab_diag(out_dir, n_lines=4000, n_zones=1000,
                  cache_bytes=4 << 20, max_spill_bytes=1 << 30, seed=21):
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    report = {"n_lines": n_lines, "n_zones": n_zones,
              "cache_bytes": cache_bytes,
              "max_spill_bytes": max_spill_bytes, "seed": seed,
              "python": sys.version.split()[0]}

    # ---- sparse scene ---------------------------------------------------
    sparse_l, sparse_z = datagen.gen_xtab_sparse(n_lines, n_zones, seed=seed)
    line_jsonl = os.path.join(out_dir, "sparse-lines.jsonl")
    zone_jsonl = os.path.join(out_dir, "sparse-zones.jsonl")
    datagen.write_jsonl(line_jsonl, sparse_l)
    datagen.write_jsonl(zone_jsonl, sparse_z)
    line_cat = os.path.join(out_dir, "sparse-lines.cat")
    zone_cat = os.path.join(out_dir, "sparse-zones.cat")
    build(line_jsonl, line_cat, cache_bytes=cache_bytes, force=True)
    build(zone_jsonl, zone_cat, cache_bytes=cache_bytes, force=True)
    sparse_xtab = os.path.join(out_dir, "sparse.xtab")
    rss0 = _rss_bytes()
    sparse_report = build_crosstab(
        line_cat, zone_cat, sparse_xtab,
        cache_bytes=cache_bytes, max_spill_bytes=max_spill_bytes, force=True)
    report["sparse"] = sparse_report
    report["sparse_rss_delta_bytes"] = _rss_bytes() - rss0
    # full differential against the naive double scan
    expected = crosstab_scan(load_records(line_jsonl), load_records(zone_jsonl))
    got = _read_xtab_rows(sparse_xtab)
    assert got == expected, "sparse cross-table differs from brute-force oracle"
    total_pairs = n_lines * n_zones
    report["sparse"]["total_possible_pairs"] = total_pairs
    report["sparse"]["pruning_ratio"] = round(
        sparse_report["exact_intersections"] / max(1, total_pairs), 6)
    report["sparse"]["note"] = (
        "exact intersections %.3fx fewer than the O(L*Z) full scan; "
        "bbox pruning is doing real work"
        % (total_pairs / max(1, sparse_report["exact_intersections"]),))

    # ---- full-overlap worst case ---------------------------------------
    ov_l, ov_z = datagen.gen_xtab_full_overlap(
        max(200, n_lines // 4), max(50, n_zones // 4), seed=seed + 1)
    ov_line_jsonl = os.path.join(out_dir, "overlap-lines.jsonl")
    ov_zone_jsonl = os.path.join(out_dir, "overlap-zones.jsonl")
    datagen.write_jsonl(ov_line_jsonl, ov_l)
    datagen.write_jsonl(ov_zone_jsonl, ov_z)
    ov_line_cat = os.path.join(out_dir, "overlap-lines.cat")
    ov_zone_cat = os.path.join(out_dir, "overlap-zones.cat")
    build(ov_line_jsonl, ov_line_cat, cache_bytes=cache_bytes, force=True)
    build(ov_zone_jsonl, ov_zone_cat, cache_bytes=cache_bytes, force=True)
    ov_xtab = os.path.join(out_dir, "overlap.xtab")
    ov_report = build_crosstab(
        ov_line_cat, ov_zone_cat, ov_xtab,
        cache_bytes=cache_bytes, max_spill_bytes=max_spill_bytes, force=True)
    expected_ov = crosstab_scan(load_records(ov_line_jsonl),
                                load_records(ov_zone_jsonl))
    got_ov = _read_xtab_rows(ov_xtab)
    assert got_ov == expected_ov, "overlap cross-table differs from oracle"
    nl, nz = len(ov_l), len(ov_z)
    ov_report["total_possible_pairs"] = nl * nz
    ov_report["note"] = (
        "every bbox overlaps: %d/%d pairs are exact intersections (no "
        "pruning possible); the build spills and still emits every pair"
        % (ov_report["exact_intersections"], nl * nz))
    assert ov_report["exact_intersections"] == nl * nz
    assert ov_report["positive_length_pairs"] == nl * nz
    assert ov_report["output_rows"] == nl * nz
    report["overlap"] = ov_report
    report["peak_rss_bytes"] = _rss_bytes()
    return report
