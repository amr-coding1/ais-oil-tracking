"""Trade implications analysis — linking physical AIS flows to crude timespreads.

Three core analyses:
    1. ARA floating storage vs Brent M1-M2 spread
    2. Baltic export volumes vs Brent price
    3. Brent loading activity vs term structure
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.statistics import (
    CorrelationResult,
    multi_lag_correlation,
    regime_analysis,
    walk_forward_test,
)

logger = logging.getLogger(__name__)


def _load_daily_metrics(conn: sqlite3.Connection) -> pd.DataFrame:
    """Load daily_metrics table into a pandas DataFrame."""
    df = pd.read_sql_query(
        "SELECT * FROM daily_metrics ORDER BY date", conn
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    return df


class TradeImplicationsAnalyser:
    """Runs all three trade implications analyses."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def run_all(self) -> dict[str, Any]:
        """Run all analyses and return results."""
        df = _load_daily_metrics(self.conn)
        if df.empty or len(df) < 14:
            return {"error": "Insufficient daily metrics data for analysis"}

        results = {}
        results["ara_storage_vs_spread"] = self._ara_storage_analysis(df)
        results["baltic_exports_vs_price"] = self._baltic_exports_analysis(df)
        results["brent_loading_vs_spread"] = self._brent_loading_analysis(df)
        results["current_signal"] = self._current_signal(df)

        return results

    def _ara_storage_analysis(self, df: pd.DataFrame) -> dict[str, Any]:
        """Analysis 1: ARA floating storage vs Brent M1-M2 spread.

        Hypothesis: Increased floating storage → contango widening.
        """
        storage = df["floating_storage_ara_bbl"].dropna()
        spread = df["brent_m1_m2_spread"].dropna()

        # Align on common dates
        common = storage.index.intersection(spread.index)
        if len(common) < 10:
            return {"error": f"Only {len(common)} overlapping data points — ARA floating storage "
                    "requires tankers to be stationary & laden for 7+ days before detection. "
                    "Keep the collector running for at least 2 weeks to accumulate data."}

        storage = storage.loc[common]
        spread = spread.loc[common]

        return {
            "description": (
                "ARA floating storage (barrels) vs Brent M1-M2 calendar spread. "
                "Hypothesis: rising floating storage coincides with or precedes "
                "contango widening (weaker M1-M2 spread)."
            ),
            "multi_lag": [
                c.to_dict() for c in multi_lag_correlation(
                    storage, spread, lags=[0, 7, 14, 28]
                )
            ],
            "walk_forward": walk_forward_test(storage, spread),
            "regime": regime_analysis(storage, spread, spread),
            "n_observations": len(common),
        }

    def _baltic_exports_analysis(self, df: pd.DataFrame) -> dict[str, Any]:
        """Analysis 2: Baltic export volumes vs Brent price.

        Hypothesis: Elevated Baltic exports → weaker Brent (more supply).
        Note: Ideally we would use the Urals-Dated Brent differential,
        which is not freely available. Using flat Brent as proxy.
        """
        exports = df["baltic_export_volume_bbl"].dropna()
        price = df["brent_m1"].dropna()

        common = exports.index.intersection(price.index)
        if len(common) < 10:
            return {"error": f"Only {len(common)} overlapping data points — Baltic export tracking "
                    "requires tankers to depart from Baltic terminals (Primorsk, Ust-Luga, etc.) "
                    "with detected cargo. Keep the collector running to capture departures."}

        exports = exports.loc[common]
        price = price.loc[common]

        return {
            "description": (
                "Baltic crude export volumes vs Brent front-month price. "
                "Proxy for Urals-Dated Brent differential analysis. "
                "Hypothesis: elevated Baltic exports → more Russian crude "
                "hitting the market → Urals discount widens."
            ),
            "limitation": (
                "Urals-Dated Brent differential not freely available. "
                "Using flat Brent price as rough proxy."
            ),
            "multi_lag": [
                c.to_dict() for c in multi_lag_correlation(
                    exports, price, lags=[0, 7, 14, 28]
                )
            ],
            "walk_forward": walk_forward_test(exports, price),
            "n_observations": len(common),
        }

    def _brent_loading_analysis(self, df: pd.DataFrame) -> dict[str, Any]:
        """Analysis 3: Brent basket loading activity vs term structure.

        Hypothesis: Below-average loading → physical tightness → backwardation.
        """
        # Total BFOET loadings per day
        bfoet_cols = [
            "brent_loadings_count", "forties_loadings_count",
            "oseberg_loadings_count", "ekofisk_loadings_count",
            "troll_loadings_count",
        ]
        available_cols = [c for c in bfoet_cols if c in df.columns]
        if not available_cols:
            return {"error": "No loading count data available"}

        df["total_bfoet_loadings"] = df[available_cols].sum(axis=1)
        loadings = df["total_bfoet_loadings"].dropna()
        spread = df["brent_m1_m2_spread"].dropna()

        common = loadings.index.intersection(spread.index)
        if len(common) < 10:
            return {"error": f"Only {len(common)} overlapping data points — BFOET loading detection "
                    "requires tankers to complete full loading cycles at Brent terminals "
                    "(12-48 hours each). The collector needs more run time."}

        loadings = loadings.loc[common]
        spread = spread.loc[common]

        # Check for zero-variance (all loading counts are 0 — no events yet)
        if loadings.std() == 0:
            return {
                "error": (
                    "No variation in loading counts yet — the collector needs to "
                    "run long enough for vessels to complete loading cycles at "
                    "Brent terminals (typically 12-48 hours per cargo)."
                ),
            }

        # Compute deviation from rolling average
        rolling_avg = loadings.rolling(window=14, min_periods=7).mean()
        deviation = loadings - rolling_avg
        deviation = deviation.dropna()
        spread_aligned = spread.loc[deviation.index]

        if len(deviation) < 10 or deviation.std() == 0:
            return {"error": "Insufficient loading variation for correlation analysis"}

        return {
            "description": (
                "Brent basket (BFOET) loading activity deviations from "
                "14-day rolling average vs Brent M1-M2 spread. "
                "Hypothesis: below-average loadings → physical tightness → "
                "M1-M2 spread strengthens (backwardation)."
            ),
            "multi_lag": [
                c.to_dict() for c in multi_lag_correlation(
                    deviation, spread_aligned, lags=[0, 7, 14, 28]
                )
            ],
            "walk_forward": walk_forward_test(deviation, spread_aligned),
            "n_observations": len(deviation),
        }

    def _current_signal(self, df: pd.DataFrame) -> dict[str, Any]:
        """Generate current market signal reading."""
        if df.empty:
            return {"error": "No data"}

        # Use the latest row that actually has Brent price data
        df_with_price = df[df["brent_m1"].notna()]
        if df_with_price.empty:
            return {"error": "No market data available yet"}

        latest = df_with_price.iloc[-1]
        brent = float(latest["brent_m1"])
        spread_raw = latest.get("brent_m1_m2_spread")
        spread = float(spread_raw) if pd.notna(spread_raw) else None

        ara_raw = latest.get("floating_storage_ara_bbl")
        ara_storage = float(ara_raw) if pd.notna(ara_raw) else None

        # Calculate percentile of current ARA storage vs history
        storage_series = df["floating_storage_ara_bbl"].dropna()
        if len(storage_series) > 0 and ara_storage is not None:
            percentile = float((storage_series < ara_storage).mean() * 100)
        else:
            percentile = None

        regime = "backwardation" if spread and spread > 0 else "contango"

        return {
            "date": str(df_with_price.index[-1].date()) if hasattr(df_with_price.index[-1], 'date') else str(df_with_price.index[-1]),
            "ara_floating_storage_bbl": ara_storage,
            "ara_storage_percentile": round(percentile, 1) if percentile is not None else None,
            "brent_m1": brent,
            "brent_m1_m2_spread": spread,
            "regime": regime,
            "commentary": self._generate_commentary(
                ara_storage, percentile, spread, regime
            ),
        }

    @staticmethod
    def _generate_commentary(
        ara_storage: float | None,
        percentile: float | None,
        spread: float | None,
        regime: str,
    ) -> str:
        """Generate human-readable market commentary."""
        parts = []

        if ara_storage is not None and percentile is not None:
            storage_mmbbl = ara_storage / 1_000_000
            level = "elevated" if percentile > 70 else "low" if percentile < 30 else "moderate"
            parts.append(
                f"ARA floating storage is at {storage_mmbbl:.1f} MMbbl "
                f"({percentile:.0f}th percentile vs observation history) — {level} levels."
            )

        if spread is not None:
            parts.append(
                f"Brent M1-M2 spread is ${spread:.2f}/bbl ({regime})."
            )

        if ara_storage is not None and percentile is not None and spread is not None:
            if percentile > 70 and regime == "contango":
                parts.append(
                    "High floating storage in contango is economically rational "
                    "(cash-and-carry). The physical market appears well-supplied."
                )
            elif percentile > 70 and regime == "backwardation":
                parts.append(
                    "Elevated floating storage despite backwardation suggests "
                    "distressed cargoes or logistical bottlenecks — a signal of "
                    "market dislocation worth monitoring."
                )
            elif percentile < 30 and regime == "backwardation":
                parts.append(
                    "Low floating storage in backwardation is consistent — "
                    "the physical market is tight and incentivises prompt delivery."
                )
            elif percentile < 30 and regime == "contango":
                parts.append(
                    "Low floating storage in contango suggests the market may "
                    "be transitioning — contango may not persist if physical "
                    "barrels remain scarce."
                )

        return " ".join(parts) if parts else "Insufficient data for commentary."
