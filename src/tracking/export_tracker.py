"""Baltic crude export flow tracker.

Monitors crude oil exports from Baltic terminals:
    - Primorsk, Russia (Urals grade)
    - Ust-Luga, Russia (Urals grade)
    - Gdansk/Naftoport, Poland (CPC Blend)
    - Butinge, Lithuania

Tracks:
    - Laden departures and estimated volumes
    - Destination analysis (through Danish Straits vs intra-Baltic)
    - Shadow fleet indicators (vessel age, flag state, AIS gaps)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

BALTIC_TERMINALS = {"primorsk", "ust_luga", "gdansk", "butinge"}

# Danish Straits approximate longitude threshold — vessels heading west
# past this point are exiting the Baltic to the Atlantic market
DANISH_STRAITS_LON = 12.0


class ExportTracker:
    """Tracks crude oil export departures from Baltic terminals."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_weekly_exports(self, weeks: int = 1) -> list[dict[str, Any]]:
        """Get export departures from Baltic terminals in the last N weeks."""
        rows = self.conn.execute(
            """
            SELECT le.*, v.name, v.vessel_class, v.imo
            FROM loading_events le
            JOIN vessels v ON le.mmsi = v.mmsi
            WHERE le.arrival_time >= datetime('now', ?)
              AND le.terminal IN ('Primorsk', 'Ust-Luga', 'Gdansk/Naftoport', 'Butinge')
            ORDER BY le.departure_time DESC
            """,
            (f"-{weeks * 7} days",),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_export_volume_by_terminal(self, days: int = 7) -> dict[str, dict[str, Any]]:
        """Get export volumes grouped by terminal."""
        rows = self.conn.execute(
            """
            SELECT terminal,
                   COUNT(*) as departures,
                   COALESCE(SUM(estimated_cargo_bbl), 0) as total_bbl,
                   GROUP_CONCAT(DISTINCT destination_reported) as destinations
            FROM loading_events
            WHERE arrival_time >= datetime('now', ?)
              AND terminal IN ('Primorsk', 'Ust-Luga', 'Gdansk/Naftoport', 'Butinge')
            GROUP BY terminal
            """,
            (f"-{days} days",),
        ).fetchall()
        return {
            row["terminal"]: {
                "departures": row["departures"],
                "total_bbl": row["total_bbl"],
                "destinations": (row["destinations"] or "").split(","),
            }
            for row in rows
        }

    def get_destination_breakdown(self, days: int = 7) -> dict[str, int]:
        """Classify Baltic export destinations.

        Uses heading_at_departure and reported destination to classify:
            - 'Atlantic' (heading west through Danish Straits)
            - 'Intra-Baltic' (staying in Baltic)
            - 'Unknown'
        """
        rows = self.conn.execute(
            """
            SELECT heading_at_departure, destination_reported
            FROM loading_events
            WHERE arrival_time >= datetime('now', ?)
              AND terminal IN ('Primorsk', 'Ust-Luga', 'Gdansk/Naftoport', 'Butinge')
            """,
            (f"-{days} days",),
        ).fetchall()

        breakdown: dict[str, int] = {"Atlantic": 0, "Intra-Baltic": 0, "Unknown": 0}

        for row in rows:
            heading = row["heading_at_departure"]
            dest = (row["destination_reported"] or "").upper()

            # Classify based on heading and destination
            if self._is_atlantic_dest(heading, dest):
                breakdown["Atlantic"] += 1
            elif self._is_baltic_dest(heading, dest):
                breakdown["Intra-Baltic"] += 1
            else:
                breakdown["Unknown"] += 1

        return breakdown

    def get_shadow_fleet_indicators(self, days: int = 30) -> dict[str, Any]:
        """Analyse fleet characteristics at Russian Baltic terminals.

        Tracks vessel age and flag state distribution — indicators of
        the "shadow fleet" used to circumvent sanctions.
        """
        rows = self.conn.execute(
            """
            SELECT DISTINCT le.mmsi, v.name, v.imo, v.vessel_class
            FROM loading_events le
            JOIN vessels v ON le.mmsi = v.mmsi
            WHERE le.arrival_time >= datetime('now', ?)
              AND le.terminal IN ('Primorsk', 'Ust-Luga')
            """,
            (f"-{days} days",),
        ).fetchall()

        return {
            "unique_vessels": len(rows),
            "vessels": [dict(r) for r in rows],
        }

    def get_ais_gap_vessels(self, hours_threshold: int = 24) -> list[dict[str, Any]]:
        """Find vessels that loaded at Russian ports and then went dark.

        AIS gaps after loading at sanctioned terminals are noteworthy
        as they may indicate sanctions evasion.
        """
        rows = self.conn.execute(
            """
            SELECT le.mmsi, v.name, le.terminal, le.departure_time,
                   vs.last_position_time,
                   ROUND(
                       (julianday('now') - julianday(vs.last_position_time)) * 24
                   ) as hours_since_last
            FROM loading_events le
            JOIN vessels v ON le.mmsi = v.mmsi
            JOIN vessel_state vs ON le.mmsi = vs.mmsi
            WHERE le.terminal IN ('Primorsk', 'Ust-Luga')
              AND le.departure_time IS NOT NULL
              AND (julianday('now') - julianday(vs.last_position_time)) * 24 > ?
            ORDER BY hours_since_last DESC
            """,
            (hours_threshold,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _is_atlantic_dest(heading: float | None, dest: str) -> bool:
        """Classify as Atlantic-bound based on heading/destination."""
        atlantic_keywords = [
            "ROTTERDAM", "ANTWERP", "ARA", "EUROPOORT", "HOUSTON",
            "INDIA", "CHINA", "SUEZ", "MED", "AUGUSTA", "TRIESTE",
            "SINGAPORE", "FUJAIRAH", "TURKEY", "ISKENDERUN",
        ]
        if any(kw in dest for kw in atlantic_keywords):
            return True
        # Heading roughly west/southwest (200-340 degrees) suggests
        # transit through Danish Straits
        if heading is not None and 200 <= heading <= 340:
            return True
        return False

    @staticmethod
    def _is_baltic_dest(heading: float | None, dest: str) -> bool:
        """Classify as intra-Baltic."""
        baltic_keywords = [
            "GDANSK", "GOTHENBURG", "KALININGRAD", "KLAIPEDA",
            "VENTSPILS", "TALLINN", "HELSINKI", "STOCKHOLM",
            "PORVOO", "SKOELDVIK", "NYNASHAMN",
        ]
        if any(kw in dest for kw in baltic_keywords):
            return True
        return False
