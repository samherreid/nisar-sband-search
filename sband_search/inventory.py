"""Build a frame inventory directly from the Bhoonidhi NISAR S-band GUNW catalog.

This intentionally does NOT cross-reference results against a static AOI file
(e.g. an ``antarctica.json``/``.yaml`` track plan). The inventory is just
whatever frames the catalog actually returns for the requested search box --
the search box narrows the query, but frame membership is never filtered
against a separate expected-frame list. If Bhoonidhi doesn't know about a
frame, it isn't in the inventory; if it does, it is, regardless of whether
anyone had it enumerated in advance.

Output: a GeoDataFrame of one row per cataloged GUNW granule, with
``track``, ``direction`` (A/D), ``frame``, ``frame_id`` (``TRACK_DIR_FRAME``,
matching the format used by --tdf elsewhere in this repo) and a footprint
geometry, ready to save (GeoPackage/CSV) or plot on an interactive map.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import geopandas as gpd
import requests
from shapely.geometry import shape

from .bhoonidhi import (
    AUTH_URL,
    BASE,
    CONFIRMED_COLLECTIONS,
    SEARCH_INTERVAL_S,
    SEARCH_URL,
    authenticate,
    fail,
    request_retry,
)

GUNW_COLLECTION = CONFIRMED_COLLECTIONS["GUNW"]


def _next_link(data: dict) -> Optional[dict]:
    return next((link for link in data.get("links", []) if link.get("rel") == "next"), None)


def _refresh_access_token(session, user, refresh_token):
    if not refresh_token:
        return None, None
    payload = {"userId": user, "refresh_token": refresh_token, "grant_type": "refresh_token"}
    r = session.post(AUTH_URL, json=payload, timeout=60)
    if not r.ok:
        return None, None
    data = r.json()
    return data.get("access_token"), data.get("refresh_token", refresh_token)


def _deduplicate_features(features: list[dict]) -> list[dict]:
    unique: dict[str, dict] = {}
    anonymous = []
    for feature in features:
        product_id = feature.get("id")
        if product_id is None:
            anonymous.append(feature)
        else:
            unique[product_id] = feature
    return list(unique.values()) + anonymous


def _login(session: requests.Session, user: str, password: str) -> tuple[str, Optional[str]]:
    """Authenticate once, returning both the access and refresh tokens."""
    r = session.post(
        AUTH_URL,
        json={"userId": user, "password": password, "grant_type": "password"},
        timeout=60,
    )
    if not r.ok:
        fail(f"authentication failed: HTTP {r.status_code}\n{r.text[:1000]}")
    data = r.json()
    token = data.get("access_token")
    if not token:
        fail(f"no access_token returned:\n{json.dumps(data, indent=2)[:2000]}")
    return str(token), data.get("refresh_token")


def fetch_all_gunw(
    session: requests.Session,
    user: str,
    password: str,
    *,
    bbox: list[float],
    limit: int = 500,
    max_products: Optional[int] = None,
) -> list[dict]:
    """Query every cataloged NISAR S-band GUNW granule intersecting bbox.

    bbox is [west, south, east, north] in degrees (STAC/Bhoonidhi order).
    Falls back to recursive spatial-tile subdivision if a continuation
    token 500s server-side (a documented Bhoonidhi bug).
    """
    token, refresh_token = _login(session, user, password)
    headers = {"Authorization": f"Bearer {token}"}

    page_limit = min(limit, 500)
    if max_products is not None:
        page_limit = min(page_limit, max_products)
    if limit > 500:
        print(f"WARNING: Bhoonidhi's maximum page size is 500; using 500 instead of {limit}.")

    payload = {"collections": [GUNW_COLLECTION], "bbox": bbox, "limit": page_limit}

    all_features: list[dict] = []
    url, method, body = SEARCH_URL, "POST", payload
    page = 0
    pagination_failed = False

    while True:
        page += 1
        r = request_retry(
            session,
            method,
            url,
            headers=headers,
            json=body if method == "POST" else None,
            attempts=3 if page > 1 else 8,
        )

        if r.status_code == 401:
            new_token, new_refresh = _refresh_access_token(session, user, refresh_token)
            if not new_token:
                new_token = authenticate(session, user, password)
                new_refresh = refresh_token
            token, refresh_token = new_token, new_refresh
            headers["Authorization"] = f"Bearer {token}"
            r = request_retry(session, method, url, headers=headers, json=body if method == "POST" else None)

        if page > 1 and 500 <= r.status_code <= 599:
            print(f"  Continuation request failed with HTTP {r.status_code}; switching to spatially tiled searches.")
            pagination_failed = True
            break

        if not r.ok:
            fail(f"search failed on page {page}: HTTP {r.status_code}\n{r.text[:2000]}")

        data = r.json()
        features = data.get("features", [])
        all_features.extend(features)
        print(f"Page {page}: {len(features)} products (running total {len(all_features)})")

        if max_products is not None and len(all_features) >= max_products:
            print(f"Reached requested test cap of {max_products} products.")
            return all_features[:max_products]

        next_link = _next_link(data)
        if not next_link:
            break

        url = next_link.get("href")
        if not url:
            break
        if url.startswith("/"):
            url = BASE + url
        method = next_link.get("method", "GET").upper()
        body = next_link.get("body")
        if method == "POST" and body is None:
            body = dict(payload)
            if "token" in next_link:
                body["token"] = next_link["token"]
        time.sleep(SEARCH_INTERVAL_S)

    if not pagination_failed:
        return all_features

    print("Restarting search in spatial tiles (duplicate IDs will be removed)...")
    tiled_features: list[dict] = []
    tile_count = 0
    max_depth = 16

    def fetch_tile(tile_bbox, depth=0):
        nonlocal token, refresh_token, headers, tile_count
        tile_payload = {"collections": [GUNW_COLLECTION], "bbox": tile_bbox, "limit": page_limit}
        r = request_retry(session, "POST", SEARCH_URL, headers=headers, json=tile_payload)

        if r.status_code == 401:
            new_token, new_refresh = _refresh_access_token(session, user, refresh_token)
            if not new_token:
                new_token = authenticate(session, user, password)
                new_refresh = refresh_token
            token, refresh_token = new_token, new_refresh
            headers["Authorization"] = f"Bearer {token}"
            r = request_retry(session, "POST", SEARCH_URL, headers=headers, json=tile_payload)

        if r.status_code == 404:
            try:
                error_data = r.json()
            except ValueError:
                error_data = {}
            if "NO RESULTS" in str(error_data.get("Description", "")).upper():
                tile_count += 1
                print(f"  Empty tile {tile_count}: bbox={tile_bbox}")
                time.sleep(SEARCH_INTERVAL_S)
                return

        if not r.ok:
            fail(f"tiled search failed for bbox {tile_bbox}: HTTP {r.status_code}\n{r.text[:2000]}")

        data = r.json()
        features = data.get("features", [])
        time.sleep(SEARCH_INTERVAL_S)
        if not _next_link(data):
            tile_count += 1
            tiled_features.extend(features)
            print(f"  Complete tile {tile_count}: bbox={tile_bbox}, {len(features)} products")
            return

        if depth >= max_depth:
            fail(f"a spatial tile still exceeds the page size after {max_depth} subdivisions (bbox {tile_bbox})")

        west, south, east, north = tile_bbox
        if (east - west) / 360 >= (north - south) / 180:
            mid = (west + east) / 2
            children = [[west, south, mid, north], [mid, south, east, north]]
        else:
            mid = (south + north) / 2
            children = [[west, south, east, mid], [west, mid, east, north]]
        for child in children:
            fetch_tile(child, depth + 1)

    fetch_tile(payload["bbox"])
    deduplicated = _deduplicate_features(tiled_features)
    print(f"Tiled search returned {len(tiled_features)} tile hits; {len(deduplicated)} unique products.")
    return deduplicated


def _scalarize(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _property(props: dict, candidates: list[str]):
    lower = {str(k).lower(): v for k, v in props.items()}
    for key in candidates:
        if key.lower() in lower:
            return lower[key.lower()]
    return None


def _normalize_direction(value) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().upper()
    if v in {"A", "ASC", "ASCENDING"} or "ASCEND" in v:
        return "A"
    if v in {"D", "DES", "DESC", "DESCENDING"} or "DESCEND" in v:
        return "D"
    return None


def features_to_gdf(features: list[dict]) -> gpd.GeoDataFrame:
    """Convert raw STAC features straight into the frame inventory -- no AOI filtering."""
    rows = []
    for feature in features:
        geom_json = feature.get("geometry")
        if not geom_json:
            continue

        product_id = feature.get("id", "")
        props = feature.get("properties") or {}

        track = _property(props, ["Track", "track"])
        frame = _property(props, ["Frame", "frame"])
        direction = _normalize_direction(
            _property(props, ["Node", "Orbit_Direction", "orbit_direction", "orbitDirection", "Direction"])
        )
        track = int(track) if track is not None else None
        frame = int(frame) if frame is not None else None

        row = {
            "product_id": product_id,
            "granule_id": product_id,
            "collection": feature.get("collection", GUNW_COLLECTION),
            "track": track,
            "direction": direction,
            "frame": frame,
            "frame_id": (
                f"{track:03d}_{direction}_{frame:03d}"
                if track is not None and direction is not None and frame is not None
                else None
            ),
        }

        used = {c.casefold() for c in row} | {"geometry"}
        for key, value in props.items():
            col = str(key)
            if col.casefold() in used:
                col = "stac_" + col
            base, suffix = col, 2
            while col.casefold() in used:
                col = f"{base}_{suffix}"
                suffix += 1
            row[col] = _scalarize(value)
            used.add(col.casefold())

        try:
            row["geometry"] = shape(geom_json)
        except Exception as e:
            print(f"WARNING: skipped invalid geometry for {product_id}: {e}")
            continue

        rows.append(row)

    if not rows:
        return gpd.GeoDataFrame(
            columns=["product_id", "granule_id", "collection", "track", "direction", "frame", "frame_id", "geometry"],
            geometry="geometry",
            crs="EPSG:4326",
        )

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    bad = ~gdf.geometry.is_valid
    if bad.any():
        print(f"Repairing {int(bad.sum())} invalid footprint geometries...")
        try:
            gdf.loc[bad, "geometry"] = gdf.loc[bad, "geometry"].make_valid()
        except Exception:
            gdf.loc[bad, "geometry"] = gdf.loc[bad, "geometry"].buffer(0)
    return gdf


def build_inventory(
    *,
    user: Optional[str] = None,
    password: Optional[str] = None,
    bbox: list[float] = (-180, -90, 180, -60),
    limit: int = 500,
    max_products: Optional[int] = None,
) -> gpd.GeoDataFrame:
    """End-to-end: authenticate, search Bhoonidhi, return the frame inventory.

    bbox defaults to south of -60 deg latitude (Antarctica-covering) but any
    box works -- pass e.g. (-180, -90, 180, 90) for a global search.
    """
    from .bhoonidhi import credentials as _credentials

    if user is None or password is None:
        user, password = _credentials()

    with requests.Session() as session:
        print(f"Searching {GUNW_COLLECTION} over bbox={list(bbox)}...")
        features = fetch_all_gunw(
            session, user, password, bbox=list(bbox), limit=limit, max_products=max_products
        )

    print(f"\nCatalog returned {len(features)} raw STAC items.")
    gdf = features_to_gdf(features)
    if gdf.empty:
        fail("No GUNW footprints were returned. Check API access, credentials, and the search bbox.")
    return gdf


def save_inventory(gdf: gpd.GeoDataFrame, output: Path | str) -> Path:
    """Write the inventory to a GeoPackage plus a plain CSV sidecar."""
    output = Path(output).expanduser().resolve()
    if output.exists():
        output.unlink()
    gdf.to_file(output, layer="gunw_frames", driver="GPKG")

    csv_path = output.with_suffix(".csv")
    gdf.drop(columns="geometry").to_csv(csv_path, index=False)
    print(f"GeoPackage: {output}")
    print(f"CSV:        {csv_path}")
    return output


def build_map(gdf: gpd.GeoDataFrame):
    """Interactive ascending/descending map of the frame inventory.

    Returns a folium.Map (geopandas' .explore() backend) with two layers,
    "Ascending" (green) and "Descending" (blue), toggleable via the layer
    control, each popup showing frame_id/track/frame.
    """
    tooltip_cols = [c for c in ["frame_id", "track", "direction", "frame", "product_id"] if c in gdf.columns]

    asc = gdf[gdf["direction"] == "A"]
    desc = gdf[gdf["direction"] == "D"]

    m = None
    if not asc.empty:
        m = asc.explore(
            color="#2ca02c",
            name="Ascending",
            tooltip=tooltip_cols,
            style_kwds={"fillOpacity": 0.25, "weight": 1.5},
        )
    if not desc.empty:
        m = desc.explore(
            m=m,
            color="#1f77b4",
            name="Descending",
            tooltip=tooltip_cols,
            style_kwds={"fillOpacity": 0.25, "weight": 1.5},
        )
    if m is None:
        fail("inventory is empty; nothing to plot")

    import folium

    folium.LayerControl(collapsed=False).add_to(m)
    return m


def print_summary(gdf: gpd.GeoDataFrame) -> None:
    asc = gdf[gdf["direction"] == "A"]
    desc = gdf[gdf["direction"] == "D"]
    unknown = gdf[~gdf["direction"].isin(["A", "D"])]
    print("\n" + "=" * 72)
    print("NISAR S-BAND GUNW FRAME INVENTORY")
    print("=" * 72)
    print(f"Total GUNW products: {len(gdf)}")
    print(f"Ascending:            {len(asc)}")
    print(f"Descending:           {len(desc)}")
    print(f"Unknown direction:    {len(unknown)}")
    if not gdf.empty:
        print(f"Unique frame_ids:     {gdf['frame_id'].nunique()}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-lat", type=float, default=-60.0, help="Northern latitude limit (search south of this); default -60.")
    parser.add_argument("--max-lat", type=float, default=-90.0, help="Southern latitude limit; default -90 (South Pole).")
    parser.add_argument("-o", "--output", default="sband_frame_inventory.gpkg", help="Output GeoPackage path.")
    parser.add_argument("--limit", type=int, default=500, help="Page size for the Bhoonidhi search; max 500.")
    parser.add_argument("--max-products", type=int, default=None, help="Stop after this many products (for a quick test run).")
    args = parser.parse_args()

    gdf = build_inventory(
        bbox=(-180, args.max_lat, 180, args.min_lat),
        limit=args.limit,
        max_products=args.max_products,
    )
    save_inventory(gdf, args.output)
    print_summary(gdf)


if __name__ == "__main__":
    main()
