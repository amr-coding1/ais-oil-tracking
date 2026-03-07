"""Terminal loading event detection.

Detection logic:
    1. Tanker enters terminal geofence (< radius_nm from terminal coordinates)
    2. Speed drops below 1 knot (vessel is alongside / at SBM)
    3. Draft INCREASES during the stay (vessel is filling with crude)
    4. Vessel departs with higher draft than arrival

This tracks Brent basket (BFOET) loading at North Sea terminals and
crude export loading at Baltic terminals.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.database import classify_vessel, insert_loading_event

logger = logging.getLogger(__name__)

STATIONARY_SPEED_KNOTS = 1.0
MIN_LOADING_HOURS = 12


class LoadingDetector:
    """Detects cargo loading events at tracked terminals."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # Active visits: mmsi -> {terminal, arrival_time, draft_arrival, ...}
        self._active_visits: dict[int, dict[str, Any]] = {}

    def on_vessel_state_change(
        self,
        mmsi: int,
        latitude: float,
        longitude: float,
        sog: float | None,
        draft_m: float | None,
        destination: str | None,
        cog: float | None,
        region: str,
        timestamp: str,
    ) -> dict[str, Any] | None:
        """Process a vessel state update.

        Call this when a vessel's state is updated near a terminal.
        Returns a loading event dict if a departure was detected.
        """
        is_at_terminal = region.startswith("terminal:")
        is_slow = sog is not None and sog < STATIONARY_SPEED_KNOTS

        if is_at_terminal and is_slow:
            # Vessel is at a terminal — track the visit
            if mmsi not in self._active_visits:
                terminal_key = region.replace("terminal:", "")
                self._active_visits[mmsi] = {
                    "terminal_key": terminal_key,
                    "arrival_time": timestamp,
                    "draft_arrival": draft_m,
                    "latest_draft": draft_m,
                    "destination": destination,
                }
                logger.debug(
                    "Vessel MMSI %d arrived at terminal %s (draft: %s)",
                    mmsi, terminal_key, draft_m,
                )
            else:
                # Update latest draft during visit
                if draft_m is not None:
                    self._active_visits[mmsi]["latest_draft"] = draft_m
                if destination:
                    self._active_visits[mmsi]["destination"] = destination

        elif mmsi in self._active_visits and not is_at_terminal:
            # Vessel has left the terminal zone — check for loading event
            visit = self._active_visits.pop(mmsi)
            return self._evaluate_departure(
                mmsi, visit, draft_m, cog, timestamp
            )

        return None

    def check_pending_visits(self) -> list[dict[str, Any]]:
        """Periodically check if any tracked visits should be finalized.

        This handles cases where we stop getting updates for a vessel
        (e.g. it moved out of AIS range).
        """
        events = []
        now = datetime.now(timezone.utc)
        stale_mmsis = []

        for mmsi, visit in self._active_visits.items():
            arrival = datetime.fromisoformat(visit["arrival_time"])
            if hasattr(arrival, 'tzinfo') and arrival.tzinfo is None:
                arrival = arrival.replace(tzinfo=timezone.utc)
            hours = (now - arrival).total_seconds() / 3600

            # If a vessel has been at terminal for > 72 hours without
            # departure, it may have left without us detecting
            if hours > 72:
                stale_mmsis.append(mmsi)

        for mmsi in stale_mmsis:
            visit = self._active_visits.pop(mmsi)
            logger.info(
                "Stale terminal visit for MMSI %d at %s — removing",
                mmsi, visit["terminal_key"],
            )

        return events

    def _evaluate_departure(
        self,
        mmsi: int,
        visit: dict[str, Any],
        departure_draft: float | None,
        cog: float | None,
        timestamp: str,
    ) -> dict[str, Any] | None:
        """Evaluate if a terminal departure constitutes a loading event."""
        arrival_draft = visit.get("draft_arrival")
        latest_draft = visit.get("latest_draft")
        effective_departure_draft = departure_draft or latest_draft

        # Check duration
        arrival_time = visit["arrival_time"]
        try:
            arr_dt = datetime.fromisoformat(arrival_time)
            dep_dt = datetime.fromisoformat(timestamp)
            if arr_dt.tzinfo is None:
                arr_dt = arr_dt.replace(tzinfo=timezone.utc)
            if dep_dt.tzinfo is None:
                dep_dt = dep_dt.replace(tzinfo=timezone.utc)
            hours = (dep_dt - arr_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            hours = 0

        if hours < MIN_LOADING_HOURS:
            logger.debug(
                "MMSI %d: terminal visit too short (%.1fh < %dh), not a loading event",
                mmsi, hours, MIN_LOADING_HOURS,
            )
            return None

        # Determine if draft increased (loading occurred)
        draft_increased = False
        if arrival_draft and effective_departure_draft:
            draft_increased = effective_departure_draft > arrival_draft + 0.5

        # Look up terminal info
        terminal_key = visit["terminal_key"]
        terminal_info = self._get_terminal_info(terminal_key)
        grade = terminal_info.get("grade") if terminal_info else None
        terminal_name = terminal_info.get("name", terminal_key) if terminal_info else terminal_key

        # Estimate cargo
        estimated_cargo_bbl = None
        if draft_increased and arrival_draft and effective_departure_draft:
            vessel = self.conn.execute(
                "SELECT length_m, vessel_class FROM vessels WHERE mmsi = ?",
                (mmsi,),
            ).fetchone()
            if vessel and vessel["length_m"]:
                _, full_load_bbl = classify_vessel(vessel["length_m"])
                if full_load_bbl > 0:
                    # Rough estimate: fraction of capacity loaded
                    max_draft = self.conn.execute(
                        "SELECT max_draft_m FROM vessels WHERE mmsi = ?",
                        (mmsi,),
                    ).fetchone()
                    if max_draft and max_draft["max_draft_m"]:
                        load_frac = (
                            (effective_departure_draft - arrival_draft)
                            / max_draft["max_draft_m"]
                        )
                        estimated_cargo_bbl = full_load_bbl * min(load_frac, 1.0)

        event = {
            "mmsi": mmsi,
            "terminal": terminal_name,
            "grade": grade,
            "arrival_time": arrival_time,
            "departure_time": timestamp,
            "draft_arrival_m": arrival_draft,
            "draft_departure_m": effective_departure_draft,
            "estimated_cargo_bbl": estimated_cargo_bbl,
            "destination_reported": visit.get("destination"),
            "heading_at_departure": cog,
            "draft_increased": draft_increased,
            "duration_hours": hours,
        }

        # Persist to database
        insert_loading_event(
            self.conn,
            mmsi=mmsi,
            terminal=terminal_name,
            grade=grade,
            arrival_time=arrival_time,
            departure_time=timestamp,
            draft_arrival_m=arrival_draft,
            draft_departure_m=effective_departure_draft,
            estimated_cargo_bbl=estimated_cargo_bbl,
            destination_reported=visit.get("destination"),
            heading_at_departure=cog,
        )
        self.conn.commit()

        logger.info(
            "Loading event: MMSI %d at %s (%s) — %.1fh, draft %.1f→%.1f, "
            "cargo ~%.0f kbbl, dest: %s",
            mmsi, terminal_name, grade or "?", hours,
            arrival_draft or 0, effective_departure_draft or 0,
            (estimated_cargo_bbl or 0) / 1000,
            visit.get("destination", "?"),
        )

        return event

    def _get_terminal_info(self, terminal_key: str) -> dict[str, Any] | None:
        """Look up terminal config by key."""
        from src.database import load_terminals
        terminals = load_terminals().get("terminals", {})
        return terminals.get(terminal_key)

    def get_weekly_loading_summary(self) -> list[dict[str, Any]]:
        """Get loading counts by grade for the current week."""
        rows = self.conn.execute(
            """
            SELECT grade, terminal, COUNT(*) as cargo_count,
                   SUM(estimated_cargo_bbl) as total_bbl,
                   AVG(estimated_cargo_bbl) as avg_cargo_bbl
            FROM loading_events
            WHERE arrival_time >= datetime('now', '-7 days')
            GROUP BY grade, terminal
            ORDER BY grade
            """
        ).fetchall()
        return [dict(r) for r in rows]
