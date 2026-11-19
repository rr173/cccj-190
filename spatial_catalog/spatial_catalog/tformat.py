"""On-disk format for the line-facility topology network.

A topology directory is an independent, versioned, immutable artifact:

  topo_manifest.json -- written LAST (atomic rename).  Absence => not a
                         committed topology, exactly like a base catalog.
  nodes.tsv          -- one node per data row, sorted by node key.
  edges.tsv          -- one line-part edge per data row, sorted by
                        (source_id, part_index) Unicode code point order.
  build_report.json  -- measured build diagnostics (counters, spills, RSS).
  selection.jsonl    -- every scanned record that did not become an edge
                        and the reason (filter exclusion, explicit type
                        rejection policy).

Connection semantics (the contract this format encodes)
--------------------------------------------------------
* Only LineString / MultiLineString records become edges.  A MultiLineString
  is split into one edge per part; every edge keeps its source object id and
  its 0-based part index, so every part stays traceable to its origin.
* A connection exists only when two line ends occupy the *exactly equal*
  2-D coordinate (float64 bit equality after -0.0 == +0.0 normalisation).
  Crossings, overlaps, tangencies and endpoints landing in another line's
  interior create no node and no vertex: only first/last positions count.
* A zero-length part is a self loop: both ends reference the same node and
  that node's end count (degree) is incremented twice.
* Repeated lines stay separate edges (distinct source references).

Deterministic identifiers
-------------------------
Node keys are ``N`` + a zero-padded decimal ordinal assigned by sorting the
distinct coordinates lexicographically by ``(x, y)``.  Edge keys use the
same ordinal of the edge in the sorted edge table.  Component keys use the
node key of the smallest node in the component.  Every identifier is a pure
function of the input content: directory layout, hash seeds and locale can
never change the output.

Spill records (external sort runs) use fixed/stable binary layouts, never
the TSV encoding, so no escaping layer sits on the hot build path.
"""

import math
import struct

from .errors import CorruptionError, TopologyError

TOPO_FORMAT_NAME = "offline-line-topology"
TOPO_FORMAT_VERSION = 1

MANIFEST_NAME = "topo_manifest.json"
NODES_NAME = "nodes.tsv"
EDGES_NAME = "edges.tsv"
REPORT_NAME = "build_report.json"
SELECTION_NAME = "selection.jsonl"

NODES_MAGIC_LINE = "# spatial-catalog line topology nodes v1\n"
NODES_COLUMNS_LINE = (
    "# node_key\tx\ty\tdegree\tincident_edges\tcomponent_key\n")
EDGES_MAGIC_LINE = "# spatial-catalog line topology edges v1\n"
EDGES_COLUMNS_LINE = (
    "# edge_key\tsource_id\tpart_index\tfrom_node\tto_node\tgeometry\t"
    "planar_length\tcomponent_key\n")
NODES_HEADER_BYTES = (NODES_MAGIC_LINE + NODES_COLUMNS_LINE).encode("utf-8")
EDGES_HEADER_BYTES = (EDGES_MAGIC_LINE + EDGES_COLUMNS_LINE).encode("utf-8")

# Width of the zero-padded ordinal in stable keys (supports up to 10^12
# nodes/edges, far beyond any feasible build).
ORD_WIDTH = 12
MAX_ORDINAL = 10 ** ORD_WIDTH - 1

# Tab/CR/LF in an object id would make the TSV grammar ambiguous; the base
# catalog build already bounds ids to 4096 UTF-8 bytes.
_FORBIDDEN_ID_CHARS = frozenset(("\t", "\r", "\n"))

# ----------------------------------------------------------- stable keys

def node_key(ordinal):
    return "N%0*d" % (ORD_WIDTH, ordinal)


def edge_key(ordinal):
    return "E%0*d" % (ORD_WIDTH, ordinal)


def component_key(node_ordinal):
    return "C%0*d" % (ORD_WIDTH, node_ordinal)


def ordinal_of_key(key, prefix):
    """Parse a N/E/C key back to its ordinal; reject malformed keys."""
    if not (isinstance(key, str) and len(key) == ORD_WIDTH + 1
            and key[:1] == prefix and key[1:].isdigit()):
        raise CorruptionError("malformed %r-prefixed stable key: %r"
                              % (prefix, key))
    return int(key[1:])


# ------------------------------------------------------------- id checks

def check_topo_id(object_id, role):
    """Validate an id for unambiguous TSV serialization."""
    if not isinstance(object_id, str) or object_id == "":
        raise TopologyError("%s id %r is not a non-empty string"
                            % (role, object_id), reason="bad_id")
    bad = next((ch for ch in object_id if ch in _FORBIDDEN_ID_CHARS), None)
    if bad is not None:
        raise TopologyError(
            "%s id %r contains a tab/newline character and cannot be written "
            "as a TSV field" % (role, object_id), reason="id_not_tsv_safe")


# ------------------------------------------------------- binary records

# Edge spill record (external sort input; external_sort adds its own u32
# length prefix, so this layout carries none):
#   sid_len(u16) part(u32) scan_seq(u64) ncoords(u32) sid  ncoords*2*f64 BE
_EDGE_HEAD = struct.Struct(">HIQI")
_COORD = struct.Struct(">dd")

# Endpoint spill record:
#   x(f64) y(f64) edge_seq(u64) end(u8, 0=from 1=to)
_ENDPOINT = struct.Struct(">ddQB")
ENDPOINT_SIZE = _ENDPOINT.size
END_FROM = 0
END_TO = 1

# Incidence spill record (re-sorted to join node keys back onto edges):
#   edge_seq(u64) end(u8) x(f64) y(f64)
_INCIDENCE = struct.Struct(">QBdd")
INCIDENCE_SIZE = _INCIDENCE.size

# Final flat edge record (edges.flat, fixed-width except the variable
# source id and coordinates):
#   sid_len(u16) part(u32) from_ord(u64) to_ord(u64) ncoords(u32)
#   sid  ncoords*2*f64
_FLAT_HEAD = struct.Struct(">HIIQI")

# Fixed-width node map record: ord(u64) x(f64) y(f64)
_NODEMAP = struct.Struct(">Qdd")
NODEMAP_SIZE = _NODEMAP.size
# Fixed-width node->component record: component_root_ord(u64)
_NODECOMP = struct.Struct(">Q")
NODECOMP_SIZE = _NODECOMP.size


def normalize_coord(x, y):
    """Normalize one position: -0.0 collapses onto +0.0 (they compare equal
    and must share one node); non-finite values are rejected."""
    if not (math.isfinite(x) and math.isfinite(y)):
        raise TopologyError("non-finite coordinate (%r, %r)" % (x, y),
                            reason="non_finite_coordinate")
    if x == 0.0:
        x = 0.0
    if y == 0.0:
        y = 0.0
    return float(x), float(y)


def pack_edge_record(source_id, part_index, scan_seq, coords):
    sb = source_id.encode("utf-8")
    out = [_EDGE_HEAD.pack(len(sb), part_index, scan_seq, len(coords)), sb]
    out.extend(_COORD.pack(x, y) for x, y in coords)
    return b"".join(out)


def unpack_edge_record(rec):
    sn, part, scan_seq, ncoords = _EDGE_HEAD.unpack_from(rec, 0)
    start = _EDGE_HEAD.size
    source_id = rec[start:start + sn].decode("utf-8")
    p = start + sn
    coords = []
    for _i in range(ncoords):
        coords.append(_COORD.unpack_from(rec, p))
        p += _COORD.size
    return source_id, part, scan_seq, coords


def edge_record_key(rec):
    """External-sort key: raw UTF-8 bytes of source_id then part index.

    UTF-8 byte order equals Unicode code point order, so this is exactly
    the required locale-independent id ordering.  The scan sequence is
    deliberately not part of the key: equal (source_id, part) identities
    cannot occur because each part of each object is emitted once."""
    sn = struct.unpack_from(">H", rec, 0)[0]
    part = struct.unpack_from(">I", rec, 2)[0]
    start = _EDGE_HEAD.size
    return rec[start:start + sn], part


def pack_endpoint(x, y, edge_seq, end):
    return _ENDPOINT.pack(x, y, edge_seq, end)


def unpack_endpoint(rec):
    return _ENDPOINT.unpack(rec)


def endpoint_record_key(rec):
    """Sort endpoints by exact coordinate; ties keep a deterministic
    secondary order (edge sequence, then from-before-to) so repeated runs
    produce byte-identical run files."""
    x, y, seq, end = _ENDPOINT.unpack(rec)
    return x, y, seq, end


def pack_incidence(edge_seq, end, x, y):
    return _INCIDENCE.pack(edge_seq, end, x, y)


def unpack_incidence(rec):
    return _INCIDENCE.unpack(rec)


def incidence_record_key(rec):
    seq, end, _x, _y = _INCIDENCE.unpack(rec)
    return seq, end


def pack_flat_edge(source_id, part_index, from_ord, to_ord, coords):
    sb = source_id.encode("utf-8")
    out = [_FLAT_HEAD.pack(len(sb), part_index, from_ord, to_ord,
                           len(coords)), sb]
    out.extend(_COORD.pack(x, y) for x, y in coords)
    return b"".join(out)


def unpack_flat_edge(rec):
    sn, part, from_ord, to_ord, ncoords = _FLAT_HEAD.unpack_from(rec, 0)
    start = _FLAT_HEAD.size
    source_id = rec[start:start + sn].decode("utf-8")
    p = start + sn
    coords = []
    for _i in range(ncoords):
        coords.append(_COORD.unpack_from(rec, p))
        p += _COORD.size
    return source_id, part, from_ord, to_ord, coords


def pack_nodemap(ordinal, x, y):
    return _NODEMAP.pack(ordinal, x, y)


def unpack_nodemap(rec):
    return _NODEMAP.unpack(rec)


# ------------------------------------------------------- geometry text

def encode_geometry_wkt(coords):
    """Canonical, verifiable geometry text for an edge row.

    The full original coordinate sequence is embedded (not just a
    reference) so an edge is self-verifying against the source catalog.
    Floats use Python ``repr`` (shortest round-trip, locale independent).
    """
    return "LINESTRING(" + ", ".join(
        "%s %s" % (repr(float(x)), repr(float(y))) for x, y in coords) + ")"


def parse_geometry_wkt(text):
    """Parse the canonical geometry text back to a coordinate list."""
    if not text.startswith("LINESTRING(") or not text.endswith(")"):
        raise CorruptionError("geometry field is not a canonical LINESTRING: %r"
                              % text[:48])
    body = text[len("LINESTRING("):-1]
    coords = []
    if body:
        for pair in body.split(", "):
            parts = pair.split(" ")
            if len(parts) != 2:
                raise CorruptionError(
                    "geometry position %r is not two numbers" % pair)
            try:
                x, y = float(parts[0]), float(parts[1])
            except ValueError:
                raise CorruptionError(
                    "geometry position %r has non-numeric components" % pair)
            if not (math.isfinite(x) and math.isfinite(y)):
                raise CorruptionError(
                    "geometry position %r is not finite" % pair)
            coords.append((x, y))
    if len(coords) < 1:
        raise CorruptionError("geometry LINESTRING has no positions")
    return coords


def planar_length(coords):
    """Pure-Python planar Euclidean length over the original sequence.

    Independent of GEOS so validation can re-derive every stored length
    bit-for-bit (same float64 arithmetic: hypot of each segment, summed)."""
    total = 0.0
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        total += math.hypot(x1 - x0, y1 - y0)
    return total


# ------------------------------------------------------------- TSV rows

def encode_node_line(node_ord, x, y, degree, incident_refs, component_ord):
    """One deterministic nodes.tsv data row (including trailing LF).

    incident_refs is a list of (edge_ord, end) tuples already sorted by
    (edge_ord, end); it is rendered as ``E....@S`` / ``E....@T`` tokens.
    """
    refs = ",".join("%s@%s" % (edge_key(eo), ("S" if end == END_FROM else "T"))
                    for eo, end in incident_refs)
    return ("%s\t%s\t%s\t%d\t%s\t%s\n" % (
        node_key(node_ord), repr(float(x)), repr(float(y)), degree, refs,
        component_key(component_ord))).encode("utf-8")


def parse_node_line(line_bytes, line_no):
    if not line_bytes.endswith(b"\n") or b"\r" in line_bytes:
        raise CorruptionError(
            "nodes.tsv line %d: missing LF or contains CR" % line_no)
    text = line_bytes[:-1].decode("utf-8")
    parts = text.split("\t")
    if len(parts) != 6:
        raise CorruptionError(
            "nodes.tsv line %d: expected 6 tab-separated fields, got %d"
            % (line_no, len(parts)))
    key_s, xs, ys, deg_s, refs_s, comp_s = parts
    n_ord = ordinal_of_key(key_s, "N")
    try:
        x, y = float(xs), float(ys)
        degree = int(deg_s)
    except ValueError:
        raise CorruptionError(
            "nodes.tsv line %d: non-numeric coordinate/degree" % line_no)
    canonical = encode_node_line(
        n_ord, x, y, degree,
        [(_parse_ref_token(tok)) for tok in refs_s.split(",")] if refs_s else [],
        ordinal_of_key(comp_s, "C"))
    if canonical != line_bytes:
        raise CorruptionError(
            "nodes.tsv line %d: non-canonical encoding %r (expected %r)"
            % (line_no, line_bytes, canonical))
    refs = []
    if refs_s:
        for tok in refs_s.split(","):
            refs.append(_parse_ref_token(tok))
    if not math.isfinite(x) or not math.isfinite(y):
        raise CorruptionError(
            "nodes.tsv line %d: coordinate not finite" % line_no)
    if degree < 1:
        raise CorruptionError(
            "nodes.tsv line %d: degree must be >= 1, got %d"
            % (line_no, degree))
    if degree != len(refs):
        raise CorruptionError(
            "nodes.tsv line %d: degree %d != %d incident edge references"
            % (line_no, degree, len(refs)))
    # references must be strictly ordered (edge ordinal, then S before T)
    for a, b in zip(refs, refs[1:]):
        if not (a < b):
            raise CorruptionError(
                "nodes.tsv line %d: incident references out of order"
                % line_no)
    return n_ord, x, y, degree, refs, ordinal_of_key(comp_s, "C")


def _parse_ref_token(tok):
    if "@" not in tok:
        raise CorruptionError("bad incident edge token %r" % tok)
    ek, side = tok.split("@", 1)
    if side not in ("S", "T"):
        raise CorruptionError("incident edge token %r has bad side marker" % tok)
    return ordinal_of_key(ek, "E"), (END_FROM if side == "S" else END_TO)


def encode_edge_line(edge_ord, source_id, part_index, from_ord, to_ord,
                     coords, length, component_ord):
    return ("%s\t%s\t%d\t%s\t%s\t%s\t%s\t%s\n" % (
        edge_key(edge_ord), source_id, part_index,
        node_key(from_ord), node_key(to_ord),
        encode_geometry_wkt(coords), repr(float(length)),
        component_key(component_ord))).encode("utf-8")


def parse_edge_line(line_bytes, line_no):
    if not line_bytes.endswith(b"\n") or b"\r" in line_bytes:
        raise CorruptionError(
            "edges.tsv line %d: missing LF or contains CR" % line_no)
    text = line_bytes[:-1].decode("utf-8")
    parts = text.split("\t")
    if len(parts) != 8:
        raise CorruptionError(
            "edges.tsv line %d: expected 8 tab-separated fields, got %d"
            % (line_no, len(parts)))
    (ek, source_id, part_s, from_s, to_s, geom_s, len_s, comp_s) = parts
    e_ord = ordinal_of_key(ek, "E")
    from_ord = ordinal_of_key(from_s, "N")
    to_ord = ordinal_of_key(to_s, "N")
    comp_ord = ordinal_of_key(comp_s, "C")
    try:
        part_index = int(part_s)
        length = float(len_s)
    except ValueError:
        raise CorruptionError(
            "edges.tsv line %d: non-numeric part/length field" % line_no)
    if not source_id:
        raise CorruptionError("edges.tsv line %d: empty source_id" % line_no)
    if part_index < 0:
        raise CorruptionError(
            "edges.tsv line %d: negative part_index" % line_no)
    coords = parse_geometry_wkt(geom_s)
    # canonical re-encoding check (also pins the numeric repr spelling)
    canonical = encode_edge_line(e_ord, source_id, part_index, from_ord,
                                 to_ord, coords, length, comp_ord)
    if canonical != line_bytes:
        raise CorruptionError(
            "edges.tsv line %d: non-canonical encoding %r (expected %r)"
            % (line_no, line_bytes, canonical))
    if not math.isfinite(length) or length < 0.0:
        raise CorruptionError(
            "edges.tsv line %d: length must be finite and >= 0" % line_no)
    return (e_ord, source_id, part_index, from_ord, to_ord, coords,
            length, comp_ord)


# --------------------------------------------------------------- hashing

def sha256_file(path):
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


# --------------------------------------------------------------- manifest

def catalog_identity(catalog_path, manifest):
    """Content identity of the input catalog, derived solely from its
    manifest (which pins both files' lengths and SHA-256)."""
    import hashlib
    import json
    import os
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
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"))
    identity["identity_digest"] = digest.hexdigest()
    return identity


def dump_manifest(path, manifest):
    """Atomic manifest write; always the last action of a build."""
    import json
    import os
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_manifest(path):
    import json
    import os
    from .errors import NotACatalogError
    if not os.path.isfile(path):
        raise NotACatalogError(
            "no topology manifest at %s -- not a committed topology "
            "(missing, half-built, or failed build)" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CorruptionError("topology manifest unreadable: %s" % exc)
    if manifest.get("format") != TOPO_FORMAT_NAME:
        raise CorruptionError(
            "manifest: unknown topology format %r" % manifest.get("format"))
    if manifest.get("version") != TOPO_FORMAT_VERSION:
        from .errors import UnsupportedVersionError
        raise UnsupportedVersionError(
            "topology version %r unsupported" % manifest.get("version"))
    return manifest
