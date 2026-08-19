"""
tests/test_pricing_baseline.py

Minimal regression coverage for the EXISTING core/pricing.py and core/ctd.py
modules. No test suite existed for these before this workbench extension
(despite the README implying one did) — a handful of sanity checks, not
full coverage, so "existing tests must keep passing" is actually meaningful
going forward.
"""

from datetime import date

import pytest

from core.basket import conversion_factor, get_basket
from core.carry import implied_repo
from core.ctd import ctd_transition_threshold, rank_basket
from core.pricing import accrued_interest, price_bond, ytm_from_price


def test_price_bond_at_par_when_ytm_equals_coupon():
    settlement = date(2026, 3, 1)
    maturity = date(2033, 6, 30)
    coupon = 0.04250
    price = price_bond(coupon, maturity, coupon, settlement)
    assert abs(price - 100.0) < 0.05


def test_ytm_from_price_inverts_price_bond():
    settlement = date(2026, 3, 1)
    maturity = date(2036, 5, 15)
    coupon = 0.04375
    true_ytm = 0.0452
    price = price_bond(coupon, maturity, true_ytm, settlement)
    recovered_ytm = ytm_from_price(coupon, maturity, price, settlement)
    assert abs(recovered_ytm - true_ytm) < 1e-6


def test_rank_basket_flags_highest_implied_repo_as_ctd():
    settlement = date(2026, 3, 1)
    basket = get_basket()
    yields = {b["cusip"]: 0.045 for b in basket}

    df = rank_basket(yields=yields, futures_price=108.5, repo_rate=0.053, settlement=settlement)

    assert bool(df.loc[0, "is_ctd"]) is True
    assert df.loc[0, "implied_repo_pct"] == df["implied_repo_pct"].max()
    assert df["implied_repo_pct"].is_monotonic_decreasing


def test_ctd_transition_threshold_equalizes_implied_repo():
    """
    F* is defined as the futures price where implied_repo(bond A) ==
    implied_repo(bond B). Verify that identity actually holds at the F*
    the closed-form solves for (this is what the README's "exact closed
    form" claim rests on).
    """
    settlement = date(2026, 3, 1)
    delivery = date(2026, 6, 1)
    days = (delivery - settlement).days

    ctd_coupon, ctd_maturity = 0.03750, date(2033, 3, 15)
    runner_coupon, runner_maturity = 0.04500, date(2033, 11, 15)
    ctd_price, runner_price = 95.16, 99.60

    ctd_cf = conversion_factor(ctd_coupon, ctd_maturity, delivery)
    runner_cf = conversion_factor(runner_coupon, runner_maturity, delivery)

    result = ctd_transition_threshold(
        ctd_price, runner_price, ctd_cf, runner_cf, ctd_coupon, runner_coupon,
        days, settlement, ctd_maturity, runner_maturity,
    )
    f_star = result["transition_threshold_futures_price"]

    ctd_accrued = accrued_interest(ctd_coupon, ctd_maturity, settlement)
    runner_accrued = accrued_interest(runner_coupon, runner_maturity, settlement)

    ir_ctd = implied_repo(ctd_price, f_star, ctd_cf, ctd_coupon, days, ctd_accrued)
    ir_runner = implied_repo(runner_price, f_star, runner_cf, runner_coupon, days, runner_accrued)

    assert ir_ctd == pytest.approx(ir_runner, abs=1e-6)
