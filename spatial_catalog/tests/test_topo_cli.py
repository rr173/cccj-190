"""End-to-end CLI tests for the topo build/validate/info/diag subcommands."""

import json
import os
import subprocess
import sys

import pytest

from spatial_catalog.builder import build as build_catalog
from spatial_catalog.cli import main
from helpers import linestring, write_records


def _catalog(tmp_path, records):
    src = write_records(tmp_path / "in.jsonl", records)
    cat = str(tmp_path / "cat")
    build_catalog(src, cat, cache_bytes=1 << 16, force=True)
    return cat


def _records():
    return [
        {"object_id": "a", "geometry": linestring([[0, 0], [1, 0]])},
        {"object_id": "b", "geometry": linestring([[1, 0], [2, 0]])},
        {"object_id": "c", "geometry": linestring([[2, 0], [2, 1]])},
        {"object_id": "pt",
         "geometry": {"type": "Point", "coordinates": [5, 5]}},
    ]


def test_cli_build_validate_info(tmp_path, capsys):
    cat = _catalog(tmp_path, _records())
    out = str(tmp_path / "topo")
    rc = main(["topo", "build", "--catalog", cat, "--out", out,
               "--cache-bytes", str(1 << 16),
               "--max-spill-bytes", str(1 << 26),
               "--exclude-non-lines", "--force"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["edges"] == 3
    assert os.path.exists(os.path.join(out, "topo_manifest.json"))

    rc = main(["topo", "validate", "--topology", out, "--catalog", cat])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] and summary["nodes"] == 4

    rc = main(["topo", "info", "--topology", out])
    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["format"] == "offline-line-topology"
    assert manifest["counts"]["edges"] == 3


def test_cli_build_with_ids_file_and_prefix(tmp_path, capsys):
    cat = _catalog(tmp_path, _records())
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("a\nb\n")
    out = str(tmp_path / "topo")
    rc = main(["topo", "build", "--catalog", cat, "--out", out,
               "--ids-file", str(ids_file),
               "--id-prefix", "c",
               "--cache-bytes", str(1 << 16),
               "--max-spill-bytes", str(1 << 26),
               "--exclude-non-lines", "--force"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["edges"] == 3
    assert report["records_excluded_filter"] == 1   # pt excluded


def test_cli_type_rejection_exit_code(tmp_path):
    cat = _catalog(tmp_path, _records())
    out = str(tmp_path / "topo")
    # without --exclude-non-lines the point triggers an expected failure
    rc = main(["topo", "build", "--catalog", cat, "--out", out,
               "--cache-bytes", str(1 << 16), "--force"])
    assert rc == 2
    assert not os.path.exists(os.path.join(out, "topo_manifest.json"))


def test_cli_validate_corrupt_returns_two(tmp_path):
    cat = _catalog(tmp_path, _records())
    out = str(tmp_path / "topo")
    main(["topo", "build", "--catalog", cat, "--out", out,
          "--cache-bytes", str(1 << 16),
          "--max-spill-bytes", str(1 << 26),
          "--exclude-non-lines", "--force"])
    p = os.path.join(out, "edges.tsv")
    os.truncate(p, os.path.getsize(p) - 4)
    rc = main(["topo", "validate", "--topology", out, "--no-catalog"])
    assert rc == 2


def test_cli_diagnose_alias(tmp_path, capsys):
    cat = _catalog(tmp_path, _records())
    # 'diagnose' is an alias for 'diag'; both select the same command.
    ns_args = ["topo", "diagnose", "--out", str(tmp_path / "d"),
               "--n", "1400", "--big", "1500",
               "--cache-bytes", str(1 << 16), "--seed", "31"]
    rc = main(ns_args)
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["validate"]["ok"]


def test_cli_module_invocation(tmp_path):
    cat = _catalog(tmp_path, _records())
    out = str(tmp_path / "topo")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ,
               PYTHONPATH=repo_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.run(
        [sys.executable, "-m", "spatial_catalog", "topo", "build",
         "--catalog", cat, "--out", out, "--cache-bytes", str(1 << 16),
         "--max-spill-bytes", str(1 << 26),
         "--exclude-non-lines", "--force"],
        cwd=repo_root, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert os.path.exists(os.path.join(out, "topo_manifest.json"))
