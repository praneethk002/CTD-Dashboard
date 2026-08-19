"""
tests/test_portfolio_reference.py

Sanity checks on the canonical demo portfolio reference data in
core/portfolio.py. These are the CLEAN weights — the deliberate 100.5%
weight-sum data-quality issue is applied only by data/portfolio_seed.py to
what gets written to the DB, not to this canonical in-code reference list.
"""

from core.portfolio import SECURITIES, get_bucket_map, get_securities


def test_portfolio_weights_sum_to_one():
    total = sum(s["portfolio_weight"] for s in get_securities())
    assert abs(total - 1.0) < 1e-9


def test_benchmark_weights_sum_to_one():
    total = sum(s["benchmark_weight"] for s in get_securities())
    assert abs(total - 1.0) < 1e-9


def test_security_ids_are_unique():
    ids = [s["security_id"] for s in get_securities()]
    assert len(ids) == len(set(ids))


def test_every_security_has_a_classification():
    for s in get_securities():
        assert s["classification"], f"{s['security_id']} missing classification"


def test_bucket_map_covers_all_securities():
    bucket_map = get_bucket_map()
    assert set(bucket_map) == {s["security_id"] for s in SECURITIES}


def test_get_securities_returns_a_copy_not_the_live_list():
    securities = get_securities()
    securities[0]["portfolio_weight"] = 999
    assert SECURITIES[0]["portfolio_weight"] != 999
