"""
core/carry.py

Cash-futures carry analytics for US Treasury basis trades.

Key concepts:
  Gross basis  = cash price - (futures price × conversion factor)
                 The raw price difference between the bond and the futures contract.
                 Always >= 0 for non-CTD bonds; approximately 0 for CTD.

  Carry        = coupon income - repo financing cost (over holding period)
                 What you earn (or pay) to hold the bond financed in the repo market.

  Net basis    = gross basis - carry
                 The residual after accounting for carry.
                 For the CTD, net basis ≈ 0 at fair value.
                 Positive net basis = bond is expensive relative to futures.

  Implied repo = the synthetic financing rate implied by the cash-futures relationship.
                 If implied repo > actual repo rate → positive carry → attractive to hold.
                 The bond with the HIGHEST implied repo is the CTD.

Day count conventions (US Treasury market standard):
  Coupon accrual: ACT/365
  Repo financing: ACT/360

No I/O. No database. Pure functions only.
"""

from datetime import date


def gross_basis(
    cash_price: float,
    futures_price: float,
    conv_factor: float,
) -> float:
    """
    Compute gross basis.

    Gross basis = cash price - (futures price × conversion factor)

    This is the raw difference between holding the bond outright versus
    the futures-equivalent position. The futures leg is adjusted by the
    conversion factor to make the comparison fair.

    Args:
        cash_price:    clean cash price as % of par (e.g. 99.50)
        futures_price: futures contract price (e.g. 108.50)
        conv_factor:   CME conversion factor for this bond (e.g. 0.9163)

    Returns:
        gross basis in price points (e.g. 0.1234)
    """
    return cash_price - (futures_price * conv_factor)


def carry(
    coupon: float,
    cash_price: float,
    repo_rate: float,
    days: int,
) -> float:
    """
    Compute carry — net income from holding a financed bond position.

    Carry = coupon income (ACT/365) - repo financing cost (ACT/360)

    The asymmetric day count is US market convention:
      - Coupon accrues on ACT/365 (more days = more coupon income)
      - Repo is charged on ACT/360 (repo market standard, slightly higher cost)

    Args:
        coupon:     annual coupon rate as decimal (e.g. 0.04375)
        cash_price: dirty cash price as % of par (clean + accrued)
        repo_rate:  overnight repo rate as decimal (e.g. 0.053)
        days:       holding period in calendar days

    Returns:
        carry in price points per $100 face value (e.g. 0.45)
        Positive = net income (coupon > financing cost)
        Negative = net cost  (financing cost > coupon)
    """
    coupon_income   = (coupon * 100) * (days / 365)       # ACT/365
    financing_cost  = cash_price * repo_rate * (days / 360)  # ACT/360

    return coupon_income - financing_cost


def net_basis(
    cash_price: float,
    futures_price: float,
    conv_factor: float,
    coupon: float,
    repo_rate: float,
    days: int,
) -> float:
    """
    Compute net basis.

    Net basis = gross basis - carry

    Net basis is the residual mispricing after accounting for carry.
    At fair value, the CTD's net basis = 0.
    A positive net basis means the bond is expensive relative to futures.

    Args:
        cash_price:    clean cash price as % of par
        futures_price: futures contract price
        conv_factor:   CME conversion factor
        coupon:        annual coupon rate as decimal
        repo_rate:     repo rate as decimal
        days:          days to futures delivery

    Returns:
        net basis in price points (e.g. 0.0719)
    """
    gb = gross_basis(cash_price, futures_price, conv_factor)

    # carry uses dirty price (clean + accrued), approximated here as cash_price
    # for simplicity — production would pass dirty price explicitly
    c  = carry(coupon, cash_price, repo_rate, days)

    return gb - c


def implied_repo(
    cash_price: float,
    futures_price: float,
    conv_factor: float,
    coupon: float,
    days: int,
    accrued: float = 0.0,
) -> float:
    """
    Compute the implied repo rate.

    The implied repo is the synthetic financing rate implied by the
    cash-futures relationship. It answers: "If I buy this bond today
    and deliver it into the futures contract at expiry, what annualised
    return do I earn on my cash investment?"

    Formula (ACT/360 annualisation, repo market convention):
      Invoice price = futures_price × conv_factor + accrued_at_delivery
      Implied repo  = (invoice + coupon_income - dirty_price) / dirty_price × (360/days)

    The bond with the HIGHEST implied repo is the CTD — the short will
    always deliver whichever bond maximises this return.

    Args:
        cash_price:    clean cash price as % of par
        futures_price: futures contract price
        conv_factor:   CME conversion factor
        coupon:        annual coupon rate as decimal
        days:          calendar days to futures delivery
        accrued:       accrued interest at delivery date (% of par)

    Returns:
        implied repo rate as decimal (e.g. 0.0552 = 5.52%)
    """
    dirty_price   = cash_price + accrued
    invoice_price = futures_price * conv_factor + accrued

    # coupon income earned between today and delivery (ACT/365)
    coupon_income = (coupon * 100) * (days / 365)

    total_proceeds = invoice_price + coupon_income

    return ((total_proceeds - dirty_price) / dirty_price) * (360 / days)
