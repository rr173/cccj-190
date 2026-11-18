"""Read-only catalog queries.

  * window  -- closed-rectangle intersection; boundary touch counts.
               DFS over node pages; the stack is bounded by
               depth x page_capacity, so it cannot grow with the data.
  * nearest -- best-first kNN driven by bbox distance lower bounds with
               a spillable candidate queue; stops only when every
               unvisited candidate's lower bound exceeds the current
               k-th true distance (ties by object_id are still explored,
               so equidistant id-order is exact).

Every query reports real counters: node pages visited, payloads decoded,
exact geometry predicates evaluated, and priority-queue spill stats.
"""

import math
import os
from collections import OrderedDict

from shapely.geometry import LineString, Point, box
from shapely.wkb import loads as _loads_wkb

from . import format as fmt
from .errors import (CatalogError, CorruptionError, IllegalReferenceError,
                     QueryError)
from .spill import SpillablePQ

DEFAULT_PAGE_CACHE_BYTES = 8 << 20
DEFAULT_QUEUE_SPILL_BYTES = 16 << 20
_PQ_ITEM_BYTES_EST = 96  # conservative per-entry estimate for spill budget


class QueryStats:
    """Per-query counters proving the index -- not a full scan -- ran."""

    __slots__ = ("nodes_visited", "payloads_decoded", "exact_predicates",
                 "pq_spills", "pq_high_water", "pq_pushed")

    def __init__(self):
        self.nodes_visited = 0
        self.payloads_decoded = 0
        self.exact_predicates = 0
        self.pq_spills = 0
        self.pq_high_water = 0
        self.pq_pushed = 0

    def as_dict(self):
        return {
            "nodes_visited": self.nodes_visited,
            "payloads_decoded": self.payloads_decoded,
            "exact_predicates": self.exact_predicates,
            "pq_spills": self.pq_spills,
            "pq_high_water": self.pq_high_water,
            "pq_pushed": self.pq_pushed,
        }


def _rect_geometry(minx, miny, maxx, maxy):
    """Closed rectangle as a shapely geometry, degeneracy-aware."""
    if minx == maxx and miny == maxy:
        return Point(minx, miny)
    if minx == maxx or miny == maxy:
        return LineString([(minx, miny), (maxx, maxy)])
    return box(minx, miny, maxx, maxy)


def _bbox_mindist(x, y, bbox):
    """Lower bound on the true point-geometry distance.

    One ulp is shaved off so float rounding can never make the bound
    exceed the true distance (conservative is safe: it may visit an extra
    node, it may never prune a true answer).
    """
    minx, miny, maxx, maxy = bbox
    dx = max(minx - x, 0.0, x - maxx)
    dy = max(miny - y, 0.0, y - maxy)
    return math.nextafter(math.hypot(dx, dy), -math.inf)


class _RevStr:
    """Reverse-order string wrapper so a min-heap can evict the worst id."""
    __slots__ = ("s",)

    def __init__(self, s):
        self.s = s

    def __lt__(self, other):
        return self.s > other.s


class _TopK:
    """Best-k by (distance asc, object_id asc); evicts the worst entry."""

    def __init__(self, k):
        self.k = k
        self._heap = []  # (-dist, _RevStr(id), id, dist): worst at [0]

    def full(self):
        return len(self._heap) >= self.k

    @property
    def worst(self):
        return (self._heap[0][3], self._heap[0][2])  # (dist, id)

    def add(self, dist, object_id):
        import heapq
        if self.k <= 0:
            return
        if len(self._heap) < self.k:
            heapq.heappush(self._heap, (-dist, _RevStr(object_id), object_id, dist))
        else:
            worst_dist, worst_id = self.worst
            if (dist, object_id) < (worst_dist, worst_id):
                heapq.heapreplace(self._heap,
                                  (-dist, _RevStr(object_id), object_id, dist))

    def results(self):
        out = sorted((dist, oid) for _n, _r, oid, dist in self._heap)
        return [(oid, dist) for dist, oid in out]


class WindowResult:
    """Lazy iterator over matching ids; stats accumulate as it runs."""

    def __init__(self, catalog, rect, stats):
        self._catalog = catalog
        self._rect = rect
        self.stats = stats
        self._done = False
        self._gen = self._iterate()

    def _iterate(self):
        minx, miny, maxx, maxy = self._rect
        rect_geom = _rect_geometry(*self._rect)
        cat = self._catalog
        stack = [cat._root_page]
        while stack:
            cat._check_open()
            page_no = stack.pop()
            kind, entries = cat._read_page(page_no)
            self.stats.nodes_visited += 1
            if kind == fmt.PAGE_KIND_INTERNAL:
                for e in entries:
                    if e[0] <= maxx and e[2] >= minx and e[1] <= maxy and e[3] >= miny:
                        stack.append(e[4])
            else:
                for e in entries:
                    if not (e[0] <= maxx and e[2] >= minx and
                            e[1] <= maxy and e[3] >= miny):
                        continue
                    object_id, geom = cat._read_payload(e[4], e[5])
                    self.stats.payloads_decoded += 1
                    self.stats.exact_predicates += 1
                    if rect_geom.intersects(geom):
                        yield object_id

    def __iter__(self):
        return self

    def __next__(self):
        if self._done:
            raise StopIteration
        try:
            return next(self._gen)
        except StopIteration:
            self._done = True
            raise

    def close(self):
        self._done = True
        self._gen.close()


class NearestResult:
    """Eager kNN answer: ordered [(object_id, distance), ...] plus stats."""

    def __init__(self, results, stats):
        self.results = results
        self.stats = stats

    def __iter__(self):
        return iter(self.results)

    def __len__(self):
        return len(self.results)


class Catalog:
    """An opened, committed catalog.  Use as a context manager."""

    def __init__(self, path, page_cache_bytes=DEFAULT_PAGE_CACHE_BYTES,
                 queue_spill_bytes=DEFAULT_QUEUE_SPILL_BYTES, verify=False):
        self._path = path
        self._manifest = fmt.load_manifest(os.path.join(path, fmt.MANIFEST_NAME))
        m = self._manifest
        self._page_size = m["page_size"]
        self._root_page = m["root_page"]
        self._depth = m["depth"]
        self._object_count = m["object_count"]
        self._page_count = m["page_count"]
        self._closed = False

        nodes_path = os.path.join(path, fmt.NODES_NAME)
        payloads_path = os.path.join(path, fmt.PAYLOADS_NAME)
        for name, p in ((fmt.NODES_NAME, nodes_path), (fmt.PAYLOADS_NAME, payloads_path)):
            if not os.path.isfile(p):
                raise CorruptionError("catalog file missing: %s" % p)
            actual = os.path.getsize(p)
            expect = m["files"][name]["length"]
            if actual != expect:
                raise CorruptionError(
                    "%s length %d != manifest %d (truncated or corrupted)"
                    % (name, actual, expect))
        self._nodes_fh = open(nodes_path, "rb")
        self._payloads_fh = open(payloads_path, "rb")
        try:
            head = self._nodes_fh.read(fmt.NODES_HEADER_SIZE)
            nh = fmt.unpack_nodes_header(head)
            if nh["page_size"] != self._page_size:
                raise CorruptionError("nodes.bin page_size disagrees with manifest")
            if nh["page_count"] != self._page_count:
                raise CorruptionError("nodes.bin page_count disagrees with manifest")
            if not (0 <= self._root_page < self._page_count):
                raise IllegalReferenceError(
                    "root page %d outside page range %d" % (self._root_page, self._page_count))
            if fmt.NODES_HEADER_SIZE + self._page_count * self._page_size != \
                    m["files"][fmt.NODES_NAME]["length"]:
                raise CorruptionError("nodes.bin length inconsistent with page table")
            ph = fmt.unpack_payloads_header(
                self._payloads_fh.read(fmt.PAYLOADS_HEADER_SIZE))
            if ph["record_count"] != self._object_count:
                raise CorruptionError("payloads.bin record_count disagrees with manifest")
            if ph["file_length"] != m["files"][fmt.PAYLOADS_NAME]["length"]:
                raise CorruptionError("payloads.bin length disagrees with header")
        except Exception:
            self.close()
            raise
        self._payloads_length = m["files"][fmt.PAYLOADS_NAME]["length"]

        cap = max(4, page_cache_bytes // self._page_size)
        self._page_cache_cap = cap
        self._page_cache = OrderedDict()
        self._pq_max_items = max(64, queue_spill_bytes // _PQ_ITEM_BYTES_EST)
        if verify:
            self.validate_full()

    # ------------------------------------------------------------ dunder

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        for fh in (getattr(self, "_nodes_fh", None), getattr(self, "_payloads_fh", None)):
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass

    def _check_open(self):
        if self._closed:
            raise CatalogError("catalog is closed")

    # ------------------------------------------------------------ info

    @property
    def manifest(self):
        return self._manifest

    @property
    def object_count(self):
        return self._object_count

    @property
    def page_count(self):
        return self._page_count

    @property
    def depth(self):
        return self._depth

    # ------------------------------------------------------------ reads

    def _read_page(self, page_no):
        if not (0 <= page_no < self._page_count):
            raise IllegalReferenceError(
                "page reference %d outside [0, %d)" % (page_no, self._page_count))
        cached = self._page_cache.get(page_no)
        if cached is not None:
            self._page_cache.move_to_end(page_no)
            return cached
        self._nodes_fh.seek(fmt.NODES_HEADER_SIZE + page_no * self._page_size)
        buf = self._nodes_fh.read(self._page_size)
        page = fmt.unpack_page(buf, self._page_size)  # CRC verified here
        self._page_cache[page_no] = page
        if len(self._page_cache) > self._page_cache_cap:
            self._page_cache.popitem(last=False)
        return page

    def _read_payload(self, offset, length):
        if not (fmt.PAYLOADS_HEADER_SIZE <= offset
                and offset + length <= self._payloads_length):
            raise IllegalReferenceError(
                "payload reference [%d,+%d) outside payloads.bin (%d bytes)"
                % (offset, length, self._payloads_length))
        self._payloads_fh.seek(offset)
        buf = self._payloads_fh.read(length)
        if len(buf) != length:
            raise CorruptionError("payload read truncated at offset %d" % offset)
        object_id, wkb = fmt.unpack_payload_record(buf)
        return object_id, _loads_wkb(wkb)

    # ------------------------------------------------------------ window

    def window(self, minx, miny, maxx, maxy, stats=None):
        """Ids whose geometry truly intersects the closed rectangle.

        Boundary touch counts as intersection.  The id order is the tree
        traversal order (deterministic for a given catalog, not sorted).
        """
        self._check_open()
        for name, v in (("minx", minx), ("miny", miny), ("maxx", maxx), ("maxy", maxy)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise QueryError("window %s must be a finite number, got %r" % (name, v))
        if minx > maxx or miny > maxy:
            raise QueryError("window rect has min > max: (%r, %r, %r, %r)"
                             % (minx, miny, maxx, maxy))
        return WindowResult(self, (float(minx), float(miny), float(maxx), float(maxy)),
                            stats or QueryStats())

    # ------------------------------------------------------------ nearest

    def nearest(self, x, y, k, stats=None):
        """k nearest geometries to (x, y) by true geometry distance.

        Ordering: distance asc, ties by object_id Unicode code point asc.
        Best-first over bbox distance lower bounds; the search stops only
        when every unvisited candidate's lower bound exceeds the current
        k-th distance, so equidistant id tie-breaks are resolved exactly.
        """
        self._check_open()
        stats = stats or QueryStats()
        for name, v in (("x", x), ("y", y)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise QueryError("nearest %s must be a finite number, got %r" % (name, v))
        if isinstance(k, bool) or not isinstance(k, int) or k < 0:
            raise QueryError("nearest k must be a non-negative integer, got %r" % (k,))
        if k == 0 or self._object_count == 0:
            return NearestResult([], stats)

        point = Point(float(x), float(y))
        topk = _TopK(k)
        pq = SpillablePQ(max_items=self._pq_max_items)
        try:
            kind, entries = self._read_page(self._root_page)
            stats.nodes_visited += 1
            self._push_entries(pq, kind, entries, x, y)
            stats.pq_pushed += len(entries)
            while not pq.empty():
                lower, data = pq.pop()
                if topk.full() and lower > topk.worst[0]:
                    # Every unvisited candidate is strictly farther than
                    # the current k-th answer: no id tie can be displaced.
                    break
                if data[:1] == b"I":
                    (child,) = _unpack_u64(data[1:])
                    kind, entries = self._read_page(child)
                    stats.nodes_visited += 1
                    self._push_entries(pq, kind, entries, x, y)
                    stats.pq_pushed += len(entries)
                else:
                    poff, plen = _unpack_leaf_ref(data[1:])
                    object_id, geom = self._read_payload(poff, plen)
                    stats.payloads_decoded += 1
                    stats.exact_predicates += 1
                    topk.add(geom.distance(point), object_id)
        finally:
            stats.pq_spills = pq.spills
            stats.pq_high_water = pq.high_water
            pq.close()
        return NearestResult(topk.results(), stats)

    def _push_entries(self, pq, kind, entries, x, y):
        if kind == fmt.PAGE_KIND_INTERNAL:
            for e in entries:
                lower = _bbox_mindist(x, y, e[:4])
                pq.push(lower, b"I" + _pack_u64(e[4]))
        else:
            for e in entries:
                lower = _bbox_mindist(x, y, e[:4])
                pq.push(lower, b"L" + _pack_leaf_ref(e[4], e[5]))

    # ------------------------------------------------------------ validate

    def validate_full(self):
        """Full structural check: file hashes, every page CRC, every
        payload record CRC, and every internal/leaf reference in range."""
        import hashlib

        def hash_file(path):
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()

        for name in (fmt.NODES_NAME, fmt.PAYLOADS_NAME):
            digest = hash_file(os.path.join(self._path, name))
            if digest != self._manifest["files"][name]["sha256"]:
                raise CorruptionError("%s sha256 mismatch with manifest" % name)

        leaf_entries = 0
        for page_no in range(self._page_count):
            self._nodes_fh.seek(fmt.NODES_HEADER_SIZE + page_no * self._page_size)
            buf = self._nodes_fh.read(self._page_size)
            kind, entries = fmt.unpack_page(buf, self._page_size)
            if kind == fmt.PAGE_KIND_INTERNAL:
                for e in entries:
                    if not (0 <= e[4] < self._page_count):
                        raise IllegalReferenceError(
                            "page %d references child page %d out of range"
                            % (page_no, e[4]))
            else:
                leaf_entries += len(entries)
                for e in entries:
                    if not (fmt.PAYLOADS_HEADER_SIZE <= e[4]
                            and e[4] + e[5] <= self._payloads_length):
                        raise IllegalReferenceError(
                            "page %d references payload [%d,+%d) out of range"
                            % (page_no, e[4], e[5]))
        if leaf_entries != self._object_count:
            raise CorruptionError(
                "leaf entries %d != object_count %d" % (leaf_entries, self._object_count))

        # Walk payloads sequentially: every record CRC + exact end offset.
        self._payloads_fh.seek(fmt.PAYLOADS_HEADER_SIZE)
        seen = 0
        pos = fmt.PAYLOADS_HEADER_SIZE
        while pos < self._payloads_length:
            head = self._payloads_fh.read(fmt.RECORD_HEADER_SIZE)
            if len(head) < fmt.RECORD_HEADER_SIZE:
                raise CorruptionError("payload record header truncated at %d" % pos)
            _crc, id_len, wkb_len = fmt.RECORD_HEADER.unpack(head)
            total = fmt.RECORD_HEADER_SIZE + id_len + wkb_len
            rest = self._payloads_fh.read(total - fmt.RECORD_HEADER_SIZE)
            if len(rest) != total - fmt.RECORD_HEADER_SIZE:
                raise CorruptionError("payload record body truncated at %d" % pos)
            fmt.unpack_payload_record(head + rest)
            pos += total
            seen += 1
        if pos != self._payloads_length:
            raise CorruptionError("payload walk ended at %d, file is %d"
                                  % (pos, self._payloads_length))
        if seen != self._object_count:
            raise CorruptionError("payload records %d != object_count %d"
                                  % (seen, self._object_count))
        return {
            "ok": True,
            "objects": self._object_count,
            "pages": self._page_count,
            "payload_records": seen,
        }


def _pack_u64(v):
    import struct
    return struct.pack(">Q", v)


def _unpack_u64(b):
    import struct
    return struct.unpack(">Q", b)


def _pack_leaf_ref(poff, plen):
    import struct
    return struct.pack(">QI", poff, plen)


def _unpack_leaf_ref(b):
    import struct
    return struct.unpack(">QI", b)
