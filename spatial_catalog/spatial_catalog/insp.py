"""Inspection-coverage reconciliation builder.

Given a committed line-topology (the edge roster), a chainage registry and
one or more unordered UTF-8 JSONL inspection files, reproduce the coverage
ledger at two receipt cutoffs for one report window and reconcile them.

Pipeline (every stage streams; nothing assumes the data fits RAM):

  phase 0  open the topology READ-ONLY and pin its content identity.
  phase 1  parse the chainage registry, external-sort it by edge, reject
           duplicate registrations, then merge-join against the topology
           edge table: unknown edges and zero-length geometry edges are
           rejected.  Survivors are the managed roster (managed.tsv).
  phase 2  parse every inspection JSONL line into a normalised revision
           record, external-sort by (inspection_id, batch), collapse exact
           duplicate deliveries, and FAIL on conflicting field values for
           the same (inspection_id, batch) with file/line evidence.
  phase 3  per inspection id pick the max-batch revision at each cutoff
           (retracts win and invalidate; no fallback to older versions),
           drop retracts, clip to the report window, external-sort the
           visible observations by (edge, date), merge-join with the managed
           roster to validate hi <= L and map direction (reverse maps
           [lo,hi) to [L-hi, L-lo)), and emit per-edge date endpoints.
  phase 4  per managed edge run the fused date x chainage sweep: for each
           elementary date interval, the chainage envelope of each cutoff's
           active observations (priority, then checked_on, then smaller id)
           is computed with budgeted spillable heaps; adjacent equal-value
           chainage cells merge, adjacent identical whole-edge profiles
           merge into date runs.  Both ledgers and the aligned diff are
           emitted as rectangle rows; the summary accumulates per row.
  phase 5  checksums, measured report, then the manifest commit point.

Memory/disk accounting: external-sort chunks and every sweep heap/profile
are sized from ``cache_bytes``; all long-tail state lives in budgeted spill
files and an explicit ``max_spill_bytes`` ceiling fails the build loudly
instead of publishing a partial monthly report.  A failed or interrupted
build never has a manifest and a previously committed target is restored
untouched.
"""

import builtins
import json
import os
import resource
import shutil
import struct
import time
from datetime import datetime, timezone

from . import iformat as F
from .errors import (CorruptionError, InspectionError,
                     InspectionSpillBudgetExceeded, NotACatalogError)
from .spill import external_sort, SpillableHeap
from .treader import Topology

DEFAULT_CACHE_BYTES = 64 << 20
DEFAULT_SPILL_BYTES = 1 << 30
_GUARD_EVERY = 2048

# Fault-injection hooks used by the failure tests (see topo.py).
_FAULT_STAGE = None
_FAULT_KIND = "oserror"

open = builtins.open  # noqa: A001  (module alias so tests can patch it)

_LEN = struct.Struct(">I")

# Gap cell value: (status_code, inspection_id, batch).
_GAP_VAL = (F.CODE_GAP, "", 0)


def _maybe_fault(stage):
    if _FAULT_STAGE == stage:
        if _FAULT_KIND == "keyboard":
            raise KeyboardInterrupt("injected consumer abort at %s" % stage)
        raise OSError("injected storage media failure at %s" % stage)


def _rss_bytes():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def inject_fault(stage, kind="oserror"):
    """Test hook: fail the next build when it enters ``stage``."""
    global _FAULT_STAGE, _FAULT_KIND
    _FAULT_STAGE = stage
    _FAULT_KIND = kind


def clear_fault():
    """Test hook: remove any injected failure."""
    global _FAULT_STAGE
    _FAULT_STAGE = None


class InspStats:
    """Measured build counters; every field is incremented by real work."""

    __slots__ = (
        "topology_edges_scanned", "registry_lines", "managed_edges",
        "revision_files", "revision_lines", "revisions_upsert",
        "revisions_retract", "duplicate_deliveries",
        "visible_observations", "visible_before", "visible_after",
        "observations_clipped_empty",
        "elementary_date_intervals", "observation_activations",
        "cov_before_segments", "cov_after_segments", "diff_segments",
        "date_runs_before", "date_runs_after", "date_runs_diff",
        "coverage_rows_before", "coverage_rows_after", "diff_rows",
        "spill_runs", "heap_spills", "heap_high_water", "profile_spills",
        "spill_bytes_peak", "tmp_bytes_written",
        "peak_rss_bytes")

    def __init__(self):
        for slot in self.__slots__:
            setattr(self, slot, 0)

    def as_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


# ----------------------------------------------------------- input parsing

def _json_loads_strict(text, where):
    """Parse one JSON object line; NaN/Infinity and non-objects rejected."""
    def _bad_const(_c):
        raise ValueError("NaN/Infinity literal not allowed")
    try:
        obj = json.loads(text, parse_constant=_bad_const)
    except ValueError as exc:
        raise InspectionError("invalid JSON at %s: %s" % (where, exc),
                              reason="bad_json")
    if not isinstance(obj, dict):
        raise InspectionError("line at %s is not a JSON object" % where,
                              reason="bad_json")
    return obj


def _req(obj, field, where):
    if field not in obj:
        raise InspectionError("missing field %r at %s" % (field, where),
                              reason="missing_field")
    return obj[field]


def _norm_revision(obj, where):
    """Validate and normalise one inspection revision line.

    Returns (inspection_id, batch, content_bytes) where content_bytes is the
    canonical JSON encoding of the normalised revision (used for duplicate
    and conflict detection)."""
    inspection_id = _req(obj, "inspection_id", where)
    F.check_text_id(inspection_id, "inspection_id")
    batch = _req(obj, "batch", where)
    F.check_int64(batch, "batch")
    if batch <= 0:
        raise InspectionError("batch must be a positive integer at %s"
                              % where, reason="bad_batch")
    action = _req(obj, "action", where)
    if action == F.ACTION_RETRACT:
        # A retract carries no observation content; an unseen id is a legal
        # no-op invalidation.  Normalise to a fixed canonical content.
        return inspection_id, batch, b'{"action":"retract"}'
    if action != F.ACTION_UPSERT:
        raise InspectionError("unknown action %r at %s" % (action, where),
                              reason="bad_action")
    source_id = _req(obj, "source_id", where)
    F.check_text_id(source_id, "source_id")
    part_index = _req(obj, "part_index", where)
    F.check_int64(part_index, "part_index")
    if part_index < 0:
        raise InspectionError("part_index must be >= 0 at %s" % where,
                              reason="bad_part_index")
    direction = _req(obj, "direction", where)
    if direction not in F.DIRECTIONS:
        raise InspectionError("direction must be 'forward' or 'reverse' at "
                              "%s, got %r" % (where, direction),
                              reason="bad_direction")
    lo = _req(obj, "lo", where)
    hi = _req(obj, "hi", where)
    F.check_int64(lo, "lo")
    F.check_int64(hi, "hi")
    if not (0 <= lo < hi):
        raise InspectionError("require 0 <= lo < hi at %s, got [%r, %r)"
                              % (where, lo, hi), reason="bad_interval")
    start = F.parse_date(_req(obj, "start", where), "start")
    end = F.parse_date(_req(obj, "end", where), "end")
    if not (start < end):
        raise InspectionError("coverage date interval is empty at %s "
                              "(%s >= %s)" % (where, F.format_date(start),
                                              F.format_date(end)),
                              reason="bad_date_interval")
    checked_on = F.parse_date(_req(obj, "checked_on", where), "checked_on")
    priority = _req(obj, "priority", where)
    F.check_int64(priority, "priority")
    if priority < 0:
        raise InspectionError("priority must be >= 0 at %s" % where,
                              reason="bad_priority")
    result = _req(obj, "result", where)
    if result not in F.RESULTS:
        raise InspectionError("result must be 'pass' or 'defect' at %s, "
                              "got %r" % (where, result), reason="bad_result")
    content = {
        "action": F.ACTION_UPSERT,
        "source_id": source_id,
        "part_index": part_index,
        "direction": direction,
        "lo": lo, "hi": hi,
        "start": start, "end": end,
        "checked_on": checked_on,
        "priority": priority,
        "result": result,
    }
    canon = json.dumps(content, sort_keys=True, separators=(",", ":")
                       ).encode("utf-8")
    return inspection_id, batch, canon


def _read_lp(path):
    """Iterate length-prefixed records of a spill file."""
    with open(path, "rb") as fh:
        while True:
            head = fh.read(_LEN.size)
            if not head:
                return
            (n,) = _LEN.unpack(head)
            rec = fh.read(n)
            if len(rec) != n:
                raise InspectionError("spill file %s truncated" % path,
                                      reason="truncated_spill")
            yield rec


def _write_lp(fh, rec):
    fh.write(_LEN.pack(len(rec)))
    fh.write(rec)


class _Peek:
    """One-element lookahead over an iterator."""

    def __init__(self, iterable):
        self._it = iter(iterable)
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
        value = self._buf
        self._advance()
        return value


class _EdgeGrouper:
    """Groups a sorted record stream into consecutive equal-edge runs.

    ``peek_key`` inspects the next group's edge without consuming anything;
    ``take_group`` returns a lazy sub-iterator that must be fully consumed
    before the next ``take_group``/``peek_key`` (the streams are sorted, so
    groups are contiguous)."""

    def __init__(self, sorted_iter, key_fn):
        self._src = _Peek(sorted_iter)
        self._key_fn = key_fn

    def peek_key(self):
        if self._src.empty():
            return None
        return self._key_fn(self._src.peek())

    def take_group(self):
        key = self._key_fn(self._src.peek())
        src = self._src
        key_fn = self._key_fn

        def group():
            while not src.empty() and key_fn(src.peek()) == key:
                yield src.next()
        return key, group()


# --------------------------------------------------------- profile merging

def _pack_cov_seg(c0, c1, value):
    status, iid, batch = value
    ib = iid.encode("utf-8")
    return struct.pack(">qqBQH", c0, c1, status, batch, len(ib)) + ib


def _unpack_cov_seg(rec):
    c0, c1, status, batch, il = struct.unpack_from(">qqBQH", rec, 0)
    iid = rec[struct.calcsize(">qqBQH"):struct.calcsize(">qqBQH") + il]
    return c0, c1, (status, iid.decode("utf-8"), batch)


def _pack_diff_seg(c0, c1, value):
    (b_status, b_iid, b_batch), (a_status, a_iid, a_batch) = value
    bb = b_iid.encode("utf-8")
    ab = a_iid.encode("utf-8")
    head = struct.pack(">qqBQHBQH", c0, c1, b_status, b_batch, len(bb),
                       a_status, a_batch, len(ab))
    return head + bb + ab


def _unpack_diff_seg(rec):
    fmt = ">qqBQHBQH"
    (c0, c1, b_status, b_batch, bl, a_status, a_batch,
     al) = struct.unpack_from(fmt, rec, 0)
    p = struct.calcsize(fmt)
    b_iid = rec[p:p + bl].decode("utf-8")
    a_iid = rec[p + bl:p + bl + al].decode("utf-8")
    return c0, c1, ((b_status, b_iid, b_batch), (a_status, a_iid, a_batch))


class _ProfileStore:
    """Order-preserving spillable segment list (bytes records).

    Segments stay in memory until the byte budget is exceeded, then the
    whole list spills to a temp file and later appends go straight to disk.
    """

    def __init__(self, max_bytes, spill_dir, tag, on_spill=None):
        self._max = max(1024, int(max_bytes))
        self._dir = spill_dir
        self._tag = tag
        self._on_spill = on_spill
        self._items = []
        self._nbytes = 0
        self._count = 0
        self._fh = None
        self._path = None
        self._spilled = False

    def add(self, rec):
        self._count += 1
        if self._spilled:
            _write_lp(self._fh, rec)
            return
        self._items.append(rec)
        self._nbytes += len(rec)
        if self._nbytes > self._max:
            self._spill()

    def _spill(self):
        self._path = os.path.join(self._dir, "%s.tmp" % self._tag)
        self._fh = open(self._path, "w+b")
        for rec in self._items:
            _write_lp(self._fh, rec)
        self._items = []
        self._spilled = True
        if self._on_spill is not None:
            self._on_spill()

    def __len__(self):
        return self._count

    def __iter__(self):
        if not self._spilled:
            return iter(self._items)
        self._fh.flush()
        self._fh.seek(0)
        return self._read_file()

    def _read_file(self):
        while True:
            head = self._fh.read(_LEN.size)
            if not head:
                return
            (n,) = _LEN.unpack(head)
            rec = self._fh.read(n)
            if len(rec) != n:
                raise InspectionError("profile spill truncated",
                                      reason="truncated_spill")
            yield rec

    def reset(self):
        self._items = []
        self._nbytes = 0
        self._count = 0
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self._path is not None:
            try:
                os.unlink(self._path)
            except OSError:
                pass
            self._path = None
        self._spilled = False

    def equal_to(self, other):
        if len(self) != len(other):
            return False
        for a, b in zip(self, other):
            if a != b:
                return False
        return True


class _ProfileMerger:
    """Merges per-interval chainage profiles across adjacent date intervals.

    Chainage-adjacent cells with an identical value merge within one
    interval (``add``); adjacent date intervals whose whole-edge profiles
    are identical merge into one date run (``end_interval``).  When
    ``allow_empty`` is set (the diff profile), an empty interval flushes the
    run and starts none, so a run can never cross an empty profile.
    """

    def __init__(self, allow_empty, pack_seg, unpack_seg, emit_row,
                 store_budget, spill_dir, tag, stats, seg_counter,
                 run_counter):
        self._allow_empty = allow_empty
        self._pack_seg = pack_seg
        self._unpack_seg = unpack_seg
        self._emit_row = emit_row

        def _on_spill():
            stats.profile_spills += 1
        self._run = _ProfileStore(store_budget, spill_dir, tag + ".run",
                                  on_spill=_on_spill)
        self._cur = _ProfileStore(store_budget, spill_dir, tag + ".cur",
                                  on_spill=_on_spill)
        self._run_active = False
        self._run_t0 = None
        self._run_t1 = None
        self._pend_value = None
        self._pend_c0 = None
        self._pend_c1 = None
        self._stats = stats
        self._seg_counter = seg_counter
        self._run_counter = run_counter

    def start_interval(self):
        self._cur.reset()
        self._pend_value = None

    def add(self, c0, c1, value):
        if (self._pend_value is not None and self._pend_value == value
                and self._pend_c1 == c0):
            self._pend_c1 = c1
        else:
            self._flush_pending()
            self._pend_value = value
            self._pend_c0 = c0
            self._pend_c1 = c1

    def _flush_pending(self):
        if self._pend_value is not None:
            self._cur.add(self._pack_seg(self._pend_c0, self._pend_c1,
                                         self._pend_value))
            setattr(self._stats, self._seg_counter,
                    getattr(self._stats, self._seg_counter) + 1)
            self._pend_value = None

    def end_interval(self, t0, t1):
        self._flush_pending()
        if self._allow_empty and len(self._cur) == 0:
            self._flush_run()
            return
        if not self._run_active:
            self._run, self._cur = self._cur, self._run
            self._run_active = True
            self._run_t0 = t0
            self._run_t1 = t1
            return
        if self._run.equal_to(self._cur):
            self._run_t1 = t1
        else:
            self._flush_run()
            self._run, self._cur = self._cur, self._run
            self._run_active = True
            self._run_t0 = t0
            self._run_t1 = t1

    def _flush_run(self):
        if not self._run_active:
            return
        setattr(self._stats, self._run_counter,
                getattr(self._stats, self._run_counter) + 1)
        for seg in self._run:
            c0, c1, value = self._unpack_seg(seg)
            self._emit_row(self._run_t0, self._run_t1, c0, c1, value)
        self._run.reset()
        self._run_active = False

    def finish(self):
        self._flush_run()


# ---------------------------------------------------------------- builder

def build_coverage(topology_dir, registry_path, inspection_paths, out_dir,
                   date_from, date_to, cutoff_before, cutoff_after,
                   cache_bytes=DEFAULT_CACHE_BYTES,
                   max_spill_bytes=DEFAULT_SPILL_BYTES,
                   force=False, log=lambda *a: None):
    """Build the two coverage ledgers and their reconciliation diff."""
    t0 = time.monotonic()
    # ---- parameter validation (before touching the filesystem) ----------
    if not (isinstance(cache_bytes, int) and cache_bytes >= 1 << 16):
        raise InspectionError("cache_bytes must be an int >= 65536, got %r"
                              % (cache_bytes,), reason="bad_budget")
    if not (isinstance(max_spill_bytes, int) and max_spill_bytes >= 1 << 16):
        raise InspectionError("max_spill_bytes must be an int >= 65536, got %r"
                              % (max_spill_bytes,), reason="bad_budget")
    from_ord = F.parse_date(date_from, "date_from")
    to_ord = F.parse_date(date_to, "date_to")
    if not (from_ord < to_ord):
        raise InspectionError("report window is empty: [%s, %s)"
                              % (date_from, date_to), reason="bad_window")
    for name, c in (("cutoff_before", cutoff_before),
                    ("cutoff_after", cutoff_after)):
        F.check_int64(c, name)
        if c < 0:
            raise InspectionError("%s must be >= 0, got %r" % (name, c),
                                  reason="bad_cutoff")
    if not (cutoff_before <= cutoff_after):
        raise InspectionError("require cutoff_before <= cutoff_after, got "
                              "%d > %d" % (cutoff_before, cutoff_after),
                              reason="bad_cutoff")
    if isinstance(inspection_paths, str):
        inspection_paths = [inspection_paths]
    inspection_paths = list(inspection_paths)
    if not inspection_paths:
        raise InspectionError("at least one inspection file is required",
                              reason="no_input")

    stats = InspStats()
    failed_dir = out_dir + ".failed"
    backup_dir = out_dir + ".old-%d" % os.getpid()
    build_dir = out_dir + ".build-%d" % os.getpid()
    if os.path.exists(out_dir):
        if not force:
            raise InspectionError(
                "output directory exists: %s (use force to replace)" % out_dir,
                reason="output_exists")
    if os.path.exists(out_dir):
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        os.replace(out_dir, backup_dir)
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)
    tmpdir = os.path.join(build_dir, "tmp")
    os.makedirs(tmpdir)
    evidence_path = os.path.join(build_dir, "insp_rejects.jsonl")
    sort_chunk = max(1 << 16, cache_bytes // 2)
    heap_items = max(64, cache_bytes // 1024)
    store_budget = max(1 << 12, cache_bytes // 8)
    topo = None
    failed_already = False
    open_heaps = []

    def write_evidence(rows):
        with open(evidence_path, "w", encoding="utf-8") as fh:
            for ev in rows:
                fh.write(json.dumps(ev, ensure_ascii=False,
                                    sort_keys=True) + "\n")

    def fail(message, reason=None, evidence=None, exc=None):
        nonlocal failed_already
        if failed_already:
            raise InspectionError(message, reason=reason)
        failed_already = True
        for h in open_heaps:
            try:
                h.close()
            except Exception:
                pass
        payload = {"status": "failed", "reason": reason or "build_error",
                   "detail": message,
                   "topology": os.path.abspath(topology_dir),
                   "finished_utc": datetime.now(timezone.utc).isoformat(),
                   "counters": stats.as_dict()}
        if evidence:
            evs = evidence if isinstance(evidence, list) else [evidence]
            write_evidence(evs)
            payload["evidence_file"] = "insp_rejects.jsonl"
        with open(os.path.join(build_dir, F.REPORT_NAME), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        if os.path.exists(failed_dir):
            shutil.rmtree(failed_dir)
        try:
            os.replace(build_dir, failed_dir)
        except OSError:
            shutil.rmtree(build_dir, ignore_errors=True)
        if os.path.exists(backup_dir) and not os.path.exists(out_dir):
            os.replace(backup_dir, out_dir)
        ev = os.path.join(failed_dir, "insp_rejects.jsonl") if evidence else None
        if isinstance(exc, KeyboardInterrupt):
            raise
        if isinstance(exc, InspectionSpillBudgetExceeded):
            raise InspectionSpillBudgetExceeded(
                "%s (evidence: %s)" % (message, failed_dir),
                evidence_path=ev, reason=reason)
        raise InspectionError("%s (evidence: %s)" % (message, failed_dir),
                              evidence_path=ev, reason=reason)

    def check_budget():
        used = _dir_bytes(tmpdir)
        if used > stats.spill_bytes_peak:
            stats.spill_bytes_peak = used
        if used > max_spill_bytes:
            raise InspectionSpillBudgetExceeded(
                "spill budget %d bytes exhausted at %d bytes; no partial "
                "monthly report is published" % (max_spill_bytes, used),
                reason="spill_budget_exceeded")

    def on_flush(_path, _n):
        stats.spill_runs += 1
        check_budget()

    def track_heap(heap):
        open_heaps.append(heap)
        return heap

    try:
        # -------------------------------------- phase 0: open topology
        log("phase 0: open topology read-only")
        _maybe_fault("open")
        try:
            topo = Topology(topology_dir)
        except (NotACatalogError, CorruptionError) as exc:
            fail("cannot open topology: %s" % exc,
                 reason="topology_open_failed",
                 evidence={"reason": "topology_open_failed",
                           "detail": str(exc)}, exc=exc)
        identity = F.topology_identity(topology_dir, topo.manifest)

        # ----------------------- phase 1: registry join -> managed roster
        log("phase 1: parse registry, validate against topology edges")
        _maybe_fault("registry")
        registry_path_abs = os.path.abspath(registry_path)
        reg_raw = os.path.join(tmpdir, "registry.raw")
        stats.registry_lines = 0
        try:
            with open(registry_path, "r", encoding="utf-8") as fh:
                with open(reg_raw, "wb") as out:
                    for line_no, line in enumerate(fh, 1):
                        line = line.strip()
                        if not line:
                            continue
                        stats.registry_lines += 1
                        where = "%s:%d" % (registry_path, line_no)
                        obj = _json_loads_strict(line, where)
                        source_id = _req(obj, "source_id", where)
                        F.check_text_id(source_id, "source_id")
                        part_index = _req(obj, "part_index", where)
                        F.check_int64(part_index, "part_index")
                        if part_index < 0:
                            raise InspectionError(
                                "registry part_index must be >= 0 at %s"
                                % where, reason="bad_part_index")
                        chainage_end = _req(obj, "chainage_end", where)
                        F.check_int64(chainage_end, "chainage_end")
                        if chainage_end <= 0:
                            raise InspectionError(
                                "registry chainage_end must be a positive "
                                "integer at %s, got %r"
                                % (where, chainage_end), reason="bad_chainage")
                        rec = F.pack_managed(source_id, part_index,
                                             chainage_end)
                        _write_lp(out, rec)
                        stats.tmp_bytes_written += _LEN.size + len(rec)
        except InspectionError as exc:
            fail("registry error: %s" % exc, reason=exc.reason or "bad_registry",
                 evidence={"reason": exc.reason or "bad_registry",
                           "detail": str(exc)}, exc=exc)
        except OSError as exc:
            fail("cannot read registry: %s" % exc, reason="registry_unreadable",
                 evidence={"reason": "registry_unreadable",
                           "detail": str(exc)}, exc=exc)
        reg_len, reg_sha = F.sha256_file(registry_path)

        reg_sorted = external_sort(
            _read_lp(reg_raw), key_fn=F.managed_key,
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="registry",
            on_flush=on_flush)
        # duplicate registration detection on the sorted stream
        managed_sorted = os.path.join(tmpdir, "managed.bin")
        n_managed = 0
        with open(managed_sorted, "wb") as mfh:
            prev_key = None
            prev_rec = None
            for rec in reg_sorted:
                key = F.managed_key(rec)
                if key == prev_key:
                    sid, part, _l = F.unpack_managed(rec)
                    psid, ppart, _pl = F.unpack_managed(prev_rec)
                    fail("duplicate registry entry for edge (%r, %d)"
                         % (sid, part),
                         reason="duplicate_registry",
                         evidence={"reason": "duplicate_registry",
                                   "source_id": sid, "part_index": part,
                                   "detail": "edge (%r, %d) registered twice"
                                             % (sid, part)})
                prev_key = key
                prev_rec = rec
                _write_lp(mfh, rec)
                n_managed += 1
        stats.managed_edges = n_managed
        check_budget()

        # merge-join managed roster against the topology edge table
        _maybe_fault("topo_join")
        managed_final = os.path.join(tmpdir, "managed.final")
        managed_tsv = os.path.join(build_dir, F.MANAGED_NAME)
        unknown = []
        zero_length = []
        with open(managed_final, "wb") as ofh, \
                open(managed_tsv, "wb") as tsfh:
            tsfh.write(F.MANAGED_HEADER_BYTES)
            topo_iter = topo.iter_edges()
            topo_peek = _Peek(topo_iter)
            for rec in _read_lp(managed_sorted):
                sid, part, chainage_end = F.unpack_managed(rec)
                key = (sid, part)
                # advance topology cursor to this edge
                while not topo_peek.empty():
                    row = topo_peek.peek()
                    tkey = (row[1], row[2])
                    if tkey < key:
                        topo_peek.next()
                    else:
                        break
                if topo_peek.empty() or (topo_peek.peek()[1],
                                         topo_peek.peek()[2]) != key:
                    unknown.append({"reason": "unknown_edge",
                                    "source_id": sid, "part_index": part,
                                    "detail": "registry edge not present in "
                                              "the topology edge table"})
                    continue
                stats.topology_edges_scanned += 1
                planar_length = topo_peek.peek()[6]
                if planar_length == 0.0:
                    zero_length.append({"reason": "zero_length_edge",
                                        "source_id": sid,
                                        "part_index": part,
                                        "detail": "registry edge has "
                                                  "zero-length geometry"})
                    continue
                _write_lp(ofh, rec)
                tsfh.write(F.encode_managed_line(sid, part, chainage_end))
        if unknown or zero_length:
            ev = unknown + zero_length
            fail("registry references rejected: %d unknown, %d zero-length"
                 % (len(unknown), len(zero_length)),
                 reason="registry_reference_rejected", evidence=ev)
        stats.managed_edges = n_managed
        managed_tsv_len, managed_tsv_sha = F.sha256_file(managed_tsv)
        check_budget()

        # ---------------- phase 2: parse + sort revisions, dedupe to winners
        log("phase 2: parse inspection files, sort revisions")
        _maybe_fault("revisions")
        rev_raw = os.path.join(tmpdir, "revisions.raw")
        inspection_meta = []
        with open(rev_raw, "wb") as out:
            for file_index, path in enumerate(inspection_paths):
                n_lines = 0
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        for line_no, line in enumerate(fh, 1):
                            line = line.strip()
                            if not line:
                                continue
                            n_lines += 1
                            stats.revision_lines += 1
                            where = "%s:%d" % (path, line_no)
                            obj = _json_loads_strict(line, where)
                            iid, batch, canon = _norm_revision(obj, where)
                            rec = F.pack_revision(iid, batch, file_index,
                                                  line_no, canon)
                            _write_lp(out, rec)
                            stats.tmp_bytes_written += _LEN.size + len(rec)
                except InspectionError as exc:
                    fail("inspection input error: %s" % exc,
                         reason=exc.reason or "bad_revision",
                         evidence={"reason": exc.reason or "bad_revision",
                                   "file": os.path.abspath(path),
                                   "detail": str(exc)}, exc=exc)
                except OSError as exc:
                    fail("cannot read inspection file %s: %s" % (path, exc),
                         reason="inspection_unreadable",
                         evidence={"reason": "inspection_unreadable",
                                   "file": os.path.abspath(path),
                                   "detail": str(exc)}, exc=exc)
                flen, fsha = F.sha256_file(path)
                inspection_meta.append({
                    "path": os.path.abspath(path), "length": flen,
                    "sha256": fsha, "revision_lines": n_lines})
        stats.revision_files = len(inspection_paths)
        check_budget()

        rev_sorted = external_sort(
            _read_lp(rev_raw), key_fn=F.revision_key,
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="revisions",
            on_flush=on_flush)

        # dedupe identical deliveries, fail on conflicts, select winners
        log("phase 3: select visible revisions at both cutoffs")
        _maybe_fault("select")
        obs_raw = os.path.join(tmpdir, "observations.raw")
        conflicts = []
        n_conflict_records = 0
        n_conflict_ids = 0
        with open(obs_raw, "wb") as out:
            # Stream the (id, batch)-sorted revisions one sub-group at a
            # time: only the current (id, batch) delivery set is resident,
            # never a whole id's revision history.
            cur_id = None
            best_before = None
            best_after = None
            id_conflicted = False
            sub_batch = None
            sub_records = []        # [(file_index, line_no, content)]

            def flush_subgroup():
                nonlocal sub_batch, sub_records, best_before, best_after
                nonlocal id_conflicted, n_conflict_records
                if sub_batch is None:
                    return
                contents = {c for _f, _l, c in sub_records}
                if len(contents) > 1:
                    n_conflict_records += len(sub_records)
                    # evidence is capped in memory; the count stays exact
                    for fi, ln, _c in sub_records[:1024]:
                        if len(conflicts) < 1024:
                            conflicts.append({
                                "reason": "revision_conflict",
                                "inspection_id": cur_id, "batch": sub_batch,
                                "file": os.path.abspath(inspection_paths[fi]),
                                "line": ln})
                    id_conflicted = True
                else:
                    stats.duplicate_deliveries += len(sub_records) - 1
                    content = sub_records[0][2]
                    if sub_batch <= cutoff_after:
                        best_after = (sub_batch, content)
                        if sub_batch <= cutoff_before:
                            best_before = (sub_batch, content)
                sub_batch = None
                sub_records = []

            def flush_id():
                nonlocal best_before, best_after, id_conflicted, n_conflict_ids
                if cur_id is None:
                    return
                if id_conflicted:
                    n_conflict_ids += 1
                else:
                    _emit_visible(cur_id, best_before, best_after,
                                  from_ord, to_ord, out, stats)
                best_before = None
                best_after = None
                id_conflicted = False

            for rec in rev_sorted:
                iid, batch, fi, ln, content = F.unpack_revision(rec)
                if iid != cur_id:
                    flush_subgroup()
                    flush_id()
                    cur_id = iid
                if batch != sub_batch:
                    flush_subgroup()
                    sub_batch = batch
                sub_records.append((fi, ln, content))
            flush_subgroup()
            flush_id()
        if conflicts:
            ev = conflicts[:256]
            fail("conflicting revisions for the same (inspection_id, batch): "
                 "%d record(s) across %d id(s)"
                 % (n_conflict_records, n_conflict_ids),
                 reason="revision_conflict", evidence=ev)
        check_budget()

        # ---- phase 3b: sort visible observations, join with roster, map
        log("phase 3b: sort observations, join roster, map chainage")
        _maybe_fault("map")
        obs_sorted = external_sort(
            _read_lp(obs_raw), key_fn=F.observation_edge_d0_key,
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="obs",
            on_flush=on_flush)
        mapped_raw = os.path.join(tmpdir, "mapped.raw")
        datepoints_raw = os.path.join(tmpdir, "datepoints.raw")
        unmanaged = []
        with open(mapped_raw, "wb") as mfh, \
                open(datepoints_raw, "wb") as dfh:
            managed_peek = _Peek(_read_lp(managed_final))
            for rec in obs_sorted:
                (sid, iid, part, side, batch, priority, checked_on, d0, d1,
                 direction, lo, hi, result) = F.unpack_observation(rec)
                key = (sid, part)
                while not managed_peek.empty():
                    msid, mpart, ml = F.unpack_managed(managed_peek.peek())
                    if (msid, mpart) < key:
                        managed_peek.next()
                    else:
                        break
                if managed_peek.empty():
                    unmanaged.append({"reason": "unmanaged_edge",
                                      "source_id": sid, "part_index": part,
                                      "inspection_id": iid,
                                      "detail": "observation references an "
                                                "edge that is not managed"})
                    continue
                msid, mpart, chainage_end = F.unpack_managed(
                    managed_peek.peek())
                if (msid, mpart) != key:
                    unmanaged.append({"reason": "unmanaged_edge",
                                      "source_id": sid, "part_index": part,
                                      "inspection_id": iid,
                                      "detail": "observation references an "
                                                "edge that is not managed"})
                    continue
                if hi > chainage_end:
                    fail("observation %r interval [%d, %d) exceeds managed "
                         "chainage end %d of edge (%r, %d)"
                         % (iid, lo, hi, chainage_end, sid, part),
                         reason="interval_out_of_range",
                         evidence={"reason": "interval_out_of_range",
                                   "inspection_id": iid, "source_id": sid,
                                   "part_index": part, "lo": lo, "hi": hi,
                                   "chainage_end": chainage_end})
                if direction == F.DIRECTION_TO_CODE[F.DIR_REVERSE]:
                    c0, c1 = chainage_end - hi, chainage_end - lo
                else:
                    c0, c1 = lo, hi
                mrec = F.pack_mapped(sid, iid, part, c0, c1, d0, d1, side,
                                     priority, checked_on, batch, result)
                _write_lp(mfh, mrec)
                stats.tmp_bytes_written += _LEN.size + len(mrec)
                _write_lp(dfh, F.pack_datepoint(sid, part, d0))
                _write_lp(dfh, F.pack_datepoint(sid, part, d1))
                stats.tmp_bytes_written += 2 * (
                    _LEN.size + len(F.pack_datepoint(sid, part, d0)))
        if unmanaged:
            fail("observations reference unmanaged edges: %d"
                 % len(unmanaged), reason="unmanaged_edge",
                 evidence=unmanaged[:256])
        # window bounds for every managed edge (even observation-less ones)
        with open(datepoints_raw, "ab") as dfh:
            for rec in _read_lp(managed_final):
                sid, part, _l = F.unpack_managed(rec)
                _write_lp(dfh, F.pack_datepoint(sid, part, from_ord))
                _write_lp(dfh, F.pack_datepoint(sid, part, to_ord))
        check_budget()

        datepoints_sorted = external_sort(
            _read_lp(datepoints_raw), key_fn=F.datepoint_key,
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="dates",
            on_flush=on_flush)
        # dedupe date endpoints
        datepoints_final = os.path.join(tmpdir, "datepoints.bin")
        n_datepoints = 0
        with open(datepoints_final, "wb") as dfh:
            prev = None
            for rec in datepoints_sorted:
                key = F.datepoint_key(rec)
                if key == prev:
                    continue
                prev = key
                _write_lp(dfh, rec)
                n_datepoints += 1
        check_budget()

        # ------------------------- phase 4: the fused coverage/diff sweep
        log("phase 4: fused date x chainage sweep per managed edge")
        _maybe_fault("sweep")
        cov_before_tsv = os.path.join(build_dir, F.COVERAGE_BEFORE_NAME)
        cov_after_tsv = os.path.join(build_dir, F.COVERAGE_AFTER_NAME)
        diff_tsv = os.path.join(build_dir, F.DIFF_NAME)
        summary = {"status_changes": {}, "evidence_only": {}}
        with open(cov_before_tsv, "wb") as fb, \
                open(cov_after_tsv, "wb") as fa, \
                open(diff_tsv, "wb") as fd:
            fb.write(F.COVERAGE_HEADER_BYTES)
            fa.write(F.COVERAGE_HEADER_BYTES)
            fd.write(F.DIFF_HEADER_BYTES)
            _run_sweep(managed_final, datepoints_final, mapped_raw,
                       fb, fa, fd, summary, stats,
                       heap_items, store_budget, tmpdir, check_budget)
            for fh in (fb, fa, fd):
                fh.flush()
                os.fsync(fh.fileno())
        check_budget()

        # ------------------------- phase 5: summary, report, manifest
        log("phase 5: summary, checksums, manifest commit")
        _maybe_fault("commit")
        status_changes = summary["status_changes"]
        evidence_only = summary["evidence_only"]
        status_total = sum(status_changes.values())
        evidence_total = sum(evidence_only.values())
        summary_doc = {
            "report_window": {"from": date_from, "to": date_to},
            "cutoffs": {"before": cutoff_before, "after": cutoff_after},
            "unit": "chainage_unit_days",
            "status_changes": dict(sorted(status_changes.items())),
            "status_change_total": status_total,
            "evidence_only_changes": dict(sorted(evidence_only.items())),
            "evidence_only_total": evidence_total,
            "changed_units_days_total": status_total + evidence_total,
        }
        summary_path = os.path.join(build_dir, F.SUMMARY_NAME)
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary_doc, fh, indent=2, sort_keys=True,
                      ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        cov_b_len, cov_b_sha = F.sha256_file(cov_before_tsv)
        cov_a_len, cov_a_sha = F.sha256_file(cov_after_tsv)
        diff_len, diff_sha = F.sha256_file(diff_tsv)
        summ_len, summ_sha = F.sha256_file(summary_path)
        stats.spill_bytes_peak = max(stats.spill_bytes_peak,
                                     _dir_bytes(tmpdir))
        stats.peak_rss_bytes = max(_rss_bytes(), stats.peak_rss_bytes)
        report = {
            "status": "ok",
            **stats.as_dict(),
            "cache_bytes": cache_bytes,
            "max_spill_bytes": max_spill_bytes,
            "managed_tsv_sha256": managed_tsv_sha,
            "coverage_before_sha256": cov_b_sha,
            "coverage_after_sha256": cov_a_sha,
            "diff_sha256": diff_sha,
            "summary_sha256": summ_sha,
            "duration_s": round(time.monotonic() - t0, 3),
        }
        report_path = os.path.join(build_dir, F.REPORT_NAME)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        report_len, report_sha = F.sha256_file(report_path)
        manifest = {
            "format": F.INSP_FORMAT_NAME,
            "version": F.INSP_FORMAT_VERSION,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "topology": identity,
            "registry": {"path": registry_path_abs, "length": reg_len,
                         "sha256": reg_sha, "lines": stats.registry_lines},
            "inspection_files": inspection_meta,
            "report_window": {"from": date_from, "to": date_to,
                              "from_ordinal": from_ord,
                              "to_ordinal": to_ord},
            "cutoffs": {"before": cutoff_before, "after": cutoff_after},
            "counts": {
                "managed_edges": stats.managed_edges,
                "revision_lines": stats.revision_lines,
                "duplicate_deliveries": stats.duplicate_deliveries,
                "visible_observations": stats.visible_observations,
                "coverage_rows_before": stats.coverage_rows_before,
                "coverage_rows_after": stats.coverage_rows_after,
                "diff_rows": stats.diff_rows,
            },
            "files": {
                F.MANAGED_NAME: {"length": managed_tsv_len,
                                 "sha256": managed_tsv_sha},
                F.COVERAGE_BEFORE_NAME: {"length": cov_b_len,
                                         "sha256": cov_b_sha},
                F.COVERAGE_AFTER_NAME: {"length": cov_a_len,
                                        "sha256": cov_a_sha},
                F.DIFF_NAME: {"length": diff_len, "sha256": diff_sha},
                F.SUMMARY_NAME: {"length": summ_len, "sha256": summ_sha},
                F.REPORT_NAME: {"length": report_len, "sha256": report_sha},
            },
            "ordering": "edges by (source_id, part_index) Unicode code "
                        "point order; within an edge by date start then "
                        "chainage start",
            "cache_bytes": cache_bytes,
            "max_spill_bytes": max_spill_bytes,
        }
        shutil.rmtree(tmpdir, ignore_errors=True)
        F.dump_manifest(os.path.join(build_dir, F.MANIFEST_NAME), manifest)
        dir_fd = os.open(build_dir, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        os.replace(build_dir, out_dir)
        log("done: %d managed edges, %d/%d coverage rows, %d diff rows"
            % (stats.managed_edges, stats.coverage_rows_before,
               stats.coverage_rows_after, stats.diff_rows))
        return report

    except KeyboardInterrupt as exc:
        fail("reconciliation aborted by consumer", reason="aborted",
             evidence={"reason": "aborted", "counters": stats.as_dict()},
             exc=exc)
    except InspectionSpillBudgetExceeded as exc:
        fail("on-disk spill budget exhausted; build aborted without "
             "publishing", reason="spill_budget_exceeded",
             evidence={"reason": "spill_budget_exceeded",
                       "spill_bytes_peak": stats.spill_bytes_peak,
                       "max_spill_bytes": max_spill_bytes}, exc=exc)
    except InspectionError as exc:
        if failed_already:
            raise
        fail("build error: %s" % exc, reason=exc.reason or "build_error",
             evidence={"reason": exc.reason or "build_error",
                       "detail": str(exc)})
    except CorruptionError as exc:
        fail("%s: %s" % (type(exc).__name__, exc), reason="corruption",
             evidence={"reason": "corruption", "detail": str(exc)})
    except OSError as exc:
        fail("I/O error: %s" % exc, reason="io_error",
             evidence={"reason": "io_error", "detail": str(exc)})
    finally:
        if topo is not None:
            topo.close()
        for h in open_heaps:
            try:
                h.close()
            except Exception:
                pass
        shutil.rmtree(build_dir, ignore_errors=True)


# ----------------------------------------------------- winner emission

def _emit_visible(iid, best_before, best_after, from_ord, to_ord, out,
                  stats):
    """Emit the visible observation record(s) for one inspection id.

    The same revision winning both cutoffs becomes one SIDE_BOTH record;
    different winners become one record per side.  Retracts and revisions
    clipped outside the report window emit nothing (no fallback to older
    versions)."""
    if (best_before is not None and best_after is not None
            and best_before[0] == best_after[0]):
        _emit_one(iid, best_before[0], best_before[1], F.SIDE_BOTH,
                  from_ord, to_ord, out, stats)
        return
    if best_before is not None:
        _emit_one(iid, best_before[0], best_before[1], F.SIDE_BEFORE,
                  from_ord, to_ord, out, stats)
    if best_after is not None:
        _emit_one(iid, best_after[0], best_after[1], F.SIDE_AFTER,
                  from_ord, to_ord, out, stats)


def _emit_one(iid, batch, content_bytes, side, from_ord, to_ord, out, stats):
    content = json.loads(content_bytes.decode("utf-8"))
    if content["action"] != F.ACTION_UPSERT:
        stats.revisions_retract += 1
        return
    stats.revisions_upsert += 1
    d0 = max(content["start"], from_ord)
    d1 = min(content["end"], to_ord)
    if d0 >= d1:
        stats.observations_clipped_empty += 1
        return
    stats.visible_observations += 1
    if side & F.SIDE_BEFORE:
        stats.visible_before += 1
    if side & F.SIDE_AFTER:
        stats.visible_after += 1
    rec = F.pack_observation(
        content["source_id"], iid, content["part_index"], side, batch,
        content["priority"], content["checked_on"], d0, d1,
        F.DIRECTION_TO_CODE[content["direction"]], content["lo"],
        content["hi"], F.RESULT_TO_CODE[content["result"]])
    _write_lp(out, rec)
    stats.tmp_bytes_written += _LEN.size + len(rec)


# ------------------------------------------------------------- the sweep

def _run_sweep(managed_path, datepoints_path, mapped_path, fb, fa, fd,
               summary, stats, heap_items, store_budget, tmpdir,
               check_budget):
    """Merge-join the managed roster, per-edge date endpoints and mapped
    observations (all edge-sorted) and run the fused sweep edge by edge."""
    dp_groups = _EdgeGrouper(_read_lp(datepoints_path), F.datepoint_edge_key)
    obs_groups = _EdgeGrouper(_read_lp(mapped_path), F.mapped_edge_key)
    managed_peek = _Peek(_read_lp(managed_path))
    heaps = []
    guard = 0
    try:
        while True:
            dp_key = dp_groups.peek_key()
            if dp_key is None:
                break
            _k, dates_sub = dp_groups.take_group()
            if managed_peek.empty():
                raise InspectionError(
                    "datepoint stream has an edge with no managed roster "
                    "entry", reason="invariant_managed")
            mrec = managed_peek.peek()
            if F.managed_key(mrec) != dp_key:
                raise InspectionError(
                    "managed roster and datepoint stream misaligned",
                    reason="invariant_managed")
            managed_peek.next()
            sid, part, chainage_end = F.unpack_managed(mrec)
            obs_sub = None
            okey = obs_groups.peek_key()
            if okey is not None:
                if okey == dp_key:
                    obs_sub = obs_groups.take_group()[1]
                elif okey < dp_key:
                    raise InspectionError(
                        "observation stream has an edge with no managed "
                        "roster entry", reason="invariant_obs")
            _sweep_edge(sid, part, chainage_end, dates_sub, obs_sub,
                        fb, fa, fd, summary, stats, heap_items,
                        store_budget, tmpdir, heaps)
            guard += 1
            if guard >= _GUARD_EVERY:
                guard = 0
                check_budget()
        if not managed_peek.empty():
            raise InspectionError("managed roster has edges with no date "
                                  "endpoints", reason="invariant_managed")
        if obs_groups.peek_key() is not None:
            raise InspectionError("observation stream has edges with no "
                                  "managed roster entry",
                                  reason="invariant_obs")
    finally:
        for h in heaps:
            try:
                h.close()
            except Exception:
                pass


def _sweep_edge(sid, part, chainage_end, dates_iter, obs_iter, fb, fa, fd,
                summary, stats, heap_items, store_budget, tmpdir, heaps):
    """Run the fused date x chainage sweep for one managed edge."""
    L = chainage_end

    def emit_before(t0, t1, c0, c1, value):
        status, iid, batch = value
        fb.write(F.encode_coverage_line(sid, part, t0, t1, c0, c1,
                                        F.CODE_TO_STATUS[status], iid, batch))
        stats.coverage_rows_before += 1

    def emit_after(t0, t1, c0, c1, value):
        status, iid, batch = value
        fa.write(F.encode_coverage_line(sid, part, t0, t1, c0, c1,
                                        F.CODE_TO_STATUS[status], iid, batch))
        stats.coverage_rows_after += 1

    def emit_diff(t0, t1, c0, c1, value):
        (b_status, b_iid, b_batch), (a_status, a_iid, a_batch) = value
        fd.write(F.encode_diff_line(
            sid, part, t0, t1, c0, c1,
            F.CODE_TO_STATUS[b_status], b_iid, b_batch,
            F.CODE_TO_STATUS[a_status], a_iid, a_batch))
        stats.diff_rows += 1
        area = (c1 - c0) * (t1 - t0)
        if b_status != a_status:
            key = "%s>%s" % (F.CODE_TO_STATUS[b_status],
                             F.CODE_TO_STATUS[a_status])
            summary["status_changes"][key] = \
                summary["status_changes"].get(key, 0) + area
        else:
            key = "%s>%s" % (F.CODE_TO_STATUS[b_status],
                             F.CODE_TO_STATUS[a_status])
            summary["evidence_only"][key] = \
                summary["evidence_only"].get(key, 0) + area

    cov_b = _ProfileMerger(False, _pack_cov_seg, _unpack_cov_seg,
                           emit_before, store_budget, tmpdir, "cb",
                           stats, "cov_before_segments", "date_runs_before")
    cov_a = _ProfileMerger(False, _pack_cov_seg, _unpack_cov_seg,
                           emit_after, store_budget, tmpdir, "ca",
                           stats, "cov_after_segments", "date_runs_after")
    diff_m = _ProfileMerger(True, _pack_diff_seg, _unpack_diff_seg,
                            emit_diff, store_budget, tmpdir, "df",
                            stats, "diff_segments", "date_runs_diff")

    obs_peek = _Peek(obs_iter) if obs_iter is not None else _Peek(iter(()))
    dates = _Peek(F.datepoint_date(rec) for rec in dates_iter)
    date_active = SpillableHeap(F.mapped_chainage_key,
                                max_items=heap_items, spill_dir=tmpdir,
                                tag="da")
    heaps.append(date_active)
    if dates.empty():
        # defensive: every managed edge has at least [from, to)
        cov_b.finish()
        cov_a.finish()
        diff_m.finish()
        return
    t0 = dates.next()
    while not dates.empty():
        t1 = dates.next()
        stats.elementary_date_intervals += 1
        # admit observations starting at or before t0 (d0-sorted stream)
        while not obs_peek.empty() and F.mapped_d0(obs_peek.peek()) <= t0:
            date_active.push(obs_peek.next())
        next_active = SpillableHeap(F.mapped_chainage_key,
                                    max_items=heap_items, spill_dir=tmpdir,
                                    tag="na")
        winner_before = SpillableHeap(F.mapped_winner_key,
                                      max_items=heap_items, spill_dir=tmpdir,
                                      tag="wb")
        winner_after = SpillableHeap(F.mapped_winner_key,
                                     max_items=heap_items, spill_dir=tmpdir,
                                     tag="wa")
        heaps.extend((next_active, winner_before, winner_after))
        cov_b.start_interval()
        cov_a.start_interval()
        diff_m.start_interval()
        _sweep_interval(date_active, next_active, winner_before,
                        winner_after, L, t1, cov_b, cov_a, diff_m, stats)
        cov_b.end_interval(t0, t1)
        cov_a.end_interval(t0, t1)
        diff_m.end_interval(t0, t1)
        _close_heap(winner_before, stats, heaps)
        _close_heap(winner_after, stats, heaps)
        _close_heap(date_active, stats, heaps)
        date_active = next_active
        t0 = t1
    cov_b.finish()
    cov_a.finish()
    diff_m.finish()
    _close_heap(date_active, stats, heaps)


def _close_heap(heap, stats, heaps):
    """Accumulate a heap's measured spill/high-water into stats, close it,
    and drop it from the live-heap tracking list."""
    stats.heap_spills += heap.spills
    if heap.high_water > stats.heap_high_water:
        stats.heap_high_water = heap.high_water
    heap.close()
    if heap in heaps:
        heaps.remove(heap)


def _sweep_interval(date_active, next_active, winner_before, winner_after,
                    L, t1, cov_b, cov_a, diff_m, stats):
    """One elementary date interval: chainage envelope for both cutoffs.

    Pops the active observations in chainage-start order, carries the still
    date-alive ones into ``next_active``, and emits one cell per elementary
    chainage segment to the three profile mergers."""
    p = 0
    while p < L:
        # insert observations whose chainage starts at or before p
        while not date_active.empty():
            rec = date_active.peek()
            if F.mapped_c0(rec) > p:
                break
            date_active.pop()
            stats.observation_activations += 1
            if F.mapped_d1(rec) > t1:
                next_active.push(rec)
            if F.mapped_c1(rec) > p:
                side = F.mapped_side(rec)
                if side & F.SIDE_BEFORE:
                    winner_before.push(rec)
                if side & F.SIDE_AFTER:
                    winner_after.push(rec)
        # lazily discard chainage-expired winners
        while not winner_before.empty() and \
                F.mapped_c1(winner_before.peek()) <= p:
            winner_before.pop()
        while not winner_after.empty() and \
                F.mapped_c1(winner_after.peek()) <= p:
            winner_after.pop()
        # current winners -> cell values
        if winner_before.empty():
            bval = _GAP_VAL
        else:
            rec = winner_before.peek()
            bval = (F.mapped_result(rec), F.mapped_iid(rec),
                    F.mapped_batch(rec))
        if winner_after.empty():
            aval = _GAP_VAL
        else:
            rec = winner_after.peek()
            aval = (F.mapped_result(rec), F.mapped_iid(rec),
                    F.mapped_batch(rec))
        # next chainage event point
        nxt = L
        if not date_active.empty():
            c0 = F.mapped_c0(date_active.peek())
            if c0 < nxt:
                nxt = c0
        if not winner_before.empty():
            c1 = F.mapped_c1(winner_before.peek())
            if c1 < nxt:
                nxt = c1
        if not winner_after.empty():
            c1 = F.mapped_c1(winner_after.peek())
            if c1 < nxt:
                nxt = c1
        cov_b.add(p, nxt, bval)
        cov_a.add(p, nxt, aval)
        if bval != aval:
            diff_m.add(p, nxt, (bval, aval))
        p = nxt
