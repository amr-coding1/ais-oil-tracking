"""Shared test fixtures."""

from __future__ import annotations

import pytest

from src.database import init_db, upsert_vessel, insert_position, upsert_vessel_state


@pytest.fixture
def db():
    """Create an in-memory database with schema initialised."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def db_with_tankers(db):
    """Database seeded with sample tanker vessels."""
    vessels = [
        {"mmsi": 123456789, "imo": 9000001, "name": "TEST AFRAMAX",
         "ship_type": 80, "length_m": 240.0, "beam_m": 42.0},
        {"mmsi": 234567890, "imo": 9000002, "name": "TEST SUEZMAX",
         "ship_type": 80, "length_m": 270.0, "beam_m": 48.0},
        {"mmsi": 345678901, "imo": 9000003, "name": "TEST VLCC",
         "ship_type": 80, "length_m": 330.0, "beam_m": 60.0},
        {"mmsi": 456789012, "imo": 9000004, "name": "TEST CARGO",
         "ship_type": 70, "length_m": 200.0, "beam_m": 30.0},
    ]
    for v in vessels:
        upsert_vessel(db, **v)

    # Set max_draft for testing laden/ballast
    db.execute("UPDATE vessels SET max_draft_m = 14.0 WHERE mmsi = 123456789")
    db.execute("UPDATE vessels SET max_draft_m = 16.0 WHERE mmsi = 234567890")
    db.execute("UPDATE vessels SET max_draft_m = 21.0 WHERE mmsi = 345678901")
    db.commit()

    return db
