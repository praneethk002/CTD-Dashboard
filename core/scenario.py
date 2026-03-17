"""
core/scenario.py

Parallel yield shock scenario analysis.

For each yield shift in a defined grid (e.g. -100bps to +100bps in 25bp steps),
reprice all 12 basket bonds, rerank by implied repo, and identify:
  - Which bond is CTD at each yield level
  - The implied repo spread between CTD and runner-up at each level
  - Whether the CTD identity changes across the grid

This answers the desk's morning question:
  "If yields move 50bps either way, does our CTD change?"

Limitation (documented):
  Parallel shifts only — the yield curve shape is held constant.
  Steepening/flattening scenarios are not modelled in this MVP.

No I/O. No database. Pure functions only.
"""

from datetime import date

import pandas as pd

from core.ctd import rank_basket


# default scenario grid: -100bps to +100bps in 25bp steps
DEFAULT_SHIFTS_BPS = [-100, -75, -50, -25, 0, 25, 50, 75, 100]


def scenario_grid(
    base_yields: dict[str, float],
    futures_price: float,
    repo_rate: float,
    settlement: date,
    shifts_bps: list[int] = DEFAULT_SHIFTS_BPS,
) -> pd.DataFrame:
    """
    Run parallel yield shock scenarios across the basket.

    For each shift, applies the same yield change to every bond in the basket
    (parallel shift), reprices, and reruns CTD ranking.

    Args:
        base_yields:   dict of {cusip: ytm} — today's yields
        futures_price: current TY futures price
        repo_rate:     GC repo rate as decimal
        settlement:    today's settlement date
        shifts_bps:    list of yield shifts in basis points (e.g. [-100, -50, 0, 50, 100])

    Returns:
        DataFrame with one row per (shift, bond) combination:
          shift_bps, cusip, label, implied_repo_pct, net_basis_ticks, is_ctd
        Plus summary columns: ctd_label, spread_to_runner_bps
    """
    all_rows = []

    for shift_bps in shifts_bps:
        shift = shift_bps / 10_000  # convert bps to decimal

        # apply parallel shift to all bond yields
        shocked_yields = {cusip: ytm + shift for cusip, ytm in base_yields.items()}

        # reprice and rerank the basket
        df = rank_basket(shocked_yields, futures_price, repo_rate, settlement)

        # extract CTD and runner-up for this scenario
        ctd    = df.iloc[0]
        runner = df.iloc[1]

        spread_bps = round(
            (ctd["implied_repo_pct"] - runner["implied_repo_pct"]) * 100, 2
        )

        for _, row in df.iterrows():
            all_rows.append({
                "shift_bps":             shift_bps,
                "cusip":                 row["cusip"],
                "label":                 row["label"],
                "implied_repo_pct":      row["implied_repo_pct"],
                "net_basis_ticks":       row["net_basis_ticks"],
                "is_ctd":                row["is_ctd"],
                "ctd_label":             ctd["label"],
                "spread_to_runner_bps":  spread_bps,
            })

    return pd.DataFrame(all_rows)


def ctd_by_scenario(scenario_df: pd.DataFrame) -> pd.DataFrame:
    """
    Extract just the CTD bond for each scenario.

    Useful for summarising how the CTD identity changes across the grid.

    Args:
        scenario_df: output of scenario_grid()

    Returns:
        DataFrame with one row per shift:
          shift_bps, ctd_label, implied_repo_pct, spread_to_runner_bps
    """
    ctd_rows = scenario_df[scenario_df["is_ctd"]].copy()

    return ctd_rows[[
        "shift_bps",
        "ctd_label",
        "implied_repo_pct",
        "spread_to_runner_bps",
    ]].reset_index(drop=True)
