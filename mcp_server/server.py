"""
mcp_server/server.py

FastMCP server exposing 8 tools for CTD basis analytics.

This is the centrepiece of the system. Claude connects to this server
via MCP and calls these tools to answer morning brief questions like:
  "What's the TY basis this morning?"
  "Is the CTD at risk of switching?"
  "What happens to our position if yields rise 50bps?"

All 8 tools are READ-ONLY — they query the SQLite database and return
structured data. No tool writes to the database.

The MCP server runs over stdio transport, which means Claude connects
to it by spawning this process as a subprocess. This is the standard
MCP pattern for local tool servers.

Transport: stdio (standard MCP local pattern)
Entry:     python -m mcp_server.server

The 8 tools:
  1. get_current_basket         — full basket ranked by implied repo
  2. get_basis_history          — 90-day net basis series for a bond
  3. get_basis_percentile       — where today's CTD basis sits historically
  4. get_ctd_transitions        — historical CTD switch log
  5. get_transition_proximity   — spread to runner-up + risk flag + trend
  6. run_scenario_grid          — basket reranked under yield shocks
  7. get_ctd_transition_threshold — exact F* in closed form
  8. get_carry_roll             — carry decomposition over 3M/6M horizon
"""

import sys
from datetime import date, timedelta
from pathlib import Path

# ensure project root is on path when run as __main__
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastmcp import FastMCP

from core.basket import get_basket, conversion_factor, DELIVERY_DATE
from core.carry import coupon_income_act_act, implied_repo as compute_implied_repo
from core.ctd import rank_basket, ctd_transition_threshold
from core.pricing import accrued_interest, price_bond
from core.scenario import scenario_grid, ctd_by_scenario
from data.db import BasisDB
from data.fred_client import get_yield_curve, build_basket_yields

mcp = FastMCP("CTD Basis Monitor")
db  = BasisDB()


# ------------------------------------------------------------------
# Tool 1: get_current_basket
# ------------------------------------------------------------------

@mcp.tool()
def get_current_basket(contract: str = "TYM26") -> list[dict]:
    """
    Return the full delivery basket ranked by implied repo rate.

    The bond ranked #1 (highest implied repo) is the CTD.
    All metrics are from the most recent snapshot in the database.

    Args:
        contract: futures contract identifier (default "TYM26")

    Returns:
        List of 12 bonds, each with:
          label, cusip, cash_price, conv_factor,
          gross_basis, net_basis_ticks, implied_repo_pct, dv01, is_ctd
        Sorted by implied_repo_pct descending (CTD first).
    """
    snapshot = db.get_latest_snapshot(contract)

    return [
        {
            "label":            r["label"],
            "cusip":            r["cusip"],
            "cash_price":       r["cash_price"],
            "conv_factor":      r["conv_factor"],
            "gross_basis":      r["gross_basis"],
            "net_basis_ticks":  r["net_basis_ticks"],
            "implied_repo_pct": round(r["implied_repo"] * 100, 4),
            "dv01":             r["dv01"],
            "is_ctd":           bool(r["is_ctd"]),
        }
        for r in snapshot
    ]


# ------------------------------------------------------------------
# Tool 2: get_basis_history
# ------------------------------------------------------------------

@mcp.tool()
def get_basis_history(
    cusip: str,
    contract: str = "TYM26",
    days: int = 90,
) -> dict:
    """
    Return net basis history for a specific bond over the last N days.

    Includes a 20-day moving average and percentile rank for each observation.
    Used to assess whether today's basis is historically tight or wide.

    Args:
        cusip:    bond CUSIP identifier
        contract: futures contract (default "TYM26")
        days:     lookback period in trading days (default 90)

    Returns:
        dict with:
          cusip, label, history (list of {date, net_basis_ticks, ma_20d})
          current_percentile_rank
    """
    history = db.get_basis_history(cusip, contract, days)

    if not history:
        return {"error": f"No history found for {cusip} in {contract}"}

    values = [r["net_basis_ticks"] for r in history]

    # compute 20-day moving average
    enriched = []
    for i, row in enumerate(history):
        window = values[max(0, i - 19): i + 1]
        enriched.append({
            "date":            row["snapshot_dt"],
            "net_basis_ticks": round(row["net_basis_ticks"], 3),
            "ma_20d":          round(sum(window) / len(window), 3),
        })

    # percentile rank of the most recent value
    today = values[-1]
    percentile = sum(v < today for v in values) / len(values) * 100

    return {
        "cusip":                    cusip,
        "label":                    history[0].get("label", cusip),
        "history":                  enriched,
        "current_percentile_rank":  round(percentile, 1),
    }


# ------------------------------------------------------------------
# Tool 3: get_basis_percentile
# ------------------------------------------------------------------

@mcp.tool()
def get_basis_percentile(
    contract: str = "TYM26",
    days: int = 90,
) -> dict:
    """
    Return where today's CTD net basis sits in its N-day distribution.

    A low percentile means the basis is historically tight — the bond
    is cheap relative to futures. A high percentile means it's wide.

    Args:
        contract: futures contract (default "TYM26")
        days:     lookback window (default 90)

    Returns:
        dict with today_net_basis_ticks, percentile_rank,
        min_90d, max_90d, mean_90d
    """
    return db.get_basis_percentile(contract, days)


# ------------------------------------------------------------------
# Tool 4: get_ctd_transitions
# ------------------------------------------------------------------

@mcp.tool()
def get_ctd_transitions(contract: str = "TYM26") -> list[dict]:
    """
    Return the historical log of CTD transitions for a contract.

    Each record shows when the CTD switched, which bond it switched
    from/to, and the implied repo spread at the time of the switch.
    A narrow spread at transition time indicates the switch was
    anticipated; a wide spread indicates a surprise.

    Args:
        contract: futures contract (default "TYM26")

    Returns:
        List of transition records:
          change_dt, prev_ctd_cusip, new_ctd_cusip, implied_repo_spread_bps
        Sorted most recent first.
    """
    return db.get_ctd_transitions(contract)


# ------------------------------------------------------------------
# Tool 5: get_transition_proximity
# ------------------------------------------------------------------

@mcp.tool()
def get_transition_proximity(contract: str = "TYM26") -> dict:
    """
    Return the implied repo spread between the CTD and runner-up bond.

    This is the primary morning risk signal for the basis desk:
      - How close is the CTD to switching?
      - Is the spread narrowing (risk increasing) or widening (risk decreasing)?

    Risk flags:
      CRITICAL  — spread < 5bps  → switch imminent, immediate attention
      ELEVATED  — 5-15bps        → heightened monitoring required
      LOW       — > 15bps        → no near-term transition risk

    Trend:
      NARROWING — spread tightened > 2bps vs 5 days ago
      WIDENING  — spread widened  > 2bps vs 5 days ago
      STABLE    — within 2bps either way

    Args:
        contract: futures contract (default "TYM26")

    Returns:
        dict with ctd_label, runner_label, current_spread_bps,
        trend, risk_flag
    """
    return db.get_transition_proximity(contract)


# ------------------------------------------------------------------
# Tool 6: run_scenario_grid
# ------------------------------------------------------------------

@mcp.tool()
def run_scenario_grid(
    contract: str = "TYM26",
    shifts_bps: list[int] = None,
) -> dict:
    """
    Reprice the basket under parallel yield shocks and rerank CTD.

    For each yield shift, applies the same change to all 12 bonds
    (parallel shift), reprices, and reruns the CTD ranking.
    Answers: "If yields move X bps, does our CTD change?"

    Args:
        contract:   futures contract (default "TYM26")
        shifts_bps: list of yield shifts in bps (default: -100 to +100 in 25bp steps)

    Returns:
        dict with:
          scenarios — list of {shift_bps, ctd_label, implied_repo_pct, spread_to_runner_bps}
          ctd_switches — list of shifts where CTD identity changes vs today
    """
    if shifts_bps is None:
        shifts_bps = [-100, -75, -50, -25, 0, 25, 50, 75, 100]

    # get today's basket to extract current yields
    snapshot = db.get_latest_snapshot(contract)
    if not snapshot:
        return {"error": f"No snapshot found for {contract}"}

    settlement     = date.today()
    futures_price  = snapshot[0]["futures_price"]
    repo_rate      = snapshot[0]["repo_rate"]

    # reconstruct yields from stored cash prices using YTM solver
    from core.pricing import ytm_from_price
    basket  = get_basket()
    yields  = {}

    for bond in basket:
        row = next((r for r in snapshot if r["cusip"] == bond["cusip"]), None)
        if row:
            try:
                ytm = ytm_from_price(
                    bond["coupon"],
                    bond["maturity"],
                    row["cash_price"],
                    settlement,
                )
                yields[bond["cusip"]] = ytm
            except Exception:
                yields[bond["cusip"]] = 0.045  # fallback

    # run scenario grid
    scen_df  = scenario_grid(yields, futures_price, repo_rate, settlement, shifts_bps)
    summary  = ctd_by_scenario(scen_df)

    # identify today's CTD
    today_ctd = next((r["label"] for r in snapshot if r["is_ctd"]), None)

    scenarios     = summary.to_dict(orient="records")
    ctd_switches  = [
        s["shift_bps"]
        for s in scenarios
        if s["ctd_label"] != today_ctd
    ]

    return {
        "scenarios":    scenarios,
        "ctd_switches": ctd_switches,
        "today_ctd":    today_ctd,
    }


# ------------------------------------------------------------------
# Tool 7: get_ctd_transition_threshold
# ------------------------------------------------------------------

@mcp.tool()
def get_ctd_transition_threshold(contract: str = "TYM26") -> dict:
    """
    Compute F* — the exact futures price at which the CTD switches.

    Uses the closed-form solution derived by setting the implied repo
    of the CTD equal to the runner-up and solving for futures price F:

      F* = (CA_B·P_A - CA_A·P_B) / (CF_A·P_B - CF_B·P_A)

    F* is operationally meaningful when the transition proximity spread
    is ELEVATED or CRITICAL (< 15bps). When spread is wide (LOW risk),
    F* may fall far outside the current price range.

    Args:
        contract: futures contract (default "TYM26")

    Returns:
        dict with:
          ctd_label, runner_label,
          current_futures_price,
          transition_threshold_futures_price (F*),
          distance_to_threshold_pts,
          direction ("RALLY" or "SELLOFF" to trigger switch)
    """
    snapshot = db.get_latest_snapshot(contract)
    if not snapshot:
        return {"error": f"No snapshot found for {contract}"}

    settlement    = date.today()
    futures_price = snapshot[0]["futures_price"]

    # CTD = first row (sorted by implied_repo desc)
    ctd_row    = snapshot[0]
    runner_row = snapshot[1]

    basket = get_basket()

    ctd_bond    = next(b for b in basket if b["cusip"] == ctd_row["cusip"])
    runner_bond = next(b for b in basket if b["cusip"] == runner_row["cusip"])

    days = max(1, (DELIVERY_DATE - settlement).days)

    threshold = ctd_transition_threshold(
        ctd_price       = ctd_row["cash_price"],
        runner_price    = runner_row["cash_price"],
        ctd_cf          = ctd_row["conv_factor"],
        runner_cf       = runner_row["conv_factor"],
        ctd_coupon      = ctd_bond["coupon"],
        runner_coupon   = runner_bond["coupon"],
        days            = days,
        settlement      = settlement,
        ctd_maturity    = ctd_bond["maturity"],
        runner_maturity = runner_bond["maturity"],
    )

    f_star   = threshold["transition_threshold_futures_price"]
    distance = round(abs(futures_price - f_star), 4)
    direction = "RALLY" if f_star > futures_price else "SELLOFF"

    return {
        "ctd_label":                         ctd_row["label"],
        "runner_label":                      runner_row["label"],
        "current_futures_price":             futures_price,
        "transition_threshold_futures_price": f_star,
        "distance_to_threshold_pts":         distance,
        "direction":                         direction,
    }


# ------------------------------------------------------------------
# Tool 8: get_carry_roll
# ------------------------------------------------------------------

@mcp.tool()
def get_carry_roll(
    cusip: str,
    contract: str = "TYM26",
    repo_rate: float = None,
) -> dict:
    """
    Decompose carry for a bond over 3-month and 6-month horizons.

    Carry = coupon income (ACT/ACT) - repo financing cost (ACT/360)
    Positive carry = trade earns money while held.
    Negative carry = trade costs money — common when repo rate > coupon yield.

    Args:
        cusip:     bond CUSIP identifier
        contract:  futures contract (default "TYM26")
        repo_rate: repo rate as decimal (default: uses rate from latest snapshot)

    Returns:
        dict with for each horizon (3m, 6m):
          coupon_income, financing_cost, net_carry (all in price points)
        plus implied_repo_pct and annualised_carry_bps
    """
    snapshot = db.get_latest_snapshot(contract)
    if not snapshot:
        return {"error": f"No snapshot found for {contract}"}

    row = next((r for r in snapshot if r["cusip"] == cusip), None)
    if not row:
        return {"error": f"CUSIP {cusip} not found in {contract} basket"}

    rate       = repo_rate if repo_rate is not None else row["repo_rate"]
    settlement = date.today()

    basket = get_basket()
    bond   = next(b for b in basket if b["cusip"] == cusip)

    ai          = accrued_interest(bond["coupon"], bond["maturity"], settlement)
    dirty_price = row["cash_price"] + ai

    result = {"label": row["label"], "cusip": cusip, "repo_rate_pct": round(rate * 100, 3)}

    for label, days in [("3m", 91), ("6m", 182)]:
        ci             = coupon_income_act_act(bond["coupon"], bond["maturity"], settlement, days)
        financing_cost = dirty_price * rate * (days / 360)
        net            = ci - financing_cost

        result[label] = {
            "days":            days,
            "coupon_income":   round(ci, 4),
            "financing_cost":  round(financing_cost, 4),
            "net_carry":       round(net, 4),
            "net_carry_bps":   round(net * 100, 2),
        }

    result["implied_repo_pct"] = round(row["implied_repo"] * 100, 4)

    return result


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
