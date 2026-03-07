"""Tests for statistical methods and trade implications analysis."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.statistics import (
    CorrelationResult,
    lagged_correlation,
    multi_lag_correlation,
    pearson_with_ci,
    regime_analysis,
    walk_forward_test,
)


class TestPearsonWithCI:
    def test_perfect_positive(self):
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        r, p, ci_lo, ci_hi = pearson_with_ci(x, x)
        assert r == pytest.approx(1.0, abs=1e-10)
        assert p < 0.001

    def test_perfect_negative(self):
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        y = -x
        r, p, ci_lo, ci_hi = pearson_with_ci(x, y)
        assert r == pytest.approx(-1.0, abs=1e-10)

    def test_no_correlation(self):
        np.random.seed(42)
        x = np.random.randn(100)
        y = np.random.randn(100)
        r, p, ci_lo, ci_hi = pearson_with_ci(x, y)
        assert abs(r) < 0.3  # Likely small with random data
        assert ci_lo < r < ci_hi

    def test_insufficient_data(self):
        x = np.array([1.0, 2.0])
        y = np.array([3.0, 4.0])
        r, p, ci_lo, ci_hi = pearson_with_ci(x, y)
        assert r == 0.0
        assert p == 1.0


class TestLaggedCorrelation:
    def test_zero_lag(self):
        x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        y = pd.Series([2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0])
        result = lagged_correlation(x, y, lag=0)
        assert result.correlation == pytest.approx(1.0, abs=1e-10)
        assert result.lag_days == 0

    def test_positive_lag(self):
        x = pd.Series(range(20), dtype=float)
        y = pd.Series(range(20), dtype=float)
        result = lagged_correlation(x, y, lag=5)
        assert result.n_observations == 15
        assert result.lag_days == 5

    def test_result_dict(self):
        x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        y = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        result = lagged_correlation(x, y)
        d = result.to_dict()
        assert "correlation" in d
        assert "p_value" in d
        assert "significant" in d


class TestMultiLag:
    def test_returns_list(self):
        x = pd.Series(np.random.randn(50))
        y = pd.Series(np.random.randn(50))
        results = multi_lag_correlation(x, y, lags=[0, 7, 14])
        assert len(results) == 3
        assert all(isinstance(r, CorrelationResult) for r in results)


class TestWalkForward:
    def test_basic(self):
        np.random.seed(42)
        x = pd.Series(np.random.randn(50))
        y = pd.Series(np.random.randn(50))
        result = walk_forward_test(x, y)
        assert "in_sample" in result
        assert "out_of_sample" in result
        assert result["train_size"] == 30
        assert result["test_size"] == 20

    def test_insufficient_data(self):
        x = pd.Series([1.0, 2.0, 3.0])
        y = pd.Series([4.0, 5.0, 6.0])
        result = walk_forward_test(x, y)
        assert "error" in result


class TestRegimeAnalysis:
    def test_splits_correctly(self):
        np.random.seed(42)
        n = 100
        x = pd.Series(np.random.randn(n))
        y = pd.Series(np.random.randn(n))
        spread = pd.Series(np.random.randn(n))  # roughly 50/50 split
        result = regime_analysis(x, y, spread)
        assert "contango" in result
        assert "backwardation" in result

    def test_insufficient_regime_data(self):
        x = pd.Series([1.0, 2.0, 3.0])
        y = pd.Series([4.0, 5.0, 6.0])
        spread = pd.Series([1.0, 1.0, 1.0])  # all backwardation, no contango
        result = regime_analysis(x, y, spread)
        assert "error" in result["contango"]
