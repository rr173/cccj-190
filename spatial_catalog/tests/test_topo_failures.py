"""Failure-mode tests: injected media faults, consumer interruption,
spill-budget exhaustion, staging convergence and protection of an
already-committed target.

A failed or interrupted build must never publish a new completion marker
(no topo_manifest.json at the target), must leave temp/staging files
converged, and must leave a previously valid network byte-identical.
"""

import json
import os

import pytest

from spatial_catalog import topo as topo_mod
from spatial_catalog.builder import build as build_catalog
from spatial_catalog.errors import (TopologyError,
                                    TopologySpillBudgetExceeded)
from spatial_catalog.tformat import MANIFEST_NAME
from spatial_catalog.topo import Selection, build_topology, inject_fault, clear_fault
from spatial_catalog.treader import Topology
from helpers import linestring, write_records


def _make_catalog(tmp_path, n=40):
    recs = [{"object_id": "f-%04d" % i,
             "geometry": linestring([[i, 0], [i + 1, 1]])}
            for i in range(n)]
    src = write_records(tmp_path / "in.jsonl", recs)
    cat = str(tmp_path / "cat")
    build_catalog(src, cat, cache_bytes=1 << 16, force=True)
    return cat


STAGES = ["scan", "sort_edges", "nodemap", "joins", "union", "render",
          "commit"]


@pytest.fixture(autouse=True)
def _no_fault():
    clear_fault()
    yield
    clear_fault()


@pytest.mark.parametrize("stage", STAGES)
def test_media_failure_never_publishes(tmp_path, stage):
    cat = _make_catalog(tmp_path)
    out = str(tmp_path / "topo")
    inject_fault(stage, "oserror")
    with pytest.raises(TopologyError):
        build_topology(cat, out, selection=Selection(),
                       cache_bytes=1 << 16, max_spill_bytes=1 << 26,
                       force=True)
    clear_fault()
    # no completion marker at the target; evidence is in the failed dir
    assert not os.path.exists(os.path.join(out, MANIFEST_NAME))
    failed = out + ".failed"
    assert os.path.isdir(failed)
    assert os.path.exists(os.path.join(failed, "build_report.json"))
    # no staging directory is left behind
    assert not os.path.exists(out + ".build-%d" % os.getpid())


@pytest.mark.parametrize("stage", ["scan", "union", "render", "commit"])
def test_consumer_interrupt_never_publishes(tmp_path, stage):
    cat = _make_catalog(tmp_path)
    out = str(tmp_path / "topo")
    inject_fault(stage, "keyboard")
    with pytest.raises(KeyboardInterrupt):
        build_topology(cat, out, selection=Selection(),
                       cache_bytes=1 << 16, max_spill_bytes=1 << 26,
                       force=True)
    clear_fault()
    assert not os.path.exists(os.path.join(out, MANIFEST_NAME))
    assert not os.path.exists(out + ".build-%d" % os.getpid())


def test_spill_budget_exhaustion_fails_loudly(tmp_path):
    cat = _make_catalog(tmp_path, n=200)
    out = str(tmp_path / "topo")
    # the smallest legal disk ceiling: real spills exceed it, so the build
    # must abort rather than truncate the network.
    with pytest.raises(TopologySpillBudgetExceeded):
        build_topology(cat, out, selection=Selection(),
                       cache_bytes=1 << 16, max_spill_bytes=1 << 16,
                       force=True)
    assert not os.path.exists(os.path.join(out, MANIFEST_NAME))
    # the failed evidence carries the measured peak usage
    report = json.load(open(os.path.join(out + ".failed",
                                         "build_report.json"),
                            encoding="utf-8"))
    assert report["reason"] == "spill_budget_exceeded"
    assert report["counters"]["spill_bytes_peak"] > 0


def test_previous_valid_topology_is_protected(tmp_path):
    cat = _make_catalog(tmp_path, n=20)
    out = str(tmp_path / "topo")
    build_topology(cat, out, selection=Selection(),
                   cache_bytes=1 << 16, max_spill_bytes=1 << 26, force=True)
    assert os.path.exists(os.path.join(out, MANIFEST_NAME))
    before = {}
    for name in os.listdir(out):
        p = os.path.join(out, name)
        if os.path.isfile(p):
            before[name] = (os.path.getsize(p),
                            __import__("hashlib").sha256(
                                open(p, "rb").read()).hexdigest())
    # a failed rebuild must restore the old network byte-identically
    inject_fault("union", "oserror")
    with pytest.raises(TopologyError):
        build_topology(cat, out, selection=Selection(),
                       cache_bytes=1 << 16, max_spill_bytes=1 << 26,
                       force=True)
    clear_fault()
    assert os.path.exists(os.path.join(out, MANIFEST_NAME))
    for name, (size, digest) in before.items():
        p = os.path.join(out, name)
        assert os.path.getsize(p) == size
        assert __import__("hashlib").sha256(
            open(p, "rb").read()).hexdigest() == digest
    # the restored network still validates
    with Topology(out) as t:
        assert t.validate_full(catalog=cat)["ok"]


def test_successful_rebuild_replaces_old_target(tmp_path):
    cat = _make_catalog(tmp_path, n=10)
    out = str(tmp_path / "topo")
    build_topology(cat, out, selection=Selection(),
                   cache_bytes=1 << 16, force=True)
    # second successful build with a subset leaves a valid, smaller network
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("f-0000\nf-0001\n")
    build_topology(cat, out,
                   selection=Selection(ids_file=str(ids_file)),
                   cache_bytes=1 << 16, force=True)
    with Topology(out) as t:
        summary = t.validate_full(catalog=cat)
    assert summary["ok"] and summary["edges"] == 2
    # no backup/staging leftovers in the parent directory
    leftovers = [n for n in os.listdir(str(tmp_path))
                 if n.startswith("topo.old") or n.startswith("topo.build")]
    assert leftovers == []


def test_disk_full_on_final_write_does_not_publish(tmp_path, monkeypatch):
    cat = _make_catalog(tmp_path, n=15)
    out = str(tmp_path / "topo")
    real_open = topo_mod.builtins.open
    calls = {"n": 0}

    def flaky_open(path, *a, **k):
        fh = real_open(path, *a, **k)
        # fail writes to nodes.tsv only, simulating a full staging volume
        if str(path).endswith("nodes.tsv") and "w" in (a[0] if a else k.get("mode", "")):
            class _Boom:
                def __init__(self, inner):
                    self._inner = inner

                def write(self, data):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise OSError("no space left on device")
                    return self._inner.write(data)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return self._inner.__exit__(*exc)

                def __getattr__(self, name):
                    return getattr(self._inner, name)
            return _Boom(fh)
        return fh

    monkeypatch.setattr(topo_mod, "open", flaky_open)
    with pytest.raises(TopologyError):
        build_topology(cat, out, selection=Selection(),
                       cache_bytes=1 << 16, max_spill_bytes=1 << 26,
                       force=True)
    assert not os.path.exists(os.path.join(out, MANIFEST_NAME))


def test_empty_build_converges_cleanly(tmp_path):
    cat = _make_catalog(tmp_path, n=5)
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("# empty\n")
    out = str(tmp_path / "topo")
    report = build_topology(
        cat, out, selection=Selection(ids_file=str(ids_file)),
        cache_bytes=1 << 16, force=True)
    assert (report["nodes"], report["edges"], report["components"]) == (0, 0, 0)
    # header-only result files still validate
    with Topology(out) as t:
        assert t.validate_full(catalog=cat)["ok"]
    assert os.path.exists(os.path.join(out, MANIFEST_NAME))


def test_oracle_diff_survives_faults_and_rebuild(tmp_path):
    """Independent-oracle differential around injected faults.

    A random small graph is first built and diffed node-by-node against
    the naive oracle.  Then a media fault and a consumer abort are each
    injected; neither may publish a completion marker or corrupt the
    committed network.  A final clean rebuild must again match the oracle
    exactly, proving failure leaves no latent inconsistency."""
    import random
    from spatial_catalog import tformat as tf
    from spatial_catalog.topo_oracle import expected_network

    rng = random.Random(909)
    pool = [(x, y) for x in range(5) for y in range(5)]
    raw = []
    for i in range(rng.randint(8, 14)):
        coords = [pool[rng.randrange(len(pool))]
                  for _ in range(rng.randint(2, 4))]
        raw.append({"object_id": "g-%02d" % i,
                    "geometry": {"type": "LineString",
                                 "coordinates": coords}})
    src = write_records(tmp_path / "in.jsonl", raw)
    cat = str(tmp_path / "cat")
    build_catalog(src, cat, cache_bytes=1 << 16, force=True)
    out = str(tmp_path / "topo")

    def oracle_tables():
        exp = expected_network(raw)
        with Topology(out) as t:
            return exp, list(t.iter_nodes()), list(t.iter_edges())

    # 1) clean build matches the independent oracle
    build_topology(cat, out, selection=Selection(),
                   cache_bytes=1 << 16, max_spill_bytes=1 << 26, force=True)
    exp, nodes, edges = oracle_tables()
    assert len(nodes) == len(exp["nodes"])
    for got, want in zip(nodes, exp["nodes"]):
        assert got == want
    assert len(edges) == len(exp["edges"])
    good_manifest = open(os.path.join(out, MANIFEST_NAME), "rb").read()

    # 2) a media fault and an abort never publish a new marker
    for stage, kind in (("joins", "oserror"), ("union", "keyboard")):
        inject_fault(stage, kind)
        exc = KeyboardInterrupt if kind == "keyboard" else TopologyError
        with pytest.raises(exc):
            build_topology(cat, out, selection=Selection(),
                           cache_bytes=1 << 16, max_spill_bytes=1 << 26,
                           force=True)
        clear_fault()
        # the committed marker is the original, untouched
        assert open(os.path.join(out, MANIFEST_NAME), "rb").read() == \
            good_manifest
        exp2, nodes2, edges2 = oracle_tables()
        assert nodes2 == nodes and edges2 == edges

    # 3) final clean rebuild still matches the oracle byte-for-byte
    build_topology(cat, out, selection=Selection(),
                   cache_bytes=1 << 16, max_spill_bytes=1 << 26, force=True)
    exp3, nodes3, edges3 = oracle_tables()
    assert nodes3 == nodes and edges3 == edges
    with Topology(out) as t:
        assert t.validate_full(catalog=cat)["ok"]
