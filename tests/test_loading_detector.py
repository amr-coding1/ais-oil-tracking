"""Tests for terminal loading event detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.database import upsert_vessel, upsert_vessel_state
from src.tracking.loading_detector import LoadingDetector


class TestLoadingDetector:
    def test_loading_event_detected(self, db_with_tankers):
        """Vessel arrives at terminal, stays >12h, departs with higher draft."""
        detector = LoadingDetector(db_with_tankers)
        mmsi = 123456789
        now = datetime.now(timezone.utc)

        # Step 1: Vessel arrives at Sullom Voe
        event = detector.on_vessel_state_change(
            mmsi=mmsi,
            latitude=60.45, longitude=-1.30,
            sog=0.3, draft_m=6.0,
            destination="SULLOM VOE",
            cog=180.0,
            region="terminal:sullom_voe",
            timestamp=(now - timedelta(hours=24)).isoformat(),
        )
        assert event is None  # No event yet — still at terminal

        # Step 2: Vessel departs terminal zone
        event = detector.on_vessel_state_change(
            mmsi=mmsi,
            latitude=60.50, longitude=-1.20,
            sog=12.0, draft_m=12.0,
            destination="ROTTERDAM",
            cog=150.0,
            region="north_sea_north",
            timestamp=now.isoformat(),
        )
        assert event is not None
        assert event["terminal"] == "Sullom Voe"
        assert event["grade"] == "Brent"
        assert event["draft_increased"] is True
        assert event["duration_hours"] >= 23.0

    def test_short_visit_not_loading(self, db_with_tankers):
        """Visit < 12 hours should not count as loading."""
        detector = LoadingDetector(db_with_tankers)
        mmsi = 123456789
        now = datetime.now(timezone.utc)

        # Arrive
        detector.on_vessel_state_change(
            mmsi=mmsi, latitude=60.45, longitude=-1.30,
            sog=0.3, draft_m=6.0, destination="SULLOM VOE",
            cog=180.0, region="terminal:sullom_voe",
            timestamp=(now - timedelta(hours=6)).isoformat(),
        )

        # Depart after only 6 hours
        event = detector.on_vessel_state_change(
            mmsi=mmsi, latitude=60.50, longitude=-1.20,
            sog=12.0, draft_m=7.0, destination="ROTTERDAM",
            cog=150.0, region="north_sea_north",
            timestamp=now.isoformat(),
        )
        assert event is None

    def test_no_draft_increase_still_recorded(self, db_with_tankers):
        """Event recorded even without draft increase (could be discharge)."""
        detector = LoadingDetector(db_with_tankers)
        mmsi = 123456789
        now = datetime.now(timezone.utc)

        # Arrive laden
        detector.on_vessel_state_change(
            mmsi=mmsi, latitude=52.0, longitude=4.0,
            sog=0.3, draft_m=12.0, destination="ROTTERDAM",
            cog=90.0, region="terminal:sullom_voe",
            timestamp=(now - timedelta(hours=18)).isoformat(),
        )

        # Depart with lower draft (discharge)
        event = detector.on_vessel_state_change(
            mmsi=mmsi, latitude=52.1, longitude=4.1,
            sog=10.0, draft_m=6.0, destination="SULLOM VOE",
            cog=330.0, region="north_sea_south",
            timestamp=now.isoformat(),
        )
        assert event is not None
        assert event["draft_increased"] is False

    def test_weekly_summary(self, db_with_tankers):
        """Weekly summary returns correct structure."""
        detector = LoadingDetector(db_with_tankers)
        summary = detector.get_weekly_loading_summary()
        assert isinstance(summary, list)
