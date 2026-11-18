"""Unit tests for cross-table binary records, canonical TSV encoding,
key ordering and the sorted-stream dedupe guarantee."""

import pytest

from spatial_catalog import xformat as xf
from spatial_catalog.errors import CorruptionError
from spatial_catalog.xtab import _DedupSorted


def test_binary_record_roundtrip():
    vals = ("src-1", "zön-Ω", 2.5, 10.0, 0.25)
    rec = xf.pack_row_record(*vals)
    assert xf.unpack_row_record(rec) == vals


def test_sort_key_is_utf8_byte_order():
    ids = ["z", "ä", "Ω", "乙", "线"]
    recs = [xf.pack_row_record(s, "zone", 1.0, 2.0, 0.5) for s in ids]
    ordered = sorted(recs, key=xf.row_record_key)
    got = [xf.unpack_row_record(r)[0] for r in ordered]
    assert got == sorted(ids)  # UTF-8 byte order == code point order


def test_tsv_canonical_encoding_and_parse():
    line = xf.encode_row_line("s", "z", 0.1, 3.0, 0.1 / 3.0)
    assert line.endswith(b"\n") and b"\r" not in line
    parsed = xf.parse_row_line(line, 3)
    assert parsed[0] == "s" and parsed[1] == "z"
    assert parsed[2] == 0.1 and parsed[3] == 3.0
    assert parsed[4] == 0.1 / 3.0
    # re-encoding yields identical bytes (deterministic, locale-free)
    assert xf.encode_row_line(*parsed) == line


def test_parse_rejects_bad_field_count():
    with pytest.raises(CorruptionError):
        xf.parse_row_line(b"a\tb\tc\n", 1)


def test_parse_rejects_non_finite():
    line = xf.encode_row_line("a", "b", 1.0, 2.0, 0.5).replace(b"0.5", b"inf")
    with pytest.raises(CorruptionError):
        xf.parse_row_line(line, 1)


def test_parse_rejects_non_positive_intersection():
    line = xf.encode_row_line("a", "b", 0.0, 2.0, 0.0)
    with pytest.raises(CorruptionError):
        xf.parse_row_line(line, 1)


def test_parse_rejects_intersection_exceeding_source():
    line = xf.encode_row_line("a", "b", 3.0, 2.0, 1.5)
    with pytest.raises(CorruptionError):
        xf.parse_row_line(line, 1)


def test_parse_rejects_ratio_mismatch():
    line = xf.encode_row_line("a", "b", 1.0, 4.0, 0.5)  # true ratio 0.25
    with pytest.raises(CorruptionError):
        xf.parse_row_line(line, 1)


def test_parse_rejects_cr_and_missing_lf():
    line = xf.encode_row_line("a", "b", 1.0, 2.0, 0.5)
    with pytest.raises(CorruptionError):
        xf.parse_row_line(line.rstrip(b"\n"), 1)
    with pytest.raises(CorruptionError):
        xf.parse_row_line(line.replace(b"\n", b"\r\n"), 1)


def test_tsv_unsafe_ids_rejected():
    from spatial_catalog.xformat import RowIdError
    with pytest.raises(RowIdError):
        xf.check_row_id("a\tb", "line")
    with pytest.raises(RowIdError):
        xf.check_row_id("a\nb", "zone")
    xf.check_row_id("unicode-Ω-乙", "line")  # fine


def test_dedup_sorted_drops_repeats_keeps_first():
    recs = [
        xf.pack_row_record("a", "z1", 1.0, 2.0, 0.5),
        xf.pack_row_record("a", "z1", 9.0, 2.0, 4.5),  # duplicate pair
        xf.pack_row_record("a", "z2", 1.0, 2.0, 0.5),
        xf.pack_row_record("b", "z1", 1.0, 2.0, 0.5),
        xf.pack_row_record("b", "z1", 2.0, 2.0, 1.0),  # duplicate pair
    ]
    dedup = _DedupSorted(iter(recs))
    out = [xf.unpack_row_record(r) for r in dedup]
    assert [r[:2] for r in out] == [("a", "z1"), ("a", "z2"), ("b", "z1")]
    # first occurrence's values survive
    assert out[0][2] == 1.0
    assert dedup.dropped == 2


def test_dedup_passes_unique_stream_unchanged():
    recs = [xf.pack_row_record("a", "z", 1.0, 2.0, 0.5),
            xf.pack_row_record("b", "z", 1.0, 2.0, 0.5)]
    dedup = _DedupSorted(iter(recs))
    assert len(list(dedup)) == 2
    assert dedup.dropped == 0


def test_manifest_identity_stability(tmp_path):
    # identical content -> identical digest; changed hash -> different
    base = {
        "catalog_path": "/x/cat", "format": "offline-spatial-catalog",
        "version": 1, "object_count": 3, "manifest_build_id": "b1",
        "nodes_sha256": "n" * 64, "nodes_length": 10,
        "payloads_sha256": "p" * 64, "payloads_length": 20,
    }
    m = {"format": base["format"], "version": 1, "object_count": 3,
         "build_id": "b1",
         "files": {"nodes.bin": {"sha256": "n" * 64, "length": 10},
                   "payloads.bin": {"sha256": "p" * 64, "length": 20}}}
    id1 = xf.catalog_identity("/x/cat", m)
    id2 = xf.catalog_identity("/x/cat", dict(m))
    assert id1["identity_digest"] == id2["identity_digest"]
    m2 = dict(m)
    m2["files"] = dict(m["files"])
    m2["files"]["nodes.bin"] = {"sha256": "q" * 64, "length": 10}
    id3 = xf.catalog_identity("/x/cat", m2)
    assert id3["identity_digest"] != id1["identity_digest"]
