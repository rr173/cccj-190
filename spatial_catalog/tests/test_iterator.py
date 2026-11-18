"""Iterator abort and catalog close must release file handles; queries
on a closed catalog must fail explicitly."""

import os

import pytest

from spatial_catalog.errors import CatalogError
from helpers import build_catalog, open_catalog, point


def _fd_count():
    return len(os.listdir("/proc/self/fd"))


@pytest.fixture
def cat_dir(tmp_path):
    records = [{"object_id": "p%03d" % i, "geometry": point(float(i), 0.0)}
               for i in range(200)]
    out, _ = build_catalog(tmp_path, records)
    return out


def test_partial_iteration_then_close_releases_files(tmp_path, cat_dir):
    before = _fd_count()
    cat = open_catalog(cat_dir)
    res = cat.window(-1, -1, 1000, 1)
    it = iter(res)
    next(it)  # consume one, then abandon the rest
    cat.close()
    assert cat._nodes_fh.closed and cat._payloads_fh.closed
    assert _fd_count() <= before


def test_context_manager_closes(tmp_path, cat_dir):
    before = _fd_count()
    with open_catalog(cat_dir) as cat:
        assert len(list(cat.window(-1, -1, 1000, 1))) == 200
    assert cat._nodes_fh.closed and cat._payloads_fh.closed
    assert _fd_count() <= before


def test_query_after_close_fails(tmp_path, cat_dir):
    cat = open_catalog(cat_dir)
    cat.close()
    with pytest.raises(CatalogError):
        cat.nearest(0, 0, 1)
    with pytest.raises(CatalogError):
        cat.window(0, 0, 1, 1)
    cat.close()  # idempotent


def test_window_iterator_close(tmp_path, cat_dir):
    with open_catalog(cat_dir) as cat:
        res = cat.window(-1, -1, 1000, 1)
        it = iter(res)
        next(it)
        res.close()
        with pytest.raises(StopIteration):
            next(it)


def test_nearest_pq_spill_files_cleaned(tmp_path, cat_dir):
    import glob
    import tempfile
    tmp = tempfile.gettempdir()
    before = set(glob.glob(os.path.join(tmp, "spcpq-*")))
    with open_catalog(cat_dir, queue_spill_bytes=64) as cat:  # tiny: must spill
        res = cat.nearest(50, 0, 200)  # k = n: drains the whole queue
        assert len(res.results) == 200
        assert res.stats.pq_spills > 0
    after = set(glob.glob(os.path.join(tmp, "spcpq-*")))
    assert after <= before  # this query left no spill directories behind
