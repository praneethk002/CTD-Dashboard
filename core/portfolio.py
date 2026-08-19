"""
core/portfolio.py

Reference data for the demo fixed-income portfolio.

This is a synthetic, clearly-labeled demo portfolio — NOT real holdings.
8 US Treasury securities spanning the curve (2Y-30Y), each with a portfolio
weight and a benchmark weight. The benchmark is a re-weighting of the same
8-security universe (not a real market index like the Bloomberg Barclays
Treasury Index) — a documented simplification.

The portfolio is deliberately tilted long-duration relative to the benchmark
(overweight 20Y/30Y, funded from a 10Y underweight), so that duration
positioning shows up as a real, explainable driver of active return once
priced against a historical yield path (see data/portfolio_seed.py).

"region" is a constant ("US") on every row. This is a single-issuer UST
demo, not a real multi-region portfolio — included as a schema field for
completeness, not as a meaningful attribution dimension here.

No I/O. No database. Pure reference data + accessors only.
"""

from datetime import date

SECURITIES: list[dict] = [
    {
        "security_id": "DEMO-UST-02Y",
        "label": "UST 2Y",
        "coupon": 0.04000,
        "maturity": date(2028, 6, 30),
        "classification": "Short (0-3Y)",
        "region": "US",
        "portfolio_weight": 0.10,
        "benchmark_weight": 0.12,
    },
    {
        "security_id": "DEMO-UST-03Y",
        "label": "UST 3Y",
        "coupon": 0.03875,
        "maturity": date(2029, 5, 31),
        "classification": "Short (0-3Y)",
        "region": "US",
        "portfolio_weight": 0.10,
        "benchmark_weight": 0.13,
    },
    {
        "security_id": "DEMO-UST-05YA",
        "label": "UST 5Y A",
        "coupon": 0.04125,
        "maturity": date(2031, 6, 30),
        "classification": "Intermediate (3-10Y)",
        "region": "US",
        "portfolio_weight": 0.15,
        "benchmark_weight": 0.12,
    },
    {
        "security_id": "DEMO-UST-05YB",
        "label": "UST 5Y B",
        "coupon": 0.03750,
        "maturity": date(2030, 11, 30),
        "classification": "Intermediate (3-10Y)",
        "region": "US",
        "portfolio_weight": 0.10,
        "benchmark_weight": 0.08,
    },
    {
        "security_id": "DEMO-UST-07Y",
        "label": "UST 7Y",
        "coupon": 0.04250,
        "maturity": date(2033, 6, 30),
        "classification": "Intermediate (3-10Y)",
        "region": "US",
        "portfolio_weight": 0.15,
        "benchmark_weight": 0.15,
    },
    {
        "security_id": "DEMO-UST-10Y",
        "label": "UST 10Y",
        "coupon": 0.04375,
        "maturity": date(2036, 5, 15),
        "classification": "Intermediate (3-10Y)",
        "region": "US",
        "portfolio_weight": 0.20,
        "benchmark_weight": 0.25,
    },
    {
        "security_id": "DEMO-UST-20Y",
        "label": "UST 20Y",
        "coupon": 0.04500,
        "maturity": date(2046, 5, 15),
        "classification": "Long (10Y+)",
        "region": "US",
        "portfolio_weight": 0.10,
        "benchmark_weight": 0.08,
    },
    {
        "security_id": "DEMO-UST-30Y",
        "label": "UST 30Y",
        "coupon": 0.04625,
        "maturity": date(2056, 5, 15),
        "classification": "Long (10Y+)",
        "region": "US",
        "portfolio_weight": 0.10,
        "benchmark_weight": 0.07,
    },
]

TOTAL_NOTIONAL = 10_000_000.0  # demo total par, USD


def get_securities() -> list[dict]:
    """Return the canonical 8-security demo universe (clean weights, no seeded issues)."""
    return [dict(s) for s in SECURITIES]


def get_bucket_map() -> dict[str, str]:
    """Return {security_id: classification} for the demo universe."""
    return {s["security_id"]: s["classification"] for s in SECURITIES}
