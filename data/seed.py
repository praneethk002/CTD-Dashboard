"""
data/seed.py

Generates 90 days of synthetic TYM26 basis history and writes it to SQLite.

Why synthetic?
  Real Treasury market data requires Bloomberg (~$25k/year) or CME DataMine.
  Synthetic data lets us demonstrate the full workflow — percentile rankings,
  CTD transitions, trend analysis — without institutional data access.

How it works:
  1. Start from a base 10Y yield of 4.50%, repo rate 5.30%
  2. Simulate a random walk: each day's yield = previous day + N(0, 7bps)
  3. For each day, price all 12 basket bonds, rank by implied repo
  4. Write the full basket snapshot to SQLite via BasisDB

Parameters:
  RNG seed = 42    → reproducible results
  Base yield = 4.50%
  Daily vol  = 7bps (realistic for 10Y Treasury)
  Repo rate  = 5.30% (fixed — simplification)
  Futures price = computed from CTD implied repo parity each day

Usage:
  python -m data.seed              # seed 90 days, keep existing data
  python -m data.seed --reset      # drop and recreate tables first
  python -m data.seed --days 180   # seed 180 days
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

# ensure project root is on path when run as __main__
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.basket import get_basket, DELIVERY_DATE
from core.ctd import rank_basket
from data.db import BasisDB
from data.fred_client import build_basket_yields


# ------------------------------------------------------------------
# Seed parameters
# ------------------------------------------------------------------

RNG_SEED       = 42
BASE_YIELD     = 0.0450   # 4.50% starting 10Y yield
DAILY_VOL      = 0.0007   # 7bps daily standard deviation
REPO_RATE      = 0.0530   # 5.30% GC repo rate (fixed)
BASE_FUTURES   = 108.50   # starting futures price


def seed(days: int = 90, reset: bool = False, db_path: Path = None):
    """
    Generate synthetic history and write to SQLite.

    Args:
        days:    number of trading days to generate
        reset:   if True, drop and recreate tables before seeding
        db_path: path to SQLite file (default: project root)
    """
    db = BasisDB(db_path) if db_path else BasisDB()

    if reset:
        print("Resetting database...")
        db.reset()

    rng     = np.random.default_rng(RNG_SEED)
    basket  = get_basket()

    # generate yield path: random walk with daily shocks
    shocks       = rng.normal(0, DAILY_VOL, days)
    yield_path   = BASE_YIELD + np.cumsum(shocks)
    yield_path   = np.clip(yield_path, 0.005, 0.15)  # keep yields in [0.5%, 15%]

    # futures price drifts slightly with yields (inverse relationship)
    futures_path = BASE_FUTURES - (yield_path - BASE_YIELD) * 800

    # generate dates: go back `days` calendar days from today
    # skip weekends (crude approximation — real implementation uses trading calendar)
    end_date   = date.today() - timedelta(days=1)
    all_dates  = _trading_days(end_date, days)

    print(f"Seeding {len(all_dates)} days of TYM26 history...")

    written = 0
    for i, snapshot_dt in enumerate(all_dates):
        ytm           = float(yield_path[i])
        futures_price = float(futures_path[i])
        days_to_del   = max(1, (DELIVERY_DATE - snapshot_dt).days)

        # build flat yield curve at today's 10Y yield
        # (simplification: all bonds priced at same yield)
        yields = {b["cusip"]: ytm for b in basket}

        # rank basket and compute all metrics
        basket_df = rank_basket(
            yields        = yields,
            futures_price = futures_price,
            repo_rate     = REPO_RATE,
            settlement    = snapshot_dt,
        )

        db.write_snapshot(
            snapshot_dt      = snapshot_dt,
            contract         = "TYM26",
            basket_df        = basket_df,
            futures_price    = futures_price,
            repo_rate        = REPO_RATE,
            days_to_delivery = days_to_del,
        )

        written += 1

        if written % 10 == 0:
            ctd = basket_df[basket_df["is_ctd"]].iloc[0]
            print(
                f"  {snapshot_dt}  yield={ytm*100:.3f}%  "
                f"CTD={ctd['label']}  IR={ctd['implied_repo_pct']:.2f}%"
            )

    print(f"\n✓ Seeded {written} snapshots ({written * 12} rows) into basis_monitor.db")


def _trading_days(end_date: date, n: int) -> list[date]:
    """
    Generate the last N weekday dates ending at end_date.
    Excludes Saturday (5) and Sunday (6).
    """
    days   = []
    cursor = end_date

    while len(days) < n:
        if cursor.weekday() < 5:  # Monday=0 ... Friday=4
            days.append(cursor)
        cursor -= timedelta(days=1)

    return list(reversed(days))


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed synthetic TYM26 history")
    parser.add_argument("--days",  type=int,  default=90,    help="Number of trading days to generate")
    parser.add_argument("--reset", action="store_true",      help="Drop and recreate tables before seeding")
    args = parser.parse_args()

    seed(days=args.days, reset=args.reset)
