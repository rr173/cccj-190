"""Topology open/validate corruption coverage.

Includes byte damage, missing/extra rows, duplicate keys, dangling node
and edge references, coordinate/source-end disagreement, degree mismatch
and component declaration errors.  Each deep-grammar test repins the
manifest length/hash so the failure comes from the semantic checker, not
the open-time boundary guard.
"""

import hashlib
import json
import os

import pytest

from spatial_catalog import tformat as tf
from spatial_catalog.builder import build as build_catalog
from spatial_catalog.errors import (CorruptionError, NotACatalogError,
                                    UnsupportedVersionError)
from spatial_catalog.topo import Selection, build_topology
from spatial_catalog.treader import Topology
from helpers import linestring, square, write_records


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def _repin(topo, name):
    """Repin one result file's length and SHA in the manifest so open
    succeeds and the deeper validators are exercised."""
    mp = os.path.join(topo, tf.MANIFEST_NAME)
    m = json.load(open(mp, encoding="utf-8"))
    p = os.path.join(topo, name)
    m["files"][name]["length"] = os.path.getsize(p)
    m["files"][name]["sha256"] = _sha(p)
    json.dump(m, open(mp, "w", encoding="utf-8"))


@pytest.fixture
def topo_dir(tmp_path):
    recs = [
        {"object_id": "a", "geometry": linestring([[0, 0], [1, 0]])},
        {"object_id": "b", "geometry": linestring([[1, 0], [2, 0]])},
        {"object_id": "c", "geometry": linestring([[2, 0], [2, 1]])},
        {"object_id": "z",
         "geometry": {"type": "MultiLineString",
                      "coordinates": [[[9, 9], [9, 9]], [[9, 9], [8, 8]]]}},
    ]
    src = write_records(tmp_path / "in.jsonl", recs)
    cat = str(tmp_path / "cat")
    build_catalog(src, cat, cache_bytes=1 << 16, force=True)
    out = str(tmp_path / "topo")
    build_topology(cat, out, selection=Selection(),
                   cache_bytes=1 << 16, force=True)
    return out, cat


# ------------------------------------------------------------- boundary
def test_truncated_nodes_rejected_at_open(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.NODES_NAME)
    os.truncate(p, os.path.getsize(p) - 5)
    with pytest.raises(CorruptionError):
        Topology(out)


def test_truncated_edges_rejected_at_open(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.EDGES_NAME)
    os.truncate(p, os.path.getsize(p) - 5)
    with pytest.raises(CorruptionError):
        Topology(out)


def test_bit_flip_detected_by_hash(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.NODES_NAME)
    with open(p, "r+b") as fh:
        fh.seek(len(tf.NODES_HEADER_BYTES) + 4)
        b = fh.read(1)
        fh.seek(len(tf.NODES_HEADER_BYTES) + 4)
        fh.write(bytes([b[0] ^ 0x01]))
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


def test_missing_manifest_is_not_a_topology(topo_dir):
    out, _ = topo_dir
    os.unlink(os.path.join(out, tf.MANIFEST_NAME))
    with pytest.raises(NotACatalogError):
        Topology(out)


def test_half_built_directory_rejected(tmp_path):
    d = tmp_path / "half"
    d.mkdir()
    (d / tf.NODES_NAME).write_bytes(tf.NODES_HEADER_BYTES)
    with pytest.raises(NotACatalogError):
        Topology(str(d))


def test_missing_result_file_rejected(topo_dir):
    out, _ = topo_dir
    os.unlink(os.path.join(out, tf.EDGES_NAME))
    with pytest.raises(CorruptionError):
        Topology(out)


def test_manifest_version_and_format_guards(topo_dir):
    out, _ = topo_dir
    mp = os.path.join(out, tf.MANIFEST_NAME)
    m = json.load(open(mp, encoding="utf-8"))
    m["version"] = 999
    json.dump(m, open(mp, "w", encoding="utf-8"))
    with pytest.raises(UnsupportedVersionError):
        Topology(out)
    m = json.load(open(mp, encoding="utf-8"))
    m["version"] = tf.TOPO_FORMAT_VERSION
    m["format"] = "unknown-format"
    json.dump(m, open(mp, "w", encoding="utf-8"))
    with pytest.raises(CorruptionError):
        Topology(out)


def test_manifest_count_mismatch_rejected(topo_dir):
    out, _ = topo_dir
    mp = os.path.join(out, tf.MANIFEST_NAME)
    m = json.load(open(mp, encoding="utf-8"))
    m["counts"]["nodes"] += 1
    json.dump(m, open(mp, "w", encoding="utf-8"))
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


# ------------------------------------------------------- nodes.tsv damage
def _read_lines(out, name):
    with open(os.path.join(out, name), "rb") as fh:
        return fh.read().split(b"\n")


def test_duplicate_node_key_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.NODES_NAME)
    data = open(p, "rb").read()
    header, body = data[:len(tf.NODES_HEADER_BYTES)], \
        data[len(tf.NODES_HEADER_BYTES):]
    rows = [r for r in body.split(b"\n") if r]
    rows.insert(2, rows[1])          # duplicate an existing node row
    open(p, "wb").write(header + b"\n".join(rows) + b"\n")
    _repin(out, tf.NODES_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            list(t.iter_nodes())


def test_degree_mismatch_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.NODES_NAME)
    lines = _read_lines(out, tf.NODES_NAME)
    # first data row is lines[2]; bump its degree field
    fields = lines[2].split(b"\t")
    fields[3] = b"99"
    lines[2] = b"\t".join(fields)
    open(p, "wb").write(b"\n".join(lines))
    _repin(out, tf.NODES_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


def test_component_declaration_error_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.NODES_NAME)
    lines = _read_lines(out, tf.NODES_NAME)
    fields = lines[2].split(b"\t")
    fields[5] = tf.component_key(9999).encode("utf-8")
    lines[2] = b"\t".join(fields)
    open(p, "wb").write(b"\n".join(lines))
    _repin(out, tf.NODES_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


def test_non_canonical_node_encoding_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.NODES_NAME)
    data = open(p, "rb").read()
    # rewrite 0.0 as 0 (same value, non-canonical spelling)
    tampered = data.replace(b"\t0.0\t", b"\t0\t", 1)
    open(p, "wb").write(tampered)
    _repin(out, tf.NODES_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            list(t.iter_nodes())


# ------------------------------------------------------- edges.tsv damage
def test_dangling_node_reference_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.EDGES_NAME)
    lines = _read_lines(out, tf.EDGES_NAME)
    fields = lines[2].split(b"\t")
    fields[3] = tf.node_key(7777).encode("utf-8")   # nonexistent from-node
    lines[2] = b"\t".join(fields)
    open(p, "wb").write(b"\n".join(lines))
    _repin(out, tf.EDGES_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


def test_duplicate_edge_identity_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.EDGES_NAME)
    data = open(p, "rb").read()
    header, body = data[:len(tf.EDGES_HEADER_BYTES)], \
        data[len(tf.EDGES_HEADER_BYTES):]
    rows = [r for r in body.split(b"\n") if r]
    rows.append(rows[0])             # duplicate (source, part) identity
    open(p, "wb").write(header + b"\n".join(rows) + b"\n")
    _repin(out, tf.EDGES_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


def test_edge_endpoint_disagrees_with_geometry(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.EDGES_NAME)
    lines = _read_lines(out, tf.EDGES_NAME)
    fields = lines[2].split(b"\t")
    # point the from-node at a different existing node key
    fields[3] = tf.node_key(2).encode("utf-8")
    lines[2] = b"\t".join(fields)
    open(p, "wb").write(b"\n".join(lines))
    _repin(out, tf.EDGES_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


def test_edge_length_mismatch_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.EDGES_NAME)
    lines = _read_lines(out, tf.EDGES_NAME)
    fields = lines[2].split(b"\t")
    fields[6] = b"3.14159"
    lines[2] = b"\t".join(fields)
    open(p, "wb").write(b"\n".join(lines))
    _repin(out, tf.EDGES_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


def test_edge_order_violation_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.EDGES_NAME)
    data = open(p, "rb").read()
    header, body = data[:len(tf.EDGES_HEADER_BYTES)], \
        data[len(tf.EDGES_HEADER_BYTES):]
    rows = [r for r in body.split(b"\n") if r]
    rows = [rows[-1]] + rows[:-1]   # move last row to front
    open(p, "wb").write(header + b"\n".join(rows) + b"\n")
    _repin(out, tf.EDGES_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            list(t.iter_edges())


def test_bad_geometry_wkt_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.EDGES_NAME)
    lines = _read_lines(out, tf.EDGES_NAME)
    fields = lines[2].split(b"\t")
    fields[5] = b"POINT(0 0)"
    lines[2] = b"\t".join(fields)
    open(p, "wb").write(b"\n".join(lines))
    _repin(out, tf.EDGES_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            list(t.iter_edges())


# ----------------------------------------------------- selection/report
def test_selection_count_mismatch_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.SELECTION_NAME)
    with open(p, "ab") as fh:
        fh.write(json.dumps({"object_id": "ghost",
                             "reason": "excluded_by_filter",
                             "geometry_type": "LineString"})
                 .encode("utf-8") + b"\n")
    _repin(out, tf.SELECTION_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


def test_report_field_missing_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.REPORT_NAME)
    report = json.load(open(p, encoding="utf-8"))
    del report["peak_rss_bytes"]
    json.dump(report, open(p, "w", encoding="utf-8"))
    _repin(out, tf.REPORT_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


def test_report_physical_truncation_rejected_at_open(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.REPORT_NAME)
    # do NOT repin: the manifest must catch the length change at open time
    os.truncate(p, max(1, os.path.getsize(p) // 2))
    with pytest.raises(CorruptionError):
        Topology(out)


def test_report_count_disagreement_rejected(topo_dir):
    out, _ = topo_dir
    p = os.path.join(out, tf.REPORT_NAME)
    report = json.load(open(p, encoding="utf-8"))
    report["nodes"] += 1
    json.dump(report, open(p, "w", encoding="utf-8"))
    _repin(out, tf.REPORT_NAME)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=False)


def test_selection_physical_truncation_rejected_at_open(tmp_path):
    # needs a non-empty selection file, so build with an id allowlist that
    # excludes one record.
    recs = [
        {"object_id": "keep", "geometry": linestring([[0, 0], [1, 0]])},
        {"object_id": "drop", "geometry": linestring([[4, 4], [5, 4]])},
    ]
    src = write_records(tmp_path / "in.jsonl", recs)
    cat = str(tmp_path / "cat")
    build_catalog(src, cat, cache_bytes=1 << 16, force=True)
    ids = tmp_path / "ids.txt"
    ids.write_text("keep\n")
    out = str(tmp_path / "topo")
    build_topology(cat, out, selection=Selection(ids_file=str(ids)),
                   cache_bytes=1 << 16, force=True)
    p = os.path.join(out, tf.SELECTION_NAME)
    assert os.path.getsize(p) > 0
    os.truncate(p, os.path.getsize(p) - 1)
    with pytest.raises(CorruptionError):
        Topology(out)


# ----------------------------------------------------- source re-identity
def test_source_identity_drift_detected(tmp_path, topo_dir):
    out, _cat = topo_dir
    new_src = write_records(tmp_path / "new.jsonl", [
        {"object_id": "totally-different",
         "geometry": linestring([[0, 0], [1, 1]])}])
    new_cat = str(tmp_path / "newcat")
    build_catalog(new_src, new_cat, cache_bytes=1 << 16, force=True)
    with Topology(out) as t:
        with pytest.raises(CorruptionError):
            t.validate_full(catalog=new_cat)


def test_unreachable_source_reported_not_fatal(topo_dir):
    out, _cat = topo_dir
    with Topology(out) as t:
        summary = t.validate_full(catalog="/no/such/catalog")
    assert summary["ok"]
    assert summary["source_catalog"].startswith("unreachable")


def test_clean_validation_passes_and_is_independent(topo_dir):
    out, cat = topo_dir
    with Topology(out) as t:
        summary = t.validate_full(catalog=cat)
    assert summary["ok"]
    # a,b,c chain (4 nodes) plus the two z-part nodes (one shared self-loop
    # node at (9,9) and (8,8)) -> 6 nodes, two connected components
    assert summary["nodes"] == 6
    assert summary["components"] == 2
