"""
api/app.py

Flask API — two responsibilities:

1. STATIC ENDPOINTS (direct calls, no MCP protocol)
   Fast endpoints for the dashboard panels — the 8 CTD tools imported
   directly below, plus the /api/portfolio/* routes further down which
   call core/reporting.py directly rather than importing MCP tool
   functions (same effect: fetch via data/, compute via core/, no MCP
   round-trip needed for a same-process call).

2. /api/chat  — THE REAL MCP ENDPOINT
   This is where MCP protocol actually runs.
   When the analyst asks a question:
     - Flask spawns the MCP server as a subprocess
     - Connects via real MCP stdio protocol (ClientSession + stdio_client)
     - Calls session.list_tools() to discover all 12 tools (8 CTD + 4 portfolio)
     - Sends tools + question to Claude via Anthropic API
     - Claude decides which tools to call (returns tool_use blocks)
     - Flask calls session.call_tool() for each — real MCP protocol calls
     - Sends results back to Claude
     - Claude synthesises a PM-ready brief
     - Flask returns: narrative + full tool call chain for UI visualisation

Usage:
  export ANTHROPIC_API_KEY=your_key
  python -m api.app
  Server: http://localhost:5001
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from flask import Flask, jsonify, request
from flask_cors import CORS
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core.commentary import generate_commentary
from core.reporting import generate_report
from data.db import BasisDB
from data.portfolio_db import PortfolioDB
from mcp_server.server import (
    get_basis_history,
    get_basis_percentile,
    get_carry_roll,
    get_ctd_transition_threshold,
    get_ctd_transitions,
    get_current_basket,
    get_transition_proximity,
    run_scenario_grid,
)

app  = Flask(__name__)
# allow_private_network=True: Chrome's Private Network Access policy blocks a
# public HTTPS page (e.g. a Lovable preview) from fetching a private-network
# target (localhost) unless the CORS preflight explicitly opts in via
# Access-Control-Allow-Private-Network: true. Without this, the browser
# hangs/blocks the request silently — this is a local dev API with no
# sensitive data, so opting in here is safe.
CORS(app, allow_private_network=True)
db   = BasisDB()
pf_db = PortfolioDB()

# path to MCP server entry point
MCP_SERVER_PATH = str(Path(__file__).parent.parent / "mcp_server" / "server.py")

# system prompt — tells Claude what context it's operating in
SYSTEM_PROMPT = """You are a fixed income analyst assistant with two areas of coverage:

1. A US Treasury futures CTD (Cheapest-to-Deliver) basis monitor — 8 tools
   (get_current_basket, get_basis_history, get_basis_percentile,
   get_ctd_transitions, get_transition_proximity, run_scenario_grid,
   get_ctd_transition_threshold, get_carry_roll). Active contract: TYM26
   (10-year Treasury futures, June 2026 delivery).

2. A synthetic demo Fixed Income Performance & Analytics Workbench — 4
   tools (get_portfolio_performance, get_portfolio_attribution,
   get_portfolio_controls, get_portfolio_report) covering an 8-security
   demo US Treasury portfolio. This is NOT real account data. Horizons are
   1D/MTD/QTD/YTD. When asked "why" the portfolio did something, call
   get_portfolio_attribution (or get_portfolio_report for the full
   picture) rather than guessing from performance numbers alone. If asked
   whether the numbers can be trusted, call get_portfolio_controls before
   answering — do not assert data quality without checking.

When answering questions:
- Always call the relevant tools to get live data before answering — never
  invent a number, especially a portfolio return, contribution, or control
  result; every figure must come from a tool call
- Be concise and precise — this is a desk brief, not a research report
- Lead with the actionable signal, then the supporting data
- Use fixed income terminology correctly (basis points, ticks, implied
  repo, duration, convexity, active return, etc.)
- For the CTD monitor: if the risk flag is ELEVATED or CRITICAL, make that
  the first thing you say
- For the portfolio workbench: if get_portfolio_controls reports status
  REVIEW, mention that before stating any return or attribution figure —
  don't present numbers as settled if there's an open data-quality issue
- If price_effect_approx / residual come up, be clear that the price
  effect is a duration+convexity approximation, not an exact repricing
"""


# ------------------------------------------------------------------
# /api/chat — Real MCP protocol endpoint
# ------------------------------------------------------------------

@app.post("/api/chat")
def chat():
    """
    The real MCP endpoint.

    Flow:
      1. Spawn mcp_server/server.py as subprocess
      2. Connect via MCP stdio protocol (ClientSession)
      3. Discover tools via session.list_tools()
      4. Send tools + user query to Claude (Anthropic API)
      5. Claude returns tool_use blocks
      6. Execute each via session.call_tool() — real MCP calls
      7. Feed results back to Claude
      8. Return narrative + tool call log to UI

    Returns:
      {
        narrative:  "Claude's synthesised PM brief",
        tool_calls: [
          { name, input, result }
        ]
      }
    """
    body = request.get_json()
    if not body or "question" not in body:
        return jsonify({"error": "Missing 'question' in request body"}), 400

    question = body["question"]
    api_key  = os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set — run: export ANTHROPIC_API_KEY=your_key"}), 500

    try:
        loop   = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:

            result = loop.run_until_complete(_run_mcp_chat(question, api_key))
        finally:
            loop.close()

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


async def _run_mcp_chat(question: str, api_key: str) -> dict:
    """
    Async core of the MCP chat flow.

    Spawns the MCP server subprocess, connects via stdio protocol,
    runs the full Claude tool-use loop, returns structured output.
    """
    anthropic_client = anthropic.Anthropic(api_key=api_key)

    # spawn server.py as a real subprocess — MCP stdio transport
    server_params = StdioServerParameters(
        command = sys.executable,   # use same Python interpreter
        args    = [MCP_SERVER_PATH],
        env     = {**os.environ},
    )

    tool_calls_log = []

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:

            # initialise MCP connection
            await session.initialize()

            # discover tools via MCP protocol — list_tools RPC call
            tools_response  = await session.list_tools()

            # convert MCP tool schemas to Anthropic API format
            anthropic_tools = [
                {
                    "name":         tool.name,
                    "description":  tool.description or "",
                    "input_schema": tool.inputSchema,
                }
                for tool in tools_response.tools
            ]

            # first Claude turn — send question with tools attached
            messages = [{"role": "user", "content": question}]

            response = anthropic_client.messages.create(
                model      = "claude-sonnet-4-20250514",
                max_tokens = 1024,
                system     = SYSTEM_PROMPT,
                tools      = anthropic_tools,
                messages   = messages,
            )

            # tool use loop — Claude may call multiple tools across turns
            while response.stop_reason == "tool_use":

                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

                # append full assistant response to message history
                messages.append({
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": b.text}
                        if b.type == "text"
                        else {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                        for b in response.content
                    ],
                })

                # execute each tool via real MCP call_tool RPC
                tool_results = []
                for block in tool_use_blocks:

                    mcp_result = await session.call_tool(
                        block.name,
                        arguments = block.input,
                    )

                    result_text = (
                        mcp_result.content[0].text
                        if mcp_result.content else "{}"
                    )

                    try:
                        result_data = json.loads(result_text)
                    except Exception:
                        result_data = result_text

                    # log tool call for UI visualisation
                    tool_calls_log.append({
                        "name":   block.name,
                        "input":  block.input,
                        "result": result_data,
                    })

                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result_text,
                    })

                messages.append({"role": "user", "content": tool_results})

                # next Claude turn with tool results
                response = anthropic_client.messages.create(
                    model      = "claude-sonnet-4-20250514",
                    max_tokens = 1024,
                    system     = SYSTEM_PROMPT,
                    tools      = anthropic_tools,
                    messages   = messages,
                )

            # extract final narrative text
            narrative = " ".join(
                b.text for b in response.content if hasattr(b, "text")
            ).strip()

    return {"narrative": narrative, "tool_calls": tool_calls_log}


# ------------------------------------------------------------------
# Static dashboard endpoints
# ------------------------------------------------------------------

def _contract() -> str:
    return request.args.get("contract", "TYM26")


@app.get("/api/status")
def status():
    import sqlite3
    try:
        conn   = sqlite3.connect(str(Path(__file__).parent.parent / "basis_monitor.db"))
        rows   = conn.execute("SELECT COUNT(*) FROM basis_snapshots").fetchone()[0]
        latest = conn.execute("SELECT MAX(snapshot_dt) FROM basis_snapshots").fetchone()[0]
        conn.close()
        return jsonify({"status": "ok", "total_rows": rows, "latest_snapshot": latest})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.get("/api/basket")
def basket():
    return jsonify(get_current_basket(_contract()))


@app.get("/api/history/<cusip>")
def history(cusip):
    days = int(request.args.get("days", 90))
    return jsonify(get_basis_history(cusip, _contract(), days))


@app.get("/api/percentile")
def percentile():
    days = int(request.args.get("days", 90))
    return jsonify(get_basis_percentile(_contract(), days))


@app.get("/api/transitions")
def transitions():
    return jsonify(get_ctd_transitions(_contract()))


@app.get("/api/proximity")
def proximity():
    return jsonify(get_transition_proximity(_contract()))


@app.get("/api/scenarios")
def scenarios():
    shifts_raw = request.args.get("shifts")
    shifts     = [int(x) for x in shifts_raw.split(",")] if shifts_raw else None
    return jsonify(run_scenario_grid(_contract(), shifts))


@app.get("/api/threshold")
def threshold():
    return jsonify(get_ctd_transition_threshold(_contract()))


@app.get("/api/carry/<cusip>")
def carry(cusip):
    repo_rate = request.args.get("repo_rate")
    rate      = float(repo_rate) if repo_rate else None
    return jsonify(get_carry_roll(cusip, _contract(), rate))


# ------------------------------------------------------------------
# Fixed Income Performance & Analytics Workbench endpoints
#
# Business logic lives entirely in core/ (returns, attribution, controls,
# reporting, commentary) and data/portfolio_db.py — routes only fetch via
# PortfolioDB and hand off to core.reporting.generate_report(), mirroring
# how the CTD routes above stay thin wrappers around mcp_server.server /
# core functions rather than embedding calculations inline.
# ------------------------------------------------------------------

VALID_HORIZONS = {"1D", "MTD", "QTD", "YTD"}


def _pf_horizon() -> str:
    horizon = request.args.get("horizon", "YTD")
    if horizon not in VALID_HORIZONS:
        raise ValueError(f"Invalid horizon '{horizon}' — must be one of {sorted(VALID_HORIZONS)}")
    return horizon


def _pf_load(horizon: str):
    """Fetch everything generate_report() needs for one horizon."""
    begin, end = pf_db.resolve_horizon_dates(horizon)
    securities = pf_db.get_securities()
    snap_begin = pf_db.get_snapshot_all(begin)
    snap_end   = pf_db.get_snapshot_all(end)
    history    = pf_db.get_history_all(begin, end)
    return securities, snap_begin, snap_end, history


def _serialize_security(sec: dict, snap: dict = None) -> dict:
    out = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in sec.items()}
    if snap:
        out.update({
            "price": snap.get("price"), "yield": snap.get("yield"),
            "accrued_interest": snap.get("accrued_interest"),
            "modified_duration": snap.get("modified_duration"),
            "convexity": snap.get("convexity"), "dv01": snap.get("dv01"),
        })
    return out


@app.get("/api/portfolio/summary")
def portfolio_summary():
    from core.reporting import compute_risk_metrics

    securities = pf_db.get_securities()
    latest = pf_db.get_latest_date()
    if latest is None:
        return jsonify({"error": "No portfolio data — run: python -m data.portfolio_seed --reset"}), 500

    snapshot = pf_db.get_snapshot_all(latest)
    risk = compute_risk_metrics(securities, snapshot, "portfolio_weight")
    holdings = [_serialize_security(sec, snapshot.get(sec["security_id"])) for sec in securities]

    return jsonify({"as_of": latest.isoformat(), "risk_metrics": risk, "holdings": holdings})


@app.get("/api/portfolio/performance")
def portfolio_performance():
    try:
        horizon = _pf_horizon()
        securities, snap_begin, snap_end, history = _pf_load(horizon)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    report = generate_report(securities, snap_begin, snap_end, history, horizon)
    return jsonify({
        "horizon": horizon, "as_of": report["as_of"],
        "performance": report["performance"], "risk_metrics": report["risk_metrics"],
    })


@app.get("/api/portfolio/contributors")
def portfolio_contributors():
    try:
        horizon = _pf_horizon()
        securities, snap_begin, snap_end, history = _pf_load(horizon)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    report = generate_report(securities, snap_begin, snap_end, history, horizon)
    return jsonify({"horizon": horizon, "contributors": report["contributors"]})


@app.get("/api/portfolio/attribution")
def portfolio_attribution():
    level = request.args.get("level", "bucket")
    if level not in ("portfolio", "bucket", "security"):
        return jsonify({"error": "level must be one of: portfolio, bucket, security"}), 400

    try:
        horizon = _pf_horizon()
        securities, snap_begin, snap_end, history = _pf_load(horizon)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    report = generate_report(securities, snap_begin, snap_end, history, horizon)

    if level == "portfolio":
        payload = report["executive_summary"]
    elif level == "security":
        payload = report["contributors"]
    else:
        payload = report["attribution_summary"]

    return jsonify({"horizon": horizon, "level": level, "attribution": payload})


@app.get("/api/portfolio/holdings")
def portfolio_holdings():
    securities = pf_db.get_securities()
    latest = pf_db.get_latest_date()
    if latest is None:
        return jsonify({"error": "No portfolio data — run: python -m data.portfolio_seed --reset"}), 500

    snapshot = pf_db.get_snapshot_all(latest)
    holdings = [_serialize_security(sec, snapshot.get(sec["security_id"])) for sec in securities]
    return jsonify({"as_of": latest.isoformat(), "holdings": holdings})


@app.get("/api/portfolio/controls")
def portfolio_controls():
    from core.controls import run_all_controls

    securities = pf_db.get_securities()
    latest = pf_db.get_latest_date()
    earliest = pf_db.get_earliest_date()
    if latest is None or earliest is None:
        return jsonify({"error": "No portfolio data — run: python -m data.portfolio_seed --reset"}), 500

    snapshot = pf_db.get_snapshot_all(latest)
    history = pf_db.get_history_all(earliest, latest)
    return jsonify(run_all_controls(securities, snapshot, history))


@app.get("/api/portfolio/report")
def portfolio_report():
    try:
        horizon = _pf_horizon()
        securities, snap_begin, snap_end, history = _pf_load(horizon)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(generate_report(securities, snap_begin, snap_end, history, horizon))


@app.get("/api/portfolio/security/<security_id>")
def portfolio_security(security_id):
    from core.attribution import security_contribution

    security = pf_db.get_security(security_id)
    if security is None:
        return jsonify({"error": f"Unknown security_id '{security_id}'"}), 404

    try:
        horizon = _pf_horizon()
        securities, snap_begin, snap_end, history = _pf_load(horizon)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    report = generate_report(securities, snap_begin, snap_end, history, horizon)

    contribution = next(
        (c for group in (report["contributors"]["top_positive"], report["contributors"]["top_negative"])
         for c in group if c["security_id"] == security_id),
        None,
    )
    if contribution is None and security_id in snap_begin and security_id in snap_end:
        contribution = security_contribution(
            security, snap_begin[security_id], snap_end[security_id], "portfolio_weight"
        )

    earliest = pf_db.get_earliest_date()
    latest = pf_db.get_latest_date()
    full_history = pf_db.get_history(security_id, earliest, latest)

    return jsonify({
        "security": _serialize_security(security),
        "horizon": horizon,
        "contribution": contribution,
        "history": [{**h, "date": h["date"].isoformat()} for h in full_history],
    })


@app.post("/api/portfolio/commentary")
def portfolio_commentary():
    body = request.get_json(silent=True) or {}
    horizon = body.get("horizon", "YTD")
    if horizon not in VALID_HORIZONS:
        return jsonify({"error": f"Invalid horizon '{horizon}' — must be one of {sorted(VALID_HORIZONS)}"}), 400

    try:
        securities, snap_begin, snap_end, history = _pf_load(horizon)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    report = generate_report(securities, snap_begin, snap_end, history, horizon)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    return jsonify(generate_commentary(report, api_key))


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    print("CTD Basis Monitor API — http://localhost:5001")
    print(f"MCP server: {MCP_SERVER_PATH}")
    print(f"ANTHROPIC_API_KEY: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'NOT SET'}")
    app.run(host="0.0.0.0", port=5001, debug=False)
