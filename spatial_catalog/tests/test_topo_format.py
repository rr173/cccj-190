"""Codec/format unit tests: binary record round-trips, canonical TSV
encoding, geometry text and planar length determinism."""

import math

import pytest

from spatial_catalog import tformat as tf
from spatial_catalog.errors import CorruptionError


def test_edge_record_roundtrip_and_key():
    coords = [(0.0, 0.0), (1.5, -2.25), (3.0, 4.0)]
    rec = tf.pack_edge_record("src-Ω", 7, 42, coords)
    sid, part, seq, got = tf.unpack_edge_record(rec)
    assert (sid, part, seq, got) == ("src-Ω", 7, 42, coords)
    # key sorts by UTF-8 id bytes then part (code point order)
    rec2 = tf.pack_edge_record("src-Ω", 8, 1, coords)
    rec3 = tf.pack_edge_record("aaa", 0, 2, coords)
    ordered = sorted([rec2, rec, rec3], key=tf.edge_record_key)
    # "aaa" sorts before "src-Ω"; within one id, part 7 before part 8
    assert [tf.unpack_edge_record(r)[0] for r in ordered] == \
        ["aaa", "src-Ω", "src-Ω"]
    assert [tf.unpack_edge_record(r)[1] for r in ordered[1:]] == [7, 8]
    keys = [tf.edge_record_key(r) for r in ordered]
    assert keys == sorted(keys)


def test_endpoint_and_incidence_roundtrip():
    e = tf.pack_endpoint(1.0, 2.0, 99, tf.END_TO)
    assert tf.unpack_endpoint(e) == (1.0, 2.0, 99, tf.END_TO)
    inc = tf.pack_incidence(99, tf.END_FROM, 1.0, 2.0)
    assert tf.unpack_incidence(inc) == (99, tf.END_FROM, 1.0, 2.0)


def test_flat_edge_and_nodemap_roundtrip():
    coords = [(0.0, 0.0), (1.0, 1.0)]
    flat = tf.pack_flat_edge("s", 2, 3, 4, coords)
    assert tf.unpack_flat_edge(flat) == ("s", 2, 3, 4, coords)
    nm = tf.pack_nodemap(5, 7.0, 8.0)
    assert tf.unpack_nodemap(nm) == (5, 7.0, 8.0)
    assert len(nm) == tf.NODEMAP_SIZE


def test_stable_key_ordinal_roundtrip():
    assert tf.node_key(0) == "N%012d" % 0
    assert tf.edge_key(123) == "E%012d" % 123
    assert tf.component_key(9) == "C%012d" % 9
    assert tf.ordinal_of_key(tf.node_key(42), "N") == 42
    with pytest.raises(CorruptionError):
        tf.ordinal_of_key("X000000000001", "N")
    with pytest.raises(CorruptionError):
        tf.ordinal_of_key("N12", "N")


def test_geometry_wkt_roundtrip_and_canonical():
    coords = [(0.0, 0.0), (1.0, 2.5), (-3.25, 4.0)]
    text = tf.encode_geometry_wkt(coords)
    assert text.startswith("LINESTRING(") and text.endswith(")")
    assert tf.parse_geometry_wkt(text) == coords
    with pytest.raises(CorruptionError):
        tf.parse_geometry_wkt("POINT(0 0)")


def test_planar_length_matches_hand_sum():
    # 3-4-5 triangle segment plus a unit segment
    coords = [(0.0, 0.0), (3.0, 4.0), (3.0, 5.0)]
    assert tf.planar_length(coords) == 5.0 + 1.0
    # zero-length part
    assert tf.planar_length([(2.0, 2.0), (2.0, 2.0)]) == 0.0


def test_node_line_canonical_and_parse():
    refs = [(0, tf.END_FROM), (0, tf.END_TO), (3, tf.END_FROM)]
    line = tf.encode_node_line(2, 1.0, 0.0, 3, refs, 2)
    parsed = tf.parse_node_line(line, 5)
    assert parsed == (2, 1.0, 0.0, 3, refs, 2)
    # a re-spelled number breaks the canonical check
    bad = line.replace(b"1.0", b"1", 1)
    with pytest.raises(CorruptionError):
        tf.parse_node_line(bad, 5)
    # degree/incidence disagreement is rejected
    bad_refs = refs[:2]
    line2 = tf.encode_node_line(2, 1.0, 0.0, 3, bad_refs, 2)
    with pytest.raises(CorruptionError):
        tf.parse_node_line(line2, 5)


def test_edge_line_canonical_and_parse():
    coords = [(0.0, 0.0), (1.0, 1.0)]
    line = tf.encode_edge_line(1, "sid", 0, 2, 3, coords,
                               math.sqrt(2.0), 2)
    parsed = tf.parse_edge_line(line, 4)
    assert parsed == (1, "sid", 0, 2, 3, coords, math.sqrt(2.0), 2)
    # negative length rejected
    bad = line.replace(repr(math.sqrt(2.0)).encode(), b"-1.0", 1)
    with pytest.raises(CorruptionError):
        tf.parse_edge_line(bad, 4)


def test_normalize_coord_collapses_negative_zero():
    assert tf.normalize_coord(-0.0, -0.0) == (0.0, 0.0)
    with pytest.raises(Exception):
        tf.normalize_coord(float("nan"), 1.0)
    with pytest.raises(Exception):
        tf.normalize_coord(1.0, float("inf"))


def test_check_topo_id_rules():
    tf.check_topo_id("ok-Ω", "source")
    for bad in ("", "a\tb", "x\ny"):
        with pytest.raises(Exception):
            tf.check_topo_id(bad, "source")
