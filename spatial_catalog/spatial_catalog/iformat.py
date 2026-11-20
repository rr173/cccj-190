"""On-disk format for the inspection-coverage reconciliation artifact.

A reconciliation directory is an independent, versioned, immutable artifact:

  inspcov_manifest.json -- written LAST (atomic rename).  Absence => not a
                           committed reconciliation (missing, half-built or
                           failed build), exactly like a base catalog.
  managed.tsv           -- the managed edge roster (source_id, part_index,
                           chainage_end) that survived topology validation.
  coverage_before.tsv   -- full [from,to) x [0,L) coverage ledger at the
                           earlier receipt cutoff, as merged rectangle rows.
  coverage_after.tsv    -- the same ledger at the later receipt cutoff.
  diff.tsv              -- merged rectangles where the two ledgers disagree
                           in status or evidence, carrying both sides.
  summary.json          -- machine-readable change totals (status pairs and
                           evidence-only replacements, in chainage-unit*day).
  build_report.json     -- measured build diagnostics (counters, spills, RSS).

Every ledger row is a rectangle with a date start/end and a chainage
start/end.  Rows are canonical UTF-8 TSV (no quoting, no escaping; ids with
tab/CR/LF are rejected at build time).  Ordering is the Unicode code point
order of (source_id, part_index) -- identical to raw UTF-8 byte order -- then
date start, then chainage start, so the on-disk sort key is locale
independent and reproducible regardless of input file split, read order or
cache size.

Spill records (external sort runs and sweep structures) use fixed/stable
binary layouts, never the TSV encoding, so no escaping layer sits on the hot
reconciliation path.
"""

import datetime
import hashlib
import json
import os
import re
import struct

from .errors import (CorruptionError, InspectionError, NotACatalogError,
                     UnsupportedVersionError)

INSP_FORMAT_NAME = "offline-inspection-coverage"
INSP_FORMAT_VERSION = 1

MANIFEST_NAME = "inspcov_manifest.json"
MANAGED_NAME = "managed.tsv"
COVERAGE_BEFORE_NAME = "coverage_before.tsv"
COVERAGE_AFTER_NAME = "coverage_after.tsv"
DIFF_NAME = "diff.tsv"
SUMMARY_NAME = "summary.json"
REPORT_NAME = "build_report.json"

MANAGED_MAGIC_LINE = "# spatial-catalog inspection managed edges v1\n"
MANAGED_COLUMNS_LINE = "# source_id\tpart_index\tchainage_end\n"
COVERAGE_MAGIC_LINE = "# spatial-catalog inspection coverage v1\n"
COVERAGE_COLUMNS_LINE = (
    "# source_id\tpart_index\tdate_start\tdate_end\tchainage_start\t"
    "chainage_end\tstatus\tinspection_id\tbatch\n")
DIFF_MAGIC_LINE = "# spatial-catalog inspection coverage diff v1\n"
DIFF_COLUMNS_LINE = (
    "# source_id\tpart_index\tdate_start\tdate_end\tchainage_start\t"
    "chainage_end\tbefore_status\tbefore_inspection_id\tbefore_batch\t"
    "after_status\tafter_inspection_id\tafter_batch\n")

MANAGED_HEADER_BYTES = (MANAGED_MAGIC_LINE + MANAGED_COLUMNS_LINE).encode("utf-8")
COVERAGE_HEADER_BYTES = (COVERAGE_MAGIC_LINE + COVERAGE_COLUMNS_LINE).encode("utf-8")
DIFF_HEADER_BYTES = (DIFF_MAGIC_LINE + DIFF_COLUMNS_LINE).encode("utf-8")

# Result / status vocabulary.  Status "gap" carries no evidence.
STATUS_GAP = "gap"
STATUS_PASS = "pass"
STATUS_DEFECT = "defect"
STATUSES = frozenset((STATUS_GAP, STATUS_PASS, STATUS_DEFECT))
RESULTS = frozenset((STATUS_PASS, STATUS_DEFECT))   # an observation's result

DIR_FORWARD = "forward"
DIR_REVERSE = "reverse"
DIRECTIONS = frozenset((DIR_FORWARD, DIR_REVERSE))

# Internal status codes used by the sweep and segment records.
CODE_GAP = 0
CODE_PASS = 1
CODE_DEFECT = 2
RESULT_TO_CODE = {STATUS_PASS: CODE_PASS, STATUS_DEFECT: CODE_DEFECT}
CODE_TO_STATUS = {CODE_GAP: STATUS_GAP, CODE_PASS: STATUS_PASS,
                  CODE_DEFECT: STATUS_DEFECT}
DIRECTION_TO_CODE = {DIR_FORWARD: 0, DIR_REVERSE: 1}

ACTION_UPSERT = "upsert"
ACTION_RETRACT = "retract"

# Side tags for the two receipt cutoffs (bitmask).
SIDE_BEFORE = 1
SIDE_AFTER = 2
SIDE_BOTH = 3

INT64_MAX = (1 << 63) - 1

_FORBIDDEN_ID_CHARS = frozenset(("\t", "\r", "\n"))
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


# ------------------------------------------------------------- primitives

def check_int64(value, what):
    """Every input integer must fit the signed 64-bit ledger unit range."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InspectionError("%s must be an integer, got %r" % (what, value),
                              reason="bad_integer")
    if value > INT64_MAX or value < -INT64_MAX - 1:
        raise InspectionError(
            "%s exceeds the signed 64-bit ledger range: %r" % (what, value),
            reason="integer_overflow")


def check_text_id(value, what):
    """Validate an id for unambiguous TSV serialization."""
    if not isinstance(value, str) or value == "":
        raise InspectionError("%s must be a non-empty string, got %r"
                              % (what, value), reason="bad_id")
    bad = next((ch for ch in value if ch in _FORBIDDEN_ID_CHARS), None)
    if bad is not None:
        raise InspectionError(
            "%s %r contains a tab/newline character and cannot be written "
            "as a TSV field" % (what, value), reason="id_not_tsv_safe")


def parse_date(text, what="date"):
    """Strict proleptic Gregorian YYYY-MM-DD -> day ordinal (int)."""
    if not isinstance(text, str):
        raise InspectionError("%s must be a YYYY-MM-DD string, got %r"
                              % (what, text), reason="bad_date")
    m = _DATE_RE.match(text)
    if not m:
        raise InspectionError(
            "%s is not strict YYYY-MM-DD: %r" % (what, text),
            reason="bad_date")
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime.date(year, month, day).toordinal()
    except ValueError as exc:
        raise InspectionError("%s is not a valid Gregorian date: %r (%s)"
                              % (what, text, exc), reason="bad_date")


def format_date(ordinal):
    """Day ordinal -> canonical YYYY-MM-DD."""
    return datetime.date.fromordinal(ordinal).isoformat()


# ------------------------------------------------------------- TSV rows

def encode_managed_line(source_id, part_index, chainage_end):
    return ("%s\t%d\t%d\n" % (source_id, part_index, chainage_end)
            ).encode("utf-8")


def parse_managed_line(line_bytes, line_no):
    if not line_bytes.endswith(b"\n") or b"\r" in line_bytes:
        raise CorruptionError(
            "managed.tsv line %d: missing LF or contains CR" % line_no)
    text = line_bytes[:-1].decode("utf-8")
    parts = text.split("\t")
    if len(parts) != 3:
        raise CorruptionError(
            "managed.tsv line %d: expected 3 tab-separated fields, got %d"
            % (line_no, len(parts)))
    source_id, part_s, len_s = parts
    if not source_id:
        raise CorruptionError("managed.tsv line %d: empty source_id" % line_no)
    try:
        part_index = int(part_s)
        chainage_end = int(len_s)
    except ValueError:
        raise CorruptionError(
            "managed.tsv line %d: non-integer part_index/chainage_end"
            % line_no)
    if part_index < 0 or chainage_end <= 0:
        raise CorruptionError(
            "managed.tsv line %d: part_index/chainage_end out of range"
            % line_no)
    if encode_managed_line(source_id, part_index, chainage_end) != line_bytes:
        raise CorruptionError(
            "managed.tsv line %d: non-canonical encoding" % line_no)
    return source_id, part_index, chainage_end


def _encode_evidence(status, inspection_id, batch):
    """Evidence fields: gap rows carry two empty fields."""
    if status == STATUS_GAP:
        return "", ""
    return inspection_id, str(batch)


def encode_coverage_line(source_id, part_index, date_start, date_end,
                         chainage_start, chainage_end, status,
                         inspection_id, batch):
    iid, bat = _encode_evidence(status, inspection_id, batch)
    return ("%s\t%d\t%s\t%s\t%d\t%d\t%s\t%s\t%s\n" % (
        source_id, part_index, format_date(date_start), format_date(date_end),
        chainage_start, chainage_end, status, iid, bat)).encode("utf-8")


def parse_coverage_line(line_bytes, line_no, name="coverage"):
    if not line_bytes.endswith(b"\n") or b"\r" in line_bytes:
        raise CorruptionError(
            "%s line %d: missing LF or contains CR" % (name, line_no))
    text = line_bytes[:-1].decode("utf-8")
    parts = text.split("\t")
    if len(parts) != 9:
        raise CorruptionError(
            "%s line %d: expected 9 tab-separated fields, got %d"
            % (name, line_no, len(parts)))
    (source_id, part_s, ds, de, cs, ce, status, iid, bat) = parts
    row = _parse_coverage_fields(source_id, part_s, ds, de, cs, ce, status,
                                 iid, bat, line_no, name)
    if encode_coverage_line(*row) != line_bytes:
        raise CorruptionError(
            "%s line %d: non-canonical encoding" % (name, line_no))
    return row


def _parse_coverage_fields(source_id, part_s, ds, de, cs, ce, status,
                           iid, bat, line_no, name):
    if not source_id:
        raise CorruptionError("%s line %d: empty source_id" % (name, line_no))
    if status not in STATUSES:
        raise CorruptionError(
            "%s line %d: unknown status %r" % (name, line_no, status))
    try:
        part_index = int(part_s)
        date_start = parse_date(ds)
        date_end = parse_date(de)
        chainage_start = int(cs)
        chainage_end = int(ce)
    except ValueError:
        raise CorruptionError(
            "%s line %d: non-integer or bad date field" % (name, line_no))
    if part_index < 0:
        raise CorruptionError("%s line %d: negative part_index"
                              % (name, line_no))
    if not (date_start < date_end):
        raise CorruptionError(
            "%s line %d: empty date interval" % (name, line_no))
    if not (0 <= chainage_start < chainage_end):
        raise CorruptionError(
            "%s line %d: bad chainage interval" % (name, line_no))
    if status == STATUS_GAP:
        if iid != "" or bat != "":
            raise CorruptionError(
                "%s line %d: gap row must carry empty evidence"
                % (name, line_no))
        inspection_id, batch = "", 0
    else:
        if iid == "" or bat == "":
            raise CorruptionError(
                "%s line %d: %s row must carry evidence" % (name, line_no,
                                                            status))
        try:
            batch = int(bat)
        except ValueError:
            raise CorruptionError(
                "%s line %d: non-integer batch" % (name, line_no))
        if batch <= 0:
            raise CorruptionError(
                "%s line %d: batch must be positive" % (name, line_no))
        inspection_id = iid
    return (source_id, part_index, date_start, date_end, chainage_start,
            chainage_end, status, inspection_id, batch)


def encode_diff_line(source_id, part_index, date_start, date_end,
                     chainage_start, chainage_end,
                     before_status, before_id, before_batch,
                     after_status, after_id, after_batch):
    b_iid, b_bat = _encode_evidence(before_status, before_id, before_batch)
    a_iid, a_bat = _encode_evidence(after_status, after_id, after_batch)
    return ("%s\t%d\t%s\t%s\t%d\t%d\t%s\t%s\t%s\t%s\t%s\t%s\n" % (
        source_id, part_index, format_date(date_start), format_date(date_end),
        chainage_start, chainage_end,
        before_status, b_iid, b_bat, after_status, a_iid, a_bat)
        ).encode("utf-8")


def parse_diff_line(line_bytes, line_no):
    name = "diff.tsv"
    if not line_bytes.endswith(b"\n") or b"\r" in line_bytes:
        raise CorruptionError("diff.tsv line %d: missing LF or contains CR"
                              % line_no)
    text = line_bytes[:-1].decode("utf-8")
    parts = text.split("\t")
    if len(parts) != 12:
        raise CorruptionError(
            "diff.tsv line %d: expected 12 tab-separated fields, got %d"
            % (line_no, len(parts)))
    (source_id, part_s, ds, de, cs, ce,
     b_status, b_iid, b_bat, a_status, a_iid, a_bat) = parts
    before = _parse_coverage_fields(source_id, part_s, ds, de, cs, ce,
                                    b_status, b_iid, b_bat, line_no, name)
    after = _parse_coverage_fields(source_id, part_s, ds, de, cs, ce,
                                   a_status, a_iid, a_bat, line_no, name)
    row = (before[0], before[1], before[2], before[3], before[4], before[5],
           before[6], before[7], before[8], after[6], after[7], after[8])
    if (before[6], before[7], before[8]) == (after[6], after[7], after[8]):
        raise CorruptionError(
            "diff.tsv line %d: identical before/after state is not a "
            "difference" % line_no)
    if encode_diff_line(*row) != line_bytes:
        raise CorruptionError("diff.tsv line %d: non-canonical encoding"
                              % line_no)
    return row


# ------------------------------------------------------- binary records

# Revision record (external sort by inspection id, then batch):
#   id_len(u16) batch(u64) file_index(u32) line_no(u64) content_len(u32)
#   id_bytes  content_bytes
_REV_HEAD = struct.Struct(">HQIQI")


def pack_revision(inspection_id, batch, file_index, line_no, content_bytes):
    ib = inspection_id.encode("utf-8")
    return (_REV_HEAD.pack(len(ib), batch, file_index, line_no,
                           len(content_bytes)) + ib + content_bytes)


def unpack_revision(rec):
    id_len, batch, file_index, line_no, clen = _REV_HEAD.unpack_from(rec, 0)
    p = _REV_HEAD.size
    inspection_id = rec[p:p + id_len].decode("utf-8")
    content = rec[p + id_len:p + id_len + clen]
    return inspection_id, batch, file_index, line_no, content


def revision_key(rec):
    """Sort key: (id bytes, batch, file, line).  UTF-8 byte order equals
    Unicode code point order; the file/line tiebreak makes duplicate
    retention deterministic without affecting the business result."""
    id_len, batch, file_index, line_no, _c = _REV_HEAD.unpack_from(rec, 0)
    p = _REV_HEAD.size
    return rec[p:p + id_len], batch, file_index, line_no


# Visible observation record (external sort by edge, then clipped date
# start).  Direction is still unmapped here; chainage mapping happens when
# the managed-edge join supplies L.
#   sid_len(u16) iid_len(u16) part(u64) side(u8) batch(u64) priority(i64)
#   checked_on(i32) d0(i32) d1(i32) direction(u8) lo(i64) hi(i64)
#   result(u8)  sid_bytes iid_bytes
_OBS_HEAD = struct.Struct(">HHQBQqiiiBqqB")


def pack_observation(source_id, inspection_id, part_index, side, batch,
                     priority, checked_on, d0, d1, direction, lo, hi, result):
    sb = source_id.encode("utf-8")
    ib = inspection_id.encode("utf-8")
    return (_OBS_HEAD.pack(len(sb), len(ib), part_index, side, batch,
                           priority, checked_on, d0, d1, direction, lo, hi,
                           result) + sb + ib)


def unpack_observation(rec):
    (sl, il, part, side, batch, priority, checked_on, d0, d1, direction,
     lo, hi, result) = _OBS_HEAD.unpack_from(rec, 0)
    p = _OBS_HEAD.size
    source_id = rec[p:p + sl].decode("utf-8")
    inspection_id = rec[p + sl:p + sl + il].decode("utf-8")
    return (source_id, inspection_id, part, side, batch, priority,
            checked_on, d0, d1, direction, lo, hi, result)


def observation_edge_key(rec):
    sl = struct.unpack_from(">H", rec, 0)[0]
    part = struct.unpack_from(">Q", rec, 4)[0]
    p = _OBS_HEAD.size
    return rec[p:p + sl], part


def observation_edge_d0_key(rec):
    """Sort key for the sweep input: (edge, clipped d0)."""
    sl = struct.unpack_from(">H", rec, 0)[0]
    part = struct.unpack_from(">Q", rec, 4)[0]
    # d0 sits after part(8) side(1) batch(8) priority(8) checked_on(4)
    d0 = struct.unpack_from(">i", rec, 4 + 8 + 1 + 8 + 8 + 4)[0]
    p = _OBS_HEAD.size
    return rec[p:p + sl], part, d0


# Mapped observation record (sweep working record; chainage mapped, dates
# clipped, edge attached for grouping).  Sorted by (edge, d0) for the sweep.
#   sid_len(u16) iid_len(u16) part(u64) c0(i64) c1(i64) d0(i32) d1(i32)
#   side(u8) priority(i64) checked_on(i32) batch(u64) result(u8)
#   sid_bytes iid_bytes
_MAP_HEAD = struct.Struct(">HHQqqiiBqiQB")
# field offsets inside _MAP_HEAD (sid_len, iid_len, part, c0, c1, d0, d1,
# side, priority, checked_on, batch, result)
_MAP_OFF_PART = 4
_MAP_OFF_C0 = 12
_MAP_OFF_C1 = 20
_MAP_OFF_D0 = 28
_MAP_OFF_D1 = 32
_MAP_OFF_SIDE = 36
_MAP_OFF_PRIORITY = 37
_MAP_OFF_CHECKED = 45
_MAP_OFF_BATCH = 49
_MAP_OFF_RESULT = 57


def pack_mapped(source_id, inspection_id, part_index, c0, c1, d0, d1, side,
                priority, checked_on, batch, result):
    sb = source_id.encode("utf-8")
    ib = inspection_id.encode("utf-8")
    return (_MAP_HEAD.pack(len(sb), len(ib), part_index, c0, c1, d0, d1,
                           side, priority, checked_on, batch, result)
            + sb + ib)


def unpack_mapped(rec):
    (sl, il, part, c0, c1, d0, d1, side, priority, checked_on, batch,
     result) = _MAP_HEAD.unpack_from(rec, 0)
    p = _MAP_HEAD.size
    sid = rec[p:p + sl].decode("utf-8")
    iid = rec[p + sl:p + sl + il].decode("utf-8")
    return (sid, iid, part, c0, c1, d0, d1, side, priority, checked_on,
            batch, result)


def mapped_edge_key(rec):
    sl = struct.unpack_from(">H", rec, 0)[0]
    part = struct.unpack_from(">Q", rec, _MAP_OFF_PART)[0]
    p = _MAP_HEAD.size
    return rec[p:p + sl], part


# Lightweight single-field accessors (hot sweep loop avoids full unpack).
def mapped_c0(rec):
    return struct.unpack_from(">q", rec, _MAP_OFF_C0)[0]


def mapped_c1(rec):
    return struct.unpack_from(">q", rec, _MAP_OFF_C1)[0]


def mapped_d0(rec):
    return struct.unpack_from(">i", rec, _MAP_OFF_D0)[0]


def mapped_d1(rec):
    return struct.unpack_from(">i", rec, _MAP_OFF_D1)[0]


def mapped_side(rec):
    return rec[_MAP_OFF_SIDE]


def mapped_result(rec):
    return rec[_MAP_OFF_RESULT]


def mapped_batch(rec):
    return struct.unpack_from(">Q", rec, _MAP_OFF_BATCH)[0]


def mapped_iid(rec):
    sl = struct.unpack_from(">H", rec, 0)[0]
    il = struct.unpack_from(">H", rec, 2)[0]
    return rec[_MAP_HEAD.size + sl:_MAP_HEAD.size + sl + il].decode("utf-8")


def mapped_chainage_key(rec):
    """date_active ordering: by chainage start (then a deterministic tie)."""
    c0, c1 = struct.unpack_from(">qq", rec, _MAP_OFF_C0)
    return c0, c1, rec


def mapped_winner_key(rec):
    """Winner ordering: higher priority first, then later checked_on, then
    smaller inspection id (Unicode code point order = UTF-8 byte order)."""
    priority = struct.unpack_from(">q", rec, _MAP_OFF_PRIORITY)[0]
    checked_on = struct.unpack_from(">i", rec, _MAP_OFF_CHECKED)[0]
    sl = struct.unpack_from(">H", rec, 0)[0]
    il = struct.unpack_from(">H", rec, 2)[0]
    iid = rec[_MAP_HEAD.size + sl:_MAP_HEAD.size + sl + il]
    return -priority, -checked_on, iid


# Date endpoint record (external sort by edge, then date ordinal):
#   sid_len(u16) part(u64) date(i32)  sid_bytes
_DATE_HEAD = struct.Struct(">HQi")


def pack_datepoint(source_id, part_index, date_ord):
    sb = source_id.encode("utf-8")
    return _DATE_HEAD.pack(len(sb), part_index, date_ord) + sb


def unpack_datepoint(rec):
    sl, part, date_ord = _DATE_HEAD.unpack_from(rec, 0)
    sid = rec[_DATE_HEAD.size:_DATE_HEAD.size + sl].decode("utf-8")
    return sid, part, date_ord


def datepoint_key(rec):
    sl, part, date_ord = _DATE_HEAD.unpack_from(rec, 0)
    return rec[_DATE_HEAD.size:_DATE_HEAD.size + sl], part, date_ord


def datepoint_edge_key(rec):
    sl, part, _date_ord = _DATE_HEAD.unpack_from(rec, 0)
    return rec[_DATE_HEAD.size:_DATE_HEAD.size + sl], part


def datepoint_date(rec):
    return struct.unpack_from(">i", rec, 10)[0]


# Managed edge record (sorted by edge): sid_len(u16) part(u64) L(i64) sid
_MANAGED_HEAD = struct.Struct(">HQq")


def pack_managed(source_id, part_index, chainage_end):
    sb = source_id.encode("utf-8")
    return _MANAGED_HEAD.pack(len(sb), part_index, chainage_end) + sb


def unpack_managed(rec):
    sl, part, chainage_end = _MANAGED_HEAD.unpack_from(rec, 0)
    sid = rec[_MANAGED_HEAD.size:_MANAGED_HEAD.size + sl].decode("utf-8")
    return sid, part, chainage_end


def managed_key(rec):
    sl, part, _l = _MANAGED_HEAD.unpack_from(rec, 0)
    return rec[_MANAGED_HEAD.size:_MANAGED_HEAD.size + sl], part


# --------------------------------------------------------------- hashing

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
        raise NotACatalogError(
            "no reconciliation manifest at %s -- not a committed "
            "reconciliation (missing, half-built, or failed build)" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CorruptionError("reconciliation manifest unreadable: %s" % exc)
    if manifest.get("format") != INSP_FORMAT_NAME:
        raise CorruptionError(
            "manifest: unknown reconciliation format %r"
            % manifest.get("format"))
    if manifest.get("version") != INSP_FORMAT_VERSION:
        raise UnsupportedVersionError(
            "reconciliation version %r unsupported" % manifest.get("version"))
    return manifest


def topology_identity(topo_path, topo_manifest):
    """Content identity of the consumed topology, derived solely from its
    manifest (which pins every result file's length and SHA-256)."""
    files = topo_manifest["files"]
    identity = {
        "topology_path": os.path.abspath(topo_path),
        "format": topo_manifest["format"],
        "version": topo_manifest["version"],
        "edges": topo_manifest["counts"]["edges"],
        "nodes": topo_manifest["counts"]["nodes"],
        "edges_sha256": files["edges.tsv"]["sha256"],
        "edges_length": files["edges.tsv"]["length"],
        "nodes_sha256": files["nodes.tsv"]["sha256"],
        "nodes_length": files["nodes.tsv"]["length"],
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"))
    identity["identity_digest"] = digest.hexdigest()
    return identity
