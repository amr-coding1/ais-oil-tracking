#!/usr/bin/env python3
"""Run trade implications analysis and output results."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.market_data import update_daily_metrics_market_data
from src.analysis.trade_implications import TradeImplicationsAnalyser
from src.database import get_connection, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("analysis")


def main():
    logger.info("Running trade implications analysis...")

    conn = init_db()

    # Update market data first
    logger.info("Updating market data...")
    updated = update_daily_metrics_market_data(conn)
    logger.info("Updated %d rows of market data", updated)

    # Run analysis
    analyser = TradeImplicationsAnalyser(conn)
    results = analyser.run_all()

    # Output results
    print("\n" + "=" * 60)
    print("TRADE IMPLICATIONS ANALYSIS RESULTS")
    print("=" * 60)
    print(json.dumps(results, indent=2, default=str))

    # Save to file
    output_dir = Path(__file__).resolve().parent.parent / "analysis" / "results"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "latest_analysis.json"
    output_path.write_text(json.dumps(results, indent=2, default=str))
    logger.info("Results saved to %s", output_path)

    conn.close()


if __name__ == "__main__":
    main()
