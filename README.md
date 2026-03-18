# CTD Basis Monitor — TYM26

Daily monitor for the US Treasury cash-futures basis on the TY (10-year) contract.
For each bond in the TYM26 delivery basket, the system computes net basis and implied
repo, stores daily snapshots in SQLite, and tracks when the CTD is close to switching.
An MCP server exposes the database to Claude, which synthesises a PM-ready morning
brief on demand.

**The question it answers every morning:** where is today's net basis relative to its
90-day range, who is the CTD, how close is a transition, and what happens to CTD
identity under a parallel yield shift?

---

## Architecture

```
core/          Pure analytics — no I/O, no side effects
  basket.py    TYM26 delivery basket + CME conversion factor formula
  pricing.py   price_bond(), ytm(), dv01(), modified_duration(), convexity()
  carry.py     gross_basis(), net_basis(), implied_repo(), carry()
  ctd.py       rank_basket(); closed-form F* transition threshold; basis DV01
  scenario.py  scenario_grid() — parallel yield shocks -> re-ranked basket

data/          Storage and feeds
  db.py        BasisDB: SQLite schema, write snapshot, window-function queries
  fred_client.py  FRED API: 6 yield series, 5-min cache, maturity interpolation
  ingest.py    CLI: python -m data.ingest --contract TYM26 --date 2026-03-18

mcp_server/    FastMCP server — 8 tools over stdio, JSON-RPC 2.0
api/           Flask: /api/chat runs real MCP client (ClientSession + stdio_client)
ui/            Single-file dashboard (vanilla JS, Chart.js, IBM Plex Mono)
```

The `/api/chat` endpoint is a real MCP client — Flask spawns `mcp_server/server.py`
as a subprocess, connects via `ClientSession + stdio_client`, calls `list_tools()`,
and executes each tool via `call_tool()`. The UI renders the full tool call chain
alongside Claude's narrative.

---

## Analytic depth

**CTD transition threshold F\*** — the exact futures price at which bond B overtakes
bond A as CTD, derived by setting `implied_repo_A(F*) = implied_repo_B(F*)` and
solving analytically:

```
F* = (CA_B·P_A − CA_A·P_B) / (CF_A·P_B − CF_B·P_A)
```

where `CA_x = P_x · coupon_x · (days/365)` is the coupon accrual and `P_x` is the
dirty cash price. Exact closed-form — not an approximation. Verified numerically in
the test suite.

**DV01 of the basis position** (long cash / short futures):

```
DV01_basis = DV01_cash − DV01_futures_CTD / CF_CTD
```

**90-day rolling percentile** via SQLite window functions — `PERCENT_RANK() OVER
(ORDER BY net_basis)` on the `basis_snapshots` table gives the current basis
position in its own history without pulling data into Python.

---

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key
export FRED_API_KEY=your_key

# Seed 90-day synthetic history
python -m data.seed --reset --days 90

# Start the API
python -m api.app   # http://localhost:5001

# Open the dashboard
open ui/index.html
```

---

## MCP Tools

| # | Tool | Returns |
|---|------|---------|
| 1 | `get_current_basket` | 12-bond basket ranked by implied repo; CTD flagged |
| 2 | `get_basis_history` | 90-day net basis + 20-day MA + percentile rank |
| 3 | `get_basis_percentile` | CTD basis position in its 90-day distribution |
| 4 | `get_ctd_transitions` | Historical switch log with implied repo spread at switch |
| 5 | `get_transition_proximity` | Spread to runner-up (bps); risk flag LOW/ELEVATED/CRITICAL |
| 6 | `run_scenario_grid` | Basket re-ranked under parallel yield shocks (±100 bps) |
| 7 | `get_ctd_transition_threshold` | F* and distance from current futures price |
| 8 | `get_carry_roll` | 3M/6M carry: coupon income (ACT/365) vs financing (ACT/360) |

---

## Fixed income conventions

| Convention | Applied in |
|---|---|
| ACT/365 | Coupon accrual |
| ACT/360 | Repo financing |
| ACT/ACT (ICMA) | Accrued interest for bond pricing |
| Semi-annual compounding | All bond pricing |
| CME 6% standard yield | Conversion factor |

---

## Known limitations

| Limitation | Note |
|---|---|
| Yield proxy | FRED 10Y linearly interpolated per maturity; production requires per-CUSIP yields |
| Synthetic history | Random walk (seed=42, 7 bps/day vol); not real market data |
| Static basket | TYM26 hardcoded; no auto-roll at contract expiry |
| Delivery option | Net basis residual includes quality and timing options — not separately priced |
| Curve shape | Scenario grid assumes parallel shifts only |

Reference: Burghardt, Belton, Lane, Papa — *The Treasury Bond Basis*.
