"""
core/pricing.py

Bond pricing analytics for US Treasury securities.

All functions assume:
  - Semi-annual coupon payments (US Treasury convention)
  - Clean price as percentage of par (e.g. 99.50 means $99.50 per $100 face)
  - Yields as decimals (e.g. 0.045 = 4.5%)

Pricing model: standard discounted cash flow (DCF).
Each future cash flow is discounted at the yield, semi-annually compounded.

Clean price vs dirty price:
  Dirty price = present value of all cash flows
  Clean price = dirty price - accrued interest
  Markets quote clean prices; settlement uses dirty prices.

No I/O. No database. Pure functions only.
"""

import calendar
from datetime import date

import numpy as np
from scipy.optimize import brentq


def price_bond(
    coupon: float,
    maturity: date,
    ytm: float,
    settlement: date,
) -> float:
    """
    Compute the clean price of a Treasury bond.

    Uses standard semi-annual DCF pricing:
      dirty_price = sum(coupon/2 / (1 + ytm/2)^t) + 100 / (1 + ytm/2)^n
      clean_price = dirty_price - accrued_interest

    Args:
        coupon:     annual coupon rate as decimal (e.g. 0.04375)
        maturity:   bond maturity date
        ytm:        yield to maturity as decimal (e.g. 0.045)
        settlement: settlement date (typically trade date + 1 business day)

    Returns:
        clean price as percentage of par (e.g. 98.75)
    """
    cash_flows, periods = _build_cash_flows(coupon, maturity, settlement)

    # discount each cash flow: CF / (1 + y/2)^t
    half_yield = ytm / 2
    discount_factors = (1 + half_yield) ** (-periods)
    dirty_price = float(np.dot(cash_flows, discount_factors))

    accrued = accrued_interest(coupon, maturity, settlement)

    return dirty_price - accrued


def dv01(
    coupon: float,
    maturity: date,
    ytm: float,
    settlement: date,
) -> float:
    """
    Compute DV01 — dollar value of 1 basis point.

    DV01 is the price change for a 1bp (0.0001) increase in yield.
    Computed via central difference for accuracy:
      DV01 = (price(ytm - 1bp) - price(ytm + 1bp)) / 2

    Args:
        coupon:     annual coupon rate as decimal
        maturity:   bond maturity date
        ytm:        yield to maturity as decimal
        settlement: settlement date

    Returns:
        DV01 in price points per $100 face value (e.g. 0.085)
    """
    bump = 0.0001  # 1 basis point

    price_down = price_bond(coupon, maturity, ytm - bump, settlement)
    price_up   = price_bond(coupon, maturity, ytm + bump, settlement)

    return (price_down - price_up) / 2


def modified_duration(
    coupon: float,
    maturity: date,
    ytm: float,
    settlement: date,
) -> float:
    """
    Compute modified duration.

    Modified duration = DV01 / (clean_price / 10000)
    It represents the percentage price change per 1% yield move.

    Args:
        coupon:     annual coupon rate as decimal
        maturity:   bond maturity date
        ytm:        yield to maturity as decimal
        settlement: settlement date

    Returns:
        modified duration in years (e.g. 7.43)
    """
    d = dv01(coupon, maturity, ytm, settlement)
    p = price_bond(coupon, maturity, ytm, settlement)

    # DV01 = ModDur * Price / 10000
    # => ModDur = DV01 * 10000 / Price
    return d * 10_000 / p


def convexity(
    coupon: float,
    maturity: date,
    ytm: float,
    settlement: date,
) -> float:
    """
    Compute convexity — the second derivative of price w.r.t. yield.

    Convexity = (price(ytm+1bp) + price(ytm-1bp) - 2*price) / (price * bump^2)

    Captures the curvature of the price-yield relationship.
    Always positive for standard bonds — convexity always benefits the holder.

    Args:
        coupon:     annual coupon rate as decimal
        maturity:   bond maturity date
        ytm:        yield to maturity as decimal
        settlement: settlement date

    Returns:
        convexity (dimensionless, e.g. 68.4)
    """
    bump = 0.0001  # 1 basis point

    p      = price_bond(coupon, maturity, ytm,        settlement)
    p_up   = price_bond(coupon, maturity, ytm + bump, settlement)
    p_down = price_bond(coupon, maturity, ytm - bump, settlement)

    return (p_up + p_down - 2 * p) / (p * bump ** 2)


def ytm_from_price(
    coupon: float,
    maturity: date,
    clean_price: float,
    settlement: date,
) -> float:
    """
    Compute yield to maturity from a clean price.

    Inverts price_bond() using Brent's method (scipy.optimize.brentq).
    Brent's method is preferred over Newton-Raphson here because it is
    guaranteed to converge on a bracketed interval — no risk of divergence.

    Args:
        coupon:      annual coupon rate as decimal
        maturity:    bond maturity date
        clean_price: observed market clean price (% of par)
        settlement:  settlement date

    Returns:
        yield to maturity as decimal (e.g. 0.0452)
    """
    def objective(y):
        return price_bond(coupon, maturity, y, settlement) - clean_price

    # search between 0.1bp and 30% — covers all realistic Treasury yields
    return brentq(objective, 0.0001, 0.30, xtol=1e-10)


def accrued_interest(
    coupon: float,
    maturity: date,
    settlement: date,
) -> float:
    """
    Compute accrued interest using ACT/ACT (ICMA) day count convention.

    Accrued = (coupon/2) * (days since last coupon / days in coupon period)

    US Treasuries use actual/actual day count — we count real calendar days
    in the coupon period, not a fixed 180-day assumption.

    Args:
        coupon:     annual coupon rate as decimal
        maturity:   bond maturity date
        settlement: settlement date

    Returns:
        accrued interest as percentage of par (e.g. 1.23)
    """
    prev_coupon, next_coupon = _coupon_dates(maturity, settlement)

    days_since_last  = (settlement - prev_coupon).days
    days_in_period   = (next_coupon - prev_coupon).days

    return (coupon / 2) * (days_since_last / days_in_period) * 100


def coupon_cashflows_between(
    coupon: float,
    maturity: date,
    begin: date,
    end: date,
) -> float:
    """
    Total coupon cash paid strictly after `begin` and on/before `end`.

    Needed because accrued_interest() resets to ~0 immediately after each
    coupon date — a plain begin-vs-end dirty-price comparison silently drops
    any coupon cash paid mid-period. Total return over a period must add
    this back: total_return = (end_dirty + coupon_cash_between - begin_dirty)
    / begin_dirty.

    Args:
        coupon:   annual coupon rate as decimal
        maturity: bond maturity date
        begin:    period start date (exclusive)
        end:      period end date (inclusive)

    Returns:
        total coupon cash paid in the window, as percentage of par
    """
    if begin >= maturity:
        return 0.0

    _, next_coupon = _coupon_dates(maturity, begin)
    semi_coupon = (coupon / 2) * 100
    total = 0.0

    d = next_coupon
    while d <= end and d <= maturity:
        total += semi_coupon
        m = d.month + 6
        y = d.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        try:
            d = date(y, m, d.day)
        except ValueError:
            last_day = calendar.monthrange(y, m)[1]
            d = date(y, m, last_day)

    return total


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _coupon_dates(maturity: date, settlement: date) -> tuple[date, date]:
    """
    Find the previous and next coupon dates relative to settlement.

    US Treasuries pay coupons on the same day/month as maturity, every 6 months.
    E.g. a bond maturing Feb-15 pays coupons on Feb-15 and Aug-15 each year.
    """
    coupon_month = maturity.month
    coupon_day   = maturity.day

    # generate candidate coupon dates around settlement year
    candidates = []
    for year in range(settlement.year - 1, settlement.year + 2):
        for month_offset in [0, 6]:
            m = coupon_month + month_offset
            y = year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            try:
                candidates.append(date(y, m, coupon_day))
            except ValueError:
                # handle month-end edge cases (e.g. Feb 30 doesn't exist)
                last_day = calendar.monthrange(y, m)[1]
                candidates.append(date(y, m, last_day))

    candidates.sort()

    # find the coupon period that brackets settlement
    for i, d in enumerate(candidates):
        if d > settlement:
            return candidates[i - 1], d

    raise ValueError(f"Could not find coupon dates for settlement {settlement}")


def _build_cash_flows(
    coupon: float,
    maturity: date,
    settlement: date,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Build arrays of cash flows and their time periods (in semi-annual units).

    Returns:
        cash_flows: array of coupon payments + final principal
        periods:    array of fractional semi-annual periods to each cash flow
    """
    prev_coupon, next_coupon = _coupon_dates(maturity, settlement)

    # fractional period from settlement to next coupon
    days_to_next   = (next_coupon - settlement).days
    days_in_period = (next_coupon - prev_coupon).days
    first_period   = days_to_next / days_in_period  # fraction of a semi-annual period

    # generate all future coupon dates from next_coupon to maturity
    coupon_dates = []
    d = next_coupon
    while d <= maturity:
        coupon_dates.append(d)
        # advance by 6 months
        m = d.month + 6
        y = d.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        try:
            d = date(y, m, d.day)
        except ValueError:
            last_day = calendar.monthrange(y, m)[1]
            d = date(y, m, last_day)

    n = len(coupon_dates)

    # semi-annual coupon per $100 face value
    semi_coupon = (coupon / 2) * 100

    cash_flows = np.full(n, semi_coupon)
    cash_flows[-1] += 100.0  # add principal repayment at maturity

    # periods: first cash flow at first_period, then 1, 2, 3... semi-annual periods later
    periods = first_period + np.arange(n)

    return cash_flows, periods
