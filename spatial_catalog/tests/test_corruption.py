"""Corruption, truncation, illegal references, and half-built catalogs
must fail loudly -- never silently miss objects."""

import json
import os
import struct

import pytest

from spatial_catalog import format as fmt
from spatial_catalog.catalog import Catalog
from spatial_catalog.errors import (CorruptionError, IllegalReferenceError,
                                    NotACatalogError, UnsupportedVersionError)
from helpers import build_catalog, open_catalog, point, square


@pytest.fixture
def cat_dir(tmp_path):
    records = [{"object_id": "p%03d" % i,
                "geometry": point(float(i % 37), float(i % 53))}
               for i in range(300)]
    records.append({"object_id": "poly", "geometry": square(100, 100, 110, 110)})
    out, _ = build_catalog(tmp_path, records, page_size=512)
    return out


def test_missing_manifest_is_not_a_catalog(tmp_path, cat_dir):
    os.unlink(os.path.join(cat_dir, "manifest.json"))
    with pytest.raises(NotACatalogError):
        Catalog(cat_dir)


def test_half_built_directory_is_not_a_catalog(tmp_path):
    # nodes/payloads exist but no manifest (build interrupted)
    d = tmp_path / "half"
    d.mkdir()
    (d / "nodes.bin").write_bytes(b"\x00" * 100)
    (d / "payloads.bin").write_bytes(b"\x00" * 100)
    with pytest.raises(NotACatalogError):
        Catalog(str(d))


def test_truncated_nodes_file_detected(tmp_path, cat_dir):
    p = os.path.join(cat_dir, "nodes.bin")
    size = os.path.getsize(p)
    os.truncate(p, size - 100)
    with pytest.raises(CorruptionError):
        Catalog(cat_dir)


def test_truncated_payloads_file_detected(tmp_path, cat_dir):
    p = os.path.join(cat_dir, "payloads.bin")
    size = os.path.getsize(p)
    os.truncate(p, size - 5)
    with pytest.raises(CorruptionError):
        Catalog(cat_dir)


def _flip_byte(path, offset):
    with open(path, "r+b") as fh:
        fh.seek(offset)
        b = fh.read(1)
        fh.seek(offset)
        fh.write(bytes([b[0] ^ 0xFF]))


def test_flipped_node_page_byte_detected_on_query(tmp_path, cat_dir):
    # corrupt one byte inside the root page (last page)
    manifest = json.load(open(os.path.join(cat_dir, "manifest.json")))
    root = manifest["root_page"]
    page_size = manifest["page_size"]
    offset = fmt.NODES_HEADER_SIZE + root * page_size + 20
    _flip_byte(os.path.join(cat_dir, "nodes.bin"), offset)
    with open_catalog(cat_dir) as cat:
        with pytest.raises(CorruptionError):
            list(cat.window(-1, -1, 200, 200))


def test_flipped_payload_byte_detected_on_query(tmp_path, cat_dir):
    # corrupt a byte well inside the payload data region
    _flip_byte(os.path.join(cat_dir, "payloads.bin"),
               fmt.PAYLOADS_HEADER_SIZE + 15)
    with open_catalog(cat_dir) as cat:
        with pytest.raises(CorruptionError):
            list(cat.window(-1, -1, 200, 200))


def test_full_validate_detects_payload_corruption(tmp_path, cat_dir):
    _flip_byte(os.path.join(cat_dir, "payloads.bin"),
               fmt.PAYLOADS_HEADER_SIZE + 15)
    with open_catalog(cat_dir) as cat:
        with pytest.raises(CorruptionError):
            cat.validate_full()


def test_manifest_version_rejected(tmp_path, cat_dir):
    mpath = os.path.join(cat_dir, "manifest.json")
    manifest = json.load(open(mpath))
    manifest["version"] = 999
    json.dump(manifest, open(mpath, "w"))
    with pytest.raises(UnsupportedVersionError):
        Catalog(cat_dir)


def test_manifest_garbage_rejected(tmp_path, cat_dir):
    with open(os.path.join(cat_dir, "manifest.json"), "w") as fh:
        fh.write("{not json")
    with pytest.raises(CorruptionError):
        Catalog(cat_dir)


def test_illegal_child_page_reference(tmp_path, cat_dir):
    """Rewrite the root page with a child pointer beyond the file (CRC
    recomputed so the page itself is 'valid'): the query must raise
    IllegalReferenceError, not read garbage."""
    manifest = json.load(open(os.path.join(cat_dir, "manifest.json")))
    root = manifest["root_page"]
    page_size = manifest["page_size"]
    nodes = os.path.join(cat_dir, "nodes.bin")
    with open(nodes, "rb") as fh:
        fh.seek(fmt.NODES_HEADER_SIZE + root * page_size)
        buf = fh.read(page_size)
    kind, entries = fmt.unpack_page(buf, page_size)
    assert kind == fmt.PAGE_KIND_INTERNAL  # 300 objects @ page_size 512
    bad = (entries[0][0], entries[0][1], entries[0][2], entries[0][3],
             10 ** 9)
    page = fmt.pack_internal_page([bad] + entries[1:], page_size)
    with open(nodes, "r+b") as fh:
        fh.seek(fmt.NODES_HEADER_SIZE + root * page_size)
        fh.write(page)
    with open_catalog(cat_dir) as cat:
        with pytest.raises(IllegalReferenceError):
            list(cat.window(-1, -1, 200, 200))


def test_illegal_payload_reference(tmp_path, cat_dir):
    """Point a leaf entry's payload offset beyond payloads.bin."""
    manifest = json.load(open(os.path.join(cat_dir, "manifest.json")))
    page_size = manifest["page_size"]
    nodes = os.path.join(cat_dir, "nodes.bin")
    # find a leaf page and corrupt its first entry's payload offset
    with open(nodes, "rb") as fh:
        head = fmt.unpack_nodes_header(fh.read(fmt.NODES_HEADER_SIZE))
    target = None
    with open(nodes, "rb") as fh:
        for pg in range(head["page_count"]):
            fh.seek(fmt.NODES_HEADER_SIZE + pg * page_size)
            buf = fh.read(page_size)
            kind, entries = fmt.unpack_page(buf, page_size)
            if kind == fmt.PAGE_KIND_LEAF and entries:
                target = (pg, entries)
                break
    assert target is not None
    pg, entries = target
    e = entries[0]
    bad = (e[0], e[1], e[2], e[3], 10 ** 12, e[5])
    page = fmt.pack_leaf_page([bad] + entries[1:], page_size)
    with open(nodes, "r+b") as fh:
        fh.seek(fmt.NODES_HEADER_SIZE + pg * page_size)
        fh.write(page)
    with open_catalog(cat_dir) as cat:
        with pytest.raises(IllegalReferenceError):
            list(cat.window(-1, -1, 200, 200))


def test_validate_full_ok(tmp_path, cat_dir):
    with open_catalog(cat_dir) as cat:
        summary = cat.validate_full()
    assert summary["ok"]
    assert summary["objects"] == 301


def test_verify_on_open(tmp_path, cat_dir):
    with open_catalog(cat_dir, verify=True):
        pass
