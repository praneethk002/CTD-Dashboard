"""
core/attribution.py

Security- and bucket-level performance-driver ("attribution") analysis for
the demo Fixed Income Performance & Analytics Workbench.

Decomposes each security's total return over a period into:
  carry               — the exact income return contribution: the change in
                        accrued interest over the period PLUS any coupon
                        cash actually paid during the period (accrued resets
                        to ~0 right after each coupon date, so cash paid
                        must be added back explicitly — see
                        core.pricing.coupon_cashflows_between), all divided
                        by the begin-of-period dirty price. This is exact,
                        not approximated — it's the same income figure that
                        total_return itself is built from.
  price_effect_approx — an EXPLICIT APPROXIMATION of the yield-driven price
                        change: -modified_duration * delta_yield
                                + 0.5 * convexity * delta_yield^2
                        Duration/convexity are held at their begin-of-period
                        values (a first/second-order Taylor expansion around
                        the starting yield, not a full re-pricing).
  residual            — total_return - carry - price_effect_approx. Because
                        carry is exact, this residual is purely the error of
                        the duration/convexity approximation versus the
                        bond's actual price return. Always computed and
                        always shown — NOT forced to zero, NOT hidden in a
                        fake category.

Each snapshot dict passed in must contain: date, price, yield,
accrued_interest, modified_duration, convexity, dv01.

No I/O. No database. Pure functions — all data passed as explicit args.
"""

from core.pricing import coupon_cashflows_between
from core.returns import simple_return


def price_effect_approx(modified_duration: float, convexity: float, delta_yield: float) -> float:
    """
    Approximate price return from a yield change using a duration + convexity
    Taylor expansion around the starting yield:

        price_effect ≈ -modified_duration * delta_yield
                        + 0.5 * convexity * delta_yield^2

    This is an APPROXIMATION — it does not account for curve-shape changes,
    the passage of time (roll-down), or convexity/duration drift over the
    period. Labeled explicitly wherever it is surfaced.
    """
    return -modified_duration * delta_yield + 0.5 * convexity * (delta_yield ** 2)


def carry_component(coupon: float, maturity, begin_date, end_date, begin_accrued: float,
                     end_accrued: float, begin_dirty_price: float) -> float:
    """
    Exact income return contribution over the period:

        carry = (end_accrued - begin_accrued + coupon_cash_paid) / begin_dirty_price

    coupon_cash_paid comes from core.pricing.coupon_cashflows_between() —
    any coupon(s) that fell inside (begin_date, end_date]. Combined with the
    price-only return, this reconciles exactly to total_return, so the
    residual in security_contribution() is purely the duration/convexity
    approximation's error, not a mix of income-accounting slop and pricing
    error.
    """
    coupon_cash = coupon_cashflows_between(coupon, maturity, begin_date, end_date)
    return (end_accrued - begin_accrued + coupon_cash) / begin_dirty_price


def security_contribution(security: dict, snap_begin: dict, snap_end: dict, weight_key: str) -> dict:
    """
    Decompose one security's return over a period and its contribution to
    the portfolio (or benchmark) total return.

    Args:
        security:   reference dict, must include security_id, coupon, and weight_key
        snap_begin: {date, price, yield, accrued_interest, modified_duration, convexity, dv01}
        snap_end:   same shape, at period end
        weight_key: "portfolio_weight" or "benchmark_weight"

    Returns:
        {security_id, weight, total_return, carry, price_effect_approx,
         residual, contribution_to_portfolio}
    """
    begin_dirty = snap_begin["price"] + snap_begin["accrued_interest"]
    end_dirty   = snap_end["price"] + snap_end["accrued_interest"]

    coupon_cash = coupon_cashflows_between(
        security["coupon"], security["maturity"], snap_begin["date"], snap_end["date"]
    )
    total_return = simple_return(begin_dirty, end_dirty + coupon_cash)

    carry = carry_component(
        security["coupon"], security["maturity"], snap_begin["date"], snap_end["date"],
        snap_begin["accrued_interest"], snap_end["accrued_interest"], begin_dirty,
    )

    delta_yield = snap_end["yield"] - snap_begin["yield"]
    effect = price_effect_approx(
        snap_begin["modified_duration"], snap_begin["convexity"], delta_yield
    )

    residual = total_return - carry - effect
    weight = security[weight_key]

    return {
        "security_id": security["security_id"],
        "label": security.get("label", security["security_id"]),
        "weight": weight,
        "total_return": total_return,
        "carry": carry,
        "price_effect_approx": effect,
        "residual": residual,
        "contribution_to_portfolio": weight * total_return,
    }


def bucket_attribution(securities: list[dict], snap_begin: dict, snap_end: dict, weight_key: str) -> list[dict]:
    """
    Group security_contribution results by classification (maturity bucket).

    Returns one dict per bucket:
        {bucket, weight, contribution_to_portfolio, carry, price_effect_approx, residual}
    where carry/price_effect_approx/residual are weight-averaged across the
    bucket's securities (weighted by each security's weight within the bucket).
    """
    by_bucket: dict[str, list[dict]] = {}
    for sec in securities:
        contrib = security_contribution(sec, snap_begin[sec["security_id"]], snap_end[sec["security_id"]], weight_key)
        bucket = sec.get("classification") or "Unclassified"
        by_bucket.setdefault(bucket, []).append(contrib)

    results = []
    for bucket, contribs in by_bucket.items():
        bucket_weight = sum(c["weight"] for c in contribs)
        bucket_contribution = sum(c["contribution_to_portfolio"] for c in contribs)
        if bucket_weight > 0:
            carry_avg = sum(c["carry"] * c["weight"] for c in contribs) / bucket_weight
            effect_avg = sum(c["price_effect_approx"] * c["weight"] for c in contribs) / bucket_weight
            residual_avg = sum(c["residual"] * c["weight"] for c in contribs) / bucket_weight
        else:
            carry_avg = effect_avg = residual_avg = 0.0

        results.append({
            "bucket": bucket,
            "weight": bucket_weight,
            "contribution_to_portfolio": bucket_contribution,
            "carry": carry_avg,
            "price_effect_approx": effect_avg,
            "residual": residual_avg,
            "securities": contribs,
        })

    return sorted(results, key=lambda r: r["bucket"])


def compare_portfolio_vs_benchmark(securities: list[dict], snap_begin: dict, snap_end: dict) -> list[dict]:
    """
    Per-bucket portfolio vs. benchmark contribution comparison.

    Returns one dict per bucket:
        {bucket, portfolio_contribution, benchmark_contribution, difference}
    """
    portfolio_buckets = {b["bucket"]: b["contribution_to_portfolio"]
                          for b in bucket_attribution(securities, snap_begin, snap_end, "portfolio_weight")}
    benchmark_buckets = {b["bucket"]: b["contribution_to_portfolio"]
                          for b in bucket_attribution(securities, snap_begin, snap_end, "benchmark_weight")}

    all_buckets = sorted(set(portfolio_buckets) | set(benchmark_buckets))
    return [
        {
            "bucket": bucket,
            "portfolio_contribution": portfolio_buckets.get(bucket, 0.0),
            "benchmark_contribution": benchmark_buckets.get(bucket, 0.0),
            "difference": portfolio_buckets.get(bucket, 0.0) - benchmark_buckets.get(bucket, 0.0),
        }
        for bucket in all_buckets
    ]
