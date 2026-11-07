"""Core orchestration: run one watch (or all configured watches) end-to-end."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from .config import AppConfig, WatchConfig
from .geo import haversine_miles
from .matching import matches_any_keyword, matches_exclusions, to_broad_query
from .notify import TelegramNotifier
from .sheets_client import SheetsClient
from .sociavault_client import SociaVaultClient, SociaVaultError
from .store import Store
from .xlsx_writer import XlsxWriter

logger = logging.getLogger(__name__)


def listing_link(listing_id: str) -> str:
    return f"https://www.facebook.com/marketplace/item/{listing_id}/"


def resolve_watch_location(
    client: SociaVaultClient, store: Store, watch: WatchConfig
) -> tuple[float, float]:
    cached = store.get_cached_location(watch.city)
    if cached:
        return cached
    location = client.resolve_location(watch.city)
    lat, lng = location["latitude"], location["longitude"]
    store.cache_location(watch.city, lat, lng)
    logger.info("Resolved '%s' -> (%.4f, %.4f) [cached for future runs]", watch.city, lat, lng)
    return lat, lng


def is_offsite_listing(listing: dict) -> bool:
    """Is this a partner listing fulfilled off Facebook (eBay and similar)?

    Detected from the SEARCH response's `delivery_types`, so it costs nothing:
    partner listings come back as `{"0": "SHIPPING_OFFSITE"}` while genuine
    ones always include IN_PERSON (alone, or with PUBLIC_MEETUP /
    SHIPPING_ONSITE / DOOR_*). Measured over 68 live listings on 2026-08-16:
    3 were SHIPPING_OFFSITE, and it never appeared alongside IN_PERSON.

    There is no other tell. The word "ebay" appears NOWHERE in either the
    search or item-detail payload - the eBay branding is Facebook's own UI,
    so `exclude_keywords` can never match it. And `strikethrough_price` is
    not a proxy: 0 of 3 offsite listings had one, while 17 of 65 ordinary
    listings did (it just means the seller cut their price), so filtering on
    it would discard real listings and catch none of these.

    Worth doing before the item-detail fetch, because these listings return an
    empty `location` there - so today we pay a credit and then drop them as
    "no coordinates", which also makes them indistinguishable in the logs from
    genuine ship-only listings by real sellers.
    """
    delivery_types = listing.get("delivery_types") or {}
    values = delivery_types.values() if isinstance(delivery_types, dict) else delivery_types
    return any(str(v).upper() == "SHIPPING_OFFSITE" for v in values)


def _age_cutoff(watch: WatchConfig, now: datetime) -> Optional[datetime]:
    """The instant a listing must have been posted at or after, as aware UTC.

    Two ways to ask for the same thing, and a watch may set both - in which
    case the later (stricter) of the two wins, because that's how every other
    filter here composes: a listing has to satisfy all of them.

    **posted_after** is read as LOCAL midnight, never UTC midnight. Everything
    a human touches here is EDT - the date typed into the config, the "Posted
    At" column (_extract_posted_date renders it local on purpose), the log
    lines - while `creation_time` comes back from Facebook in UTC. Comparing
    the two without converting is exactly the 4-hour error CLAUDE.md warns
    about: with a UTC-midnight cutoff, anything posted after 20:00 EDT on the
    cutoff date carries the *next* day's UTC stamp and would be dropped
    despite being, to the user reading the sheet, posted on the day they asked
    for. Inclusive of the date itself: `2026-09-01` keeps a listing posted at
    09:15 EDT on September 1st.

    **max_age_days** is elapsed time, not calendar days: 30 means the last
    30x24 hours counted back from `now`. `now` is taken once per run and passed
    in rather than read here, so every listing in a run is judged against the
    same instant instead of a boundary that creeps while the loop runs.
    """
    cutoffs = []
    if watch.posted_after is not None:
        cutoffs.append(
            datetime.combine(watch.posted_after, time.min).astimezone(timezone.utc)
        )
    if watch.max_age_days is not None:
        cutoffs.append(now - timedelta(days=watch.max_age_days))
    return max(cutoffs) if cutoffs else None


def _age_cutoff_label(watch: WatchConfig) -> str:
    """How the cutoff was configured, for log lines. Names the knobs the user
    actually set, so the fix is obvious from the message alone."""
    parts = []
    if watch.posted_after is not None:
        parts.append(f"posted_after={watch.posted_after}")
    if watch.max_age_days is not None:
        parts.append(f"max_age_days={watch.max_age_days}")
    return " + ".join(parts) or "no age limit"


def _listing_posted_at(listing: dict) -> Optional[datetime]:
    """When a listing was posted, as an aware UTC datetime, from the SEARCH
    response - so it costs nothing.

    `creation_time` is present on the search payload for every listing: 17 of
    17 on a live probe on 2026-09-26, with item detail returning byte-identical
    values for the same ids. (_extract_posted_date's docstring claims the
    search response leaves this null and only item detail has it. That is no
    longer true, and the age filter would be pointless if it were - it would
    cost a credit per listing to learn an age we already have for free.)

    Returns None when the field is missing or unparseable, which callers must
    treat as "we couldn't tell", not as "old".
    """
    raw = listing.get("creation_time")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _listing_city(listing: dict) -> Optional[str]:
    """A geocodable "City, State" string from the SEARCH response - free.

    The search endpoint's per-listing `location` is city-level only, with no
    coordinates at all (confirmed by live probe 2026-08-15: 0 of 89 listings
    carried lat/lng), so this is good enough for a coarse pre-filter and never
    for the authoritative distance check.

    Returns None when the state is missing, which makes callers fail open. A
    bare "Saint Johns" (seen in that same probe) could geocode to Michigan
    instead of Florida, and a wrong geocode here would reject an in-range
    listing without ever checking its real pin.
    """
    location = listing.get("location") or {}
    city, state = location.get("city"), location.get("state")
    if city and state:
        return f"{city}, {state}"
    # Fall back to display_name only when it actually carries a state -
    # the field comes back both ways ("Jacksonville, Florida" vs "Saint Johns").
    display = (location.get("display_name") or "").strip()
    return display if "," in display else None


def lookup_city(store: Store, city: str) -> Optional[tuple[float, float]]:
    """City name -> coordinates, from the local cache ONLY. Never geocodes.

    Deliberately does not call `resolve_location()`. That endpoint returns
    confidently wrong answers often enough to be unusable as a rejection
    input: on 2026-08-15 it placed "Orange Park, FL" - a Jacksonville suburb
    ~13 mi out - at 139.8 mi, "Westminster, CA" in Ontario, Canada, and
    "Columbus, GA" in Ohio. 3 wrong out of 15.

    A wrong answer here is worse than no answer, because the pre-filter uses it
    to *reject*: Orange Park listings were dropped and marked seen (a one-way
    door) without ever having their real pin checked. Three defences that all
    fail against this:
      - failing open on errors - a confident wrong answer isn't an error;
      - a state bounding-box check - Orange Park's bad coordinates are inside
        Florida, so it passes;
      - self-healing from real pins - a wrongly-rejected city never gets an
        item fetch, so it never gets a pin to heal from.

    So the cache is populated only from real seller pins in item detail (see
    run_watch), which come from Facebook itself and have been accurate. It
    warms more slowly - a city is only learned once we've paid for one item
    fetch there - but it can never invent a reason to drop a nearby listing.
    """
    return store.get_cached_location(city)


@dataclass
class RunState:
    """What one run learns that isn't about any single watch.

    Just the one fact so far: SociaVault stopped answering because the account
    is out of money. That's a property of the account, so the first watch to
    hit it is telling every later watch something they'd otherwise each have
    to rediscover - and it has to reach run_all, which decides the exit code
    and the alert.
    """

    out_of_credits: bool = False


def run_watch(
    client: SociaVaultClient,
    sheets: Optional[SheetsClient],
    xlsx_writer: XlsxWriter,
    store: Store,
    watch: WatchConfig,
    dry_run: bool = False,
    notifier: Optional[TelegramNotifier] = None,
    run_state: Optional[RunState] = None,
) -> int:
    """Runs a single watch end-to-end. Returns the number of new rows added."""
    logger.info("Running watch '%s' (%s, %s mi radius)", watch.name, watch.city, watch.radius_miles)
    state = run_state if run_state is not None else RunState()
    latitude, longitude = resolve_watch_location(client, store, watch)

    # Query broadly per search term (Marketplace search is fuzzy about
    # punctuation, and relevance depends on having a recognizable word like
    # "tires" in there - see effective_search_terms), then do exact matching
    # ourselves against the results.
    candidates: dict[str, dict] = {}
    total_raw = 0
    for term in watch.effective_search_terms:
        broad_query = to_broad_query(term) or term
        try:
            listings = client.search_all_pages(
                query=broad_query,
                latitude=latitude,
                longitude=longitude,
                radius_km=watch.radius_km,
                price_min=watch.price_min,
                price_max=watch.price_max,
                max_pages=watch.max_pages,
            )
        except SociaVaultError as exc:
            if exc.is_out_of_credits:
                # Every remaining call this run will fail identically, so stop
                # here instead of logging the same 402 once per search term.
                state.out_of_credits = True
                logger.error(
                    "Watch '%s': SociaVault is OUT OF CREDITS (HTTP 402). This is a "
                    "billing state, not a search result - nothing below ran, and no "
                    "conclusion can be drawn about what is or isn't listed. Top up "
                    "the account; the monitor recovers by itself on the next run.",
                    watch.name,
                )
                break
            logger.error("Search failed for watch '%s', term '%s': %s", watch.name, term, exc)
            continue

        total_raw += len(listings)
        sample_titles = [l.get("title", "?") for l in listings[:5]]
        logger.debug(
            "Watch '%s': search term '%s' (sent as query '%s') returned %d raw "
            "listing(s) from SociaVault. Sample titles: %s",
            watch.name, term, broad_query, len(listings), sample_titles,
        )

        for listing in listings:
            candidates[listing["id"]] = listing

    # Deliberately NOT warned about when the account is out of credits: the
    # advice below ("broaden your search terms") is actively wrong then, and
    # following it would push real config changes in response to an unpaid
    # bill. That warning ran 88 times a day for 8 days in Sept 2026 saying
    # exactly the wrong thing.
    if total_raw == 0 and not state.out_of_credits:
        logger.warning(
            "Watch '%s': SociaVault returned ZERO raw listings across all search "
            "terms %s for '%s' within %s mi. This means the API itself found "
            "nothing for these queries/location - it's not your keyword filters "
            "or price range narrowing things down after the fact. If you can "
            "find results by hand on Facebook, try broadening effective_search_terms "
            "(e.g. add a plain category word like 'tires') or widening radius_miles. "
            "Run with --verbose to see the exact query strings sent per term.",
            watch.name, watch.effective_search_terms, watch.city, watch.radius_miles,
        )

    new_rows = []
    # The same finds again, as dicts, for the optional Telegram push. Kept
    # beside new_rows rather than parsed back out of it: the row is a
    # positional list whose layout has already changed once, and a notifier
    # reading row[5] would break silently the next time it does.
    new_listings: list[dict] = []
    # Listing IDs are only marked "seen" once the write actually succeeds
    # (see below) - not here - so a failed write or a --dry-run never loses
    # a listing; it'll simply be picked up again on the next real run.
    pending_seen_ids: list[str] = []
    already_seen_count = 0
    offsite_count = 0
    too_old_count = 0
    # Listings the age filter could not judge because the search response
    # carried no usable creation_time. Not a rejection - they're let through -
    # but counted, because if this ever stops being ~0 the filter has quietly
    # stopped filtering and nothing else would say so.
    undated_count = 0
    keyword_mismatch_count = 0
    excluded_count = 0
    price_filtered_count = 0
    # Location rejections are split across four counters on purpose. They used
    # to share one bucket, which is precisely why the get_item() envelope bug
    # stayed invisible for two days: "fetch failed" and "listing has no pickup
    # point" are wildly different problems, and lumping them together made a
    # total systemic failure look exactly like a quiet market.
    city_filtered_count = 0
    item_fetch_failed_count = 0
    no_coords_count = 0
    out_of_range_count = 0
    condition_filtered_count = 0

    # One instant for the whole run, so a rolling max_age_days window can't
    # shift between the first candidate and the last.
    age_cutoff = _age_cutoff(watch, datetime.now(timezone.utc))

    for listing_id, listing in candidates.items():
        if store.has_seen(watch.name, listing_id):
            already_seen_count += 1
            continue

        # Free, and first: an off-Facebook partner listing (eBay et al) can
        # never be inspected or picked up locally, and its item detail carries
        # no seller pin - so there is nothing further worth spending on it.
        if watch.exclude_offsite and is_offsite_listing(listing):
            offsite_count += 1
            store.mark_seen(watch.name, listing_id)
            continue

        # Also free, and also before anything is spent: the search response
        # already tells us when the seller posted this. A "macbook" search on
        # 2026-09-26 came back with 8 of 17 listings from May-July, so this is
        # the stage that keeps stale results out of the sheet - and doing it
        # here means they never cost a geocode or an item credit.
        if age_cutoff is not None:
            posted_at = _listing_posted_at(listing)
            if posted_at is None:
                # "We couldn't tell", not "it's old". Fails OPEN so an upstream
                # field change can never silently bin every listing; the count
                # above is what makes that visible instead.
                undated_count += 1
            elif posted_at < age_cutoff:
                too_old_count += 1
                # Deliberately NOT marked seen, unlike every other rejection
                # here. This check costs nothing, so re-deciding it each run is
                # free - and it keeps the door open: widen the window later and
                # these listings return. Marking them seen is a one-way door
                # that would make widening it impossible.
                continue

        title = listing.get("title", "")

        # Item detail (description, condition) costs an extra SociaVault
        # credit and isn't in the search response - fetch it at most once
        # per listing, lazily, only if this watch actually needs it for
        # something, and reuse it for whichever checks need it below.
        item_detail: Optional[dict] = None
        item_fetch_attempted = False

        def get_item_detail() -> Optional[dict]:
            nonlocal item_detail, item_fetch_attempted
            if not item_fetch_attempted:
                item_fetch_attempted = True
                item_detail = _fetch_item(client, listing_id, state)
            return item_detail

        title_matches = matches_any_keyword(title, watch.keywords, watch.keyword_any_order)

        # Cheapest rejection first: if the title doesn't match and this watch
        # never consults the description, nothing below can rescue this
        # listing - drop it without spending a geocode or an item credit.
        if not title_matches and not watch.check_description:
            keyword_mismatch_count += 1
            store.mark_seen(watch.name, listing_id)  # checked once, decided - never re-check
            continue

        # Coarse city-level radius pre-filter, deliberately placed BEFORE the
        # first item-detail fetch below - that's the whole point, since item
        # detail is the expensive per-listing call and the search response
        # already tells us the city for free. Rejecting on the city centroid
        # is rough (it's a city, not a yard-precise pin), so this only ever
        # rejects; anything that survives still gets the exact seller-pin
        # check further down. Listings with no usable city, or a city we've
        # never seen a real pin for, fall through to that precise check.
        city = _listing_city(listing)
        if city:
            city_coords = lookup_city(store, city)
            if city_coords is not None:
                city_distance = haversine_miles(
                    latitude, longitude, city_coords[0], city_coords[1]
                )
                if city_distance > watch.radius_miles:
                    city_filtered_count += 1
                    store.mark_seen(watch.name, listing_id)
                    continue

        description_matches = False
        if not title_matches:
            # check_description is necessarily True here - the cheap early
            # return above already dropped the alternative.
            description = _extract_description(get_item_detail())
            if state.out_of_credits:
                # An empty description here isn't "no keywords in it", it's "we
                # couldn't look". Falling through would mark this listing seen
                # as a keyword mismatch and never revisit it.
                break
            description_matches = matches_any_keyword(
                description, watch.keywords, watch.keyword_any_order
            )

        if not title_matches and not description_matches:
            keyword_mismatch_count += 1
            store.mark_seen(watch.name, listing_id)  # checked once, decided - never re-check
            continue

        exclusion_text = title
        if watch.check_description:
            exclusion_text = f"{title} {_extract_description(get_item_detail())}"
        if matches_exclusions(exclusion_text, watch.exclude_keywords):
            excluded_count += 1
            store.mark_seen(watch.name, listing_id)
            continue

        price = (listing.get("price") or {}).get("amount")
        if watch.price_min is not None and price is not None and price < watch.price_min:
            price_filtered_count += 1
            store.mark_seen(watch.name, listing_id)  # checked once, decided - never re-check
            continue
        if watch.price_max is not None and price is not None and price > watch.price_max:
            price_filtered_count += 1
            store.mark_seen(watch.name, listing_id)  # checked once, decided - never re-check
            continue

        # radius_miles is sent to SociaVault's search too, but Facebook itself
        # doesn't honor it strictly - scrolling far enough surfaces listings
        # well outside the requested radius, the same way it surfaces
        # keyword-irrelevant ones (hence matches_any_keyword above). The
        # search response's own `location` field is city-level only (no
        # coordinates); the real per-listing pin only comes from item detail,
        # so this necessarily costs a credit even for watches that don't need
        # description/condition. Listings with no coordinates at all (e.g.
        # ship-only listings with no fixed pickup point) are dropped too,
        # same as an item-detail fetch failure - neither can be confirmed
        # in-range, and treating them as in-range is exactly the bug we're
        # fixing.
        item_detail_for_location = get_item_detail()
        if item_detail_for_location is None:
            if state.out_of_credits:
                # Same reasoning as above, and the reason this branch exists at
                # all: an unpaid bill must not permanently discard listings.
                break
            # The fetch itself failed - a transport/API problem, NOT a fact
            # about this listing. Counted separately from "no coordinates"
            # because a spike here means something is broken upstream.
            item_fetch_failed_count += 1
            store.mark_seen(watch.name, listing_id)
            continue
        item_location = _extract_location(item_detail_for_location)
        if item_location is None:
            no_coords_count += 1
            store.mark_seen(watch.name, listing_id)
            continue

        # The ONLY way location_cache gets populated for listing cities (see
        # lookup_city for why geocoding is not trusted here). We've already
        # paid for this item's detail and it carries a real pin from Facebook,
        # so record the city at no extra cost. One seller's pin stands in for
        # the city centroid, which is plenty for the coarse pre-filter above,
        # and unlike a geocoder it cannot place a nearby suburb 140 mi away.
        if city and store.get_cached_location(city) is None:
            store.cache_location(city, item_location[0], item_location[1])

        distance_miles = haversine_miles(latitude, longitude, item_location[0], item_location[1])
        if distance_miles > watch.radius_miles:
            out_of_range_count += 1
            store.mark_seen(watch.name, listing_id)
            continue

        # Only spend an extra credit on item detail if the watch actually
        # needs condition info - the search endpoint alone doesn't include it.
        if watch.condition_filter:
            condition = _extract_condition(get_item_detail())
            if state.out_of_credits:
                break
            if condition is not None and watch.condition_filter.lower() not in condition.lower():
                condition_filtered_count += 1
                store.mark_seen(watch.name, listing_id)  # durable decision - fine to record now
                continue

        formatted_price = (listing.get("price") or {}).get("formatted_amount", str(price or ""))
        # The listing id deliberately isn't a column of its own: Sheets stores
        # a bare number at ~15 significant digits, so 16- and 17-digit ids were
        # silently rounded (27% of them corrupted) and nothing ever read the
        # column back. The id is intact in the link, which is stored as text.
        new_rows.append(
            [
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                title,
                formatted_price,
                f"{distance_miles:.1f}",
                listing_link(listing_id),
                _extract_posted_date(item_detail_for_location),
                watch.name,
            ]
        )
        new_listings.append(
            {
                "title": title,
                "price": formatted_price,
                "distance": f"{distance_miles:.1f}",
                "link": listing_link(listing_id),
                "posted": _extract_posted_date(item_detail_for_location),
            }
        )
        pending_seen_ids.append(listing_id)

    # Every way a candidate can be rejected, in pipeline order. Keep this in
    # sync with the counters above - it drives both the summary line and the
    # all-dropped alarm below.
    reject_stages = [
        ("offsite_partner_listing", offsite_count),
        ("posted_before_cutoff", too_old_count),
        ("keyword_mismatch", keyword_mismatch_count),
        ("excluded_keyword", excluded_count),
        ("price_filtered", price_filtered_count),
        ("city_prefilter_out_of_radius", city_filtered_count),
        ("item_fetch_FAILED", item_fetch_failed_count),
        ("no_coordinates", no_coords_count),
        ("out_of_radius_seller_pin", out_of_range_count),
        ("condition_filtered", condition_filtered_count),
    ]
    considered = len(candidates) - already_seen_count
    rejected_summary = ", ".join(f"{name}={count}" for name, count in reject_stages if count)

    if undated_count:
        logger.warning(
            "Watch '%s': %d of %d considered listing(s) carried no usable "
            "creation_time, so the age filter (%s) could not be applied to them - "
            "they were kept rather than dropped. This field was present on every "
            "listing when the filter was written; if this number is large, "
            "SociaVault has changed the search payload and the age filter is no "
            "longer doing its job.",
            watch.name, undated_count, considered, _age_cutoff_label(watch),
        )

    # INFO, not DEBUG: cron never passes --verbose, so at DEBUG this breakdown
    # was invisible in exactly the situation it exists for. Without it the only
    # signal a normal run gives is "no new listings", which is what made the
    # get_item() outage indistinguishable from a quiet market for two days.
    logger.info(
        "Watch '%s': %d raw -> %d unique -> %d already seen -> %d considered -> %d new%s",
        watch.name, total_raw, len(candidates), already_seen_count, considered,
        len(new_rows),
        f" | rejected: {rejected_summary}" if rejected_summary else "",
    )

    if not new_rows:
        dominant_stage, dominant_count = max(reject_stages, key=lambda s: s[1])
        all_lost_to_one_stage = considered > 0 and dominant_count == considered

        if all_lost_to_one_stage and dominant_stage == "posted_before_cutoff":
            # The one stage that is allowed to eat 100% of candidates without
            # it meaning anything is broken: it means nothing new was posted
            # since the cutoff, which on a tight window is most runs. INFO, not
            # WARNING - the alarm below exists to catch systemic failure, and
            # one that cries wolf every three hours stops being read.
            logger.info(
                "Watch '%s': %d candidate(s) considered, all posted before the "
                "age cutoff (%s). Nothing new since then - widen or remove that "
                "setting to see them.",
                watch.name, considered, _age_cutoff_label(watch),
            )
        elif all_lost_to_one_stage and dominant_stage == "keyword_mismatch":
            # Report `considered`, NOT total_raw. The condition above is about
            # newly-considered listings, but this used to quote the raw search
            # total - which includes everything already skipped as seen. On
            # 2026-08-15 that read "ALL 172 failed the keyword match" when the
            # real number was 4, pointing at keyword tuning that wasn't needed.
            logger.warning(
                "Watch '%s': %d newly-considered listing(s) (%d more skipped as "
                "already seen), and ALL of them failed the keyword match against "
                "%s. Run with --verbose to see sample titles and check whether "
                "your keyword variants are too strict for how sellers actually "
                "word titles.",
                watch.name, considered, already_seen_count, watch.keywords,
            )
        elif all_lost_to_one_stage:
            # The canary that was missing on 2026-08-13: when a single stage
            # eats 100% of candidates, that is almost never a quiet market -
            # it's a bug or an upstream API change. The get_item() envelope bug
            # produced exactly this shape (every candidate dying at the
            # location stage) and still logged nothing louder than "no new
            # listings" for two days.
            logger.warning(
                "Watch '%s': %d candidate(s) considered, 0 survived - ALL of "
                "them were rejected at a single stage: '%s'. One stage "
                "rejecting 100%% of candidates usually means a systemic "
                "failure rather than a genuinely quiet market. Full breakdown: %s",
                watch.name, considered, dominant_stage, rejected_summary,
            )
        logger.info("Watch '%s': no new listings", watch.name)
        return 0

    if dry_run:
        logger.info(
            "Watch '%s': %d new listing(s) (dry run - not written, not marked seen%s)",
            watch.name,
            len(new_rows),
            ", not notified" if watch.notify else "",
        )
    else:
        if watch.output_type == "google_sheets":
            assert sheets is not None, "SheetsClient should have been created - config bug"
            sheets.append_rows(watch.sheet_id, watch.sheet_tab, new_rows)
        else:
            xlsx_writer.append_rows(watch.output_file, watch.sheet_tab, new_rows)

        # Write succeeded - now, and only now, record these as seen.
        for listing_id in pending_seen_ids:
            store.mark_seen(watch.name, listing_id)
        logger.info("Watch '%s': %d new listing(s) added", watch.name, len(new_rows))

        # Strictly last, and strictly non-fatal. The rows are written and the
        # ids are marked seen by this point, so a failure here costs one
        # notification and nothing else. Raising instead would abort the watch
        # after its side effects had already landed, and run_all would report
        # the whole run as failed over an undelivered message.
        if watch.notify and notifier is not None:
            try:
                notifier.send(watch.name, new_listings)
                logger.info("Watch '%s': notified about %d listing(s)", watch.name, len(new_listings))
            except Exception as exc:
                logger.warning(
                    "Watch '%s': %d listing(s) were saved, but the Telegram "
                    "notification failed: %s. The listings are in the sheet - "
                    "only the alert was lost.",
                    watch.name, len(new_listings), exc,
                )

    for row in new_rows:
        logger.info("  + %s - %s - %s", row[1], row[2], row[3])

    return len(new_rows)


def _fetch_item(
    client: SociaVaultClient, listing_id: str, state: RunState
) -> Optional[dict]:
    """Fetches full item detail (description, condition, etc.) - costs an
    extra SociaVault credit, so callers should only invoke this when at
    least one enabled feature (check_description or condition_filter)
    actually needs it, and at most once per listing.

    Records credit exhaustion on `state` rather than just logging it: callers
    mark a listing seen when this returns None, and "seen" is a one-way door.
    On 2026-08-08 that door closed on 50 listings whose only sin was being
    considered after the account hit zero.
    """
    try:
        return client.get_item(listing_id)
    except SociaVaultError as exc:
        if exc.is_out_of_credits:
            state.out_of_credits = True
            logger.error(
                "SociaVault is OUT OF CREDITS (HTTP 402) while fetching item %s. "
                "Abandoning this watch; the listings it hadn't decided yet stay "
                "unseen and will be reconsidered once the account is topped up.",
                listing_id,
            )
        else:
            logger.warning("Couldn't fetch item detail for %s: %s", listing_id, exc)
        return None


def _extract_description(item: Optional[dict]) -> str:
    if not item:
        return ""
    return item.get("description") or ""


def _extract_location(item: Optional[dict]) -> Optional[tuple[float, float]]:
    if not item:
        return None
    location = item.get("location") or {}
    lat, lng = location.get("latitude"), location.get("longitude")
    if lat is None or lng is None:
        return None
    return lat, lng


def _extract_posted_date(item: Optional[dict]) -> str:
    """When the seller actually posted the listing, as a bare local date.

    Costs nothing: item detail is already fetched for every listing that gets
    this far (the seller-pin radius check above needs it), and the search
    response carries the field but leaves it null.

    Three deliberate choices:

    - **Bare date, not a timestamp.** Written with USER_ENTERED, a bare
      `2026-08-21` lands in Sheets as a real date; the "Date Found" column's
      `... UTC` suffix makes that one text, which no formula can subtract.
    - **Local time, not UTC**, unlike every other timestamp this project
      writes. The sibling evaluation tool stamps its "Closed" column with a
      local `datetime.now()`, and "on market" is Closed minus Posted - mixing
      the two zones would put anything posted after 20:00 EDT a day out.
    - **Empty when absent**, never the `listing_date_text` ("Listed 3 weeks
      ago") sitting beside it in the response. That field is prose, and prose
      in a date column breaks the same arithmetic.
    """
    if not item:
        return ""
    raw = item.get("creation_time")
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        logger.warning(
            "Unparseable creation_time %r - leaving Posted At empty for this row.", raw
        )
        return ""


def _extract_condition(item: Optional[dict]) -> Optional[str]:
    """The seller's stated condition, e.g. "used_good". None when absent.

    Two things about the payload that the first version of this got wrong, both
    confirmed against a live item-detail response on 2026-09-26:

        "attributes": {"0": {"attribute_name": "Condition",
                             "value": "used_good",
                             "label": "Used - good"}}

    - `attributes` is an object keyed by string index, NOT a JSON array - the
      same shape quirk `SociaVaultClient.search()` already normalizes for
      listings. Iterating it as a list yields the bare key `"0"`, and calling
      `.get()` on a string raises AttributeError, so any watch that switched
      `condition_filter` on would have crashed its whole run.
    - The attribute's NAME is in `attribute_name`. `label` holds the human
      rendering of its value ("Used - good"), so matching `label ==
      "Condition"` never matched anything either way.

    Still tolerates a plain list, since that's what the docs show and it costs
    one isinstance to keep working if they ever ship it that way.
    """
    if not item:
        return None
    attributes = item.get("attributes") or {}
    if isinstance(attributes, dict):
        attributes = list(attributes.values())
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        if str(attribute.get("attribute_name", "")).strip().lower() == "condition":
            # `value` is the machine-readable form and what condition_filter is
            # written against; `label` is the fallback if it's ever missing.
            return attribute.get("value") or attribute.get("label")
    return None


def run_all(config: AppConfig, only_watch: Optional[str] = None, dry_run: bool = False) -> int:
    client = SociaVaultClient(config.sociavault_api_key, timeout=config.request_timeout)
    store = Store(config.db_path)
    xlsx_writer = XlsxWriter()

    watches = [w for w in config.watches if not only_watch or w.name == only_watch]
    if not watches:
        raise ValueError(f"No watch named '{only_watch}' found in config.")

    # Only touch Google auth at all if some watch actually needs it.
    sheets = None
    if any(w.output_type == "google_sheets" for w in watches):
        sheets = SheetsClient(config.google_service_account_file)

    # Built whenever Telegram is configured at all, not just when some watch
    # sets notify: true - run-level alerts below use it regardless of the
    # per-watch flags. Individual finds are still gated on watch.notify.
    telegram = config.notifications.telegram if config.notifications else None
    notifier = (
        TelegramNotifier(telegram.bot_token, telegram.chat_id, timeout=config.request_timeout)
        if telegram
        else None
    )

    run_state = RunState()
    total_new = 0
    had_error = False
    for watch in watches:
        try:
            total_new += run_watch(
                client, sheets, xlsx_writer, store, watch,
                dry_run=dry_run, notifier=notifier, run_state=run_state,
            )
        except Exception:
            logger.exception("Watch '%s' failed unexpectedly", watch.name)
            had_error = True

    if run_state.out_of_credits:
        # Alert first, then fail: the exit code is what cron sees, but the
        # push is what a person sees. This run collected whatever it managed
        # to before the account hit zero, and that much is already written.
        _alert_out_of_credits(notifier, dry_run)
        raise RuntimeError(
            "SociaVault ran out of credits (HTTP 402) - this run is incomplete. "
            "Top up the account; the next run picks up where it stopped."
        )

    if had_error:
        raise RuntimeError("One or more watches failed. See the log above for details.")

    return total_new


def _alert_out_of_credits(notifier: Optional[TelegramNotifier], dry_run: bool) -> None:
    """Say it once per run, loudly, on every channel available."""
    logger.error(
        "SociaVault is out of credits, so this run searched nothing and can say "
        "nothing about what is or isn't listed. Nothing was marked seen on the "
        "way out, so no listing is lost - top up and the next run catches up."
    )
    if notifier is None or dry_run:
        return
    try:
        notifier.send_alert(
            "FB Marketplace monitor is blind: SociaVault is out of credits. "
            "No listings are being collected until the account is topped up. "
            "Nothing has been lost - the backlog is picked up on the next run."
        )
    except Exception as exc:
        # Same rule as everywhere else here: an undelivered message is never
        # allowed to become a second failure on top of the first.
        logger.warning("Couldn't send the out-of-credits alert: %s", exc)
