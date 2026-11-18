"""On-disk format for the line-facility / zone-boundary cross-table.

A cross-table directory is an independent, versioned, immutable artifact:

  xtab_manifest.json -- written LAST (atomic rename).  Absence => not a
                        committed cross-table, exactly like a base catalog.
  rows.tsv           -- deterministic UTF-8 rows, one positive-length
                        (source_id, zone_id) pair per line.
  build_report.json  -- measured build diagnostics (counters, spills, RSS).

The rows file starts with two fixed header lines (a versioned magic line
and a column-name line); every data row is::

    source_id<TAB>zone_id<TAB>intersect_length<TAB>source_length<TAB>ratio<LF>

Ordering is the Unicode code point order of (source_id, zone_id), which
for UTF-8 is identical to raw byte order -- so the on-disk sort key is
just the encoded id bytes and the ordering is locale independent.

Floats use Python's ``repr``: the shortest string that round-trips the
exact float64 bit pattern, independent of process locale.  ``ratio`` is
stored as the same float64 value that validation re-derives as
``intersect_length / source_length``; ``validate`` enforces bit equality.

Spill records (external sort runs) use a fixed binary layout, never the
TSV encoding, so no escaping layer sits on the hot candidate path.
"""

import hashlib
import json
import os
import struct

from .errors import CorruptionError, CrossTableError, UnsupportedVersionError

XTABLE_FORMAT_NAME = "offline-line-zone-crosstab"
XTABLE_FORMAT_VERSION = 1

MANIFEST_NAME = "xtab_manifest.json"
ROWS_NAME = "rows.tsv"
REPORT_NAME = "build_report.json"

MAGIC_LINE = "# spatial-catalog line-zone crosstab v1\n"
COLUMNS_LINE = "# source_id\tzone_id\tintersect_length\tsource_length\trat\n"
HEADER_BYTES = (MAGIC_LINE + COLUMNS_LINE).encode("utf-8")
ROW_FIELDS = ("source_id", "zone_id", "intersect_length",
              "source_length", "ratio")

# Binary spill record: sid_len(u16) sid zid_len(u16) zid 3*f64 BE.
_ROW_HEAD = struct.Struct(">HH")
_ROW_FLOATS = struct.Struct(">ddd")

# object ids in base catalogs are bounded to 4096 UTF-8 bytes; a TSV row
# additionally forbids tab/CR/LF so the row grammar stays unambiguous.
_FORBIDDEN_ID_CHARS = frozenset(("\t", "\r", "\n"))


class RowIdError(CrossTableError):
    """An id cannot be represented unambiguously as a TSV row field."""


def check_row_id(object_id, role, source_index=None):
    """Validate an id for TSV serialization; raise with locatable evidence."""
    if not isinstance(object_id, str) or object_id == "":
        raise RowIdError(
            "%s catalog %sid %r is not a non-empty string"
            % (role, ("#%d " % source_index) if source_index is not None else "",
               object_id), reason="bad_id",
            detail={"role": role, "index": source_index})
    bad = next((ch for ch in object_id if ch in _FORBIDDEN_ID_CHARS), None)
    if bad is not None:
        raise RowIdError(
            "%s catalog id %r contains a tab/newline character and cannot "
            "be written as a TSV row" % (role, object_id),
            reason="id_not_tsv_safe", detail={"role": role, "object_id": object_id})
    if len(object_id.encode("utf-8")) > 0xFFFF:
        raise RowIdError("%s catalog id %r exceeds 65535 UTF-8 bytes"
                         % (role, object_id), reason="id_too_long",
                         detail={"role": role, "object_id": object_id})


# ----------------------------------------------------------- binary records

def pack_row_record(source_id, zone_id, intersect_length, source_length, ratio):
    """Bare binary record used inside external-sort runs.

    external_sort adds its own u32 length prefix per record, so this
    layout must not include one."""
    sb = source_id.encode("utf-8")
    zb = zone_id.encode("utf-8")
    return (_ROW_HEAD.pack(len(sb), len(zb)) + sb + zb
            + _ROW_FLOATS.pack(intersect_length, source_length, ratio))


def row_record_key(rec):
    """External-sort key: raw UTF-8 bytes of (source_id, zone_id).

    UTF-8 byte order equals Unicode code point order, so this key gives
    the required deterministic id ordering with no decoding."""
    sn, zn = _ROW_HEAD.unpack_from(rec, 0)
    start = _ROW_HEAD.size
    sb = rec[start:start + sn]
    zb = rec[start + sn:start + sn + zn]
    return sb, zb


def unpack_row_record(rec):
    sn, zn = _ROW_HEAD.unpack_from(rec, 0)
    start = _ROW_HEAD.size
    source_id = rec[start:start + sn].decode("utf-8")
    zone_id = rec[start + sn:start + sn + zn].decode("utf-8")
    intersect_length, source_length, ratio = _ROW_FLOATS.unpack_from(
        rec, start + sn + zn)
    return source_id, zone_id, intersect_length, source_length, ratio


# ----------------------------------------------------------------- TSV rows

def encode_row_line(source_id, zone_id, intersect_length, source_length, ratio):
    """One deterministic UTF-8 TSV data row (including trailing LF)."""
    return ("%s\t%s\t%s\t%s\t%s\n" % (
        source_id, zone_id,
        repr(float(intersect_length)), repr(float(source_length)),
        repr(float(ratio)))).encode("utf-8")


def parse_row_line(line_bytes, line_no):
    """Parse/validate one data row; raise CorruptionError with line evidence."""
    if not line_bytes.endswith(b"\n") or b"\r" in line_bytes:
        raise CorruptionError(
            "rows.tsv line %d: missing LF or contains CR" % line_no)
    text = line_bytes[:-1].decode("utf-8")  # raises on bad UTF-8
    parts = text.split("\t")
    if len(parts) != 5:
        raise CorruptionError(
            "rows.tsv line %d: expected 5 tab-separated fields, got %d"
            % (line_no, len(parts)))
    source_id, zone_id, a, b, c = parts
    if not source_id or not zone_id:
        raise CorruptionError("rows.tsv line %d: empty id field" % line_no)
    try:
        intersect_length = float(a)
        source_length = float(b)
        ratio = float(c)
    except ValueError:
        raise CorruptionError(
            "rows.tsv line %d: non-numeric numeric field: %r" % (line_no, parts[2:]))
    # Reject alternate spellings (1.0 vs 1, whitespace, uppercase exponent,
    # locale artifacts): the on-disk representation must be canonical.
    canonical = encode_row_line(source_id, zone_id, intersect_length,
                                source_length, ratio)
    if canonical != line_bytes:
        raise CorruptionError(
            "rows.tsv line %d: non-canonical numeric encoding %r (expected %r)"
            % (line_no, line_bytes, canonical))
    import math
    for name, v in (("intersect_length", intersect_length),
                    ("source_length", source_length), ("ratio", ratio)):
        if not math.isfinite(v):
            raise CorruptionError(
                "rows.tsv line %d: %s is not finite" % (line_no, name))
    if not (intersect_length > 0.0):
        raise CorruptionError(
            "rows.tsv line %d: intersect_length must be positive, got %r"
            % (line_no, intersect_length))
    if not (source_length > 0.0):
        raise CorruptionError(
            "rows.tsv line %d: source_length must be positive, got %r"
            % (line_no, source_length))
    if intersect_length > source_length:
        raise CorruptionError(
            "rows.tsv line %d: intersect_length %r > source_length %r"
            % (line_no, intersect_length, source_length))
    if not (0.0 < ratio <= 1.0):
        raise CorruptionError(
            "rows.tsv line %d: ratio %r outside (0, 1]" % (line_no, ratio))
    # The stored ratio must be exactly the float64 quotient of the lengths.
    derived = intersect_length / source_length
    if derived != ratio:
        raise CorruptionError(
            "rows.tsv line %d: stored ratio %r != intersect/source %r"
            % (line_no, ratio, derived))
    return source_id, zone_id, intersect_length, source_length, ratio


def sha256_file(path):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
    return n, h.hexdigest()


# --------------------------------------------------------------- manifest

def catalog_identity(catalog_path, manifest):
    """Content identity of an input catalog, derived solely from its
    manifest (which already pins both files' lengths and SHA-256)."""
    files = manifest["files"]
    identity = {
        "catalog_path": os.path.abspath(catalog_path),
        "format": manifest["format"],
        "version": manifest["version"],
        "object_count": manifest["object_count"],
        "manifest_build_id": manifest.get("build_id"),
        "nodes_sha256": files["nodes.bin"]["sha256"],
        "nodes_length": files["nodes.bin"]["length"],
        "payloads_sha256": files["payloads.bin"]["sha256"],
        "payloads_length": files["payloads.bin"]["length"],
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    identity["identity_digest"] = digest.hexdigest()
    return identity


def dump_manifest(path, manifest):
    """Atomic manifest write; always the last action of a build."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_manifest(path):
    if not os.path.isfile(path):
        from .errors import NotACatalogError
        raise NotACatalogError(
            "no cross-table manifest at %s -- not a committed cross-table "
            "(missing, half-built, or failed build)" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CorruptionError("cross-table manifest unreadable: %s" % exc)
    if manifest.get("format") != XTABLE_FORMAT_NAME:
        raise CorruptionError(
            "manifest: unknown cross-table format %r" % manifest.get("format"))
    if manifest.get("version") != XTABLE_FORMAT_VERSION:
        raise UnsupportedVersionError(
            "cross-table version %r unsupported" % manifest.get("version"))
    return manifest
