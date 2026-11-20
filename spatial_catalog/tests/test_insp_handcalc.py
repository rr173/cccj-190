"""Hand-computed acceptance tests derived from crew data (not from any
production scan).  The flagship case is the worked example from the
contract: a length-100 edge over the report window Nov 1..4 (exclusive),
comparing receipt cutoffs 2 and 4."""

import json
import os

import pytest

from spatial_catalog import iformat as F
from spatial_catalog.ireader import Coverage
from insp_helpers import (line, make_registry, make_topology, parse_cov_rows,
                          parse_diff_rows, retract, run_coverage, upsert,
                          write_jsonl)

W = ("2026-11-01", "2026-11-04")   # report window: Nov 1, 2, 3


def _worked_example(tmp_path):
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


def test_worked_example_ledgers_and_diff(tmp_path):
    topo, reg, insp = _worked_example(tmp_path)
    out = str(tmp_path / "out")
    report = run_coverage(topo, reg, [insp], out, W, (2, 4))
    assert report["status"] == "ok"

    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    after = parse_cov_rows(os.path.join(out, F.COVERAGE_AFTER_NAME))
    diff = parse_diff_rows(os.path.join(out, F.DIFF_NAME))

    # ---- cutoff 2 ledger: A@1 pass [10,50); B@2 defect [30,50) from day 2
    d1, d2, d4 = (F.parse_date(s) for s in
                  ("2026-11-01", "2026-11-02", "2026-11-04"))
    assert before == [
        ("e1", 0, d1, d2, 0, 10, "gap", "", 0),
        ("e1", 0, d1, d2, 10, 50, "pass", "A", 1),
        ("e1", 0, d1, d2, 50, 100, "gap", "", 0),
        ("e1", 0, d2, d4, 0, 10, "gap", "", 0),
        ("e1", 0, d2, d4, 10, 30, "pass", "A", 1),
        ("e1", 0, d2, d4, 30, 50, "defect", "B", 2),
        ("e1", 0, d2, d4, 50, 100, "gap", "", 0),
    ]
    # ---- cutoff 4 ledger: A@3 pass [40,80); B retracted
    assert after == [
        ("e1", 0, d1, d4, 0, 40, "gap", "", 0),
        ("e1", 0, d1, d4, 40, 80, "pass", "A", 3),
        ("e1", 0, d1, d4, 80, 100, "gap", "", 0),
    ]
    # ---- diff: day 1 [40,50) evidence-only; days 2-3 [40,50) defect->pass;
    #      days 2-3 [30,40) defect->gap (newly re-gapped)
    assert diff == [
        ("e1", 0, d1, d2, 10, 40, "pass", "A", 1, "gap", "", 0),
        ("e1", 0, d1, d2, 40, 50, "pass", "A", 1, "pass", "A", 3),
        ("e1", 0, d1, d2, 50, 80, "gap", "", 0, "pass", "A", 3),
        ("e1", 0, d2, d4, 10, 30, "pass", "A", 1, "gap", "", 0),
        ("e1", 0, d2, d4, 30, 40, "defect", "B", 2, "gap", "", 0),
        ("e1", 0, d2, d4, 40, 50, "defect", "B", 2, "pass", "A", 3),
        ("e1", 0, d2, d4, 50, 80, "gap", "", 0, "pass", "A", 3),
    ]
    # ---- machine summary: status changes 200, evidence-only 10
    summary = json.load(open(os.path.join(out, F.SUMMARY_NAME)))
    assert summary["status_change_total"] == 200
    assert summary["evidence_only_total"] == 10
    assert summary["status_changes"] == {
        "pass>gap": 70, "gap>pass": 90, "defect>gap": 20,
        "defect>pass": 20}
    assert summary["evidence_only_changes"] == {"pass>pass": 10}
    # the delivered artifact self-validates
    with Coverage(out) as cov:
        assert cov.validate_full()["ok"]


def test_correction_moved_outside_window_does_not_resurrect_old(tmp_path):
    """A correction whose new interval falls outside the report window
    removes the old coverage entirely: the old version is NOT picked back
    up just because the newest version is out of window."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 50)}])
    reg = make_registry(tmp_path, {("e1", 0): 50})
    revisions = [
        upsert("A", 1, "e1", 0, "forward", 0, 50,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass"),
        # batch 2 moves A entirely after the report window
        upsert("A", 2, "e1", 0, "forward", 0, 50,
               "2026-12-01", "2026-12-05", "2026-11-01", 0, "defect"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 2))
    after = parse_cov_rows(os.path.join(out, F.COVERAGE_AFTER_NAME))
    d1, d4 = F.parse_date("2026-11-01"), F.parse_date("2026-11-04")
    # at cutoff 2 the only visible revision is the out-of-window one, so the
    # whole edge reverts to gap (the batch-1 version is not resurrected)
    assert after == [("e1", 0, d1, d4, 0, 50, "gap", "", 0)]


def test_correction_moved_to_another_edge(tmp_path):
    """A correction may relocate the observation to a different edge; the
    original edge loses the coverage, the new edge gains it."""
    topo = make_topology(tmp_path, [
        {"object_id": "e1", "geometry": line(0, 40)},
        {"object_id": "e2", "geometry": line(100, 160)},
    ])
    reg = make_registry(tmp_path, {("e1", 0): 40, ("e2", 0): 60})
    revisions = [
        upsert("A", 1, "e1", 0, "forward", 0, 40,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass"),
        upsert("A", 2, "e2", 0, "forward", 10, 25,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "defect"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 2))
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    after = parse_cov_rows(os.path.join(out, F.COVERAGE_AFTER_NAME))
    d1, d4 = F.parse_date("2026-11-01"), F.parse_date("2026-11-04")
    # cutoff 1: e1 fully pass
    assert ("e1", 0, d1, d4, 0, 40, "pass", "A", 1) in before
    # cutoff 2: e1 reverts to gap, e2 [10,25) defect
    assert ("e1", 0, d1, d4, 0, 40, "gap", "", 0) in after
    assert ("e2", 0, d1, d4, 10, 25, "defect", "A", 2) in after


def test_unicode_codepoint_tiebreak(tmp_path):
    """Same priority and same checked_on: the smaller inspection_id in
    Unicode code point order wins."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 10)}])
    reg = make_registry(tmp_path, {("e1", 0): 10})
    revisions = [
        upsert("z-crew", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", 5, "pass"),
        upsert("a-crew", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", 5, "defect"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 1))
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    d1, d4 = F.parse_date("2026-11-01"), F.parse_date("2026-11-04")
    # 'a-crew' < 'z-crew' in code point order, so a-crew's defect wins
    assert before == [("e1", 0, d1, d4, 0, 10, "defect", "a-crew", 1)]


def test_priority_then_checked_on_ordering(tmp_path):
    """Higher priority wins; ties broken by later checked_on."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 10)}])
    reg = make_registry(tmp_path, {("e1", 0): 10})
    revisions = [
        upsert("early", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", 5, "pass"),
        upsert("late", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-03", 5, "defect"),
        upsert("hiprio", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", 9, "pass"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 1))
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    d1, d4 = F.parse_date("2026-11-01"), F.parse_date("2026-11-04")
    # hiprio (priority 9) beats the later-checked priority-5 observation
    assert before == [("e1", 0, d1, d4, 0, 10, "pass", "hiprio", 1)]
    # without hiprio, the later checked_on wins
    insp2 = write_jsonl(tmp_path / "insp2.jsonl", revisions[:2])
    out2 = str(tmp_path / "out2")
    run_coverage(topo, reg, [insp2], out2, W, (1, 1))
    before2 = parse_cov_rows(os.path.join(out2, F.COVERAGE_BEFORE_NAME))
    assert before2 == [("e1", 0, d1, d4, 0, 10, "defect", "late", 1)]


def test_unrecorded_edge_is_all_gap(tmp_path):
    """A managed edge with zero observations is fully gap (it still gets a
    complete ledger)."""
    topo = make_topology(tmp_path, [
        {"object_id": "e1", "geometry": line(0, 30)},
        {"object_id": "e2", "geometry": line(100, 140)},
    ])
    reg = make_registry(tmp_path, {("e1", 0): 30, ("e2", 0): 40})
    revisions = [
        upsert("A", 1, "e1", 0, "forward", 0, 30,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W, (1, 1))
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    d1, d4 = F.parse_date("2026-11-01"), F.parse_date("2026-11-04")
    assert ("e2", 0, d1, d4, 0, 40, "gap", "", 0) in before
    with Coverage(out) as cov:
        assert cov.validate_full()["ok"]


def test_duplicate_delivery_counts_once(tmp_path):
    """The identical revision delivered twice (even across files) is one
    observation, not two."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 10)}])
    reg = make_registry(tmp_path, {("e1", 0): 10})
    rev = upsert("A", 1, "e1", 0, "forward", 0, 10,
                 "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass")
    f1 = write_jsonl(tmp_path / "f1.jsonl", [rev, dict(rev)])
    f2 = write_jsonl(tmp_path / "f2.jsonl", [dict(rev)])
    out = str(tmp_path / "out")
    report = run_coverage(topo, reg, [f1, f2], out, W, (1, 1))
    assert report["duplicate_deliveries"] == 2
    assert report["visible_observations"] == 1
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    d1, d4 = F.parse_date("2026-11-01"), F.parse_date("2026-11-04")
    assert before == [("e1", 0, d1, d4, 0, 10, "pass", "A", 1)]


def test_conflicting_revision_rejected(tmp_path):
    """Same (inspection_id, batch) with different field values is a
    conflict: the build fails and names the file and line."""
    from spatial_catalog.errors import InspectionError
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 10)}])
    reg = make_registry(tmp_path, {("e1", 0): 10})
    rev_a = upsert("A", 1, "e1", 0, "forward", 0, 10,
                   "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass")
    rev_b = upsert("A", 1, "e1", 0, "forward", 0, 5,
                   "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass")
    insp = write_jsonl(tmp_path / "insp.jsonl", [rev_a, rev_b])
    out = str(tmp_path / "out")
    with pytest.raises(InspectionError):
        run_coverage(topo, reg, [insp], out, W, (1, 1))
    # no manifest published; evidence names file and line
    assert not os.path.exists(os.path.join(out, F.MANIFEST_NAME))
    evidence = open(os.path.join(out + ".failed", "insp_rejects.jsonl")
                    ).read()
    assert "revision_conflict" in evidence
    assert "insp.jsonl" in evidence


def test_retract_of_unseen_id_is_legal_noop(tmp_path):
    """A retract for a never-seen inspection id is a legal invalidation
    statement (a no-op), not an error."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 10)}])
    reg = make_registry(tmp_path, {("e1", 0): 10})
    insp = write_jsonl(tmp_path / "insp.jsonl", [retract("ghost", 1)])
    out = str(tmp_path / "out")
    report = run_coverage(topo, reg, [insp], out, W, (1, 1))
    assert report["status"] == "ok"
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    d1, d4 = F.parse_date("2026-11-01"), F.parse_date("2026-11-04")
    assert before == [("e1", 0, d1, d4, 0, 10, "gap", "", 0)]


def test_equal_cutoffs_produce_empty_diff(tmp_path):
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 10)}])
    reg = make_registry(tmp_path, {("e1", 0): 10})
    revisions = [upsert("A", 1, "e1", 0, "forward", 0, 10,
                        "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass")]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    report = run_coverage(topo, reg, [insp], out, W, (2, 2))
    assert report["diff_rows"] == 0
    summary = json.load(open(os.path.join(out, F.SUMMARY_NAME)))
    assert summary["status_change_total"] == 0
    assert summary["evidence_only_total"] == 0
    with Coverage(out) as cov:
        assert cov.validate_full()["ok"]
