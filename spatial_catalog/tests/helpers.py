"""Shared test helpers."""

import json
import os

from spatial_catalog.builder import build
from spatial_catalog.catalog import Catalog


def write_records(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return str(path)


def build_catalog(tmp_path, records, name="cat", **kwargs):
    src = write_records(tmp_path / "in.jsonl", records)
    out = str(tmp_path / name)
    kwargs.setdefault("cache_bytes", 1 << 20)  # small budget: forces spills
    report = build(src, out, **kwargs)
    return out, report


def open_catalog(path, **kwargs):
    kwargs.setdefault("page_cache_bytes", 1 << 16)
    kwargs.setdefault("queue_spill_bytes", 1 << 14)
    return Catalog(path, **kwargs)


def point(x, y):
    return {"type": "Point", "coordinates": [x, y]}


def linestring(coords):
    return {"type": "LineString", "coordinates": coords}


def square(x0, y0, x1, y1, hole=None):
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    coords = [ring] if hole is None else [ring, hole]
    return {"type": "Polygon", "coordinates": coords}
