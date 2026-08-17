"""Configuration loading and validation for the FB Marketplace monitor."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

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
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    exclude_keywords: list[str] = field(default_factory=list)
    condition_filter: Optional[str] = None  # e.g. "used" or "new"; omit to skip the check
    max_pages: int = 5
    # Drop listings fulfilled off Facebook (delivery type SHIPPING_OFFSITE) -
    # eBay and similar partner listings. Defaults ON: they can't be inspected
    # or collected in person, and their item detail carries no seller pin, so
    # they're unusable for a local-pickup watch. See monitor.is_offsite_listing.
    exclude_offsite: bool = True

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
class AppConfig:
    sociavault_api_key: str
    watches: list[WatchConfig]
    google_service_account_file: Optional[str] = None  # only needed if any watch uses google_sheets
    db_path: str = "data/seen_listings.db"
    log_file: Optional[str] = None
    request_timeout: int = 15


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
                price_min=w.get("price_min"),
                price_max=w.get("price_max"),
                exclude_keywords=w.get("exclude_keywords", []) or [],
                condition_filter=w.get("condition_filter"),
                max_pages=int(w.get("max_pages", 5)),
                exclude_offsite=bool(w.get("exclude_offsite", True)),
            )
        )

    names = [w.name for w in watches]
    if len(names) != len(set(names)):
        raise ValueError("Watch names must be unique (used as the dedupe key).")

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

    return AppConfig(
        sociavault_api_key=api_key,
        google_service_account_file=creds_file,
        watches=watches,
        db_path=raw.get("db_path", "data/seen_listings.db"),
        log_file=raw.get("log_file"),
        request_timeout=int(raw.get("request_timeout", 15)),
    )
