"""Shared Bhoonidhi (NRSC/ISRO) REST client for NISAR S-band products.

Bhoonidhi has no predictable filesystem/URL layout: every product, including
GUNW itself, must be found via ``/data/search`` (a STAC-like endpoint that
accepts CQL2-JSON filters) and fetched via ``/download?id=...&collection=...``.
There is no way to derive a GOFF or GCOV path from a known GUNW path here
(unlike the ASF/S3 L-band trees), so callers always search per collection.

Credentials:
    export BHOONIDHI_USER='your_user_id'
    export BHOONIDHI_PASSWORD='your_password'
or you will be prompted interactively (password entry is hidden).
"""

from __future__ import annotations

import getpass
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable

import requests

BASE = "https://bhoonidhi-api.nrsc.gov.in"
AUTH_URL = f"{BASE}/auth/token"
SEARCH_URL = f"{BASE}/data/search"
DOWNLOAD_URL = f"{BASE}/download"

# Confirmed against the live catalog (see phase_velo/scripts/bhoonidhi_*.py).
CONFIRMED_COLLECTIONS = {
    "GUNW": "NISAR_SSAR_GUNW",
    "GOFF": "NISAR_SSAR_GOFF",
    "GCOV": "NISAR_SSAR_GCOV",
}

TDF_RE = re.compile(r"^(?P<track>\d{1,3})_(?P<direction>[AD])_(?P<frame>\d{1,3})$")
PRODUCT_TDF_RE = re.compile(
    r"^NISAR_S2_PR_(?P<product>[A-Z]+)_\d{3}_"
    r"(?P<track>\d{3})_(?P<direction>[AD])_(?P<frame>\d{3})(?:_|$)"
)

# Bhoonidhi's documented limit is three search requests per second.
SEARCH_INTERVAL_S = 0.4


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def collection_for(product: str) -> str:
    """Map a product code (GUNW/GOFF/GCOV/...) to its Bhoonidhi collection name.

    GUNW/GOFF/GCOV are confirmed. Anything else follows the same
    ``NISAR_SSAR_<product>`` naming pattern but has NOT been verified against
    the live catalog -- treat it as a guess and check the results.
    """
    product = product.upper()
    if product in CONFIRMED_COLLECTIONS:
        return CONFIRMED_COLLECTIONS[product]
    guessed = f"NISAR_SSAR_{product}"
    print(
        f"  WARNING: no confirmed Bhoonidhi collection for product {product!r}; "
        f"guessing {guessed!r} (unverified pattern, may not exist)",
        file=sys.stderr,
    )
    return guessed


def credentials() -> tuple[str, str]:
    user = os.environ.get("BHOONIDHI_USER") or input("Bhoonidhi user ID: ").strip()
    password = os.environ.get("BHOONIDHI_PASSWORD") or getpass.getpass(
        "Bhoonidhi password: "
    )
    if not user or not password:
        fail("BHOONIDHI_USER and BHOONIDHI_PASSWORD are required")
    return user, password


def authenticate(session: requests.Session, user: str, password: str) -> str:
    response = session.post(
        AUTH_URL,
        json={"userId": user, "password": password, "grant_type": "password"},
        timeout=60,
    )
    if not response.ok:
        fail(f"authentication failed: HTTP {response.status_code}: {response.text[:500]}")
    token = response.json().get("access_token")
    if not token:
        fail("authentication response did not contain access_token")
    return str(token)


def request_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    attempts: int = 6,
    **kwargs,
) -> requests.Response:
    for attempt in range(attempts):
        try:
            response = session.request(method, url, headers=headers, timeout=180, **kwargs)
        except requests.RequestException:
            if attempt + 1 == attempts:
                raise
        else:
            if response.status_code not in {412, 429, 500, 502, 503, 504}:
                return response
            if attempt + 1 == attempts:
                return response
        wait = min(60, 2**attempt)
        print(f"  retrying {method} after {wait}s...")
        time.sleep(wait)
    raise RuntimeError("retry loop exited unexpectedly")


def authorized_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    user: str,
    password: str,
    **kwargs,
) -> requests.Response:
    """Make a request, reauthenticating once if the access token expired."""
    response = request_retry(session, method, url, headers=headers, **kwargs)
    if response.status_code != 401:
        return response

    print("  Bhoonidhi session expired; reauthenticating...")
    headers["Authorization"] = f"Bearer {authenticate(session, user, password)}"
    return request_retry(session, method, url, headers=headers, **kwargs)


def tdf_filter(track: int, direction: str, frame: int) -> dict:
    # Exact property names returned by the live SSAR catalog.
    terms = [
        {"op": "eq", "args": [{"property": "Track"}, track]},
        {
            "op": "eq",
            "args": [
                {"property": "Node"},
                "Ascending" if direction == "A" else "Descending",
            ],
        },
        {"op": "eq", "args": [{"property": "Frame"}, frame]},
        {"op": "eq", "args": [{"property": "Online"}, "Y"]},
    ]
    return {"op": "and", "args": terms}


def search_tdf(
    session: requests.Session,
    headers: dict[str, str],
    user: str,
    password: str,
    collection: str,
    track: int,
    direction: str,
    frame: int,
) -> list[dict]:
    """Search one collection for an exact (track, direction, frame)."""
    body = {
        "collections": [collection],
        "filter": tdf_filter(track, direction, frame),
        "filter-lang": "cql2-json",
        "limit": 500,
    }
    response = authorized_request(
        session, "POST", SEARCH_URL, headers=headers, user=user, password=password, json=body
    )
    if response.status_code == 404 and "NO RESULTS" in response.text.upper():
        return []
    if not response.ok:
        fail(
            f"search failed for {collection} {track:03d}_{direction}_{frame:03d}: "
            f"HTTP {response.status_code}: {response.text[:1000]}"
        )
    data = response.json()
    if any(link.get("rel") == "next" for link in data.get("links", [])):
        fail(
            f"more than 500 {collection} products matched "
            f"{track:03d}_{direction}_{frame:03d}; narrow the requested frames"
        )
    time.sleep(SEARCH_INTERVAL_S)

    # Do not trust catalog property normalization alone: verify the requested
    # T/D/F against the NISAR product identifier before any download.
    expected = (track, direction, frame)
    selected = []
    for feature in data.get("features", []):
        product_id = str(feature.get("id", ""))
        match = PRODUCT_TDF_RE.match(Path(product_id).name)
        if match is None:
            print(f"  WARNING: skipping unparseable product id: {product_id}")
            continue
        actual = (int(match.group("track")), match.group("direction"), int(match.group("frame")))
        if actual == expected:
            selected.append(feature)
        else:
            print(f"  WARNING: catalog returned {actual} for requested {expected}; skipping {product_id}")
    return selected


def product_filename(product_id: str) -> str:
    name = Path(product_id).name
    return name if name.lower().endswith(".h5") else f"{name}.h5"


def download_product(
    session: requests.Session,
    headers: dict[str, str],
    user: str,
    password: str,
    collection: str,
    product_id: str,
    output_dir: Path,
) -> str:
    """Download one product to output_dir. Returns 'cached' or 'downloaded'."""
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / product_filename(product_id)
    if destination.is_file() and destination.stat().st_size > 0:
        return "cached"

    partial = destination.with_suffix(destination.suffix + ".part")
    stream_attempts = 5
    for attempt in range(stream_attempts):
        response = authorized_request(
            session,
            "GET",
            DOWNLOAD_URL,
            headers=headers,
            user=user,
            password=password,
            params={"id": product_id, "collection": collection},
            stream=True,
            attempts=8,
        )
        if not response.ok:
            fail(f"download failed for {product_id}: HTTP {response.status_code}: {response.text[:1000]}")

        content_type = response.headers.get("Content-Type", "").lower()
        if "json" in content_type:
            fail(f"download returned JSON instead of HDF5 for {product_id}: {response.text[:1000]}")

        try:
            with partial.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        stream.write(chunk)
        except requests.exceptions.RequestException as exc:
            if attempt + 1 == stream_attempts:
                raise
            wait = min(60, 2**attempt)
            print(f"  stream error for {product_id} ({exc}); retrying after {wait}s...")
            time.sleep(wait)
            continue
        break

    if not partial.is_file() or partial.stat().st_size == 0:
        fail(f"empty download for {product_id}")
    partial.replace(destination)
    return "downloaded"


def parse_tdfs(values: Iterable[str]) -> list[tuple[int, str, int]]:
    """Parse ``TRACK_A|D_FRAME`` strings, e.g. '147_A_137', '066_D_129'."""
    parsed = []
    for value in values:
        match = TDF_RE.fullmatch(value.strip().upper())
        if not match:
            fail(f"invalid tdf {value!r}; expected TRACK_A|D_FRAME, e.g. 147_A_137")
        parsed.append((int(match.group("track")), match.group("direction"), int(match.group("frame"))))
    return list(dict.fromkeys(parsed))
