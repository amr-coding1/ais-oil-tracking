"""Floating storage detection algorithm.

A vessel is classified as floating storage when ALL conditions are met:
    1. Speed < 1 knot averaged over a sustained period
    2. Stationary duration > 7 days (filters berth waiting, bunkering, STS)
    3. Draft indicates laden condition: current draft > 70% of max observed draft
    4. Not inside a port terminal berthing zone

Thresholds are documented and defensible:
    - 1 knot: standard maritime definition of "at anchor"
    - 7 days: filters STS transfers (1-3 days) and port waiting (<5 days)
    - 70%: conservative laden estimate accounting for AIS draft inaccuracies
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from src.database import (
    classify_vessel,
    end_floating_storage_event,
    estimate_laden_fraction,
    get_active_floating_storage,
    start_floating_storage_event,
)

logger = logging.getLogger(__name__)

# Detection thresholds
STATIONARY_SPEED_KNOTS = 1.0
MIN_STORAGE_DAYS = 7
LADEN_THRESHOLD = 0.70


class FloatingStorageDetector:
    """Detects and tracks floating storage events."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # Active tracking: mmsi -> {start_time, latitude, longitude, ...}
        self._candidates: dict[int, dict[str, Any]] = {}

    def evaluate(self) -> dict[str, Any]:
        """Run detection across all stationary vessels.

        Call periodically (e.g. every hour). Returns summary stats.
        """
        now = datetime.now(timezone.utc)
        threshold_time = (now - timedelta(days=MIN_STORAGE_DAYS)).isoformat()

        # Find vessels that are currently stationary and laden
        stationary = self.conn.execute(
            """
            SELECT vs.mmsi, vs.latitude, vs.longitude, vs.sog, vs.draft_m,
                   vs.region, vs.status, vs.status_since,
                   v.max_draft_m, v.vessel_class, v.length_m
            FROM vessel_state vs
            JOIN vessels v ON vs.mmsi = v.mmsi
            WHERE vs.sog < ?
              AND vs.status_since IS NOT NULL
              AND vs.region NOT LIKE 'terminal:%'
            """,
            (STATIONARY_SPEED_KNOTS,),
        ).fetchall()

        new_events = 0
        ended_events = 0

        for row in stationary:
            mmsi = row["mmsi"]
            laden_status, load_fraction = estimate_laden_fraction(
                row["draft_m"], row["max_draft_m"]
            )

            # Check all conditions
            is_laden = laden_status == "laden"
            status_since = row["status_since"]
            if status_since and status_since <= threshold_time and is_laden:
                # This vessel qualifies as floating storage
                if not self._has_active_event(mmsi):
                    vessel_class = row["vessel_class"] or "Unknown"
                    _, full_load_bbl = classify_vessel(row["length_m"])
                    cargo_bbl = full_load_bbl * load_fraction

                    start_floating_storage_event(
                        self.conn,
                        mmsi=mmsi,
                        start_time=status_since,
                        region=self._storage_region(row["latitude"], row["longitude"]),
                        latitude=row["latitude"],
                        longitude=row["longitude"],
                        estimated_cargo_bbl=cargo_bbl,
                        draft=row["draft_m"],
                    )
                    new_events += 1
                    logger.info(
                        "New floating storage: MMSI %d (%s) ~%.0f kbbl at (%.2f, %.2f)",
                        mmsi, vessel_class, cargo_bbl / 1000,
                        row["latitude"], row["longitude"],
                    )

        # End events for vessels no longer stationary
        active_events = get_active_floating_storage(self.conn)
        for event in active_events:
            state = self.conn.execute(
                "SELECT sog, status FROM vessel_state WHERE mmsi = ?",
                (event["mmsi"],),
            ).fetchone()
            if state and (state["sog"] is None or state["sog"] >= STATIONARY_SPEED_KNOTS):
                end_floating_storage_event(
                    self.conn, event["id"], now.isoformat()
                )
                ended_events += 1
                logger.info(
                    "Floating storage ended: MMSI %d (event %d)",
                    event["mmsi"], event["id"],
                )

        self.conn.commit()

        return {
            "stationary_vessels": len(stationary),
            "new_storage_events": new_events,
            "ended_storage_events": ended_events,
            "active_storage_events": len(active_events) + new_events - ended_events,
        }

    def get_total_storage_bbl(self, region: str | None = None) -> float:
        """Get total barrels currently in floating storage."""
        sql = """
            SELECT COALESCE(SUM(estimated_cargo_bbl), 0) as total
            FROM floating_storage_events
            WHERE end_time IS NULL
        """
        params: list[Any] = []
        if region:
            sql += " AND region = ?"
            params.append(region)
        row = self.conn.execute(sql, params).fetchone()
        return row["total"] if row else 0.0

    def get_storage_summary(self) -> dict[str, float]:
        """Get floating storage totals by region."""
        rows = self.conn.execute(
            """
            SELECT region, SUM(estimated_cargo_bbl) as total_bbl,
                   COUNT(*) as vessel_count
            FROM floating_storage_events
            WHERE end_time IS NULL
            GROUP BY region
            """
        ).fetchall()
        return {
            row["region"]: {
                "total_bbl": row["total_bbl"],
                "vessel_count": row["vessel_count"],
            }
            for row in rows
        }

    def _has_active_event(self, mmsi: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM floating_storage_events WHERE mmsi = ? AND end_time IS NULL",
            (mmsi,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _storage_region(lat: float, lon: float) -> str:
        """Broad region for floating storage (ARA, North Sea, Baltic)."""
        if 51.0 <= lat <= 52.5 and 3.0 <= lon <= 5.0:
            return "ARA"
        if lon > 12.0:
            return "Baltic"
        return "North Sea"
