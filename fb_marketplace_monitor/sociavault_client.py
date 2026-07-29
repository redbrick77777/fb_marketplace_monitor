"""Thin client for the SociaVault Facebook Marketplace endpoints.

SociaVault is a third-party paid scraping service, NOT an official Meta API.
There is no official Facebook Marketplace API as of this writing. This client
was written against SociaVault's public docs (docs.sociavault.com) as of
mid-2026 - their own documentation has some internal inconsistencies (e.g.
whether `locations` comes back as a list or an object keyed by index), so
this client defensively handles both shapes. Re-check their docs periodically
in case field names or behavior change.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.sociavault.com/v1/scrape/facebook-marketplace"


class SociaVaultError(Exception):
    """Raised for any unrecoverable SociaVault API failure."""


class SociaVaultClient:
    def __init__(self, api_key: str, timeout: int = 15, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key})

    def _get(self, path: str, params: dict) -> dict:
        url = f"{BASE_URL}/{path}"
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "Request error (attempt %d/%d): %s", attempt, self.max_retries, exc
                )
                time.sleep(2**attempt)
                continue

            if resp.status_code == 429:
                wait = 5 * (2**attempt)
                logger.warning("Rate limited (429). Waiting %ds before retry.", wait)
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                raise SociaVaultError("Invalid SociaVault API key (401 Unauthorized).")

            if resp.status_code >= 400:
                raise SociaVaultError(
                    f"SociaVault API error {resp.status_code} for {url}: {resp.text[:300]}"
                )

            data = resp.json()

            # SociaVault has been observed embedding failures in an
            # otherwise-200 response (success: false + an error message)
            # rather than always using a 4xx/5xx status. Without this check,
            # a real failure silently looked identical to "zero listings
            # found" - which is misleading and hard to debug.
            if isinstance(data, dict) and data.get("success") is False:
                raise SociaVaultError(
                    f"SociaVault returned success=false for {url}: "
                    f"{data.get('error') or data.get('message') or data}"
                )

            return data

        raise SociaVaultError(
            f"Failed to reach SociaVault after {self.max_retries} attempts: {last_error}"
        )

    def resolve_location(self, query: str) -> dict:
        data = self._get("location-search", {"query": query})
        # Docs show two different shapes for this field across examples -
        # handle both a plain list and an index-keyed object defensively.
        locations = data.get("locations")
        if locations is None and isinstance(data.get("data"), dict):
            locations = data["data"].get("locations")
        if isinstance(locations, dict):
            locations = [locations[k] for k in sorted(locations.keys())]
        if not locations:
            raise SociaVaultError(f"No location found for '{query}'.")
        return locations[0]

    def search(
        self,
        query: str,
        latitude: float,
        longitude: float,
        radius_km: int,
        price_min: Optional[int] = None,
        price_max: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> dict:
        params = {
            "query": query,
            "lat": latitude,
            "lng": longitude,
            "radius_km": radius_km,
        }
        if price_min is not None:
            params["price_min"] = price_min
        if price_max is not None:
            params["price_max"] = price_max
        if cursor:
            params["cursor"] = cursor

        raw = self._get("search", params)

        # SociaVault double-wraps this endpoint's real payload under "data"
        # (confirmed against a live response - {"success": true, "data":
        # {"success": true, "credits_charged": ..., "listings": {...}}}),
        # the same inconsistency already handled for location-search.
        payload = raw.get("data") if isinstance(raw.get("data"), dict) else raw

        listings = payload.get("listings", [])
        if isinstance(listings, dict):
            # Listings come back keyed by string index ("0", "1", ...)
            # rather than as a JSON array - normalize to an ordered list.
            listings = [
                listings[k]
                for k in sorted(listings.keys(), key=lambda k: int(k) if k.isdigit() else k)
            ]

        return {"listings": listings, "cursor": payload.get("cursor")}

    def search_all_pages(
        self,
        query: str,
        latitude: float,
        longitude: float,
        radius_km: int,
        price_min: Optional[int] = None,
        price_max: Optional[int] = None,
        max_pages: int = 5,
    ) -> list[dict]:
        listings: list[dict] = []
        cursor: Optional[str] = None

        for _ in range(max_pages):
            data = self.search(
                query, latitude, longitude, radius_km, price_min, price_max, cursor
            )
            listings.extend(data.get("listings", []))
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(0.5)

        return listings

    def get_item(self, item_id: str) -> dict:
        return self._get("item", {"id": item_id})
