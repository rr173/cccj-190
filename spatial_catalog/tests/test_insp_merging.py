"""Merge-rule edge cases: empty diff profiles break date runs, and
shadowed (hidden) boundaries are absorbed without splitting the delivered
rectangles."""

import os

from spatial_catalog import iformat as F
from insp_helpers import (line, make_registry, make_topology, parse_cov_rows,
                          parse_diff_rows, run_coverage, upsert, write_jsonl)

W3 = ("2026-11-01", "2026-11-04")   # Nov 1, 2, 3


def test_empty_diff_profile_breaks_date_merge(tmp_path):
    """Day 1 and day 3 have the identical diff, day 2 has none: the two
    diff rows must NOT merge across the empty day-2 profile."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 10)}])
    reg = make_registry(tmp_path, {("e1", 0): 10})
    revisions = [
        upsert("A", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass"),
        upsert("B", 2, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-02", "2026-11-01", 5, "defect"),
        upsert("C", 3, "e1", 0, "forward", 0, 10,
               "2026-11-03", "2026-11-04", "2026-11-03", 5, "defect"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W3, (1, 3))
    diff = parse_diff_rows(os.path.join(out, F.DIFF_NAME))
    d1 = F.parse_date("2026-11-01")
    d2 = F.parse_date("2026-11-02")
    d3 = F.parse_date("2026-11-03")
    d4 = F.parse_date("2026-11-04")
    # two separate single-day rows, never merged across the empty day 2
    assert diff == [
        ("e1", 0, d1, d2, 0, 10, "pass", "A", 1, "defect", "B", 2),
        ("e1", 0, d3, d4, 0, 10, "pass", "A", 1, "defect", "C", 3),
    ]


def test_identical_diff_profiles_merge_across_days(tmp_path):
    """The same diff on two adjacent days (no empty day between) merges
    into one date run."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 10)}])
    reg = make_registry(tmp_path, {("e1", 0): 10})
    revisions = [
        upsert("A", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", 0, "pass"),
        upsert("B", 2, "e1", 0, "forward", 0, 10,
               "2026-11-02", "2026-11-04", "2026-11-02", 5, "defect"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W3, (1, 2))
    diff = parse_diff_rows(os.path.join(out, F.DIFF_NAME))
    d2 = F.parse_date("2026-11-02")
    d4 = F.parse_date("2026-11-04")
    # days 2 and 3 share the identical diff profile -> one merged row
    assert diff == [
        ("e1", 0, d2, d4, 0, 10, "pass", "A", 1, "defect", "B", 2),
    ]


def test_shadowed_observation_boundary_is_hidden(tmp_path):
    """A lower-priority observation starting mid-window never wins, so its
    date boundary is hidden: the delivered coverage is one unsplit row and
    the report counts the hidden boundary."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 10)}])
    reg = make_registry(tmp_path, {("e1", 0): 10})
    revisions = [
        upsert("A", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", 5, "pass"),
        # B is entirely shadowed by A (lower priority, same span)
        upsert("B", 1, "e1", 0, "forward", 0, 10,
               "2026-11-02", "2026-11-04", "2026-11-02", 1, "defect"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    report = run_coverage(topo, reg, [insp], out, W3, (1, 1))
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    d1 = F.parse_date("2026-11-01")
    d4 = F.parse_date("2026-11-04")
    # B's day-2 start boundary is hidden: one unsplit pass row
    assert before == [("e1", 0, d1, d4, 0, 10, "pass", "A", 1)]
    # the sweep still processed two elementary date intervals (B's day-2
    # start boundary was examined, then merged away)
    assert report["elementary_date_intervals"] == 2
    assert report["date_runs_before"] == 1
    assert report["coverage_rows_before"] == 1
    # B was activated in the sweeps (its boundary was processed) yet never
    # delivered
    assert report["observation_activations"] >= 2


def test_shadowed_chainage_boundary_is_hidden(tmp_path):
    """A lower-priority observation over a sub-span is shadowed by a
    higher-priority one; its chainage boundaries do not split the row."""
    topo = make_topology(tmp_path, [{"object_id": "e1",
                                     "geometry": line(0, 10)}])
    reg = make_registry(tmp_path, {("e1", 0): 10})
    revisions = [
        upsert("A", 1, "e1", 0, "forward", 0, 10,
               "2026-11-01", "2026-11-04", "2026-11-01", 5, "pass"),
        upsert("B", 1, "e1", 0, "forward", 3, 7,
               "2026-11-01", "2026-11-04", "2026-11-01", 1, "defect"),
    ]
    insp = write_jsonl(tmp_path / "insp.jsonl", revisions)
    out = str(tmp_path / "out")
    run_coverage(topo, reg, [insp], out, W3, (1, 1))
    before = parse_cov_rows(os.path.join(out, F.COVERAGE_BEFORE_NAME))
    d1 = F.parse_date("2026-11-01")
    d4 = F.parse_date("2026-11-04")
    # B's chainage boundaries 3 and 7 are hidden under A
    assert before == [("e1", 0, d1, d4, 0, 10, "pass", "A", 1)]
