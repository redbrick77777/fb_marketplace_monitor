"""SQLite-backed local state.

Tracks which listing IDs have already been added to the Sheet (per watch, so
the same real-world listing can be tracked independently across different
watches), and caches resolved city -> lat/lng lookups so a city is only
resolved once, not spending a SociaVault credit on it every single run.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Store:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        resolved = Path(db_path).resolve()
        logger.info("Store: using database at '%s' (resolved: %s)", db_path, resolved)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_listings (
                    watch_name TEXT NOT NULL,
                    listing_id TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    PRIMARY KEY (watch_name, listing_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS location_cache (
                    city TEXT PRIMARY KEY,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    cached_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def has_seen(self, watch_name: str, listing_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_listings WHERE watch_name = ? AND listing_id = ?",
                (watch_name, listing_id),
            ).fetchone()
            return row is not None

    def mark_seen(self, watch_name: str, listing_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen_listings (watch_name, listing_id, first_seen) "
                "VALUES (?, ?, ?)",
                (watch_name, listing_id, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def get_cached_location(self, city: str) -> Optional[tuple[float, float]]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT latitude, longitude FROM location_cache WHERE city = ?",
                (city,),
            ).fetchone()
            return (row[0], row[1]) if row else None

    def cache_location(self, city: str, latitude: float, longitude: float) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO location_cache "
                "(city, latitude, longitude, cached_at) VALUES (?, ?, ?, ?)",
                (city, latitude, longitude, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
