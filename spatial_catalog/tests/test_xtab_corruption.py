"""Cross-table open/validate: truncation, hash damage, non-canonical
numeric encodings, ordering and uniqueness violations, header damage,
manifest tampering, and input-catalog re-identity drift."""

import json
import os

import pytest

from spatial_catalog import xformat as xf
from spatial_catalog.builder import build
from spatial_catalog.errors import (CorruptionError, NotACatalogError,
                                    UnsupportedVersionError)
from spatial_catalog.xtab import build_crosstab
from spatial_catalog.xreader import XTable
from helpers import linestring, square, write_records


def _repin_length(out):
    """After deliberately mutating rows.tsv, repin the manifest length so
    the open-time boundary check passes and the deeper grammar/order/hash
    validation is what gets exercised."""
    mp = os.path.join(out, xf.MANIFEST_NAME)
    m = json.load(open(mp))
    m["files"][xf.ROWS_NAME]["length"] = os.path.getsize(
        os.path.join(out, xf.ROWS_NAME))
    json.dump(m, open(mp, "w"))


@pytest.fixture
def xtab_dir(tmp_path):
    lines = [
        {"object_id": "L1", "geometry": linestring([[0, 1], [10, 1]])},
        {"object_id": "L2", "geometry": linestring([[5, -5], [5, 5]])},
    ]
    zones = [
        {"object_id": "A", "geometry": square(2, 0, 6, 4)},
        {"object_id": "B", "geometry": square(8, 0, 12, 4)},
    ]
    lsrc = write_records(tmp_path / "l.jsonl", lines)
    zsrc = write_records(tmp_path / "z.jsonl", zones)
    lc, zc = str(tmp_path / "l.cat"), str(tmp_path / "z.cat")
    build(lsrc, lc, cache_bytes=1 << 20, force=True)
    build(zsrc, zc, cache_bytes=1 << 20, force=True)
    out = str(tmp_path / "xt")
    build_crosstab(lc, zc, out, cache_bytes=1 << 20, force=True)
    return out, lc, zc


def test_open_checks_manifest_boundary(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    # truncate rows.tsv: open must reject against the pinned length
    p = os.path.join(out, xf.ROWS_NAME)
    os.truncate(p, os.path.getsize(p) - 3)
    with pytest.raises(CorruptionError):
        XTable(out)


def test_missing_manifest_is_not_a_crosstab(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    os.unlink(os.path.join(out, xf.MANIFEST_NAME))
    with pytest.raises(NotACatalogError):
        XTable(out)


def test_half_built_directory_rejected(tmp_path):
    d = tmp_path / "half"
    d.mkdir()
    (d / xf.ROWS_NAME).write_bytes(xf.HEADER_BYTES)
    with pytest.raises(NotACatalogError):
        XTable(str(d))


def test_hash_corruption_detected_by_validate(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    p = os.path.join(out, xf.ROWS_NAME)
    with open(p, "r+b") as fh:
        fh.seek(len(xf.HEADER_BYTES) + 5)
        b = fh.read(1)
        fh.seek(len(xf.HEADER_BYTES) + 5)
        fh.write(bytes([b[0] ^ 0x01]))
    with XTable(out) as xt:
        with pytest.raises(CorruptionError):
            xt.validate_full(line_catalog=False, zone_catalog=False)


def test_truncation_detected_by_validate(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    p = os.path.join(out, xf.ROWS_NAME)
    size = os.path.getsize(p)
    os.truncate(p, size - 1)
    with pytest.raises(CorruptionError):
        XTable(out)  # length mismatch caught at open


def test_damaged_header_line_rejected(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    p = os.path.join(out, xf.ROWS_NAME)
    with open(p, "r+b") as fh:
        fh.seek(2)
        fh.write(b"X")
    with XTable(out) as xt:
        with pytest.raises(CorruptionError):
            list(xt.iter_rows())


def test_non_canonical_numeric_spelling_rejected(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    p = os.path.join(out, xf.ROWS_NAME)
    data = open(p, "rb").read()
    lines = data.split(b"\n")
    # rewrite 4.0 as 4 (same value, non-canonical spelling)
    for i, ln in enumerate(lines):
        if ln.startswith(b"L1\tA\t4.0"):
            lines[i] = ln.replace(b"4.0", b"4", 1)
            break
    open(p, "wb").write(b"\n".join(lines))
    _repin_length(out)
    with XTable(out) as xt:
        with pytest.raises(CorruptionError):
            list(xt.iter_rows())


def test_wrong_field_count_rejected(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    p = os.path.join(out, xf.ROWS_NAME)
    with open(p, "ab") as fh:
        fh.write(b"only\ttwo\tfields\n")
    _repin_length(out)
    with XTable(out) as xt:
        with pytest.raises(CorruptionError):
            list(xt.iter_rows())


def test_out_of_order_rows_rejected(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    p = os.path.join(out, xf.ROWS_NAME)
    body = open(p, "rb").read()
    header, rest = body[:len(xf.HEADER_BYTES)], body[len(xf.HEADER_BYTES):]
    rows = [r for r in rest.split(b"\n") if r]
    # move the last row to the front: guaranteed code-point inversion
    rows = [rows[-1]] + rows[:-1]
    open(p, "wb").write(header + b"\n".join(rows) + b"\n")
    _repin_length(out)
    with XTable(out) as xt:
        with pytest.raises(CorruptionError):
            list(xt.iter_rows())


def test_duplicate_pair_rejected(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    p = os.path.join(out, xf.ROWS_NAME)
    body = open(p, "rb").read()
    header, rest = body[:len(xf.HEADER_BYTES)], body[len(xf.HEADER_BYTES):]
    rows = [r for r in rest.split(b"\n") if r]
    # prepend the last row as a duplicate of an existing (source, zone)
    rows = [rows[-1]] + rows
    open(p, "wb").write(header + b"\n".join(rows) + b"\n")
    _repin_length(out)
    with XTable(out) as xt:
        with pytest.raises(CorruptionError):
            list(xt.iter_rows())


def test_ratio_mismatch_rejected(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    p = os.path.join(out, xf.ROWS_NAME)
    body = open(p, "rb").read()
    # tamper the ratio field of the first data row
    header_end = len(xf.HEADER_BYTES)
    nl = body.index(b"\n", header_end)
    row = body[header_end:nl].split(b"\t")
    row[-1] = b"0.123456789"
    tampered = b"\t".join(row)
    new = body[:header_end] + tampered + body[nl:]
    open(p, "wb").write(new)
    _repin_length(out)
    with XTable(out) as xt:
        with pytest.raises(CorruptionError):
            list(xt.iter_rows())


def test_manifest_version_guard(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    mp = os.path.join(out, xf.MANIFEST_NAME)
    m = json.load(open(mp))
    m["version"] = 999
    json.dump(m, open(mp, "w"))
    with pytest.raises(UnsupportedVersionError):
        XTable(out)


def test_manifest_format_guard(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    mp = os.path.join(out, xf.MANIFEST_NAME)
    m = json.load(open(mp))
    m["format"] = "something-else"
    json.dump(m, open(mp, "w"))
    with pytest.raises(CorruptionError):
        XTable(out)


def test_record_count_mismatch_rejected(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    mp = os.path.join(out, xf.MANIFEST_NAME)
    m = json.load(open(mp))
    m["record_count"] += 1
    json.dump(m, open(mp, "w"))
    with XTable(out) as xt:
        with pytest.raises(CorruptionError):
            xt.validate_full(line_catalog=False, zone_catalog=False)


def test_input_identity_drift_detected(tmp_path, xtab_dir):
    out, lc, zc = xtab_dir
    # rebuild the line catalog from changed content -> new identity
    new_lines = [{"object_id": "L9",
                  "geometry": linestring([[0, 0], [3, 3]])}]
    write_records(os.path.join(tmp_path, "newl.jsonl"), new_lines)
    build(str(tmp_path / "newl.jsonl"), lc,
          cache_bytes=1 << 20, force=True)
    with XTable(out) as xt:
        with pytest.raises(CorruptionError):
            xt.validate_full(line_catalog=lc, zone_catalog=zc)


def test_input_identity_match_passes(tmp_path, xtab_dir):
    out, lc, zc = xtab_dir
    with XTable(out) as xt:
        summary = xt.validate_full(line_catalog=lc, zone_catalog=zc)
    assert summary["ok"]
    assert summary["input_identities"]["line"]
    assert summary["input_identities"]["zone"]


def test_unreachable_input_is_reported_not_fatal(tmp_path, xtab_dir):
    out, _lc, _zc = xtab_dir
    with XTable(out) as xt:
        summary = xt.validate_full(
            line_catalog=str(tmp_path / "gone"),
            zone_catalog=str(tmp_path / "also-gone"))
    assert summary["ok"]
    assert summary["input_identities"]["line"].startswith("unreachable")
