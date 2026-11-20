"""Spillable data structures: external merge sort and a spillable priority
queue.  Both honour an explicit byte/item budget and overflow to disk,
so neither build nor query can grow without bound on adversarial input
(e.g. maximally overlapping geometries)."""

import heapq
import os
import shutil
import struct
import tempfile

_LEN = struct.Struct(">I")
_PQ_HEAD = struct.Struct(">dI")  # lower-bound f64 + payload length u32


# ---------------------------------------------------------- external sort

def _write_run(records, path):
    with open(path, "wb") as fh:
        for rec in records:
            fh.write(_LEN.pack(len(rec)))
            fh.write(rec)


def _read_run(path):
    """Iterate the length-prefixed records of a run file."""
    with open(path, "rb") as fh:
        while True:
            head = fh.read(_LEN.size)
            if not head:
                return
            (n,) = _LEN.unpack(head)
            rec = fh.read(n)
            if len(rec) != n:
                raise IOError("run file %s truncated" % path)
            yield rec


def _merge_run_iterators(run_paths, key_fn):
    """k-way merge of sorted run files; deletes files as they are consumed."""
    iters = []
    for path in run_paths:
        iters.append((path, _read_run(path)))
    heap = []
    for idx, (path, it) in enumerate(iters):
        try:
            rec = next(it)
        except StopIteration:
            _quiet_unlink(path)
            continue
        heap.append((key_fn(rec), idx, rec, it, path))
    heapq.heapify(heap)
    while heap:
        _, idx, rec, it, path = heapq.heappop(heap)
        yield rec
        try:
            nxt = next(it)
        except StopIteration:
            _quiet_unlink(path)
            continue
        heapq.heappush(heap, (key_fn(nxt), idx, nxt, it, path))


def _quiet_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def external_sort(records, key_fn, chunk_bytes, tmpdir, tag="sort", fanin=32,
                  on_flush=None):
    """Sort an iterable of byte records without holding them all in memory.

    records:     iterable of bytes
    key_fn:      bytes -> comparable key
    chunk_bytes: in-memory run budget; exceeded -> spill a sorted run
    on_flush:    optional callback(path, n_records) invoked every time a
                 run is materialised on disk (diagnostics use it to count
                 real spills instead of guessing)
    returns:     iterator over records in key order
    """
    os.makedirs(tmpdir, exist_ok=True)
    runs = []
    buf = []
    buf_bytes = 0
    n_run = 0

    def flush():
        nonlocal buf, buf_bytes, n_run
        if not buf:
            return
        buf.sort(key=key_fn)
        path = os.path.join(tmpdir, "%s-run-%05d.tmp" % (tag, n_run))
        n_run += 1
        _write_run(buf, path)
        runs.append(path)
        if on_flush is not None:
            on_flush(path, len(buf))
        buf = []
        buf_bytes = 0

    for rec in records:
        buf.append(rec)
        buf_bytes += len(rec)
        if buf_bytes >= chunk_bytes:
            flush()
    flush()

    if not runs:
        return iter(())
    if len(runs) == 1:
        return _merge_run_iterators(runs, key_fn)

    # Merge in passes so the number of simultaneously open files stays bounded.
    while len(runs) > fanin:
        nxt_runs = []
        for i in range(0, len(runs), fanin):
            group = runs[i:i + fanin]
            path = os.path.join(tmpdir, "%s-merge-%05d.tmp" % (tag, len(runs) + i))
            with open(path, "wb") as fh:
                for rec in _merge_run_iterators(group, key_fn):
                    fh.write(_LEN.pack(len(rec)))
                    fh.write(rec)
            nxt_runs.append(path)
        runs = nxt_runs
    return _merge_run_iterators(runs, key_fn)


# --------------------------------------------------------- spillable PQ

class _RunReader:
    __slots__ = ("path", "fh", "lower", "data", "exhausted")

    def __init__(self, path):
        self.path = path
        self.fh = open(path, "rb")
        self.exhausted = False
        self.lower = None
        self.data = None
        self._advance()

    def _advance(self):
        head = self.fh.read(_PQ_HEAD.size)
        if len(head) < _PQ_HEAD.size:
            self.exhausted = True
            self.lower = None
            self.data = None
            return
        lower, n = _PQ_HEAD.unpack(head)
        data = self.fh.read(n)
        if len(data) != n:
            raise IOError("spill run %s truncated" % self.path)
        self.lower = lower
        self.data = data

    def take(self):
        item = (self.lower, self.data)
        self._advance()
        return item

    def close(self):
        try:
            self.fh.close()
        finally:
            try:
                os.unlink(self.path)
            except OSError:
                pass


class SpillablePQ:
    """Min-priority queue keyed by a float lower bound.

    Up to ``max_items`` entries stay in an in-memory heap; beyond that the
    heap is flushed to a sorted run file on disk and pops merge heap and
    run heads.  Memory stays bounded regardless of how many candidates a
    worst-case (heavy overlap) search generates.
    """

    def __init__(self, max_items=65536, spill_dir=None, max_runs=16):
        self.max_items = max(64, int(max_items))
        self._heap = []            # (lower, seq, data)
        self._runs = []            # list[_RunReader]
        self._heads = []           # heap of (lower, run_index)
        self._seq = 0
        self._max_runs = max_runs
        self._own_dir = spill_dir is None
        self._tmpdir = spill_dir or tempfile.mkdtemp(prefix="spcpq-")
        os.makedirs(self._tmpdir, exist_ok=True)
        self.spills = 0
        self.high_water = 0        # max live items (memory + spilled)
        self.total_pushed = 0
        self._run_items = 0        # live items currently in spill runs
        self._closed = False

    # -- internal ------------------------------------------------------

    def _spill(self):
        items = sorted(self._heap)
        self._heap = []
        path = os.path.join(self._tmpdir, "run-%06d.tmp" % self.spills)
        with open(path, "wb") as fh:
            for lower, _seq, data in items:
                fh.write(_PQ_HEAD.pack(lower, len(data)))
                fh.write(data)
        self._runs.append(_RunReader(path))
        self._run_items += len(items)
        self._heads = [(r.lower, i) for i, r in enumerate(self._runs)
                       if not r.exhausted]
        heapq.heapify(self._heads)
        self.spills += 1
        if len(self._runs) > self._max_runs:
            self._merge_runs()

    def _merge_runs(self):
        path = os.path.join(self._tmpdir, "merge-%06d.tmp" % self.spills)
        self.spills += 1
        sources = self._runs
        heap = [(r.lower, i, r) for i, r in enumerate(sources) if not r.exhausted]
        heapq.heapify(heap)
        with open(path, "wb") as fh:
            while heap:
                _, i, r = heapq.heappop(heap)
                lower, data = r.take()
                fh.write(_PQ_HEAD.pack(lower, len(data)))
                fh.write(data)
                if not r.exhausted:
                    heapq.heappush(heap, (r.lower, i, r))
        for r in sources:
            r.close()
        self._runs = [_RunReader(path)]
        self._heads = [(self._runs[0].lower, 0)] if not self._runs[0].exhausted else []

    # -- public --------------------------------------------------------

    def push(self, lower, data):
        if self._closed:
            raise ValueError("push on closed SpillablePQ")
        heapq.heappush(self._heap, (lower, self._seq, data))
        self._seq += 1
        self.total_pushed += 1
        live = len(self._heap) + self._run_items
        if live > self.high_water:
            self.high_water = live
        if len(self._heap) > self.max_items:
            self._spill()

    def empty(self):
        return not self._heap and not self._heads

    def pop(self):
        """Removes and returns (lower, data) with the smallest lower bound."""
        if self.empty():
            raise IndexError("pop from empty SpillablePQ")
        take_heap = False
        if self._heap:
            take_heap = True
            if self._heads and self._heads[0][0] < self._heap[0][0]:
                take_heap = False
        if take_heap:
            lower, _seq, data = heapq.heappop(self._heap)
            return lower, data
        _lower, idx = heapq.heappop(self._heads)
        reader = self._runs[idx]
        lower, data = reader.take()
        self._run_items -= 1
        if not reader.exhausted:
            heapq.heappush(self._heads, (reader.lower, idx))
        else:
            reader.close()
        return lower, data

    def close(self):
        if self._closed:
            return
        self._closed = True
        for r in self._runs:
            r.close()
        self._runs = []
        self._heads = []
        self._heap = []
        if self._own_dir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ------------------------------------------------------- spillable heap

class _HeapRunReader:
    """Streaming reader over one sorted spill run of length-prefixed byte
    records; the ordering key is recomputed from each record via key_fn."""

    __slots__ = ("path", "fh", "key_fn", "exhausted", "key", "data")

    def __init__(self, path, key_fn):
        self.path = path
        self.fh = open(path, "rb")
        self.key_fn = key_fn
        self.exhausted = False
        self.key = None
        self.data = None
        self._advance()

    def _advance(self):
        head = self.fh.read(_LEN.size)
        if len(head) < _LEN.size:
            self.exhausted = True
            self.key = None
            self.data = None
            return
        (n,) = _LEN.unpack(head)
        data = self.fh.read(n)
        if len(data) != n:
            raise IOError("heap spill run %s truncated" % self.path)
        self.data = data
        self.key = self.key_fn(data)

    def take(self):
        item = self.data
        self._advance()
        return item

    def close(self):
        try:
            self.fh.close()
        finally:
            _quiet_unlink(self.path)


class SpillableHeap:
    """Min-heap over byte records ordered by ``key_fn(record)``.

    Up to ``max_items`` records stay in an in-memory heap; beyond that the
    heap is flushed to a sorted run file on disk and pops merge the memory
    heap with the run heads.  Keys are arbitrary comparable tuples (they are
    never serialised: each run reader recomputes them from the record), so
    this structure serves sweeps whose ordering is not a single float.
    Memory stays bounded regardless of how many records a worst-case input
    pushes.
    """

    _INSTANCE_SEQ = [0]   # class-level counter: unique run-file prefix

    def __init__(self, key_fn, max_items=65536, spill_dir=None, tag="heap",
                 max_runs=32):
        self._key_fn = key_fn
        SpillableHeap._INSTANCE_SEQ[0] += 1
        # unique per-instance prefix so heaps sharing one spill_dir never
        # overwrite each other's run files
        self._tag = "%s-%d" % (tag, SpillableHeap._INSTANCE_SEQ[0])
        self.max_items = max(16, int(max_items))
        self._heap = []            # (key, seq, record)
        self._runs = []            # list[_HeapRunReader]
        self._heads = []           # heap of (key, run_index)
        self._seq = 0
        self._max_runs = max_runs
        self._own_dir = spill_dir is None
        self._tmpdir = spill_dir or tempfile.mkdtemp(prefix="spcheap-")
        os.makedirs(self._tmpdir, exist_ok=True)
        self.spills = 0
        self.high_water = 0        # max live records (memory + spilled)
        self.total_pushed = 0
        self._run_items = 0        # live records currently in spill runs
        self._closed = False

    def _spill(self):
        items = sorted(self._heap)
        self._heap = []
        path = os.path.join(self._tmpdir,
                            "%s-run-%06d.tmp" % (self._tag, self.spills))
        with open(path, "wb") as fh:
            for _key, _seq, rec in items:
                fh.write(_LEN.pack(len(rec)))
                fh.write(rec)
        self._runs.append(_HeapRunReader(path, self._key_fn))
        self._run_items += len(items)
        self._heads = [(r.key, i) for i, r in enumerate(self._runs)
                       if not r.exhausted]
        heapq.heapify(self._heads)
        self.spills += 1
        if len(self._runs) > self._max_runs:
            self._merge_runs()

    def _merge_runs(self):
        path = os.path.join(self._tmpdir,
                            "%s-merge-%06d.tmp" % (self._tag, self.spills))
        self.spills += 1
        sources = self._runs
        heap = [(r.key, i, r) for i, r in enumerate(sources) if not r.exhausted]
        heapq.heapify(heap)
        with open(path, "wb") as fh:
            while heap:
                _, i, r = heapq.heappop(heap)
                rec = r.take()
                fh.write(_LEN.pack(len(rec)))
                fh.write(rec)
                if not r.exhausted:
                    heapq.heappush(heap, (r.key, i, r))
        for r in sources:
            r.close()
        self._runs = [_HeapRunReader(path, self._key_fn)]
        self._heads = [(self._runs[0].key, 0)] \
            if not self._runs[0].exhausted else []

    def push(self, rec):
        if self._closed:
            raise ValueError("push on closed SpillableHeap")
        heapq.heappush(self._heap, (self._key_fn(rec), self._seq, rec))
        self._seq += 1
        self.total_pushed += 1
        live = len(self._heap) + self._run_items
        if live > self.high_water:
            self.high_water = live
        if len(self._heap) > self.max_items:
            self._spill()

    def empty(self):
        return not self._heap and not self._heads

    def __len__(self):
        return len(self._heap) + self._run_items

    def peek(self):
        """Return the smallest record without removing it (None if empty)."""
        if self.empty():
            return None
        take_heap = True
        if self._heads:
            if not self._heap:
                take_heap = False
            elif self._heads[0][0] < self._heap[0][0]:
                take_heap = False
        if take_heap:
            return self._heap[0][2]
        return self._runs[self._heads[0][1]].data

    def pop(self):
        """Removes and returns the smallest record."""
        if self.empty():
            raise IndexError("pop from empty SpillableHeap")
        take_heap = True
        if self._heads:
            if not self._heap:
                take_heap = False
            elif self._heads[0][0] < self._heap[0][0]:
                take_heap = False
        if take_heap:
            _key, _seq, rec = heapq.heappop(self._heap)
            return rec
        _key, idx = heapq.heappop(self._heads)
        reader = self._runs[idx]
        rec = reader.take()
        self._run_items -= 1
        if not reader.exhausted:
            heapq.heappush(self._heads, (reader.key, idx))
        else:
            reader.close()
        return rec

    def close(self):
        if self._closed:
            return
        self._closed = True
        for r in self._runs:
            r.close()
        self._runs = []
        self._heads = []
        self._heap = []
        if self._own_dir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
