# Fixed Income Performance & Analytics Workbench

A Python-based prototype for automating fixed-income performance measurement,
performance-driver analysis, data-quality controls, and recurring reporting —
built on top of an existing Treasury futures cheapest-to-deliver (CTD) basis
analytics engine.

This is a portfolio piece, not a product. All portfolio, benchmark, and
holdings data is **synthetic and clearly labeled as such** — nothing here
reflects real accounts, real counterparties, or any institution's actual
holdings.

**What questions does it answer?**

1. **What happened?** — portfolio return, benchmark return, active return,
   over 1D / MTD / QTD / YTD.
2. **Why did it happen?** — security- and maturity-bucket-level attribution:
   carry vs. yield-driven price effect vs. an explicitly-labeled residual.
3. **Can I trust the numbers?** — a deterministic, rule-based data-quality
   layer that flags stale prices, missing classifications, weight breaks,
   and similar issues before they reach a report.
4. **What should an analyst investigate?** — a structured report object
   (returns + attribution + exceptions) that a short AI-drafted or
   template-based commentary can narrate, without ever being asked to
   calculate anything itself.

---

## Why I built this

I wanted to explore how far fixed-income performance reporting could be
automated in Python — the return math, the attribution, the data-quality
checks — while keeping every number auditable back to a plain calculation,
rather than treating the calculation itself as something an LLM does. AI
shows up exactly once in this codebase: turning already-computed numbers
into a short written commentary. It never calculates a return, an
attribution component, or a control result.

The Treasury futures CTD basis engine below predates this workbench and
was the original project — the portfolio/attribution/controls layer here
was built on top of it, reusing its bond-pricing math (price, YTM,
duration, convexity, DV01, accrued interest) rather than reimplementing it.

---

## The workbench, in order

### 1. Performance (`core/returns.py`)

Portfolio return, benchmark return, and active return over 1D/MTD/QTD/YTD.
The demo portfolio has no external cash flows (no client contributions or
withdrawals during the window), so a simple beginning-vs-ending market
value return is the correct methodology here — the module's docstring
explains what would change (Modified Dietz or a daily-linked TWR) if
external flows were introduced. It **does** correctly handle an *internal*
cash flow that's easy to get wrong: coupon payments falling inside the
measurement window are added back explicitly, since accrued interest resets
to zero right after each coupon date.

### 2. Attribution (`core/attribution.py`)

Each security's return is split into:
- **carry** — the exact income component (change in accrued interest, plus
  any coupon actually paid during the period)
- **price_effect_approx** — an explicitly-labeled *approximation* of the
  yield-driven price change: `-modified_duration·Δyield + 0.5·convexity·Δyield²`
- **residual** — whatever the duration/convexity approximation doesn't
  explain. Always shown, never forced to zero or hidden in a fake category.

Rolls up to maturity-bucket level and compares portfolio vs. benchmark
contribution bucket-by-bucket, so you can click from portfolio → bucket →
security.

### 3. Data-quality controls (`core/controls.py`)

Eleven deterministic rules — stale/missing prices, missing classification,
duplicate securities, weights not summing to 100%, abnormal daily returns,
missing benchmark mapping, invalid/missing maturity or coupon, market-value
inconsistency, upcoming coupon events. Every rule is plain Python — **no
LLM is ever asked to judge whether financial data is correct.** Output is a
single `PASS` / `REVIEW` status plus a list of concrete issues, not a wall
of twenty green checkmarks.

### 4. Reporting + commentary (`core/reporting.py`, `core/commentary.py`)

`generate_report()` assembles one structured object — performance,
attribution, risk metrics, contributors, data-quality exceptions — that
backs both the API's `/report` endpoint and the commentary layer.
Commentary is either Anthropic-generated or template-generated from that
same structured object; the response always states which one it is
(`ai_generated` vs. `template_fallback`), and the app works fully without
an API key.

---

## Advanced Fixed Income Analytics: Treasury futures CTD basis

*Which Treasury note is cheapest to deliver into the futures contract right
now, and how close are we to that changing?*

This is the original engine the workbench above is built on: for every bond
in the TYM26 delivery basket, it computes net basis and implied repo, tracks
daily snapshots, and identifies exactly how far the futures price would need
to move before the cheapest-to-deliver (CTD) bond changes.

**CTD transition threshold F\*** — the exact futures price at which bond B
overtakes bond A as CTD, derived by setting `implied_repo_A(F*) =
implied_repo_B(F*)` and solving analytically:

```
F* = (CA_B·P_A − CA_A·P_B) / (CF_A·P_B − CF_B·P_A)
```

where `CA_x = P_x · coupon_x · (days/365)` is the coupon accrual and `P_x`
is the dirty cash price. Exact closed-form, not an approximation — the
identity `implied_repo_A(F*) == implied_repo_B(F*)` is checked directly in
`tests/test_pricing_baseline.py`.

**DV01 of the basis position** (long cash / short futures):

```
DV01_basis = DV01_cash − DV01_futures_CTD / CF_CTD
```

**90-day rolling percentile** of net basis, and a parallel-yield-shock
scenario grid that re-ranks the basket at each shift and flags any
resulting CTD switch. An MCP server exposes all of this — plus the
portfolio workbench's performance, attribution, controls, and report
tools — to Claude, which can synthesize a PM-ready morning brief on
demand via `/api/chat` covering either the CTD basis or the demo
portfolio (or both), calling real tools rather than inventing numbers.

### Architecture

```
core/          Pure analytics — no I/O, no side effects
  portfolio.py    Demo 8-security universe: weights, classifications
  returns.py      Portfolio/benchmark/active return, horizon resolution
  attribution.py  Carry / price-effect / residual decomposition
  controls.py     11 deterministic data-quality rules
  reporting.py    Assembles the structured report object
  commentary.py   AI or template narrative from the report object
  basket.py       TYM26 delivery basket + CME conversion factor formula
  pricing.py      price_bond(), ytm(), dv01(), modified_duration(), convexity()
  carry.py        gross_basis(), net_basis(), implied_repo(), carry()
  ctd.py          rank_basket(); closed-form F* transition threshold; basis DV01
  scenario.py     scenario_grid() — parallel yield shocks -> re-ranked basket

data/          Storage and feeds
  portfolio_db.py    PortfolioDB: SQLite for the demo portfolio (own DB file)
  portfolio_seed.py  Deterministic synthetic history for the demo portfolio
  db.py              BasisDB: SQLite schema for the CTD basis monitor
  fred_client.py     FRED API: 6 yield series, 5-min cache, maturity interpolation

mcp_server/    FastMCP server — 8 tools over stdio, JSON-RPC 2.0 (CTD engine)
api/           Flask: CTD routes + /api/portfolio/* routes + /api/chat (MCP client)
ui/            Single-file CTD dashboard (vanilla JS, Chart.js, IBM Plex Mono)
```

`api/app.py` fetches via `data/` and computes via `core/`; no calculation
logic lives in a route handler. The `/api/chat` endpoint is a real MCP
client — Flask spawns `mcp_server/server.py` as a subprocess, connects via
`ClientSession + stdio_client`, calls `list_tools()`, and executes each tool
via `call_tool()`.

### API endpoints

**Fixed Income Performance & Analytics Workbench:**

| Endpoint | Returns |
|---|---|
| `GET /api/portfolio/summary` | Holdings, weights, current risk metrics |
| `GET /api/portfolio/performance?horizon=` | Portfolio/benchmark/active return + risk |
| `GET /api/portfolio/contributors?horizon=` | Top positive/negative security contributors |
| `GET /api/portfolio/attribution?horizon=&level=` | Drilldown: portfolio / bucket / security |
| `GET /api/portfolio/holdings` | Full holdings table |
| `GET /api/portfolio/controls` | Data-quality status + issue list |
| `GET /api/portfolio/report?horizon=` | Full structured report |
| `GET /api/portfolio/security/<id>` | Single-security detail + history |
| `POST /api/portfolio/commentary` | AI or template commentary, clearly labeled |

`horizon` is one of `1D`, `MTD`, `QTD`, `YTD`.

These four are also exposed as MCP tools (`get_portfolio_performance`,
`get_portfolio_attribution`, `get_portfolio_controls`, `get_portfolio_report`)
so `/api/chat` can answer natural-language questions about the portfolio by
calling them directly, the same way it already does for the CTD tools below
— e.g. "why did the portfolio underperform this month?" triggers a real
`get_portfolio_attribution` call, not an invented answer.

**Advanced CTD analytics (unchanged, all pre-existing):**

| # | Tool / Endpoint | Returns |
|---|------|---------|
| 1 | `get_current_basket` / `/api/basket` | 12-bond basket ranked by implied repo; CTD flagged |
| 2 | `get_basis_history` / `/api/history/<cusip>` | 90-day net basis + 20-day MA + percentile rank |
| 3 | `get_basis_percentile` / `/api/percentile` | CTD basis position in its 90-day distribution |
| 4 | `get_ctd_transitions` / `/api/transitions` | Historical switch log with implied repo spread |
| 5 | `get_transition_proximity` / `/api/proximity` | Spread to runner-up (bps); risk flag |
| 6 | `run_scenario_grid` / `/api/scenarios` | Basket re-ranked under parallel yield shocks |
| 7 | `get_ctd_transition_threshold` / `/api/threshold` | F* and distance from current futures price |
| 8 | `get_carry_roll` / `/api/carry/<cusip>` | 3M/6M carry: coupon income vs. financing |

### Fixed income conventions

| Convention | Applied in |
|---|---|
| ACT/365 | Coupon accrual |
| ACT/360 | Repo financing |
| ACT/ACT (ICMA) | Accrued interest for bond pricing |
| Semi-annual compounding | All bond pricing |
| CME 6% standard yield | Conversion factor |

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# optional — enables AI commentary and the CTD /api/chat brief;
# everything else works without it
export ANTHROPIC_API_KEY=your_key
export FRED_API_KEY=your_key

# seed the demo portfolio (166 trading days, 8 securities)
python -m data.portfolio_seed --reset

# seed the CTD basis monitor (90 days, 12-bond basket)
python -m data.seed --reset --days 90

# run the test suite
pytest

# start the API
python -m api.app   # http://localhost:5001

# open the (CTD) dashboard — new endpoints aren't wired into this UI yet;
# the portfolio workbench frontend is being rebuilt separately (see
# FRONTEND_SPEC.md)
open ui/index.html
```

---

## Known limitations

| Limitation | Note |
|---|---|
| Synthetic data throughout | Both the demo portfolio and the CTD basket use deterministic synthetic history, not real market data |
| Benchmark is a re-weighting | The demo benchmark reuses the same 8-security universe at different weights — not a real market index (e.g. not actual Bloomberg Barclays Treasury Index constituents/weights) |
| "Region" is a constant | Single-issuer (US Treasury) demo — region is included as a schema field, not a meaningful attribution dimension here |
| Price-effect approximation | Duration/convexity only — no curve-shape change, roll-down, or re-estimation mid-period; the residual is shown explicitly rather than hidden |
| Parallel-shift scenarios only | The CTD scenario grid assumes parallel yield shifts, not curve-shape changes |
| Static CTD basket | TYM26 hardcoded; no auto-roll at contract expiry |
| Delivery option unpriced | CTD net basis residual includes quality/timing options, not separately priced |

**What I'd add for production use**, kept explicitly separate from what's
actually implemented above: validated market-data feeds (not synthetic),
accounting-system integration (e.g. Eagle or similar), real
transaction/corporate-action feeds, licensed benchmark constituent data,
formal composite construction and GIPS governance, and audit logging on
every calculation and report. This prototype does not claim GIPS
compliance, production-readiness, or integration with any real
institution's systems.

Reference: Burghardt, Belton, Lane, Papa — *The Treasury Bond Basis*.
