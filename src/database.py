"""SQLite database operations for vessel tracking data."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

import os as _os

# Allow overriding DB location via env var (useful for cloud deployment with persistent volumes)
_db_env = _os.getenv("DB_PATH")
DB_PATH = Path(_db_env) if _db_env else Path(__file__).resolve().parent.parent / "data" / "vessels.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vessels (
    mmsi INTEGER PRIMARY KEY,
    imo INTEGER,
    name TEXT,
    callsign TEXT,
    ship_type INTEGER,
    length_m REAL,
    beam_m REAL,
    dwt_estimate REAL,
    vessel_class TEXT,
    max_draft_m REAL,
    first_seen TIMESTAMP,
    last_updated TIMESTAMP
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi INTEGER NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    sog REAL,
    cog REAL,
    heading INTEGER,
    draft_m REAL,
    nav_status INTEGER,
    destination TEXT,
    source TEXT,
    region TEXT,
    FOREIGN KEY (mmsi) REFERENCES vessels(mmsi)
);

CREATE INDEX IF NOT EXISTS idx_positions_mmsi_time
    ON positions(mmsi, timestamp);
CREATE INDEX IF NOT EXISTS idx_positions_region_time
    ON positions(region, timestamp);
CREATE INDEX IF NOT EXISTS idx_positions_timestamp
    ON positions(timestamp);

CREATE TABLE IF NOT EXISTS vessel_state (
    mmsi INTEGER PRIMARY KEY,
    latitude REAL,
    longitude REAL,
    sog REAL,
    draft_m REAL,
    destination TEXT,
    region TEXT,
    status TEXT,
    status_since TIMESTAMP,
    last_position_time TIMESTAMP,
    FOREIGN KEY (mmsi) REFERENCES vessels(mmsi)
);

CREATE TABLE IF NOT EXISTS floating_storage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi INTEGER NOT NULL,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    region TEXT,
    avg_latitude REAL,
    avg_longitude REAL,
    estimated_cargo_bbl REAL,
    draft_at_start REAL,
    FOREIGN KEY (mmsi) REFERENCES vessels(mmsi)
);

CREATE TABLE IF NOT EXISTS loading_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi INTEGER NOT NULL,
    terminal TEXT NOT NULL,
    grade TEXT,
    arrival_time TIMESTAMP,
    departure_time TIMESTAMP,
    draft_arrival_m REAL,
    draft_departure_m REAL,
    estimated_cargo_bbl REAL,
    destination_reported TEXT,
    heading_at_departure REAL,
    FOREIGN KEY (mmsi) REFERENCES vessels(mmsi)
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    date DATE PRIMARY KEY,
    total_vessels_tracked INTEGER,
    floating_storage_ara_bbl REAL,
    floating_storage_northsea_bbl REAL,
    floating_storage_baltic_bbl REAL,
    brent_loadings_count INTEGER,
    forties_loadings_count INTEGER,
    oseberg_loadings_count INTEGER,
    ekofisk_loadings_count INTEGER,
    troll_loadings_count INTEGER,
    baltic_export_departures INTEGER,
    baltic_export_volume_bbl REAL,
    avg_ara_dwell_days REAL,
    brent_m1 REAL,
    brent_m1_m2_spread REAL,
    brent_m1_m6_spread REAL
);
"""

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_config(name: str) -> dict[str, Any]:
    """Load a JSON config file from the config/ directory."""
    path = _CONFIG_DIR / name
    with open(path) as f:
        return json.load(f)


def load_vessel_classes() -> dict[str, Any]:
    return load_config("vessel_classes.json")


def load_terminals() -> dict[str, Any]:
    return load_config("terminals.json")


def load_anchorage_zones() -> dict[str, Any]:
    return load_config("anchorage_zones.json")


# ---------------------------------------------------------------------------
# Vessel classification helpers
# ---------------------------------------------------------------------------

_VC: dict[str, Any] | None = None


def _get_vc() -> dict[str, Any]:
    global _VC
    if _VC is None:
        _VC = load_vessel_classes()
    return _VC


def classify_vessel(length_m: float | None) -> tuple[str, float]:
    """Return (vessel_class, full_load_bbl) from vessel length."""
    vc = _get_vc()
    if length_m is None or length_m <= 0:
        return "Unknown", 0.0
    for cls_name in ["VLCC", "Suezmax", "Aframax", "MR", "Small"]:
        cls = vc["vessel_classes"][cls_name]
        if cls["min_length_m"] <= length_m < cls["max_length_m"]:
            return cls_name, cls["full_load_bbl"]
    if length_m >= 400:
        return "VLCC", vc["vessel_classes"]["VLCC"]["full_load_bbl"]
    return "Unknown", 0.0


def is_tanker(ship_type: int | None) -> bool:
    """Check if AIS ship type code indicates a tanker (80-89)."""
    if ship_type is None:
        return False
    return 80 <= ship_type <= 89


def _get_typical_max_draft(vessel_class: str | None) -> float | None:
    """Return the typical max draft for a vessel class, or None."""
    if not vessel_class:
        return None
    vc = _get_vc()
    cls = vc["vessel_classes"].get(vessel_class)
    if cls:
        return cls.get("typical_max_draft_m")
    return None


def estimate_laden_fraction(
    current_draft: float | None,
    max_draft: float | None,
    vessel_class: str | None = None,
) -> tuple[str, float]:
    """Return (laden_status, load_fraction) based on draft ratio.

    When max_draft is unavailable or unreliable (equal to current_draft,
    meaning the vessel hasn't been observed long enough), falls back to
    the typical max draft for the vessel class.

    laden_status: 'laden', 'ballast', or 'uncertain'
    """
    vc = _get_vc()
    if current_draft is None:
        return "uncertain", 0.0

    # Use observed max_draft, but fall back to class typical if it's missing
    # or if it equals current_draft (bootstrap problem — only one observation)
    effective_max = max_draft
    if effective_max is None or effective_max <= 0:
        effective_max = _get_typical_max_draft(vessel_class)
    elif effective_max == current_draft:
        # Only one draft value observed — prefer class typical if available
        typical = _get_typical_max_draft(vessel_class)
        if typical and typical > current_draft:
            effective_max = typical

    if effective_max is None or effective_max <= 0:
        return "uncertain", 0.0

    fraction = current_draft / effective_max
    if fraction >= vc["laden_threshold"]:
        return "laden", fraction
    elif fraction < vc["ballast_threshold"]:
        return "ballast", fraction
    return "uncertain", fraction


# ---------------------------------------------------------------------------
# Database connection & schema
# ---------------------------------------------------------------------------


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode enabled.

    Uses check_same_thread=False because the application serialises all
    writes behind a threading.Lock in the DataFusionEngine.
    """
    if db_path and str(db_path) == ":memory:":
        conn = sqlite3.connect(":memory:", timeout=30)
    else:
        path = Path(db_path) if db_path else DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Initialise the database schema and return a connection."""
    conn = get_connection(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    logger.info("Database initialised at %s", db_path or DB_PATH)
    return conn


# ---------------------------------------------------------------------------
# Vessel upsert
# ---------------------------------------------------------------------------


def upsert_vessel(
    conn: sqlite3.Connection,
    mmsi: int,
    *,
    imo: int | None = None,
    name: str | None = None,
    callsign: str | None = None,
    ship_type: int | None = None,
    length_m: float | None = None,
    beam_m: float | None = None,
) -> None:
    """Insert or update vessel metadata."""
    now = datetime.now(timezone.utc).isoformat()
    vessel_class, _ = classify_vessel(length_m)
    conn.execute(
        """
        INSERT INTO vessels (mmsi, imo, name, callsign, ship_type, length_m,
                             beam_m, vessel_class, first_seen, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mmsi) DO UPDATE SET
            imo = COALESCE(excluded.imo, vessels.imo),
            name = COALESCE(excluded.name, vessels.name),
            callsign = COALESCE(excluded.callsign, vessels.callsign),
            ship_type = COALESCE(excluded.ship_type, vessels.ship_type),
            length_m = COALESCE(excluded.length_m, vessels.length_m),
            beam_m = COALESCE(excluded.beam_m, vessels.beam_m),
            vessel_class = CASE
                WHEN excluded.length_m IS NOT NULL THEN excluded.vessel_class
                ELSE vessels.vessel_class
            END,
            last_updated = excluded.last_updated
        """,
        (mmsi, imo, name, callsign, ship_type, length_m, beam_m,
         vessel_class, now, now),
    )


# ---------------------------------------------------------------------------
# Position insert
# ---------------------------------------------------------------------------


def insert_position(
    conn: sqlite3.Connection,
    mmsi: int,
    timestamp: str,
    latitude: float,
    longitude: float,
    *,
    sog: float | None = None,
    cog: float | None = None,
    heading: int | None = None,
    draft_m: float | None = None,
    nav_status: int | None = None,
    destination: str | None = None,
    source: str | None = None,
    region: str | None = None,
) -> None:
    """Insert a position record and update vessel state."""
    conn.execute(
        """
        INSERT INTO positions (mmsi, timestamp, latitude, longitude, sog, cog,
                               heading, draft_m, nav_status, destination,
                               source, region)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (mmsi, timestamp, latitude, longitude, sog, cog, heading, draft_m,
         nav_status, destination, source, region),
    )
    # Update max_draft_m on the vessel record
    if draft_m is not None:
        conn.execute(
            """
            UPDATE vessels
            SET max_draft_m = MAX(COALESCE(max_draft_m, 0), ?)
            WHERE mmsi = ?
            """,
            (draft_m, mmsi),
        )


# ---------------------------------------------------------------------------
# Vessel state upsert
# ---------------------------------------------------------------------------


def upsert_vessel_state(
    conn: sqlite3.Connection,
    mmsi: int,
    latitude: float,
    longitude: float,
    sog: float | None,
    draft_m: float | None,
    destination: str | None,
    region: str | None,
    status: str,
    timestamp: str,
) -> None:
    """Update the current state for a vessel."""
    conn.execute(
        """
        INSERT INTO vessel_state (mmsi, latitude, longitude, sog, draft_m,
                                  destination, region, status, status_since,
                                  last_position_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mmsi) DO UPDATE SET
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            sog = excluded.sog,
            draft_m = excluded.draft_m,
            destination = excluded.destination,
            region = excluded.region,
            status = excluded.status,
            status_since = CASE
                WHEN vessel_state.status != excluded.status
                THEN excluded.status_since
                ELSE vessel_state.status_since
            END,
            last_position_time = excluded.last_position_time
        """,
        (mmsi, latitude, longitude, sog, draft_m, destination, region,
         status, timestamp, timestamp),
    )


# ---------------------------------------------------------------------------
# Floating storage events
# ---------------------------------------------------------------------------


def start_floating_storage_event(
    conn: sqlite3.Connection,
    mmsi: int,
    start_time: str,
    region: str | None,
    latitude: float,
    longitude: float,
    estimated_cargo_bbl: float,
    draft: float | None,
) -> int:
    """Create a new floating storage event. Returns the event id."""
    cursor = conn.execute(
        """
        INSERT INTO floating_storage_events
            (mmsi, start_time, region, avg_latitude, avg_longitude,
             estimated_cargo_bbl, draft_at_start)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (mmsi, start_time, region, latitude, longitude,
         estimated_cargo_bbl, draft),
    )
    return cursor.lastrowid  # type: ignore[return-value]


def end_floating_storage_event(
    conn: sqlite3.Connection, event_id: int, end_time: str
) -> None:
    """Mark a floating storage event as ended."""
    conn.execute(
        "UPDATE floating_storage_events SET end_time = ? WHERE id = ?",
        (end_time, event_id),
    )


# ---------------------------------------------------------------------------
# Loading events
# ---------------------------------------------------------------------------


def insert_loading_event(
    conn: sqlite3.Connection,
    mmsi: int,
    terminal: str,
    grade: str | None,
    arrival_time: str,
    departure_time: str | None,
    draft_arrival_m: float | None,
    draft_departure_m: float | None,
    estimated_cargo_bbl: float | None,
    destination_reported: str | None,
    heading_at_departure: float | None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO loading_events
            (mmsi, terminal, grade, arrival_time, departure_time,
             draft_arrival_m, draft_departure_m, estimated_cargo_bbl,
             destination_reported, heading_at_departure)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (mmsi, terminal, grade, arrival_time, departure_time,
         draft_arrival_m, draft_departure_m, estimated_cargo_bbl,
         destination_reported, heading_at_departure),
    )
    return cursor.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Daily metrics
# ---------------------------------------------------------------------------


def upsert_daily_metrics(conn: sqlite3.Connection, **kwargs: Any) -> None:
    """Insert or update daily aggregate metrics."""
    date = kwargs.pop("date")
    columns = ", ".join(kwargs.keys())
    placeholders = ", ".join(["?"] * len(kwargs))
    updates = ", ".join(f"{k} = excluded.{k}" for k in kwargs)
    sql = f"""
        INSERT INTO daily_metrics (date, {columns})
        VALUES (?, {placeholders})
        ON CONFLICT(date) DO UPDATE SET {updates}
    """
    conn.execute(sql, (date, *kwargs.values()))


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_active_floating_storage(
    conn: sqlite3.Connection, region: str | None = None
) -> list[sqlite3.Row]:
    """Get all currently active (open) floating storage events."""
    sql = """
        SELECT fs.*, v.name, v.vessel_class, v.imo
        FROM floating_storage_events fs
        JOIN vessels v ON fs.mmsi = v.mmsi
        WHERE fs.end_time IS NULL
    """
    params: list[Any] = []
    if region:
        sql += " AND fs.region = ?"
        params.append(region)
    return conn.execute(sql, params).fetchall()


def get_recent_loading_events(
    conn: sqlite3.Connection,
    days: int = 7,
    terminal: str | None = None,
) -> list[sqlite3.Row]:
    """Get loading events from the last N days."""
    sql = """
        SELECT le.*, v.name, v.vessel_class, v.imo
        FROM loading_events le
        JOIN vessels v ON le.mmsi = v.mmsi
        WHERE le.arrival_time >= datetime('now', ?)
    """
    params: list[Any] = [f"-{days} days"]
    if terminal:
        sql += " AND le.terminal = ?"
        params.append(terminal)
    sql += " ORDER BY le.arrival_time DESC"
    return conn.execute(sql, params).fetchall()


def get_vessel_positions(
    conn: sqlite3.Connection, mmsi: int, hours: int = 24
) -> list[sqlite3.Row]:
    """Get position history for a vessel."""
    return conn.execute(
        """
        SELECT * FROM positions
        WHERE mmsi = ? AND timestamp >= datetime('now', ?)
        ORDER BY timestamp
        """,
        (mmsi, f"-{hours} hours"),
    ).fetchall()


def get_all_vessel_states(
    conn: sqlite3.Connection, status: str | None = None
) -> list[sqlite3.Row]:
    """Get current state of all tracked vessels."""
    sql = """
        SELECT vs.*, v.name, v.imo, v.vessel_class, v.max_draft_m, v.ship_type
        FROM vessel_state vs
        JOIN vessels v ON vs.mmsi = v.mmsi
    """
    params: list[Any] = []
    if status:
        sql += " WHERE vs.status = ?"
        params.append(status)
    return conn.execute(sql, params).fetchall()


def get_daily_metrics_range(
    conn: sqlite3.Connection, start_date: str, end_date: str
) -> list[sqlite3.Row]:
    """Get daily metrics for a date range."""
    return conn.execute(
        "SELECT * FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date",
        (start_date, end_date),
    ).fetchall()


def get_tanker_count(conn: sqlite3.Connection) -> int:
    """Count of tracked tanker vessels."""
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM vessels WHERE ship_type BETWEEN 80 AND 89"
    ).fetchone()
    return row["cnt"] if row else 0
