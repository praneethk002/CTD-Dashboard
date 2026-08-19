"""
tests/test_returns.py

Return math and horizon boundary resolution. Uses a small self-contained
2-security fixture (not the full 8-security demo universe) so these tests
don't depend on the seeded DB.
"""

from datetime import date

import pytest

from core.returns import (
    aggregate_market_value,
    compute_returns,
    horizon_start_date,
    resolve_supported_horizons,
    security_market_value,
    simple_return,
)

SECURITIES = [
    {"security_id": "A", "coupon": 0.04, "maturity": date(2030, 6, 30),
     "portfolio_weight": 0.6, "benchmark_weight": 0.5},
    {"security_id": "B", "coupon": 0.05, "maturity": date(2035, 6, 30),
     "portfolio_weight": 0.4, "benchmark_weight": 0.5},
]

# begin/end fall between coupon dates for both bonds (no mid-period coupon
# payment), so aggregate MV ratio should equal total_return exactly.
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


def test_simple_return():
    assert simple_return(100, 110) == pytest.approx(0.10)
    assert simple_return(100, 90) == pytest.approx(-0.10)


def test_simple_return_rejects_zero_begin():
    with pytest.raises(ValueError):
        simple_return(0, 100)


def test_security_market_value():
    assert security_market_value(1_000_000, 99.0, 0.5) == pytest.approx(1_000_000 * 99.5 / 100)


def test_aggregate_market_value_matches_manual_sum():
    total_notional = 10_000_000.0
    computed = aggregate_market_value(SECURITIES, SNAP_BEGIN, "portfolio_weight", total_notional)

    manual = sum(
        sec["portfolio_weight"] * total_notional * (SNAP_BEGIN[sec["security_id"]]["price"]
            + SNAP_BEGIN[sec["security_id"]]["accrued_interest"]) / 100.0
        for sec in SECURITIES
    )
    assert computed == pytest.approx(manual)


def test_compute_returns_no_coupon_in_window():
    result = compute_returns(SECURITIES, SNAP_BEGIN, SNAP_END)

    assert result["portfolio_coupon_cash"] == pytest.approx(0.0)
    assert result["benchmark_coupon_cash"] == pytest.approx(0.0)

    expected_portfolio_return = simple_return(result["portfolio_mv_begin"], result["portfolio_mv_end"])
    assert result["portfolio_return"] == pytest.approx(expected_portfolio_return)

    expected_active_bps = (result["portfolio_return"] - result["benchmark_return"]) * 10_000
    assert result["active_return_bps"] == pytest.approx(expected_active_bps)


def test_compute_returns_adds_back_mid_period_coupon_cash():
    # move snap_end past both bonds' June 30 coupon date
    snap_end_after_coupon = {
        "A": {**SNAP_END["A"], "date": date(2026, 7, 15), "accrued_interest": 0.10},
        "B": {**SNAP_END["B"], "date": date(2026, 7, 15), "accrued_interest": 0.12},
    }
    snap_begin_before_coupon = {
        "A": {**SNAP_BEGIN["A"], "date": date(2026, 1, 15)},
        "B": {**SNAP_BEGIN["B"], "date": date(2026, 1, 15)},
    }
    result = compute_returns(SECURITIES, snap_begin_before_coupon, snap_end_after_coupon)

    # both bonds pay a coupon between Jan 15 and Jul 15 -> coupon cash must be > 0
    assert result["portfolio_coupon_cash"] > 0
    assert result["benchmark_coupon_cash"] > 0

    # sanity: dropping the coupon cash would produce a lower (wrong) return
    naive_return = simple_return(result["portfolio_mv_begin"], result["portfolio_mv_end"])
    assert result["portfolio_return"] > naive_return


def test_horizon_start_date_boundaries():
    as_of = date(2026, 8, 19)
    assert horizon_start_date("MTD", as_of) == date(2026, 8, 1)
    assert horizon_start_date("QTD", as_of) == date(2026, 7, 1)
    assert horizon_start_date("YTD", as_of) == date(2026, 1, 1)


def test_horizon_start_date_rejects_1d():
    with pytest.raises(ValueError):
        horizon_start_date("1D", date(2026, 8, 19))


def test_resolve_supported_horizons_full_range():
    supported = resolve_supported_horizons(date(2025, 12, 31), date(2026, 8, 19))
    assert set(supported) == {"1D", "MTD", "QTD", "YTD"}


def test_resolve_supported_horizons_partial_range_excludes_ytd():
    supported = resolve_supported_horizons(date(2026, 6, 1), date(2026, 8, 19))
    assert "YTD" not in supported
    assert {"1D", "MTD", "QTD"}.issubset(set(supported))
