"""
core/controls.py

Deterministic data-quality controls for the demo Fixed Income Performance &
Analytics Workbench.

Every check here is a plain Python rule over already-fetched data — there is
NO LLM involvement in deciding whether financial data is correct. AI is only
ever used downstream (core/commentary.py) to narrate results this layer has
already computed and flagged.

Each check returns an "Issue" (or None / a list of Issues):
    {issue, security_id, reason, severity, suggested_action}

Overall status from run_all_controls() is PASS or REVIEW — never a wall of
individual green checks.

No I/O. No database. Pure functions — all data passed as explicit args.
"""

from datetime import date

from core.pricing import _coupon_dates

WEIGHT_SUM_TOLERANCE = 0.0005   # 5 bps tolerance on weights summing to 100%
ABNORMAL_RETURN_Z    = 2.5      # flag daily returns beyond 2.5 std dev of trailing history


def _issue(issue, security_id, reason, severity, suggested_action):
    return {
        "issue": issue,
        "security_id": security_id,
        "reason": reason,
        "severity": severity,
        "suggested_action": suggested_action,
    }


def check_stale_price(prev_snapshot: dict, curr_snapshot: dict) -> dict | None:
    """Flag when price AND yield are unchanged from the prior snapshot date."""
    if prev_snapshot["price"] == curr_snapshot["price"] and prev_snapshot["yield"] == curr_snapshot["yield"]:
        return _issue(
            "Stale price", curr_snapshot.get("security_id"),
            f"Price ({curr_snapshot['price']}) and yield ({curr_snapshot['yield']}) unchanged from prior snapshot "
            f"({prev_snapshot.get('date')} -> {curr_snapshot.get('date')}).",
            "REVIEW",
            "Confirm the price feed refreshed for this security before using it in reporting.",
        )
    return None


def check_missing_classification(security: dict) -> dict | None:
    if not security.get("classification"):
        return _issue(
            "Missing classification", security["security_id"],
            "No maturity-bucket / asset-class classification on file.",
            "REVIEW",
            "Assign a classification before including this security in bucket-level attribution.",
        )
    return None


def check_missing_yield(snapshot: dict) -> dict | None:
    if snapshot.get("yield") is None:
        return _issue(
            "Missing yield", snapshot.get("security_id"),
            "No yield recorded for this snapshot date.",
            "REVIEW",
            "Backfill or re-derive yield from price before computing returns/attribution.",
        )
    return None


def check_duplicate_security(securities: list[dict]) -> list[dict]:
    seen: dict[str, int] = {}
    for s in securities:
        seen[s["security_id"]] = seen.get(s["security_id"], 0) + 1
    return [
        _issue(
            "Duplicate security", sec_id,
            f"security_id appears {count} times in the reference table.",
            "REVIEW",
            "De-duplicate the holdings reference data before computing portfolio-level metrics.",
        )
        for sec_id, count in seen.items() if count > 1
    ]


def check_weights_sum(securities: list[dict], weight_key: str) -> dict | None:
    total = sum(s[weight_key] for s in securities)
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        return _issue(
            f"{weight_key.replace('_', ' ').title()}s do not sum to 100%", None,
            f"{weight_key} totals {total * 100:.2f}% across all securities (expected 100.00%).",
            "REVIEW",
            "Reconcile weights against the source holdings file before publishing performance.",
        )
    return None


def check_abnormal_return(security_id: str, daily_return: float, trailing_returns: list[float]) -> dict | None:
    """Flag daily_return if it's more than ABNORMAL_RETURN_Z std deviations from the
    mean of trailing_returns (trailing_returns should exclude daily_return itself)."""
    n = len(trailing_returns)
    if n < 5:
        return None  # not enough history to judge "abnormal"

    mean = sum(trailing_returns) / n
    variance = sum((r - mean) ** 2 for r in trailing_returns) / n
    std = variance ** 0.5
    if std == 0:
        return None

    z = (daily_return - mean) / std
    if abs(z) > ABNORMAL_RETURN_Z:
        return _issue(
            "Abnormal daily return", security_id,
            f"Daily return {daily_return * 100:.2f}% is {abs(z):.1f} std dev from its trailing "
            f"{n}-day mean ({mean * 100:.2f}%).",
            "REVIEW",
            "Verify the underlying price/yield move against an independent source before reporting.",
        )
    return None


def check_missing_benchmark_mapping(security: dict) -> dict | None:
    if security.get("benchmark_weight") is None:
        return _issue(
            "Missing benchmark mapping", security["security_id"],
            "No benchmark_weight assigned for this security.",
            "REVIEW",
            "Map this holding to the benchmark before computing active return.",
        )
    return None


def check_invalid_maturity(security: dict, as_of: date) -> dict | None:
    maturity = security.get("maturity")
    if maturity is None:
        return _issue(
            "Missing maturity", security["security_id"],
            "No maturity date on file.",
            "REVIEW",
            "Backfill maturity from the security master before pricing this holding.",
        )
    if maturity <= as_of:
        return _issue(
            "Invalid maturity", security["security_id"],
            f"Maturity {maturity.isoformat()} is on or before the as-of date {as_of.isoformat()}.",
            "REVIEW",
            "Confirm whether this security has matured/rolled and should be removed from the holdings file.",
        )
    return None


def check_missing_coupon(security: dict) -> dict | None:
    if security.get("coupon") is None:
        return _issue(
            "Missing coupon", security["security_id"],
            "No coupon rate on file.",
            "REVIEW",
            "Backfill coupon from the security master before computing carry/accrued interest.",
        )
    return None


def check_mv_consistency(security: dict, snapshot: dict) -> dict | None:
    """
    Compares a reported market value (if the snapshot carries one under
    'reported_market_value') against price x notional + accrued. Returns
    None if the snapshot doesn't carry a reported figure to check against —
    this is an available control, not one every demo snapshot will trigger.
    """
    reported = snapshot.get("reported_market_value")
    if reported is None:
        return None

    from core.returns import TOTAL_NOTIONAL, security_market_value
    notional = security["portfolio_weight"] * TOTAL_NOTIONAL
    computed = security_market_value(notional, snapshot["price"], snapshot["accrued_interest"])

    if abs(computed - reported) / max(abs(computed), 1.0) > 0.005:  # >0.5% discrepancy
        return _issue(
            "Market value inconsistency", security["security_id"],
            f"Computed MV ({computed:,.0f}) differs from reported MV ({reported:,.0f}) by more than 0.5%.",
            "REVIEW",
            "Reconcile price x notional + accrued against the source system's reported market value.",
        )
    return None


def check_coupon_event(security: dict, as_of: date, window_days: int = 7) -> dict | None:
    """Informational flag when a coupon payment falls within window_days of as_of."""
    maturity = security.get("maturity")
    if maturity is None or maturity <= as_of:
        return None
    _, next_coupon = _coupon_dates(maturity, as_of)
    days_to_coupon = (next_coupon - as_of).days
    if 0 <= days_to_coupon <= window_days:
        return _issue(
            "Upcoming coupon payment", security["security_id"],
            f"Coupon payment due {next_coupon.isoformat()} ({days_to_coupon} day(s) from as-of date).",
            "INFO",
            "Confirm accrued interest resets and settlement cash is reflected after the payment date.",
        )
    return None


def run_all_controls(securities: list[dict], snapshots: dict[str, dict], history: dict[str, list[dict]]) -> dict:
    """
    Run every control rule and roll up into a single PASS/REVIEW status.

    Args:
        securities: reference data, list of security dicts
        snapshots:  {security_id: latest snapshot dict} (must include security_id, date, price,
                    yield, accrued_interest, and optionally reported_market_value)
        history:    {security_id: [snapshot dicts, chronological]} — used for stale-price
                    (prev vs. curr) and abnormal-return (trailing distribution) checks

    Returns:
        {status: "PASS" | "REVIEW", issues: [Issue, ...]}
    """
    issues: list[dict] = []
    as_of = max((s["date"] for s in snapshots.values()), default=date.today())

    issues.extend(check_duplicate_security(securities))
    for key in ("portfolio_weight", "benchmark_weight"):
        issue = check_weights_sum(securities, key)
        if issue:
            issues.append(issue)

    for sec in securities:
        sec_id = sec["security_id"]

        for issue in (
            check_missing_classification(sec),
            check_missing_benchmark_mapping(sec),
            check_missing_coupon(sec),
            check_invalid_maturity(sec, as_of),
            check_coupon_event(sec, as_of),
        ):
            if issue:
                issues.append(issue)

        snap = snapshots.get(sec_id)
        if snap is None:
            continue

        yield_issue = check_missing_yield(snap)
        if yield_issue:
            issues.append(yield_issue)

        mv_issue = check_mv_consistency(sec, snap)
        if mv_issue:
            issues.append(mv_issue)

        sec_history = history.get(sec_id, [])
        if len(sec_history) >= 2:
            stale_issue = check_stale_price(sec_history[-2], sec_history[-1])
            if stale_issue:
                issues.append(stale_issue)

        if len(sec_history) >= 6:
            dirty = [h["price"] + h["accrued_interest"] for h in sec_history]
            daily_returns = [(dirty[i] / dirty[i - 1]) - 1.0 for i in range(1, len(dirty))]
            trailing, latest = daily_returns[:-1], daily_returns[-1]
            abnormal_issue = check_abnormal_return(sec_id, latest, trailing)
            if abnormal_issue:
                issues.append(abnormal_issue)

    status = "REVIEW" if any(i["severity"] != "INFO" for i in issues) else "PASS"
    return {"status": status, "issues": issues}
