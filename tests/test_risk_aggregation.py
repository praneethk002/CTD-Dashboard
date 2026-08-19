"""
tests/test_risk_aggregation.py

Portfolio-level weighted duration/yield/DV01 aggregation (core/reporting.py)
against a hand-computed weighted sum on a small fixture.
"""

import pytest

from core.reporting import compute_risk_metrics

SECURITIES = [
    {"security_id": "A", "portfolio_weight": 0.6, "benchmark_weight": 0.5},
    {"security_id": "B", "portfolio_weight": 0.4, "benchmark_weight": 0.5},
]

SNAPSHOT = {
    "A": {"modified_duration": 3.5, "yield": 0.041, "dv01": 0.035},
    "B": {"modified_duration": 9.2, "yield": 0.048, "dv01": 0.091},
}

TOTAL_NOTIONAL = 10_000_000.0


def test_weighted_duration_matches_manual_sum():
    result = compute_risk_metrics(SECURITIES, SNAPSHOT, "portfolio_weight", TOTAL_NOTIONAL)
    manual_duration = 0.6 * 3.5 + 0.4 * 9.2
    assert result["weighted_modified_duration"] == pytest.approx(manual_duration)


def test_weighted_yield_matches_manual_sum():
    result = compute_risk_metrics(SECURITIES, SNAPSHOT, "portfolio_weight", TOTAL_NOTIONAL)
    manual_yield = 0.6 * 0.041 + 0.4 * 0.048
    assert result["weighted_yield"] == pytest.approx(manual_yield)


def test_dollar_dv01_matches_manual_sum():
    result = compute_risk_metrics(SECURITIES, SNAPSHOT, "portfolio_weight", TOTAL_NOTIONAL)
    manual_dv01 = (
        (0.6 * TOTAL_NOTIONAL / 100.0) * 0.035
        + (0.4 * TOTAL_NOTIONAL / 100.0) * 0.091
    )
    assert result["dv01_usd"] == pytest.approx(manual_dv01)


def test_benchmark_weight_key_produces_different_result():
    portfolio_result = compute_risk_metrics(SECURITIES, SNAPSHOT, "portfolio_weight", TOTAL_NOTIONAL)
    benchmark_result = compute_risk_metrics(SECURITIES, SNAPSHOT, "benchmark_weight", TOTAL_NOTIONAL)
    # weights differ (0.6/0.4 vs 0.5/0.5) so the aggregates must differ too
    assert portfolio_result["weighted_modified_duration"] != benchmark_result["weighted_modified_duration"]


def test_handles_missing_snapshot_gracefully():
    partial_snapshot = {"A": SNAPSHOT["A"]}  # "B" missing entirely
    result = compute_risk_metrics(SECURITIES, partial_snapshot, "portfolio_weight", TOTAL_NOTIONAL)
    # only A contributes, but denominator still uses total_weight (0.6+0.4=1.0) per current design
    assert result["weighted_modified_duration"] == pytest.approx(0.6 * 3.5)
