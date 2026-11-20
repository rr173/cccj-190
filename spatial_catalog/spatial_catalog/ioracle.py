"""Independent per-cell inspection-coverage oracle (test-only).

This module re-derives the expected coverage ledgers, diff and summary from
raw inputs by the simplest possible per-cell computation -- it expands every
managed edge's [from, to) x [0, L) grid cell by cell and picks the winner
with a plain loop.  It deliberately shares NO code with the production
sweep (insp.py): no spillable heaps, no profile mergers, no iformat sweep
records.  It exists so differential tests can compare the disk-bounded
production artifact, cell by cell and row by row, against a second,
independently reasoned answer on small inputs (small integer chainages and
short date windows keep the grid tiny).
"""

import datetime

GAP = "gap"


def _ord(s):
    return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10])).toordinal()


def _iso(o):
    return datetime.date.fromordinal(o).isoformat()


def select_visible(revisions, cutoff):
    """{inspection_id: winning upsert revision} at one receipt cutoff.

    revisions: iterable of plain dicts with keys inspection_id, batch,
    action ('upsert'/'retract') and the upsert fields.  The winner is the
    max batch <= cutoff; a winning retract invalidates (no fallback)."""
    best = {}
    for rev in revisions:
        iid = rev["inspection_id"]
        b = rev["batch"]
        if b <= cutoff and (iid not in best or b > best[iid]["batch"]):
            best[iid] = rev
    return {iid: rev for iid, rev in best.items()
            if rev["action"] == "upsert"}


def _visible_obs(managed_edge, L, revisions, cutoff, from_ord, to_ord):
    """The clipped, chainage-mapped observations of one edge at a cutoff."""
    sid, part = managed_edge
    visible = select_visible(revisions, cutoff)
    obs = []
    for rev in visible.values():
        if (rev["source_id"], rev["part_index"]) != (sid, part):
            continue
        d0 = max(_ord(rev["start"]), from_ord)
        d1 = min(_ord(rev["end"]), to_ord)
        if d0 >= d1:
            continue
        if rev["direction"] == "reverse":
            c0, c1 = L - rev["hi"], L - rev["lo"]
        else:
            c0, c1 = rev["lo"], rev["hi"]
        obs.append({"d0": d0, "d1": d1, "c0": c0, "c1": c1,
                    "priority": rev["priority"],
                    "checked_on": _ord(rev["checked_on"]),
                    "inspection_id": rev["inspection_id"],
                    "batch": rev["batch"], "result": rev["result"]})
    return obs


def _cell_winner(obs, d, c):
    """The winning observation at one (date, chainage) cell, or None."""
    best = None
    best_key = None
    for o in obs:
        if o["d0"] <= d < o["d1"] and o["c0"] <= c < o["c1"]:
            key = (o["priority"], o["checked_on"])
            if best_key is None or key > best_key or (
                    key == best_key and
                    o["inspection_id"] < best["inspection_id"]):
                best_key = key
                best = o
    return best


def coverage_grid(managed, revisions, cutoff, from_ord, to_ord):
    """{edge: {(d, c): (status, inspection_id, batch)}} per-cell map."""
    grid = {}
    for edge, L in managed.items():
        obs = _visible_obs(edge, L, revisions, cutoff, from_ord, to_ord)
        cells = {}
        for d in range(from_ord, to_ord):
            for c in range(L):
                w = _cell_winner(obs, d, c)
                if w is None:
                    cells[(d, c)] = (GAP, "", 0)
                else:
                    cells[(d, c)] = (w["result"], w["inspection_id"],
                                     w["batch"])
        grid[edge] = cells
    return grid


def _merge_day_profile(day_values, L):
    """Run-length encode one day's chainage values into (c0, c1, value)."""
    segs = []
    start = 0
    cur = day_values[0]
    for c in range(1, L):
        if day_values[c] != cur:
            segs.append((start, c, cur))
            start = c
            cur = day_values[c]
    segs.append((start, L, cur))
    return segs


def _merge_dates(day_profiles, from_ord, to_ord):
    """Merge adjacent days with identical profiles into date runs.

    day_profiles: {date_ord: [(c0, c1, value), ...]} for every date in the
    window.  Returns [(d0, d1, c0, c1, value), ...] rows."""
    rows = []
    run_start = None
    run_profile = None
    prev_d = None
    for d in range(from_ord, to_ord):
        prof = day_profiles[d]
        if run_profile is not None and prof == run_profile and \
                prev_d is not None and d == prev_d + 1:
            prev_d = d
            continue
        if run_profile is not None:
            for c0, c1, val in run_profile:
                rows.append((run_start, prev_d + 1, c0, c1, val))
        run_start = d
        run_profile = prof
        prev_d = d
    if run_profile is not None:
        for c0, c1, val in run_profile:
            rows.append((run_start, prev_d + 1, c0, c1, val))
    return rows


def coverage_rows(managed, revisions, cutoff, from_ord, to_ord):
    """Merged coverage rows for one cutoff.

    Returns a sorted list of (source_id, part_index, d0, d1, c0, c1,
    status, inspection_id, batch) matching parse_coverage_line tuples."""
    grid = coverage_grid(managed, revisions, cutoff, from_ord, to_ord)
    rows = []
    for edge, L in managed.items():
        cells = grid[edge]
        day_profiles = {}
        for d in range(from_ord, to_ord):
            day_values = [cells[(d, c)] for c in range(L)]
            day_profiles[d] = _merge_day_profile(day_values, L)
        for (d0, d1, c0, c1, val) in _merge_dates(day_profiles,
                                                  from_ord, to_ord):
            rows.append((edge[0], edge[1], d0, d1, c0, c1, val[0], val[1],
                         val[2]))
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[4]))
    return rows


def diff_and_summary(managed, revisions, cutoff_before, cutoff_after,
                     from_ord, to_ord):
    """Merged diff rows and the summary totals.

    Returns (diff_rows, summary) where diff_rows are sorted tuples
    (source_id, part_index, d0, d1, c0, c1, b_status, b_iid, b_batch,
    a_status, a_iid, a_batch) matching parse_diff_line tuples, and summary
    is {"status_changes": {...}, "evidence_only": {...}}."""
    before = coverage_grid(managed, revisions, cutoff_before,
                           from_ord, to_ord)
    after = coverage_grid(managed, revisions, cutoff_after,
                          from_ord, to_ord)
    rows = []
    status_changes = {}
    evidence_only = {}
    for edge, L in managed.items():
        b_cells = before[edge]
        a_cells = after[edge]
        # per-day diff profile: run-length of differing (before, after)
        day_profiles = {}
        for d in range(from_ord, to_ord):
            day_pairs = []
            for c in range(L):
                bv = b_cells[(d, c)]
                av = a_cells[(d, c)]
                day_pairs.append(None if bv == av else (bv, av))
            # run-length encode, treating None (no diff) as a hard break
            segs = []
            c = 0
            while c < L:
                if day_pairs[c] is None:
                    c += 1
                    continue
                start = c
                pair = day_pairs[c]
                c += 1
                while c < L and day_pairs[c] == pair:
                    c += 1
                segs.append((start, c, pair))
            day_profiles[d] = segs
        # date-merge identical non-empty profiles (empty breaks the run)
        run_start = None
        run_profile = None
        prev_d = None

        def flush():
            if run_profile is None:
                return
            for c0, c1, pair in run_profile:
                rows.append((edge[0], edge[1], run_start, prev_d + 1,
                             c0, c1, pair[0][0], pair[0][1], pair[0][2],
                             pair[1][0], pair[1][1], pair[1][2]))

        for d in range(from_ord, to_ord):
            prof = day_profiles[d]
            if not prof:
                flush()
                run_profile = None
                prev_d = d
                continue
            if run_profile is not None and prof == run_profile and \
                    d == prev_d + 1:
                prev_d = d
                continue
            flush()
            run_start = d
            run_profile = prof
            prev_d = d
        flush()
        # summary accumulation straight from the cells (counted once)
        for d in range(from_ord, to_ord):
            for c in range(L):
                bv = b_cells[(d, c)]
                av = a_cells[(d, c)]
                if bv == av:
                    continue
                key = "%s>%s" % (bv[0], av[0])
                if bv[0] != av[0]:
                    status_changes[key] = status_changes.get(key, 0) + 1
                else:
                    evidence_only[key] = evidence_only.get(key, 0) + 1
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[4]))
    return rows, {"status_changes": status_changes,
                  "evidence_only": evidence_only}
