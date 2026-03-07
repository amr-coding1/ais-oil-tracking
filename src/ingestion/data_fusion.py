"""Unified vessel state management fusing data from all three AIS sources.

All three sources (aisstream, Digitraffic, BarentsWatch) report MMSI as the
common identifier. This module deduplicates updates, maintains the canonical
vessel state, and routes data into the database.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from src.database import (
    classify_vessel,
    get_connection,
    init_db,
    insert_position,
    is_tanker,
    upsert_vessel,
    upsert_vessel_state,
)
from src.tracking.vessel_tracker import VesselTracker

logger = logging.getLogger(__name__)

# Minimum seconds between position inserts for the same MMSI
# to avoid flooding the database from overlapping sources.
DEDUP_WINDOW_SECONDS = 60


class DataFusionEngine:
    """Receives callbacks from all AIS clients, deduplicates, and persists."""

    def __init__(self, db_path: str | None = None) -> None:
        self.conn = init_db(db_path)
        self.tracker = VesselTracker(self.conn)
        self._lock = threading.Lock()
        # Track last position insert time per MMSI for dedup
        self._last_insert: dict[int, float] = {}
        # In-memory metadata cache for joining position + static data
        self._metadata_cache: dict[int, dict[str, Any]] = {}
        self._position_count = 0
        self._metadata_count = 0

    # ------------------------------------------------------------------
    # Callbacks — these are called from multiple threads
    # ------------------------------------------------------------------

    def on_position(
        self,
        mmsi: int,
        timestamp: str,
        latitude: float | None,
        longitude: float | None,
        sog: float | None = None,
        cog: float | None = None,
        heading: int | None = None,
        nav_status: int | None = None,
        source: str = "unknown",
    ) -> None:
        """Handle a position report from any AIS source."""
        if latitude is None or longitude is None:
            return

        now = time.time()
        with self._lock:
            # Dedup: skip if we inserted a position for this MMSI recently
            last = self._last_insert.get(mmsi, 0)
            if now - last < DEDUP_WINDOW_SECONDS:
                return

            # Check if this vessel is a known tanker (or unknown — keep it
            # until we get metadata confirming ship type)
            cached = self._metadata_cache.get(mmsi, {})
            ship_type = cached.get("ship_type")
            if ship_type is not None and not is_tanker(ship_type):
                return

            # Get draft from metadata cache if not in position report
            draft_m = cached.get("draft_m")
            destination = cached.get("destination")

            # Determine region from geofencing
            region = self.tracker.assign_region(latitude, longitude)

            # Determine vessel status
            max_draft = self._get_max_draft(mmsi)
            status = self.tracker.determine_status(
                sog=sog, draft_m=draft_m, max_draft=max_draft,
                latitude=latitude, longitude=longitude,
            )

            # Persist — ensure vessel record exists before inserting position
            try:
                upsert_vessel(
                    self.conn, mmsi,
                    ship_type=cached.get("ship_type"),
                    length_m=cached.get("length_m"),
                )
                insert_position(
                    self.conn, mmsi, timestamp, latitude, longitude,
                    sog=sog, cog=cog, heading=heading, draft_m=draft_m,
                    nav_status=nav_status, destination=destination,
                    source=source, region=region,
                )
                upsert_vessel_state(
                    self.conn, mmsi, latitude, longitude,
                    sog, draft_m, destination, region, status, timestamp,
                )
                self.conn.commit()
                self._last_insert[mmsi] = now
                self._position_count += 1
                if self._position_count % 500 == 0:
                    logger.info(
                        "Positions stored: %d | Vessels tracked: %d",
                        self._position_count, len(self._last_insert),
                    )
            except Exception:
                logger.exception("Error inserting position for MMSI %d", mmsi)

    def on_metadata(
        self,
        mmsi: int,
        imo: int | None = None,
        name: str | None = None,
        callsign: str | None = None,
        ship_type: int | None = None,
        length_m: float | None = None,
        beam_m: float | None = None,
        draft_m: float | None = None,
        destination: str | None = None,
        eta: Any = None,
        source: str = "unknown",
    ) -> None:
        """Handle a metadata/static data message from any AIS source."""
        with self._lock:
            # Update in-memory cache
            if mmsi not in self._metadata_cache:
                self._metadata_cache[mmsi] = {}
            cache = self._metadata_cache[mmsi]
            if ship_type is not None:
                cache["ship_type"] = ship_type
            if draft_m is not None:
                cache["draft_m"] = draft_m
            if destination:
                cache["destination"] = destination
            if length_m is not None:
                cache["length_m"] = length_m

            # Only persist tankers (or unknowns we haven't classified yet)
            if ship_type is not None and not is_tanker(ship_type):
                return

            try:
                upsert_vessel(
                    self.conn, mmsi, imo=imo, name=name, callsign=callsign,
                    ship_type=ship_type, length_m=length_m, beam_m=beam_m,
                )
                self.conn.commit()
                self._metadata_count += 1
            except Exception:
                logger.exception("Error upserting vessel MMSI %d", mmsi)

    def _get_max_draft(self, mmsi: int) -> float | None:
        row = self.conn.execute(
            "SELECT max_draft_m FROM vessels WHERE mmsi = ?", (mmsi,)
        ).fetchone()
        return row["max_draft_m"] if row else None

    @property
    def stats(self) -> dict[str, int]:
        return {
            "positions_stored": self._position_count,
            "metadata_updates": self._metadata_count,
            "vessels_in_cache": len(self._metadata_cache),
        }
