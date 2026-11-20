"""Command line interface.

  python -m spatial_catalog build   --input in.jsonl --out CAT [options]
  python -m spatial_catalog window  --catalog CAT --rect X1 Y1 X2 Y2
  python -m spatial_catalog nearest --catalog CAT --x X --y Y --k K
  python -m spatial_catalog info    --catalog CAT
  python -m spatial_catalog validate --catalog CAT
  python -m spatial_catalog diag    --out DIR [--n N] [--cache-bytes B]

  python -m spatial_catalog xtab build    --lines CAT --zones CAT --out XT [opts]
  python -m spatial_catalog xtab validate --crosstab XT [--lines CAT] [--zones CAT]
  python -m spatial_catalog xtab info     --crosstab XT
  python -m spatial_catalog xtab diag     --out DIR [options]

All query commands print a single JSON document including real traversal
statistics.  Exit codes: 0 ok, 2 expected failure (build rejects, bad
query, corrupt catalog), 1 unexpected error.
"""

import argparse
import json
import sys

from . import format as fmt
from .builder import build
from .catalog import Catalog
from .errors import (BuildError, CatalogError, CrossTableError,
                     InspectionError, TopologyError)


def _print(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def cmd_build(args):
    report = build(
        args.input, args.out,
        cache_bytes=args.cache_bytes,
        page_size=args.page_size,
        max_payload_bytes=args.max_payload_bytes,
        max_coord=args.max_coord,
        force=args.force,
        log=lambda *a: print(*a, file=sys.stderr),
    )
    _print(report)
    return 0


def _open(args):
    return Catalog(args.catalog,
                   page_cache_bytes=args.page_cache_bytes,
                   queue_spill_bytes=args.queue_spill_bytes,
                   verify=getattr(args, "verify", False))


def cmd_window(args):
    with _open(args) as cat:
        result = cat.window(*args.rect)
        ids = list(result)
        _print({"query": "window", "rect": list(args.rect),
                "count": len(ids), "ids": ids,
                "stats": result.stats.as_dict()})
    return 0


def cmd_nearest(args):
    with _open(args) as cat:
        result = cat.nearest(args.x, args.y, args.k)
        _print({"query": "nearest", "x": args.x, "y": args.y, "k": args.k,
                "count": len(result.results),
                "results": [{"object_id": oid, "distance": dist}
                            for oid, dist in result.results],
                "stats": result.stats.as_dict()})
    return 0


def cmd_info(args):
    with _open(args) as cat:
        _print(cat.manifest)
    return 0


def cmd_validate(args):
    with _open(args) as cat:
        _print(cat.validate_full())
    return 0


def cmd_diag(args):
    from .diag import run_diag
    _print(run_diag(args.out, n=args.n, cache_bytes=args.cache_bytes,
                    seed=args.seed))
    return 0


# ------------------------------------------------------- cross-table CLI

def cmd_xtab_build(args):
    from .xtab import build_crosstab
    report = build_crosstab(
        args.lines, args.zones, args.out,
        cache_bytes=args.cache_bytes,
        max_spill_bytes=args.max_spill_bytes, force=args.force,
        log=lambda *a: print(*a, file=sys.stderr))
    _print(report)
    return 0


def cmd_xtab_validate(args):
    from .xreader import XTable
    with XTable(args.crosstab) as xt:
        result = xt.validate_full(
            line_catalog=args.lines if args.lines else None,
            zone_catalog=args.zones if args.zones else None)
    _print(result)
    return 0


def cmd_xtab_info(args):
    from .xreader import XTable
    with XTable(args.crosstab) as xt:
        _print(xt.manifest)
    return 0


def cmd_xtab_diag(args):
    from .xdiag import run_xtab_diag
    _print(run_xtab_diag(args.out, n_lines=args.n_lines,
                         n_zones=args.n_zones,
                         cache_bytes=args.cache_bytes,
                         max_spill_bytes=args.max_spill_bytes,
                         seed=args.seed))
    return 0


# ------------------------------------------------------------- topology CLI

def cmd_topo_build(args):
    from .topo import Selection, build_topology
    selection = Selection(ids_file=args.ids_file, id_prefixes=args.id_prefix)
    report = build_topology(
        args.catalog, args.out, selection=selection,
        exclude_non_lines=args.exclude_non_lines,
        cache_bytes=args.cache_bytes,
        max_spill_bytes=args.max_spill_bytes, force=args.force,
        log=lambda *a: print(*a, file=sys.stderr))
    _print(report)
    return 0


def cmd_topo_validate(args):
    from .treader import Topology
    with Topology(args.topology) as topo:
        result = topo.validate_full(
            catalog=False if args.no_catalog else (args.catalog or None))
    _print(result)
    return 0


def cmd_topo_info(args):
    from .treader import Topology
    with Topology(args.topology) as topo:
        _print(topo.manifest)
    return 0


def cmd_topo_diag(args):
    from .tdiag import run_topo_diag
    _print(run_topo_diag(args.out, n=args.n, big=args.big,
                         cache_bytes=args.cache_bytes,
                         max_spill_bytes=args.max_spill_bytes,
                         seed=args.seed))
    return 0


# --------------------------------------------- inspection reconciliation CLI

def cmd_inspcov_build(args):
    from .insp import build_coverage
    report = build_coverage(
        args.topology, args.registry, args.inspections, args.out,
        date_from=args.date_from, date_to=args.date_to,
        cutoff_before=args.cutoff_before, cutoff_after=args.cutoff_after,
        cache_bytes=args.cache_bytes, max_spill_bytes=args.max_spill_bytes,
        force=args.force,
        log=lambda *a: print(*a, file=sys.stderr))
    _print(report)
    return 0


def cmd_inspcov_validate(args):
    from .ireader import Coverage
    with Coverage(args.coverage) as cov:
        result = cov.validate_full(
            registry=False if args.no_registry else (args.registry or None),
            topology=False if args.no_topology else (args.topology or None))
    _print(result)
    return 0


def cmd_inspcov_info(args):
    from .ireader import Coverage
    with Coverage(args.coverage) as cov:
        _print(cov.manifest)
    return 0


def cmd_inspcov_diag(args):
    from .idiag import run_insp_diag
    _print(run_insp_diag(args.out, n_edges=args.n_edges,
                         trunk_obs=args.trunk_obs,
                         cache_bytes=args.cache_bytes,
                         max_spill_bytes=args.max_spill_bytes,
                         seed=args.seed))
    return 0


def make_parser():
    p = argparse.ArgumentParser(
        prog="spatial_catalog",
        description="Disk-resident packed spatial index for offline GeoJSON "
                    "boundaries and line facilities.")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build a catalog from JSONL")
    b.add_argument("--input", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--cache-bytes", type=int, default=64 << 20,
                   help="build memory budget (sort chunks + buffers)")
    b.add_argument("--page-size", type=int, default=fmt.DEFAULT_PAGE_SIZE)
    b.add_argument("--max-payload-bytes", type=int,
                   default=fmt.DEFAULT_MAX_PAYLOAD_BYTES,
                   help="hard per-geometry size limit")
    b.add_argument("--max-coord", type=float, default=fmt.DEFAULT_MAX_COORD)
    b.add_argument("--force", action="store_true")
    b.set_defaults(func=cmd_build)

    def add_open(sp):
        sp.add_argument("--catalog", required=True)
        sp.add_argument("--page-cache-bytes", type=int,
                        default=8 << 20,
                        help="query memory budget: node page LRU cache")
        sp.add_argument("--queue-spill-bytes", type=int,
                        default=16 << 20,
                        help="query memory budget: candidate queue before "
                             "it spills to disk")

    w = sub.add_parser("window", help="closed-rectangle intersection query")
    add_open(w)
    w.add_argument("--rect", type=float, nargs=4, required=True,
                   metavar=("MINX", "MINY", "MAXX", "MAXY"))
    w.set_defaults(func=cmd_window)

    n = sub.add_parser("nearest", help="k nearest geometries to a point")
    add_open(n)
    n.add_argument("--x", type=float, required=True)
    n.add_argument("--y", type=float, required=True)
    n.add_argument("--k", type=int, required=True)
    n.set_defaults(func=cmd_nearest)

    i = sub.add_parser("info", help="print the catalog manifest")
    add_open(i)
    i.set_defaults(func=cmd_info)

    v = sub.add_parser("validate", help="full structural + checksum validation")
    add_open(v)
    v.set_defaults(func=cmd_validate)

    d = sub.add_parser("diag", help="generate data, build, and print "
                                    "traversal statistics vs full scan")
    d.add_argument("--out", required=True)
    d.add_argument("--n", type=int, default=100000)
    d.add_argument("--cache-bytes", type=int, default=8 << 20)
    d.add_argument("--seed", type=int, default=7)
    d.set_defaults(func=cmd_diag)

    # ---- cross-table -------------------------------------------------
    x = sub.add_parser("xtab", help="line/zone length-attribution "
                                    "cross-table commands")
    xsub = x.add_subparsers(dest="xtab_command", required=True)

    xb = xsub.add_parser("build", help="build the line/zone cross-table "
                                       "from two committed catalogs")
    xb.add_argument("--lines", required=True, help="line catalog directory")
    xb.add_argument("--zones", required=True, help="zone catalog directory")
    xb.add_argument("--out", required=True, help="cross-table output directory")
    xb.add_argument("--cache-bytes", type=int, default=64 << 20,
                    help="in-memory budget: sort chunk + page cache")
    xb.add_argument("--max-spill-bytes", type=int, default=1 << 30,
                    help="hard on-disk spill budget; exhaustion fails the "
                         "build instead of truncating candidates")
    xb.add_argument("--force", action="store_true")
    xb.set_defaults(func=cmd_xtab_build)

    xv = xsub.add_parser("validate", help="full cross-table validation: "
                                          "hashes, order, numerics, uniqueness")
    xv.add_argument("--crosstab", required=True)
    xv.add_argument("--lines", help="line catalog path (defaults to the "
                                    "path pinned in the manifest)")
    xv.add_argument("--zones", help="zone catalog path (defaults to the "
                                    "path pinned in the manifest)")
    xv.set_defaults(func=cmd_xtab_validate)

    xi = xsub.add_parser("info", help="print the cross-table manifest")
    xi.add_argument("--crosstab", required=True)
    xi.set_defaults(func=cmd_xtab_info)

    xd = xsub.add_parser("diag", help="runtime-generated sparse/overlap "
                                      "cross-table diagnostics vs oracle")
    xd.add_argument("--out", required=True)
    xd.add_argument("--n-lines", type=int, default=4000)
    xd.add_argument("--n-zones", type=int, default=1000)
    xd.add_argument("--cache-bytes", type=int, default=4 << 20)
    xd.add_argument("--max-spill-bytes", type=int, default=1 << 30)
    xd.add_argument("--seed", type=int, default=21)
    xd.set_defaults(func=cmd_xtab_diag)

    # ---- line topology ------------------------------------------------
    t = sub.add_parser("topo", help="line-facility node/edge topology "
                                    "network commands")
    tsub = t.add_subparsers(dest="topo_command", required=True)

    tb = tsub.add_parser("build", help="extract the node-edge network from "
                                       "a committed catalog (exact end-point "
                                       "matching, disk bounded)")
    tb.add_argument("--catalog", required=True, help="input catalog directory")
    tb.add_argument("--out", required=True, help="topology output directory")
    tb.add_argument("--ids-file",
                    help="UTF-8 text file of selected object_ids (one per "
                         "line, '#' comments and blank lines allowed)")
    tb.add_argument("--id-prefix", action="append", default=[],
                    dest="id_prefix",
                    help="select object_ids with this prefix (repeatable)")
    tb.add_argument("--exclude-non-lines", action="store_true",
                    help="non-line selected records are excluded and "
                         "summarized instead of failing the build")
    tb.add_argument("--cache-bytes", type=int, default=64 << 20,
                    help="in-memory budget: sort chunks and union-find pages")
    tb.add_argument("--max-spill-bytes", type=int, default=1 << 30,
                    help="hard on-disk spill budget; exhaustion fails the "
                         "build instead of truncating the network")
    tb.add_argument("--force", action="store_true")
    tb.set_defaults(func=cmd_topo_build)

    tv = tsub.add_parser("validate", help="full topology validation: "
                                          "hashes, keys, degrees, references, "
                                          "independent component closure")
    tv.add_argument("--topology", required=True)
    tv.add_argument("--catalog", help="source catalog path (defaults to the "
                                      "path pinned in the manifest)")
    tv.add_argument("--no-catalog", action="store_true",
                    help="skip the optional source catalog re-identity and "
                         "deep geometry consistency check")
    tv.set_defaults(func=cmd_topo_validate)

    ti = tsub.add_parser("info", help="print the topology manifest")
    ti.add_argument("--topology", required=True)
    ti.set_defaults(func=cmd_topo_info)

    td = tsub.add_parser(
        "diag", aliases=("diagnose",),
        help="runtime-generated topology diagnostics: many small "
             "components + one giant component, oracle diff "
             "('diagnose' is an alias)")
    td.add_argument("--out", required=True)
    td.add_argument("--n", type=int, default=6000)
    td.add_argument("--big", type=int, default=1500,
                    help="size of the single giant connected component")
    td.add_argument("--cache-bytes", type=int, default=1 << 20)
    td.add_argument("--max-spill-bytes", type=int, default=1 << 30)
    td.add_argument("--seed", type=int, default=31)
    td.set_defaults(func=cmd_topo_diag)

    # ---- inspection coverage reconciliation ---------------------------
    ic = sub.add_parser("inspcov", help="inspection coverage reconciliation "
                                        "(two receipt cutoffs, one report "
                                        "window, per-edge ledgers + diff)")
    icsub = ic.add_subparsers(dest="inspcov_command", required=True)

    icb = icsub.add_parser(
        "build", help="reproduce the coverage ledger at two receipt "
                      "cutoffs and reconcile them (disk bounded)")
    icb.add_argument("--topology", required=True,
                     help="committed topology directory (the edge roster)")
    icb.add_argument("--registry", required=True,
                     help="chainage registry JSONL: source_id, part_index, "
                          "chainage_end per managed edge")
    icb.add_argument("--inspections", required=True, nargs="+",
                     help="one or more unordered UTF-8 JSONL inspection "
                          "revision files")
    icb.add_argument("--date-from", required=True, dest="date_from",
                     help="report window start YYYY-MM-DD (inclusive)")
    icb.add_argument("--date-to", required=True, dest="date_to",
                     help="report window end YYYY-MM-DD (exclusive)")
    icb.add_argument("--cutoff-before", required=True, type=int,
                     dest="cutoff_before",
                     help="earlier receipt batch cutoff (>= 0)")
    icb.add_argument("--cutoff-after", required=True, type=int,
                     dest="cutoff_after",
                     help="later receipt batch cutoff (>= cutoff-before)")
    icb.add_argument("--out", required=True,
                     help="reconciliation output directory")
    icb.add_argument("--cache-bytes", type=int, default=64 << 20,
                     help="in-memory budget: sort chunks, sweep heaps and "
                          "profile stores")
    icb.add_argument("--max-spill-bytes", type=int, default=1 << 30,
                     help="hard on-disk spill budget; exhaustion fails the "
                          "build instead of publishing a partial report")
    icb.add_argument("--force", action="store_true")
    icb.set_defaults(func=cmd_inspcov_build)

    icv = icsub.add_parser(
        "validate", help="full reconciliation validation: hashes, order, "
                         "complete tiling, diff<->ledger consistency, "
                         "summary totals")
    icv.add_argument("--coverage", required=True)
    icv.add_argument("--registry", help="registry path (defaults to the "
                                        "path pinned in the manifest)")
    icv.add_argument("--topology", help="topology path (defaults to the "
                                        "path pinned in the manifest)")
    icv.add_argument("--no-registry", action="store_true",
                     help="skip the optional registry re-derivation check")
    icv.add_argument("--no-topology", action="store_true",
                     help="skip the optional topology re-identity check")
    icv.set_defaults(func=cmd_inspcov_validate)

    ici = icsub.add_parser("info", help="print the reconciliation manifest")
    ici.add_argument("--coverage", required=True)
    ici.set_defaults(func=cmd_inspcov_info)

    icd = icsub.add_parser(
        "diag", help="runtime-generated reconciliation diagnostics: one "
                     "long-tail trunk, shadowed interleaved observations "
                     "and many scattered edges, oracle-diffed")
    icd.add_argument("--out", required=True)
    icd.add_argument("--n-edges", type=int, default=400, dest="n_edges")
    icd.add_argument("--trunk-obs", type=int, default=4000,
                     dest="trunk_obs")
    icd.add_argument("--cache-bytes", type=int, default=1 << 20)
    icd.add_argument("--max-spill-bytes", type=int, default=1 << 30)
    icd.add_argument("--seed", type=int, default=47)
    icd.set_defaults(func=cmd_inspcov_diag)
    return p


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        return args.func(args)
    except BuildError as exc:
        print("build failed: %s" % exc, file=sys.stderr)
        return 2
    except CrossTableError as exc:
        print("cross-table failed: %s" % exc, file=sys.stderr)
        return 2
    except TopologyError as exc:
        print("topology failed: %s" % exc, file=sys.stderr)
        return 2
    except InspectionError as exc:
        print("inspection reconciliation failed: %s" % exc, file=sys.stderr)
        return 2
    except CatalogError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
