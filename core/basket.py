"""
core/basket.py

TY (10-year Treasury futures) delivery basket.

The CME defines eligibility for TY delivery as: Treasury notes/bonds with
original maturity <= 10 years and remaining maturity between 6.5 and 10 years
at the first day of the delivery month.

This module:
  - Enumerates the 12 bonds currently eligible for TYM26 delivery
  - Computes each bond's CME conversion factor

Conversion Factor (CF):
  The CF adjusts the invoice price the short receives when delivering a bond.
  It is computed by pricing the bond at a hypothetical 6% yield (CME standard),
  divided by par (100). This makes all bonds roughly equivalent to deliver —
  but only roughly, which is what creates the CTD opportunity.

  CF = price_at_6pct_yield / 100

No I/O. No database. Pure functions only.
"""

import calendar
from datetime import date
from typing import TypedDict

import numpy as np


class Bond(TypedDict):
    cusip: str           # unique bond identifier
    coupon: float        # annual coupon rate, decimal (e.g. 0.04375)
    maturity: date       # maturity date
    label: str           # human-readable label e.g. "4.375% Feb-34"


# TYM26 delivery basket — 12 bonds eligible for June 2026 delivery
# Coupons range 3.625% - 4.625%, maturities Dec 2032 - Jun 2036
# Source: CME TY contract specifications
BASKET: list[Bond] = [
    {"cusip": "912828YV6", "coupon": 0.03625, "maturity": date(2032, 12, 31), "label": "3.625% Dec-32"},
    {"cusip": "91282CAF9", "coupon": 0.03750, "maturity": date(2033,  3, 31), "label": "3.750% Mar-33"},
    {"cusip": "91282CBH3", "coupon": 0.04000, "maturity": date(2033,  6, 30), "label": "4.000% Jun-33"},
    {"cusip": "91282CCF6", "coupon": 0.04125, "maturity": date(2033,  9, 30), "label": "4.125% Sep-33"},
    {"cusip": "91282CDA6", "coupon": 0.04500, "maturity": date(2033, 11, 15), "label": "4.500% Nov-33"},
    {"cusip": "91282CDB4", "coupon": 0.04250, "maturity": date(2034,  2, 15), "label": "4.250% Feb-34"},
    {"cusip": "91282CDC2", "coupon": 0.04375, "maturity": date(2034,  5, 15), "label": "4.375% May-34"},
    {"cusip": "91282CDD0", "coupon": 0.04250, "maturity": date(2034,  8, 15), "label": "4.250% Aug-34"},
    {"cusip": "91282CDE8", "coupon": 0.04000, "maturity": date(2034, 11, 15), "label": "4.000% Nov-34"},
    {"cusip": "91282CEA5", "coupon": 0.04375, "maturity": date(2035,  2, 15), "label": "4.375% Feb-35"},
    {"cusip": "91282CEB3", "coupon": 0.04625, "maturity": date(2035,  5, 15), "label": "4.625% May-35"},
    {"cusip": "91282CEC1", "coupon": 0.04500, "maturity": date(2036,  6, 15), "label": "4.500% Jun-36"},
]

# TYM26 futures delivery date — first business day of June 2026
DELIVERY_DATE = date(2026, 6, 1)

# CME standard yield for conversion factor computation
CF_STANDARD_YIELD = 0.06


def get_basket() -> list[Bond]:
    """Return the full TYM26 delivery basket."""
    return BASKET


def conversion_factor(coupon: float, maturity: date, delivery: date = DELIVERY_DATE) -> float:
    """
    Compute the CME conversion factor for a bond.

    The CF is the clean price of the bond (per $1 face value) if it were
    priced at exactly 6% yield on the first day of the delivery month,
    rounded to 4 decimal places per CME convention.

    CME methodology (two-case algorithm):
      1. Count whole months from first day of delivery month to maturity month.
      2. Round DOWN to the nearest 3-month multiple (CME spec).
      3. Express as n_periods whole semi-annual periods + z_months (0 or 3).
      4. Case z_months == 0 (no stub):
           pv = annuity(n_periods, 3%) + discount(n_periods, 3%)
           accrued = 0
      5. Case z_months == 3 (3-month stub):
           Price at stub date (3 months out) = first_coupon + annuity + discount
           Discount back 0.5 periods: pv = pv_at_stub * v^0.5
           accrued = semi_coupon * 0.5  (3 months into coupon period)
      6. CF = pv - accrued, rounded to 4 decimal places.

    Args:
        coupon:   annual coupon rate as decimal (e.g. 0.04375)
        maturity: bond maturity date
        delivery: futures first delivery date (default TYM26)

    Returns:
        conversion factor as float (e.g. 0.9432)
    """
    semi_coupon = coupon / 2              # semi-annual coupon per $1 face value
    semi_yield  = CF_STANDARD_YIELD / 2  # 3% semi-annual discount rate
    v           = 1.0 / (1.0 + semi_yield)

    # whole months from first day of delivery month to maturity month
    months_remaining = (
        (maturity.year - delivery.year) * 12
        + (maturity.month - delivery.month)
    )

    # CME: round DOWN to the nearest 3-month multiple
    months_rounded = (months_remaining // 3) * 3

    n_periods = months_rounded // 6  # whole semi-annual coupon periods
    z_months  = months_rounded % 6   # 0 or 3 months stub

    annuity = (
        semi_coupon * (1.0 - v ** n_periods) / semi_yield
        if n_periods > 0
        else 0.0
    )

    if z_months == 0:
        # no stub — evaluated at a coupon date, no accrued
        pv      = annuity + v ** n_periods
        accrued = 0.0
    else:
        # 3-month stub — next coupon is 3 months (half a period) away
        # value at stub date = immediate coupon + remaining annuity + principal
        pv_at_stub = semi_coupon + annuity + v ** n_periods
        # discount back half a semi-annual period
        pv      = pv_at_stub * v ** 0.5
        # accrued at delivery = 3 months into a 6-month coupon period
        accrued = semi_coupon * 0.5

    return round(pv - accrued, 4)
