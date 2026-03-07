#!/usr/bin/env python3
"""Start all three AIS data collectors.

Runs aisstream.io (async WebSocket), Digitraffic (MQTT), and BarentsWatch
(SSE) concurrently, fusing data into a unified vessel tracking database.

Also runs periodic background tasks:
    - Floating storage detection (every hour)
    - Daily metrics aggregation (every 6 hours)
    - Market data updates (every 6 hours)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.ingestion.aisstream_client import AISStreamClient
from src.ingestion.barentswatch_client import BarentsWatchClient
from src.ingestion.digitraffic_client import DigitrafficClient
from src.ingestion.data_fusion import DataFusionEngine
from src.tracking.floating_storage import FloatingStorageDetector
from src.analysis.market_data import update_daily_metrics_market_data

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("collector")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown = threading.Event()


def _signal_handler(signum, frame):
    logger.info("Shutdown signal received")
    _shutdown.set()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


def _prune_old_positions(conn, days: int = 30) -> int:
    """Delete position records older than N days to prevent unbounded DB growth."""
    cursor = conn.execute(
        "DELETE FROM positions WHERE timestamp < datetime('now', ?)",
        (f"-{days} days",),
    )
    conn.commit()
    return cursor.rowcount


def run_periodic_tasks(fusion: DataFusionEngine) -> None:
    """Run periodic analysis tasks in a background thread."""
    detector = FloatingStorageDetector(fusion.conn)
    last_storage_check = 0
    last_metrics_update = 0
    last_prune = 0

    while not _shutdown.is_set():
        now = time.time()

        # Floating storage detection — every hour
        if now - last_storage_check > 3600:
            try:
                result = detector.evaluate()
                logger.info("Floating storage check: %s", result)
                last_storage_check = now
            except Exception:
                logger.exception("Error in floating storage detection")

        # Market data + daily metrics — every 6 hours
        if now - last_metrics_update > 21600:
            try:
                updated = update_daily_metrics_market_data(fusion.conn)
                logger.info("Market data updated: %d rows", updated)
                _aggregate_daily_metrics(fusion.conn)
                last_metrics_update = now
            except Exception:
                logger.exception("Error updating market data")

        # Prune old positions — every 24 hours
        if now - last_prune > 86400:
            try:
                deleted = _prune_old_positions(fusion.conn, days=30)
                if deleted:
                    logger.info("Pruned %d position records older than 30 days", deleted)
                last_prune = now
            except Exception:
                logger.exception("Error pruning old positions")

        _shutdown.wait(timeout=60)


def _aggregate_daily_metrics(conn) -> None:
    """Compute and store today's AIS-derived metrics."""
    from datetime import date
    today = date.today().isoformat()

    # Count vessels
    row = conn.execute(
        "SELECT COUNT(DISTINCT mmsi) as cnt FROM vessel_state"
    ).fetchone()
    total_vessels = row["cnt"] if row else 0

    # Floating storage by region
    storage_rows = conn.execute(
        """
        SELECT
            SUM(CASE WHEN region = 'ARA' THEN estimated_cargo_bbl ELSE 0 END) as ara,
            SUM(CASE WHEN region = 'North Sea' THEN estimated_cargo_bbl ELSE 0 END) as ns,
            SUM(CASE WHEN region = 'Baltic' THEN estimated_cargo_bbl ELSE 0 END) as baltic
        FROM floating_storage_events
        WHERE end_time IS NULL
        """
    ).fetchone()

    # Loading counts by grade
    grade_counts = {}
    for grade_key, grade_name in [
        ("brent", "Brent"), ("forties", "Forties"),
        ("oseberg", "Oseberg"), ("ekofisk", "Ekofisk"), ("troll", "Troll"),
    ]:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM loading_events "
            "WHERE grade = ? AND date(arrival_time) = ?",
            (grade_name, today),
        ).fetchone()
        grade_counts[f"{grade_key}_loadings_count"] = row["cnt"] if row else 0

    # Baltic exports
    baltic_row = conn.execute(
        """
        SELECT COUNT(*) as cnt, COALESCE(SUM(estimated_cargo_bbl), 0) as vol
        FROM loading_events
        WHERE date(arrival_time) = ?
          AND terminal IN ('Primorsk', 'Ust-Luga', 'Gdansk/Naftoport', 'Butinge')
        """,
        (today,),
    ).fetchone()

    # Average ARA dwell time
    dwell_row = conn.execute(
        """
        SELECT AVG(julianday('now') - julianday(status_since)) as avg_days
        FROM vessel_state
        WHERE region LIKE 'anchorage:%' AND sog < 1.0
        """
    ).fetchone()

    conn.execute(
        """
        INSERT INTO daily_metrics (
            date, total_vessels_tracked,
            floating_storage_ara_bbl, floating_storage_northsea_bbl,
            floating_storage_baltic_bbl,
            brent_loadings_count, forties_loadings_count,
            oseberg_loadings_count, ekofisk_loadings_count,
            troll_loadings_count,
            baltic_export_departures, baltic_export_volume_bbl,
            avg_ara_dwell_days
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            total_vessels_tracked = excluded.total_vessels_tracked,
            floating_storage_ara_bbl = excluded.floating_storage_ara_bbl,
            floating_storage_northsea_bbl = excluded.floating_storage_northsea_bbl,
            floating_storage_baltic_bbl = excluded.floating_storage_baltic_bbl,
            brent_loadings_count = excluded.brent_loadings_count,
            forties_loadings_count = excluded.forties_loadings_count,
            oseberg_loadings_count = excluded.oseberg_loadings_count,
            ekofisk_loadings_count = excluded.ekofisk_loadings_count,
            troll_loadings_count = excluded.troll_loadings_count,
            baltic_export_departures = excluded.baltic_export_departures,
            baltic_export_volume_bbl = excluded.baltic_export_volume_bbl,
            avg_ara_dwell_days = excluded.avg_ara_dwell_days
        """,
        (
            today, total_vessels,
            storage_rows["ara"] if storage_rows else 0,
            storage_rows["ns"] if storage_rows else 0,
            storage_rows["baltic"] if storage_rows else 0,
            grade_counts["brent_loadings_count"],
            grade_counts["forties_loadings_count"],
            grade_counts["oseberg_loadings_count"],
            grade_counts["ekofisk_loadings_count"],
            grade_counts["troll_loadings_count"],
            baltic_row["cnt"] if baltic_row else 0,
            baltic_row["vol"] if baltic_row else 0,
            dwell_row["avg_days"] if dwell_row and dwell_row["avg_days"] else None,
        ),
    )
    conn.commit()
    logger.info("Daily metrics aggregated for %s", today)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    logger.info("=" * 60)
    logger.info("North Sea & Baltic Crude Flow Monitor — Data Collector")
    logger.info("=" * 60)

    # Validate API keys
    aisstream_key = os.getenv("AISSTREAM_API_KEY", "")
    bw_client_id = os.getenv("BARENTSWATCH_CLIENT_ID", "")
    bw_client_secret = os.getenv("BARENTSWATCH_CLIENT_SECRET", "")

    if not aisstream_key:
        logger.warning("AISSTREAM_API_KEY not set — aisstream client will not start")
    if not bw_client_id or not bw_client_secret:
        logger.warning("BarentsWatch credentials not set — BarentsWatch client will not start")

    # Initialise data fusion engine
    fusion = DataFusionEngine()
    logger.info("Database initialised")

    threads: list[threading.Thread] = []

    # Start Digitraffic (no API key needed)
    digi_client = DigitrafficClient(
        on_position=fusion.on_position,
        on_metadata=fusion.on_metadata,
    )
    t = threading.Thread(target=digi_client.start, name="digitraffic", daemon=True)
    t.start()
    threads.append(t)
    logger.info("Digitraffic MQTT client started")

    # Start BarentsWatch
    if bw_client_id and bw_client_secret:
        bw_client = BarentsWatchClient(
            client_id=bw_client_id,
            client_secret=bw_client_secret,
            on_position=fusion.on_position,
            on_metadata=fusion.on_metadata,
        )
        t = threading.Thread(target=bw_client.start, name="barentswatch", daemon=True)
        t.start()
        threads.append(t)
        logger.info("BarentsWatch SSE client started")

    # Start aisstream (async — run in its own thread with event loop)
    if aisstream_key:
        ais_client = AISStreamClient(
            api_key=aisstream_key,
            on_position=fusion.on_position,
            on_metadata=fusion.on_metadata,
        )

        def run_aisstream():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(ais_client.start())

        t = threading.Thread(target=run_aisstream, name="aisstream", daemon=True)
        t.start()
        threads.append(t)
        logger.info("aisstream.io WebSocket client started")

    # Start periodic background tasks
    t = threading.Thread(
        target=run_periodic_tasks, args=(fusion,),
        name="periodic_tasks", daemon=True,
    )
    t.start()
    threads.append(t)
    logger.info("Background tasks started (storage detection, metrics aggregation)")

    logger.info("All collectors running. Press Ctrl+C to stop.")

    # Wait for shutdown
    _shutdown.wait()
    logger.info("Shutting down collectors...")

    # Stats
    logger.info("Final stats: %s", fusion.stats)
    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
