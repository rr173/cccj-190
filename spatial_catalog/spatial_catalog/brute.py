"""Independent brute-force reference implementation.

Used ONLY by tests and diagnostics to differential-check the indexed
queries.  It deliberately shares no code with the production tree walk
or pruning logic: it parses JSONL on its own and applies shapely's
single-geometry predicates to every record (a sequential full scan).
"""

import gzip
import json

from shapely.geometry import LineString, Point, box, shape


def load_records(path):
    """Parse JSONL into [(object_id, shapely_geometry)] -- full scan."""
    records = []
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            records.append((obj["object_id"], shape(obj["geometry"])))
    return records


def rect_geometry(minx, miny, maxx, maxy):
    """Closed rectangle, degeneracy-aware (independent copy)."""
    if minx == maxx and miny == maxy:
        return Point(minx, miny)
    if minx == maxx or miny == maxy:
        return LineString([(minx, miny), (maxx, maxy)])
    return box(minx, miny, maxx, maxy)


def window_scan(records, minx, miny, maxx, maxy):
    """Full-scan window query; returns ids sorted for deterministic diff."""
    rect = rect_geometry(minx, miny, maxx, maxy)
    return sorted(oid for oid, geom in records if rect.intersects(geom))


def nearest_scan(records, x, y, k):
    """Full-scan kNN; returns [(object_id, distance)] ordered by
    (distance, object_id) -- the same contract as the indexed query."""
    if k <= 0:
        return []
    point = Point(float(x), float(y))
    scored = [(geom.distance(point), oid) for oid, geom in records]
    scored.sort(key=lambda t: (t[0], t[1]))
    return [(oid, dist) for dist, oid in scored[:k]]


# ------------------------------------------------- cross-table oracle

def crosstab_scan(line_records, zone_records):
    """Independent correctness oracle for the line/zone cross-table.

    Naive O(L*Z) double full scan using ONLY Shapely single-geometry
    intersection and length.  It never opens a catalog, never touches the
    R-tree, pruning, external sort or dedupe production code.  Returns the
    complete expected set of rows as
    (source_id, zone_id, intersect_length, source_length, ratio) tuples
    sorted by (source_id, zone_id) -- exactly the production row order.

    The same semantics are re-derived here from scratch: a pair appears
    only when the intersection has a strictly positive 1-D length, and the
    ratio is the float64 quotient of the two GEOS planar lengths.
    """
    rows = []
    for source_id, line_geom in line_records:
        source_length = float(line_geom.length)
        for zone_id, zone_geom in zone_records:
            intersect_length = float(line_geom.intersection(zone_geom).length)
            if intersect_length > 0.0:
                rows.append((source_id, zone_id, intersect_length,
                             source_length,
                             intersect_length / source_length))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def crosstab_rows_from_jsonl(line_path, zone_path):
    """Convenience: run the oracle straight from the two JSONL inputs."""
    return crosstab_scan(load_records(line_path), load_records(zone_path))
