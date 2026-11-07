"""Configuration loading and validation for the FB Marketplace monitor."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

MILES_TO_KM = 1.60934


@dataclass
class WatchConfig:
    name: str
    keywords: list[str]
    city: str
    radius_miles: float
    sheet_tab: str  # used as the worksheet/tab name for either output backend
    output_type: str = "xlsx"  # "xlsx" (local file, no setup) or "google_sheets"
    sheet_id: Optional[str] = None  # required if output_type == "google_sheets"
    output_file: Optional[str] = None  # required if output_type == "xlsx"
    search_terms: list[str] = field(default_factory=list)  # what's SENT to SociaVault's search
    check_description: bool = False  # fall back to checking description if title doesn't match
    # Also accept a keyword whose words all appear in any order ("KTM 890"
    # matches "890 KTM Adventure R"). Off by default: on short keywords it
    # over-matches - "Cobia 220" would hit "Cobia 256 with 220 hp". Applies to
    # `keywords` only, never to exclude_keywords. See matching.matches_any_keyword.
    keyword_any_order: bool = False
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    # Two ways to say "don't show me stale listings"; both optional, and a
    # watch may set both - then the stricter one wins. See monitor._age_cutoff.
    #
    # posted_after is a fixed LOCAL date, inclusive: `2026-09-01` keeps
    # listings posted at any time on September 1st. It never moves by itself.
    # max_age_days is the rolling equivalent, measured in 24-hour periods back
    # from the moment the run starts: `30` keeps the last 30 days, always.
    posted_after: Optional[date] = None
    max_age_days: Optional[int] = None
    exclude_keywords: list[str] = field(default_factory=list)
    condition_filter: Optional[str] = None  # e.g. "used" or "new"; omit to skip the check
    max_pages: int = 5
    # Drop listings fulfilled off Facebook (delivery type SHIPPING_OFFSITE) -
    # eBay and similar partner listings. Defaults ON: they can't be inspected
    # or collected in person, and their item detail carries no seller pin, so
    # they're unusable for a local-pickup watch. See monitor.is_offsite_listing.
    exclude_offsite: bool = True
    # Push a Telegram message when this watch finds something. Off by default:
    # it's meant for watches that match rarely (a specific tire size, roughly
    # once every three days), not for the ones that match dozens of times a
    # day. See notifications in AppConfig for the credentials it needs.
    notify: bool = False

    @property
    def radius_km(self) -> int:
        return round(self.radius_miles * MILES_TO_KM)

    @property
    def effective_search_terms(self) -> list[str]:
        """What actually gets sent as the `query` to SociaVault. Falls back to
        `keywords` if `search_terms` isn't set, for backward compatibility -
        but that fallback is exactly what caused the "found nothing" bug:
        raw size strings like "285 70 18" are worse search queries than
        "285 70 18 tires". Set search_terms explicitly to control this.
        """
        return self.search_terms or self.keywords


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass
class NotificationsConfig:
    telegram: Optional[TelegramConfig] = None


@dataclass
class AppConfig:
    sociavault_api_key: str
    watches: list[WatchConfig]
    google_service_account_file: Optional[str] = None  # only needed if any watch uses google_sheets
    db_path: str = "data/seen_listings.db"
    log_file: Optional[str] = None
    request_timeout: int = 15
    notifications: Optional[NotificationsConfig] = None  # only needed if a watch sets notify: true


def _parse_posted_after(value, context: str) -> Optional[date]:
    """A watch's age cutoff, as a plain date.

    Raises rather than falling back to "no cutoff" on bad input. A filter that
    silently does nothing is the worst outcome here: the run looks healthy, the
    log says nothing, and months-old listings keep arriving exactly as before -
    which is the complaint this option exists to answer.

    PyYAML already turns an unquoted `2026-09-01` into a `date`, so the string
    branch is for `"2026-09-01"` written with quotes.
    """
    if value is None or value == "":
        return None
    # datetime is a subclass of date, so it has to be tested first. A value
    # with a time attached (`2026-09-01 08:00:00`) keeps only the date part -
    # the cutoff is deliberately whole-day, see WatchConfig.posted_after.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise ValueError(
        f"Invalid posted_after {value!r} in {context} - expected a date in "
        "YYYY-MM-DD form, e.g. 2026-09-01, or omit the key for no age limit."
    )


def _parse_max_age_days(value, context: str) -> Optional[int]:
    """A watch's rolling age window, in whole days.

    Rejects anything that isn't a positive whole number, for the same reason
    _parse_posted_after does: the failure mode of guessing is a filter that
    looks configured and silently isn't. `2.5` is refused rather than truncated
    to 2 - a cutoff quietly half a day off is worse than being told to fix it.
    """
    if value is None or value == "":
        return None
    # bool is a subclass of int, so `max_age_days: true` would otherwise sail
    # through as a 1-day window.
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(
            f"Invalid max_age_days {value!r} in {context} - expected a whole "
            "number of days, e.g. 30."
        )
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            raise ValueError(
                f"Invalid max_age_days {value!r} in {context} - expected a whole "
                "number of days, e.g. 30."
            )
        value = int(stripped)
    if not isinstance(value, int):
        raise ValueError(
            f"Invalid max_age_days {value!r} in {context} - expected a whole "
            "number of days, e.g. 30."
        )
    if value < 1:
        raise ValueError(
            f"Invalid max_age_days {value} in {context} - must be at least 1. "
            "Omit the key entirely for no age limit."
        )
    return value


def _parse_strict_bool(value, key: str, default: bool, context: str) -> bool:
    """A yes/no setting that refuses anything but a real YAML boolean.

    bool() would read a quoted "false" as True - the opposite of what was
    written - so a typo'd value is refused rather than guessed at.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(
        f"Invalid {key} {value!r} in {context} - expected true or false (unquoted)."
    )


def _require(d: dict, key: str, context: str):
    if key not in d or d[key] in (None, ""):
        raise ValueError(f"Missing required config key '{key}' in {context}")
    return d[key]


def load_config(path: str) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml "
            "and fill in your values."
        )

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # Secrets can come from the config file OR environment variables.
    # Env vars win, so you can keep credentials out of the YAML if you prefer.
    api_key = os.environ.get("SOCIAVAULT_API_KEY") or raw.get("sociavault_api_key")
    if not api_key:
        raise ValueError(
            "SociaVault API key missing. Set 'sociavault_api_key' in the config "
            "or the SOCIAVAULT_API_KEY environment variable."
        )

    watches_raw = raw.get("watches")
    if not watches_raw:
        raise ValueError("Config must define at least one entry under 'watches'.")

    watches: list[WatchConfig] = []
    for i, w in enumerate(watches_raw):
        context = f"watches[{i}] ({w.get('name', '?')})"
        output_type = w.get("output_type", "xlsx")
        if output_type not in ("xlsx", "google_sheets"):
            raise ValueError(
                f"Invalid output_type '{output_type}' in {context} - "
                "must be 'xlsx' or 'google_sheets'."
            )

        sheet_id = None
        output_file = None
        if output_type == "google_sheets":
            sheet_id = _require(w, "sheet_id", context)
        else:
            output_file = _require(w, "output_file", context)

        watches.append(
            WatchConfig(
                name=_require(w, "name", context),
                keywords=_require(w, "keywords", context),
                city=_require(w, "city", context),
                radius_miles=float(w.get("radius_miles", 50)),
                sheet_tab=w.get("sheet_tab", "Sheet1"),
                output_type=output_type,
                sheet_id=sheet_id,
                output_file=output_file,
                search_terms=w.get("search_terms", []) or [],
                check_description=bool(w.get("check_description", False)),
                keyword_any_order=_parse_strict_bool(
                    w.get("keyword_any_order"), "keyword_any_order", False, context
                ),
                price_min=w.get("price_min"),
                price_max=w.get("price_max"),
                posted_after=_parse_posted_after(w.get("posted_after"), context),
                max_age_days=_parse_max_age_days(w.get("max_age_days"), context),
                exclude_keywords=w.get("exclude_keywords", []) or [],
                condition_filter=w.get("condition_filter"),
                max_pages=int(w.get("max_pages", 5)),
                exclude_offsite=bool(w.get("exclude_offsite", True)),
                notify=bool(w.get("notify", False)),
            )
        )

    names = [w.name for w in watches]
    if len(names) != len(set(names)):
        raise ValueError("Watch names must be unique (used as the dedupe key).")

    # A cutoff in the future rejects every listing there will ever be, and the
    # run still exits 0 saying "no new listings". Not fatal - it's a plausible
    # thing to want for a day or two - but it must never be silent.
    today = date.today()
    for w in watches:
        if w.posted_after and w.max_age_days:
            # Legal, and occasionally what you want ("last 30 days, but never
            # before the day I reset this watch"). Said out loud anyway, so the
            # one that's doing nothing today isn't a mystery later.
            logger.info(
                "Watch '%s' sets both posted_after (%s) and max_age_days (%d) - "
                "whichever cutoff is later applies.",
                w.name, w.posted_after, w.max_age_days,
            )
        if w.posted_after and w.posted_after > today:
            logger.warning(
                "Watch '%s' has posted_after: %s, which is in the FUTURE - it will "
                "reject every listing until that date arrives, and the run will "
                "still look healthy. Set it to a past date to collect anything.",
                w.name, w.posted_after,
            )

    # Only require Google credentials if at least one watch actually needs them.
    creds_file = None
    if any(w.output_type == "google_sheets" for w in watches):
        creds_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or raw.get(
            "google_service_account_file"
        )
        if not creds_file:
            raise ValueError(
                "One or more watches use output_type 'google_sheets', but no "
                "Google service account file is configured. Set "
                "'google_service_account_file' in the config or the "
                "GOOGLE_SERVICE_ACCOUNT_FILE environment variable."
            )
        if not Path(creds_file).exists():
            raise FileNotFoundError(f"Google service account file not found: {creds_file}")

    notifications = _load_notifications(raw, watches)

    return AppConfig(
        sociavault_api_key=api_key,
        google_service_account_file=creds_file,
        watches=watches,
        db_path=raw.get("db_path", "data/seen_listings.db"),
        log_file=raw.get("log_file"),
        request_timeout=int(raw.get("request_timeout", 15)),
        notifications=notifications,
    )


def _load_notifications(raw: dict, watches: list[WatchConfig]) -> Optional[NotificationsConfig]:
    """Telegram credentials, used only by watches that ask to be notified.

    Missing credentials are logged loudly and then ignored. They are NOT a
    startup error: notifications are an optional extra on top of the real job,
    and refusing to start would take down every watch - including the ones
    that never asked for an alert - because an unrelated side channel wasn't
    configured. The ERROR line repeats every run, which is loud enough that a
    watch can't silently stop notifying without it showing in the log.
    """
    # Canonical shape is a top-level `telegram:` block. `notifications.telegram`
    # is also accepted: it's what this shipped with in 0c8949b, and the extra
    # nesting earned nothing with only one channel to configure.
    telegram_raw = raw.get("telegram") or (raw.get("notifications") or {}).get("telegram") or {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or telegram_raw.get("bot_token")
    # chat_id is numeric in YAML (and negative for groups) - keep it a string.
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or telegram_raw.get("chat_id")

    wants_notifying = [w.name for w in watches if w.notify]
    if wants_notifying:
        missing = [
            name
            for name, value in (("bot_token", token), ("chat_id", chat_id))
            if not value
        ]
        if missing:
            logger.error(
                "Watch(es) %s set notify: true, but telegram.%s %s missing - "
                "those watches will run normally and write their listings, but "
                "send NO alerts. Set %s in the config (or via TELEGRAM_BOT_TOKEN "
                "/ TELEGRAM_CHAT_ID) to turn alerts on, or remove notify: true "
                "to silence this.",
                ", ".join(wants_notifying),
                " and .".join(missing),
                "is" if len(missing) == 1 else "are",
                "it" if len(missing) == 1 else "them",
            )

    if not token or not chat_id:
        return None
    return NotificationsConfig(telegram=TelegramConfig(bot_token=token, chat_id=str(chat_id)))
