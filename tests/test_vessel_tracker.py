"""Tests for vessel tracker geofencing and status determination."""

from __future__ import annotations

import pytest

from src.database import estimate_laden_fraction
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

    def test_status_ballast_with_class_fallback(self, tracker):
        """When max_draft is None, class typical should be used."""
        status = tracker.determine_status(
            sog=12.0, draft_m=6.0, max_draft=None,
            latitude=55.0, longitude=0.0,
            vessel_class="Aframax",  # typical_max_draft_m = 15.0 → 6/15 = 0.4 < 0.5
        )
        assert status == "moving_ballast"

    def test_status_laden_with_class_fallback(self, tracker):
        """When max_draft equals current (bootstrap), class typical should be used."""
        status = tracker.determine_status(
            sog=12.0, draft_m=12.0, max_draft=12.0,
            latitude=55.0, longitude=0.0,
            vessel_class="Aframax",  # typical_max_draft_m = 15.0 → 12/15 = 0.8 >= 0.7
        )
        assert status == "moving_laden"

    def test_status_uncertain_no_draft(self, tracker):
        """No draft data at all should give uncertain."""
        status = tracker.determine_status(
            sog=12.0, draft_m=None, max_draft=None,
            latitude=55.0, longitude=0.0,
            vessel_class="Aframax",
        )
        assert status == "moving_uncertain"


class TestEstimateLadenFraction:
    def test_with_observed_max_draft(self):
        status, frac = estimate_laden_fraction(12.0, 14.0)
        assert status == "laden"

    def test_ballast_with_observed_max_draft(self):
        status, frac = estimate_laden_fraction(5.0, 14.0)
        assert status == "ballast"

    def test_class_fallback_when_no_max_draft(self):
        status, frac = estimate_laden_fraction(6.0, None, vessel_class="Aframax")
        assert status == "ballast"
        assert 0.39 < frac < 0.41  # 6/15 = 0.4

    def test_class_fallback_when_bootstrap(self):
        """max_draft == current_draft means only one observation."""
        status, frac = estimate_laden_fraction(6.0, 6.0, vessel_class="Aframax")
        assert status == "ballast"  # 6/15 = 0.4

    def test_no_fallback_when_max_draft_exceeds_current(self):
        """When max_draft > current_draft, it's a real observation — use it."""
        status, frac = estimate_laden_fraction(6.0, 14.0, vessel_class="Aframax")
        assert status == "ballast"
        assert 0.42 < frac < 0.44  # 6/14 not 6/15

    def test_uncertain_with_no_info(self):
        status, frac = estimate_laden_fraction(None, None)
        assert status == "uncertain"
