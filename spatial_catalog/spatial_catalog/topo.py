"""Line-facility topology extraction builder (Route A).

Chosen route (the two compliant routes are compared in the README):

  **A -- externally sort the line-part ends, merge equal coordinates into
  nodes, then complete connected components with a disk union-find.**

Staged state (every stage streams; nothing assumes the graph fits RAM):

  phase 0  open the existing catalog READ-ONLY; pin its content identity.
  phase 1  stream every selected record once.  Only LineString/
           MultiLineString become edges (one edge per part, source id +
           part index retained).  Each part emits one binary edge record
           and two endpoint records to length-prefixed raw files; one
           geometry is resident at a time.
  phase 2  external sort edges by (source_id, part) and endpoints by exact
           (x, y).  The endpoint merge groups bit-equal coordinates only:
           no snapping, no tolerance, no new vertices; crossings, interior
           touches and overlaps never connect.  Groups assign stable node
           ordinals and degrees (a self loop contributes two ends).
  phase 3  re-sort the node-end incidences and the scan-order/edge-order
           map, then merge-join node ordinals back onto every edge and
           write a flat edge table.
  phase 4  disk union-find (tunion.py: parent/rank in files, bounded page
           caches) unites the ends of every edge; component representatives
           are relabelled to the smallest member node via external sorts,
           so component keys are independent of catalog physical layout.
  phase 5  render edges.tsv and nodes.tsv in stable order with canonical
           encodings, cross-checking every degree and incidence count.
  phase 6  checksums, selection evidence, measured report, then the
           manifest as the atomic commit point.

Memory/disk accounting: sort chunks and union-find pages are sized from
``cache_bytes``; all long-tail state lives in budgeted spill files and an
explicit ``max_spill_bytes`` ceiling fails the build loudly instead of
publishing a truncated network.  A failed or interrupted build never has a
manifest and a previously committed target is restored untouched.
"""

import builtins
import json
import os
import resource
import shutil
import struct
import time
from datetime import datetime, timezone

from . import tformat as tf
from .catalog import Catalog
from .errors import (CorruptionError, CrossTableError, NotACatalogError,
                     TopologyError, TopologySpillBudgetExceeded)
from .spill import external_sort
from .tunion import DiskUnionFind

LINE_TYPES = frozenset(("LineString", "MultiLineString"))

DEFAULT_CACHE_BYTES = 64 << 20
DEFAULT_SPILL_BYTES = 1 << 30
_GUARD_EVERY = 2048

# Fault-injection hooks used by the failure tests: (stage, kind).  When
# _FAULT_STAGE matches the stage currently entered, _maybe_fault raises the
# configured exception (OSError for media failure, KeyboardInterrupt for a
# consumer abort).  Production runs always leave both at None.
_FAULT_STAGE = None
_FAULT_KIND = "oserror"

# module-level alias so fault-injection tests can patch open cleanly
open = builtins.open  # noqa: A001

_LEN = struct.Struct(">I")
_SEQMAP = struct.Struct(">QQ")          # edge_seq, edge_ord
_NODEEND = struct.Struct(">QBQ")        # edge_seq, end, node_ord
_JOIN = struct.Struct(">QQQ")           # edge_ord, from_ord, to_ord
_COMPRAW = struct.Struct(">QQ")         # root, node
_NODEMIN = struct.Struct(">QQ")         # node, minnode
_INC2 = struct.Struct(">QQB")           # node_ord, edge_ord, end


def _maybe_fault(stage):
    if _FAULT_STAGE == stage:
        if _FAULT_KIND == "keyboard":
            raise KeyboardInterrupt("injected consumer abort at %s" % stage)
        raise OSError("injected storage media failure at %s" % stage)


def _rss_bytes():
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


def _read_lp(path):
    with open(path, "rb") as fh:
        while True:
            head = fh.read(_LEN.size)
            if not head:
                return
            (n,) = _LEN.unpack(head)
            rec = fh.read(n)
            if len(rec) != n:
                raise TopologyError("spill file %s truncated" % path,
                                    reason="truncated_spill")
            yield rec


class TopoStats:
    """Measured build counters; every field is incremented by real work."""

    __slots__ = ("records_scanned", "records_excluded_filter",
                 "records_type_rejected", "line_objects_selected",
                 "parts_parsed", "zero_length_parts", "endpoint_records",
                 "nodes", "edges", "components", "spill_runs",
                 "spill_bytes_peak", "tmp_bytes_written",
                 "nodes_tsv_bytes", "edges_tsv_bytes", "peak_rss_bytes",
                 "union_find_finds", "union_find_unions",
                 "union_find_evictions")

    def __init__(self):
        for slot in self.__slots__:
            setattr(self, slot, 0)

    def as_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


def inject_fault(stage, kind="oserror"):
    """Test hook: fail the next build when it enters ``stage``."""
    global _FAULT_STAGE, _FAULT_KIND
    _FAULT_STAGE = stage
    _FAULT_KIND = kind


def clear_fault():
    """Test hook: remove any injected failure."""
    global _FAULT_STAGE
    _FAULT_STAGE = None


# ----------------------------------------------------------- input filters

class Selection:
    """Deterministic object-id selection: explicit allowlist file and/or
    prefix rules.  With no rules every id is selected."""

    def __init__(self, ids_file=None, id_prefixes=()):
        self.prefixes = tuple(id_prefixes or ())
        self.allow = None
        self.allow_bytes = 0
        if ids_file is not None:
            allow = set()
            with open(ids_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n").rstrip("\r")
                    if not line or line.startswith("#"):
                        continue
                    allow.add(line)
            # An empty allowlist is legal: it is the explicit empty
            # selection, which must produce a valid empty network.
            self.allow = frozenset(allow)
            self.allow_bytes = os.path.getsize(ids_file)

    def selected(self, object_id):
        # No rules at all: the whole catalog is in scope.
        if self.allow is None and not self.prefixes:
            return True
        if self.allow is not None and object_id in self.allow:
            return True
        return any(object_id.startswith(p) for p in self.prefixes)

    def spec(self):
        return {"prefixes": list(self.prefixes),
                "allowlist_size": 0 if self.allow is None else len(self.allow),
                "allowlist_file_bytes": self.allow_bytes}

    def rules(self):
        """Persistable, self-contained selection rules so validation can
        re-derive the exact in-scope set without depending on the original
        (mutable, user-owned) allowlist file."""
        return {"prefixes": list(self.prefixes),
                # has_allowlist distinguishes "no allowlist given" (whole
                # catalog in scope) from an explicitly empty allowlist.
                "has_allowlist": self.allow is not None,
                "allowlist": [] if self.allow is None
                else sorted(self.allow)}


def _part_coords(geom):
    """Yield (part_index, coords_list) for a LineString/MultiLineString."""
    if geom.geom_type == "LineString":
        yield 0, [tuple(p) for p in geom.coords]
    else:
        for idx, part in enumerate(geom.geoms):
            yield idx, [tuple(p) for p in part.coords]


def validate_part(object_id, part_index, coords):
    """Defensive per-part validation; raises TopologyError with source id
    and part position on any malformed part."""
    if not coords:
        raise TopologyError(
            "source %r part %d: empty part has no coordinates"
            % (object_id, part_index), reason="empty_part",
            detail={"object_id": object_id, "part": part_index})
    if len(coords) < 2:
        raise TopologyError(
            "source %r part %d: line part needs >= 2 positions, got %d"
            % (object_id, part_index, len(coords)),
            reason="degenerate_part",
            detail={"object_id": object_id, "part": part_index})
    for j, (x, y) in enumerate(coords):
        if not (x == x and y == y) or not (
                x not in (float("inf"), float("-inf"))
                and y not in (float("inf"), float("-inf"))):
            raise TopologyError(
                "source %r part %d position %d: non-finite coordinate %r"
                % (object_id, part_index, j, (x, y)),
                reason="non_finite_coordinate",
                detail={"object_id": object_id, "part": part_index,
                        "position": j})


# --------------------------------------------------------------- builder

def build_topology(catalog_dir, out_dir, selection=None,
                   exclude_non_lines=False,
                   cache_bytes=DEFAULT_CACHE_BYTES,
                   max_spill_bytes=DEFAULT_SPILL_BYTES,
                   force=False, log=lambda *a: None):
    """Build a topology network directory from a committed catalog."""
    global _FAULT_STAGE
    t0 = time.monotonic()
    if not (isinstance(cache_bytes, int) and cache_bytes >= 1 << 16):
        raise TopologyError("cache_bytes must be an int >= 65536, got %r"
                            % (cache_bytes,), reason="bad_budget")
    if not (isinstance(max_spill_bytes, int) and max_spill_bytes >= 1 << 16):
        raise TopologyError("max_spill_bytes must be an int >= 65536, got %r"
                            % (max_spill_bytes,), reason="bad_budget")
    selection = selection or Selection()
    stats = TopoStats()
    failed_dir = out_dir + ".failed"
    backup_dir = out_dir + ".old-%d" % os.getpid()
    build_dir = out_dir + ".build-%d" % os.getpid()
    if os.path.exists(out_dir):
        if not force:
            raise TopologyError(
                "output directory exists: %s (use force to replace)" % out_dir,
                reason="output_exists")
    if os.path.exists(out_dir):
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        os.replace(out_dir, backup_dir)
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)
    tmpdir = os.path.join(build_dir, "tmp")
    os.makedirs(tmpdir)
    evidence_path = os.path.join(build_dir, "topo_rejects.jsonl")
    selection_path = os.path.join(build_dir, tf.SELECTION_NAME)
    sort_chunk = max(1 << 16, cache_bytes // 2)
    # Union-find page budget: sized so that whenever the node table spans
    # more pages than the bounded cache holds, parent/rank pages genuinely
    # evict to disk rather than quietly fitting in RAM.
    uf_page = max(1 << 12, cache_bytes // 8)
    cat = None
    failed_already = False
    # temp file handles/iterators that must be closed on every exit path
    uf = None
    sorted_iters = []

    def write_evidence(rows):
        with open(evidence_path, "w", encoding="utf-8") as fh:
            for ev in rows:
                fh.write(json.dumps(ev, ensure_ascii=False,
                                    sort_keys=True) + "\n")

    def fail(message, reason=None, evidence=None, exc=None):
        nonlocal failed_already
        if failed_already:
            raise TopologyError(message, reason=reason)
        failed_already = True
        for it in sorted_iters:
            close = getattr(it, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        if uf is not None:
            try:
                uf.close()
            except Exception:
                pass
        payload = {"status": "failed", "reason": reason or "build_error",
                   "detail": message,
                   "catalog": os.path.abspath(catalog_dir),
                   "finished_utc": datetime.now(timezone.utc).isoformat(),
                   "counters": stats.as_dict()}
        if evidence:
            evs = evidence if isinstance(evidence, list) else [evidence]
            write_evidence(evs)
            payload["evidence_file"] = "topo_rejects.jsonl"
        with open(os.path.join(build_dir, tf.REPORT_NAME), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        if os.path.exists(failed_dir):
            shutil.rmtree(failed_dir)
        try:
            os.replace(build_dir, failed_dir)
        except OSError:
            shutil.rmtree(build_dir, ignore_errors=True)
        if os.path.exists(backup_dir) and not os.path.exists(out_dir):
            os.replace(backup_dir, out_dir)
        ev = os.path.join(failed_dir, "topo_rejects.jsonl") if evidence else None
        if isinstance(exc, KeyboardInterrupt):
            raise
        if isinstance(exc, TopologySpillBudgetExceeded):
            raise TopologySpillBudgetExceeded(
                "%s (evidence: %s)" % (message, failed_dir),
                evidence_path=ev, reason=reason)
        raise TopologyError("%s (evidence: %s)" % (message, failed_dir),
                            evidence_path=ev, reason=reason)

    def check_budget():
        used = _dir_bytes(tmpdir)
        if used > stats.spill_bytes_peak:
            stats.spill_bytes_peak = used
        if used > max_spill_bytes:
            raise TopologySpillBudgetExceeded(
                "spill budget %d bytes exhausted at %d bytes; no partial "
                "network is published" % (max_spill_bytes, used),
                reason="spill_budget_exceeded")

    def on_flush(_path, n_records):
        stats.spill_runs += 1
        check_budget()

    try:
        # ------------------------------------------ phase 0: open catalog
        log("phase 0: open catalog read-only")
        _maybe_fault("scan")
        try:
            cat = Catalog(catalog_dir, page_cache_bytes=max(1 << 16,
                                                            cache_bytes // 4),
                          queue_spill_bytes=max(1 << 16, cache_bytes // 4))
        except (NotACatalogError, CorruptionError, CrossTableError) as exc:
            fail("cannot open catalog: %s" % exc,
                 reason="catalog_open_failed",
                 evidence={"reason": "catalog_open_failed", "detail": str(exc)})
        identity = tf.catalog_identity(catalog_dir, cat.manifest)

        # ---------------------------------- phase 1: scan + raw spill files
        log("phase 1: stream scan, validate, emit edge/endpoint records")
        raw_edges = os.path.join(tmpdir, "edges.raw")
        raw_ends = os.path.join(tmpdir, "endpoints.raw")
        selection_rows = []
        edge_seq = 0
        guard = 0
        with open(raw_edges, "wb") as efh, open(raw_ends, "wb") as enfh:
            for leaf_index, entry in enumerate(cat.iter_leaf_entries()):
                poff, plen = entry[4], entry[5]
                try:
                    object_id, geom = cat._read_payload(poff, plen)
                except Exception as exc:
                    fail("corrupt payload at leaf #%d (payload offset %d, "
                         "length %d): %s" % (leaf_index, poff, plen, exc),
                         reason="corrupt_payload",
                         evidence={"reason": "corrupt_payload",
                                   "object_id": None, "part": None,
                                   "leaf_index": leaf_index,
                                   "payload_offset": poff,
                                   "payload_length": plen,
                                   "detail": str(exc)}, exc=exc)
                stats.records_scanned += 1
                if not selection.selected(object_id):
                    stats.records_excluded_filter += 1
                    selection_rows.append({
                        "object_id": object_id,
                        "reason": "excluded_by_filter",
                        "geometry_type": geom.geom_type})
                    continue
                if geom.geom_type not in LINE_TYPES:
                    ev = {"reason": "wrong_geometry_type",
                          "object_id": object_id, "part": None,
                          "geometry_type": geom.geom_type,
                          "detail": "topology accepts only LineString/"
                                    "MultiLineString"}
                    if not exclude_non_lines:
                        fail("selected record %r has non-line geometry %s"
                             % (object_id, geom.geom_type),
                             reason="wrong_geometry_type", evidence=[ev])
                    stats.records_type_rejected += 1
                    selection_rows.append(ev)
                    continue
                tf.check_topo_id(object_id, "source")
                stats.line_objects_selected += 1
                for part_index, coords in _part_coords(geom):
                    validate_part(object_id, part_index, coords)
                    coords = [tf.normalize_coord(x, y) for x, y in coords]
                    length = tf.planar_length(coords)
                    if length == 0.0:
                        stats.zero_length_parts += 1
                    rec = tf.pack_edge_record(object_id, part_index,
                                              edge_seq, coords)
                    efh.write(_LEN.pack(len(rec)))
                    efh.write(rec)
                    stats.tmp_bytes_written += _LEN.size + len(rec)
                    fx, fy = coords[0]
                    tx, ty = coords[-1]
                    e0 = tf.pack_endpoint(fx, fy, edge_seq, tf.END_FROM)
                    e1 = tf.pack_endpoint(tx, ty, edge_seq, tf.END_TO)
                    enfh.write(_LEN.pack(len(e0)))
                    enfh.write(e0)
                    enfh.write(_LEN.pack(len(e1)))
                    enfh.write(e1)
                    stats.tmp_bytes_written += 2 * (_LEN.size + len(e0))
                    stats.endpoint_records += 2
                    stats.parts_parsed += 1
                    edge_seq += 1
                    guard += 1
                    if guard >= _GUARD_EVERY:
                        guard = 0
                        check_budget()
        with open(selection_path, "w", encoding="utf-8") as fh:
            for row in selection_rows:
                fh.write(json.dumps(row, ensure_ascii=False,
                                    sort_keys=True) + "\n")
        check_budget()
        n_edges = edge_seq

        # ------------- phase 2: external sorts + endpoint merge -> nodes
        log("phase 2: external sort edges and endpoints; merge node classes")
        _maybe_fault("sort_edges")
        edges_sorted = external_sort(
            _read_lp(raw_edges), key_fn=tf.edge_record_key,
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="edges",
            on_flush=on_flush)
        sorted_iters.append(edges_sorted)
        ends_sorted = external_sort(
            _read_lp(raw_ends), key_fn=tf.endpoint_record_key,
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="ends",
            on_flush=on_flush)
        sorted_iters.append(ends_sorted)

        edges_final = os.path.join(tmpdir, "edges.final")
        seqmap_raw = os.path.join(tmpdir, "seqmap.raw")
        nodemap_path = os.path.join(tmpdir, "nodemap.bin")
        nodeend_path = os.path.join(tmpdir, "nodeends.bin")
        # 2a: consume sorted edges -> final order + scan->final ordinal map.
        # The scan sequence is carried inside every edge record, so the map
        # is produced directly in this single streaming pass.
        with open(edges_final, "wb") as ffh, open(seqmap_raw, "wb") as sfh:
            edge_ord = 0
            for rec in edges_sorted:
                source_id, part, scan_seq, coords = tf.unpack_edge_record(rec)
                sfh.write(_SEQMAP.pack(scan_seq, edge_ord))
                ffh.write(_LEN.pack(len(rec)))
                ffh.write(rec)
                edge_ord += 1
            if edge_ord != n_edges:
                fail("edge count invariant violated: sorted %d != emitted %d"
                     % (edge_ord, n_edges), reason="counter_invariant")
        check_budget()

        # 2b: endpoint merge -> node ordinals + node-end incidences
        _maybe_fault("nodemap")
        n_nodes = 0
        degree_check = 0
        with open(nodemap_path, "wb") as nfh, open(nodeend_path, "wb") as neh:
            cur_xy = None
            cur_ord = -1
            for rec in ends_sorted:
                x, y, seq, end = tf.unpack_endpoint(rec)
                if cur_xy is None or (x, y) != cur_xy:
                    cur_xy = (x, y)
                    cur_ord = n_nodes
                    if cur_ord > tf.MAX_ORDINAL:
                        fail("node count exceeds stable key width %d"
                             % tf.MAX_ORDINAL, reason="key_space_exhausted")
                    nfh.write(tf.pack_nodemap(cur_ord, x, y))
                    n_nodes += 1
                neh.write(_NODEEND.pack(seq, end, cur_ord))
                degree_check += 1
        stats.nodes = n_nodes
        if degree_check != stats.endpoint_records:
            fail("endpoint invariant violated: merged %d != emitted %d"
                 % (degree_check, stats.endpoint_records),
                 reason="counter_invariant")
        if degree_check != 2 * n_edges:
            fail("end count %d != 2 * edges %d"
                 % (degree_check, n_edges), reason="counter_invariant")
        check_budget()

        # -------- phase 3: join node ordinals back onto ordered edges
        log("phase 3: incidence re-sort and node-key join")
        _maybe_fault("joins")
        ne_sorted = external_sort(
            _read_fixed(nodeend_path, _NODEEND),
            key_fn=lambda r: _NODEEND.unpack(r)[:2],
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="nodeend",
            on_flush=on_flush)
        sorted_iters.append(ne_sorted)
        sm_sorted = external_sort(
            _read_fixed(seqmap_raw, _SEQMAP),
            key_fn=lambda r: _SEQMAP.unpack(r)[:1],
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="seqmap",
            on_flush=on_flush)
        sorted_iters.append(sm_sorted)
        joins_path = os.path.join(tmpdir, "joins.bin")
        # both streams are edge_seq sorted; node ends are (from,to) ordered
        with open(joins_path, "wb") as jfh:
            sm_iter = iter(sm_sorted)
            ne_iter = iter(ne_sorted)
            for sm_rec in sm_iter:
                seq, edge_ord = _SEQMAP.unpack(sm_rec)
                from_ord = to_ord = None
                # pull the two end records for this seq (ends sorted S<T)
                got = 0
                while got < 2:
                    try:
                        ne_rec = next(ne_iter)
                    except StopIteration:
                        fail("missing node-end incidence for edge seq %d"
                             % seq, reason="dangling_incidence")
                    s2, end2, ord2 = _NODEEND.unpack(ne_rec)
                    if s2 != seq:
                        fail("incidence join misaligned: seq %d != %d"
                             % (s2, seq), reason="join_misalignment")
                    if end2 == tf.END_FROM:
                        from_ord = ord2
                    else:
                        to_ord = ord2
                    got += 1
                if from_ord is None or to_ord is None:
                    fail("edge seq %d is missing an end node" % seq,
                         reason="dangling_incidence")
                jfh.write(_JOIN.pack(edge_ord, from_ord, to_ord))
            # no incidence may remain unconsumed
            try:
                extra = next(ne_iter)
                fail("unconsumed node-end incidence: %r"
                     % (_NODEEND.unpack(extra),), reason="join_misalignment")
            except StopIteration:
                pass
        joins_sorted = external_sort(
            _read_fixed(joins_path, _JOIN),
            key_fn=lambda r: (_JOIN.unpack(r)[0],),
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="joins",
            on_flush=on_flush)
        sorted_iters.append(joins_sorted)
        check_budget()

        # -------- phase 4: flat edge table + disk union-find components
        log("phase 4: disk union-find component closure")
        _maybe_fault("union")
        flat_path = os.path.join(tmpdir, "edges.flat")
        inc2_path = os.path.join(tmpdir, "inc2.bin")
        parent_path = os.path.join(tmpdir, "uf-parent.bin")
        rank_path = os.path.join(tmpdir, "uf-rank.bin")
        uf = DiskUnionFind(n_nodes, parent_path, rank_path, page_bytes=uf_page)
        edge_count = 0
        final_iter = iter(_read_lp(edges_final))
        join_iter = iter(joins_sorted)
        with open(flat_path, "wb") as flfh, open(inc2_path, "wb") as i2fh:
            while True:
                try:
                    edge_ord_rec = next(final_iter)
                except StopIteration:
                    edge_ord_rec = None
                try:
                    join_rec = next(join_iter)
                except StopIteration:
                    join_rec = None
                if edge_ord_rec is None and join_rec is None:
                    break
                if edge_ord_rec is None or join_rec is None:
                    fail("edge/join stream length mismatch (edge left=%s, "
                         "join left=%s)"
                         % (edge_ord_rec is not None, join_rec is not None),
                         reason="join_misalignment")
                j_ord, from_ord, to_ord = _JOIN.unpack(join_rec)
                source_id, part, _scan_seq, coords = \
                    tf.unpack_edge_record(edge_ord_rec)
                if j_ord != edge_count:
                    fail("join order gap: edge ord %d at position %d"
                         % (j_ord, edge_count), reason="join_misalignment")
                flfh.write(tf.pack_flat_edge(source_id, part, from_ord,
                                             to_ord, coords))
                i2fh.write(_INC2.pack(from_ord, edge_count, tf.END_FROM))
                i2fh.write(_INC2.pack(to_ord, edge_count, tf.END_TO))
                uf.union(from_ord, to_ord)
                edge_count += 1
        if edge_count != n_edges:
            fail("flat edge count %d != %d" % (edge_count, n_edges),
                 reason="counter_invariant")
        stats.union_find_finds = uf.stats["finds"]
        stats.union_find_unions = uf.stats["unions"]
        stats.union_find_evictions = (
            uf.stats["parent_page_evictions"]
            + uf.stats["rank_page_evictions"])
        check_budget()

        # relabel components to the smallest member node, fully bounded.
        # pass 1: (root, node) sorted by ROOT ONLY; stream each root group
        # and write one (root, minnode) row plus (node, root) member rows.
        compraw_path = os.path.join(tmpdir, "compraw.bin")
        with open(compraw_path, "wb") as cfh:
            for node_i, root_i in uf.roots():
                cfh.write(_COMPRAW.pack(root_i, node_i))
        uf.close()
        uf = None
        comp_sorted = external_sort(
            _read_fixed(compraw_path, _COMPRAW),
            key_fn=lambda r: (_COMPRAW.unpack(r)[0],),
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="comp",
            on_flush=on_flush)
        sorted_iters.append(comp_sorted)
        rootmin_path = os.path.join(tmpdir, "rootmin.bin")
        memberraw_path = os.path.join(tmpdir, "memberraw.bin")
        with open(rootmin_path, "wb") as rmfh, \
                open(memberraw_path, "wb") as mbfh:
            cur_root = None
            min_node = None
            for rec in comp_sorted:
                root_i, node_i = _COMPRAW.unpack(rec)
                if cur_root is None or root_i != cur_root:
                    if cur_root is not None:
                        rmfh.write(_NODEMIN.pack(cur_root, min_node))
                    cur_root = root_i
                    min_node = node_i
                elif node_i < min_node:
                    min_node = node_i
                mbfh.write(_COMPRAW.pack(root_i, node_i))
            if cur_root is not None:
                rmfh.write(_NODEMIN.pack(cur_root, min_node))
        # pass 2: both files sorted by root; streaming merge replaces each
        # member's root with its component's minimum member node, emitting
        # (node, minnode) rows in node order for the direct-offset lookup.
        rootmin_sorted = external_sort(
            _read_fixed(rootmin_path, _NODEMIN),
            key_fn=lambda r: (_NODEMIN.unpack(r)[0],),
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="rootmin",
            on_flush=on_flush)
        sorted_iters.append(rootmin_sorted)
        member_sorted = external_sort(
            _read_fixed(memberraw_path, _COMPRAW),
            key_fn=lambda r: (_COMPRAW.unpack(r)[0],),
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="member",
            on_flush=on_flush)
        sorted_iters.append(member_sorted)
        nodecomp_unsorted = os.path.join(tmpdir, "nodecomp.raw")
        with open(nodecomp_unsorted, "wb") as ncfh:
            rm_iter = iter(rootmin_sorted)
            cur_root = None
            cur_min = None
            # Both streams are sorted by root; rootmin has exactly one row
            # per distinct root, member rows repeat the same root.  Pull a
            # new rootmin row only when the member root changes.
            for m_rec in member_sorted:
                m_root, m_node = _COMPRAW.unpack(m_rec)
                if m_root != cur_root:
                    rm_rec = next(rm_iter, None)
                    if rm_rec is None:
                        fail("component root %d has no minimum label"
                             % m_root, reason="component_invariant")
                    rm_root, cur_min = _NODEMIN.unpack(rm_rec)
                    if rm_root != m_root:
                        fail("root/min join misaligned: rootmin %d != %d"
                             % (rm_root, m_root),
                             reason="component_invariant")
                    cur_root = m_root
                ncfh.write(_NODEMIN.pack(m_node, cur_min))
            # every rootmin row must have been consumed exactly once
            if next(rm_iter, None) is not None:
                fail("unconsumed root/min labels remain",
                     reason="component_invariant")
        # pass 3: order by node so nodecomp.bin is a direct-offset array
        nodecomp_sorted = external_sort(
            _read_fixed(nodecomp_unsorted, _NODEMIN),
            key_fn=lambda r: (_NODEMIN.unpack(r)[0],),
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="nodecomp",
            on_flush=on_flush)
        sorted_iters.append(nodecomp_sorted)
        nodecomp_path = os.path.join(tmpdir, "nodecomp.bin")
        prev_node = -1
        seen_mins = set()
        with open(nodecomp_path, "wb") as ncfh:
            for rec in nodecomp_sorted:
                node_i, min_node = _NODEMIN.unpack(rec)
                if node_i != prev_node + 1:
                    fail("node component array gap at node %d" % node_i,
                         reason="component_invariant")
                prev_node = node_i
                seen_mins.add(min_node)
                ncfh.write(struct.pack(">Q", min_node))
        n_components = len(seen_mins)
        stats.components = n_components
        check_budget()

        # ---------------- phase 5: render deterministic TSV outputs
        log("phase 5: render nodes.tsv and edges.tsv")
        _maybe_fault("render")
        comp_cache = _FixedLookup(nodecomp_path, tf.NODECOMP_SIZE,
                                  max_entries=max(64, uf_page // 64))
        edges_tsv = os.path.join(build_dir, tf.EDGES_NAME)
        with open(edges_tsv, "wb") as out:
            out.write(tf.EDGES_HEADER_BYTES)
            edge_ord = 0
            for rec in _read_flat_stream(flat_path):
                source_id, part, from_ord, to_ord, coords = \
                    tf.unpack_flat_edge(rec)
                length = tf.planar_length(coords)
                c_from = comp_cache.get(from_ord)
                c_to = comp_cache.get(to_ord)
                if c_from != c_to:
                    fail("edge %d crosses components %d/%d"
                         % (edge_ord, c_from, c_to),
                         reason="component_invariant")
                out.write(tf.encode_edge_line(
                    edge_ord, source_id, part, from_ord, to_ord, coords,
                    length, c_from))
                edge_ord += 1
            if edge_ord != n_edges:
                fail("rendered %d edges != %d" % (edge_ord, n_edges),
                     reason="counter_invariant")
            out.flush()
            os.fsync(out.fileno())
        stats.edges_tsv_bytes = os.path.getsize(edges_tsv)

        inc_sorted = external_sort(
            _read_fixed(inc2_path, _INC2),
            key_fn=lambda r: _INC2.unpack(r),
            chunk_bytes=sort_chunk, tmpdir=tmpdir, tag="inc2",
            on_flush=on_flush)
        sorted_iters.append(inc_sorted)
        nodes_tsv = os.path.join(build_dir, tf.NODES_NAME)
        with open(nodes_tsv, "wb") as out:
            out.write(tf.NODES_HEADER_BYTES)
            node_index = 0
            cur_node = None
            refs = []
            rendered = 0

            def flush_node():
                nonlocal rendered
                if cur_node is None:
                    return
                if cur_node != node_index - 1:
                    fail("node ordinal gap while rendering: %d after %d"
                         % (cur_node, node_index - 1),
                         reason="node_ordinal_gap")
                x, y = _nodemap_coord(nodemap_path, cur_node)
                if len(refs) == 0:
                    fail("node %d has no incident edges" % cur_node,
                         reason="degree_invariant")
                comp = comp_cache.get(cur_node)
                out.write(tf.encode_node_line(cur_node, x, y, len(refs),
                                              refs, comp))
                rendered += 1

            for rec in inc_sorted:
                node_ord, edge_ord, end = _INC2.unpack(rec)
                if cur_node is None or node_ord != cur_node:
                    flush_node()
                    cur_node = node_ord
                    node_index += 1
                    refs = []
                refs.append((edge_ord, end))
            flush_node()
            if rendered != n_nodes:
                fail("rendered %d nodes != %d" % (rendered, n_nodes),
                     reason="counter_invariant")
            out.flush()
            os.fsync(out.fileno())
        stats.nodes_tsv_bytes = os.path.getsize(nodes_tsv)
        comp_cache.close()
        check_budget()

        # ---------------- phase 6: manifest commit (atomic publish)
        log("phase 6: checksums and manifest commit point")
        _maybe_fault("commit")
        nodes_len, nodes_sha = tf.sha256_file(nodes_tsv)
        edges_len, edges_sha = tf.sha256_file(edges_tsv)
        sel_len, sel_sha = tf.sha256_file(selection_path)
        stats.edges = n_edges
        stats.spill_bytes_peak = max(stats.spill_bytes_peak,
                                     _dir_bytes(tmpdir))
        stats.peak_rss_bytes = max(_rss_bytes(), stats.peak_rss_bytes)
        report = {
            "status": "ok",
            **stats.as_dict(),
            "cache_bytes": cache_bytes,
            "max_spill_bytes": max_spill_bytes,
            "nodes_tsv_sha256": nodes_sha,
            "edges_tsv_sha256": edges_sha,
            "duration_s": round(time.monotonic() - t0, 3),
        }
        report_path = os.path.join(build_dir, tf.REPORT_NAME)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        report_len, report_sha = tf.sha256_file(report_path)
        manifest = {
            "format": tf.TOPO_FORMAT_NAME,
            "version": tf.TOPO_FORMAT_VERSION,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "input_catalog": identity,
            "selection": selection.spec(),
            "selection_rules": selection.rules(),
            "exclude_non_lines": bool(exclude_non_lines),
            "connection_semantics": "exact float64 coordinate equality of "
                                    "line ends only; no snapping, tolerance, "
                                    "interior intersection or vertex insert",
            "counts": {"objects": stats.records_scanned,
                       "excluded_by_filter": stats.records_excluded_filter,
                       "type_rejected": stats.records_type_rejected,
                       "line_objects": stats.line_objects_selected,
                       "parts": stats.parts_parsed,
                       "zero_length_parts": stats.zero_length_parts,
                       "endpoint_records": stats.endpoint_records,
                       "nodes": n_nodes, "edges": n_edges,
                       "components": n_components},
            "files": {
                tf.NODES_NAME: {"length": nodes_len, "sha256": nodes_sha},
                tf.EDGES_NAME: {"length": edges_len, "sha256": edges_sha},
                tf.SELECTION_NAME: {"length": sel_len, "sha256": sel_sha},
                tf.REPORT_NAME: {"length": report_len, "sha256": report_sha},
            },
            "ordering": "nodes by node key; edges by (source_id, part_index) "
                        "Unicode code point order",
            "cache_bytes": cache_bytes,
            "max_spill_bytes": max_spill_bytes,
        }
        shutil.rmtree(tmpdir, ignore_errors=True)
        tf.dump_manifest(os.path.join(build_dir, tf.MANIFEST_NAME), manifest)
        dir_fd = os.open(build_dir, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        os.replace(build_dir, out_dir)
        log("done: %d nodes, %d edges, %d components"
            % (n_nodes, n_edges, n_components))
        return report

    except KeyboardInterrupt as exc:
        fail("topology build aborted by consumer", reason="aborted",
             evidence={"reason": "aborted", "counters": stats.as_dict()},
             exc=exc)
    except TopologySpillBudgetExceeded as exc:
        fail("on-disk spill budget exhausted; build aborted without "
             "publishing", reason="spill_budget_exceeded",
             evidence={"reason": "spill_budget_exceeded",
                       "spill_bytes_peak": stats.spill_bytes_peak,
                       "max_spill_bytes": max_spill_bytes}, exc=exc)
    except TopologyError:
        raise
    except CorruptionError as exc:
        fail("%s: %s" % (type(exc).__name__, exc),
             reason="corruption",
             evidence={"reason": "corruption", "detail": str(exc)})
    except OSError as exc:
        fail("I/O error: %s" % exc, reason="io_error",
             evidence={"reason": "io_error", "detail": str(exc)})
    finally:
        if cat is not None:
            cat.close()
        if uf is not None:
            try:
                uf.close()
            except Exception:
                pass
        shutil.rmtree(build_dir, ignore_errors=True)


# ----------------------------------------------------------- small helpers

def _read_fixed(path, st):
    with open(path, "rb") as fh:
        size = st.size
        while True:
            buf = fh.read(size)
            if not buf:
                return
            if len(buf) != size:
                raise TopologyError("fixed-width file %s truncated" % path,
                                    reason="truncated_spill")
            yield buf


def _read_flat_stream(path):
    with open(path, "rb") as fh:
        while True:
            head = fh.read(tf._FLAT_HEAD.size)
            if not head:
                return
            sn, part, fo, to, ncoords = tf._FLAT_HEAD.unpack(head)
            rest = fh.read(sn + ncoords * tf._COORD.size)
            if len(rest) != sn + ncoords * tf._COORD.size:
                raise TopologyError("flat edge file %s truncated" % path,
                                    reason="truncated_spill")
            yield head + rest


def _nodemap_coord(nodemap_path, ordinal):
    with open(nodemap_path, "rb") as fh:
        fh.seek(ordinal * tf.NODEMAP_SIZE)
        buf = fh.read(tf.NODEMAP_SIZE)
    if len(buf) != tf.NODEMAP_SIZE:
        raise TopologyError("nodemap read truncated for ordinal %d" % ordinal,
                            reason="truncated_spill")
    _o, x, y = tf.unpack_nodemap(buf)
    return x, y


class _FixedLookup:
    """Bounded direct-offset read cache over a fixed-width u64 file."""

    def __init__(self, path, width, max_entries=256):
        self._fh = open(path, "rb")
        self._width = width
        self._max = max(8, int(max_entries))
        self._cache = {}
        from collections import OrderedDict
        self._order = OrderedDict()

    def get(self, index):
        if index in self._cache:
            self._order.move_to_end(index)
            return self._cache[index]
        self._fh.seek(index * self._width)
        buf = self._fh.read(self._width)
        if len(buf) != self._width:
            raise TopologyError("fixed lookup truncated at index %d" % index,
                                reason="truncated_spill")
        (value,) = struct.unpack(">Q", buf)
        self._cache[index] = value
        self._order[index] = True
        if len(self._order) > self._max:
            old, _ = self._order.popitem(last=False)
            self._cache.pop(old, None)
        return value

    def close(self):
        self._fh.close()

