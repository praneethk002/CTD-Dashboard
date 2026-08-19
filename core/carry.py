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
  Coupon accrual: ACT/ACT (ICMA) — consistent with pricing.py accrued_interest()
  Repo financing: ACT/360

No I/O. No database. Pure functions only.
"""

from datetime import date, timedelta

from core.pricing import accrued_interest, _coupon_dates


def coupon_income_act_act(
    coupon: float,
    maturity: date,
    settlement: date,
    days: int,
) -> float:
    """
    Compute coupon income over a holding period using ACT/ACT day count.

    Income = change in accrued interest over the period, plus any full
    coupon payment received if a coupon date falls within the period.

    Args:
        coupon:     annual coupon rate as decimal (e.g. 0.04375)
        maturity:   bond maturity date
        settlement: start of holding period
        days:       holding period in calendar days

    Returns:
        coupon income in price points per $100 face value
    """
    delivery = settlement + timedelta(days=days)

    ai_today    = accrued_interest(coupon, maturity, settlement)
    ai_delivery = accrued_interest(coupon, maturity, delivery)

    # Check whether a coupon payment falls within the holding period
    _, next_coupon = _coupon_dates(maturity, settlement)

    if settlement < next_coupon <= delivery:
        # A full semi-annual coupon was received during the holding period.
        # Income = full coupon - accrued surrendered at purchase + accrued at delivery.
        return (coupon / 2) * 100 - ai_today + ai_delivery

    return ai_delivery - ai_today


def gross_basis(
    cash_price: float,
    futures_price: float,
    conv_factor: float,
) -> float:
    """
    Compute gross basis.

    Gross basis = cash price - (futures price × conversion factor)

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
    dirty_price: float,
    repo_rate: float,
    days: int,
    maturity: date,
    settlement: date,
) -> float:
    """
    Compute carry — net income from holding a financed bond position.

    Carry = coupon income (ACT/ACT) - repo financing cost (ACT/360)

    The asymmetric day count is US market convention:
      - Coupon accrues on ACT/ACT (ICMA) — consistent with Treasury accrued interest
      - Repo is charged on ACT/360 (repo market standard, slightly higher cost)

    Args:
        coupon:      annual coupon rate as decimal (e.g. 0.04375)
        dirty_price: dirty cash price as % of par (clean + accrued today)
        repo_rate:   overnight repo rate as decimal (e.g. 0.053)
        days:        holding period in calendar days
        maturity:    bond maturity date
        settlement:  start of holding period (settlement date)

    Returns:
        carry in price points per $100 face value (e.g. 0.45)
        Positive = net income (coupon > financing cost)
        Negative = net cost  (financing cost > coupon)
    """
    ci             = coupon_income_act_act(coupon, maturity, settlement, days)
    financing_cost = dirty_price * repo_rate * (days / 360)

    return ci - financing_cost


def net_basis(
    cash_price: float,
    futures_price: float,
    conv_factor: float,
    coupon: float,
    repo_rate: float,
    days: int,
    maturity: date,
    settlement: date,
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
        maturity:      bond maturity date
        settlement:    today's settlement date

    Returns:
        net basis in price points (e.g. 0.0719)
    """
    gb          = gross_basis(cash_price, futures_price, conv_factor)
    ai          = accrued_interest(coupon, maturity, settlement)
    dirty_price = cash_price + ai
    c           = carry(coupon, dirty_price, repo_rate, days, maturity, settlement)

    return gb - c


def implied_repo(
    cash_price: float,
    futures_price: float,
    conv_factor: float,
    coupon: float,
    days: int,
    settlement: date,
    maturity: date,
) -> float:
    """
    Compute the implied repo rate.

    The implied repo is the synthetic financing rate implied by the
    cash-futures relationship. It answers: "If I buy this bond today
    and deliver it into the futures contract at expiry, what annualised
    return do I earn on my cash investment?"

    Formula (ACT/360 annualisation, repo market convention):
      dirty_price   = cash_price + accrued_at_settlement  (ACT/ACT)
      invoice_price = futures_price × conv_factor + accrued_at_delivery  (ACT/ACT)
      coupon_income = ACT/ACT income between settlement and delivery
      Implied repo  = (invoice + coupon_income - dirty_price) / dirty_price × (360/days)

    The bond with the HIGHEST implied repo is the CTD.

    Args:
        cash_price:    clean cash price as % of par
        futures_price: futures contract price
        conv_factor:   CME conversion factor
        coupon:        annual coupon rate as decimal
        days:          calendar days to futures delivery
        settlement:    today's settlement date
        maturity:      bond maturity date

    Returns:
        implied repo rate as decimal (e.g. 0.0552 = 5.52%)
    """
    delivery    = settlement + timedelta(days=days)

    ai_today    = accrued_interest(coupon, maturity, settlement)
    ai_delivery = accrued_interest(coupon, maturity, delivery)

    dirty_price   = cash_price + ai_today
    invoice_price = futures_price * conv_factor + ai_delivery
    ci            = coupon_income_act_act(coupon, maturity, settlement, days)

    total_proceeds = invoice_price + ci

    return ((total_proceeds - dirty_price) / dirty_price) * (360 / days)
