"""Read-only inspection-coverage reader and full validator.

Opening a reconciliation checks the manifest boundary (existence, format,
version, pinned lengths for every result file).  ``validate_full`` re-reads
every delivered file and proves, without rerunning the topology build:

  * every file's exact length and SHA-256 match the manifest;
  * every TSV row has the fixed header, field count, canonical encoding,
    valid dates/intervals and status vocabulary;
  * rows are strictly ordered by (source_id, part_index) Unicode code point
    order, then date start, then chainage start;
  * each coverage ledger tiles every managed edge's [from, to) x [0, L)
    completely -- no gap and no overlap (the window comes from the manifest,
    each edge's L from managed.tsv);
  * the diff is consistent with the two ledgers: an INDEPENDENT alignment of
    coverage_before and coverage_after re-derives exactly the delivered diff
    rectangles (this catches any drift between the ledgers and the diff);
  * summary.json totals equal the totals re-accumulated from diff.tsv;
  * the manifest's batch parameters (cutoffs, window) and row counts match
    the delivered content;
  * optionally, when the pinned registry is reachable, managed.tsv is
    re-derived from it; when the pinned topology is reachable, its content
    identity still matches.

The validator streams rows and holds at most one edge's rows at a time.
"""

import json
import os

from . import iformat as F
from .errors import CorruptionError


def _read_lines(path, magic, columns, parse, name):
    """Stream parsed rows, enforcing the two fixed headers."""
    with open(path, "rb") as fh:
        m = fh.readline()
        if m != magic.encode("utf-8"):
            raise CorruptionError("%s: bad/missing magic header" % name)
        c = fh.readline()
        if c != columns.encode("utf-8"):
            raise CorruptionError("%s: bad/missing column header" % name)
        line_no = 2
        for raw in fh:
            line_no += 1
            yield parse(raw, line_no)


def _edge_groups(row_iter, key_of):
    """Yield (edge_key, [rows]) per edge, one edge resident at a time."""
    cur_key = None
    cur_rows = []
    for row in row_iter:
        key = key_of(row)
        if cur_key is not None and key != cur_key:
            yield cur_key, cur_rows
            cur_rows = []
        cur_key = key
        cur_rows.append(row)
    if cur_key is not None:
        yield cur_key, cur_rows


class Coverage:
    """An opened, committed inspection-coverage reconciliation."""

    def __init__(self, path):
        self._path = path
        self._manifest = F.load_manifest(os.path.join(path, F.MANIFEST_NAME))
        for name in (F.MANAGED_NAME, F.COVERAGE_BEFORE_NAME,
                     F.COVERAGE_AFTER_NAME, F.DIFF_NAME, F.SUMMARY_NAME,
                     F.REPORT_NAME):
            p = os.path.join(path, name)
            if not os.path.isfile(p):
                raise CorruptionError("reconciliation file missing: %s" % p)
            actual = os.path.getsize(p)
            expect = self._manifest["files"][name]["length"]
            if actual != expect:
                raise CorruptionError(
                    "%s length %d != manifest %d (truncated or corrupted)"
                    % (name, actual, expect))
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self._closed = True

    @property
    def manifest(self):
        return self._manifest

    @property
    def path(self):
        return self._path

    # -------------------------------------------------------- streaming
    def iter_managed(self):
        return _read_lines(os.path.join(self._path, F.MANAGED_NAME),
                           F.MANAGED_MAGIC_LINE, F.MANAGED_COLUMNS_LINE,
                           F.parse_managed_line, "managed.tsv")

    def iter_coverage_before(self):
        return _read_lines(os.path.join(self._path, F.COVERAGE_BEFORE_NAME),
                           F.COVERAGE_MAGIC_LINE, F.COVERAGE_COLUMNS_LINE,
                           F.parse_coverage_line, "coverage_before.tsv")

    def iter_coverage_after(self):
        return _read_lines(os.path.join(self._path, F.COVERAGE_AFTER_NAME),
                           F.COVERAGE_MAGIC_LINE, F.COVERAGE_COLUMNS_LINE,
                           F.parse_coverage_line, "coverage_after.tsv")

    def iter_diff(self):
        return _read_lines(os.path.join(self._path, F.DIFF_NAME),
                           F.DIFF_MAGIC_LINE, F.DIFF_COLUMNS_LINE,
                           F.parse_diff_line, "diff.tsv")

    # -------------------------------------------------------- validation
    def validate_full(self, registry=None, topology=None):
        m = self._manifest
        # 1) checksums and pinned lengths for every delivered file
        for name in (F.MANAGED_NAME, F.COVERAGE_BEFORE_NAME,
                     F.COVERAGE_AFTER_NAME, F.DIFF_NAME, F.SUMMARY_NAME,
                     F.REPORT_NAME):
            actual_len, actual_sha = F.sha256_file(
                os.path.join(self._path, name))
            pinned = m["files"][name]
            if actual_len != pinned["length"]:
                raise CorruptionError(
                    "%s length %d != manifest %d" % (name, actual_len,
                                                     pinned["length"]))
            if actual_sha != pinned["sha256"]:
                raise CorruptionError("%s sha256 mismatch with manifest"
                                      % name)

        from_ord = m["report_window"]["from_ordinal"]
        to_ord = m["report_window"]["to_ordinal"]

        # 2) managed roster: order, canonical, uniqueness
        managed = {}        # (source_id, part) -> chainage_end
        prev_key = None
        n_managed = 0
        for source_id, part, chainage_end in self.iter_managed():
            key = (source_id, part)
            if prev_key is not None and not (prev_key < key):
                if prev_key == key:
                    raise CorruptionError(
                        "managed.tsv: duplicate edge %r" % (key,))
                raise CorruptionError(
                    "managed.tsv: edges not in Unicode code point order at "
                    "%r" % (key,))
            prev_key = key
            managed[key] = chainage_end
            n_managed += 1
        if n_managed != m["counts"]["managed_edges"]:
            raise CorruptionError(
                "managed.tsv rows %d != manifest managed_edges %d"
                % (n_managed, m["counts"]["managed_edges"]))

        # 3) both coverage ledgers: order + complete tiling
        rows_before = self._check_coverage(
            self.iter_coverage_before(), managed, from_ord, to_ord,
            "coverage_before.tsv")
        rows_after = self._check_coverage(
            self.iter_coverage_after(), managed, from_ord, to_ord,
            "coverage_after.tsv")
        if rows_before != m["counts"]["coverage_rows_before"]:
            raise CorruptionError(
                "coverage_before rows %d != manifest %d"
                % (rows_before, m["counts"]["coverage_rows_before"]))
        if rows_after != m["counts"]["coverage_rows_after"]:
            raise CorruptionError(
                "coverage_after rows %d != manifest %d"
                % (rows_after, m["counts"]["coverage_rows_after"]))

        # 4) diff rows: order, non-overlap, real differences + totals
        n_diff, summary = self._check_diff(self.iter_diff(), managed)
        if n_diff != m["counts"]["diff_rows"]:
            raise CorruptionError(
                "diff rows %d != manifest %d"
                % (n_diff, m["counts"]["diff_rows"]))

        # 5) diff <-> ledgers consistency (independent re-derivation)
        self._check_diff_matches_ledgers(managed)

        # 6) summary.json matches the totals re-accumulated from diff.tsv
        with open(os.path.join(self._path, F.SUMMARY_NAME),
                  "r", encoding="utf-8") as fh:
            summ = json.load(fh)
        self._check_summary(summ, summary, m)

        # 7) optional registry / topology re-identity
        registry_check = "skipped"
        if registry is not False:
            reg_path = registry if isinstance(registry, str) else \
                m["registry"]["path"]
            registry_check = self._check_registry(reg_path, managed, m)
        topology_check = "skipped"
        if topology is not False:
            topo_path = topology if isinstance(topology, str) else \
                m["topology"]["topology_path"]
            topology_check = self._check_topology(topo_path, m)

        return {"ok": True,
                "managed_edges": n_managed,
                "coverage_rows_before": rows_before,
                "coverage_rows_after": rows_after,
                "diff_rows": n_diff,
                "status_change_total": summ["status_change_total"],
                "evidence_only_total": summ["evidence_only_total"],
                "registry": registry_check,
                "topology": topology_check}

    # -------------------------------------------------- coverage tiling
    def _check_coverage(self, row_iter, managed, from_ord, to_ord, name):
        """Verify order and that each edge's rows tile [from,to) x [0,L)."""
        total = 0
        prev_row_key = None
        for edge_key, rows in _edge_groups(row_iter,
                                           lambda r: (r[0], r[1])):
            if edge_key not in managed:
                raise CorruptionError(
                    "%s: rows for unmanaged edge %r" % (name, edge_key))
            L = managed[edge_key]
            # rows must be sorted by (date_start, chainage_start) and tile
            expect_date = from_ord
            i = 0
            while i < len(rows):
                d0, d1 = rows[i][2], rows[i][3]
                if d0 != expect_date:
                    raise CorruptionError(
                        "%s: edge %r date gap/overlap at %s (expected start "
                        "%s)" % (name, edge_key, F.format_date(d0),
                                 F.format_date(expect_date)))
                # one date run: chainage segments tile [0, L)
                expect_c = 0
                j = i
                while j < len(rows) and rows[j][2] == d0 and rows[j][3] == d1:
                    c0, c1 = rows[j][4], rows[j][5]
                    if c0 != expect_c:
                        raise CorruptionError(
                            "%s: edge %r chainage gap/overlap at %d "
                            "(expected %d) in date run [%s,%s)"
                            % (name, edge_key, c0, expect_c,
                               F.format_date(d0), F.format_date(d1)))
                    if c1 > L:
                        raise CorruptionError(
                            "%s: edge %r chainage %d exceeds L=%d"
                            % (name, edge_key, c1, L))
                    expect_c = c1
                    j += 1
                if expect_c != L:
                    raise CorruptionError(
                        "%s: edge %r date run [%s,%s) covers chainage "
                        "[0,%d) != L=%d" % (name, edge_key,
                                            F.format_date(d0),
                                            F.format_date(d1), expect_c, L))
                expect_date = d1
                i = j
            if expect_date != to_ord:
                raise CorruptionError(
                    "%s: edge %r coverage ends at %s != window to %s"
                    % (name, edge_key, F.format_date(expect_date),
                       F.format_date(to_ord)))
            # global (edge, date, chainage) ordering across the whole file
            for row in rows:
                row_key = (row[0], row[1], row[2], row[4])
                if prev_row_key is not None and not (prev_row_key < row_key):
                    raise CorruptionError(
                        "%s: rows out of order at %r" % (name, row_key))
                prev_row_key = row_key
            total += len(rows)
        return total

    # -------------------------------------------------- diff row checks
    def _check_diff(self, row_iter, managed):
        """Verify diff row order, non-overlap, real differences; accumulate
        the summary totals from the delivered rows."""
        status_changes = {}
        evidence_only = {}
        total = 0
        prev_row_key = None
        for edge_key, rows in _edge_groups(row_iter,
                                           lambda r: (r[0], r[1])):
            if edge_key not in managed:
                raise CorruptionError(
                    "diff.tsv: rows for unmanaged edge %r" % (edge_key,))
            L = managed[edge_key]
            # rows within an edge: sorted by (date_start, chainage_start),
            # non-overlapping in the date x chainage plane
            prev_c_end = None
            prev_d_end = None
            for row in rows:
                (sid, part, d0, d1, c0, c1, b_status, b_iid, b_batch,
                 a_status, a_iid, a_batch) = row
                if (b_status, b_iid, b_batch) == (a_status, a_iid, a_batch):
                    raise CorruptionError(
                        "diff.tsv: identical before/after at %r" % (row,))
                if c1 > L:
                    raise CorruptionError(
                        "diff.tsv: edge %r chainage %d exceeds L=%d"
                        % (edge_key, c1, L))
                # non-overlap: rows sorted by (d0, c0); within a date run
                # chainage must not overlap; across runs dates must not
                if prev_d_end is not None:
                    if d0 < prev_d_end:
                        # same date run as previous row -> chainage disjoint
                        if c0 < prev_c_end:
                            raise CorruptionError(
                                "diff.tsv: overlapping rows at %r" % (row,))
                prev_c_end = c1
                prev_d_end = d1
                row_key = (sid, part, d0, c0)
                if prev_row_key is not None and not (prev_row_key < row_key):
                    raise CorruptionError(
                        "diff.tsv: rows out of order at %r" % (row_key,))
                prev_row_key = row_key
                area = (c1 - c0) * (d1 - d0)
                key = "%s>%s" % (b_status, a_status)
                if b_status != a_status:
                    status_changes[key] = status_changes.get(key, 0) + area
                else:
                    evidence_only[key] = evidence_only.get(key, 0) + area
                total += 1
        return total, {"status_changes": status_changes,
                       "evidence_only": evidence_only}

    # --------------------------------------- diff vs ledgers consistency
    def _check_diff_matches_ledgers(self, managed):
        """Re-derive the diff from the two ledgers and require exact equality
        with diff.tsv (independent alignment + merge)."""
        before_groups = _edge_groups(self.iter_coverage_before(),
                                     lambda r: (r[0], r[1]))
        after_groups = _edge_groups(self.iter_coverage_after(),
                                    lambda r: (r[0], r[1]))
        diff_groups = _edge_groups(self.iter_diff(),
                                   lambda r: (r[0], r[1]))
        # walk the three edge-sorted streams together
        b = _peek_iter(before_groups)
        a = _peek_iter(after_groups)
        d = _peek_iter(diff_groups)
        while True:
            b_key = b.peek()[0] if not b.empty() else None
            a_key = a.peek()[0] if not a.empty() else None
            if b_key is None and a_key is None:
                if not d.empty():
                    raise CorruptionError(
                        "diff.tsv has edge %r with no coverage"
                        % (d.peek()[0],))
                break
            if b_key is None or (a_key is not None and a_key < b_key):
                raise CorruptionError(
                    "coverage ledgers disagree on edge set at %r" % (a_key,))
            if a_key is None or b_key < a_key:
                raise CorruptionError(
                    "coverage ledgers disagree on edge set at %r" % (b_key,))
            # same edge on both sides
            _bk, b_rows = b.next()
            _ak, a_rows = a.next()
            expected = _expected_diff_rows(b_rows, a_rows)
            if not d.empty() and d.peek()[0] < b_key:
                raise CorruptionError(
                    "diff.tsv has edge %r with no coverage" % (d.peek()[0],))
            if not d.empty() and d.peek()[0] == b_key:
                _dk, d_rows = d.next()
            else:
                d_rows = []
            got = [(_status_of(r[6]), r[7], r[8], r[2], r[3], r[4], r[5],
                    _status_of(r[9]), r[10], r[11]) for r in d_rows]
            if len(got) != len(expected):
                raise CorruptionError(
                    "diff.tsv edge %r: %d rows != %d expected from ledgers"
                    % (b_key, len(got), len(expected)))
            for got_row, exp_row in zip(got, expected):
                if tuple(got_row) != tuple(exp_row):
                    raise CorruptionError(
                        "diff.tsv edge %r row %r != expected %r from "
                        "ledgers" % (b_key, got_row, exp_row))

    # -------------------------------------------------- summary check
    def _check_summary(self, summ, recomputed, m):
        if summ.get("unit") != "chainage_unit_days":
            raise CorruptionError("summary.json: bad unit")
        if summ.get("cutoffs") != m["cutoffs"]:
            raise CorruptionError(
                "summary.json cutoffs %r != manifest %r"
                % (summ.get("cutoffs"), m["cutoffs"]))
        if summ.get("report_window", {}).get("from") != \
                m["report_window"]["from"] or \
                summ.get("report_window", {}).get("to") != \
                m["report_window"]["to"]:
            raise CorruptionError(
                "summary.json window disagrees with manifest")
        for field, recomputed_map in (
                ("status_changes", recomputed["status_changes"]),
                ("evidence_only_changes", recomputed["evidence_only"])):
            got = {k: v for k, v in summ.get(field, {}).items() if v}
            want = {k: v for k, v in recomputed_map.items() if v}
            if got != want:
                raise CorruptionError(
                    "summary.json %s %r != recomputed %r from diff.tsv"
                    % (field, got, want))
        st = sum(recomputed["status_changes"].values())
        ev = sum(recomputed["evidence_only"].values())
        if summ.get("status_change_total") != st:
            raise CorruptionError(
                "summary.json status_change_total %r != %r"
                % (summ.get("status_change_total"), st))
        if summ.get("evidence_only_total") != ev:
            raise CorruptionError(
                "summary.json evidence_only_total %r != %r"
                % (summ.get("evidence_only_total"), ev))
        if summ.get("changed_units_days_total") != st + ev:
            raise CorruptionError("summary.json changed total mismatch")

    # -------------------------------------------------- input re-identity
    def _check_registry(self, reg_path, managed, m):
        if not os.path.exists(reg_path):
            return "unreachable:%s" % reg_path
        length, sha = F.sha256_file(reg_path)
        if sha != m["registry"]["sha256"] or length != m["registry"]["length"]:
            raise CorruptionError(
                "registry content changed since the build (pinned %s)"
                % m["registry"]["sha256"])
        # re-derive the managed roster from the registry file
        derived = {}
        with open(reg_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                key = (obj["source_id"], obj["part_index"])
                derived[key] = obj["chainage_end"]
        if derived != managed:
            raise CorruptionError(
                "managed.tsv does not match the registry's managed roster")
        return "ok"

    def _check_topology(self, topo_path, m):
        if not os.path.exists(topo_path):
            return "unreachable:%s" % topo_path
        from . import tformat as tf
        try:
            topo_manifest = tf.load_manifest(
                os.path.join(topo_path, tf.MANIFEST_NAME))
        except Exception as exc:
            raise CorruptionError("topology unreadable: %s" % exc)
        current = F.topology_identity(topo_path, topo_manifest)
        if current["identity_digest"] != m["topology"]["identity_digest"]:
            raise CorruptionError(
                "topology content identity changed since the build")
        return "ok"


# ----------------------------------------------------------- helpers

class _peek_iter:
    def __init__(self, it):
        self._it = iter(it)
        self._buf = None
        self._empty = False
        self._advance()

    def _advance(self):
        try:
            self._buf = next(self._it)
        except StopIteration:
            self._buf = None
            self._empty = True

    def peek(self):
        return self._buf

    def empty(self):
        return self._empty

    def next(self):
        v = self._buf
        self._advance()
        return v


def _status_of(status_str):
    return status_str


def _expected_diff_rows(before_rows, after_rows):
    """Independently re-derive the merged diff rows of one edge from its two
    coverage ledgers.  Returns a list of tuples
    (b_status, b_iid, b_batch, d0, d1, c0, c1, a_status, a_iid, a_batch)."""
    # collect all date boundaries
    bounds = set()
    for r in before_rows + after_rows:
        bounds.add(r[2])
        bounds.add(r[3])
    bounds = sorted(bounds)
    # index each ledger's rows by their date run for quick profile lookup
    def profiles(rows):
        """Return list of (d0, d1, [(c0,c1,val)...]) date runs."""
        runs = []
        i = 0
        while i < len(rows):
            d0, d1 = rows[i][2], rows[i][3]
            segs = []
            j = i
            while j < len(rows) and rows[j][2] == d0 and rows[j][3] == d1:
                segs.append((rows[j][4], rows[j][5],
                             (rows[j][6], rows[j][7], rows[j][8])))
                j += 1
            runs.append((d0, d1, segs))
            i = j
        return runs
    b_runs = profiles(before_rows)
    a_runs = profiles(after_rows)

    def profile_at(runs, t):
        for d0, d1, segs in runs:
            if d0 <= t < d1:
                return segs
        return None

    # per elementary date interval, align the two chainage profiles
    interval_profiles = []   # list of (t0, t1, [(c0,c1,bval,aval)])
    for k in range(len(bounds) - 1):
        t0, t1 = bounds[k], bounds[k + 1]
        b_segs = profile_at(b_runs, t0)
        a_segs = profile_at(a_runs, t0)
        cells = _align_profiles(b_segs, a_segs)
        # chainage-merge adjacent identical (bval, aval) cells
        merged = []
        for c0, c1, bval, aval in cells:
            if bval == aval:
                continue
            if merged and merged[-1][2] == bval and \
                    merged[-1][3] == aval and merged[-1][1] == c0:
                merged[-1] = (merged[-1][0], c1, bval, aval)
            else:
                merged.append((c0, c1, bval, aval))
        interval_profiles.append((t0, t1, merged))
    # date-merge adjacent identical non-empty profiles (never across empty)
    out = []
    run_start = None
    run_end = None
    run_profile = None
    for t0, t1, prof in interval_profiles:
        if not prof:
            if run_profile is not None:
                out.extend(_emit_diff_run(run_start, run_end, run_profile))
                run_profile = None
            continue
        if run_profile is not None and run_profile == prof and \
                run_end == t0:
            run_end = t1
        else:
            if run_profile is not None:
                out.extend(_emit_diff_run(run_start, run_end, run_profile))
            run_start = t0
            run_end = t1
            run_profile = prof
    if run_profile is not None:
        out.extend(_emit_diff_run(run_start, run_end, run_profile))
    return out


def _align_profiles(b_segs, a_segs):
    """Align two chainage profiles (each tiles [0,L)) into elementary cells
    (c0, c1, bval, aval)."""
    cells = []
    i = j = 0
    cur = 0
    while i < len(b_segs) or j < len(a_segs):
        b_c0, b_c1, bval = b_segs[i] if i < len(b_segs) else (None, None, None)
        a_c0, a_c1, aval = a_segs[j] if j < len(a_segs) else (None, None, None)
        nxt = min(x for x in (b_c1, a_c1) if x is not None)
        cells.append((cur, nxt, bval, aval))
        cur = nxt
        if b_c1 == nxt:
            i += 1
        if a_c1 == nxt:
            j += 1
    return cells


def _emit_diff_run(t0, t1, profile):
    rows = []
    for c0, c1, bval, aval in profile:
        rows.append((bval[0], bval[1], bval[2], t0, t1, c0, c1,
                     aval[0], aval[1], aval[2]))
    return rows
