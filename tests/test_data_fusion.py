"""Tests for data fusion and deduplication."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from src.database import init_db, is_tanker
from src.ingestion.data_fusion import DataFusionEngine, DEDUP_WINDOW_SECONDS


class TestDataFusion:
    @pytest.fixture
    def engine(self):
        engine = DataFusionEngine(db_path=":memory:")
        yield engine

    def test_position_stored(self, engine):
        """A tanker position should be stored in the database."""
        # First register the vessel as a tanker
        engine.on_metadata(
            mmsi=123456789, ship_type=80, name="TEST TANKER",
            length_m=240.0, beam_m=42.0, source="test",
        )
        engine.on_position(
            mmsi=123456789,
            timestamp=datetime.now(timezone.utc).isoformat(),
            latitude=57.0, longitude=2.0,
            sog=12.5, cog=180.0,
            source="test",
        )
        assert engine._position_count == 1

    def test_non_tanker_filtered(self, engine):
        """Non-tanker vessels should be filtered out."""
        engine.on_metadata(
            mmsi=999999999, ship_type=70, name="CARGO VESSEL",
            source="test",
        )
        engine.on_position(
            mmsi=999999999,
            timestamp=datetime.now(timezone.utc).isoformat(),
            latitude=57.0, longitude=2.0,
            sog=10.0, source="test",
        )
        assert engine._position_count == 0

    def test_dedup_window(self, engine):
        """Duplicate positions within the dedup window should be skipped."""
        engine.on_metadata(
            mmsi=123456789, ship_type=80, name="TEST TANKER",
            length_m=240.0, source="test",
        )
        ts = datetime.now(timezone.utc).isoformat()

        # First position — should be stored
        engine.on_position(
            mmsi=123456789, timestamp=ts,
            latitude=57.0, longitude=2.0, sog=12.0,
            source="aisstream",
        )
        assert engine._position_count == 1

        # Second position within dedup window — should be skipped
        engine.on_position(
            mmsi=123456789, timestamp=ts,
            latitude=57.001, longitude=2.001, sog=12.1,
            source="barentswatch",
        )
        assert engine._position_count == 1

    def test_metadata_upsert(self, engine):
        """Metadata should be upserted correctly."""
        engine.on_metadata(
            mmsi=123456789, ship_type=80, name="TANKER V1",
            imo=9000001, length_m=240.0, source="aisstream",
        )
        assert engine._metadata_count == 1

        # Update with new name from different source
        engine.on_metadata(
            mmsi=123456789, ship_type=80, name="TANKER V2",
            source="barentswatch",
        )
        assert engine._metadata_count == 2

        # Verify latest name
        row = engine.conn.execute(
            "SELECT name FROM vessels WHERE mmsi = 123456789"
        ).fetchone()
        assert row["name"] == "TANKER V2"

    def test_stats(self, engine):
        stats = engine.stats
        assert "positions_stored" in stats
        assert "metadata_updates" in stats
        assert "vessels_in_cache" in stats
