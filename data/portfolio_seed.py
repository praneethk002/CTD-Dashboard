"""
data/portfolio_seed.py

Generates deterministic synthetic daily history for the demo 8-security
portfolio (core/portfolio.py) and writes it to portfolio_analytics.db.

Why synthetic? Same reason as data/seed.py for the CTD basket: real
Treasury market data requires a paid data vendor. This is a clearly-labeled
demo dataset, not real market data.

Date range is FIXED (2025-12-31 -> 2026-08-19), not relative to "today" —
this is a point-in-time portfolio piece built for a specific application
cycle, and the fixed range guarantees 1D/MTD/QTD/YTD are all computable
without any of them silently falling back to partial data.

Yield path per security = a smooth, bounded common curve-level path (a
gentle wiggle, not an unbounded random walk — an unbounded random walk
compounds over a 166-day window into unrealistically large cumulative
yield swings for a demo meant to read as a believable, explainable story)
+ a deterministic mild bear-steepening drift by maturity bucket (Short
~-5bp, Intermediate ~+5bp, Long ~+15bp by the end date, layered on top of
the common path) + small daily noise (~2.5bp, shared "curve day" noise +
~1.5bp per-security idiosyncratic noise, both non-cumulative so they add
day-to-day texture — useful for the abnormal-return control — without
distorting the multi-month endpoint). The bucket drift is what makes the
attribution story real: the portfolio's long-duration overweight vs. the
benchmark (see core/portfolio.py) genuinely detracts as long yields drift
up more than short yields.

Also injects 3 deliberate, deterministic data-quality issues so the
Controls layer (core/controls.py) has something concrete to catch on every
reset:
  1. DEMO-UST-05YB's final-date snapshot repeats the prior day's price/yield
     unchanged -> caught by check_stale_price.
  2. DEMO-UST-20Y's classification is written as NULL -> caught by
     check_missing_classification.
  3. DEMO-UST-10Y's portfolio_weight is written 0.5pp high (0.205 instead of
     0.20), so total portfolio weights sum to 100.5% -> caught by
     check_weights_sum.

Usage:
  python -m data.portfolio_seed            # seed, keep existing data
  python -m data.portfolio_seed --reset     # drop and recreate tables first
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.portfolio import get_securities, TOTAL_NOTIONAL
from core.pricing import accrued_interest, convexity, dv01, modified_duration, price_bond
from data.portfolio_db import PortfolioDB

RNG_SEED = 7
START_DATE = date(2025, 12, 31)
END_DATE = date(2026, 8, 19)

COMMON_DAILY_NOISE_VOL = 0.00025    # ~2.5bp shared daily "curve day" noise, non-cumulative
IDIOSYNCRATIC_DAILY_VOL = 0.00015   # ~1.5bp per-security noise, non-cumulative
COMMON_WIGGLE_AMPLITUDE = 0.0006    # ~6bp smooth curve-level wiggle over the period
COMMON_LEVEL_DRIFT = 0.0004         # ~4bp smooth curve-level drift by end date

BUCKET_DRIFT_BPS = {
    "Short (0-3Y)": -0.0005,          # -5bp cumulative by END_DATE
    "Intermediate (3-10Y)": 0.0005,   # +5bp cumulative by END_DATE
    "Long (10Y+)": 0.0015,            # +15bp cumulative by END_DATE
}

BASE_YIELDS = {
    "DEMO-UST-02Y": 0.0390,
    "DEMO-UST-03Y": 0.0385,
    "DEMO-UST-05YA": 0.0400,
    "DEMO-UST-05YB": 0.0395,
    "DEMO-UST-07Y": 0.0415,
    "DEMO-UST-10Y": 0.0430,
    "DEMO-UST-20Y": 0.0460,
    "DEMO-UST-30Y": 0.0470,
}

STALE_PRICE_SECURITY = "DEMO-UST-05YB"
MISSING_CLASSIFICATION_SECURITY = "DEMO-UST-20Y"
OVERWEIGHT_SECURITY = "DEMO-UST-10Y"
OVERWEIGHT_DELTA = 0.005  # pushes total portfolio weight to 100.5%


def _trading_days(start: date, end: date) -> list[date]:
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:  # Mon-Fri
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _yield_paths(dates: list[date], rng: np.random.Generator) -> dict[str, np.ndarray]:
    n = len(dates)
    t = np.arange(n) / (n - 1)  # 0 -> 1 over the window

    # smooth, bounded common curve-level path: a gentle wiggle plus a small
    # net drift, NOT an unbounded random walk (which would compound into an
    # unrealistically large cumulative move over a 166-day window)
    common_level = COMMON_WIGGLE_AMPLITUDE * np.sin(2 * np.pi * t * 0.5) + COMMON_LEVEL_DRIFT * t
    common_daily_noise = rng.normal(0, COMMON_DAILY_NOISE_VOL, n)  # shared across all bonds, non-cumulative
    common = common_level + common_daily_noise

    securities = get_securities()
    paths: dict[str, np.ndarray] = {}
    for sec in securities:
        sec_id = sec["security_id"]
        base = BASE_YIELDS[sec_id]
        bucket_drift = BUCKET_DRIFT_BPS[sec["classification"]] * t
        idio = rng.normal(0, IDIOSYNCRATIC_DAILY_VOL, n)  # per-security, non-cumulative
        path = base + common + bucket_drift + idio
        paths[sec_id] = np.clip(path, 0.01, 0.10)
    return paths


def seed(reset: bool = False, db_path: Path = None, start: date = START_DATE, end: date = END_DATE):
    db = PortfolioDB(db_path) if db_path else PortfolioDB()

    if reset:
        print("Resetting portfolio_analytics.db...")
        db.reset()

    securities = get_securities()

    # write reference data, applying the 2 deliberate reference-data issues
    ref_rows = []
    for sec in securities:
        row = dict(sec)
        row["par_amount"] = row["portfolio_weight"] * TOTAL_NOTIONAL
        if row["security_id"] == MISSING_CLASSIFICATION_SECURITY:
            row["classification"] = None
        if row["security_id"] == OVERWEIGHT_SECURITY:
            row["portfolio_weight"] = row["portfolio_weight"] + OVERWEIGHT_DELTA
            row["par_amount"] = row["portfolio_weight"] * TOTAL_NOTIONAL
        ref_rows.append(row)
    db.write_securities(ref_rows)

    dates = _trading_days(start, end)
    rng = np.random.default_rng(RNG_SEED)
    yield_paths = _yield_paths(dates, rng)

    print(f"Seeding {len(dates)} trading days x {len(securities)} securities "
          f"({dates[0]} -> {dates[-1]})...")

    snapshots_by_security: dict[str, list[dict]] = {s["security_id"]: [] for s in securities}

    for i, dt in enumerate(dates):
        for sec in securities:
            sec_id = sec["security_id"]
            coupon = sec["coupon"]
            maturity = sec["maturity"]
            ytm = float(yield_paths[sec_id][i])

            price = price_bond(coupon, maturity, ytm, dt)
            accrued = accrued_interest(coupon, maturity, dt)
            md = modified_duration(coupon, maturity, ytm, dt)
            cvx = convexity(coupon, maturity, ytm, dt)
            d01 = dv01(coupon, maturity, ytm, dt)

            snapshots_by_security[sec_id].append({
                "date": dt,
                "security_id": sec_id,
                "price": round(price, 4),
                "yield": round(ytm, 6),
                "accrued_interest": round(accrued, 4),
                "modified_duration": round(md, 4),
                "convexity": round(cvx, 2),
                "dv01": round(d01, 4),
            })

    # deliberate issue #1: stale price on the final date for one security
    stale = snapshots_by_security[STALE_PRICE_SECURITY]
    stale[-1] = {**stale[-2], "date": stale[-1]["date"]}

    all_snapshots = [snap for snaps in snapshots_by_security.values() for snap in snaps]
    db.write_snapshots_bulk(all_snapshots)

    print(f"\nSeeded {len(all_snapshots)} snapshot rows into portfolio_analytics.db")
    print(f"Deliberate data-quality issues seeded:")
    print(f"  - stale price:            {STALE_PRICE_SECURITY} on {end.isoformat()}")
    print(f"  - missing classification: {MISSING_CLASSIFICATION_SECURITY}")
    print(f"  - weights sum to 100.5%:  {OVERWEIGHT_SECURITY} overweighted by {OVERWEIGHT_DELTA*100:.1f}pp")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed synthetic demo portfolio history")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate tables before seeding")
    args = parser.parse_args()

    seed(reset=args.reset)
