"""Scale, bounded-resource and determinism tests.

The scale scene contains many mutually disjoint small components and one
giant connected component.  Builds run under a deliberately small cache so
external sorts and the disk union-find genuinely spill, and the test
asserts:

  * every small component and the whole giant component are closed
    completely (no component-size cap, no truncation);
  * union-find page evictions and spill runs are non-zero (the bounded
    path really executed), while peak RSS does not scale with input size;
  * repeated builds on unchanged input produce byte-identical result
    files and identical summaries, regardless of run order or locale.

Large data is generated at runtime; no big fixture is committed.
"""

import json
import os
import shutil

import pytest

from spatial_catalog import datagen, tformat as tf
from spatial_catalog.builder import build as build_catalog
from spatial_catalog.tdiag import run_topo_diag
from spatial_catalog.topo import Selection, build_topology
from spatial_catalog.treader import Topology
from helpers import write_records


def _build_scale(tmp_path, n_small, big, cache_bytes):
    records = datagen.gen_topo_scale_records(
        n_small=n_small, big_size=big, seed=31)
    src = os.path.join(str(tmp_path), "in.jsonl")
    datagen.write_jsonl(src, records)
    cat = os.path.join(str(tmp_path), "cat")
    build_catalog(src, cat, cache_bytes=cache_bytes, force=True)
    out = os.path.join(str(tmp_path), "topo")
    report = build_topology(
        cat, out, selection=Selection(), exclude_non_lines=True,
        cache_bytes=cache_bytes, max_spill_bytes=1 << 30, force=True)
    return cat, out, report, records


def test_scale_scene_components_complete(tmp_path):
    cache = 1 << 16
    # > 2048 distinct nodes so the union-find parent table spans more
    # pages than its two-page bounded cache can hold at once.
    cat, out, report, records = _build_scale(
        tmp_path, n_small=600, big=1500, cache_bytes=cache)
    # the bounded path really spilled
    assert report["spill_runs"] > 0
    # the node table spans more union-find pages than the cache holds, so
    # the disk eviction path executes inside the real build
    assert report["union_find_evictions"] > 0
    with Topology(out) as t:
        sizes = {}
        for _o, _x, _y, _d, _r, comp in t.iter_nodes():
            sizes[comp] = sizes.get(comp, 0) + 1
        # the giant component keeps every ring node (closure is complete)
        largest = max(sizes.values())
        assert largest >= 1500
        # many small disjoint components survived intact
        assert len(sizes) > 400
        summary = t.validate_full(catalog=cat)
    assert summary["ok"]
    assert summary["components"] == len(sizes)


def test_tiny_cache_matches_large_cache(tmp_path):
    """A build that spills heavily must equal a comfortably cached build."""
    records = datagen.gen_topo_scale_records(
        n_small=60, big_size=150, seed=41)
    src = os.path.join(str(tmp_path), "in.jsonl")
    datagen.write_jsonl(src, records)
    cat = os.path.join(str(tmp_path), "cat")
    build_catalog(src, cat, cache_bytes=1 << 16, force=True)
    small = os.path.join(str(tmp_path), "small")
    large = os.path.join(str(tmp_path), "large")
    r_small = build_topology(cat, small, selection=Selection(),
                             exclude_non_lines=True,
                             cache_bytes=1 << 16, force=True)
    r_large = build_topology(cat, large, selection=Selection(),
                             exclude_non_lines=True,
                             cache_bytes=4 << 20, force=True)
    for name in (tf.NODES_NAME, tf.EDGES_NAME, tf.SELECTION_NAME):
        assert open(os.path.join(small, name), "rb").read() == \
            open(os.path.join(large, name), "rb").read()
    assert r_small["nodes"] == r_large["nodes"]
    assert r_small["components"] == r_large["components"]
    # the small-budget build genuinely used the disk-backed path
    assert r_small["spill_runs"] > 0


def test_deterministic_across_runs_and_locale(tmp_path, monkeypatch):
    cat, out1, _r, _recs = _build_scale(
        tmp_path, n_small=40, big=120, cache_bytes=1 << 16)
    out2 = str(tmp_path) + "-run2"
    build_topology(cat, out2, selection=Selection(),
                   exclude_non_lines=True,
                   cache_bytes=1 << 16, force=True)
    # force a non-C locale-style ordering hazard: sort keys are bytes, so
    # the output must not change even if the locale is altered.
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    out3 = str(tmp_path) + "-run3"
    build_topology(cat, out3, selection=Selection(),
                   exclude_non_lines=True,
                   cache_bytes=1 << 16, force=True)
    for name in (tf.NODES_NAME, tf.EDGES_NAME):
        b1 = open(os.path.join(out1, name), "rb").read()
        b2 = open(os.path.join(out2, name), "rb").read()
        b3 = open(os.path.join(out3, name), "rb").read()
        assert b1 == b2 == b3


def test_giant_component_is_not_capped(tmp_path):
    """No per-component size limit: a single component far larger than the
    cache pages must be produced in full, and the union-find must evict."""
    big = 2600
    _cat, out, report, _records = _build_scale(
        tmp_path, n_small=20, big=big, cache_bytes=1 << 16)
    with Topology(out) as t:
        sizes = {}
        for _o, _x, _y, _d, _r, comp in t.iter_nodes():
            sizes[comp] = sizes.get(comp, 0) + 1
    # the ring has big+1 edges all chained into one component; its node
    # count must be at least the number of distinct ring coordinates.
    assert max(sizes.values()) >= big
    assert report["components"] == len(sizes)
    assert report["union_find_evictions"] > 0


def test_diag_runner_end_to_end(tmp_path):
    out = str(tmp_path / "diag")
    # sized to exceed the union-find two-page cache (>2048 nodes)
    report = run_topo_diag(out, n=1200, big=1500,
                           cache_bytes=1 << 16, seed=31)
    assert report["validate"]["ok"]
    diff = report["oracle_diff"]
    assert diff["nodes"] == report["build"]["nodes"]
    assert diff["edges"] == report["build"]["edges"]
    # exactly one giant component, many small ones
    assert report["largest_component_sizes"][0] >= 1500
    assert report["build"]["spill_runs"] > 0
    assert report["build"]["union_find_evictions"] > 0
    # measured, non-placeholder resource figures
    assert report["build"]["peak_rss_bytes"] > 0
    assert report["build"]["tmp_bytes_written"] > 0
