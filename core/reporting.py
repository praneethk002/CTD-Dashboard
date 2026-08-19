"""
core/reporting.py

Assembles the structured performance report object for one horizon: the
same shape backs both the "report" API endpoint and the AI/template
commentary layer (core/commentary.py).

Pure — takes already-fetched data as explicit args (securities, begin/end
snapshots, and per-security history), mirroring the existing
core/ctd.py:rank_basket() convention of orchestrating pure calculations
without touching the database itself. DB fetching happens in api/app.py.

No I/O. No database.
"""

from datetime import date

from core.attribution import bucket_attribution, compare_portfolio_vs_benchmark, security_contribution
from core.controls import run_all_controls
from core.returns import TOTAL_NOTIONAL, compute_returns

TOP_N_CONTRIBUTORS = 3


def compute_risk_metrics(
    securities: list[dict],
    snapshot_by_id: dict[str, dict],
    weight_key: str = "portfolio_weight",
    total_notional: float = TOTAL_NOTIONAL,
) -> dict:
    """
    Weight-aggregate modified duration, yield, and dollar DV01 across the
    universe at a single point in time.

    Returns:
        {weighted_modified_duration, weighted_yield, dv01_usd}
    """
    total_weight = sum(s[weight_key] for s in securities)
    if total_weight == 0:
        return {"weighted_modified_duration": 0.0, "weighted_yield": 0.0, "dv01_usd": 0.0}

    weighted_duration = 0.0
    weighted_yield = 0.0
    dv01_usd = 0.0

    for sec in securities:
        snap = snapshot_by_id.get(sec["security_id"])
        if snap is None:
            continue
        w = sec[weight_key]
        weighted_duration += w * snap["modified_duration"]
        weighted_yield += w * snap["yield"]
        notional = w * total_notional
        dv01_usd += (notional / 100.0) * snap["dv01"]

    return {
        "weighted_modified_duration": weighted_duration / total_weight,
        "weighted_yield": weighted_yield / total_weight,
        "dv01_usd": dv01_usd,
    }


def _top_contributors(securities: list[dict], snap_begin: dict, snap_end: dict, weight_key: str) -> dict:
    contribs = [
        security_contribution(sec, snap_begin[sec["security_id"]], snap_end[sec["security_id"]], weight_key)
        for sec in securities
        if sec["security_id"] in snap_begin and sec["security_id"] in snap_end
    ]
    ranked = sorted(contribs, key=lambda c: c["contribution_to_portfolio"], reverse=True)
    return {
        "top_positive": ranked[:TOP_N_CONTRIBUTORS],
        "top_negative": list(reversed(ranked[-TOP_N_CONTRIBUTORS:])),
    }


def generate_report(
    securities: list[dict],
    snap_begin: dict[str, dict],
    snap_end: dict[str, dict],
    history: dict[str, list[dict]],
    horizon: str,
) -> dict:
    """
    Assemble the full structured report for one horizon.

    Args:
        securities: reference data
        snap_begin: {security_id: snapshot} at period start
        snap_end:   {security_id: snapshot} at period end (also treated as
                    the "as of" / latest snapshot for controls + risk metrics)
        history:    {security_id: [snapshot, ...chronological]} for controls
        horizon:    "1D" | "MTD" | "QTD" | "YTD"

    Returns:
        {
          horizon, as_of,
          performance, risk_metrics,
          contributors: {top_positive, top_negative},
          attribution_summary: {buckets, portfolio_vs_benchmark},
          data_quality: {status, issues},
          executive_summary,
        }
    """
    performance = compute_returns(securities, snap_begin, snap_end)
    risk_metrics = compute_risk_metrics(securities, snap_end, "portfolio_weight")
    contributors = _top_contributors(securities, snap_begin, snap_end, "portfolio_weight")

    attribution_summary = {
        "buckets": bucket_attribution(securities, snap_begin, snap_end, "portfolio_weight"),
        "portfolio_vs_benchmark": compare_portfolio_vs_benchmark(securities, snap_begin, snap_end),
    }

    data_quality = run_all_controls(securities, snap_end, history)

    as_of = max((s["date"] for s in snap_end.values()), default=date.today())

    executive_summary = {
        "headline": (
            f"Portfolio returned {performance['portfolio_return'] * 100:.2f}% vs. benchmark "
            f"{performance['benchmark_return'] * 100:.2f}% over {horizon} "
            f"(active return {performance['active_return_bps']:+.1f} bps). "
            f"Data quality: {data_quality['status']}"
            + (f" ({len(data_quality['issues'])} item(s))." if data_quality["issues"] else ".")
        ),
        "portfolio_return": performance["portfolio_return"],
        "benchmark_return": performance["benchmark_return"],
        "active_return_bps": performance["active_return_bps"],
        "data_quality_status": data_quality["status"],
    }

    return {
        "horizon": horizon,
        "as_of": as_of.isoformat(),
        "performance": performance,
        "risk_metrics": risk_metrics,
        "contributors": contributors,
        "attribution_summary": attribution_summary,
        "data_quality": data_quality,
        "executive_summary": executive_summary,
    }
