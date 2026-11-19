"""Disk-resident union-find for connected components.

The topology contract forbids a fully in-memory union-find: node counts may
exceed any cache.  This implementation keeps the parent and rank arrays in
fixed-width binary files and touches them through an explicitly bounded
page cache.  Every identical-coordinate class and every real graph
component is still processed completely -- the disk representation holds
the whole structure; only the *working set* is cached, never the graph.

Memory bound
------------
``page_bytes`` (default capped from the build's cache budget) bounds each of
the two page caches independently via an LRU policy, so total union-find
residency is O(page_bytes) regardless of node count.  Find uses path
halving; union uses rank.  A final ``roots()`` pass streams the parent file
once with the same cache, so component closure never requires loading the
array.

The class deliberately shares no algorithm with the in-memory test oracle
(``topo_oracle.py``) and none with the reader's independent component
checker: it is the build-side component mechanism only.
"""

import os
import struct
from collections import OrderedDict

_PARENT = struct.Struct(">Q")
_RANK = struct.Struct(">I")


class DiskUnionFind:
    """N elements, identity-initialised, living in two temp files."""

    def __init__(self, n, parent_path, rank_path, page_bytes=1 << 16):
        if n < 0:
            raise ValueError("negative union-find size")
        self.n = n
        self._page = max(_PARENT.size * 64, int(page_bytes))
        # page size must be a multiple of both record widths
        self._page -= self._page % _PARENT.size
        # parent and rank pages share one byte size (rank holds 2x records)
        self._rpage = self._page
        # The LRU caches hold a bounded NUMBER OF PAGES derived from the
        # byte budget, never a record count.  Each cache is allowed roughly
        # half the budget so total union-find residency stays bounded.
        self._max_parent_pages = max(2, page_bytes // (2 * self._page))
        self._max_rank_pages = max(2, page_bytes // (2 * self._rpage))
        self._pfh = open(parent_path, "w+b")
        self._rfh = open(rank_path, "w+b")
        self._pcache = OrderedDict()   # page_no -> bytearray
        self._rcache = OrderedDict()
        self._pdirty = set()
        self._rdirty = set()
        # Identity initialisation written as one contiguous stream; the
        # record offset is therefore always i * record width regardless of
        # the chosen page size, and the last page may legitimately be short.
        for i in range(n):
            self._pfh.write(_PARENT.pack(i))
        self._pfh.flush()
        os.fsync(self._pfh.fileno())
        self._parent_file_len = n * _PARENT.size
        # ranks start at zero; materialise the exact file length (full
        # pages plus a possible short tail), never sparse holes.
        self._rfh.write(b"\x00" * (n * _RANK.size))
        self._rfh.flush()
        os.fsync(self._rfh.fileno())
        self.stats = {"finds": 0, "unions": 0, "parent_page_evictions": 0,
                      "rank_page_evictions": 0}

    # -------------------------------------------------------- raw paging
    def _valid_parent_bytes(self, page_no):
        """Bytes of a parent page that actually back records; a short tail
        page must never be flushed at full width or it would overwrite the
        next page with padding zeros."""
        start = page_no * self._page
        return max(0, min(self._page, self._parent_file_len - start))

    def _valid_rank_bytes(self, page_no):
        start = page_no * self._rpage
        total = self.n * _RANK.size
        return max(0, min(self._rpage, total - start))

    def _load_parent_page(self, page_no):
        cached = self._pcache.get(page_no)
        if cached is not None:
            self._pcache.move_to_end(page_no)
            return cached
        pos = self._pfh.tell()
        self._pfh.seek(page_no * self._page)
        data = self._pfh.read(self._page)
        self._pfh.seek(pos)
        # The final page may be short; pad to full width so fixed-width
        # unpacking never sees a truncated buffer (padding stays beyond n).
        if len(data) < self._page:
            data = data + b"\x00" * (self._page - len(data))
        page = bytearray(data)
        self._pcache[page_no] = page
        if len(self._pcache) > self._max_parent_pages:
            old_no, old = self._pcache.popitem(last=False)
            if old_no in self._pdirty:
                valid = self._valid_parent_bytes(old_no)
                self._pfh.seek(old_no * self._page)
                self._pfh.write(bytes(old[:valid]))
                self._pdirty.discard(old_no)
            self.stats["parent_page_evictions"] += 1
        return page

    def _load_rank_page(self, page_no):
        cached = self._rcache.get(page_no)
        if cached is not None:
            self._rcache.move_to_end(page_no)
            return cached
        pos = self._rfh.tell()
        self._rfh.seek(page_no * self._rpage)
        data = self._rfh.read(self._rpage)
        self._rfh.seek(pos)
        if len(data) < self._rpage:
            data = data + b"\x00" * (self._rpage - len(data))
        page = bytearray(data)
        self._rcache[page_no] = page
        if len(self._rcache) > self._max_rank_pages:
            old_no, old = self._rcache.popitem(last=False)
            if old_no in self._rdirty:
                valid = self._valid_rank_bytes(old_no)
                self._rfh.seek(old_no * self._rpage)
                self._rfh.write(bytes(old[:valid]))
                self._rdirty.discard(old_no)
            self.stats["rank_page_evictions"] += 1
        return page

    def _read_parent(self, i):
        page_no, off = divmod(i * _PARENT.size, self._page)
        page = self._load_parent_page(page_no)
        return _PARENT.unpack_from(page, off)[0]

    def _write_parent(self, i, value):
        page_no, off = divmod(i * _PARENT.size, self._page)
        page = self._load_parent_page(page_no)
        _PARENT.pack_into(page, off, value)
        self._pdirty.add(page_no)

    def _read_rank(self, i):
        # Rank records are half the width of parent records; index the
        # rank file with rank record widths and the rank page byte size.
        page_no, off = divmod(i * _RANK.size, self._rpage)
        page = self._load_rank_page(page_no)
        if off + _RANK.size > len(page):
            return 0
        return _RANK.unpack_from(page, off)[0]

    def _write_rank(self, i, value):
        page_no, off = divmod(i * _RANK.size, self._rpage)
        page = self._load_rank_page(page_no)
        if off + _RANK.size > len(page):
            # rank page tail may be short: extend the in-memory page
            page.extend(b"\x00" * (off + _RANK.size - len(page)))
        _RANK.pack_into(page, off, value)
        self._rdirty.add(page_no)

    # ----------------------------------------------------------- union-find
    def find(self, i):
        """Find with path compression, implemented as two bounded walks.

        Walk 1 follows parent pointers to the root.  Walk 2 re-walks the
        same path and links every node directly to the root.  Both walks
        are iterative with an explicit list, so no recursion stack can
        grow with component size, and the on-disk parent of *every* node on
        the path -- including the starting node -- is refreshed, which a
        single path-halving pass that stops at the root's child cannot
        guarantee once a root later becomes a child of another root."""
        self.stats["finds"] += 1
        path = []
        cur = i
        while True:
            parent = self._read_parent(cur)
            if parent == cur:
                root = cur
                break
            path.append(cur)
            cur = parent
        for node in path:
            if self._read_parent(node) != root:
                self._write_parent(node, root)
        return root

    def union(self, a, b):
        self.stats["unions"] += 1
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        ka, kb = self._read_rank(ra), self._read_rank(rb)
        if ka < kb:
            ra, rb = rb, ra
        self._write_parent(rb, ra)
        if ka == kb:
            self._write_rank(ra, ka + 1)
        return True

    def flush(self):
        # Only pages still resident can carry unwritten changes; an evicted
        # page was already persisted and removed from the dirty set, so guard
        # against any stale marker instead of indexing a missing page.
        for page_no in list(self._pdirty):
            page = self._pcache.get(page_no)
            if page is None:
                self._pdirty.discard(page_no)
                continue
            valid = self._valid_parent_bytes(page_no)
            self._pfh.seek(page_no * self._page)
            self._pfh.write(bytes(page[:valid]))
        self._pdirty.clear()
        for page_no in list(self._rdirty):
            page = self._rcache.get(page_no)
            if page is None:
                self._rdirty.discard(page_no)
                continue
            valid = self._valid_rank_bytes(page_no)
            self._rfh.seek(page_no * self._rpage)
            self._rfh.write(bytes(page[:valid]))
        self._rdirty.clear()
        self._pfh.flush()
        self._rfh.flush()
        os.fsync(self._pfh.fileno())
        os.fsync(self._rfh.fileno())

    def roots(self):
        """Stream ``(i, true_root_i)`` for every element after a flush.

        A raw parent slot is only guaranteed to point at a root for nodes
        that happened to sit on a compressed path; other slots may point at
        a node that later became a child.  Each element is therefore
        resolved through :meth:`find`, which stays bounded because all
        reads go through the fixed-size page cache -- no per-node structure
        accumulates in memory.  The two-level (or longer) chains that a
        streaming raw read would mislabel as distinct components cannot
        survive this."""
        self.flush()
        for i in range(self.n):
            yield i, self.find(i)

    def close(self):
        try:
            self.flush()
        finally:
            self._pfh.close()
            self._rfh.close()
