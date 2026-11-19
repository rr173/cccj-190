"""End-to-end CLI tests for the xtab subcommands."""

import json
import os
import subprocess
import sys

from spatial_catalog.xreader import XTable
from helpers import write_records

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV = dict(os.environ, PYTHONPATH=_REPO_ROOT + os.pathsep +
            os.environ.get("PYTHONPATH", ""))


def _run(*args):
    proc = subprocess.run(
        [sys.executable, "-m", "spatial_catalog", *args],
        capture_output=True, text=True, cwd=_REPO_ROOT, env=_ENV)
    return proc


def test_xtab_cli_build_validate_info_diag(tmp_path):
    lines = [
        {"object_id": "L1",
         "geometry": {"type": "LineString", "coordinates": [[0, 1], [10, 1]]}},
        {"object_id": "L2",
         "geometry": {"type": "LineString", "coordinates": [[5, -5], [5, 5]]}},
    ]
    zones = [
        {"object_id": "A",
         "geometry": {"type": "Polygon",
                      "coordinates": [[[2, 0], [6, 0], [6, 4], [2, 4], [2, 0]]]}},
        {"object_id": "B",
         "geometry": {"type": "Polygon",
                      "coordinates": [[[8, 0], [12, 0], [12, 4], [8, 4], [8, 0]]]}},
    ]
    lp = write_records(str(tmp_path / "l.jsonl"), lines)
    zp = write_records(str(tmp_path / "z.jsonl"), zones)
    lc, zc = str(tmp_path / "l.cat"), str(tmp_path / "z.cat")
    xt = str(tmp_path / "xt")

    r = _run("build", "--input", lp, "--out", lc, "--force")
    assert r.returncode == 0, r.stderr
    r = _run("build", "--input", zp, "--out", zc, "--force")
    assert r.returncode == 0, r.stderr

    r = _run("xtab", "build", "--lines", lc, "--zones", zc,
             "--out", xt, "--cache-bytes", "1048576", "--force")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    assert report["status"] == "ok"
    assert report["output_rows"] == 3
    # measured, non-hard-coded counters
    assert report["bbox_candidates"] >= report["positive_length_pairs"]
    assert report["peak_rss_bytes"] > 0

    r = _run("xtab", "validate", "--crosstab", xt)
    assert r.returncode == 0, r.stderr
    v = json.loads(r.stdout)
    assert v["ok"] and v["rows"] == 3

    r = _run("xtab", "info", "--crosstab", xt)
    assert r.returncode == 0, r.stderr
    m = json.loads(r.stdout)
    assert m["record_count"] == 3
    assert m["line_catalog"]["object_count"] == 2

    # rows file content is deterministic UTF-8 with the fixed header
    raw = open(str(tmp_path / "xt" / "rows.tsv"), "rb").read()
    assert raw.startswith(b"# spatial-catalog line-zone crosstab v1\n")
    raw.decode("utf-8")  # strict UTF-8


def test_xtab_cli_failure_exit_code_two(tmp_path):
    # non-existent input catalog -> expected failure, exit code 2
    r = _run("xtab", "build",
             "--lines", str(tmp_path / "nope1"),
             "--zones", str(tmp_path / "nope2"),
             "--out", str(tmp_path / "xt"), "--force")
    assert r.returncode == 2
    assert "cross-table failed" in r.stderr


def test_xtab_cli_validate_corrupt_exit_two(tmp_path):
    lines = [{"object_id": "L",
              "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 0]]}}]
    zones = [{"object_id": "Z",
              "geometry": {"type": "Polygon",
                           "coordinates": [[[0, -1], [1, -1], [1, 1],
                                            [0, 1], [0, -1]]]}}]
    lp = write_records(str(tmp_path / "l.jsonl"), lines)
    zp = write_records(str(tmp_path / "z.jsonl"), zones)
    lc, zc, xt = (str(tmp_path / "l.cat"), str(tmp_path / "z.cat"),
                  str(tmp_path / "xt"))
    assert _run("build", "--input", lp, "--out", lc, "--force").returncode == 0
    assert _run("build", "--input", zp, "--out", zc, "--force").returncode == 0
    assert _run("xtab", "build", "--lines", lc, "--zones", zc,
                "--out", xt, "--force").returncode == 0
    # corrupt the rows file then validate -> exit 2
    p = str(tmp_path / "xt" / "rows.tsv")
    with open(p, "r+b") as fh:
        fh.seek(-1, 2)
        fh.truncate()  # drop final newline
    r = _run("xtab", "validate", "--crosstab", xt)
    assert r.returncode == 2


def test_xtab_cli_diag(tmp_path):
    out = str(tmp_path / "diag")
    r = _run("xtab", "diag", "--out", out,
             "--n-lines", "300", "--n-zones", "80",
             "--cache-bytes", "1048576", "--seed", "5")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    # sparse scene proves pruning; overlap scene reports degeneration
    sparse = report["sparse"]
    total = sparse["total_possible_pairs"]
    assert sparse["exact_intersections"] < total
    overlap = report["overlap"]
    assert overlap["exact_intersections"] == overlap["total_possible_pairs"]
    assert report["peak_rss_bytes"] > 0
    # both cross-tables on disk validate cleanly
    for name in ("sparse", "overlap"):
        with XTable(str(tmp_path / "diag" / ("%s.xtab" % name))) as xt:
            assert xt.validate_full(line_catalog=False,
                                    zone_catalog=False)["ok"]
