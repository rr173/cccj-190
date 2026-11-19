"""Direct unit tests for the disk-resident union-find.

These exercise the on-disk parent/rank files independently of the topology
build, including the empty and singleton cases, tiny pages that force
repeated eviction, and partition equivalence against a memory reference.
"""

import os
import random
import struct

import pytest

from spatial_catalog.tunion import DiskUnionFind, _PARENT, _RANK


def _paths(tmp_path, tag):
    return (str(tmp_path / ("%s-parent.bin" % tag)),
            str(tmp_path / ("%s-rank.bin" % tag)))


def test_empty_union_find(tmp_path):
    p, r = _paths(tmp_path, "e")
    uf = DiskUnionFind(0, p, r, page_bytes=1 << 12)
    assert list(uf.roots()) == []
    uf.close()
    assert os.path.getsize(p) == 0
    assert os.path.getsize(r) == 0


def test_singleton_is_its_own_component(tmp_path):
    p, r = _paths(tmp_path, "s")
    uf = DiskUnionFind(1, p, r, page_bytes=1 << 12)
    assert uf.union(0, 0) is False
    assert list(uf.roots()) == [(0, 0)]
    uf.close()
    assert struct.unpack(">Q", open(p, "rb").read()) == (0,)


def test_self_union_does_not_merge_distinct_roots(tmp_path):
    p, r = _paths(tmp_path, "u")
    uf = DiskUnionFind(3, p, r, page_bytes=1 << 12)
    assert uf.union(0, 0) is False
    assert uf.union(1, 2) is True
    assert uf.find(0) == 0
    assert uf.find(1) == uf.find(2)
    uf.close()


def test_partition_equivalence_under_tiny_pages(tmp_path):
    for trial in range(25):
        rng = random.Random(7000 + trial)
        n = rng.randint(2, 500)
        page_bytes = rng.choice([64, 128, 256])
        p, r = _paths(tmp_path, "p%d" % trial)
        uf = DiskUnionFind(n, p, r, page_bytes=page_bytes)
        parent = list(range(n))
        rank = [0] * n

        def rfind(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for _ in range(rng.randint(1, n * 2)):
            a, b = rng.randrange(n), rng.randrange(n)
            got = uf.union(a, b)
            ra, rb = rfind(a), rfind(b)
            assert got == (ra != rb)
            if ra != rb:
                if rank[ra] < rank[rb]:
                    ra, rb = rb, ra
                parent[rb] = ra
                if rank[ra] == rank[rb]:
                    rank[ra] += 1
        disk = {}
        for i, root in uf.roots():
            disk.setdefault(root, set()).add(i)
        ref = {}
        for i in range(n):
            ref.setdefault(rfind(i), set()).add(i)
        assert sorted(map(sorted, disk.values())) == \
            sorted(map(sorted, ref.values()))
        # when the node table exceeds the bounded page cache, eviction must
        # really happen; small sets that fit in one page legitimately don't.
        records_per_page = uf._page // _PARENT.size
        cacheable = records_per_page * uf._max_parent_pages
        if n > cacheable:
            assert uf.stats["parent_page_evictions"] > 0
        uf.close()


def test_parent_and_rank_files_are_fixed_width_and_one_component(tmp_path):
    p, r = _paths(tmp_path, "w")
    n = 37
    uf = DiskUnionFind(n, p, r, page_bytes=1 << 12)
    for i in range(n - 1):
        uf.union(i, i + 1)
    roots = {root for _i, root in uf.roots()}
    assert len(roots) == 1
    uf.close()
    # files keep exact fixed widths after a full flush
    assert os.path.getsize(p) == n * _PARENT.size
    assert os.path.getsize(r) == n * _RANK.size
    # a fresh construction over the same paths is build-only and resets
    # identity state (readers consume rendered output, never these files).
    uf2 = DiskUnionFind(n, p, r, page_bytes=1 << 12)
    assert {root for _i, root in uf2.roots()} == set(range(n))
    uf2.close()
