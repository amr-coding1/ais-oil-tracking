"""Market data fetching — Brent futures and calendar spreads via yfinance."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Brent crude futures tickers on Yahoo Finance
BRENT_FRONT = "BZ=F"  # Front-month Brent


def fetch_brent_prices(period: str = "6mo") -> pd.DataFrame:
    """Fetch Brent crude front-month prices.

    Returns DataFrame with columns: Date, Close, Volume
    """
    logger.info("Fetching Brent futures data (period=%s)", period)
    ticker = yf.Ticker(BRENT_FRONT)
    hist = ticker.history(period=period)
    if hist.empty:
        logger.warning("No Brent price data returned")
        return pd.DataFrame()
    hist = hist.reset_index()
    hist["Date"] = pd.to_datetime(hist["Date"]).dt.date
    return hist[["Date", "Close", "Volume"]].rename(
        columns={"Close": "brent_m1"}
    )


def fetch_brent_spreads(period: str = "6mo") -> pd.DataFrame:
    """Fetch Brent front-month price and compute proxy spreads.

    NOTE: Yahoo Finance does not provide individual Brent contract months
    for spread calculation. We use the front-month as a base and note this
    limitation. For a production system, ICE or CME data feeds would be used.

    Returns DataFrame with: Date, brent_m1, brent_m1_m2_spread (proxy)
    """
    prices = fetch_brent_prices(period)
    if prices.empty:
        return prices

    # Proxy M1-M2 spread: use rolling 5-day price change as a rough
    # indicator of term structure direction. This is a known limitation
    # documented in the README.
    prices["brent_m1_m2_spread"] = prices["brent_m1"].diff(5) / 5
    prices["brent_m1_m6_spread"] = prices["brent_m1"].diff(20) / 20

    return prices[["Date", "brent_m1", "brent_m1_m2_spread", "brent_m1_m6_spread"]].dropna()


def update_daily_metrics_market_data(conn: sqlite3.Connection) -> int:
    """Fetch latest market data and update daily_metrics table.

    Returns number of rows updated.
    """
    prices = fetch_brent_spreads("3mo")
    if prices.empty:
        return 0

    updated = 0
    for _, row in prices.iterrows():
        date_str = str(row["Date"])
        conn.execute(
            """
            INSERT INTO daily_metrics (date, brent_m1, brent_m1_m2_spread, brent_m1_m6_spread)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                brent_m1 = excluded.brent_m1,
                brent_m1_m2_spread = excluded.brent_m1_m2_spread,
                brent_m1_m6_spread = excluded.brent_m1_m6_spread
            """,
            (date_str, row["brent_m1"], row["brent_m1_m2_spread"], row["brent_m1_m6_spread"]),
        )
        updated += 1
    conn.commit()
    logger.info("Updated %d daily metrics rows with market data", updated)
    return updated
