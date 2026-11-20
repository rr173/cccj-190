"""Failure-mode tests: injected media faults, consumer interruption,
spill-budget exhaustion, staging convergence and protection of an
already-committed reconciliation.

A failed or interrupted build must never publish a new completion marker
(no inspcov_manifest.json at the target), must leave temp/staging files
converged, and must leave a previously valid reconciliation byte-identical.
"""

import hashlib
import os

import pytest

from spatial_catalog import insp as insp_mod
from spatial_catalog import iformat as F
from spatial_catalog.errors import (InspectionError,
                                    InspectionSpillBudgetExceeded)
from spatial_catalog.insp import (clear_fault, inject_fault)
from spatial_catalog.ireader import Coverage
from insp_helpers import (line, make_registry, make_topology, run_coverage,
                          upsert, write_jsonl)

W = ("2026-11-01", "2026-11-05")


def _scene(tmp_path, n=6):
    records = [{"object_id": "e%02d" % i, "geometry": line(i * 1000,
                                                           i * 1000 + 50)}
               for i in range(n)]
    topo = make_topology(tmp_path, records)
    edges = {("e%02d" % i, 0): 50 for i in range(n)}
    reg = make_registry(tmp_path, edges)
    revisions = []
    for i in range(n):
        revisions.append(upsert("A%d" % i, 1, "e%02d" % i, 0, "forward",
                                0, 50, "2026-11-01", "2026-11-05",
                                "2026-11-01", i % 3, "pass"))
        revisions.append(upsert("A%d" % i, 2, "e%02d" % i, 0, "reverse",
                                10, 40, "2026-11-02", "2026-11-05",
                                "2026-11-02", i % 3, "defect"))
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    return topo, reg, insp


STAGES = ["open", "registry", "topo_join", "revisions", "select", "map",
          "sweep", "commit"]


@pytest.fixture(autouse=True)
def _no_fault():
    clear_fault()
    yield
    clear_fault()


@pytest.mark.parametrize("stage", STAGES)
def test_media_failure_never_publishes(tmp_path, stage):
    topo, reg, insp = _scene(tmp_path)
    out = str(tmp_path / "out")
    inject_fault(stage, "oserror")
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, W, (1, 2))
    clear_fault()
    assert not os.path.exists(os.path.join(out, F.MANIFEST_NAME))
    failed = out + ".failed"
    assert os.path.isdir(failed)
    assert os.path.exists(os.path.join(failed, "build_report.json"))
    assert not os.path.exists(out + ".build-%d" % os.getpid())


@pytest.mark.parametrize("stage", ["registry", "select", "sweep", "commit"])
def test_consumer_interrupt_never_publishes(tmp_path, stage):
    topo, reg, insp = _scene(tmp_path)
    out = str(tmp_path / "out")
    inject_fault(stage, "keyboard")
    with pytest.raises(KeyboardInterrupt):
        run_coverage(topo, reg, [insp], out, W, (1, 2))
    clear_fault()
    assert not os.path.exists(os.path.join(out, F.MANIFEST_NAME))
    assert not os.path.exists(out + ".build-%d" % os.getpid())


def test_spill_budget_exhaustion_never_publishes(tmp_path):
    from spatial_catalog import datagen
    records, registry, revisions, window, cutoffs = datagen.gen_insp_scale(
        n_edges=120, trunk_obs=1500, seed=47)
    topo = make_topology(tmp_path, records)
    reg = write_jsonl(tmp_path / "reg.jsonl", registry)
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    with pytest.raises(InspectionSpillBudgetExceeded):
        run_coverage(topo, reg, [insp], out, window,
                     (cutoffs["before"], cutoffs["after"]),
                     cache_bytes=1 << 16, max_spill_bytes=1 << 16)
    assert not os.path.exists(os.path.join(out, F.MANIFEST_NAME))


def test_previous_valid_report_is_protected(tmp_path):
    topo, reg, insp = _scene(tmp_path)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 2))
    assert os.path.exists(os.path.join(out, F.MANIFEST_NAME))
    before = {}
    for name in os.listdir(out):
        p = os.path.join(out, name)
        if os.path.isfile(p):
            before[name] = (os.path.getsize(p),
                            hashlib.sha256(open(p, "rb").read()).hexdigest())
    # a failed rebuild must restore the old report byte-identically
    inject_fault("sweep", "oserror")
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, W, (1, 2))
    clear_fault()
    assert os.path.exists(os.path.join(out, F.MANIFEST_NAME))
    for name, (size, digest) in before.items():
        p = os.path.join(out, name)
        assert os.path.getsize(p) == size
        assert hashlib.sha256(open(p, "rb").read()).hexdigest() == digest
    with Coverage(out) as cov:
        assert cov.validate_full()["ok"]


def test_successful_rebuild_replaces_old_target(tmp_path):
    topo, reg, insp = _scene(tmp_path)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 2))
    # a second successful build with a narrower window replaces the target
    run_coverage(topo, reg, [insp], out, ("2026-11-01", "2026-11-03"),
                 (1, 2))
    with Coverage(out) as cov:
        assert cov.validate_full()["ok"]
    leftovers = [n for n in os.listdir(str(tmp_path))
                 if n.startswith("out.old") or n.startswith("out.build")]
    assert leftovers == []


def test_disk_full_on_final_write_does_not_publish(tmp_path, monkeypatch):
    topo, reg, insp = _scene(tmp_path, n=4)
    out = str(tmp_path / "out")
    real_open = insp_mod.builtins.open
    calls = {"n": 0}

    def flaky_open(path, *a, **k):
        fh = real_open(path, *a, **k)
        mode = a[0] if a else k.get("mode", "")
        if str(path).endswith("coverage_before.tsv") and "w" in mode:
            class _Boom:
                def __init__(self, inner):
                    self._inner = inner

                def write(self, data):
                    calls["n"] += 1
                    if calls["n"] == 2:
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

    monkeypatch.setattr(insp_mod, "open", flaky_open)
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, W, (1, 2))
    assert not os.path.exists(os.path.join(out, F.MANIFEST_NAME))


def test_empty_registry_produces_empty_report(tmp_path):
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 50)}])
    reg = write_jsonl(tmp_path / "reg.jsonl", [])
    insp = write_jsonl(tmp_path / "insp.jsonl", [])
    out = str(tmp_path / "out")
    report = run_coverage(topo, reg, [insp], out, W, (1, 2))
    assert report["managed_edges"] == 0
    assert report["coverage_rows_before"] == 0
    assert report["diff_rows"] == 0
    with Coverage(out) as cov:
        assert cov.validate_full()["ok"]
