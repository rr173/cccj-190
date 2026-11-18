"""Command line interface.

  python -m spatial_catalog build   --input in.jsonl --out CAT [options]
  python -m spatial_catalog window  --catalog CAT --rect X1 Y1 X2 Y2
  python -m spatial_catalog nearest --catalog CAT --x X --y Y --k K
  python -m spatial_catalog info    --catalog CAT
  python -m spatial_catalog validate --catalog CAT
  python -m spatial_catalog diag    --out DIR [--n N] [--cache-bytes B]

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
from .errors import BuildError, CatalogError


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
    return p


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        return args.func(args)
    except BuildError as exc:
        print("build failed: %s" % exc, file=sys.stderr)
        return 2
    except CatalogError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
