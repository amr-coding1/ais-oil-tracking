"""Tests for vessel tracker geofencing and status determination."""

from __future__ import annotations

import pytest

from src.tracking.vessel_tracker import VesselTracker, _haversine_nm


class TestHaversine:
    def test_same_point(self):
        assert _haversine_nm(57.0, 2.0, 57.0, 2.0) == pytest.approx(0.0, abs=0.01)

    def test_known_distance(self):
        # Roughly 60nm = 1 degree of latitude
        dist = _haversine_nm(57.0, 2.0, 58.0, 2.0)
        assert 59.0 < dist < 61.0


class TestVesselTracker:
    @pytest.fixture
    def tracker(self, db):
        return VesselTracker(db)

    def test_find_sullom_voe(self, tracker):
        """Position near Sullom Voe should match the terminal."""
        terminal = tracker.find_terminal(60.45, -1.30)
        assert terminal is not None
        assert terminal["name"] == "Sullom Voe"

    def test_find_primorsk(self, tracker):
        terminal = tracker.find_terminal(60.35, 29.00)
        assert terminal is not None
        assert terminal["name"] == "Primorsk"

    def test_no_terminal_open_sea(self, tracker):
        terminal = tracker.find_terminal(55.0, 0.0)
        assert terminal is None

    def test_find_rotterdam_anchorage(self, tracker):
        zone = tracker.find_anchorage_zone(52.0, 4.0)
        assert zone is not None
        assert zone["region"] == "ARA"

    def test_no_anchorage_open_sea(self, tracker):
        zone = tracker.find_anchorage_zone(58.0, 0.0)
        assert zone is None

    def test_assign_region_terminal(self, tracker):
        region = tracker.assign_region(60.45, -1.30)
        assert region.startswith("terminal:")

    def test_assign_region_anchorage(self, tracker):
        region = tracker.assign_region(52.0, 4.0)
        assert region.startswith("anchorage:")

    def test_assign_region_broad(self, tracker):
        region = tracker.assign_region(55.0, 0.0)
        assert "north_sea" in region

    def test_status_at_terminal(self, tracker):
        status = tracker.determine_status(
            sog=0.3, draft_m=12.0, max_draft=14.0,
            latitude=60.45, longitude=-1.30,
        )
        assert status == "at_terminal"

    def test_status_moving_laden(self, tracker):
        status = tracker.determine_status(
            sog=12.0, draft_m=12.0, max_draft=14.0,
            latitude=55.0, longitude=0.0,
        )
        assert status == "moving_laden"

    def test_status_moving_ballast(self, tracker):
        status = tracker.determine_status(
            sog=12.0, draft_m=5.0, max_draft=14.0,
            latitude=55.0, longitude=0.0,
        )
        assert status == "moving_ballast"

    def test_status_at_anchor(self, tracker):
        status = tracker.determine_status(
            sog=0.3, draft_m=12.0, max_draft=14.0,
            latitude=52.0, longitude=4.0,  # Rotterdam anchorage
        )
        assert status == "at_anchor"
