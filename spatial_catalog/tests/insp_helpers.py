"""Shared helpers for inspection-reconciliation tests."""

import json

from spatial_catalog.builder import build as build_catalog
from spatial_catalog.topo import Selection, build_topology
from spatial_catalog.insp import build_coverage
from spatial_catalog import iformat as F


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return str(path)


def make_topology(tmp_path, records, name="topo"):
    """Build a catalog + topology from raw {object_id, geometry} records."""
    src = write_jsonl(tmp_path / ("in-%s.jsonl" % name), records)
    cat = str(tmp_path / ("cat-%s" % name))
    build_catalog(src, cat, cache_bytes=1 << 16, force=True)
    topo = str(tmp_path / name)
    build_topology(cat, topo, selection=Selection(), cache_bytes=1 << 16,
                   force=True)
    return topo


def line(x0, x1, y=0):
    return {"type": "LineString", "coordinates": [[x0, y], [x1, y]]}


def make_registry(tmp_path, edges, name="reg.jsonl"):
    """edges: {(source_id, part_index): chainage_end} -> registry path."""
    rows = [{"source_id": sid, "part_index": part, "chainage_end": L}
            for (sid, part), L in sorted(edges.items())]
    return write_jsonl(tmp_path / name, rows)


def run_coverage(topo, registry, inspections, out, window, cutoffs,
                 cache_bytes=1 << 16, max_spill_bytes=1 << 26, force=True):
    return build_coverage(
        topo, registry, inspections, out, window[0], window[1],
        cutoffs[0], cutoffs[1], cache_bytes=cache_bytes,
        max_spill_bytes=max_spill_bytes, force=force)


def parse_cov_rows(path):
    rows = []
    with open(path, "rb") as fh:
        magic = fh.readline()
        cols = fh.readline()
        assert magic == F.COVERAGE_MAGIC_LINE.encode("utf-8")
        assert cols == F.COVERAGE_COLUMNS_LINE.encode("utf-8")
        for ln, raw in enumerate(fh, 3):
            rows.append(F.parse_coverage_line(raw, ln))
    return rows


def parse_diff_rows(path):
    rows = []
    with open(path, "rb") as fh:
        magic = fh.readline()
        cols = fh.readline()
        assert magic == F.DIFF_MAGIC_LINE.encode("utf-8")
        assert cols == F.DIFF_COLUMNS_LINE.encode("utf-8")
        for ln, raw in enumerate(fh, 3):
            rows.append(F.parse_diff_line(raw, ln))
    return rows


def parse_managed_rows(path):
    rows = []
    with open(path, "rb") as fh:
        fh.readline()
        fh.readline()
        for ln, raw in enumerate(fh, 3):
            rows.append(F.parse_managed_line(raw, ln))
    return rows


def upsert(iid, batch, sid, part, direction, lo, hi, start, end,
           checked_on, priority, result):
    return {"inspection_id": iid, "batch": batch, "action": "upsert",
            "source_id": sid, "part_index": part, "direction": direction,
            "lo": lo, "hi": hi, "start": start, "end": end,
            "checked_on": checked_on, "priority": priority,
            "result": result}


def retract(iid, batch):
    return {"inspection_id": iid, "batch": batch, "action": "retract"}
