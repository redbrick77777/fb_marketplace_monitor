"""Core orchestration: run one watch (or all configured watches) end-to-end."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .config import AppConfig, WatchConfig
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
        description_matches = False
        if not title_matches and watch.check_description:
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
                listing_link(listing_id),
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                watch.name,
            ]
        )
        pending_seen_ids.append(listing_id)

    logger.debug(
        "Watch '%s': %d raw -> %d unique candidates -> already_seen=%d, "
        "keyword_mismatch=%d, excluded_keyword=%d, price_filtered=%d, "
        "condition_filtered=%d, new=%d",
        watch.name, total_raw, len(candidates), already_seen_count,
        keyword_mismatch_count, excluded_count, price_filtered_count,
        condition_filtered_count, len(new_rows),
    )

    if not new_rows:
        if total_raw > 0 and keyword_mismatch_count == len(candidates) - already_seen_count and keyword_mismatch_count > 0:
            logger.warning(
                "Watch '%s': got %d raw listing(s) from SociaVault, but ALL of "
                "them failed the keyword match against %s. Run with --verbose "
                "to see sample titles and check whether your keyword variants "
                "are too strict for how sellers actually word titles.",
                watch.name, total_raw, watch.keywords,
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
