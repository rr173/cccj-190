"""Read-only cross-table reader and full validator.

Opening a cross-table checks the manifest boundary (existence, format,
version, pinned rows.tsv length).  ``validate_full`` re-reads the whole
rows file and proves:

  * the two fixed header lines are exact;
  * every row parses with canonical numeric encoding, finite positive
    values, ratio == intersect/source at the float64 bit level;
  * rows are strictly ordered by (source_id, zone_id) Unicode code point
    order, with no duplicate pair;
  * row count and the file's length/SHA-256 match the manifest;
  * when the original input catalogs are reachable, their content
    identities still match the identities pinned in the manifest.
"""

import os

from . import xformat as xf
from .catalog import Catalog
from .errors import CorruptionError, CrossTableError


class XTable:
    """An opened, committed line/zone cross-table."""

    def __init__(self, path):
        self._path = path
        self._manifest = xf.load_manifest(os.path.join(path, xf.MANIFEST_NAME))
        self._rows_path = os.path.join(path, xf.ROWS_NAME)
        if not os.path.isfile(self._rows_path):
            raise CorruptionError("cross-table file missing: %s" % self._rows_path)
        actual = os.path.getsize(self._rows_path)
        expect = self._manifest["files"][xf.ROWS_NAME]["length"]
        if actual != expect:
            raise CorruptionError(
                "rows.tsv length %d != manifest %d (truncated or corrupted)"
                % (actual, expect))
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self._closed = True

    @property
    def manifest(self):
        return self._manifest

    @property
    def record_count(self):
        return self._manifest["record_count"]

    @property
    def path(self):
        return self._path

    def iter_rows(self):
        """Stream validated (source_id, zone_id, intersect_length,
        source_length, ratio) tuples in on-disk order.

        Every row is grammar- and numeric-canonical checked, and the
        stream enforces strict (source_id, zone_id) ordering with unique
        pairs, so even a streaming consumer cannot observe unsorted or
        duplicated output."""
        if self._closed:
            raise CrossTableError("cross-table is closed")
        prev_key = None
        with open(self._rows_path, "rb") as fh:
            magic = fh.readline()
            if magic != xf.MAGIC_LINE.encode("utf-8"):
                raise CorruptionError("rows.tsv: bad/ missing magic header line")
            columns = fh.readline()
            if columns != xf.COLUMNS_LINE.encode("utf-8"):
                raise CorruptionError("rows.tsv: bad/missing column header line")
            line_no = 2
            for raw in fh:
                line_no += 1
                row = xf.parse_row_line(raw, line_no)
                key = (row[0], row[1])
                if prev_key is not None and not (prev_key < key):
                    if prev_key == key:
                        raise CorruptionError(
                            "rows.tsv: duplicate pair %r at line %d"
                            % (key, line_no))
                    raise CorruptionError(
                        "rows.tsv: rows not in Unicode code point order at "
                        "line %d (%r after %r)" % (line_no, key, prev_key))
                prev_key = key
                yield row

    def validate_full(self, line_catalog=None, zone_catalog=None):
        """Full self-contained validation plus optional input re-identity.

        ``line_catalog`` / ``zone_catalog`` default to the paths pinned in
        the manifest; pass an explicit path or False to override
        (False skips the optional input re-identity check).
        """
        # 1) checksum over the whole rows file
        actual_len, actual_sha = xf.sha256_file(self._rows_path)
        pinned = self._manifest["files"][xf.ROWS_NAME]
        if actual_len != pinned["length"]:
            raise CorruptionError(
                "rows.tsv length %d != manifest %d" % (actual_len, pinned["length"]))
        if actual_sha != pinned["sha256"]:
            raise CorruptionError("rows.tsv sha256 mismatch with manifest")

        # 2) header + every row: grammar, canonical numerics, order, unique
        prev_key = None
        count = 0
        for row in self.iter_rows():
            key = (row[0], row[1])
            if prev_key is not None and not (prev_key < key):
                if prev_key == key:
                    raise CorruptionError(
                        "rows.tsv: duplicate pair %r" % (key,))
                raise CorruptionError(
                    "rows.tsv: rows not in Unicode code point order at %r" % (key,))
            prev_key = key
            count += 1
        if count != self._manifest["record_count"]:
            raise CorruptionError(
                "rows.tsv contains %d rows but manifest pins %d"
                % (count, self._manifest["record_count"]))

        # 3) optional input catalog re-identity
        checked = {}
        for role, override in (("line", line_catalog), ("zone", zone_catalog)):
            pinned_id = self._manifest["%s_catalog" % role]
            if override is False:
                checked[role] = "skipped"
                continue
            cat_path = override if override is not None else pinned_id["catalog_path"]
            if not os.path.exists(cat_path):
                checked[role] = "unreachable:%s" % cat_path
                continue
            with Catalog(cat_path) as cat:
                current = xf.catalog_identity(cat_path, cat.manifest)
            if current["identity_digest"] != pinned_id["identity_digest"]:
                raise CorruptionError(
                    "%s catalog content identity changed since the cross-table "
                    "was built (pinned %s, current %s)"
                    % (role, pinned_id["identity_digest"],
                       current["identity_digest"]))
            checked[role] = current["identity_digest"]

        return {"ok": True, "rows": count, "rows_sha256": actual_sha,
                "input_identities": checked}
