
# CTD Basis Monitor — TYM26

A fixed income relative value morning workflow system for US Treasury futures basis desks. Replaces a 20–30 minute manual Bloomberg brief with a 10-second AI-driven summary, using Model Context Protocol (MCP) as the integration layer between live market data and Claude.

---

## What It Does

Every trading day, a FIRV analyst must answer four questions before the market opens:

- Which Treasury bond is cheapest-to-deliver (CTD) against the active futures contract?
- How close is the CTD to switching to the runner-up?
- How does the basis behave across a range of yield scenarios?
- Is today's carry positive after overnight repo financing?

This system answers all four automatically. Claude connects to a live SQLite database via 8 MCP tools, reasons over 90 days of basis history, and returns a PM-ready brief in approximately 10 seconds.

---

## Architecture

```
UI (index.html)
      │  fetch()
      ▼
Flask API (api/app.py)
      │  MCP stdio protocol — ClientSession + stdio_client
      ▼
MCP Server (mcp_server/server.py)   ← 8 tools over JSON-RPC 2.0
      │  reads
      ▼
SQLite (basis_monitor.db)
      │  populated by
      ▼
Quant Engine (core/) + Data Layer (data/)
```

The `/api/chat` endpoint is a real MCP client. Flask spawns `mcp_server/server.py` as a subprocess, connects via `ClientSession + stdio_client`, calls `session.list_tools()` to discover all 8 tools, and executes each via `session.call_tool()`. The UI renders the full tool call chain alongside Claude's synthesised narrative.

---

## Project Structure

```
capula_risk/
├── core/
│   ├── basket.py         12 TYM26 bonds; CME conversion factor formula
│   ├── pricing.py        price_bond(), dv01(), modified_duration(), convexity(), ytm()
│   ├── carry.py          gross_basis(), carry(), net_basis(), implied_repo()
│   ├── ctd.py            rank_basket() by implied repo; closed-form F* threshold
│   └── scenario.py       scenario_grid() parallel yield shocks
├── data/
│   ├── db.py             BasisDB: SQLite schema, read/write API, ROW_NUMBER CTE
│   ├── seed.py           90-day synthetic history seeder (RNG seed=42, 7bps daily vol)
│   └── fred_client.py    FRED API client: 6 yield series, 5-min cache, interpolation
├── mcp_server/
│   └── server.py         FastMCP: 8 tools, stdio transport
├── api/
│   └── app.py            Flask: /api/chat runs real MCP; static endpoints for dashboard
├── ui/
│   └── index.html        Single-file dashboard: IBM Plex Mono, 10 panels, Claude chat
└── basis_monitor.db      SQLite: basis_snapshots (1,080 rows) + ctd_log
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install numpy scipy pandas fastmcp flask flask-cors mcp anthropic
```

### 2. Seed the database

```bash
cd capula_risk
python -m data.seed --reset --days 90
```

### 3. Set your Anthropic API key

```bash
export ANTHROPIC_API_KEY=your_key_here
```

### 4. Start Flask

```bash
python -m api.app
# Running on http://localhost:5001
```

### 5. Open the dashboard

```bash
open ui/index.html
# or double-click ui/index.html in Finder / Explorer
```

---

## The 8 MCP Tools

| # | Tool | What It Returns |
|---|------|-----------------|
| 1 | `get_current_basket` | Full 12-bond basket ranked by implied repo; CTD flagged |
| 2 | `get_basis_history` | 90-day net basis series + 20-day MA + percentile rank |
| 3 | `get_basis_percentile` | Where today's CTD basis sits in its 90-day distribution |
| 4 | `get_ctd_transitions` | Historical CTD switch log with spread at time of switch |
| 5 | `get_transition_proximity` | Spread to runner-up (bps), trend, risk flag LOW/ELEVATED/CRITICAL |
| 6 | `run_scenario_grid` | Basket re-ranked under parallel yield shocks (±100bps) |
| 7 | `get_ctd_transition_threshold` | Exact F* futures price at CTD switch; distance from current |
| 8 | `get_carry_roll` | 3M/6M carry: coupon income (ACT/365), financing (ACT/360), net bps |

---

## Testing MCP Directly

You can test the MCP protocol independently of Flask and the UI:

```python
# test_mcp.py
import asyncio, sys, os, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def test():
    server_params = StdioServerParameters(
        command = sys.executable,
        args    = ["mcp_server/server.py"],
        env     = {**os.environ},
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print([t.name for t in tools.tools])

            result = await session.call_tool(
                "get_transition_proximity",
                arguments={"contract": "TYM26"}
            )
            print(json.loads(result.content[0].text))

asyncio.run(test())
```

To inspect all 8 tools visually using the official MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python mcp_server/server.py
# Opens at http://localhost:5173
```

To connect directly from Claude Desktop, add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ctd-basis-monitor": {
      "command": "python",
      "args": ["/absolute/path/to/capula_risk/mcp_server/server.py"]
    }
  }
}
```

---

## Dashboard Panels

| Panel | Description |
|-------|-------------|
| KPI Strip | CTD bond, net basis + percentile, spread to runner, 3M carry, risk flag |
| Risk Banner | Colour-coded LOW / ELEVATED / CRITICAL; CRITICAL pulses; shows trend |
| Delivery Basket | 12 bonds ranked by implied repo; click any row to load its carry + history |
| 90-Day Basis Chart | Net basis time series + 20-day MA; updates on bond click |
| Transition Proximity | Spread in bps; bar coloured green/amber/red by risk level |
| Basis Percentile | Needle gauge on 90-day distribution; min/mean/max statistics |
| Carry Decomposition | Repo rate, coupon income, financing cost, 3M/6M net carry in bps |
| F* Threshold | Closed-form futures price at CTD switch; distance and direction |
| Scenario Grid | 9 yield shift cells; red highlight where CTD identity changes |
| Claude Chat | MCP tool call log (expandable JSON) + Claude's PM-ready narrative |

---

## Fixed Income Conventions

| Convention | Used In |
|------------|---------|
| ACT/365 | Coupon accrual (carry.py, carry_roll tool) |
| ACT/360 | Repo financing (carry.py, carry_roll tool) |
| ACT/ACT (ICMA) | Accrued interest for bond pricing (pricing.py) |
| Semi-annual compounding | All bond pricing (pricing.py) |
| CME 6% standard yield | Conversion factor computation (basket.py) |

---

## Known Limitations

| Limitation | Note |
|------------|------|
| FRED yield proxy | 10Y yield linearly interpolated to each bond maturity; production uses Bloomberg BDS per-bond yields |
| Static basket | TYM26 hardcoded; no auto-update on quarterly rolls |
| Synthetic history | Random walk from seed=42; not real market prices |
| Delivery option not priced | Net basis residual includes wildcard and quality options |
| Parallel shifts only | Scenario grid holds curve shape constant; no steepening/flattening |
| Single contract | TYM26 only; TU (2Y), FV (5Y), US (30Y) not yet implemented |

---

## Roadmap

**Phase 2 — Production hardening**
- Live FRED ingest on daily cron schedule
- Bloomberg BDS integration replacing yield proxy
- Contract roll logic (TYM26 to TYU26 at expiry)
- Add TU and US contracts

**Phase 3 — Desk integration**
- `get_morning_brief(contract)` single-shot full narrative tool
- `compare_contracts(contracts)` cross-contract basis comparison
- `check_repo_richness(cusip)` implied repo vs GC repo spread
- CRITICAL alert push notifications

**Phase 4 — Asset class expansion**
- Bund futures (FGBL), Gilt futures (L), JGB futures
- Unified FIRV morning brief across all four contracts

---

## Stack

| Component | Technology |
|-----------|------------|
| Quant engine | Python, numpy, scipy (Brent's method for YTM), pandas |
| MCP server | FastMCP, stdio transport, JSON-RPC 2.0 |
| MCP client | mcp Python SDK, ClientSession, stdio_client |
| AI | Anthropic API, claude-sonnet-4, tool_use loop |
| API | Flask, flask-cors |
| Database | SQLite |
| Data | FRED API (live) + synthetic seed |
| UI | Single HTML file, IBM Plex Mono, vanilla JS, Chart.js |

