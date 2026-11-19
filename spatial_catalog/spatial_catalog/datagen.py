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


# -------------------------------------------- cross-table generators

def _polygon(ring, holes=()):
    return {"type": "Polygon", "coordinates": [ring] + list(holes)}


def _multiline(parts):
    return {"type": "MultiLineString",
            "coordinates": [[list(c) for c in part] for part in parts]}


def _multipolygon(polys):
    return {"type": "MultiPolygon",
            "coordinates": [p["coordinates"] for p in polys]}


def gen_xtab_sparse(n_lines, n_zones, seed=11, grid=1000.0):
    """Sparse, mostly disjoint scene: small squares on a wide grid and
    short lines nearby, so bbox pruning removes most pairs.  Includes
    holes, MultiLineStrings and MultiPolygons."""
    rng = random.Random(seed)
    lines, zones = [], []
    side = max(1, int(max(n_lines, n_zones) ** 0.5) + 1)
    cell = grid / side
    for i in range(n_zones):
        gx, gy = divmod(i, side)
        cx = gx * cell + 0.5 * cell + rng.uniform(-0.1, 0.1) * cell
        cy = gy * cell + 0.5 * cell + rng.uniform(-0.1, 0.1) * cell
        r = rng.uniform(0.05, 0.18) * cell
        if i % 7 == 3:
            sq1 = _square(cx, cy, r)
            sq2 = _square(cx + 3 * r, cy, r * 0.8)
            zones.append({"object_id": "z-%08d" % i,
                          "geometry": _multipolygon([sq1, sq2])})
        else:
            hole = 0.45 if i % 5 == 2 else 0.0
            zones.append({"object_id": "z-%08d" % i,
                          "geometry": _square(cx, cy, r, hole_ratio=hole)})
    for i in range(n_lines):
        gx, gy = divmod(i, side)
        cx = gx * cell + 0.5 * cell + rng.uniform(-0.2, 0.2) * cell
        cy = gy * cell + 0.5 * cell + rng.uniform(-0.2, 0.2) * cell
        length = rng.uniform(0.1, 0.6) * cell
        angle = rng.uniform(0, 6.2831853)
        coords = [(cx - 0.5 * length * _cos(angle),
                   cy - 0.5 * length * _sin(angle)),
                  (cx, cy),
                  (cx + 0.5 * length * _cos(angle),
                   cy + 0.5 * length * _sin(angle))]
        if i % 6 == 4:
            lines.append({"object_id": "l-%08d" % i,
                          "geometry": _multiline([coords[:2], coords[1:]])})
        else:
            lines.append({"object_id": "l-%08d" % i, "geometry": _line(coords)})
    rng.shuffle(lines)
    rng.shuffle(zones)
    return lines, zones


def _cos(x):
    import math
    return math.cos(x)


def _sin(x):
    import math
    return math.sin(x)


def gen_xtab_full_overlap(n_lines, n_zones, seed=12, span=100.0):
    """Degenerate worst case: every zone polygon covers the shared central
    box and every line crosses it, so every line x zone pair has positive
    intersection length: bbox pruning cannot remove a single candidate."""
    rng = random.Random(seed)
    zones = []
    for j in range(n_zones):
        # all polygons contain [-5,5] x [-5,5]; randomised outer corners
        x0 = rng.uniform(-span, -5.0)
        y0 = rng.uniform(-span, -5.0)
        x1 = rng.uniform(5.0, span)
        y1 = rng.uniform(5.0, span)
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        zones.append({"object_id": "zo-%08d" % j,
                      "geometry": {"type": "Polygon", "coordinates": [ring]}})
    lines = []
    for i in range(n_lines):
        mx, my = rng.uniform(-4.0, 4.0), rng.uniform(-4.0, 4.0)
        lines.append({"object_id": "lo-%08d" % i, "geometry": _line([
            (rng.uniform(-span, -6.0), rng.uniform(-span, -6.0)),
            (mx, my),
            (rng.uniform(6.0, span), rng.uniform(6.0, span))])})
    rng.shuffle(lines)
    rng.shuffle(zones)
    return lines, zones


def gen_xtab_unicode_ids(seed=13):
    """Mixed Unicode ids to pin code point ordering (CJK, accented,
    Greek, right-to-left)."""
    rng = random.Random(seed)

    def rect(x0, y0, x1, y1):
        return {"type": "Polygon",
                "coordinates": [[[x0, y0], [x1, y0], [x1, y1],
                                 [x0, y1], [x0, y0]]]}

    zones = [
        {"object_id": "区域-α", "geometry": rect(0, 0, 10, 10)},
        {"object_id": "zone-01", "geometry": rect(5, 5, 15, 15)},
        {"object_id": "Ωmega", "geometry": rect(-5, -5, 4, 4)},
        {"object_id": "zöne", "geometry": rect(20, 20, 30, 30)},
    ]
    lines = [
        {"object_id": "线-1", "geometry": _line([(-2, 2), (12, 2)])},
        {"object_id": "line-乙", "geometry": _line([(6, 6), (14, 14)])},
        {"object_id": "åline", "geometry": _line([(21, 25), (29, 25)])},
        {"object_id": "latin", "geometry": _line([(-4, -4), (3, 3)])},
    ]
    rng.shuffle(lines)
    rng.shuffle(zones)
    return lines, zones


# ------------------------------------------------ topology generators

def gen_topo_scale_records(n_small=3000, big_size=1000, seed=31,
                           cell=1000.0):
    """Scale scene for bounded-resource topology runs.

    * ``n_small`` disjoint tiny components (pairs/triples placed on a wide
      grid so no coordinate of one ever equals another's);
    * exactly ONE giant connected component: a closed ring of ``big_size``
      edges whose consecutive ends share exact integer coordinates, plus
      a few extra spokes, so component closure has to process one large
      graph completely rather than cap its size;
    * some deliberately non-line records (points/polygons) interspersed so
      the explicit type-rejection / filter paths are exercised at scale.
    """
    rng = random.Random(seed)
    records = []
    side = max(1, int(n_small ** 0.5) + 1)
    step = cell
    for i in range(n_small):
        gx, gy = divmod(i, side)
        ox = gx * step * 3
        oy = gy * step * 3
        if i % 3 == 0:
            # two-edge chain, three nodes
            records.append({"object_id": "sm-%08d-a" % i,
                            "geometry": _line([(ox, oy), (ox + 1, oy)])})
            records.append({"object_id": "sm-%08d-b" % i,
                            "geometry": _line([(ox + 1, oy),
                                               (ox + 2, oy + 1)])})
        elif i % 3 == 1:
            # isolated single line, own two nodes
            records.append({"object_id": "sm-%08d-s" % i,
                            "geometry": _line([(ox, oy), (ox + 1, oy + 1)])})
        else:
            # three-way fork around a shared node
            records.append({"object_id": "sm-%08d-f1" % i,
                            "geometry": _line([(ox, oy), (ox + 1, oy)])})
            records.append({"object_id": "sm-%08d-f2" % i,
                            "geometry": _line([(ox, oy), (ox, oy + 1)])})
            records.append({"object_id": "sm-%08d-f3" % i,
                            "geometry": _line([(ox, oy), (ox - 1, oy)])})
        if i % 17 == 0:
            records.append({"object_id": "pt-%08d" % i,
                            "geometry": _point(ox + 50, oy + 50)})
    # giant component: a regular polygon ring on integer coordinates built
    # from axis-aligned segments so every shared end is bit-exact equal.
    giant = []
    cx, cy = 500000, 500000
    radius = big_size
    prev = None
    for i in range(big_size):
        # axis-aligned stair-step around the ring: each edge unit length 1,
        # consecutive edges share one exact coordinate node
        x0 = cx + i
        y0 = cy + (i % 2)
        x1 = cx + i + 1
        y1 = cy + ((i + 1) % 2)
        giant.append({"object_id": "gi-%08d" % i,
                      "geometry": _line([(x0, y0), (x1, y1)])})
    # close the ring back to the first node with one more exact edge
    giant.append({"object_id": "gi-%08d" % big_size,
                  "geometry": _line([(cx + big_size, cy + (big_size % 2)),
                                     (cx, cy)])})
    rng.shuffle(records)
    rng.shuffle(giant)
    return records + giant


def gen_topo_unicode_sources(seed=32):
    """Small set with Unicode object ids and reversed coordinate order,
    for code point ordering and reverse-sequence stability checks."""
    rng = random.Random(seed)
    records = [
        {"object_id": "线-甲", "geometry": _line([(0, 0), (1, 0)])},
        {"object_id": "线-乙", "geometry": _line([(1, 0), (2, 0)])},
        {"object_id": "Ω-edge", "geometry": _line([(5, 5), (1, 0)])},
        {"object_id": "å-loop",
         "geometry": {"type": "MultiLineString",
                      "coordinates": [[[7, 7], [7, 7]],
                                      [[7, 7], [8, 8]]]}},
    ]
    rng.shuffle(records)
    return records
