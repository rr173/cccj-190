"""End-to-end CLI tests via subprocess: build, window, nearest, info,
validate, diag -- exactly the commands a user would run."""

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*argv, cwd=REPO_ROOT):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "spatial_catalog"] + list(argv),
        capture_output=True, text=True, cwd=cwd, env=env)


@pytest.fixture
def sample_jsonl(tmp_path):
    from spatial_catalog import datagen
    path = tmp_path / "data.jsonl"
    datagen.write_jsonl(str(path), datagen.gen_sparse_records(500, seed=5))
    return str(path)


def test_cli_build_query_validate(tmp_path, sample_jsonl):
    cat = str(tmp_path / "cat")
    r = run_cli("build", "--input", sample_jsonl, "--out", cat,
                "--cache-bytes", "1048576")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    assert report["status"] == "ok"
    assert report["object_count"] == 500

    r = run_cli("window", "--catalog", cat, "--rect", "0", "0", "100", "100")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["count"] == len(out["ids"]) > 0
    assert out["stats"]["nodes_visited"] >= 1
    assert out["stats"]["payloads_decoded"] >= out["count"]

    r = run_cli("nearest", "--catalog", cat, "--x", "10", "--y", "20",
                "--k", "5")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["count"] == 5
    dists = [row["distance"] for row in out["results"]]
    assert dists == sorted(dists)

    r = run_cli("nearest", "--catalog", cat, "--x", "0", "--y", "0",
                "--k", "0")
    assert json.loads(r.stdout)["count"] == 0

    r = run_cli("info", "--catalog", cat)
    assert r.returncode == 0
    assert json.loads(r.stdout)["object_count"] == 500

    r = run_cli("validate", "--catalog", cat)
    assert r.returncode == 0
    assert json.loads(r.stdout)["ok"] is True


def test_cli_build_reject_exit_code(tmp_path):
    src = tmp_path / "bad.jsonl"
    src.write_text('{"object_id": "a", "geometry": {"type": "Point", '
                   '"coordinates": [NaN, 0]}}\n')
    r = run_cli("build", "--input", str(src), "--out", str(tmp_path / "cat"))
    assert r.returncode == 2
    assert "rejects.jsonl" in r.stderr
    assert not (tmp_path / "cat").exists()
    # evidence file is readable
    rejects = (tmp_path / "cat.failed" / "rejects.jsonl").read_text()
    assert "non_finite_coordinate" in rejects


def test_cli_query_on_missing_catalog(tmp_path):
    r = run_cli("window", "--catalog", str(tmp_path / "nope"),
                "--rect", "0", "0", "1", "1")
    assert r.returncode == 2
    assert "not a committed catalog" in r.stderr


def test_cli_nearest_k_larger_than_n(tmp_path, sample_jsonl):
    cat = str(tmp_path / "cat")
    assert run_cli("build", "--input", sample_jsonl, "--out", cat).returncode == 0
    r = run_cli("nearest", "--catalog", cat, "--x", "1", "--y", "1",
                "--k", "99999")
    assert r.returncode == 0
    assert json.loads(r.stdout)["count"] == 500


def test_cli_build_gzip_input(tmp_path, sample_jsonl):
    import gzip
    gz = tmp_path / "data.jsonl.gz"
    with open(sample_jsonl, "rb") as src, gzip.open(str(gz), "wb") as dst:
        dst.write(src.read())
    cat = str(tmp_path / "catgz")
    r = run_cli("build", "--input", str(gz), "--out", cat)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["object_count"] == 500
    r = run_cli("nearest", "--catalog", cat, "--x", "5", "--y", "5", "--k", "3")
    assert json.loads(r.stdout)["count"] == 3


def test_cli_diag_smoke(tmp_path):
    r = run_cli("diag", "--out", str(tmp_path / "diag"), "--n", "2000",
                "--cache-bytes", "1048576")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    sparse = out["sparse"]
    # sparse nearest must touch far fewer payloads than a full scan
    assert sparse["queries"]["nearest"]["avg_payloads"] < \
        sparse["full_scan_payloads"] / 10
