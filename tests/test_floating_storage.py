"""Tests for floating storage detection algorithm."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.database import (
    classify_vessel,
    estimate_laden_fraction,
    insert_position,
    is_tanker,
    upsert_vessel_state,
)
from src.tracking.floating_storage import FloatingStorageDetector


class TestVesselClassification:
    def test_vlcc(self):
        cls, bbl = classify_vessel(335.0)
        assert cls == "VLCC"
        assert bbl == 2_000_000

    def test_suezmax(self):
        cls, bbl = classify_vessel(270.0)
        assert cls == "Suezmax"
        assert bbl == 1_000_000

    def test_aframax(self):
        cls, bbl = classify_vessel(240.0)
        assert cls == "Aframax"
        assert bbl == 600_000

    def test_mr_tanker(self):
        cls, bbl = classify_vessel(190.0)
        assert cls == "MR"
        assert bbl == 350_000

    def test_small_tanker(self):
        cls, bbl = classify_vessel(120.0)
        assert cls == "Small"
        assert bbl == 140_000

    def test_unknown_none(self):
        cls, bbl = classify_vessel(None)
        assert cls == "Unknown"
        assert bbl == 0.0

    def test_unknown_zero(self):
        cls, bbl = classify_vessel(0)
        assert cls == "Unknown"
        assert bbl == 0.0

    def test_very_large(self):
        cls, _ = classify_vessel(420.0)
        assert cls == "VLCC"


class TestTankerDetection:
    def test_tanker_type_80(self):
        assert is_tanker(80) is True

    def test_tanker_type_89(self):
        assert is_tanker(89) is True

    def test_cargo_not_tanker(self):
        assert is_tanker(70) is False

    def test_none_not_tanker(self):
        assert is_tanker(None) is False


class TestLadenEstimation:
    def test_laden(self):
        status, frac = estimate_laden_fraction(12.0, 14.0)
        assert status == "laden"
        assert frac == pytest.approx(12.0 / 14.0)

    def test_ballast(self):
        status, frac = estimate_laden_fraction(5.0, 14.0)
        assert status == "ballast"
        assert frac == pytest.approx(5.0 / 14.0)

    def test_uncertain(self):
        status, frac = estimate_laden_fraction(8.5, 14.0)
        assert status == "uncertain"

    def test_no_draft(self):
        status, frac = estimate_laden_fraction(None, 14.0)
        assert status == "uncertain"

    def test_no_max_draft(self):
        status, frac = estimate_laden_fraction(12.0, None)
        assert status == "uncertain"


class TestFloatingStorageDetector:
    def test_qualifies_as_floating_storage(self, db_with_tankers):
        """A stationary, laden vessel for >7 days should trigger."""
        mmsi = 123456789
        # Set vessel as stationary and laden for 10 days
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        upsert_vessel_state(
            db_with_tankers, mmsi,
            latitude=52.0, longitude=3.5,  # ARA area
            sog=0.3, draft_m=12.0,  # 12/14 = 86% laden
            destination="FOR ORDERS",
            region="anchorage:rotterdam_europoort",
            status="at_anchor",
            timestamp=ten_days_ago,
        )
        db_with_tankers.commit()

        detector = FloatingStorageDetector(db_with_tankers)
        result = detector.evaluate()
        assert result["new_storage_events"] >= 1

    def test_short_duration_excluded(self, db_with_tankers):
        """A vessel stationary for only 3 days should NOT trigger."""
        mmsi = 123456789
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        upsert_vessel_state(
            db_with_tankers, mmsi,
            latitude=52.0, longitude=3.5,
            sog=0.3, draft_m=12.0,
            destination="FOR ORDERS",
            region="anchorage:rotterdam_europoort",
            status="at_anchor",
            timestamp=three_days_ago,
        )
        db_with_tankers.commit()

        detector = FloatingStorageDetector(db_with_tankers)
        result = detector.evaluate()
        assert result["new_storage_events"] == 0

    def test_ballast_excluded(self, db_with_tankers):
        """A stationary ballast vessel should NOT trigger."""
        mmsi = 123456789
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        upsert_vessel_state(
            db_with_tankers, mmsi,
            latitude=52.0, longitude=3.5,
            sog=0.3, draft_m=5.0,  # 5/14 = 36% -> ballast
            destination="FOR ORDERS",
            region="anchorage:rotterdam_europoort",
            status="at_anchor",
            timestamp=ten_days_ago,
        )
        db_with_tankers.commit()

        detector = FloatingStorageDetector(db_with_tankers)
        result = detector.evaluate()
        assert result["new_storage_events"] == 0

    def test_at_terminal_excluded(self, db_with_tankers):
        """A vessel at a terminal (not at anchorage) should NOT trigger."""
        mmsi = 123456789
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        upsert_vessel_state(
            db_with_tankers, mmsi,
            latitude=60.45, longitude=-1.30,
            sog=0.2, draft_m=12.0,
            destination="SULLOM VOE",
            region="terminal:sullom_voe",
            status="at_terminal",
            timestamp=ten_days_ago,
        )
        db_with_tankers.commit()

        detector = FloatingStorageDetector(db_with_tankers)
        result = detector.evaluate()
        assert result["new_storage_events"] == 0

    def test_storage_region_classification(self):
        assert FloatingStorageDetector._storage_region(52.0, 4.0) == "ARA"
        assert FloatingStorageDetector._storage_region(57.0, 2.0) == "North Sea"
        assert FloatingStorageDetector._storage_region(59.0, 25.0) == "Baltic"
