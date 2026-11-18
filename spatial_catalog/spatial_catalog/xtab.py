"""Line-facility / zone-boundary length-attribution cross-table builder.

Route chosen (see README "Candidate enumeration routes" for the full
comparison): **line-driven bbox pruning with an external merge of pair
rows**.

  1. Open both already-built catalogs read-only and derive their content
     identities from the manifests.  Input catalogs are never modified.
  2. Stream every payload of each catalog once to enforce role geometry
     types (lines: LineString/MultiLineString; zones: Polygon/
     MultiPolygon), validity and positive line length.  Any violation
     aborts with locatable evidence before a single output row exists.
  3. For every line geometry (outer streaming loop, one geometry in
     memory at a time), enumerate zone candidates with the zone catalog's
     persistent R-tree using *bounding-box* pruning only, then run the
     single-geometry exact Shapely/GEOS intersection on each candidate.
     A pair becomes a row only when the intersection has a strictly
     positive 1-D length.  Point contacts and zero-length boundary
     touches produce no row.
  4. Packed pair records go straight into a budgeted external sort
     (spill.py): candidate explosions overflow to ordered run files and
     a k-way merge; neither all lines, all zones, all candidates nor all
     rows ever reside in memory.  An explicit disk-spill budget fails
     the build loudly instead of truncating candidates.
  5. The sorted stream is adjacent-deduplicated on (source_id, zone_id),
     rendered to canonical UTF-8 TSV rows, checksummed, and committed by
     atomically publishing xtab_manifest.json last.

Every counter in the build report is measured from this run -- nothing
is estimated or hard-coded.
"""

import builtins
import json
import os
import resource
import shutil
import time
from datetime import datetime, timezone

from shapely.errors import GEOSException

from . import xformat as xf
from .catalog import Catalog, QueryStats
from .errors import (BuildError, CorruptionError, CrossTableError,
                     SpillBudgetExceeded)
from .spill import external_sort

LINE_TYPES = frozenset(("LineString", "MultiLineString"))
ZONE_TYPES = frozenset(("Polygon", "MultiPolygon"))

DEFAULT_CACHE_BYTES = 64 << 20
DEFAULT_SPILL_BYTES = 1 << 30          # explicit on-disk spill budget
_GUARD_EVERY = 2048                    # tmp-budget check cadence

# module-level alias so fault-injection tests can patch open cleanly
open = builtins.open  # noqa: A001


def _rss_bytes():
    # ru_maxrss is KiB on Linux, the supported platform.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


class XTabStats:
    """Measured build counters.  Every field is incremented by real work."""

    __slots__ = ("line_objects", "zone_objects", "bbox_candidates",
                 "exact_intersections", "positive_length_pairs",
                 "zero_length_pairs", "geos_failures", "tree_nodes_visited",
                 "zone_payloads_decoded", "duplicate_pairs_dropped",
                 "spill_runs", "spill_flushes", "spill_bytes_peak",
                 "rows_tsv_bytes", "peak_rss_bytes")

    def __init__(self):
        for slot in self.__slots__:
            setattr(self, slot, 0)

    def as_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class _DedupSorted:
    """Generator over sorted pair records that guarantees at most one
    record per (source_id, zone_id).  Line-driven enumeration cannot
    produce duplicates, but the output contract is enforced here so tree
    page visit order and run boundaries can never leak a repeat."""

    def __init__(self, records):
        self._records = records
        self.dropped = 0

    def __iter__(self):
        prev_key = None
        for rec in self._records:
            key = xf.row_record_key(rec)
            if key == prev_key:
                self.dropped += 1
                continue
            prev_key = key
            yield rec


def validate_role_geometry(object_id, geom, role, allowed_types, index=None):
    """Validate one geometry against its catalog role.

    Returns a structured evidence dict, or None when the geometry is
    acceptable.  Pure: never modifies the geometry.
    """
    gtype = geom.geom_type
    evidence = {"role": role, "object_id": object_id, "index": index,
                "geometry_type": gtype}
    if gtype not in allowed_types:
        evidence["reason"] = "wrong_geometry_type"
        evidence["detail"] = "%s catalog requires %s, got %s" % (
            role, sorted(allowed_types), gtype)
        return evidence
    if not geom.is_valid:
        evidence["reason"] = "invalid_geometry"
        evidence["detail"] = "shapely is_valid is False; no repair is attempted"
        return evidence
    if role == "line" and not (geom.length > 0.0):
        evidence["reason"] = "zero_length_line"
        evidence["detail"] = "line geometry length is %r" % geom.length
        return evidence
    return None


def validate_role_geometries(pairs, role, allowed_types):
    """Streaming validation over (object_id, geometry) pairs.

    Returns a list of structured violations (empty == all good).  Kept
    independent of catalog opening so tests can feed synthetic invalid
    geometries directly.  No geometry is modified.
    """
    violations = []
    for index, (object_id, geom) in enumerate(pairs):
        evidence = validate_role_geometry(object_id, geom, role,
                                          allowed_types, index)
        if evidence is not None:
            violations.append(evidence)
    return violations


def build_crosstab(line_catalog, zone_catalog, out_dir,
                   cache_bytes=DEFAULT_CACHE_BYTES,
                   max_spill_bytes=DEFAULT_SPILL_BYTES,
                   force=False, log=lambda *a: None):
    """Build the cross-table directory.  Raises CrossTableError on any
    invalid input, GEOS failure, budget exhaustion or I/O error; a failed
    build never publishes and an existing valid output is untouched."""
    t0 = time.monotonic()
    if not (isinstance(cache_bytes, int) and cache_bytes >= 1 << 16):
        raise CrossTableError("cache_bytes must be an int >= 65536, got %r"
                              % (cache_bytes,))
    if not (isinstance(max_spill_bytes, int) and max_spill_bytes >= 1 << 16):
        raise CrossTableError("max_spill_bytes must be an int >= 65536, got %r"
                              % (max_spill_bytes,))
    if os.path.exists(out_dir):
        if not force:
            raise CrossTableError(
                "output directory exists: %s (use force to replace)" % out_dir)
    failed_dir = out_dir + ".failed"
    backup_dir = out_dir + ".old-%d" % os.getpid()
    build_dir = out_dir + ".build-%d" % os.getpid()
    # An existing valid output is moved aside atomically and restored on
    # any failure: a failed rebuild can never destroy a committed table.
    if os.path.exists(out_dir):
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        os.replace(out_dir, backup_dir)
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)
    tmpdir = os.path.join(build_dir, "tmp")
    os.makedirs(tmpdir)
    evidence_path = os.path.join(build_dir, "xtab_rejects.jsonl")
    stats = XTabStats()
    page_cache = max(1 << 16, cache_bytes // 4)
    sort_chunk = max(1 << 16, cache_bytes // 2)
    sorted_iter = None
    line_cat = zone_cat = None
    failed_already = False

    def write_evidence(rows):
        with open(evidence_path, "w", encoding="utf-8") as fh:
            for ev in rows:
                fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")

    def fail(message, reason=None, evidence=None, exc=None):
        nonlocal failed_already
        if failed_already:
            # Nested failure while tearing down: never mask the original
            # abort, never touch any committed output.
            raise CrossTableError(message, reason=reason)
        failed_already = True
        if sorted_iter is not None and hasattr(sorted_iter, "close"):
            try:
                sorted_iter.close()
            except Exception:
                pass
        payload = {"status": "failed", "reason": reason or "build_error",
                   "detail": message,
                   "line_catalog": os.path.abspath(line_catalog),
                   "zone_catalog": os.path.abspath(zone_catalog),
                   "finished_utc": datetime.now(timezone.utc).isoformat(),
                   "counters": stats.as_dict()}
        if evidence:
            write_evidence(evidence if isinstance(evidence, list) else [evidence])
            payload["evidence_file"] = "xtab_rejects.jsonl"
        with open(os.path.join(build_dir, "build_report.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        if os.path.exists(failed_dir):
            shutil.rmtree(failed_dir)
        try:
            os.replace(build_dir, failed_dir)
        except OSError:
            shutil.rmtree(build_dir, ignore_errors=True)
        ev = os.path.join(failed_dir, "xtab_rejects.jsonl") if evidence else None
        # restore the previously committed output, if any
        if os.path.exists(backup_dir) and not os.path.exists(out_dir):
            os.replace(backup_dir, out_dir)
        if isinstance(exc, SpillBudgetExceeded):
            raise SpillBudgetExceeded(
                "%s (evidence: %s)" % (message, failed_dir),
                evidence_path=ev, reason=reason)
        if isinstance(exc, KeyboardInterrupt):
            # consumer abort: bookkeeping done, then propagate the interrupt
            raise
        raise CrossTableError("%s (evidence: %s)" % (message, failed_dir),
                              evidence_path=ev, reason=reason)
    try:
        # ------------------------------------------------ phase 0: open
        log("phase 0: open input catalogs read-only")
        try:
            line_cat = Catalog(line_catalog, page_cache_bytes=page_cache,
                               queue_spill_bytes=page_cache)
            zone_cat = Catalog(zone_catalog, page_cache_bytes=page_cache,
                               queue_spill_bytes=page_cache)
        except CrossTableError:
            raise
        except Exception as exc:
            fail("cannot open input catalog: %s: %s"
                 % (type(exc).__name__, exc), reason="catalog_open_failed",
                 evidence={"reason": "catalog_open_failed", "detail": str(exc)})

        line_identity = xf.catalog_identity(line_catalog, line_cat.manifest)
        zone_identity = xf.catalog_identity(zone_catalog, zone_cat.manifest)

        # ------------------------------------------------ phase 1: validate
        log("phase 1: stream-validate role geometry types")
        zone_violations = validate_role_geometries(
            zone_cat.iter_payloads(), "zone", ZONE_TYPES)
        stats.zone_objects = zone_cat.object_count
        if zone_violations:
            fail("zone catalog contains %d invalid record(s)"
                 % len(zone_violations), reason="invalid_zone_input",
                 evidence=zone_violations)

        # Line validation is fused into the enumeration streaming pass
        # below (one decode per line, fail-fast with locatable evidence),
        # so there is no separate full line scan.  Zones are validated up
        # front because enumeration only ever reaches bbox-matching zones;
        # an invalid zone that never matches a line must still abort the
        # build rather than being silently skipped.
        stats.line_objects = line_cat.object_count

        # ------------------------------------------------ phase 2: enumerate
        log("phase 2: bbox-pruned candidate enumeration + exact intersections")

        def pair_records():
            guard = 0
            for line_index, (source_id, line_geom) in enumerate(
                    line_cat.iter_payloads()):
                # fused role validation: no geometry is ever modified
                bad = validate_role_geometry(source_id, line_geom, "line",
                                             LINE_TYPES, line_index)
                if bad is not None:
                    fail("line catalog contains invalid record(s)",
                         reason="invalid_line_input", evidence=[bad])
                xf.check_row_id(source_id, "line")
                source_length = float(line_geom.length)
                bbox_stats = QueryStats()
                for poff, plen in zone_cat.bbox_window_entries(
                        line_geom.bounds, bbox_stats):
                    stats.bbox_candidates += 1
                    zone_id, zone_geom = zone_cat._read_payload(poff, plen)
                    stats.zone_payloads_decoded += 1
                    stats.exact_intersections += 1
                    try:
                        inter = line_geom.intersection(zone_geom)
                        intersect_length = float(inter.length)
                    except GEOSException as exc:
                        fail("GEOS overlay failed for pair (line=%r, zone=%r): %s"
                             % (source_id, zone_id, exc),
                             reason="overlay_failure",
                             evidence={"reason": "overlay_failure",
                                       "source_id": source_id,
                                       "zone_id": zone_id, "detail": str(exc)},
                             exc=exc)
                    if intersect_length > 0.0:
                        xf.check_row_id(zone_id, "zone")
                        ratio = intersect_length / source_length
                        stats.positive_length_pairs += 1
                        yield xf.pack_row_record(
                            source_id, zone_id, intersect_length,
                            source_length, ratio)
                    else:
                        stats.zero_length_pairs += 1
                    guard += 1
                    if guard >= _GUARD_EVERY:
                        guard = 0
                        used = _dir_bytes(tmpdir)
                        if used > stats.spill_bytes_peak:
                            stats.spill_bytes_peak = used
                        if used > max_spill_bytes:
                            raise SpillBudgetExceeded(
                                "spill budget %d bytes exhausted at %d bytes "
                                "(candidate explosion; no rows truncated)"
                                % (max_spill_bytes, used),
                                reason="spill_budget_exceeded")
                stats.tree_nodes_visited += bbox_stats.nodes_visited

        def on_flush(_path, n_records):
            stats.spill_runs += 1
            stats.spill_flushes += n_records
            used = _dir_bytes(tmpdir)
            if used > stats.spill_bytes_peak:
                stats.spill_bytes_peak = used
            if used > max_spill_bytes:
                raise SpillBudgetExceeded(
                    "spill budget %d bytes exhausted at %d bytes "
                    "(candidate explosion; no rows truncated)"
                    % (max_spill_bytes, used), reason="spill_budget_exceeded")

        # external_sort materializes all spill runs before returning its
        # merge iterator, so the disk footprint is fully bounded here.
        sorted_iter = external_sort(
            pair_records(), key_fn=xf.row_record_key,
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="xtab",
            on_flush=on_flush)
        used = _dir_bytes(tmpdir)
        stats.spill_bytes_peak = max(stats.spill_bytes_peak, used)
        if used > max_spill_bytes:
            fail("spill budget %d bytes exhausted at %d bytes (candidate "
                 "explosion; no rows truncated)" % (max_spill_bytes, used),
                 reason="spill_budget_exceeded",
                 evidence={"reason": "spill_budget_exceeded",
                           "spill_bytes": used,
                           "max_spill_bytes": max_spill_bytes},
                 exc=SpillBudgetExceeded("budget exhausted"))

        # ----------------------------------------- phase 3: merge + TSV rows
        log("phase 3: external merge, dedupe, deterministic rows")
        rows_path = os.path.join(build_dir, xf.ROWS_NAME)
        dedup = _DedupSorted(sorted_iter)
        row_count = 0
        with open(rows_path, "wb") as rows_fh:
            rows_fh.write(xf.HEADER_BYTES)
            for rec in dedup:
                vals = xf.unpack_row_record(rec)
                rows_fh.write(xf.encode_row_line(*vals))
                row_count += 1
            rows_fh.flush()
            os.fsync(rows_fh.fileno())
        stats.duplicate_pairs_dropped = dedup.dropped
        stats.rows_tsv_bytes = os.path.getsize(rows_path)
        stats.spill_bytes_peak = max(stats.spill_bytes_peak,
                                     _dir_bytes(tmpdir))
        stats.peak_rss_bytes = max(_rss_bytes(), stats.peak_rss_bytes)
        # internal accounting invariants: every exact intersection is
        # classified exactly once, and dedupe can only remove duplicates
        if stats.positive_length_pairs + stats.zero_length_pairs \
                != stats.exact_intersections:
            raise CrossTableError(
                "internal counter invariant violated: positive %d + zero %d "
                "!= exact %d" % (stats.positive_length_pairs,
                                 stats.zero_length_pairs,
                                 stats.exact_intersections),
                reason="counter_invariant")
        if row_count + stats.duplicate_pairs_dropped \
                != stats.positive_length_pairs:
            raise CrossTableError(
                "internal row invariant violated: rows %d + dropped %d "
                "!= positive pairs %d"
                % (row_count, stats.duplicate_pairs_dropped,
                   stats.positive_length_pairs), reason="counter_invariant")

        # ------------------------------------------------ phase 4: commit
        log("phase 4: checksums, manifest (commit point), atomic publish")
        rows_len, rows_sha = xf.sha256_file(rows_path)
        if rows_len != stats.rows_tsv_bytes:
            raise CorruptionError("rows.tsv length changed after close")
        manifest = {
            "format": xf.XTABLE_FORMAT_NAME,
            "version": xf.XTABLE_FORMAT_VERSION,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "line_catalog": line_identity,
            "zone_catalog": zone_identity,
            "record_count": row_count,
            "row_order": "source_id,zone_id Unicode code point order",
            "numeric_encoding": "python repr float64 (locale independent, "
                                "exact round-trip)",
            "row_semantics": "one row per (source_id, zone_id) whose "
                             "intersection has positive 1-D length",
            "files": {
                xf.ROWS_NAME: {"length": rows_len, "sha256": rows_sha},
            },
            "cache_bytes": cache_bytes,
            "max_spill_bytes": max_spill_bytes,
        }
        report = {
            "status": "ok",
            "line_objects": stats.line_objects,
            "zone_objects": stats.zone_objects,
            "bbox_candidates": stats.bbox_candidates,
            "exact_intersections": stats.exact_intersections,
            "positive_length_pairs": stats.positive_length_pairs,
            "zero_length_pairs": stats.zero_length_pairs,
            "duplicate_pairs_dropped": stats.duplicate_pairs_dropped,
            "output_rows": row_count,
            "tree_nodes_visited": stats.tree_nodes_visited,
            "zone_payloads_decoded": stats.zone_payloads_decoded,
            "spill_runs": stats.spill_runs,
            "spill_flush_records": stats.spill_flushes,
            "spill_bytes_peak": stats.spill_bytes_peak,
            "max_spill_bytes": max_spill_bytes,
            "cache_bytes": cache_bytes,
            "rows_tsv_bytes": rows_len,
            "rows_tsv_sha256": rows_sha,
            "peak_rss_bytes": stats.peak_rss_bytes,
            "duration_s": round(time.monotonic() - t0, 3),
        }
        with open(os.path.join(build_dir, xf.REPORT_NAME), "w",
                  encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Manifest is the commit point: written last, atomically.
        xf.dump_manifest(os.path.join(build_dir, xf.MANIFEST_NAME), manifest)
        dir_fd = os.open(build_dir, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        os.replace(build_dir, out_dir)
        log("done: %d rows from %d candidates (%d exact intersections)"
            % (row_count, stats.bbox_candidates, stats.exact_intersections))
        return report

    except KeyboardInterrupt as exc:
        # Consumer abort: run the same failure bookkeeping (evidence dir,
        # backup restore, no partial publish) then re-raise the interrupt
        # so callers can detect cancellation rather than a normal error.
        fail("build aborted by consumer", reason="aborted",
             evidence={"reason": "aborted",
                       "counters": stats.as_dict()}, exc=exc)
    except SpillBudgetExceeded as exc:
        # SpillBudgetExceeded is a CrossTableError subclass; run the full
        # failure bookkeeping (evidence + restore) but re-raise the
        # specific type so callers can distinguish budget exhaustion.
        fail("on-disk spill budget exhausted; build aborted without "
             "publishing", reason="spill_budget_exceeded",
             evidence={"reason": "spill_budget_exceeded",
                       "spill_bytes_peak": stats.spill_bytes_peak,
                       "max_spill_bytes": max_spill_bytes}, exc=exc)
    except CrossTableError:
        raise
    except (GEOSException, CorruptionError, BuildError) as exc:
        fail("%s: %s" % (type(exc).__name__, exc),
             reason=type(exc).__name__.lower(),
             evidence={"reason": "runtime_error", "detail": str(exc)})
    except OSError as exc:
        fail("I/O error: %s" % exc, reason="io_error",
             evidence={"reason": "io_error", "detail": str(exc)})
    finally:
        for cat in (line_cat, zone_cat):
            if cat is not None:
                cat.close()
        shutil.rmtree(build_dir, ignore_errors=True)
