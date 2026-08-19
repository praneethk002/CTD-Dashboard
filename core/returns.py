"""
core/returns.py

Portfolio, benchmark, and active return calculation for the demo
Fixed Income Performance & Analytics Workbench.

Methodology note (important, read before extending):
  This demo portfolio has NO EXTERNAL cash flows (no client contributions,
  withdrawals, or rebalancing trades) during the measurement window — it is
  a static buy-and-hold set of weights over the seeded history. Because
  there are no external flows, a simple beginning-vs-ending market value
  return is the right level of methodology here; there is no need for
  Modified Dietz or a daily-linked time-weighted return to handle flow
  timing.

  If external cash flows were introduced (e.g. a client contribution
  mid-period), this simple return would be wrong — you'd need either
  Modified Dietz (flow-weighted single-period approximation) or a
  daily-linked TWR (geometrically chain sub-period returns split at each
  flow date). Neither is implemented here because the demo has no external
  flows to require it; documenting this rather than either silently
  ignoring the issue or over-building unused machinery.

  There IS an internal cash flow that must still be handled correctly:
  coupon payments. accrued_interest() resets to ~0 immediately after each
  coupon date, so comparing begin-vs-end dirty price alone silently drops
  any coupon cash paid mid-period (a real bug caught by hand-checking the
  first version of this module against a bond with a coupon date inside
  the measurement window). Coupon cash paid during the period is computed
  via core.pricing.coupon_cashflows_between() and added back before taking
  the return — see _aggregate_coupon_cash() below.

No I/O. No database. Pure functions — all data passed as explicit args.
"""

from datetime import date

TOTAL_NOTIONAL = 10_000_000.0  # demo total par, USD — matches core/portfolio.py

HORIZONS = ("1D", "MTD", "QTD", "YTD")


def simple_return(begin_value: float, end_value: float) -> float:
    """Beginning-to-ending return with no external flows: end/begin - 1."""
    if begin_value == 0:
        raise ValueError("begin_value is zero — cannot compute a return")
    return (end_value / begin_value) - 1.0


def security_market_value(notional: float, price: float, accrued: float) -> float:
    """Market value of one security given its notional and clean price + accrued (% of par)."""
    return notional * (price + accrued) / 100.0


def aggregate_market_value(
    securities: list[dict],
    snapshot_by_id: dict[str, dict],
    weight_key: str,
    total_notional: float = TOTAL_NOTIONAL,
) -> float:
    """
    Sum market value across all securities using either portfolio_weight or
    benchmark_weight to derive each security's notional.

    Args:
        securities:     list of security reference dicts (must include
                         security_id and weight_key)
        snapshot_by_id: {security_id: {price, accrued_interest, ...}} for one date
        weight_key:     "portfolio_weight" or "benchmark_weight"
    """
    total = 0.0
    for sec in securities:
        snap = snapshot_by_id.get(sec["security_id"])
        if snap is None:
            continue
        notional = sec[weight_key] * total_notional
        total += security_market_value(notional, snap["price"], snap["accrued_interest"])
    return total


def _aggregate_coupon_cash(
    securities: list[dict],
    begin_date: date,
    end_date: date,
    weight_key: str,
    total_notional: float = TOTAL_NOTIONAL,
) -> float:
    """Total coupon cash (in dollars) paid across the universe during (begin_date, end_date]."""
    from core.pricing import coupon_cashflows_between

    total = 0.0
    for sec in securities:
        notional = sec[weight_key] * total_notional
        pts = coupon_cashflows_between(sec["coupon"], sec["maturity"], begin_date, end_date)
        total += notional * pts / 100.0
    return total


def compute_returns(
    securities: list[dict],
    snap_begin: dict[str, dict],
    snap_end: dict[str, dict],
) -> dict:
    """
    Compute portfolio return, benchmark return, and active return for one period.

    Ending market value is adjusted to add back any coupon cash paid during
    the period (held as cash through period end — the simplest defensible
    convention for a demo with no reinvestment engine) so returns aren't
    silently understated whenever a coupon date falls inside the window.

    Args:
        securities: reference data (core.portfolio.get_securities() or DB equivalent)
        snap_begin: {security_id: snapshot} at period start (each snapshot must
                    include a "date" key)
        snap_end:   {security_id: snapshot} at period end

    Returns:
        {
          portfolio_return, benchmark_return, active_return_bps,
          portfolio_mv_begin, portfolio_mv_end,
          benchmark_mv_begin, benchmark_mv_end,
          portfolio_coupon_cash, benchmark_coupon_cash,
        }
    """
    begin_date = next(iter(snap_begin.values()))["date"]
    end_date = next(iter(snap_end.values()))["date"]

    portfolio_mv_begin = aggregate_market_value(securities, snap_begin, "portfolio_weight")
    portfolio_mv_end   = aggregate_market_value(securities, snap_end,   "portfolio_weight")
    benchmark_mv_begin = aggregate_market_value(securities, snap_begin, "benchmark_weight")
    benchmark_mv_end   = aggregate_market_value(securities, snap_end,   "benchmark_weight")

    portfolio_coupon_cash = _aggregate_coupon_cash(securities, begin_date, end_date, "portfolio_weight")
    benchmark_coupon_cash = _aggregate_coupon_cash(securities, begin_date, end_date, "benchmark_weight")

    portfolio_return = simple_return(portfolio_mv_begin, portfolio_mv_end + portfolio_coupon_cash)
    benchmark_return = simple_return(benchmark_mv_begin, benchmark_mv_end + benchmark_coupon_cash)

    return {
        "portfolio_return":      portfolio_return,
        "benchmark_return":      benchmark_return,
        "active_return_bps":     (portfolio_return - benchmark_return) * 10_000,
        "portfolio_mv_begin":    portfolio_mv_begin,
        "portfolio_mv_end":      portfolio_mv_end,
        "benchmark_mv_begin":    benchmark_mv_begin,
        "benchmark_mv_end":      benchmark_mv_end,
        "portfolio_coupon_cash": portfolio_coupon_cash,
        "benchmark_coupon_cash": benchmark_coupon_cash,
    }


def horizon_start_date(horizon: str, as_of: date) -> date:
    """
    Return the calendar boundary date for a horizon, given an as-of date.
    "1D" has no fixed calendar boundary — callers should instead use the
    prior available trading date (see data.portfolio_db.PortfolioDB).
    """
    if horizon == "MTD":
        return date(as_of.year, as_of.month, 1)
    if horizon == "QTD":
        q_start_month = ((as_of.month - 1) // 3) * 3 + 1
        return date(as_of.year, q_start_month, 1)
    if horizon == "YTD":
        return date(as_of.year, 1, 1)
    raise ValueError(f"horizon_start_date does not apply to '{horizon}' — it has no calendar boundary")


def resolve_supported_horizons(earliest_date: date, latest_date: date) -> list[str]:
    """
    Given the actual date range of seeded/available history, return the subset
    of HORIZONS that can be computed without fabricating data. A horizon is
    supported if its calendar start boundary falls on or after the earliest
    available date. "1D" is supported whenever at least two dates exist.
    """
    supported = []
    if earliest_date < latest_date:
        supported.append("1D")
    for h in ("MTD", "QTD", "YTD"):
        if horizon_start_date(h, latest_date) >= earliest_date:
            supported.append(h)
    return supported
