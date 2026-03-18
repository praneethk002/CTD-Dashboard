"""
core/ctd.py

CTD (Cheapest-to-Deliver) identification and transition analytics.

The CTD is the bond in the futures delivery basket that maximises the
implied repo rate for the short — i.e. the most profitable bond to
buy, finance overnight, and deliver into the futures contract.

Key outputs:
  rank_basket()              — ranks all 12 bonds by implied repo, flags CTD
  ctd_transition_threshold() — computes exact futures price F* at which CTD switches
  basis_dv01()               — DV01 of the net basis position

The F* formula (closed form, exact):

  At the transition point, implied repo of bond A = implied repo of bond B.
  Setting IR_A(F*) = IR_B(F*) and solving for F*:

  F* = (CA_B·P_A - CA_A·P_B) / (CF_A·P_B - CF_B·P_A)

  where CA_x = coupon_income_x + accrued_at_delivery_x  (total proceeds from coupon leg)
        P_x  = dirty cash price of bond x
        CF_x = conversion factor of bond x

  Derived by setting the numerators of each implied repo equal and solving for F.
  This is exact — not an approximation.

No I/O. No database. Pure functions only.
"""

from datetime import date

import pandas as pd

from core.basket import conversion_factor, DELIVERY_DATE
from core.carry import implied_repo, net_basis, gross_basis
from core.pricing import price_bond, dv01, accrued_interest


def rank_basket(
    yields: dict[str, float],
    futures_price: float,
    repo_rate: float,
    settlement: date,
    delivery: date = DELIVERY_DATE,
) -> pd.DataFrame:
    """
    Rank all basket bonds by implied repo rate.

    The bond with the highest implied repo is the CTD.
    Returns a DataFrame sorted descending by implied repo.

    Args:
        yields:        dict of {cusip: ytm} — yield for each basket bond
        futures_price: current TY futures price
        repo_rate:     GC repo rate as decimal
        settlement:    today's settlement date
        delivery:      futures delivery date

    Returns:
        DataFrame with columns:
          cusip, label, coupon, maturity, cash_price, conv_factor,
          gross_basis, carry, net_basis, net_basis_ticks,
          implied_repo_pct, is_ctd
        Sorted by implied_repo descending (CTD first).
    """
    from core.basket import get_basket

    days = (delivery - settlement).days
    rows = []

    for bond in get_basket():
        cusip   = bond["cusip"]
        coupon  = bond["coupon"]
        mat     = bond["maturity"]
        label   = bond["label"]

        ytm     = yields[cusip]
        cf      = conversion_factor(coupon, mat, delivery)
        accrued = accrued_interest(coupon, mat, settlement)
        price   = price_bond(coupon, mat, ytm, settlement)

        ir  = implied_repo(price, futures_price, cf, coupon, days, accrued)
        gb  = gross_basis(price, futures_price, cf)
        nb  = net_basis(price, futures_price, cf, coupon, repo_rate, days)
        d01 = dv01(coupon, mat, ytm, settlement)

        rows.append({
            "cusip":             cusip,
            "label":             label,
            "coupon":            coupon,
            "maturity":          mat.isoformat(),
            "cash_price":        round(price, 4),
            "conv_factor":       cf,
            "gross_basis":       round(gb, 4),
            "net_basis":         round(nb, 6),
            "net_basis_ticks":   round(nb * 32, 3),   # convert to 32nds
            "implied_repo_pct":  round(ir * 100, 4),  # convert to percentage
            "dv01":              round(d01, 4),
        })

    df = pd.DataFrame(rows)
    df = df.sort_values("implied_repo_pct", ascending=False).reset_index(drop=True)

    # flag the CTD — highest implied repo
    df["is_ctd"] = False
    df.loc[0, "is_ctd"] = True

    return df


def ctd_transition_threshold(
    ctd_price: float,
    runner_price: float,
    ctd_cf: float,
    runner_cf: float,
    ctd_coupon: float,
    runner_coupon: float,
    days: int,
    settlement: date,
    ctd_maturity: date,
    runner_maturity: date,
) -> dict:
    """
    Compute F* — the exact futures price at which the CTD switches.

    Derived by setting IR_A(F*) = IR_B(F*) and solving for F*:

      F* = (CA_B·P_A - CA_A·P_B) / (CF_A·P_B - CF_B·P_A)

    where CA_x = (coupon_x * days/365 * 100) + accrued_at_delivery_x
          P_x  = dirty cash price (clean + accrued today)

    Args:
        ctd_price:      clean cash price of CTD (% of par)
        runner_price:   clean cash price of runner-up (% of par)
        ctd_cf:         conversion factor of CTD
        runner_cf:      conversion factor of runner-up
        ctd_coupon:     annual coupon of CTD as decimal
        runner_coupon:  annual coupon of runner-up as decimal
        days:           calendar days to delivery
        settlement:     today's settlement date
        ctd_maturity:   maturity date of CTD
        runner_maturity: maturity date of runner-up

    Returns:
        dict with:
          transition_threshold_futures_price: F* (futures price at switch)
          distance_to_threshold_pts:          |current F - F*| in price points
          direction:                          "RALLY" or "SELLOFF"
    """
    # accrued interest today for dirty price
    ctd_accrued    = accrued_interest(ctd_coupon,    ctd_maturity,    settlement)
    runner_accrued = accrued_interest(runner_coupon, runner_maturity, settlement)

    # dirty prices
    p_a = ctd_price    + ctd_accrued
    p_b = runner_price + runner_accrued

    # coupon income + accrued at delivery
    # CA_x = coupon accrual over holding period (ACT/365) + accrued at delivery
    # For simplicity we use current accrued as proxy for accrued at delivery
    ca_a = (ctd_coupon    * 100 * days / 365) + ctd_accrued
    ca_b = (runner_coupon * 100 * days / 365) + runner_accrued

    # F* = (CA_B·P_A - CA_A·P_B) / (CF_A·P_B - CF_B·P_A)
    numerator   = ca_b * p_a - ca_a * p_b
    denominator = ctd_cf * p_b - runner_cf * p_a

    if abs(denominator) < 1e-10:
        raise ValueError("Denominator near zero — bonds are near-identical, no meaningful threshold")

    f_star = numerator / denominator

    return {
        "transition_threshold_futures_price": round(f_star, 4),
    }


def basis_dv01(
    bond_dv01: float,
    futures_dv01: float,
    conv_factor: float,
) -> float:
    """
    Compute the DV01 of a basis position (long bond, short futures).

    A basis position is: long 1 bond, short (1/CF) futures contracts.
    The CF hedge eliminates most duration risk but leaves a residual —
    the basis DV01 — because the futures contract is priced off the CTD,
    not off this specific bond.

    Hedge construction:
      To duration-neutral hedge 1 bond, short (1/CF) futures.
      Hedge DV01 = (1/CF) * futures_dv01 * CF = futures_dv01

    Residual:
      basis_dv01 = bond_dv01 - futures_dv01

    For the CTD itself this is near zero (the CF was calibrated to it).
    For non-CTD basket members the residual is meaningful and represents
    the rate sensitivity the CF hedge fails to cancel.

    Args:
        bond_dv01:    DV01 of this bond per $100 face value
        futures_dv01: DV01 of the futures contract (priced off the CTD)
        conv_factor:  CME conversion factor of this bond

    Returns:
        basis DV01 in price points
        Positive: bond is more rate-sensitive than the futures hedge
        Negative: bond is less rate-sensitive than the futures hedge
    """
    hedge_dv01 = (1.0 / conv_factor) * futures_dv01 * conv_factor
    return bond_dv01 - hedge_dv01
