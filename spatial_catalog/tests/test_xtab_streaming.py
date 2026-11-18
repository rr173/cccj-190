"""Direct tests for the bounded streaming primitives the cross-table is
built on: leaf/payload streaming scans and bbox-only candidate
enumeration with measured tree pruning."""

from spatial_catalog import datagen
from spatial_catalog.catalog import Catalog, QueryStats
from spatial_catalog.builder import build
from helpers import write_records


def _open_cat(tmp_path, records, cache_bytes=1 << 20):
    src = write_records(str(tmp_path / "in.jsonl"), records)
    out = str(tmp_path / "cat")
    build(src, out, cache_bytes=cache_bytes, force=True)
    return Catalog(out, page_cache_bytes=1 << 16, queue_spill_bytes=1 << 16)


def test_iter_payloads_streams_every_object_once(tmp_path):
    records = datagen.gen_sparse_records(500, seed=4)
    with _open_cat(tmp_path, records) as cat:
        seen = list(cat.iter_payloads())
    assert len(seen) == 500
    assert sorted(oid for oid, _g in seen) == sorted(r["object_id"] for r in records)


def test_iter_leaf_entries_count_matches_objects(tmp_path):
    records = datagen.gen_sparse_records(300, seed=5)
    with _open_cat(tmp_path, records) as cat:
        entries = list(cat.iter_leaf_entries())
    assert len(entries) == cat.object_count == 300


def test_bbox_window_prunes_sparse_catalog(tmp_path):
    # Sparse 2000-object catalog; a small bbox window must visit far fewer
    # pages and return far fewer candidates than a full scan, proving the
    # persistent tree does the pruning, not a Python-side filter.
    records = datagen.gen_sparse_records(2000, seed=6)
    with _open_cat(tmp_path, records) as cat:
        total_pages = cat.page_count
        stats = QueryStats()
        # a window in the middle of the 1000x1000 domain
        candidates = list(cat.bbox_window_entries((400.0, 400.0, 420.0, 420.0),
                                                  stats))
        # every returned reference is legal and decodes
        for poff, plen in candidates:
            oid, geom = cat._read_payload(poff, plen)
            assert isinstance(oid, str)
        # strong pruning: a handful of pages, a small fraction of objects
        assert stats.nodes_visited < total_pages / 5
        assert stats.pq_pushed == len(candidates)
        assert len(candidates) < 2000 / 20
        # a full-domain bbox returns every leaf entry (no false negatives)
        full = list(cat.bbox_window_entries(
            (-1e16, -1e16, 1e16, 1e16), QueryStats()))
        assert len(full) == cat.object_count


def test_bbox_window_closed_boundary_inclusive(tmp_path):
    # entries whose bbox edge exactly touches the window edge are included
    records = [
        {"object_id": "edge",
         "geometry": {"type": "LineString", "coordinates": [[10, 0], [10, 10]]}},
        {"object_id": "away",
         "geometry": {"type": "LineString", "coordinates": [[100, 100], [101, 101]]}},
    ]
    with _open_cat(tmp_path, records) as cat:
        got = list(cat.bbox_window_entries((0.0, 0.0, 10.0, 10.0)))
        assert len(got) == 1  # edge touches; 'away' is pruned
