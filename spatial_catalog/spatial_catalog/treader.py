"""Read-only topology reader and full validator.

Opening a topology checks the manifest boundary (existence, format,
version, pinned lengths for all result files).  ``validate_full`` re-reads
nodes.tsv and edges.tsv completely and proves:

  * both files' exact length and SHA-256 match the manifest;
  * every row has the fixed header, field count, canonical encoding,
    finite coordinates, canonical numeric spelling;
  * node and edge keys are unique, gap-free and strictly ordered;
  * every edge references existing node keys; its stored endpoints equal
    the first/last positions of its geometry, and the stored planar length
    equals the independently re-derived length (bit level);
  * every node degree equals the number of edge ends incident to it, and
    its incident reference list matches the edge table exactly;
  * component declarations are correct, checked by an INDEPENDENT
    component mechanism: an in-memory union-find plus BFS over an adjacency
    map built here from scratch.  It never imports the builder's endpoint
    merge, tunion disk union-find or traversal code;
  * when the source catalog is reachable, every edge's source id, part
    index and coordinate sequence is re-derived from the catalog payload
    (deep geometry consistency), and the catalog content identity still
    matches the manifest pin.

Everything streams except the independent checker's graph, which is used
only at validation time on the artifact being checked (the build itself
remains disk-bounded; the checker's independence is the point).
"""

import json
import os
from collections import deque

from . import tformat as tf
from .catalog import Catalog
from .errors import CorruptionError

LINE_TYPES = frozenset(("LineString", "MultiLineString"))


class Topology:
    """An opened, committed line topology network."""

    def __init__(self, path):
        self._path = path
        self._manifest = tf.load_manifest(
            os.path.join(path, tf.MANIFEST_NAME))
        for name in (tf.NODES_NAME, tf.EDGES_NAME, tf.SELECTION_NAME,
                     tf.REPORT_NAME):
            p = os.path.join(path, name)
            if not os.path.isfile(p):
                raise CorruptionError("topology file missing: %s" % p)
            actual = os.path.getsize(p)
            expect = self._manifest["files"][name]["length"]
            if actual != expect:
                raise CorruptionError(
                    "%s length %d != manifest %d (truncated or corrupted)"
                    % (name, actual, expect))
        self._nodes_path = os.path.join(path, tf.NODES_NAME)
        self._edges_path = os.path.join(path, tf.EDGES_NAME)
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
    def path(self):
        return self._path

    # -------------------------------------------------------- streaming
    def iter_nodes(self):
        """Stream parsed, order-checked node rows."""
        prev_ord = None
        with open(self._nodes_path, "rb") as fh:
            magic = fh.readline()
            if magic != tf.NODES_MAGIC_LINE.encode("utf-8"):
                raise CorruptionError("nodes.tsv: bad/missing magic header")
            columns = fh.readline()
            if columns != tf.NODES_COLUMNS_LINE.encode("utf-8"):
                raise CorruptionError("nodes.tsv: bad/missing column header")
            line_no = 2
            for raw in fh:
                line_no += 1
                row = tf.parse_node_line(raw, line_no)
                n_ord = row[0]
                if prev_ord is not None and n_ord <= prev_ord:
                    raise CorruptionError(
                        "nodes.tsv: node keys not strictly increasing at "
                        "line %d" % line_no)
                prev_ord = n_ord
                yield row

    def iter_edges(self):
        """Stream parsed, order-checked edge rows."""
        prev_key = None
        with open(self._edges_path, "rb") as fh:
            magic = fh.readline()
            if magic != tf.EDGES_MAGIC_LINE.encode("utf-8"):
                raise CorruptionError("edges.tsv: bad/missing magic header")
            columns = fh.readline()
            if columns != tf.EDGES_COLUMNS_LINE.encode("utf-8"):
                raise CorruptionError("edges.tsv: bad/missing column header")
            line_no = 2
            for raw in fh:
                line_no += 1
                row = tf.parse_edge_line(raw, line_no)
                e_ord, source_id, part_index = row[0], row[1], row[2]
                key = (source_id, part_index)
                if prev_key is not None and not (prev_key < key):
                    raise CorruptionError(
                        "edges.tsv: edges not in (source_id, part) Unicode "
                        "order at line %d (%r after %r)"
                        % (line_no, key, prev_key))
                prev_key = key
                if e_ord != (line_no - 3):
                    raise CorruptionError(
                        "edges.tsv: edge key ordinal gap at line %d" % line_no)
                yield row

    # -------------------------------------------------------- validation
    def validate_full(self, catalog=None):
        """Full self-contained validation plus optional source re-identity
        and deep geometry consistency.

        ``catalog`` defaults to the path pinned in the manifest; pass False
        to skip source checks entirely.  An unreachable pinned catalog is
        reported as ``unreachable:<path>`` rather than silently accepted.
        """
        m = self._manifest
        # 1) checksums and pinned lengths for every result file
        for name in (tf.NODES_NAME, tf.EDGES_NAME, tf.SELECTION_NAME,
                     tf.REPORT_NAME):
            actual_len, actual_sha = tf.sha256_file(
                os.path.join(self._path, name))
            pinned = m["files"][name]
            if actual_len != pinned["length"]:
                raise CorruptionError(
                    "%s length %d != manifest %d"
                    % (name, actual_len, pinned["length"]))
            if actual_sha != pinned["sha256"]:
                raise CorruptionError(
                    "%s sha256 mismatch with manifest" % name)

        # 1a) the pinned build report must be internally consistent with
        # every count declared in the manifest
        with open(os.path.join(self._path, tf.REPORT_NAME),
                  "r", encoding="utf-8") as fh:
            report = json.load(fh)
        for field in ("records_scanned", "records_excluded_filter",
                      "records_type_rejected", "line_objects_selected",
                      "parts_parsed", "zero_length_parts",
                      "endpoint_records", "nodes", "edges", "components"):
            if field not in report:
                raise CorruptionError(
                    "build_report.json missing measured field %r" % field)
        manifest_to_report = {
            "objects": "records_scanned",
            "excluded_by_filter": "records_excluded_filter",
            "type_rejected": "records_type_rejected",
            "line_objects": "line_objects_selected",
            "parts": "parts_parsed",
            "zero_length_parts": "zero_length_parts",
            "endpoint_records": "endpoint_records",
            "nodes": "nodes", "edges": "edges", "components": "components"}
        for mk, rk in manifest_to_report.items():
            if m["counts"][mk] != report[rk]:
                raise CorruptionError(
                    "build_report %s=%d disagrees with manifest counts.%s=%d"
                    % (rk, report[rk], mk, m["counts"][mk]))
        for required in ("tmp_bytes_written", "spill_bytes_peak",
                         "peak_rss_bytes"):
            if not isinstance(report.get(required), int) or report[required] < 0:
                raise CorruptionError(
                    "build_report %r must be a non-negative measured integer"
                    % required)

        # 1b) selection evidence counts must match the manifest summary
        excluded = rejected = 0
        with open(os.path.join(self._path, tf.SELECTION_NAME),
                  "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("reason") == "excluded_by_filter":
                    excluded += 1
                elif row.get("reason") == "wrong_geometry_type":
                    rejected += 1
                else:
                    raise CorruptionError(
                        "selection.jsonl has unknown reason %r"
                        % row.get("reason"))
        counts = m["counts"]
        if excluded != counts["excluded_by_filter"]:
            raise CorruptionError(
                "selection.jsonl exclusions %d != manifest %d"
                % (excluded, counts["excluded_by_filter"]))
        if rejected != counts["type_rejected"]:
            raise CorruptionError(
                "selection.jsonl type rejections %d != manifest %d"
                % (rejected, counts["type_rejected"]))

        # 2) load node rows; build independent coordinate and component maps
        node_coord = {}
        node_degree = {}
        node_refs = {}
        node_component = {}
        n_nodes = 0
        for n_ord, x, y, degree, refs, comp_ord in self.iter_nodes():
            if n_ord != n_nodes:
                raise CorruptionError(
                    "nodes.tsv: gap in node ordinals at %d (expected %d)"
                    % (n_ord, n_nodes))
            node_coord[n_ord] = (x, y)
            node_degree[n_ord] = degree
            node_refs[n_ord] = refs
            node_component[n_ord] = comp_ord
            n_nodes += 1
        if n_nodes != m["counts"]["nodes"]:
            raise CorruptionError(
                "node rows %d != manifest count %d"
                % (n_nodes, m["counts"]["nodes"]))

        # 3) load edge rows and rebuild all incidence bookkeeping
        adjacency = {i: set() for i in range(n_nodes)}
        recomputed_degree = {i: 0 for i in range(n_nodes)}
        recomputed_refs = {i: [] for i in range(n_nodes)}
        edge_parts = {}                 # (source_id, part) -> edge ordinal
        edge_rows = {}
        n_edges = 0
        for (e_ord, source_id, part_index, from_ord, to_ord, coords,
             length, comp_ord) in self.iter_edges():
            if e_ord != n_edges:
                raise CorruptionError(
                    "edges.tsv: gap in edge ordinals at %d" % e_ord)
            ident = (source_id, part_index)
            if ident in edge_parts:
                raise CorruptionError(
                    "edges.tsv: duplicate edge identity %r" % (ident,))
            edge_parts[ident] = e_ord
            edge_rows[e_ord] = (source_id, part_index, from_ord, to_ord,
                                coords, length, comp_ord)
            for node_ord, end in ((from_ord, tf.END_FROM),
                                  (to_ord, tf.END_TO)):
                if node_ord not in node_coord:
                    raise CorruptionError(
                        "edge %s references dangling node N%012d"
                        % (tf.edge_key(e_ord), node_ord))
                recomputed_degree[node_ord] += 1
                recomputed_refs[node_ord].append((e_ord, end))
                adjacency[from_ord].add(to_ord)
                adjacency[to_ord].add(from_ord)
            # endpoint coordinates must equal the geometry's first/last
            if coords[0] != node_coord[from_ord]:
                raise CorruptionError(
                    "edge %s from-node coordinates %r != geometry start %r"
                    % (tf.edge_key(e_ord), node_coord[from_ord], coords[0]))
            if coords[-1] != node_coord[to_ord]:
                raise CorruptionError(
                    "edge %s to-node coordinates %r != geometry end %r"
                    % (tf.edge_key(e_ord), node_coord[to_ord], coords[-1]))
            # length re-derived independently, bit level
            derived = tf.planar_length(coords)
            if derived != length:
                raise CorruptionError(
                    "edge %s stored length %r != re-derived %r"
                    % (tf.edge_key(e_ord), length, derived))
            n_edges += 1
        if n_edges != m["counts"]["edges"]:
            raise CorruptionError(
                "edge rows %d != manifest count %d"
                % (n_edges, m["counts"]["edges"]))
        if m["counts"]["parts"] != n_edges:
            raise CorruptionError(
                "manifest parts %d != edge rows %d"
                % (m["counts"]["parts"], n_edges))

        # 4) degree and incidence cross-check against every node row
        for i in range(n_nodes):
            if recomputed_degree[i] != node_degree[i]:
                raise CorruptionError(
                    "node %s degree %d != recomputed edge-end count %d"
                    % (tf.node_key(i), node_degree[i],
                       recomputed_degree[i]))
            refs = sorted(recomputed_refs[i])
            if refs != node_refs[i]:
                raise CorruptionError(
                    "node %s incident list %r != edge-table recomputation %r"
                    % (tf.node_key(i), node_refs[i], refs))
            if not refs:
                raise CorruptionError(
                    "node %s has degree 0 (isolated node impossible)"
                    % tf.node_key(i))

        # 5) INDEPENDENT component checker: naive in-memory BFS over the
        # adjacency map built above.  No builder code is imported here.
        seen = [False] * n_nodes
        independent_components = 0
        independent_label = {}
        for start in range(n_nodes):
            if seen[start]:
                continue
            independent_components += 1
            queue = deque([start])
            seen[start] = True
            members = []
            while queue:
                cur = queue.popleft()
                members.append(cur)
                for nxt in adjacency[cur]:
                    if not seen[nxt]:
                        seen[nxt] = True
                        queue.append(nxt)
            min_member = min(members)
            for member in members:
                independent_label[member] = min_member
        if independent_components != m["counts"]["components"]:
            raise CorruptionError(
                "manifest components %d != independent BFS count %d"
                % (m["counts"]["components"], independent_components))
        for i in range(n_nodes):
            if independent_label[i] != node_component[i]:
                raise CorruptionError(
                    "node %s declared component %s != independent closure %s"
                    % (tf.node_key(i),
                       tf.component_key(node_component[i]),
                       tf.component_key(independent_label[i])))
        # every edge's endpoints must carry the same declared component
        for e_ord, (_sid, _p, fo, to, _c, _l, comp) in edge_rows.items():
            if not (node_component[fo] == node_component[to] == comp):
                raise CorruptionError(
                    "edge %s component declaration inconsistent"
                    % tf.edge_key(e_ord))

        # 6) optional source catalog deep check
        source_check = "skipped"
        if catalog is not False:
            cat_path = catalog if catalog is not None else \
                m["input_catalog"]["catalog_path"]
            if not os.path.exists(cat_path):
                source_check = "unreachable:%s" % cat_path
            else:
                source_check = self._check_source(cat_path, edge_rows,
                                                   node_coord)
        return {"ok": True, "nodes": n_nodes, "edges": n_edges,
                "components": independent_components,
                "nodes_sha256": m["files"][tf.NODES_NAME]["sha256"],
                "edges_sha256": m["files"][tf.EDGES_NAME]["sha256"],
                "source_catalog": source_check}

    def _check_source(self, cat_path, edge_rows, node_coord):
        """Re-derive every edge straight from catalog payloads and compare
        source id, part index, exact coordinate sequence and endpoints.

        The in-scope set is taken from the selection rules persisted in
        the manifest, so the check is self-contained even when the user's
        original allowlist file has moved."""
        pinned = self._manifest["input_catalog"]
        rules = self._manifest.get("selection_rules",
                                   {"prefixes": [], "allowlist": [],
                                    "has_allowlist": False})
        prefixes = tuple(rules.get("prefixes", ()))
        has_allowlist = rules.get("has_allowlist",
                                  bool(rules.get("allowlist")))
        allowlist = frozenset(rules.get("allowlist", ()))

        def in_scope(object_id):
            # No selection rule at all: whole catalog in scope.
            if not prefixes and not has_allowlist:
                return True
            if object_id in allowlist:
                return True
            return any(object_id.startswith(p) for p in prefixes)

        with Catalog(cat_path) as cat:
            current = tf.catalog_identity(cat_path, cat.manifest)
            if current["identity_digest"] != pinned["identity_digest"]:
                raise CorruptionError(
                    "source catalog content identity changed since the "
                    "topology was built (pinned %s, current %s)"
                    % (pinned["identity_digest"],
                       current["identity_digest"]))
            found = {}
            for object_id, geom in cat.iter_payloads():
                if not in_scope(object_id):
                    continue
                if geom.geom_type not in LINE_TYPES:
                    continue
                if geom.geom_type == "LineString":
                    parts = [[tuple(p) for p in geom.coords]]
                else:
                    parts = [[tuple(p) for p in g.coords] for g in geom.geoms]
                for idx, coords in enumerate(parts):
                    norm = [tf.normalize_coord(x, y) for x, y in coords]
                    found[(object_id, idx)] = norm
        for e_ord, (sid, part, fo, to, coords, _len, _comp) in edge_rows.items():
            source_coords = found.get((sid, part))
            if source_coords is None:
                raise CorruptionError(
                    "edge %s source (%r, part %d) not found in catalog"
                    % (tf.edge_key(e_ord), sid, part))
            if source_coords != coords:
                raise CorruptionError(
                    "edge %s geometry differs from catalog payload"
                    % tf.edge_key(e_ord))
            if source_coords[0] != node_coord[fo] or \
                    source_coords[-1] != node_coord[to]:
                raise CorruptionError(
                    "edge %s endpoints disagree with catalog payload"
                    % tf.edge_key(e_ord))
        if len(found) != len(edge_rows):
            raise CorruptionError(
                "selected catalog has %d line parts but topology has %d edges"
                % (len(found), len(edge_rows)))
        return current["identity_digest"]
