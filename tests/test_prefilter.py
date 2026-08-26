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
from datetime import datetime, timedelta
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
        test_row_layout(workdir)
        test_notification_is_opt_in(workdir)
        test_notification_content(workdir)
        test_notification_failure_costs_only_the_alert(workdir)
        test_notify_without_credentials_still_runs(workdir)
        test_message_building()
        test_envelope_bug_is_loud(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    passed, total = sum(_results), len(_results)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
