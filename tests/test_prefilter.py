"""Offline checks for the city pre-filter and the location reject counters.

Run directly, no test framework needed and no dependencies beyond the app's:

    venv/bin/python3 tests/test_prefilter.py

Deliberately hits NO network and spends NO SociaVault credits: a stub client
stands in for the API and records exactly which calls it receives, which is the
whole point - most of these assertions are about calls NOT being made.

The final case replays the get_item() envelope bug of 2026-08-13, which
silently dropped every candidate on every run for two days. It's here so that
failure mode can never be silent again.
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fb_marketplace_monitor.config import WatchConfig
from fb_marketplace_monitor.monitor import run_watch
from fb_marketplace_monitor.sociavault_client import SociaVaultError
from fb_marketplace_monitor.store import Store
from fb_marketplace_monitor.xlsx_writer import XlsxWriter

JAX = (30.3285, -81.657)

CITY_COORDS = {
    "Jacksonville, Florida": JAX,
    "Orlando, Florida": (28.5383, -81.3792),        # ~140 mi - pre-filtered out
    "Neptune Beach, Florida": (30.3141, -81.3931),  # ~15 mi - in range
}

# Pins the item-detail endpoint returns, keyed by listing id.
ITEM_PINS = {
    "in_range": (30.3141, -81.3931),   # Neptune Beach - keep
    "far_pin": (25.7617, -80.1918),    # Miami pin even though city says Jacksonville
    "no_state": (30.30, -81.60),       # ambiguous city, must fail open then pass
    "no_coords": None,                 # ship-only listing, no pickup point
    "fetch_fail": "RAISE",             # transport failure, not a fact about the listing
}

_results: list[bool] = []


def check(label: str, condition: bool) -> None:
    _results.append(condition)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")


class StubClient:
    def __init__(self, listings):
        self.listings = listings
        self.get_item_calls: list[str] = []
        self.resolve_calls: list[str] = []

    def search_all_pages(self, **kwargs):
        return list(self.listings)

    def resolve_location(self, query):
        self.resolve_calls.append(query)
        if query in CITY_COORDS:
            lat, lng = CITY_COORDS[query]
            return {"latitude": lat, "longitude": lng}
        raise KeyError(f"no geocode for {query}")

    def get_item(self, item_id):
        self.get_item_calls.append(item_id)
        pin = ITEM_PINS.get(item_id)
        if pin == "RAISE":
            raise SociaVaultError("simulated transport failure")
        location = {} if pin is None else {"latitude": pin[0], "longitude": pin[1]}
        return {"description": "a lovely Rolex watch", "location": location, "attributes": []}


class EnvelopeBugClient(StubClient):
    """Reproduces the 2026-08-13 bug: get_item() returns the raw {success,
    data:{...}} envelope, so every field the caller wants sits one level too
    deep and location reads as absent."""

    def get_item(self, item_id):
        self.get_item_calls.append(item_id)
        return {"success": True, "data": {"location": {"latitude": 30.3, "longitude": -81.6}}}


def make_listing(listing_id, title, city=None, state=None, display=None, price=1000):
    location = {}
    if city:
        location["city"] = city
    if state:
        location["state"] = state
    if display:
        location["display_name"] = display
    return {
        "id": listing_id,
        "title": title,
        "location": location,
        "price": {"amount": price, "formatted_amount": f"${price}"},
    }


LISTINGS = [
    make_listing("in_range", "Rolex Submariner", "Neptune Beach", "Florida"),
    make_listing("orlando1", "Rolex Datejust", "Orlando", "Florida"),
    make_listing("orlando2", "Rolex Explorer", "Orlando", "Florida"),
    make_listing("far_pin", "Rolex GMT", "Jacksonville", "Florida"),
    make_listing("no_state", "Rolex Daytona", display="Saint Johns"),
    make_listing("no_coords", "Rolex Oyster", "Jacksonville", "Florida"),
    make_listing("fetch_fail", "Rolex Air-King", "Jacksonville", "Florida"),
]

WATCH = WatchConfig(
    name="testwatch",
    keywords=["Rolex"],
    city="Jacksonville, FL",
    radius_miles=30,
    sheet_tab="t",
    output_type="xlsx",
    output_file="unused-because-dry-run.xlsx",
    search_terms=["Rolex"],
    check_description=True,
    max_pages=1,
)


def test_prefilter(workdir: Path) -> None:
    store = Store(str(workdir / "prefilter.db"))
    store.cache_location(WATCH.city, *JAX)  # watch origin, already cached
    client = StubClient(LISTINGS)

    new_rows = run_watch(client, None, XlsxWriter(), store, WATCH, dry_run=True)

    print("\nCity pre-filter:")
    check(
        "out-of-range city costs no item-detail credit",
        "orlando1" not in client.get_item_calls and "orlando2" not in client.get_item_calls,
    )
    check(
        "two listings in one city cost only one geocode (cache works)",
        client.resolve_calls.count("Orlando, Florida") == 1,
    )
    # Two survivors: Neptune Beach, plus the ambiguous "Saint Johns" that fails
    # open past the coarse filter and then passes the precise pin check.
    check("both genuinely in-range listings survive", new_rows == 2)
    check(
        "in-range city but out-of-range seller pin is still rejected",
        "far_pin" in client.get_item_calls,
    )

    print("\nAmbiguous location handling:")
    check(
        "city with no state is never geocoded (could match the wrong state)",
        "Saint Johns" not in client.resolve_calls,
    )
    check(
        "...and fails OPEN to the pin check rather than being dropped blind",
        "no_state" in client.get_item_calls,
    )

    print("\nOpportunistic cache fill:")
    check(
        "a city seen in item detail is cached without a geocode call",
        store.get_cached_location("Jacksonville, Florida") is not None,
    )


def test_envelope_bug_is_loud(workdir: Path) -> None:
    """The regression that matters: a systemic failure must not look like a
    quiet market. Before this alarm existed, the only log line was
    "no new listings" - for two days."""
    store = Store(str(workdir / "envelope.db"))
    for city, coords in [(WATCH.city, JAX), *CITY_COORDS.items()]:
        store.cache_location(city, *coords)

    warnings: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                warnings.append(record.getMessage())

    handler = Capture()
    monitor_log = logging.getLogger("fb_marketplace_monitor.monitor")
    # main() quiets the root logger to keep this report readable, so raise this
    # logger explicitly - otherwise the warning is filtered before any handler
    # sees it and the assertion below passes/fails for the wrong reason.
    previous_level = monitor_log.level
    monitor_log.setLevel(logging.WARNING)
    monitor_log.addHandler(handler)
    try:
        listings = [l for l in LISTINGS if l["id"] not in ("orlando1", "orlando2")]
        new_rows = run_watch(
            EnvelopeBugClient(listings), None, XlsxWriter(), store, WATCH, dry_run=True
        )
    finally:
        monitor_log.removeHandler(handler)
        monitor_log.setLevel(previous_level)

    print("\nEnvelope-bug regression:")
    check("the bug still drops everything (as it would have)", new_rows == 0)
    check(
        "but now a WARNING fires naming the stage that ate them",
        any("single stage" in w and "no_coordinates" in w for w in warnings),
    )


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)  # keep app logs out of the report
    workdir = Path(tempfile.mkdtemp(prefix="fbmm-tests-"))
    try:
        test_prefilter(workdir)
        test_envelope_bug_is_loud(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    passed, total = sum(_results), len(_results)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
