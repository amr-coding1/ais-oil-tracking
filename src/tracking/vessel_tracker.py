"""Core vessel tracking: region assignment, status determination, geofencing."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from pathlib import Path
from typing import Any

from src.database import estimate_laden_fraction

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"

# Nautical mile in degrees of latitude (approximate)
NM_TO_DEG_LAT = 1.0 / 60.0


def _nm_to_deg_lon(latitude: float) -> float:
    """Convert 1 nautical mile to degrees of longitude at a given latitude."""
    return 1.0 / (60.0 * math.cos(math.radians(latitude)))


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in nautical miles between two points."""
    R_NM = 3440.065  # Earth radius in nautical miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 2 * R_NM * math.asin(math.sqrt(a))


class VesselTracker:
    """Assigns regions and determines vessel status using geofencing."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.terminals = self._load_json("terminals.json").get("terminals", {})
        self.anchorage_zones = self._load_json("anchorage_zones.json").get(
            "anchorage_zones", {}
        )

    @staticmethod
    def _load_json(filename: str) -> dict[str, Any]:
        path = CONFIG_DIR / filename
        if not path.exists():
            logger.warning("Config file not found: %s", path)
            return {}
        with open(path) as f:
            return json.load(f)

    def find_terminal(
        self, latitude: float, longitude: float
    ) -> dict[str, Any] | None:
        """Return the terminal dict if the position is inside any terminal geofence."""
        for key, terminal in self.terminals.items():
            dist = _haversine_nm(
                latitude, longitude,
                terminal["latitude"], terminal["longitude"],
            )
            if dist <= terminal["radius_nm"]:
                return {**terminal, "key": key}
        return None

    def find_anchorage_zone(
        self, latitude: float, longitude: float
    ) -> dict[str, Any] | None:
        """Return the anchorage zone dict if the position is inside any zone."""
        for key, zone in self.anchorage_zones.items():
            b = zone["bounds"]
            if (b["lat_min"] <= latitude <= b["lat_max"]
                    and b["lon_min"] <= longitude <= b["lon_max"]):
                return {**zone, "key": key}
        return None

    def assign_region(self, latitude: float, longitude: float) -> str:
        """Assign a region label based on position."""
        terminal = self.find_terminal(latitude, longitude)
        if terminal:
            return f"terminal:{terminal['key']}"

        zone = self.find_anchorage_zone(latitude, longitude)
        if zone:
            return f"anchorage:{zone['key']}"

        # Broad regional classification
        if latitude > 58.0 and longitude < 5.0:
            return "north_sea_north"
        if latitude > 54.0 and longitude < 5.0:
            return "north_sea_south"
        if longitude > 18.0:
            return "baltic_east"
        if longitude > 9.0:
            return "baltic_west"
        if latitude < 52.0 and longitude < 5.0:
            return "english_channel"
        return "north_sea"

    def determine_status(
        self,
        sog: float | None,
        draft_m: float | None,
        max_draft: float | None,
        latitude: float,
        longitude: float,
    ) -> str:
        """Determine the current operational status of a vessel.

        Returns one of:
            'at_terminal', 'at_anchor', 'moving_laden', 'moving_ballast',
            'moving_uncertain', 'stationary'
        """
        terminal = self.find_terminal(latitude, longitude)
        is_slow = sog is not None and sog < 1.0
        laden_status, _ = estimate_laden_fraction(draft_m, max_draft)

        if terminal and is_slow:
            return "at_terminal"

        if is_slow:
            zone = self.find_anchorage_zone(latitude, longitude)
            if zone:
                return "at_anchor"
            return "stationary"

        if laden_status == "laden":
            return "moving_laden"
        elif laden_status == "ballast":
            return "moving_ballast"
        return "moving_uncertain"
