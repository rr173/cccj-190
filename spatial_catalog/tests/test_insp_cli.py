"""End-to-end CLI tests for the inspcov build/validate/info/diag
subcommands."""

import json
import os
import subprocess
import sys


from spatial_catalog import iformat as F
from spatial_catalog.cli import main
from insp_helpers import (line, make_registry, make_topology, retract,
                          upsert, write_jsonl)

W = ("2026-11-01", "2026-11-04")


def _scene(tmp_path):
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 100)}])
    reg = make_registry(tmp_path, {("e1", 0): 100})
    revisions = [
        upsert("A", 1, "e1", 0, "forward", 10, 50,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass"),
        upsert("B", 2, "e1", 0, "reverse", 50, 70,
               "2026-11-02", "2026-11-04", "2026-11-02", 10, "defect"),
        upsert("A", 3, "e1", 0, "forward", 40, 80,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass"),
        retract("B", 4),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    return topo, reg, insp


def test_cli_build_validate_info(tmp_path, capsys):
    topo, reg, insp = _scene(tmp_path)
    out = str(tmp_path / "out")
    rc = main(["inspcov", "build", "--topology", topo, "--registry", reg,
               "--inspections", insp, "--date-from", W[0],
               "--date-to", W[1], "--cutoff-before", "2",
               "--cutoff-after", "4", "--out", out,
               "--cache-bytes", str(1 << 16), "--force"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert os.path.exists(os.path.join(out, F.MANIFEST_NAME))

    rc = main(["inspcov", "validate", "--coverage", out])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"]
    assert summary["status_change_total"] == 200
    assert summary["evidence_only_total"] == 10

    rc = main(["inspcov", "info", "--coverage", out])
    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["format"] == "offline-inspection-coverage"
    assert manifest["cutoffs"] == {"before": 2, "after": 4}
    assert manifest["report_window"]["from"] == "2026-11-01"


def test_cli_multiple_inspection_files(tmp_path, capsys):
    topo, reg, insp = _scene(tmp_path)
    # split the revisions into two files
    rows = [json.loads(l) for l in open(insp) if l.strip()]
    f1 = write_jsonl(tmp_path / "f1.jsonl", rows[:2])
    f2 = write_jsonl(tmp_path / "f2.jsonl", rows[2:])
    out = str(tmp_path / "out")
    rc = main(["inspcov", "build", "--topology", topo, "--registry", reg,
               "--inspections", f1, f2, "--date-from", W[0],
               "--date-to", W[1], "--cutoff-before", "2",
               "--cutoff-after", "4", "--out", out, "--force"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"


def test_cli_build_failure_exit_code(tmp_path, capsys):
    topo, reg, insp = _scene(tmp_path)
    out = str(tmp_path / "out")
    # before > after is a usage error -> exit code 2
    rc = main(["inspcov", "build", "--topology", topo, "--registry", reg,
               "--inspections", insp, "--date-from", W[0],
               "--date-to", W[1], "--cutoff-before", "5",
               "--cutoff-after", "2", "--out", out, "--force"])
    assert rc == 2
    assert not os.path.exists(os.path.join(out, F.MANIFEST_NAME))


def test_cli_validate_corrupt_returns_two(tmp_path):
    topo, reg, insp = _scene(tmp_path)
    out = str(tmp_path / "out")
    main(["inspcov", "build", "--topology", topo, "--registry", reg,
          "--inspections", insp, "--date-from", W[0], "--date-to", W[1],
          "--cutoff-before", "2", "--cutoff-after", "4", "--out", out,
          "--force"])
    p = os.path.join(out, F.DIFF_NAME)
    os.truncate(p, os.path.getsize(p) - 4)
    rc = main(["inspcov", "validate", "--coverage", out])
    assert rc == 2


def test_cli_diag(tmp_path, capsys):
    rc = main(["inspcov", "diag", "--out", str(tmp_path / "d"),
               "--n-edges", "60", "--trunk-obs", "600",
               "--cache-bytes", str(1 << 16), "--seed", "47"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["validate"]["ok"]
    assert report["budget_independence"]["identical"]
    assert report["oracle_diff"]["ok"]


def test_cli_module_invocation(tmp_path):
    topo, reg, insp = _scene(tmp_path)
    out = str(tmp_path / "out")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ,
               PYTHONPATH=repo_root + os.pathsep +
               os.environ.get("PYTHONPATH", ""))
    proc = subprocess.run(
        [sys.executable, "-m", "spatial_catalog", "inspcov", "build",
         "--topology", topo, "--registry", reg, "--inspections", insp,
         "--date-from", W[0], "--date-to", W[1], "--cutoff-before", "2",
         "--cutoff-after", "4", "--out", out, "--force"],
        cwd=repo_root, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert os.path.exists(os.path.join(out, F.MANIFEST_NAME))
