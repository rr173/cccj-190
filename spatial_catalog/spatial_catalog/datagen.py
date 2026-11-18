"""Runtime data generators shared by tests and diagnostics.

All scale data is generated at runtime from explicit seeds -- nothing is
shipped as a fixture file, so every run is reproducible.
"""

import json
import random


def _point(x, y):
    return {"type": "Point", "coordinates": [x, y]}


def _line(coords):
    return {"type": "LineString", "coordinates": [list(c) for c in coords]}


def _square(cx, cy, r, hole_ratio=0.0):
    ring = [[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r],
            [cx - r, cy + r], [cx - r, cy - r]]
    if hole_ratio > 0.0:
        hr = r * hole_ratio
        hole = [[cx - hr, cy - hr], [cx - hr, cy + hr], [cx + hr, cy + hr],
                [cx + hr, cy - hr], [cx - hr, cy - hr]]
        return {"type": "Polygon", "coordinates": [ring, hole]}
    return {"type": "Polygon", "coordinates": [ring]}


def gen_sparse_records(n, seed=1, grid=1000.0, with_holes=True):
    """Non-overlapping-ish mix of points, lines, polygons, multi-geometries
    scattered over a large sparse domain."""
    rng = random.Random(seed)
    side = max(1, int(n ** 0.5) + 1)
    cell = grid / side
    records = []
    for i in range(n):
        gx, gy = divmod(i, side)
        cx = gx * cell + rng.uniform(0.1, 0.9) * cell + rng.uniform(0, 1e-6)
        cy = gy * cell + rng.uniform(0.1, 0.9) * cell
        kind = i % 10
        if kind < 5:
            geom = _point(cx, cy)
        elif kind < 7:
            geom = _line([(cx, cy),
                          (cx + rng.uniform(-0.3, 0.3) * cell,
                           cy + rng.uniform(-0.3, 0.3) * cell),
                          (cx + rng.uniform(-0.3, 0.3) * cell,
                           cy + rng.uniform(-0.3, 0.3) * cell)])
        elif kind < 9:
            r = rng.uniform(0.05, 0.2) * cell
            hole = 0.5 if (with_holes and i % 20 == 8) else 0.0
            geom = _square(cx, cy, r, hole_ratio=hole)
        else:
            geom = {"type": "MultiPoint",
                    "coordinates": [[cx, cy],
                                    [cx + 0.05 * cell, cy + 0.05 * cell]]}
        records.append({"object_id": "sp-%08d" % i, "geometry": geom})
    rng.shuffle(records)  # build input is unordered
    return records


def gen_overlap_records(n, seed=2, span=1000.0):
    """Worst case: every geometry is a long skinny line crossing the same
    small region, so all bboxes overlap heavily."""
    rng = random.Random(seed)
    records = []
    for i in range(n):
        x0 = rng.uniform(-span, span)
        y0 = rng.uniform(-span, span)
        x1 = rng.uniform(-span, span)
        y1 = rng.uniform(-span, span)
        # route every line through the central 10x10 box
        mx = rng.uniform(-5.0, 5.0)
        my = rng.uniform(-5.0, 5.0)
        geom = _line([(x0, y0), (mx, my), (x1, y1)])
        records.append({"object_id": "ov-%08d" % i, "geometry": geom})
    rng.shuffle(records)
    return records


def gen_mixed_records(n, seed=3, extent=2000.0):
    """All six geometry types, including polygons with holes and skinny
    lines, randomly placed (overlaps allowed)."""
    rng = random.Random(seed)
    records = []
    for i in range(n):
        cx = rng.uniform(0, extent)
        cy = rng.uniform(0, extent)
        kind = i % 6
        if kind == 0:
            geom = _point(cx, cy)
        elif kind == 1:
            npts = rng.randint(2, 8)
            geom = _line([(cx + rng.uniform(-50, 50), cy + rng.uniform(-50, 50))
                          for _ in range(npts)])
        elif kind == 2:
            r = rng.uniform(1, 30)
            hole = 0.6 if rng.random() < 0.3 else 0.0
            geom = _square(cx, cy, r, hole_ratio=hole)
        elif kind == 3:
            geom = {"type": "MultiPoint",
                    "coordinates": [[cx + rng.uniform(-10, 10),
                                     cy + rng.uniform(-10, 10)]
                                    for _ in range(rng.randint(2, 5))]}
        elif kind == 4:
            geom = {"type": "MultiLineString",
                    "coordinates": [
                        [[cx + rng.uniform(-20, 20), cy + rng.uniform(-20, 20)]
                         for _ in range(rng.randint(2, 4))]
                        for _ in range(rng.randint(2, 3))]}
        else:
            # component polygons are placed on a wide grid so the
            # MultiPolygon is valid (members must not overlap)
            polys = []
            for j in range(rng.randint(2, 3)):
                px = cx + j * 60.0
                py = cy
                r = rng.uniform(1, 10)
                polys.append(_square(px, py, r)["coordinates"])
            geom = {"type": "MultiPolygon", "coordinates": polys}
        records.append({"object_id": "mx-%06d" % i, "geometry": geom})
    rng.shuffle(records)
    return records


def gen_equidistant_points(ids, center=(0.0, 0.0), radius=1.0):
    """Points at exactly `radius` from center on the axes: distances are
    exactly representable in binary floating point (integers), so the tie
    is mathematical, not float-approximate."""
    cx, cy = center
    pts = [(cx + radius, cy), (cx - radius, cy), (cx, cy + radius), (cx, cy - radius)]
    return [{"object_id": oid, "geometry": _point(px, py)}
            for oid, (px, py) in zip(ids, pts)]


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path
