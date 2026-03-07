#!/usr/bin/env python3
"""Combined entry point — runs collector + Streamlit dashboard in one process.

Used for cloud deployment (Railway, Render, etc.) where both services
need to share the same SQLite database file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

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
logger = logging.getLogger("run_all")

_shutdown = threading.Event()


def _signal_handler(signum, frame):
    logger.info("Shutdown signal received")
    _shutdown.set()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ---------------------------------------------------------------------------
# Collector (background threads)
# ---------------------------------------------------------------------------

def run_periodic_tasks(fusion: DataFusionEngine) -> None:
    """Run periodic analysis tasks in a background thread."""
    from datetime import date

    detector = FloatingStorageDetector(fusion.conn)
    last_storage_check = 0
    last_metrics_update = 0

    while not _shutdown.is_set():
        now = time.time()

        if now - last_storage_check > 3600:
            try:
                result = detector.evaluate()
                logger.info("Floating storage check: %s", result)
                last_storage_check = now
            except Exception:
                logger.exception("Error in floating storage detection")

        if now - last_metrics_update > 21600:
            try:
                updated = update_daily_metrics_market_data(fusion.conn)
                logger.info("Market data updated: %d rows", updated)
                _aggregate_daily_metrics(fusion.conn)
                last_metrics_update = now
            except Exception:
                logger.exception("Error updating market data")

        _shutdown.wait(timeout=60)


def _aggregate_daily_metrics(conn) -> None:
    """Compute and store today's AIS-derived metrics."""
    from datetime import date
    today = date.today().isoformat()

    row = conn.execute(
        "SELECT COUNT(DISTINCT mmsi) as cnt FROM vessel_state"
    ).fetchone()
    total_vessels = row["cnt"] if row else 0

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

    baltic_row = conn.execute(
        """
        SELECT COUNT(*) as cnt, COALESCE(SUM(estimated_cargo_bbl), 0) as vol
        FROM loading_events
        WHERE date(arrival_time) = ?
          AND terminal IN ('Primorsk', 'Ust-Luga', 'Gdansk/Naftoport', 'Butinge')
        """,
        (today,),
    ).fetchone()

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


def start_collector():
    """Start all AIS collector threads."""
    aisstream_key = os.getenv("AISSTREAM_API_KEY", "")
    bw_client_id = os.getenv("BARENTSWATCH_CLIENT_ID", "")
    bw_client_secret = os.getenv("BARENTSWATCH_CLIENT_SECRET", "")

    fusion = DataFusionEngine()
    logger.info("Database initialised")

    # Digitraffic (no API key needed)
    digi_client = DigitrafficClient(
        on_position=fusion.on_position,
        on_metadata=fusion.on_metadata,
    )
    t = threading.Thread(target=digi_client.start, name="digitraffic", daemon=True)
    t.start()
    logger.info("Digitraffic MQTT client started")

    # BarentsWatch
    if bw_client_id and bw_client_secret:
        bw_client = BarentsWatchClient(
            client_id=bw_client_id,
            client_secret=bw_client_secret,
            on_position=fusion.on_position,
            on_metadata=fusion.on_metadata,
        )
        t = threading.Thread(target=bw_client.start, name="barentswatch", daemon=True)
        t.start()
        logger.info("BarentsWatch SSE client started")

    # aisstream.io
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
        logger.info("aisstream.io WebSocket client started")

    # Periodic tasks
    t = threading.Thread(
        target=run_periodic_tasks, args=(fusion,),
        name="periodic_tasks", daemon=True,
    )
    t.start()
    logger.info("Background tasks started")

    return fusion


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info("=" * 60)
    logger.info("North Sea & Baltic Crude Flow Monitor — Full Deployment")
    logger.info("=" * 60)

    # Start collector in background threads
    fusion = start_collector()
    logger.info("All collectors running")

    # Start Streamlit dashboard as subprocess
    port = os.getenv("PORT", "8501")
    dashboard_path = PROJECT_ROOT / "dashboard" / "app.py"

    logger.info("Starting Streamlit dashboard on port %s", port)
    streamlit_proc = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run",
            str(dashboard_path),
            "--server.port", port,
            "--server.address", "0.0.0.0",
            "--server.headless", "true",
            "--browser.gatherUsageStats", "false",
        ],
        cwd=str(PROJECT_ROOT),
    )

    # Wait for shutdown signal
    try:
        _shutdown.wait()
    except KeyboardInterrupt:
        pass

    logger.info("Shutting down...")
    streamlit_proc.terminate()
    streamlit_proc.wait(timeout=10)
    logger.info("Final stats: %s", fusion.stats)
    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
