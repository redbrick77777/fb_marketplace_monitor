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
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
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
        return {
            "description": "a lovely Rolex watch",
            "location": location,
            "attributes": [],
            "creation_time": "2026-08-21T00:11:08.000Z",
        }


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
    # These two stand for cities a previous run already learned from real
    # seller pins - the pre-filter never geocodes a city itself, so a city is
    # only usable here if an item fetch taught us its coordinates earlier.
    store.cache_location("Orlando, Florida", *CITY_COORDS["Orlando, Florida"])
    store.cache_location("Jacksonville, Florida", *JAX)
    client = StubClient(LISTINGS)

    new_rows = run_watch(client, None, XlsxWriter(), store, WATCH, dry_run=True)

    print("\nCity pre-filter:")
    check(
        "out-of-range city costs no item-detail credit",
        "orlando1" not in client.get_item_calls and "orlando2" not in client.get_item_calls,
    )
    check(
        "the pre-filter NEVER geocodes - a wrong answer would reject a real listing",
        client.resolve_calls == [],
    )
    # Two survivors: Neptune Beach, plus the ambiguous "Saint Johns" that fails
    # open past the coarse filter and then passes the precise pin check.
    check("both genuinely in-range listings survive", new_rows == 2)
    check(
        "in-range city but out-of-range seller pin is still rejected",
        "far_pin" in client.get_item_calls,
    )

    print("\nAmbiguous / unknown location handling:")
    check(
        "a city with no state is not usable and fails OPEN to the pin check",
        "no_state" in client.get_item_calls,
    )
    check(
        "an unlearned city also fails OPEN rather than being dropped blind",
        "in_range" in client.get_item_calls,
    )

    print("\nCache fill from real pins (the only source):")
    check(
        "a city we had never seen is learned from the item's real pin",
        store.get_cached_location("Neptune Beach, Florida") is not None,
    )
    check(
        "...and learned from the pin itself, not from a geocode lookup",
        store.get_cached_location("Neptune Beach, Florida") == ITEM_PINS["in_range"],
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


def test_offsite_detection() -> None:
    """Shapes taken from real 2026-08-16 search responses."""
    from fb_marketplace_monitor.monitor import is_offsite_listing

    ebay = {"delivery_types": {"0": "SHIPPING_OFFSITE"},
            "strikethrough_price": {"formatted_amount": "$2,800"}}
    in_person = {"delivery_types": {"0": "IN_PERSON"}}
    onsite = {"delivery_types": {"0": "IN_PERSON", "1": "SHIPPING_ONSITE"}}
    meetup = {"delivery_types": {"0": "IN_PERSON", "1": "PUBLIC_MEETUP"}}
    door = {"delivery_types": {"0": "DOOR_DROPOFF", "1": "DOOR_PICKUP",
                               "2": "IN_PERSON", "3": "PUBLIC_MEETUP"}}
    # A perfectly ordinary listing whose seller just cut the price. 17 of 65
    # real listings looked like this - it must NOT be treated as offsite.
    price_cut = {"delivery_types": {"0": "IN_PERSON"},
                 "strikethrough_price": {"formatted_amount": "$3,000"}}
    empty = {"delivery_types": {}}

    print("\nOffsite (eBay/partner) detection:")
    check("SHIPPING_OFFSITE is detected", is_offsite_listing(ebay))
    check("plain IN_PERSON is not", not is_offsite_listing(in_person))
    check("IN_PERSON + SHIPPING_ONSITE is not (on-site shipping is fine)",
          not is_offsite_listing(onsite))
    check("IN_PERSON + PUBLIC_MEETUP is not", not is_offsite_listing(meetup))
    check("DOOR_* combination is not", not is_offsite_listing(door))
    check("a price-cut listing is NOT offsite (strikethrough is not a signal)",
          not is_offsite_listing(price_cut))
    check("missing/empty delivery_types is not offsite (fail open)",
          not is_offsite_listing(empty) and not is_offsite_listing({}))


def test_offsite_costs_no_item_credit(workdir: Path) -> None:
    """The whole point: reject before paying for item detail."""
    store = Store(str(workdir / "offsite.db"))
    store.cache_location(WATCH.city, *JAX)

    listings = [
        make_listing("in_range", "Rolex Submariner", "Neptune Beach", "Florida"),
        dict(make_listing("ebay1", "Rolex Sea-Dweller", "Citrus Heights", "California"),
             delivery_types={"0": "SHIPPING_OFFSITE"}),
        dict(make_listing("ebay2", "Rolex Datejust", "Jacksonville", "Florida"),
             delivery_types={"0": "SHIPPING_OFFSITE"}),
    ]
    client = StubClient(listings)
    run_watch(client, None, XlsxWriter(), store, WATCH, dry_run=True)

    print("\nOffsite listings cost nothing:")
    check("neither offsite listing triggered an item fetch",
          "ebay1" not in client.get_item_calls and "ebay2" not in client.get_item_calls)
    check("a local offsite listing is still rejected (not just far ones)",
          "ebay2" not in client.get_item_calls)
    check("the genuine listing was still fetched", "in_range" in client.get_item_calls)


def test_posted_date_extraction() -> None:
    """Shapes taken from real 2026-08-25 item-detail responses."""
    from fb_marketplace_monitor.monitor import _extract_posted_date

    print("\nPosted-At extraction:")
    # Midday UTC on purpose: local and UTC agree on the date here, so this
    # assertion is about the format alone. The zone is the next check's job.
    check(
        "a real creation_time becomes a bare ISO date",
        _extract_posted_date({"creation_time": "2026-08-04T15:32:28.000Z"}) == "2026-08-04",
    )
    # The whole reason this is local and not UTC: the sibling tool's "Closed"
    # column is stamped with a local datetime.now(), and "on market" subtracts
    # the two. A UTC date here would read a day high all evening.
    local_offset = datetime.now().astimezone().utcoffset()
    expected = "2026-08-20" if local_offset < timedelta(0) else "2026-08-21"
    check(
        "a just-after-midnight UTC stamp resolves to the LOCAL date",
        _extract_posted_date({"creation_time": "2026-08-21T00:11:08.000Z"}) == expected,
    )
    check(
        "a null creation_time (offsite listings) yields empty, not a guess",
        _extract_posted_date({"creation_time": None}) == "",
    )
    check("a missing field yields empty", _extract_posted_date({}) == "")
    check("a failed item fetch yields empty", _extract_posted_date(None) == "")
    check(
        "the human 'Listed 3 weeks ago' text is never used - it can't be subtracted",
        _extract_posted_date(
            {"creation_time": None, "listing_date_text": "Listed 3 weeks ago"}
        ) == "",
    )
    check(
        "garbage doesn't crash the run, it just leaves the cell empty",
        _extract_posted_date({"creation_time": "not a date"}) == "",
    )


def test_posted_after_filter(workdir: Path) -> None:
    """The age cutoff: stale listings out, and out for free.

    Facebook keeps serving months-old listings in search results - 8 of 17 on a
    live "macbook" probe on 2026-09-26 were from May-July. They look identical
    to new ones once they reach the sheet.
    """
    cutoff = date(2026, 9, 1)
    watch = replace(WATCH, name="agewatch", posted_after=cutoff)
    store = Store(str(workdir / "posted-after.db"))
    store.cache_location(WATCH.city, *JAX)

    # 22:00 LOCAL on the cutoff date. East of UTC that is the next UTC day, and
    # a cutoff built at UTC midnight would drop this listing even though the
    # "Posted At" column - which is rendered local - shows the cutoff date.
    cutoff_evening = datetime.combine(cutoff, time(22, 0)).astimezone(timezone.utc)

    listings = [
        dict(make_listing("in_range", "Rolex Submariner", "Neptune Beach", "Florida"),
             creation_time="2026-09-20T12:00:00.000Z"),
        dict(make_listing("no_state", "Rolex Daytona", display="Saint Johns"),
             creation_time=cutoff_evening.strftime("%Y-%m-%dT%H:%M:%S.000Z")),
        dict(make_listing("old_june", "Rolex Datejust", "Neptune Beach", "Florida"),
             creation_time="2026-06-18T19:09:54.000Z"),
        dict(make_listing("old_july", "Rolex Explorer", "Neptune Beach", "Florida"),
             creation_time="2026-07-25T17:45:25.000Z"),
        dict(make_listing("undated", "Rolex GMT", "Neptune Beach", "Florida"),
             creation_time=None),
    ]
    client = StubClient(listings)
    new_rows = run_watch(client, None, XlsxWriter(), store, watch, dry_run=True)

    print("\nposted_after age cutoff:")
    check("listings posted before the cutoff are dropped", new_rows == 2)
    check(
        "and dropped for FREE - no item-detail credit spent on a stale listing",
        "old_june" not in client.get_item_calls and "old_july" not in client.get_item_calls,
    )
    check(
        "a stale listing is NOT marked seen, so widening the window brings it back",
        not store.has_seen(watch.name, "old_june"),
    )
    check(
        "a listing with no creation_time fails OPEN rather than being binned",
        "undated" in client.get_item_calls,
    )
    if datetime.now().astimezone().utcoffset() < timedelta(0):
        # Only meaningful west of UTC (the droplet is EDT); east of it the
        # stamp lands on the same UTC day and asserts nothing.
        check(
            "(setup) the local-evening stamp really is on the NEXT UTC day",
            cutoff_evening.date() > cutoff,
        )
    check(
        "the cutoff date is inclusive and read in LOCAL time, matching Posted At",
        "no_state" in client.get_item_calls,
    )

    # Same listings, no cutoff configured: the filter must be inert when unset.
    store2 = Store(str(workdir / "posted-after-off.db"))
    store2.cache_location(WATCH.city, *JAX)
    client2 = StubClient(listings)
    run_watch(client2, None, XlsxWriter(), store2, WATCH, dry_run=True)
    check(
        "without posted_after nothing is age-filtered at all",
        "old_june" in client2.get_item_calls and "old_july" in client2.get_item_calls,
    )


def test_max_age_days_filter(workdir: Path) -> None:
    """The rolling version of the same cutoff, measured in elapsed 24h periods."""
    now = datetime.now(timezone.utc)

    def stamp(days_ago: float) -> str:
        return (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    watch = replace(WATCH, name="rollingwatch", max_age_days=30)
    store = Store(str(workdir / "max-age.db"))
    store.cache_location(WATCH.city, *JAX)

    listings = [
        dict(make_listing("in_range", "Rolex Submariner", "Neptune Beach", "Florida"),
             creation_time=stamp(2)),
        dict(make_listing("no_state", "Rolex Daytona", display="Saint Johns"),
             creation_time=stamp(29.5)),
        dict(make_listing("old_31", "Rolex Datejust", "Neptune Beach", "Florida"),
             creation_time=stamp(31)),
        dict(make_listing("old_90", "Rolex Explorer", "Neptune Beach", "Florida"),
             creation_time=stamp(90)),
    ]
    client = StubClient(listings)
    new_rows = run_watch(client, None, XlsxWriter(), store, watch, dry_run=True)

    print("\nmax_age_days rolling window:")
    check("listings inside the window survive, ones outside don't", new_rows == 2)
    check(
        "just inside the window (29.5 of 30 days) still counts",
        "no_state" in client.get_item_calls,
    )
    check(
        "and the stale ones cost no item-detail credit",
        "old_31" not in client.get_item_calls and "old_90" not in client.get_item_calls,
    )
    check(
        "a stale listing is NOT marked seen here either",
        not store.has_seen(watch.name, "old_90"),
    )


def test_age_cutoff_composition() -> None:
    """posted_after and max_age_days can both be set; the stricter one wins."""
    from fb_marketplace_monitor.monitor import _age_cutoff

    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    local_midnight = lambda d: datetime.combine(d, time.min).astimezone(timezone.utc)

    print("\nAge cutoff composition:")
    check("neither set means no cutoff at all", _age_cutoff(WATCH, now) is None)
    check(
        "posted_after alone resolves to LOCAL midnight of that date",
        _age_cutoff(replace(WATCH, posted_after=date(2026, 9, 20)), now)
        == local_midnight(date(2026, 9, 20)),
    )
    check(
        "max_age_days alone is elapsed time back from now, not a calendar date",
        _age_cutoff(replace(WATCH, max_age_days=7), now) == now - timedelta(days=7),
    )
    check(
        "both set: a tight rolling window beats an ancient fixed date",
        _age_cutoff(replace(WATCH, posted_after=date(2026, 1, 1), max_age_days=7), now)
        == now - timedelta(days=7),
    )
    check(
        "both set: a recent fixed date beats a loose rolling window",
        _age_cutoff(replace(WATCH, posted_after=date(2026, 9, 20), max_age_days=365), now)
        == local_midnight(date(2026, 9, 20)),
    )


def test_age_limit_config(workdir: Path) -> None:
    """A mistyped cutoff must fail loudly. Falling back to "no cutoff" would
    leave a healthy-looking run quietly doing nothing it was asked to do."""
    from fb_marketplace_monitor.config import load_config

    def load(line: str):
        config_file = workdir / "age-limit.yaml"
        config_file.write_text(
            "sociavault_api_key: x\n"
            "watches:\n"
            "  - name: w\n"
            "    keywords: [rolex]\n"
            "    city: Jacksonville, FL\n"
            "    output_type: xlsx\n"
            "    output_file: unused.xlsx\n"
            f"{line}"
        )
        return load_config(str(config_file)).watches[0]

    def rejects(line: str) -> bool:
        try:
            load(line)
            return False
        except ValueError:
            return True

    print("\nposted_after config parsing:")
    check("a bare YAML date is read as a date",
          load("    posted_after: 2026-09-01\n").posted_after == date(2026, 9, 1))
    check("a quoted string works too",
          load('    posted_after: "2026-09-01"\n').posted_after == date(2026, 9, 1))
    check("a timestamp keeps only its date part",
          load("    posted_after: 2026-09-01 08:30:00\n").posted_after == date(2026, 9, 1))
    check("omitting the key means no age limit", load("").posted_after is None)

    for bad in ("last tuesday", "09/01/2026", "20260901"):
        check(f"posted_after {bad!r} is rejected, not silently ignored",
              rejects(f"    posted_after: {bad}\n"))

    print("\nmax_age_days config parsing:")
    check("a whole number of days is accepted",
          load("    max_age_days: 30\n").max_age_days == 30)
    check("a quoted number works too",
          load('    max_age_days: "30"\n').max_age_days == 30)
    check("omitting the key means no age limit", load("").max_age_days is None)
    check("both keys can be set together",
          load("    posted_after: 2026-09-01\n    max_age_days: 30\n").max_age_days == 30)

    for bad in ("0", "-5", "2.5", "true", "thirty"):
        check(f"max_age_days {bad!r} is rejected, not silently ignored",
              rejects(f"    max_age_days: {bad}\n"))


def test_condition_extraction() -> None:
    """Shapes taken from a real 2026-09-26 item-detail response.

    The first version of _extract_condition read `attributes` as a list of
    dicts and matched on `label == "Condition"`. Both are wrong: it's an
    object keyed "0", "1", ... and the name lives in `attribute_name`, while
    `label` is the human form of the VALUE. Iterating the object yields the
    bare string "0", so `.get()` raised AttributeError and took the whole run
    down with it - for any watch that turned condition_filter on.
    """
    from fb_marketplace_monitor.monitor import _extract_condition

    live = {"attributes": {"0": {"attribute_name": "Condition",
                                 "value": "used_good",
                                 "label": "Used - good"}}}
    among_others = {"attributes": {
        "0": {"attribute_name": "Brand", "value": "Apple", "label": "Apple"},
        "1": {"attribute_name": "Condition", "value": "new", "label": "New"},
    }}

    print("\nCondition extraction:")
    check("the real index-keyed shape is read", _extract_condition(live) == "used_good")
    check("condition is found alongside other attributes",
          _extract_condition(among_others) == "new")
    check("a plain list is still tolerated",
          _extract_condition({"attributes": [{"attribute_name": "Condition",
                                              "value": "used_like_new"}]}) == "used_like_new")
    check("the label is the fallback when value is absent",
          _extract_condition({"attributes": {"0": {"attribute_name": "Condition",
                                                   "label": "Used - fair"}}}) == "Used - fair")
    check("no attributes means no opinion, not a crash",
          _extract_condition({"attributes": {}}) is None
          and _extract_condition({}) is None)
    check("a failed item fetch yields None", _extract_condition(None) is None)
    check("an attribute that isn't a dict is skipped rather than fatal",
          _extract_condition({"attributes": {"0": "Condition"}}) is None)


def test_condition_filter_end_to_end(workdir: Path) -> None:
    """The filter has to survive a whole run against the real payload shape."""

    class ConditionClient(StubClient):
        def __init__(self, listings, condition):
            super().__init__(listings)
            self.condition = condition

        def get_item(self, item_id):
            item = super().get_item(item_id)
            item["attributes"] = {"0": {"attribute_name": "Condition",
                                        "value": self.condition,
                                        "label": self.condition.replace("_", " ")}}
            return item

    watch = replace(WATCH, name="condwatch", condition_filter="used")
    listings = [make_listing("in_range", "Rolex Submariner", "Neptune Beach", "Florida")]

    def run(condition: str, db: str):
        store = Store(str(workdir / db))
        store.cache_location(WATCH.city, *JAX)
        try:
            return run_watch(
                ConditionClient(listings, condition), None, XlsxWriter(), store, watch,
                dry_run=True,
            )
        except Exception as exc:  # the old code raised AttributeError here
            return f"RAISED {type(exc).__name__}"

    print("\ncondition_filter end to end:")
    kept = run("used_good", "cond-used.db")
    dropped = run("new", "cond-new.db")
    check("a run with condition_filter on no longer crashes", kept == 1)
    check("a non-matching condition is filtered out", dropped == 0)


def test_row_layout(workdir: Path) -> None:
    """Column A is the found-date and the listing id is gone from the row.

    The id lived in A until 2026-08-25 and nothing ever read it back, while
    Sheets rounded 27% of the values into corruption. This asserts it stays
    gone - and that the id is still recoverable from the link, which is the
    only place it now survives."""
    store = Store(str(workdir / "layout.db"))
    store.cache_location(WATCH.city, *JAX)

    captured: list[list] = []

    class CapturingWriter:
        def append_rows(self, file_path, tab_name, rows):
            captured.extend(rows)

    client = StubClient([make_listing("in_range", "Rolex Submariner", "Neptune Beach", "Florida")])
    run_watch(client, None, CapturingWriter(), store, WATCH, dry_run=False)

    print("\nRow layout:")
    check("a row was written", len(captured) == 1)
    row = captured[0] if captured else [""] * 7
    check("still exactly 7 columns (A-G), so nothing downstream shifts", len(row) == 7)
    check("A is the found-date, not the listing id", row[0].endswith(" UTC"))
    check("the raw listing id appears in no cell of its own", "in_range" not in row)
    check("...but is still recoverable from the link in E", row[4].endswith("/in_range/"))
    check("F is the posted date as a bare date", len(row[5]) == 10 and row[5].count("-") == 2)
    check("B/C/D still hold title, price and distance", row[1] == "Rolex Submariner"
          and row[2] == "$1000" and row[3] == "15.8")
    check("G still holds the watch name", row[6] == "testwatch")


class CapturingNotifier:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, list[dict]]] = []
        self.fail = fail

    def send(self, watch_name, listings):
        self.calls.append((watch_name, listings))
        if self.fail:
            from fb_marketplace_monitor.notify import NotifyError

            raise NotifyError("simulated Telegram outage")
        return 1


def _notify_watch(**overrides) -> WatchConfig:
    return replace(WATCH, name="notifywatch", notify=True, **overrides)


def test_notification_is_opt_in(workdir: Path) -> None:
    listings = [make_listing("in_range", "Rolex Submariner", "Neptune Beach", "Florida")]

    print("\nNotifications are opt-in:")
    store = Store(str(workdir / "notify-off.db"))
    store.cache_location(WATCH.city, *JAX)
    off = CapturingNotifier()
    run_watch(StubClient(listings), None, XlsxWriter(), store, WATCH, dry_run=True, notifier=off)
    check("a watch without notify: true is never notified", off.calls == [])

    store = Store(str(workdir / "notify-dry.db"))
    store.cache_location(WATCH.city, *JAX)
    dry = CapturingNotifier()
    run_watch(
        StubClient(listings), None, XlsxWriter(), store, _notify_watch(),
        dry_run=True, notifier=dry,
    )
    check("--dry-run sends nothing, even with notify: true", dry.calls == [])


def test_notification_content(workdir: Path) -> None:
    store = Store(str(workdir / "notify-on.db"))
    store.cache_location(WATCH.city, *JAX)
    notifier = CapturingNotifier()

    class NullWriter:
        def append_rows(self, *args):
            pass

    listings = [
        make_listing("in_range", "Rolex Submariner", "Neptune Beach", "Florida"),
        make_listing("no_state", "Rolex Daytona", display="Saint Johns"),
    ]
    run_watch(
        StubClient(listings), None, NullWriter(), store, _notify_watch(),
        dry_run=False, notifier=notifier,
    )

    print("\nNotification content:")
    check("exactly one notification per run, not one per listing", len(notifier.calls) == 1)
    name, sent = notifier.calls[0] if notifier.calls else ("", [])
    check("it carries every new listing", len(sent) == 2)
    check("named by watch", name == "notifywatch")
    first = sent[0] if sent else {}
    check(
        "each listing carries what you need to act: title, price, distance, link, posted",
        first.get("title") == "Rolex Submariner"
        and first.get("price") == "$1000"
        and first.get("distance") == "15.8"
        and first.get("link", "").endswith("/in_range/")
        and len(first.get("posted", "")) == 10,
    )


def test_notification_failure_costs_only_the_alert(workdir: Path) -> None:
    """The invariant that matters. By the time the notifier runs, the rows are
    written and the ids are marked seen - and "seen" is a one-way door. If a
    Telegram outage propagated, the run would abort after its side effects had
    landed, and run_all would report the whole run as failed."""
    store = Store(str(workdir / "notify-fail.db"))
    store.cache_location(WATCH.city, *JAX)
    written: list[list] = []

    class CapturingWriter:
        def append_rows(self, file_path, tab_name, rows):
            written.extend(rows)

    watch = _notify_watch()
    listings = [make_listing("in_range", "Rolex Submariner", "Neptune Beach", "Florida")]

    print("\nA failed notification costs only the alert:")
    try:
        count = run_watch(
            StubClient(listings), None, CapturingWriter(), store, watch,
            dry_run=False, notifier=CapturingNotifier(fail=True),
        )
        raised = False
    except Exception:
        count, raised = -1, True

    check("the run does not raise", not raised)
    check("the listing is still counted as added", count == 1)
    check("the row still reached the sheet", len(written) == 1)
    check(
        "and it stays marked seen - no duplicate on the next run",
        store.has_seen(watch.name, "in_range"),
    )


def test_notify_without_credentials_still_runs(workdir: Path) -> None:
    """Notifications are an extra, never a gate on the real job.

    An earlier version raised here, which meant one unconfigured side channel
    took down every watch in the config - including the ones that never asked
    to be notified. The signal has to be a loud log line, not a dead run."""
    from fb_marketplace_monitor.config import load_config

    config_file = workdir / "no-creds.yaml"
    config_file.write_text(
        "sociavault_api_key: x\n"
        "watches:\n"
        "  - name: alerting\n"
        "    keywords: [rolex]\n"
        "    city: Jacksonville, FL\n"
        "    output_type: xlsx\n"
        "    output_file: unused.xlsx\n"
        "    notify: true\n"
        "  - name: quiet\n"
        "    keywords: [tires]\n"
        "    city: Jacksonville, FL\n"
        "    output_type: xlsx\n"
        "    output_file: unused.xlsx\n"
    )

    errors: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                errors.append(record.getMessage())

    handler = Capture()
    config_log = logging.getLogger("fb_marketplace_monitor.config")
    previous = config_log.level
    config_log.setLevel(logging.ERROR)
    config_log.addHandler(handler)
    try:
        config = load_config(str(config_file))
        loaded, failure = True, ""
    except Exception as exc:
        config, loaded, failure = None, False, str(exc)
    finally:
        config_log.removeHandler(handler)
        config_log.setLevel(previous)

    print("\nnotify: true without credentials:")
    check(f"the config still loads ({failure})", loaded)
    check("both watches survive, including the one not asking for alerts",
          loaded and len(config.watches) == 2)
    check("no notifier is configured", loaded and config.notifications is None)
    check("but an ERROR names the watch that won't be alerting",
          any("alerting" in e for e in errors))
    check("and says the listings are still written",
          any("NO alerts" in e for e in errors))


def _capture_logs(level=logging.WARNING) -> tuple[list, logging.Handler, logging.Logger, int]:
    captured: list[tuple[int, str]] = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append((record.levelno, record.getMessage()))

    handler = Capture()
    log = logging.getLogger("fb_marketplace_monitor.monitor")
    previous = log.level
    log.setLevel(level)
    log.addHandler(handler)
    return captured, handler, log, previous


def test_out_of_credits_during_search(workdir: Path) -> None:
    """A 402 is a billing state, not a search result.

    For 8 days in Sept 2026 an unpaid bill looked exactly like a quiet market:
    exit code 0, "0 new listings", and a warning telling the user to broaden
    their search terms."""
    from fb_marketplace_monitor.monitor import RunState
    from fb_marketplace_monitor.sociavault_client import SociaVaultError

    class BrokeClient(StubClient):
        def search_all_pages(self, **kwargs):
            raise SociaVaultError("Insufficient credits", status_code=402)

    store = Store(str(workdir / "broke-search.db"))
    store.cache_location(WATCH.city, *JAX)
    state = RunState()
    captured, handler, log, previous = _capture_logs()
    try:
        count = run_watch(
            BrokeClient([]), None, XlsxWriter(), store, WATCH, dry_run=True, run_state=state
        )
    finally:
        log.removeHandler(handler)
        log.setLevel(previous)

    messages = [m for _, m in captured]
    print("\nOut of credits during search:")
    check("nothing is collected", count == 0)
    check("the run knows why", state.out_of_credits)
    check("an ERROR says OUT OF CREDITS",
          any(lvl >= logging.ERROR and "OUT OF CREDITS" in m for lvl, m in captured))
    check(
        "and the 'broaden your search terms' advice is SUPPRESSED - it's wrong here",
        not any("ZERO raw listings" in m for m in messages),
    )


def test_out_of_credits_during_item_fetch_keeps_listings_unseen(workdir: Path) -> None:
    """The one that actually cost listings.

    A failed item fetch marks the listing seen, and seen is a one-way door.
    On 2026-08-08 that door closed on 50 listings whose only sin was being
    considered after the account hit zero - they were never looked at again."""
    from fb_marketplace_monitor.monitor import RunState
    from fb_marketplace_monitor.sociavault_client import SociaVaultError

    class BrokeItemClient(StubClient):
        def get_item(self, item_id):
            self.get_item_calls.append(item_id)
            raise SociaVaultError("Insufficient credits", status_code=402)

    store = Store(str(workdir / "broke-item.db"))
    store.cache_location(WATCH.city, *JAX)
    listings = [
        make_listing("in_range", "Rolex Submariner", "Neptune Beach", "Florida"),
        make_listing("second", "Rolex Datejust", "Neptune Beach", "Florida"),
    ]
    client = BrokeItemClient(listings)
    state = RunState()
    count = run_watch(
        client, None, XlsxWriter(), store, WATCH, dry_run=False, run_state=state
    )

    print("\nOut of credits during item fetch:")
    check("nothing is written", count == 0)
    check("the run knows why", state.out_of_credits)
    check(
        "the listing it choked on is NOT marked seen",
        not store.has_seen(WATCH.name, "in_range"),
    )
    check(
        "and it stops rather than burning the rest of the queue",
        not store.has_seen(WATCH.name, "second") and len(client.get_item_calls) == 1,
    )


def test_out_of_credits_alerts_and_fails_the_run(workdir: Path) -> None:
    from fb_marketplace_monitor.monitor import _alert_out_of_credits

    sent: list[str] = []

    class AlertNotifier:
        def send_alert(self, text):
            sent.append(text)

    class BrokenNotifier:
        def send_alert(self, text):
            raise RuntimeError("Telegram down")

    print("\nOut-of-credits alerting:")
    _alert_out_of_credits(AlertNotifier(), dry_run=False)
    check("an alert goes out", len(sent) == 1)
    check("and it says nothing was lost", "Nothing has been lost" in sent[0])

    sent.clear()
    _alert_out_of_credits(AlertNotifier(), dry_run=True)
    check("--dry-run stays silent", sent == [])

    try:
        _alert_out_of_credits(BrokenNotifier(), dry_run=False)
        raised = False
    except Exception:
        raised = True
    check("a dead notifier doesn't become a second failure", not raised)


def test_message_building() -> None:
    from fb_marketplace_monitor.notify import (
        MAX_LISTINGS_PER_MESSAGE,
        build_messages,
    )

    print("\nTelegram message building:")
    check("nothing to say means nothing sent", build_messages("w", []) == [])

    # Seller titles are arbitrary text. HTML markup in a title must not be able
    # to break the message's own markup, or Telegram rejects the whole send.
    hostile = build_messages(
        "w", [{"title": "<b>R&R</b> 285/70/18 <tires>", "link": "http://x/1", "price": "$450"}]
    )[0]
    check("a title's HTML is escaped, not interpreted", "&lt;b&gt;R&amp;R&lt;/b&gt;" in hostile)
    check("the link survives as a real anchor", 'href="http://x/1"' in hostile)

    many = build_messages("w", [{"title": f"t{i}", "link": f"http://x/{i}"} for i in range(25)])
    check("a big batch splits rather than being dropped", len(many) == 3)
    check("every message stays under Telegram's 4096 limit", all(len(m) < 4096 for m in many))
    check(
        "each message says how many landed in total",
        all("25 new" in m for m in many),
    )

    huge = build_messages(
        "w", [{"title": "x" * 2000, "link": "http://x/1"} for _ in range(4)]
    )
    check(
        "long titles split by length too, not just by count",
        len(huge) > 4 // MAX_LISTINGS_PER_MESSAGE and all(len(m) < 4096 for m in huge),
    )

    missing = build_messages("w", [{"title": "t", "link": "http://x/1"}])[0]
    check("a listing with no price/distance/posted still renders", "t" in missing)


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)  # keep app logs out of the report
    workdir = Path(tempfile.mkdtemp(prefix="fbmm-tests-"))
    try:
        test_prefilter(workdir)
        test_offsite_detection()
        test_offsite_costs_no_item_credit(workdir)
        test_posted_date_extraction()
        test_posted_after_filter(workdir)
        test_max_age_days_filter(workdir)
        test_age_cutoff_composition()
        test_age_limit_config(workdir)
        test_condition_extraction()
        test_condition_filter_end_to_end(workdir)
        test_row_layout(workdir)
        test_notification_is_opt_in(workdir)
        test_notification_content(workdir)
        test_notification_failure_costs_only_the_alert(workdir)
        test_notify_without_credentials_still_runs(workdir)
        test_out_of_credits_during_search(workdir)
        test_out_of_credits_during_item_fetch_keeps_listings_unseen(workdir)
        test_out_of_credits_alerts_and_fails_the_run(workdir)
        test_message_building()
        test_envelope_bug_is_loud(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    passed, total = sum(_results), len(_results)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
