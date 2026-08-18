"""Unified CLI: sband-search {inventory,download}."""

from __future__ import annotations

import argparse
from pathlib import Path

from .download import DEFAULT_PRODUCTS, download_frames
from .inventory import build_inventory, print_summary, save_inventory


def _cmd_inventory(args: argparse.Namespace) -> None:
    gdf = build_inventory(
        bbox=(-180, args.max_lat, 180, args.min_lat),
        limit=args.limit,
        max_products=args.max_products,
    )
    save_inventory(gdf, args.output)
    print_summary(gdf)


def _cmd_download(args: argparse.Namespace) -> None:
    download_frames(
        args.tdf,
        args.output_dir,
        products=args.products,
        catalog_only=args.catalog_only,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="sband-search", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", help="Build the S-band GUNW frame inventory from Bhoonidhi.")
    inv.add_argument("--min-lat", type=float, default=-60.0, help="Northern latitude limit; default -60.")
    inv.add_argument("--max-lat", type=float, default=-90.0, help="Southern latitude limit; default -90.")
    inv.add_argument("-o", "--output", default="sband_frame_inventory.gpkg")
    inv.add_argument("--limit", type=int, default=500)
    inv.add_argument("--max-products", type=int, default=None)
    inv.set_defaults(func=_cmd_inventory)

    dl = sub.add_parser("download", help="Download GUNW/GOFF/GCOV (or other) products for chosen frames.")
    dl.add_argument("--output-dir", required=True, type=Path)
    dl.add_argument("--tdf", action="append", required=True, help="e.g. --tdf 147_A_137 --tdf 066_D_129")
    dl.add_argument("--products", nargs="+", default=list(DEFAULT_PRODUCTS))
    dl.add_argument("--catalog-only", action="store_true")
    dl.set_defaults(func=_cmd_download)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
