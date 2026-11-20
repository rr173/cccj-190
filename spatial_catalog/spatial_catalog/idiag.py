"""Inspection-reconciliation diagnostics: a runtime-generated scale scene
built under an explicit cache/spill budget, re-validated by the independent
reader checks, and cross-checked for budget independence.

The scene deliberately contains one long-tail trunk edge with many heavily
interleaved, mostly shadowed observations (large active sets, many
elementary date intervals) plus a wide roster of scattered small edges, so
the run proves bounded-resource handling of both the deep trunk and the
long tail.  A second build under a generous budget must produce
byte-identical business output: budgets change only where data lives, never
the result.  A small random sub-scene is additionally diffed cell by cell
against the independent per-cell oracle (ioracle.py).
"""

import hashlib
import json
import os
import resource
import shutil
import sys

from . import datagen
from . import ioracle
from .builder import build as build_catalog
from .insp import build_coverage
from .ireader import Coverage
from .topo import Selection, build_topology


def _rss_bytes():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _business_hashes(out_dir):
    from . import iformat as F
    names = (F.MANAGED_NAME, F.COVERAGE_BEFORE_NAME, F.COVERAGE_AFTER_NAME,
             F.DIFF_NAME, F.SUMMARY_NAME)
    return {name: _sha(os.path.join(out_dir, name)) for name in names}


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _build_scene(records, registry, revisions, window, cutoffs, out_dir,
                 cache_bytes, max_spill_bytes, tag):
    jsonl = os.path.join(out_dir, "scale-%s.jsonl" % tag)
    datagen.write_jsonl(jsonl, records)
    cat_dir = os.path.join(out_dir, "scale-%s.cat" % tag)
    build_catalog(jsonl, cat_dir, cache_bytes=max(1 << 16, cache_bytes),
                  force=True)
    topo_dir = os.path.join(out_dir, "scale-%s.topo" % tag)
    build_topology(cat_dir, topo_dir, selection=Selection(),
                   cache_bytes=max(1 << 16, cache_bytes), force=True)
    reg_path = _write_jsonl(os.path.join(out_dir, "registry-%s.jsonl" % tag),
                            registry)
    insp_path = _write_jsonl(
        os.path.join(out_dir, "inspections-%s.jsonl" % tag), revisions)
    out = os.path.join(out_dir, "inspcov-%s" % tag)
    report = build_coverage(
        topo_dir, reg_path, [insp_path], out, window[0], window[1],
        cutoffs["before"], cutoffs["after"], cache_bytes=cache_bytes,
        max_spill_bytes=max_spill_bytes, force=True)
    return out, report


def run_insp_diag(out_dir, n_edges=400, trunk_obs=4000,
                  cache_bytes=1 << 20, max_spill_bytes=1 << 30, seed=47):
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    report = {"n_edges": n_edges, "trunk_obs": trunk_obs,
              "cache_bytes": cache_bytes, "max_spill_bytes": max_spill_bytes,
              "seed": seed, "python": sys.version.split()[0]}
    records, registry, revisions, window, cutoffs = datagen.gen_insp_scale(
        n_edges=n_edges, trunk_obs=trunk_obs, seed=seed)
    report["scene"] = {"catalog_records": len(records),
                       "managed_edges": len(registry),
                       "revision_lines": len(revisions),
                       "window": window, "cutoffs": cutoffs}
    rss0 = _rss_bytes()
    out_small, rep_small = _build_scene(
        records, registry, revisions, window, cutoffs, out_dir,
        cache_bytes, max_spill_bytes, "small")
    report["build"] = rep_small
    report["rss_delta_bytes"] = _rss_bytes() - rss0
    # independent reader validation (own tiling + diff<->ledger checks)
    with Coverage(out_small) as cov:
        report["validate"] = cov.validate_full()
    # budget independence: a generous-budget build must be byte-identical
    out_big, rep_big = _build_scene(
        records, registry, revisions, window, cutoffs, out_dir,
        max(cache_bytes, 8 << 20), max_spill_bytes, "big")
    report["budget_independence"] = {
        "small": _business_hashes(out_small),
        "big": _business_hashes(out_big),
    }
    same = report["budget_independence"]["small"] == \
        report["budget_independence"]["big"]
    report["budget_independence"]["identical"] = same
    assert same, "business output changed between budgets"
    assert rep_small["spill_runs"] > 0, "external sort never spilled"
    # per-cell oracle differential on a small random sub-scene
    import random
    rng = random.Random(seed + 1)
    (edges, o_records, o_revisions, o_window, from_ord,
     to_ord) = datagen.gen_insp_random(rng, n_edges=4, max_L=10,
                                       window_days=5)
    o_dir = os.path.join(out_dir, "oracle")
    os.makedirs(o_dir)
    o_out, _ = _build_scene(o_records, _registry_of(edges), o_revisions,
                            o_window, {"before": 2, "after": 4}, o_dir,
                            1 << 16, 1 << 28, "o")
    o_diff = _oracle_diff(o_out, edges, o_revisions, o_window, from_ord,
                          to_ord, before=2, after=4)
    report["oracle_diff"] = o_diff
    report["peak_rss_bytes"] = _rss_bytes()
    report["note"] = (
        "one long-tail trunk (%d obs) plus %d scattered edges; small-budget "
        "build spilled %d runs and stayed budget-independent; oracle-diffed "
        "a small sub-scene cell by cell"
        % (trunk_obs, n_edges, rep_small["spill_runs"]))
    return report


def _registry_of(edges):
    return [{"source_id": sid, "part_index": part, "chainage_end": L}
            for (sid, part), L in sorted(edges.items())]


def _oracle_diff(out_dir, edges, revisions, window, from_ord, to_ord,
                 before, after):
    """Compare the artifact to the independent per-cell oracle."""
    from . import iformat as F

    def parse_cov(name):
        rows = []
        with open(os.path.join(out_dir, name), "rb") as fh:
            fh.readline()
            fh.readline()
            for ln, raw in enumerate(fh, 3):
                rows.append(F.parse_coverage_line(raw, ln))
        return rows

    def parse_diff():
        rows = []
        with open(os.path.join(out_dir, F.DIFF_NAME), "rb") as fh:
            fh.readline()
            fh.readline()
            for ln, raw in enumerate(fh, 3):
                rows.append(F.parse_diff_line(raw, ln))
        return rows

    exp_b = ioracle.coverage_rows(edges, revisions, before, from_ord, to_ord)
    exp_a = ioracle.coverage_rows(edges, revisions, after, from_ord, to_ord)
    exp_d, exp_s = ioracle.diff_and_summary(edges, revisions, before, after,
                                            from_ord, to_ord)
    got_b = parse_cov(F.COVERAGE_BEFORE_NAME)
    got_a = parse_cov(F.COVERAGE_AFTER_NAME)
    got_d = parse_diff()
    assert got_b == exp_b, "coverage_before diverges from oracle"
    assert got_a == exp_a, "coverage_after diverges from oracle"
    assert got_d == exp_d, "diff diverges from oracle"
    with open(os.path.join(out_dir, F.SUMMARY_NAME),
              encoding="utf-8") as fh:
        summ = json.load(fh)
    assert summ["status_changes"] == exp_s["status_changes"]
    assert summ["evidence_only_changes"] == exp_s["evidence_only"]
    return {"rows_before": len(got_b), "rows_after": len(got_a),
            "diff_rows": len(got_d), "ok": True}
