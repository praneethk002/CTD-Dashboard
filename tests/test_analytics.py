"""
tests/test_analytics.py

Numerical tests for core analytics functions.

Each test verifies a specific formula against either:
  - A hand-calculated reference value
  - A mathematical identity (e.g. roundtrip, parity condition)
  - A known-good boundary condition (zero accrued at coupon date, etc.)

Run with: pytest tests/
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from datetime import date

from core.basket import conversion_factor, DELIVERY_DATE
from core.carry import coupon_income_act_act, gross_basis, carry, net_basis, implied_repo
from core.ctd import rank_basket, ctd_transition_threshold, basis_dv01
from core.pricing import price_bond, dv01, accrued_interest, ytm_from_price


# ---------------------------------------------------------------------------
# Conversion factor — CME algorithm
# ---------------------------------------------------------------------------

class TestConversionFactor:

    def test_zero_stub_june_2033(self):
        """4.000% Jun-33, delivery Jun 2026: 84 months, no stub."""
        # months = (2033-2026)*12 + (6-6) = 84 → rounds to 84 → n=14, z=0
        cf = conversion_factor(0.04000, date(2033, 6, 30), date(2026, 6, 1))
        v = 1 / 1.03
        c = 0.04000 / 2
        n = 14
        annuity  = c * (1 - v**n) / 0.03
        expected = round(annuity + v**n, 4)
        assert abs(cf - expected) < 1e-4, f"CF={cf}, expected={expected}"

    def test_three_month_stub_sep_2033(self):
        """4.125% Sep-33, delivery Jun 2026: 87 months → n=14, z=3."""
        # months = (2033-2026)*12 + (9-6) = 87 → 87//3*3=87 → n=14, z=3
        cf = conversion_factor(0.04125, date(2033, 9, 30), date(2026, 6, 1))
        v = 1 / 1.03
        c = 0.04125 / 2
        n = 14
        annuity    = c * (1 - v**n) / 0.03
        pv_at_stub = c + annuity + v**n
        pv         = pv_at_stub * v**0.5
        accrued    = c * 0.5
        expected   = round(pv - accrued, 4)
        assert abs(cf - expected) < 1e-4, f"CF={cf}, expected={expected}"

    def test_non_aligned_rounds_to_three_month_stub(self):
        """4.500% Nov-33: 89 months rounds DOWN to 87 → same formula as 87-month bond."""
        # months = (2033-2026)*12 + (11-6) = 89 → rounds to 87 → n=14, z=3
        cf_nov33 = conversion_factor(0.04500, date(2033, 11, 15), date(2026, 6, 1))
        v = 1 / 1.03
        c = 0.04500 / 2
        n = 14
        annuity    = c * (1 - v**n) / 0.03
        pv_at_stub = c + annuity + v**n
        pv         = pv_at_stub * v**0.5
        accrued    = c * 0.5
        expected   = round(pv - accrued, 4)
        assert abs(cf_nov33 - expected) < 1e-4, f"CF={cf_nov33}, expected={expected}"

    def test_non_aligned_rounds_to_zero_stub(self):
        """4.250% Feb-34: 92 months rounds DOWN to 90 → n=15, z=0 (no stub)."""
        # months = (2034-2026)*12 + (2-6) = 96-4 = 92 → rounds to 90 → n=15, z=0
        cf = conversion_factor(0.04250, date(2034, 2, 15), date(2026, 6, 1))
        v = 1 / 1.03
        c = 0.04250 / 2
        n = 15
        annuity  = c * (1 - v**n) / 0.03
        expected = round(annuity + v**n, 4)
        assert abs(cf - expected) < 1e-4, f"CF={cf}, expected={expected}"

    def test_six_percent_coupon_at_delivery_equals_one(self):
        """A 6% bond maturing at delivery has CF exactly 1.0000."""
        # 6% bond with zero remaining life: n=0, z=0
        # Any maturity on the delivery date: 0 months
        cf = conversion_factor(0.06, date(2026, 6, 30), date(2026, 6, 1))
        # months = 0 → rounds to 0 → n=0, z=0
        # pv = 0 + 1.0 = 1.0, accrued = 0
        assert abs(cf - 1.0) < 1e-4, f"CF={cf}, expected=1.0000"

    def test_low_coupon_cf_below_one(self):
        """Below-6% coupon bond must have CF < 1 (trades at discount to par at 6%)."""
        cf = conversion_factor(0.03625, date(2032, 12, 31), date(2026, 6, 1))
        assert cf < 1.0, f"Expected CF < 1 for 3.625% bond, got {cf}"

    def test_high_coupon_cf_above_one(self):
        """Above-6% coupon bond must have CF > 1 (trades at premium to par at 6%)."""
        cf = conversion_factor(0.0700, date(2034, 5, 15), date(2026, 6, 1))
        assert cf > 1.0, f"Expected CF > 1 for 7.000% bond, got {cf}"


# ---------------------------------------------------------------------------
# basis_dv01
# ---------------------------------------------------------------------------

class TestBasisDv01:

    def test_cf_equals_one_zero_residual(self):
        """For CF=1, bond DV01 and futures DV01 cancel perfectly."""
        result = basis_dv01(0.08, 0.08, 1.0)
        assert abs(result) < 1e-12

    def test_cf_less_than_one_positive_residual(self):
        """Bond DV01 > CF-scaled futures DV01 → positive basis DV01."""
        # bond_dv01=0.09, futures_dv01=0.08, cf=0.90
        # expected = 0.09 - 0.08/0.90 = 0.09 - 0.08889 = 0.00111
        result = basis_dv01(0.09, 0.08, 0.90)
        expected = 0.09 - 0.08 / 0.90
        assert abs(result - expected) < 1e-12

    def test_formula_uses_conv_factor(self):
        """Verify conv_factor is not algebraically cancelled."""
        r1 = basis_dv01(0.085, 0.080, 0.91)
        r2 = basis_dv01(0.085, 0.080, 0.95)
        # Different CFs must give different results
        assert r1 != r2

    def test_equal_dv01_nonunit_cf(self):
        """For equal DV01s but CF != 1, residual is non-zero."""
        result = basis_dv01(0.08, 0.08, 0.91)
        expected = 0.08 - 0.08 / 0.91  # small negative number
        assert abs(result - expected) < 1e-12
        assert result < 0  # futures hedge slightly over-hedges


# ---------------------------------------------------------------------------
# Coupon income ACT/ACT
# ---------------------------------------------------------------------------

class TestCouponIncomeActAct:

    def test_no_coupon_in_period(self):
        """Holding period within one coupon period — income = change in accrued."""
        # 4.000% Jun-33: coupon dates Jun-30 and Dec-30 (approx)
        # Hold from Jul 1 to Sep 30 — no coupon payment
        coupon     = 0.04000
        maturity   = date(2033, 6, 30)
        settlement = date(2026, 7, 1)
        days       = 91  # ~3 months within the Jul-Dec coupon period

        ci = coupon_income_act_act(coupon, maturity, settlement, days)

        ai_start = accrued_interest(coupon, maturity, settlement)
        ai_end   = accrued_interest(coupon, maturity, date(2026, 9, 30))
        expected = ai_end - ai_start  # no coupon crossed

        assert abs(ci - expected) < 1e-8

    def test_coupon_in_period(self):
        """If coupon falls within holding period, full coupon is included.

        Net coupon income = full coupon received - accrued surrendered at purchase
                           + accrued earned from coupon date to delivery.
        When the bond is purchased with substantial accrued (e.g. 3.5 months in),
        the net income is less than the full semi-annual coupon.
        """
        # 4.000% Jun-33: coupon dates Jun-30 and Dec-30 each year
        # Hold from April 15 2026 through July 15 2026 — crosses Jun-30 2026 coupon
        coupon     = 0.04000
        maturity   = date(2033, 6, 30)
        settlement = date(2026, 4, 15)
        days       = 91  # Apr 15 + 91 = Jul 15, crosses Jun-30 coupon

        ci = coupon_income_act_act(coupon, maturity, settlement, days)

        # Expected: (coupon/2)*100 - ai_start + ai_end
        ai_start = accrued_interest(coupon, maturity, settlement)
        ai_end   = accrued_interest(coupon, maturity, date(2026, 7, 15))
        expected = (coupon / 2) * 100 - ai_start + ai_end

        assert abs(ci - expected) < 1e-8
        # ci ≈ 2.0 - 1.165 + 0.164 ≈ 0.999 (net, after surrendering accrued at purchase)
        # income is positive but well below the full 2.0 because ~3.5 months of
        # accrued was priced into the bond on purchase
        assert 0.5 < ci < 1.5

    def test_income_positive(self):
        """Coupon income is always non-negative for a standard holding period."""
        coupon     = 0.03625
        maturity   = date(2032, 12, 31)
        settlement = date(2026, 3, 1)
        days       = 91

        ci = coupon_income_act_act(coupon, maturity, settlement, days)
        assert ci >= 0


# ---------------------------------------------------------------------------
# Carry
# ---------------------------------------------------------------------------

class TestCarry:

    def test_negative_carry_high_repo(self):
        """Low-coupon bond financed at high repo → negative carry."""
        result = carry(
            coupon      = 0.03625,
            dirty_price = 97.0,
            repo_rate   = 0.053,
            days        = 91,
            maturity    = date(2032, 12, 31),
            settlement  = date(2026, 3, 15),
        )
        assert result < 0, f"Expected negative carry, got {result}"

    def test_positive_carry_low_repo(self):
        """High-coupon bond financed at low repo → positive carry."""
        result = carry(
            coupon      = 0.06000,
            dirty_price = 102.0,
            repo_rate   = 0.010,
            days        = 91,
            maturity    = date(2033, 6, 30),
            settlement  = date(2026, 3, 15),
        )
        assert result > 0, f"Expected positive carry, got {result}"


# ---------------------------------------------------------------------------
# Implied repo and F* parity
# ---------------------------------------------------------------------------

class TestImpliedRepo:

    def test_implied_repo_returns_reasonable_value(self):
        """Implied repo should be in a plausible range for real market inputs."""
        settlement = date(2026, 3, 1)
        days       = (DELIVERY_DATE - settlement).days

        ir = implied_repo(
            cash_price    = 98.50,
            futures_price = 108.50,
            conv_factor   = 0.9163,
            coupon        = 0.04000,
            days          = days,
            settlement    = settlement,
            maturity      = date(2033, 6, 30),
        )
        # Implied repo for TY basis should be in a realistic repo range
        assert -0.20 < ir < 0.20, f"IR out of expected range: {ir:.4f}"

    def test_higher_futures_price_lowers_implied_repo(self):
        """Higher futures price increases the invoice price → higher IR, all else equal."""
        settlement = date(2026, 3, 1)
        days       = (DELIVERY_DATE - settlement).days
        kwargs = dict(
            cash_price  = 98.50,
            conv_factor = 0.9163,
            coupon      = 0.04000,
            days        = days,
            settlement  = settlement,
            maturity    = date(2033, 6, 30),
        )
        ir_low_f  = implied_repo(futures_price=107.00, **kwargs)
        ir_high_f = implied_repo(futures_price=110.00, **kwargs)
        assert ir_high_f > ir_low_f, (
            f"Higher futures → lower IR: high={ir_high_f:.6f}, low={ir_low_f:.6f}"
        )


class TestCtdTransitionThreshold:

    def test_f_star_parity(self):
        """
        F* must satisfy IR_A(F*) = IR_B(F*).

        This is the defining mathematical property of the threshold.
        After fixing the CA_x formula in ctd_transition_threshold() to
        use the same ACT/ACT coupon income as implied_repo(), this identity
        must hold exactly (within floating-point tolerance).
        """
        settlement = date(2026, 2, 1)
        days       = (DELIVERY_DATE - settlement).days

        ctd_coupon     = 0.04000
        runner_coupon  = 0.04125
        ctd_maturity   = date(2033, 6, 30)
        runner_maturity = date(2033, 9, 30)
        ctd_price      = 98.50
        runner_price   = 99.00
        ctd_cf         = conversion_factor(ctd_coupon,    ctd_maturity,    DELIVERY_DATE)
        runner_cf      = conversion_factor(runner_coupon, runner_maturity, DELIVERY_DATE)

        result = ctd_transition_threshold(
            ctd_price       = ctd_price,
            runner_price    = runner_price,
            ctd_cf          = ctd_cf,
            runner_cf       = runner_cf,
            ctd_coupon      = ctd_coupon,
            runner_coupon   = runner_coupon,
            days            = days,
            settlement      = settlement,
            ctd_maturity    = ctd_maturity,
            runner_maturity = runner_maturity,
        )

        f_star = result["transition_threshold_futures_price"]

        ir_ctd = implied_repo(
            cash_price  = ctd_price,
            futures_price = f_star,
            conv_factor = ctd_cf,
            coupon      = ctd_coupon,
            days        = days,
            settlement  = settlement,
            maturity    = ctd_maturity,
        )
        ir_runner = implied_repo(
            cash_price  = runner_price,
            futures_price = f_star,
            conv_factor = runner_cf,
            coupon      = runner_coupon,
            days        = days,
            settlement  = settlement,
            maturity    = runner_maturity,
        )

        assert abs(ir_ctd - ir_runner) < 1e-6, (
            f"F* parity failed: IR_CTD={ir_ctd:.8f}, IR_runner={ir_runner:.8f}, "
            f"diff={abs(ir_ctd - ir_runner):.2e}"
        )

    def test_denominator_near_zero_raises(self):
        """Identical bonds → denominator ≈ 0 → ValueError."""
        settlement = date(2026, 2, 1)
        days       = (DELIVERY_DATE - settlement).days
        with pytest.raises(ValueError, match="near zero"):
            ctd_transition_threshold(
                ctd_price=98.5, runner_price=98.5,
                ctd_cf=0.9163, runner_cf=0.9163,
                ctd_coupon=0.04, runner_coupon=0.04,
                days=days, settlement=settlement,
                ctd_maturity=date(2033, 6, 30),
                runner_maturity=date(2033, 6, 30),
            )


# ---------------------------------------------------------------------------
# Bond pricing
# ---------------------------------------------------------------------------

class TestBondPricing:

    def test_ytm_price_roundtrip(self):
        """price_bond → ytm_from_price must recover the original yield."""
        coupon     = 0.04375
        maturity   = date(2034, 5, 15)
        settlement = date(2026, 3, 1)
        ytm_in     = 0.04500

        price   = price_bond(coupon, maturity, ytm_in, settlement)
        ytm_out = ytm_from_price(coupon, maturity, price, settlement)

        assert abs(ytm_out - ytm_in) < 1e-8, f"Roundtrip error: {abs(ytm_out - ytm_in):.2e}"

    def test_par_bond_at_coupon_yield(self):
        """Bond priced at exactly its coupon rate should trade at par."""
        coupon     = 0.04500
        maturity   = date(2034, 5, 15)
        settlement = date(2026, 5, 15)  # on coupon date → zero accrued
        ytm        = 0.04500

        price = price_bond(coupon, maturity, ytm, settlement)
        assert abs(price - 100.0) < 0.01, f"Par bond off by {abs(price-100):.4f}"

    def test_price_decreases_with_yield(self):
        """Higher yield → lower price (standard bond convexity)."""
        coupon     = 0.04375
        maturity   = date(2034, 5, 15)
        settlement = date(2026, 3, 1)

        p_low  = price_bond(coupon, maturity, 0.03, settlement)
        p_high = price_bond(coupon, maturity, 0.06, settlement)

        assert p_low > p_high

    def test_dv01_positive(self):
        """DV01 must be positive for any standard bond."""
        d = dv01(0.04375, date(2034, 5, 15), 0.045, date(2026, 3, 1))
        assert d > 0

    def test_accrued_zero_on_coupon_date(self):
        """Accrued interest is zero on a coupon payment date."""
        coupon     = 0.04375
        maturity   = date(2034, 5, 15)
        settlement = date(2026, 5, 15)  # coupon date

        ai = accrued_interest(coupon, maturity, settlement)
        assert abs(ai) < 1e-10, f"Expected zero accrued on coupon date, got {ai}"

    def test_accrued_maximum_before_coupon(self):
        """Accrued just before coupon ≈ full semi-annual coupon."""
        coupon     = 0.04000
        maturity   = date(2033, 6, 30)
        # one day before Jun-30 coupon
        settlement = date(2026, 6, 29)

        ai = accrued_interest(coupon, maturity, settlement)
        # should be close to (0.04/2)*100 = 2.0
        assert 1.95 < ai < 2.0, f"Accrued near coupon date: {ai}"


# ---------------------------------------------------------------------------
# rank_basket integration
# ---------------------------------------------------------------------------

class TestRankBasket:

    def test_returns_twelve_bonds(self):
        """Basket must always have exactly 12 bonds."""
        from core.basket import get_basket
        basket = get_basket()
        yields = {b["cusip"]: 0.045 for b in basket}

        df = rank_basket(
            yields        = yields,
            futures_price = 108.50,
            repo_rate     = 0.053,
            settlement    = date(2026, 3, 1),
        )

        assert len(df) == 12

    def test_exactly_one_ctd(self):
        """Exactly one bond is flagged is_ctd=True."""
        from core.basket import get_basket
        basket = get_basket()
        yields = {b["cusip"]: 0.045 for b in basket}

        df = rank_basket(
            yields        = yields,
            futures_price = 108.50,
            repo_rate     = 0.053,
            settlement    = date(2026, 3, 1),
        )

        assert df["is_ctd"].sum() == 1
        assert df["is_ctd"].iloc[0] == True  # CTD is the first row (highest IR)

    def test_ctd_has_highest_implied_repo(self):
        """CTD is the bond with the maximum implied repo."""
        from core.basket import get_basket
        basket = get_basket()
        yields = {b["cusip"]: 0.045 for b in basket}

        df = rank_basket(
            yields        = yields,
            futures_price = 108.50,
            repo_rate     = 0.053,
            settlement    = date(2026, 3, 1),
        )

        ctd_ir  = df[df["is_ctd"]]["implied_repo_pct"].iloc[0]
        max_ir  = df["implied_repo_pct"].max()
        assert abs(ctd_ir - max_ir) < 1e-8

    def test_gross_basis_formula(self):
        """Verify gross_basis = cash_price - futures_price * CF for the CTD."""
        from core.basket import get_basket
        basket = get_basket()
        yields = {b["cusip"]: 0.045 for b in basket}

        futures_price = 108.50
        df = rank_basket(
            yields        = yields,
            futures_price = futures_price,
            repo_rate     = 0.053,
            settlement    = date(2026, 3, 1),
        )

        for _, row in df.iterrows():
            expected_gb = row["cash_price"] - futures_price * row["conv_factor"]
            assert abs(row["gross_basis"] - expected_gb) < 1e-4, (
                f"Gross basis mismatch for {row['label']}"
            )
