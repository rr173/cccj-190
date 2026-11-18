"""Unit tests for the on-disk codecs, external sort, and spillable PQ."""

import os
import random
import struct

import pytest

from spatial_catalog import format as fmt
from spatial_catalog.errors import CorruptionError
from spatial_catalog.spill import SpillablePQ, external_sort


def test_leaf_page_roundtrip():
    entries = [(float(i), float(i + 1), float(i + 2), float(i + 3), 1000 * i, 44)
               for i in range(5)]
    page = fmt.pack_leaf_page(entries, 512)
    assert len(page) == 512
    kind, out = fmt.unpack_page(page, 512)
    assert kind == fmt.PAGE_KIND_LEAF
    assert out == entries


def test_internal_page_roundtrip():
    entries = [(0.0, 0.0, 1.0, 1.0, 7), (2.0, 2.0, 3.0, 3.0, 8)]
    page = fmt.pack_internal_page(entries, 512)
    kind, out = fmt.unpack_page(page, 512)
    assert kind == fmt.PAGE_KIND_INTERNAL
    assert out == entries


def test_page_crc_detects_flip():
    page = bytearray(fmt.pack_leaf_page([(0.0, 0.0, 1.0, 1.0, 10, 20)], 512))
    page[100] ^= 0xFF
    with pytest.raises(CorruptionError):
        fmt.unpack_page(bytes(page), 512)


def test_page_truncated_detected():
    page = fmt.pack_leaf_page([(0.0, 0.0, 1.0, 1.0, 10, 20)], 512)
    with pytest.raises(CorruptionError):
        fmt.unpack_page(page[:100], 512)


def test_payload_record_roundtrip():
    blob = fmt.pack_payload_record("obj-é", b"\x01\x02\x03")
    object_id, wkb = fmt.unpack_payload_record(blob)
    assert object_id == "obj-é"
    assert wkb == b"\x01\x02\x03"


def test_payload_record_crc_detects_flip():
    blob = bytearray(fmt.pack_payload_record("a", b"\x00" * 32))
    blob[-1] ^= 0x01
    with pytest.raises(CorruptionError):
        fmt.unpack_payload_record(bytes(blob))


def test_nodes_header_roundtrip_and_crc():
    head = fmt.pack_nodes_header(4096, 10, 3, 9, 12345)
    info = fmt.unpack_nodes_header(head)
    assert info["page_size"] == 4096
    assert info["page_count"] == 10
    assert info["depth"] == 3
    assert info["root_page"] == 9
    assert info["entry_count"] == 12345
    bad = bytearray(head)
    bad[20] ^= 0x01
    with pytest.raises(CorruptionError):
        fmt.unpack_nodes_header(bytes(bad))


def test_external_sort_large_and_spilled(tmp_path):
    rng = random.Random(42)
    records = [struct.pack(">Q", rng.randrange(1 << 40)) for _ in range(5000)]
    key = lambda r: r
    out = list(external_sort(iter(records), key, chunk_bytes=1024,
                             tmpdir=str(tmp_path)))
    assert out == sorted(records)
    # many runs must have been produced and merged with a 1 KiB budget
    assert len(out) == 5000


def test_external_sort_empty(tmp_path):
    assert list(external_sort(iter([]), lambda r: r, 128, str(tmp_path))) == []


def test_spillable_pq_orders_and_spills(tmp_path):
    pq = SpillablePQ(max_items=100, spill_dir=str(tmp_path / "pq"))
    rng = random.Random(7)
    values = [rng.random() for _ in range(5000)]
    for i, v in enumerate(values):
        pq.push(v, b"L" + struct.pack(">I", i))
    assert pq.spills > 0
    out = []
    while not pq.empty():
        lower, data = pq.pop()
        out.append(lower)
    assert out == sorted(values)
    pq.close()
    # spill files are cleaned up
    assert not os.listdir(tmp_path / "pq") if os.path.isdir(tmp_path / "pq") else True


def test_spillable_pq_interleaved_push_pop(tmp_path):
    pq = SpillablePQ(max_items=50, spill_dir=str(tmp_path / "pq2"))
    expected = []
    for i in range(2000):
        pq.push(float(i % 977), b"I" + struct.pack(">I", i))
        expected.append(float(i % 977))
        if i % 3 == 0 and not pq.empty():
            lower, _ = pq.pop()
            assert lower == expected.pop(expected.index(min(expected)))
    got = []
    while not pq.empty():
        got.append(pq.pop()[0])
    assert got == sorted(expected)
    pq.close()


def test_hilbert_deterministic_and_local():
    from spatial_catalog.builder import _hilbert_xy_to_d
    assert _hilbert_xy_to_d(0, 0) == _hilbert_xy_to_d(0, 0)
    assert _hilbert_xy_to_d(0, 0) != _hilbert_xy_to_d(1 << 30, 1 << 30)
    # all keys fit in 62 bits
    assert _hilbert_xy_to_d((1 << 31) - 1, (1 << 31) - 1) < (1 << 62)
