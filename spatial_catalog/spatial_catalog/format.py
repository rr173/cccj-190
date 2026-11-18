"""On-disk format constants and codecs.

Layout
------
A catalog directory holds exactly three files:

  manifest.json   -- written LAST (atomic rename).  Absence => not a catalog.
  nodes.bin       -- header + fixed-size pages (the packed tree).
  payloads.bin    -- header + variable-length records (id + WKB geometry).

Tree nodes and geometry payloads live in separate files; a query only
decodes a payload when the tree walk actually reaches that leaf entry.

Every header, every node page and every payload record carries a CRC32,
and the manifest pins each file's exact length (plus SHA-256 for full
validation).  Truncation, bit flips and illegal references are therefore
detected on read, never silently ignored.
"""

import json
import struct
import zlib

from .errors import CorruptionError, UnsupportedVersionError

FORMAT_NAME = "offline-spatial-catalog"
FORMAT_VERSION = 1

NODES_MAGIC = b"SPCNOD01"
PAYLOADS_MAGIC = b"SPCPAY01"

DEFAULT_PAGE_SIZE = 4096
MIN_PAGE_SIZE = 512
MAX_PAGE_SIZE = 1 << 16

# Per-geometry payload (WKB) hard upper bound, overridable at build time.
DEFAULT_MAX_PAYLOAD_BYTES = 1 << 20          # 1 MiB
# object_id UTF-8 length bound (format hard limit is 65535 by u16 field).
MAX_ID_BYTES = 4096
# Coordinate magnitude bound: inputs are promised bounded; we enforce it.
DEFAULT_MAX_COORD = 1e15

PAGE_KIND_LEAF = 0
PAGE_KIND_INTERNAL = 1

# ---------------------------------------------------------------- nodes.bin

# magic(8) version(u32) page_size(u32) page_count(u64) depth(u32)
# root_page(u64) entry_count(u64) header_crc(u32)
NODES_HEADER = struct.Struct(">8sIIQIQQI")
NODES_HEADER_SIZE = NODES_HEADER.size

# page: crc(u32) kind(u8) count(u16) entries...
PAGE_HEADER = struct.Struct(">IBH")
PAGE_HEADER_SIZE = PAGE_HEADER.size

LEAF_ENTRY = struct.Struct(">4dQI")   # bbox(4 f64) payload_off(u64) payload_len(u32)
LEAF_ENTRY_SIZE = LEAF_ENTRY.size     # 44
INTERNAL_ENTRY = struct.Struct(">4dQ")  # bbox(4 f64) child_page(u64)
INTERNAL_ENTRY_SIZE = INTERNAL_ENTRY.size  # 40


def leaf_capacity(page_size):
    return (page_size - PAGE_HEADER_SIZE) // LEAF_ENTRY_SIZE


def internal_capacity(page_size):
    return (page_size - PAGE_HEADER_SIZE) // INTERNAL_ENTRY_SIZE


def crc32(data, value=0):
    return zlib.crc32(data, value) & 0xFFFFFFFF


def pack_nodes_header(page_size, page_count, depth, root_page, entry_count):
    body = NODES_HEADER.pack(
        NODES_MAGIC, FORMAT_VERSION, page_size, page_count, depth,
        root_page, entry_count, 0)
    crc = crc32(body[:-4])
    return body[:-4] + struct.pack(">I", crc)


def unpack_nodes_header(buf):
    if len(buf) < NODES_HEADER_SIZE:
        raise CorruptionError(
            "nodes.bin header truncated: %d of %d bytes" % (len(buf), NODES_HEADER_SIZE))
    magic, version, page_size, page_count, depth, root_page, entry_count, crc = \
        NODES_HEADER.unpack(buf[:NODES_HEADER_SIZE])
    if magic != NODES_MAGIC:
        raise CorruptionError("nodes.bin: bad magic %r" % (magic,))
    if crc32(buf[:NODES_HEADER_SIZE - 4]) != crc:
        raise CorruptionError("nodes.bin: header CRC mismatch")
    if version != FORMAT_VERSION:
        raise UnsupportedVersionError("nodes.bin version %d unsupported" % version)
    return {
        "page_size": page_size, "page_count": page_count, "depth": depth,
        "root_page": root_page, "entry_count": entry_count,
    }


def pack_leaf_page(entries, page_size):
    """entries: iterable of (minx, miny, maxx, maxy, payload_off, payload_len)."""
    entries = list(entries)
    cap = leaf_capacity(page_size)
    # Zero entries is permitted only for the empty-catalog root page.
    if not (0 <= len(entries) <= cap):
        raise ValueError("leaf page entry count %d out of range 0..%d" % (len(entries), cap))
    body = bytearray()
    body += PAGE_HEADER.pack(0, PAGE_KIND_LEAF, len(entries))
    for e in entries:
        body += LEAF_ENTRY.pack(*e)
    body += b"\x00" * (page_size - len(body))
    crc = crc32(bytes(body[4:]))
    struct.pack_into(">I", body, 0, crc)
    return bytes(body)


def pack_internal_page(entries, page_size):
    """entries: iterable of (minx, miny, maxx, maxy, child_page)."""
    entries = list(entries)
    cap = internal_capacity(page_size)
    if not (0 < len(entries) <= cap):
        raise ValueError("internal page entry count %d out of range" % len(entries))
    body = bytearray()
    body += PAGE_HEADER.pack(0, PAGE_KIND_INTERNAL, len(entries))
    for e in entries:
        body += INTERNAL_ENTRY.pack(*e)
    body += b"\x00" * (page_size - len(body))
    crc = crc32(bytes(body[4:]))
    struct.pack_into(">I", body, 0, crc)
    return bytes(body)


def unpack_page(buf, page_size):
    """Returns (kind, entries).  entries are raw tuples; CRC is verified."""
    if len(buf) != page_size:
        raise CorruptionError(
            "node page truncated: %d of %d bytes" % (len(buf), page_size))
    crc, kind, count = PAGE_HEADER.unpack(buf[:PAGE_HEADER_SIZE])
    if crc32(buf[4:]) != crc:
        raise CorruptionError("node page CRC mismatch")
    if kind == PAGE_KIND_LEAF:
        size, cap = LEAF_ENTRY_SIZE, leaf_capacity(page_size)
    elif kind == PAGE_KIND_INTERNAL:
        size, cap = INTERNAL_ENTRY_SIZE, internal_capacity(page_size)
    else:
        raise CorruptionError("node page: unknown kind %d" % kind)
    if count > cap:
        raise CorruptionError("node page: count %d exceeds capacity %d" % (count, cap))
    need = PAGE_HEADER_SIZE + count * size
    if need > page_size:
        raise CorruptionError("node page: entries overrun page")
    entries = []
    off = PAGE_HEADER_SIZE
    for _ in range(count):
        entries.append(struct.unpack_from(">4dQI" if kind == PAGE_KIND_LEAF else ">4dQ",
                                          buf, off))
        off += size
    return kind, entries


# ------------------------------------------------------------ payloads.bin

# magic(8) version(u32) record_count(u64) file_length(u64) header_crc(u32)
PAYLOADS_HEADER = struct.Struct(">8sIQQI")
PAYLOADS_HEADER_SIZE = PAYLOADS_HEADER.size

# record: crc(u32) id_len(u16) wkb_len(u32) id_bytes wkb_bytes
RECORD_HEADER = struct.Struct(">IHI")
RECORD_HEADER_SIZE = RECORD_HEADER.size


def pack_payloads_header(record_count, file_length):
    body = PAYLOADS_HEADER.pack(PAYLOADS_MAGIC, FORMAT_VERSION, record_count,
                                file_length, 0)
    return body[:-4] + struct.pack(">I", crc32(body[:-4]))


def unpack_payloads_header(buf):
    if len(buf) < PAYLOADS_HEADER_SIZE:
        raise CorruptionError("payloads.bin header truncated")
    magic, version, record_count, file_length, crc = PAYLOADS_HEADER.unpack(
        buf[:PAYLOADS_HEADER_SIZE])
    if magic != PAYLOADS_MAGIC:
        raise CorruptionError("payloads.bin: bad magic %r" % (magic,))
    if crc32(buf[:PAYLOADS_HEADER_SIZE - 4]) != crc:
        raise CorruptionError("payloads.bin: header CRC mismatch")
    if version != FORMAT_VERSION:
        raise UnsupportedVersionError("payloads.bin version %d unsupported" % version)
    return {"record_count": record_count, "file_length": file_length}


def pack_payload_record(obj_id, wkb):
    id_bytes = obj_id.encode("utf-8")
    if len(id_bytes) > 0xFFFF:
        raise ValueError("object_id too long")
    body = struct.pack(">HI", len(id_bytes), len(wkb)) + id_bytes + wkb
    return struct.pack(">I", crc32(body)) + body


def unpack_payload_record(rec):
    """Decode one record's exact bytes; CRC and length fields verified.

    Range-checking the record's file offset is the caller's job (the
    catalog reader validates every reference against the file length
    before reading).
    """
    if len(rec) < RECORD_HEADER_SIZE:
        raise CorruptionError("payload record truncated: %d bytes" % len(rec))
    crc, id_len, wkb_len = RECORD_HEADER.unpack(rec[:RECORD_HEADER_SIZE])
    if crc32(rec[4:]) != crc:
        raise CorruptionError("payload record CRC mismatch")
    if len(rec) != RECORD_HEADER_SIZE + id_len + wkb_len:
        raise CorruptionError("payload record length fields inconsistent")
    id_bytes = rec[RECORD_HEADER_SIZE:RECORD_HEADER_SIZE + id_len]
    wkb = rec[RECORD_HEADER_SIZE + id_len:]
    return id_bytes.decode("utf-8"), wkb


# --------------------------------------------------------------- manifest

MANIFEST_NAME = "manifest.json"
NODES_NAME = "nodes.bin"
PAYLOADS_NAME = "payloads.bin"


def dump_manifest(path, manifest):
    """Write manifest atomically: tmp file + rename.  Always the LAST write."""
    import os
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_manifest(path):
    import os
    if not os.path.isfile(path):
        from .errors import NotACatalogError
        raise NotACatalogError(
            "no manifest at %s -- not a committed catalog "
            "(missing, half-built, or failed build)" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CorruptionError("manifest unreadable: %s" % exc)
    if manifest.get("format") != FORMAT_NAME:
        raise CorruptionError("manifest: unknown format %r" % manifest.get("format"))
    if manifest.get("version") != FORMAT_VERSION:
        raise UnsupportedVersionError(
            "catalog version %r unsupported" % manifest.get("version"))
    return manifest
