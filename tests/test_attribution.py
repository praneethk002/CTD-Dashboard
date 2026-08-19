"""
tests/test_attribution.py

Attribution formula correctness and reconciliation. Because carry is now
computed exactly (change in accrued interest + any coupon cash paid,
core/attribution.py:carry_component), total_return == carry +
price_effect_approx + residual holds by construction — these tests guard
that identity (a sign or arithmetic slip anywhere would break it) and check
that the residual is genuinely surfaced, not clamped away.
"""

from datetime import date

import pytest

from core.attribution import (
    bucket_attribution,
    compare_portfolio_vs_benchmark,
    price_effect_approx,
    security_contribution,
)
from core.returns import simple_return

SECURITIES = [
    {"security_id": "A", "label": "Bond A", "coupon": 0.04, "maturity": date(2030, 6, 30),
     "classification": "Short", "portfolio_weight": 0.6, "benchmark_weight": 0.5},
    {"security_id": "B", "label": "Bond B", "coupon": 0.05, "maturity": date(2035, 6, 30),
     "classification": "Long", "portfolio_weight": 0.4, "benchmark_weight": 0.5},
]

SNAP_BEGIN = {
    "A": {"date": date(2026, 1, 15), "price": 99.0, "yield": 0.041, "accrued_interest": 0.20,
          "modified_duration": 3.5, "convexity": 15.0, "dv01": 0.035},
    "B": {"date": date(2026, 1, 15), "price": 101.5, "yield": 0.048, "accrued_interest": 0.25,
          "modified_duration": 7.2, "convexity": 60.0, "dv01": 0.073},
}
SNAP_END = {
    "A": {"date": date(2026, 2, 15), "price": 99.4, "yield": 0.0405, "accrued_interest": 0.37,
          "modified_duration": 3.4, "convexity": 15.0, "dv01": 0.034},
    "B": {"date": date(2026, 2, 15), "price": 101.0, "yield": 0.0485, "accrued_interest": 0.46,
          "modified_duration": 7.1, "convexity": 59.0, "dv01": 0.071},
}


def test_price_effect_approx_matches_formula():
    md, cvx, dy = 6.0, 45.0, -0.0025
    expected = -md * dy + 0.5 * cvx * (dy ** 2)
    assert price_effect_approx(md, cvx, dy) == pytest.approx(expected)


def test_price_effect_approx_sign_convention():
    # yields fall -> price should rise -> positive price effect
    assert price_effect_approx(5.0, 20.0, -0.001) > 0
    # yields rise -> price should fall -> negative price effect
    assert price_effect_approx(5.0, 20.0, 0.001) < 0


def test_security_contribution_reconciles_exactly():
    for sec in SECURITIES:
        c = security_contribution(sec, SNAP_BEGIN[sec["security_id"]], SNAP_END[sec["security_id"]], "portfolio_weight")
        assert c["total_return"] == pytest.approx(c["carry"] + c["price_effect_approx"] + c["residual"])


def test_carry_matches_manual_income_calc():
    sec = SECURITIES[0]
    begin = SNAP_BEGIN["A"]
    end = SNAP_END["A"]
    c = security_contribution(sec, begin, end, "portfolio_weight")

    begin_dirty = begin["price"] + begin["accrued_interest"]
    # no coupon date between Jan 15 and Feb 15 for a Jun/Dec-paying bond
    manual_carry = (end["accrued_interest"] - begin["accrued_interest"]) / begin_dirty
    assert c["carry"] == pytest.approx(manual_carry)


def test_residual_is_not_forced_to_zero():
    # deliberately mismatched convexity/duration vs. the actual price move
    sec = {"security_id": "C", "label": "Bond C", "coupon": 0.045, "maturity": date(2040, 6, 30),
           "classification": "Long", "portfolio_weight": 1.0, "benchmark_weight": 1.0}
    begin = {"date": date(2026, 1, 15), "price": 100.0, "yield": 0.045, "accrued_interest": 0.1,
             "modified_duration": 0.01, "convexity": 0.0, "dv01": 0.0001}  # deliberately-bad approximation inputs
    end = {"date": date(2026, 2, 15), "price": 92.0, "yield": 0.052, "accrued_interest": 0.2,
           "modified_duration": 0.01, "convexity": 0.0, "dv01": 0.0001}

    c = security_contribution(sec, begin, end, "portfolio_weight")
    assert abs(c["residual"]) > 0.01  # a large, visible residual — not clamped to ~0


def test_bucket_attribution_reconciles_to_weighted_sum():
    buckets = bucket_attribution(SECURITIES, SNAP_BEGIN, SNAP_END, "portfolio_weight")
    total_from_buckets = sum(b["contribution_to_portfolio"] for b in buckets)

    total_manual = sum(
        sec["portfolio_weight"] * security_contribution(
            sec, SNAP_BEGIN[sec["security_id"]], SNAP_END[sec["security_id"]], "portfolio_weight"
        )["total_return"]
        for sec in SECURITIES
    )
    assert total_from_buckets == pytest.approx(total_manual)


def test_compare_portfolio_vs_benchmark_difference_is_consistent():
    comparison = compare_portfolio_vs_benchmark(SECURITIES, SNAP_BEGIN, SNAP_END)
    for row in comparison:
        assert row["difference"] == pytest.approx(row["portfolio_contribution"] - row["benchmark_contribution"])
