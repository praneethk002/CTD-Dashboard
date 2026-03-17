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

    Steps per CME methodology:
      1. Compute remaining whole semi-annual periods N from delivery to maturity
      2. Compute fractional period z (months into current coupon period / 6)
      3. Price the bond at 6% using the standard Treasury pricing formula
      4. Subtract accrued interest to get clean price
      5. Round to 4 decimal places

    Args:
        coupon:   annual coupon rate as decimal (e.g. 0.04375)
        maturity: bond maturity date
        delivery: futures first delivery date (default TYM26)

    Returns:
        conversion factor as float (e.g. 0.9432)
    """
    c = coupon / 2          # semi-annual coupon per $1 face value
    y = CF_STANDARD_YIELD / 2   # semi-annual discount rate (3%)

    # months remaining from delivery to maturity
    months_remaining = (
        (maturity.year - delivery.year) * 12
        + (maturity.month - delivery.month)
    )

    # whole semi-annual coupon periods remaining
    N = months_remaining // 6

    # fractional period: months elapsed in current coupon period / 6
    z = (months_remaining % 6) / 6

    # present value of coupons (annuity) + present value of principal
    # discounted back N periods, then forward-adjusted for fractional period
    if N == 0:
        pv = (c + 1.0) / (1 + y) ** (1 - z)
    else:
        annuity = c * (1 - (1 + y) ** (-N)) / y
        pv = (annuity + (1 + y) ** (-N)) * (1 + y) ** z

    # subtract accrued interest for the fractional period
    accrued = c * z

    cf = pv - accrued

    return round(cf, 4)
