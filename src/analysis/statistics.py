"""Statistical methods for trade implications analysis.

Implements walk-forward methodology, correlation analysis with confidence
intervals, and regime-dependent analysis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)


@dataclass
class CorrelationResult:
    """Result of a correlation analysis."""
    correlation: float
    p_value: float
    ci_lower: float
    ci_upper: float
    n_observations: int
    lag_days: int

    @property
    def is_significant(self) -> bool:
        return self.p_value < 0.05

    def to_dict(self) -> dict:
        return {
            "correlation": round(self.correlation, 4),
            "p_value": round(self.p_value, 4),
            "ci_lower": round(self.ci_lower, 4),
            "ci_upper": round(self.ci_upper, 4),
            "n_observations": self.n_observations,
            "lag_days": self.lag_days,
            "significant": self.is_significant,
        }


def pearson_with_ci(
    x: np.ndarray, y: np.ndarray, confidence: float = 0.95
) -> tuple[float, float, float, float]:
    """Pearson correlation with confidence interval via Fisher z-transform.

    Returns (r, p_value, ci_lower, ci_upper).
    """
    n = len(x)
    if n < 5:
        return 0.0, 1.0, -1.0, 1.0

    r, p = stats.pearsonr(x, y)

    # Fisher z-transform for confidence interval
    # Clamp r to avoid arctanh(1) = inf
    r_clamped = np.clip(r, -0.9999, 0.9999)
    z = np.arctanh(r_clamped)
    se = 1.0 / np.sqrt(n - 3) if n > 3 else float("inf")
    z_crit = stats.norm.ppf((1 + confidence) / 2)
    ci_lower = np.tanh(z - z_crit * se)
    ci_upper = np.tanh(z + z_crit * se)

    return float(r), float(p), float(ci_lower), float(ci_upper)


def lagged_correlation(
    series_x: pd.Series,
    series_y: pd.Series,
    lag: int = 0,
) -> CorrelationResult:
    """Compute Pearson correlation between x (lagged) and y.

    Positive lag means x leads y by `lag` periods.
    """
    if lag > 0:
        x = series_x.iloc[:-lag].values
        y = series_y.iloc[lag:].values
    elif lag < 0:
        x = series_x.iloc[-lag:].values
        y = series_y.iloc[:lag].values
    else:
        x = series_x.values
        y = series_y.values

    # Align and drop NaN
    mask = ~(np.isnan(x) | np.isnan(y))
    x, y = x[mask], y[mask]

    r, p, ci_lo, ci_hi = pearson_with_ci(x, y)
    return CorrelationResult(
        correlation=r,
        p_value=p,
        ci_lower=ci_lo,
        ci_upper=ci_hi,
        n_observations=len(x),
        lag_days=lag,
    )


def multi_lag_correlation(
    series_x: pd.Series,
    series_y: pd.Series,
    lags: list[int] | None = None,
) -> list[CorrelationResult]:
    """Compute correlations at multiple lags."""
    if lags is None:
        lags = [0, 7, 14, 28]  # 0, 1, 2, 4 weeks
    return [lagged_correlation(series_x, series_y, lag) for lag in lags]


def walk_forward_test(
    series_x: pd.Series,
    series_y: pd.Series,
    train_fraction: float = 0.6,
) -> dict:
    """Walk-forward out-of-sample test.

    Fits correlation on first `train_fraction` of data, tests on remainder.
    """
    n = len(series_x)
    split = int(n * train_fraction)

    if split < 10 or (n - split) < 5:
        return {
            "in_sample": None,
            "out_of_sample": None,
            "error": "Insufficient data for walk-forward test",
        }

    in_sample = lagged_correlation(
        series_x.iloc[:split], series_y.iloc[:split], lag=0
    )
    out_of_sample = lagged_correlation(
        series_x.iloc[split:], series_y.iloc[split:], lag=0
    )

    return {
        "in_sample": in_sample.to_dict(),
        "out_of_sample": out_of_sample.to_dict(),
        "train_size": split,
        "test_size": n - split,
    }


def regime_analysis(
    series_x: pd.Series,
    series_y: pd.Series,
    spread: pd.Series,
) -> dict:
    """Split analysis by contango vs backwardation regime.

    Uses the spread series to determine the regime:
        - Contango: spread < 0 (front month cheaper than deferred)
        - Backwardation: spread > 0 (front month more expensive)
    """
    contango_mask = spread < 0
    backwardation_mask = spread > 0

    results = {}
    for regime_name, mask in [("contango", contango_mask), ("backwardation", backwardation_mask)]:
        x_regime = series_x[mask]
        y_regime = series_y[mask]
        if len(x_regime) < 5:
            results[regime_name] = {
                "error": f"Insufficient data ({len(x_regime)} obs)",
                "n_observations": len(x_regime),
            }
        else:
            corr = lagged_correlation(x_regime, y_regime, lag=0)
            results[regime_name] = corr.to_dict()

    return results
