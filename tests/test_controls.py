"""
tests/test_controls.py

Each control rule against a crafted PASS case and a crafted REVIEW case.
All rules are deterministic Python — no LLM involvement, per
core/controls.py's design.
"""

from datetime import date

from core.controls import (
    check_abnormal_return,
    check_coupon_event,
    check_duplicate_security,
    check_invalid_maturity,
    check_missing_benchmark_mapping,
    check_missing_classification,
    check_missing_coupon,
    check_missing_yield,
    check_mv_consistency,
    check_stale_price,
    check_weights_sum,
    run_all_controls,
)

GOOD_SECURITY = {
    "security_id": "A", "label": "Bond A", "coupon": 0.04, "maturity": date(2035, 6, 30),
    "classification": "Intermediate", "portfolio_weight": 0.5, "benchmark_weight": 0.5,
}


def test_check_stale_price_pass_and_review():
    prev = {"security_id": "A", "date": date(2026, 1, 1), "price": 99.0, "yield": 0.04}
    curr_changed = {"security_id": "A", "date": date(2026, 1, 2), "price": 99.1, "yield": 0.0402}
    curr_stale = {"security_id": "A", "date": date(2026, 1, 2), "price": 99.0, "yield": 0.04}

    assert check_stale_price(prev, curr_changed) is None
    issue = check_stale_price(prev, curr_stale)
    assert issue is not None
    assert issue["issue"] == "Stale price"
    assert issue["severity"] == "REVIEW"


def test_check_missing_classification_pass_and_review():
    assert check_missing_classification(GOOD_SECURITY) is None
    bad = {**GOOD_SECURITY, "classification": None}
    assert check_missing_classification(bad) is not None


def test_check_missing_yield_pass_and_review():
    assert check_missing_yield({"security_id": "A", "yield": 0.04}) is None
    assert check_missing_yield({"security_id": "A", "yield": None}) is not None


def test_check_duplicate_security_pass_and_review():
    unique = [GOOD_SECURITY, {**GOOD_SECURITY, "security_id": "B"}]
    assert check_duplicate_security(unique) == []

    duped = [GOOD_SECURITY, {**GOOD_SECURITY, "security_id": "A"}]
    issues = check_duplicate_security(duped)
    assert len(issues) == 1
    assert issues[0]["security_id"] == "A"


def test_check_weights_sum_pass_and_review():
    good = [{"portfolio_weight": 0.6}, {"portfolio_weight": 0.4}]
    bad = [{"portfolio_weight": 0.6}, {"portfolio_weight": 0.45}]
    assert check_weights_sum(good, "portfolio_weight") is None
    assert check_weights_sum(bad, "portfolio_weight") is not None


def test_check_abnormal_return_pass_and_review():
    trailing = [0.001, -0.0008, 0.0005, 0.0009, -0.0003, 0.0007]
    assert check_abnormal_return("A", 0.0006, trailing) is None  # in-line with trailing distribution
    assert check_abnormal_return("A", 0.05, trailing) is not None  # wildly abnormal


def test_check_abnormal_return_insufficient_history_returns_none():
    assert check_abnormal_return("A", 0.05, [0.001, 0.002]) is None


def test_check_missing_benchmark_mapping_pass_and_review():
    assert check_missing_benchmark_mapping(GOOD_SECURITY) is None
    bad = {**GOOD_SECURITY, "benchmark_weight": None}
    assert check_missing_benchmark_mapping(bad) is not None


def test_check_invalid_maturity_pass_and_review():
    as_of = date(2026, 8, 19)
    assert check_invalid_maturity(GOOD_SECURITY, as_of) is None
    matured = {**GOOD_SECURITY, "maturity": date(2025, 1, 1)}
    assert check_invalid_maturity(matured, as_of) is not None
    missing = {**GOOD_SECURITY, "maturity": None}
    assert check_invalid_maturity(missing, as_of) is not None


def test_check_missing_coupon_pass_and_review():
    assert check_missing_coupon(GOOD_SECURITY) is None
    bad = {**GOOD_SECURITY, "coupon": None}
    assert check_missing_coupon(bad) is not None


def test_check_mv_consistency_pass_when_no_reported_value():
    assert check_mv_consistency(GOOD_SECURITY, {"price": 99.0, "accrued_interest": 0.2}) is None


def test_check_mv_consistency_review_when_reported_value_diverges():
    snapshot = {"price": 99.0, "accrued_interest": 0.2, "reported_market_value": 999.0}
    issue = check_mv_consistency(GOOD_SECURITY, snapshot)
    assert issue is not None
    assert issue["issue"] == "Market value inconsistency"


def test_check_coupon_event_flags_within_window():
    # bond maturing Jun 30 pays coupons Jun 30 / Dec 30 -> as_of 3 days before Jun 30 should flag
    sec = {**GOOD_SECURITY, "maturity": date(2035, 6, 30)}
    as_of_near = date(2026, 6, 27)
    as_of_far = date(2026, 3, 1)

    issue_near = check_coupon_event(sec, as_of_near, window_days=7)
    issue_far = check_coupon_event(sec, as_of_far, window_days=7)

    assert issue_near is not None
    assert issue_near["severity"] == "INFO"
    assert issue_far is None


def test_run_all_controls_pass_when_clean():
    securities = [GOOD_SECURITY, {**GOOD_SECURITY, "security_id": "B", "portfolio_weight": 0.5,
                                   "benchmark_weight": 0.5, "maturity": date(2036, 3, 31)}]
    snapshots = {
        "A": {"security_id": "A", "date": date(2026, 8, 19), "price": 99.0, "yield": 0.041, "accrued_interest": 0.2},
        "B": {"security_id": "B", "date": date(2026, 8, 19), "price": 98.5, "yield": 0.043, "accrued_interest": 0.3},
    }
    history = {"A": [snapshots["A"]], "B": [snapshots["B"]]}

    result = run_all_controls(securities, snapshots, history)
    assert result["status"] == "PASS"
    assert result["issues"] == []


def test_run_all_controls_review_when_issues_present():
    securities = [{**GOOD_SECURITY, "classification": None}]
    snapshots = {"A": {"security_id": "A", "date": date(2026, 8, 19), "price": 99.0, "yield": 0.041, "accrued_interest": 0.2}}
    history = {"A": [snapshots["A"]]}

    result = run_all_controls(securities, snapshots, history)
    assert result["status"] == "REVIEW"
    assert any(i["issue"] == "Missing classification" for i in result["issues"])
