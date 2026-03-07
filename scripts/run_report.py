#!/usr/bin/env python3
"""Generate weekly market intelligence report."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import init_db
from src.reporting.weekly_report import WeeklyReportGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("report")


def main():
    logger.info("Generating weekly report...")
    conn = init_db()
    generator = WeeklyReportGenerator(conn)
    report = generator.generate()
    print("\n" + report)
    conn.close()


if __name__ == "__main__":
    main()
