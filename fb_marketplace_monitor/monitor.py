"""Core orchestration: run one watch (or all configured watches) end-to-end."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .config import AppConfig, WatchConfig
from .geo import haversine_miles
from .matching import matches_any_keyword, matches_exclusions, to_broad_query
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


def resolve_city(
    client: SociaVaultClient, store: Store, city: str
) -> Optional[tuple[float, float]]:
    """City name -> coordinates, via the persistent cache, geocoding on a miss.

    Worth a credit in a way item detail never is: cities are a small, bounded
    set that repeats across every future run (one full watchflip sweep touched
    39 distinct cities), whereas listing IDs are unbounded and single-use. A
    credit spent geocoding "Orlando, Florida" once rejects every Orlando
    listing for free from then on; a credit spent on item detail is never
    reusable.

    Returns None on any failure so callers fail OPEN - this is an optimization,
    not a correctness filter, and the seller-pin check downstream is what
    actually decides. Never let a geocoding hiccup drop a real listing.
    """
    cached = store.get_cached_location(city)
    if cached:
        return cached
    try:
        location = client.resolve_location(city)
        lat, lng = location["latitude"], location["longitude"]
    except (SociaVaultError, KeyError, TypeError) as exc:
        logger.debug("Couldn't geocode city '%s' (failing open): %s", city, exc)
        return None
    store.cache_location(city, lat, lng)
    logger.info("Geocoded '%s' -> (%.4f, %.4f) [cached for future runs]", city, lat, lng)
    return lat, lng


def run_watch(
    client: SociaVaultClient,
    sheets: Optional[SheetsClient],
    xlsx_writer: XlsxWriter,
    store: Store,
    watch: WatchConfig,
    dry_run: bool = False,
) -> int:
    """Runs a single watch end-to-end. Returns the number of new rows added."""
    logger.info("Running watch '%s' (%s, %s mi radius)", watch.name, watch.city, watch.radius_miles)
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

    if total_raw == 0:
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
    # Listing IDs are only marked "seen" once the write actually succeeds
    # (see below) - not here - so a failed write or a --dry-run never loses
    # a listing; it'll simply be picked up again on the next real run.
    pending_seen_ids: list[str] = []
    already_seen_count = 0
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

    for listing_id, listing in candidates.items():
        if store.has_seen(watch.name, listing_id):
            already_seen_count += 1
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
                item_detail = _fetch_item(client, listing_id)
            return item_detail

        title_matches = matches_any_keyword(title, watch.keywords)

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
        # check further down. Listings with no usable city fall through and
        # get decided by that precise check instead.
        city = _listing_city(listing)
        if city:
            city_coords = resolve_city(client, store, city)
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
            description_matches = matches_any_keyword(description, watch.keywords)

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

        # Free cache fill: we've already paid for this item's detail and it
        # carries a real pin, so record the city now rather than spending a
        # location-search credit on it later. One seller's pin stands in for
        # the city centroid, which is plenty for the coarse pre-filter above -
        # and this also covers cities resolve_city() couldn't geocode at all.
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
            if condition is not None and watch.condition_filter.lower() not in condition.lower():
                condition_filtered_count += 1
                store.mark_seen(watch.name, listing_id)  # durable decision - fine to record now
                continue

        formatted_price = (listing.get("price") or {}).get("formatted_amount", str(price or ""))
        new_rows.append(
            [
                listing_id,
                title,
                formatted_price,
                f"{distance_miles:.1f}",
                listing_link(listing_id),
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                watch.name,
            ]
        )
        pending_seen_ids.append(listing_id)

    # Every way a candidate can be rejected, in pipeline order. Keep this in
    # sync with the counters above - it drives both the summary line and the
    # all-dropped alarm below.
    reject_stages = [
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

        if all_lost_to_one_stage and dominant_stage == "keyword_mismatch":
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
            "Watch '%s': %d new listing(s) (dry run - not written, not marked seen)",
            watch.name,
            len(new_rows),
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

    for row in new_rows:
        logger.info("  + %s - %s - %s", row[1], row[2], row[3])

    return len(new_rows)


def _fetch_item(client: SociaVaultClient, listing_id: str) -> Optional[dict]:
    """Fetches full item detail (description, condition, etc.) - costs an
    extra SociaVault credit, so callers should only invoke this when at
    least one enabled feature (check_description or condition_filter)
    actually needs it, and at most once per listing."""
    try:
        return client.get_item(listing_id)
    except SociaVaultError as exc:
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


def _extract_condition(item: Optional[dict]) -> Optional[str]:
    if not item:
        return None
    return next(
        (a.get("value") for a in item.get("attributes", []) if a.get("label") == "Condition"),
        None,
    )


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

    total_new = 0
    had_error = False
    for watch in watches:
        try:
            total_new += run_watch(client, sheets, xlsx_writer, store, watch, dry_run=dry_run)
        except Exception:
            logger.exception("Watch '%s' failed unexpectedly", watch.name)
            had_error = True

    if had_error:
        raise RuntimeError("One or more watches failed. See the log above for details.")

    return total_new
