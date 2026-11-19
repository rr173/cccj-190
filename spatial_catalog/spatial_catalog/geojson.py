"""GeoJSON record validation.

Every rejected record produces structured evidence: a machine-readable
reason code plus a human-readable detail and the 1-based input line number.
Nothing is repaired -- invalid input is rejected, never fixed up.

Accepted geometry types: Point, LineString, Polygon and the corresponding
Multi* types, with exactly 2D finite coordinates of bounded magnitude.
"""

import json
import math

from shapely.geometry import shape

from .format import DEFAULT_MAX_COORD, DEFAULT_MAX_PAYLOAD_BYTES, MAX_ID_BYTES

ALLOWED_TYPES = frozenset([
    "Point", "LineString", "Polygon",
    "MultiPoint", "MultiLineString", "MultiPolygon",
])

# Reject reason codes (stable, machine-readable).
R_NOT_JSON = "not_json"
R_NOT_OBJECT = "not_object"
R_MISSING_ID = "missing_object_id"
R_BAD_ID = "bad_object_id"
R_DUP_ID = "duplicate_object_id"
R_MISSING_GEOM = "missing_geometry"
R_BAD_GEOM = "bad_geometry"
R_UNSUPPORTED_TYPE = "unsupported_geometry_type"
R_EMPTY = "empty_geometry"
R_NON_FINITE = "non_finite_coordinate"
R_COORD_RANGE = "coordinate_out_of_bound"
R_NOT_2D = "not_2d_position"
R_INVALID = "invalid_geometry"
R_PAYLOAD_TOO_LARGE = "payload_too_large"


class Reject(Exception):
    """A single input record is rejected; carries the evidence."""

    def __init__(self, reason, detail, line_no=None, object_id=None):
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail
        self.line_no = line_no
        self.object_id = object_id

    def evidence(self):
        ev = {"reason": self.reason, "detail": self.detail}
        if self.line_no is not None:
            ev["line"] = self.line_no
        if self.object_id is not None:
            ev["object_id"] = self.object_id
        return ev


def _reject_constant(value):
    # json parses NaN/Infinity/-Infinity through parse_constant; forbid them.
    raise Reject(R_NON_FINITE, "non-finite JSON literal %r" % (value,))


def parse_json_line(line, line_no):
    try:
        obj = json.loads(line, parse_constant=_reject_constant)
    except Reject as exc:
        exc.line_no = line_no
        raise
    except ValueError as exc:
        raise Reject(R_NOT_JSON, "invalid JSON: %s" % exc, line_no=line_no)
    return obj


def _check_number(v, max_coord, where):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise Reject(R_NON_FINITE, "coordinate at %s is not a number: %r" % (where, v))
    if not math.isfinite(v):
        raise Reject(R_NON_FINITE, "coordinate at %s is not finite: %r" % (where, v))
    if abs(v) > max_coord:
        raise Reject(R_COORD_RANGE,
                     "coordinate at %s exceeds bound %g: %r" % (where, max_coord, v))


def _check_position(p, max_coord, where):
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise Reject(R_NOT_2D, "position at %s is not a 2-element array" % where)
    _check_number(p[0], max_coord, where)
    _check_number(p[1], max_coord, where)


def _check_line(coords, min_len, max_coord, where):
    if not isinstance(coords, list) or len(coords) < min_len:
        raise Reject(R_BAD_GEOM,
                     "line at %s needs >= %d positions" % (where, min_len))
    for i, p in enumerate(coords):
        _check_position(p, max_coord, "%s[%d]" % (where, i))


def _check_ring(ring, max_coord, where):
    if not isinstance(ring, list) or len(ring) < 4:
        raise Reject(R_BAD_GEOM, "linear ring at %s needs >= 4 positions" % where)
    for i, p in enumerate(ring):
        _check_position(p, max_coord, "%s[%d]" % (where, i))
    if ring[0][0] != ring[-1][0] or ring[0][1] != ring[-1][1]:
        # Unclosed rings are rejected, not auto-closed (no repair).
        raise Reject(R_BAD_GEOM, "linear ring at %s is not closed" % where)


def check_geometry_dict(geom, max_coord):
    """Structural + finiteness checks on the raw GeoJSON geometry dict."""
    if not isinstance(geom, dict):
        raise Reject(R_BAD_GEOM, "geometry is not an object")
    gtype = geom.get("type")
    if gtype not in ALLOWED_TYPES:
        raise Reject(R_UNSUPPORTED_TYPE, "geometry type %r not supported" % (gtype,))
    if "coordinates" not in geom:
        raise Reject(R_BAD_GEOM, "geometry has no 'coordinates'")
    coords = geom["coordinates"]
    if gtype == "Point":
        _check_position(coords, max_coord, "coordinates")
    elif gtype == "LineString":
        _check_line(coords, 2, max_coord, "coordinates")
    elif gtype == "MultiPoint":
        if not isinstance(coords, list):
            raise Reject(R_BAD_GEOM, "MultiPoint coordinates not an array")
        for i, p in enumerate(coords):
            _check_position(p, max_coord, "coordinates[%d]" % i)
    elif gtype == "MultiLineString":
        if not isinstance(coords, list):
            raise Reject(R_BAD_GEOM, "MultiLineString coordinates not an array")
        for i, line in enumerate(coords):
            _check_line(line, 2, max_coord, "coordinates[%d]" % i)
    elif gtype == "Polygon":
        if not isinstance(coords, list):
            raise Reject(R_BAD_GEOM, "Polygon coordinates not an array")
        for i, ring in enumerate(coords):
            _check_ring(ring, max_coord, "coordinates[%d]" % i)
    elif gtype == "MultiPolygon":
        if not isinstance(coords, list):
            raise Reject(R_BAD_GEOM, "MultiPolygon coordinates not an array")
        for i, poly in enumerate(coords):
            if not isinstance(poly, list):
                raise Reject(R_BAD_GEOM, "MultiPolygon[%d] not an array" % i)
            for j, ring in enumerate(poly):
                _check_ring(ring, max_coord, "coordinates[%d][%d]" % (i, j))
    return gtype


class Record:
    """A validated input record."""
    __slots__ = ("object_id", "geom", "wkb", "bbox")

    def __init__(self, object_id, geom, wkb, bbox):
        self.object_id = object_id
        self.geom = geom
        self.wkb = wkb
        self.bbox = bbox


def validate_record(obj, line_no, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                    max_coord=DEFAULT_MAX_COORD):
    """Validate one parsed JSON object; returns a Record or raises Reject."""
    if not isinstance(obj, dict):
        raise Reject(R_NOT_OBJECT, "top-level value is not an object", line_no=line_no)
    object_id = obj.get("object_id")
    if object_id is None:
        raise Reject(R_MISSING_ID, "record has no 'object_id'", line_no=line_no)
    if not isinstance(object_id, str) or object_id == "":
        raise Reject(R_BAD_ID, "object_id must be a non-empty string",
                     line_no=line_no)
    if len(object_id.encode("utf-8")) > MAX_ID_BYTES:
        raise Reject(R_BAD_ID, "object_id exceeds %d UTF-8 bytes" % MAX_ID_BYTES,
                     line_no=line_no, object_id=object_id)
    if "geometry" not in obj:
        raise Reject(R_MISSING_GEOM, "record has no 'geometry'",
                     line_no=line_no, object_id=object_id)
    geom_obj = obj["geometry"]
    try:
        check_geometry_dict(geom_obj, max_coord)
    except Reject as exc:
        exc.line_no = line_no
        exc.object_id = object_id
        raise
    try:
        geom = shape(geom_obj)
    except Exception as exc:  # shapely/GEOS parse failure -> reject, not repair
        raise Reject(R_BAD_GEOM, "geometry not parseable: %s" % exc,
                     line_no=line_no, object_id=object_id)
    if geom.is_empty:
        raise Reject(R_EMPTY, "geometry is empty", line_no=line_no,
                     object_id=object_id)
    if not geom.is_valid:
        # A repeated-position (zero-length) LineString part is reported as
        # invalid by GEOS, but it is not malformed input: the topology
        # contract explicitly treats a zero-length line part as a legal
        # self loop (both ends reference the same node).  Accept line
        # geometries whose only invalidity is zero-length part(s); reject
        # every other invalidity without repairing it.
        gtype = geom.geom_type
        acceptable_line = False
        if gtype == "LineString":
            acceptable_line = (
                len(geom.coords) >= 2
                and all(geom.coords[0] == p for p in geom.coords))
        elif gtype == "MultiLineString":
            acceptable_line = all(
                len(g.coords) >= 2
                and all(g.coords[0] == p for p in g.coords)
                for g in geom.geoms if g.length == 0.0) and all(
                    g.is_valid for g in geom.geoms if g.length > 0.0)
        if not acceptable_line:
            raise Reject(R_INVALID,
                         "geometry is invalid (e.g. self-intersecting)",
                         line_no=line_no, object_id=object_id)
    wkb = geom.wkb
    if len(wkb) > max_payload_bytes:
        raise Reject(R_PAYLOAD_TOO_LARGE,
                     "geometry payload %d bytes exceeds limit %d"
                     % (len(wkb), max_payload_bytes),
                     line_no=line_no, object_id=object_id)
    minx, miny, maxx, maxy = geom.bounds
    return Record(object_id, geom, wkb, (minx, miny, maxx, maxy))
