"""Download NISAR S-band products from Bhoonidhi for chosen frames.

Output layout::

    OUTPUT/GUNW/*.h5
    OUTPUT/GOFF/*.h5
    OUTPUT/GCOV/*.h5
    OUTPUT/<other product>/*.h5   (only if you asked for it)

GUNW/GOFF/GCOV collection names are confirmed against the live Bhoonidhi
catalog. Any other product code (e.g. GSLC, RIFG, RUNW, ROFF) is guessed as
``NISAR_SSAR_<product>`` -- it follows the same naming pattern but has not
been verified, so treat those results as unconfirmed until you see real
search hits.

There is no shortcut from a known GUNW path to a GOFF/GCOV path on
Bhoonidhi (unlike the ASF/S3 L-band trees) -- every product is searched for
independently by (track, direction, frame).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import requests

from .bhoonidhi import (
    authenticate,
    collection_for,
    credentials,
    download_product,
    parse_tdfs,
    product_filename,
    search_tdf,
)

DEFAULT_PRODUCTS = ("GUNW", "GOFF", "GCOV")


def download_frames(
    tdfs: Iterable[str],
    output_dir: Path | str,
    *,
    products: Iterable[str] = DEFAULT_PRODUCTS,
    user: str | None = None,
    password: str | None = None,
    catalog_only: bool = False,
) -> list[dict]:
    """Download the given TRACK_DIR_FRAME frames for each requested product.

    tdfs: e.g. ["147_A_137", "066_D_129"]
    products: product codes to fetch per frame, e.g. ("GUNW", "GOFF", "GCOV").
    Returns the manifest list (also written to OUTPUT/bhoonidhi_manifest.json).
    """
    output_dir = Path(output_dir).expanduser().resolve()
    parsed_tdfs = parse_tdfs(tdfs)
    products = list(dict.fromkeys(p.upper() for p in products))
    for product in products:
        (output_dir / product).mkdir(parents=True, exist_ok=True)

    if user is None or password is None:
        user, password = credentials()

    manifest: list[dict] = []
    with requests.Session() as session:
        headers = {"Authorization": f"Bearer {authenticate(session, user, password)}"}
        for product in products:
            collection = collection_for(product)
            seen: set[str] = set()
            for track, direction, frame in parsed_tdfs:
                label = f"{track:03d}_{direction}_{frame:03d}"
                features = search_tdf(session, headers, user, password, collection, track, direction, frame)
                print(f"[{product}] {label}: {len(features)} online product(s)")
                for feature in features:
                    product_id = str(feature.get("id", "")).strip()
                    if not product_id or product_id in seen:
                        continue
                    seen.add(product_id)
                    record = {
                        "product": product,
                        "collection": collection,
                        "tdf": label,
                        "id": product_id,
                        "online": (feature.get("properties") or {}).get("Online"),
                    }
                    if catalog_only:
                        record["status"] = "catalog-only"
                    else:
                        record["status"] = download_product(
                            session, headers, user, password, collection, product_id, output_dir / product
                        )
                        print(f"  {record['status']}: {product_filename(product_id)}")
                    manifest.append(record)

    manifest_path = output_dir / "bhoonidhi_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    counts = {p: sum(row["product"] == p for row in manifest) for p in products}
    print(f"Manifest: {manifest_path}")
    print("Products: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    missing = [p for p, v in counts.items() if v == 0]
    if missing:
        print(f"WARNING: no online products found for: {', '.join(missing)}")
    return manifest


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--tdf", action="append", required=True, help="e.g. --tdf 147_A_137 --tdf 066_D_129")
    parser.add_argument(
        "--products",
        nargs="+",
        default=list(DEFAULT_PRODUCTS),
        help=f"Product codes to fetch; default {list(DEFAULT_PRODUCTS)}. Others (GSLC, RIFG, RUNW, ROFF, ...) are unverified guesses.",
    )
    parser.add_argument("--catalog-only", action="store_true", help="Write the manifest without downloading HDF5 products.")
    args = parser.parse_args()

    download_frames(args.tdf, args.output_dir, products=args.products, catalog_only=args.catalog_only)


if __name__ == "__main__":
    main()
