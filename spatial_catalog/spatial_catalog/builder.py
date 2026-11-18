"""Catalog builder.

Pipeline (all stages stream; nothing holds the whole dataset in memory):

  pass 1  validate each JSONL record, append payload (id + WKB) to
          payloads.bin, append a fixed-layout entry (id, line, bbox,
          payload ref) to a temp entries file.  Rejects are streamed to
          rejects.jsonl with structured evidence.
  pass 2  external sort of entries by object_id -> duplicate detection.
  pass 3  external sort of entries by (Hilbert key of bbox centre, id).
  pass 4  pack sorted entries into leaf pages; pack parent bboxes level
          by level into internal pages (each level streams from temp
          files, so arbitrarily large inputs build within the budget).
  commit  finish headers, fsync, write manifest.json LAST (atomic
          rename), rename build dir into place.

Any reject or I/O failure aborts the build: the directory is left without
a manifest and therefore can never be opened for queries.
"""

import json
import math
import os
import shutil
import struct
import time
import uuid
from datetime import datetime, timezone

from . import format as fmt
from .errors import BuildError
from .geojson import R_DUP_ID, Reject, parse_json_line, validate_record
from .spill import external_sort

_ENTRY_TAIL = struct.Struct(">Q4dQI")   # line_no, bbox(4), payload_off, payload_len
_HILBERT_KEY = struct.Struct(">Q")
_PARENT = struct.Struct(">4dQ")         # bbox(4), child_page
_RUN_LEN = struct.Struct(">I")

MAX_STORED_REJECTS = 100000
_HILBERT_BITS = 31
_HILBERT_N = 1 << _HILBERT_BITS


# ------------------------------------------------------------------ hilbert

def _hilbert_xy_to_d(x, y):
    """Map integer (x, y) in [0, 2**31) to a 62-bit Hilbert index."""
    d = 0
    s = _HILBERT_N >> 1
    n = _HILBERT_N
    while s:
        rx = 1 if (x & s) else 0
        ry = 1 if (y & s) else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = n - 1 - x
                y = n - 1 - y
            x, y = y, x
        s >>= 1
    return d


def _hilbert_key(bbox, global_bbox):
    """Hilbert index of the bbox centre, normalised over the global bbox."""
    gminx, gminy, gmaxx, gmaxy = global_bbox
    cx = (bbox[0] + bbox[2]) * 0.5
    cy = (bbox[1] + bbox[3]) * 0.5
    sx = gmaxx - gminx
    sy = gmaxy - gminy
    if sx <= 0.0 and sy <= 0.0:
        return 0
    fx = 0.0 if sx <= 0.0 else (cx - gminx) / sx
    fy = 0.0 if sy <= 0.0 else (cy - gminy) / sy
    ix = min(_HILBERT_N - 1, max(0, int(fx * (_HILBERT_N - 1))))
    iy = min(_HILBERT_N - 1, max(0, int(fy * (_HILBERT_N - 1))))
    return _hilbert_xy_to_d(ix, iy)


# ------------------------------------------------------------------ entries

def _pack_entry(object_id, line_no, bbox, payload_off, payload_len):
    id_bytes = object_id.encode("utf-8")
    return struct.pack(">H", len(id_bytes)) + id_bytes + _ENTRY_TAIL.pack(
        line_no, bbox[0], bbox[1], bbox[2], bbox[3], payload_off, payload_len)


def _entry_id_bytes(rec):
    (n,) = struct.unpack_from(">H", rec, 0)
    return rec[2:2 + n]


def _parse_entry(rec):
    (n,) = struct.unpack_from(">H", rec, 0)
    object_id = rec[2:2 + n].decode("utf-8")
    line_no, minx, miny, maxx, maxy, poff, plen = _ENTRY_TAIL.unpack_from(rec, 2 + n)
    return object_id, line_no, (minx, miny, maxx, maxy), poff, plen


def _iter_entry_file(path):
    with open(path, "rb") as fh:
        while True:
            head = fh.read(_RUN_LEN.size)
            if not head:
                return
            (n,) = _RUN_LEN.unpack(head)
            rec = fh.read(n)
            if len(rec) != n:
                raise IOError("entries temp file truncated")
            yield rec


def _write_entry_file(path, records):
    with open(path, "wb") as fh:
        for rec in records:
            fh.write(_RUN_LEN.pack(len(rec)))
            fh.write(rec)


# ------------------------------------------------------------------ builder

class _RejectSink:
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "w", encoding="utf-8")
        self.count = 0
        self.truncated = False

    def add(self, evidence):
        self.count += 1
        if self.count <= MAX_STORED_REJECTS:
            self.fh.write(json.dumps(evidence, ensure_ascii=False) + "\n")
        else:
            self.truncated = True

    def close(self):
        self.fh.close()


def _sha256_and_length(path):
    import hashlib
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


def build(input_path, out_dir, cache_bytes=64 << 20, page_size=fmt.DEFAULT_PAGE_SIZE,
          max_payload_bytes=fmt.DEFAULT_MAX_PAYLOAD_BYTES,
          max_coord=fmt.DEFAULT_MAX_COORD, force=False, log=lambda *a: None):
    """Build a catalog directory from a JSONL file.  Raises BuildError."""
    t0 = time.monotonic()
    if not os.path.isfile(input_path):
        raise BuildError("input file not found: %s" % input_path)
    if not (fmt.MIN_PAGE_SIZE <= page_size <= fmt.MAX_PAGE_SIZE):
        raise BuildError("page_size %d out of range [%d, %d]"
                         % (page_size, fmt.MIN_PAGE_SIZE, fmt.MAX_PAGE_SIZE))
    if os.path.exists(out_dir):
        if not force:
            raise BuildError("output directory exists: %s (use force to replace)"
                             % out_dir)
        if os.path.isdir(out_dir) and not os.path.islink(out_dir):
            shutil.rmtree(out_dir)
        else:
            os.unlink(out_dir)
    failed_dir = out_dir + ".failed"
    if os.path.exists(failed_dir):
        shutil.rmtree(failed_dir)
    build_dir = out_dir + ".build-%d" % os.getpid()
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)

    rejects = _RejectSink(os.path.join(build_dir, "rejects.jsonl"))
    tmpdir = os.path.join(build_dir, "tmp")
    os.makedirs(tmpdir)
    sort_chunk = max(1 << 20, cache_bytes // 2)

    def fail(message):
        rejects.close()
        report = {
            "status": "failed", "reason": message,
            "reject_count": rejects.count,
            "rejects_truncated": rejects.truncated,
            "rejects_file": "rejects.jsonl",
            "input": os.path.abspath(input_path),
            "finished_utc": datetime.now(timezone.utc).isoformat(),
        }
        with open(os.path.join(build_dir, "build_report.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        # Leave NO manifest behind: a failed build is never queryable.
        if os.path.exists(failed_dir):
            shutil.rmtree(failed_dir)
        os.replace(build_dir, failed_dir)
        raise BuildError("%s (evidence: %s)" % (message, os.path.join(failed_dir, "rejects.jsonl")),
                         rejects_path=os.path.join(failed_dir, "rejects.jsonl"),
                         reject_count=rejects.count)

    nodes_path = os.path.join(build_dir, fmt.NODES_NAME)
    payloads_path = os.path.join(build_dir, fmt.PAYLOADS_NAME)
    entries_path = os.path.join(tmpdir, "entries.tmp")

    # ---------------------------------------------------------- pass 1
    log("pass 1: validate + write payloads")
    count = 0
    global_bbox = None
    opener = open
    if input_path.endswith(".gz"):
        import gzip
        opener = gzip.open
    with open(payloads_path, "w+b") as pay_fh, \
            open(entries_path, "wb") as ent_fh, \
            opener(input_path, "rt", encoding="utf-8") as src:
        pay_fh.write(b"\x00" * fmt.PAYLOADS_HEADER_SIZE)  # placeholder
        for line_no, line in enumerate(src, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = parse_json_line(line, line_no)
                rec = validate_record(obj, line_no,
                                      max_payload_bytes=max_payload_bytes,
                                      max_coord=max_coord)
            except Reject as exc:
                rejects.add(exc.evidence())
                continue
            poff = pay_fh.tell()
            blob = fmt.pack_payload_record(rec.object_id, rec.wkb)
            pay_fh.write(blob)
            entry = _pack_entry(rec.object_id, line_no, rec.bbox, poff, len(blob))
            ent_fh.write(_RUN_LEN.pack(len(entry)))
            ent_fh.write(entry)
            b = rec.bbox
            if global_bbox is None:
                global_bbox = [b[0], b[1], b[2], b[3]]
            else:
                global_bbox[0] = min(global_bbox[0], b[0])
                global_bbox[1] = min(global_bbox[1], b[1])
                global_bbox[2] = max(global_bbox[2], b[2])
                global_bbox[3] = max(global_bbox[3], b[3])
            count += 1
        payloads_length = pay_fh.tell()
        pay_fh.seek(0)
        pay_fh.write(fmt.pack_payloads_header(count, payloads_length))
        pay_fh.flush()
        os.fsync(pay_fh.fileno())

    if rejects.count:
        fail("input contains %d invalid record(s)" % rejects.count)

    # ---------------------------------------------------------- pass 2
    log("pass 2: duplicate object_id check (external sort by id)")
    sorted_by_id = external_sort(_iter_entry_file(entries_path),
                                 key_fn=_entry_id_bytes,
                                 chunk_bytes=sort_chunk, tmpdir=tmpdir,
                                 tag="byid")
    prev_id = None
    prev_lines = []
    for rec in sorted_by_id:
        object_id, line_no, _bbox, _poff, _plen = _parse_entry(rec)
        if prev_id is not None and object_id == prev_id[0]:
            prev_id[1].append(line_no)
        else:
            if prev_id is not None and len(prev_id[1]) > 1:
                rejects.add({"reason": R_DUP_ID,
                             "detail": "object_id occurs %d times" % len(prev_id[1]),
                             "object_id": prev_id[0], "lines": prev_id[1]})
            prev_id = [object_id, [line_no]]
    if prev_id is not None and len(prev_id[1]) > 1:
        rejects.add({"reason": R_DUP_ID,
                     "detail": "object_id occurs %d times" % len(prev_id[1]),
                     "object_id": prev_id[0], "lines": prev_id[1]})
    if rejects.count:
        fail("input contains %d duplicate object_id group(s)" % rejects.count)

    # ---------------------------------------------------------- pass 3
    log("pass 3: external sort by Hilbert key")
    gb = global_bbox or [0.0, 0.0, 0.0, 0.0]

    def hilbert_records():
        for rec in _iter_entry_file(entries_path):
            _oid, _ln, bbox, _poff, _plen = _parse_entry(rec)
            yield _HILBERT_KEY.pack(_hilbert_key(bbox, gb)) + rec

    def hilbert_key_fn(rec):
        return (rec[:8], _entry_id_bytes(rec[8:]))

    sorted_by_hilbert = external_sort(hilbert_records(), key_fn=hilbert_key_fn,
                                      chunk_bytes=sort_chunk, tmpdir=tmpdir,
                                      tag="hilbert")

    # ---------------------------------------------------------- pass 4
    log("pass 4: pack leaf + internal pages")
    leaf_cap = fmt.leaf_capacity(page_size)
    internal_cap = fmt.internal_capacity(page_size)
    parents_path = os.path.join(tmpdir, "parents-a.tmp")
    parents2_path = os.path.join(tmpdir, "parents-b.tmp")

    page_count = 0
    leaf_pages = 0
    internal_pages = 0
    depth = 0

    with open(nodes_path, "w+b") as nodes_fh:
        nodes_fh.write(b"\x00" * fmt.NODES_HEADER_SIZE)  # placeholder

        def write_page(page_bytes):
            nonlocal page_count
            nodes_fh.write(page_bytes)
            page_count += 1
            return page_count - 1

        # -- leaves
        parent_count = 0
        with open(parents_path, "wb") as par_fh:
            chunk = []

            def flush_leaf():
                nonlocal parent_count, leaf_pages
                if not chunk:
                    return
                minx = min(e[0] for e in chunk)
                miny = min(e[1] for e in chunk)
                maxx = max(e[2] for e in chunk)
                maxy = max(e[3] for e in chunk)
                page_no = write_page(fmt.pack_leaf_page(chunk, page_size))
                leaf_pages += 1
                par_fh.write(_PARENT.pack(minx, miny, maxx, maxy, page_no))
                parent_count += 1
                chunk.clear()

            for rec in sorted_by_hilbert:
                _oid, _ln, bbox, poff, plen = _parse_entry(rec[8:])
                chunk.append((bbox[0], bbox[1], bbox[2], bbox[3], poff, plen))
                if len(chunk) >= leaf_cap:
                    flush_leaf()
            flush_leaf()
        depth = 1

        if parent_count == 0:
            # Empty catalog: a single empty leaf page as root.
            write_page(fmt.pack_leaf_page([], page_size))
            leaf_pages += 1
        # -- internal levels, streaming parents file -> next parents file
        cur_path, nxt_path = parents_path, parents2_path
        cur_count = parent_count
        while cur_count > 1:
            depth += 1
            next_count = 0
            with open(cur_path, "rb") as src, open(nxt_path, "wb") as dst:
                chunk = []
                while True:
                    raw = src.read(_PARENT.size)
                    if not raw:
                        break
                    chunk.append(_PARENT.unpack(raw))
                    if len(chunk) >= internal_cap:
                        minx = min(e[0] for e in chunk)
                        miny = min(e[1] for e in chunk)
                        maxx = max(e[2] for e in chunk)
                        maxy = max(e[3] for e in chunk)
                        page_no = write_page(fmt.pack_internal_page(chunk, page_size))
                        internal_pages += 1
                        dst.write(_PARENT.pack(minx, miny, maxx, maxy, page_no))
                        next_count += 1
                        chunk.clear()
                if chunk:
                    minx = min(e[0] for e in chunk)
                    miny = min(e[1] for e in chunk)
                    maxx = max(e[2] for e in chunk)
                    maxy = max(e[3] for e in chunk)
                    page_no = write_page(fmt.pack_internal_page(chunk, page_size))
                    internal_pages += 1
                    dst.write(_PARENT.pack(minx, miny, maxx, maxy, page_no))
                    next_count += 1
            cur_path, nxt_path = nxt_path, cur_path
            cur_count = next_count

        root_page = page_count - 1
        nodes_length = nodes_fh.tell()
        nodes_fh.seek(0)
        nodes_fh.write(fmt.pack_nodes_header(page_size, page_count, depth,
                                             root_page, count))
        nodes_fh.flush()
        os.fsync(nodes_fh.fileno())

    # ---------------------------------------------------------- commit
    log("commit: checksums + manifest")
    nodes_len, nodes_sha = _sha256_and_length(nodes_path)
    pay_len, pay_sha = _sha256_and_length(payloads_path)
    manifest = {
        "format": fmt.FORMAT_NAME,
        "version": fmt.FORMAT_VERSION,
        "build_id": uuid.uuid4().hex,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "object_count": count,
        "depth": depth,
        "page_size": page_size,
        "page_count": page_count,
        "leaf_pages": leaf_pages,
        "internal_pages": internal_pages,
        "root_page": root_page,
        "global_bbox": gb,
        "max_payload_bytes": max_payload_bytes,
        "max_coord": max_coord,
        "files": {
            fmt.NODES_NAME: {"length": nodes_len, "sha256": nodes_sha},
            fmt.PAYLOADS_NAME: {"length": pay_len, "sha256": pay_sha},
        },
    }
    shutil.rmtree(tmpdir, ignore_errors=True)
    report = {
        "status": "ok",
        "object_count": count,
        "rejects": 0,
        "depth": depth,
        "page_size": page_size,
        "page_count": page_count,
        "leaf_pages": leaf_pages,
        "internal_pages": internal_pages,
        "nodes_bytes": nodes_len,
        "payloads_bytes": pay_len,
        "cache_bytes": cache_bytes,
        "duration_s": round(time.monotonic() - t0, 3),
    }
    with open(os.path.join(build_dir, "build_report.json"), "w",
              encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    # The manifest is the commit point: it is written last, atomically.
    fmt.dump_manifest(os.path.join(build_dir, fmt.MANIFEST_NAME), manifest)
    dir_fd = os.open(build_dir, os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    os.replace(build_dir, out_dir)
    log("done: %d objects, depth %d, %d pages" % (count, depth, page_count))
    return report
