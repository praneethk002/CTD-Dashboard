"""
api/app.py

Flask API — two responsibilities:

1. STATIC ENDPOINTS (Tools 1-8 direct)
   Fast endpoints for the dashboard panels.
   These call MCP tool functions directly as Python imports
   so the dashboard loads instantly on page open.

2. /api/chat  — THE REAL MCP ENDPOINT
   This is where MCP protocol actually runs.
   When the analyst asks a question:
     - Flask spawns the MCP server as a subprocess
     - Connects via real MCP stdio protocol (ClientSession + stdio_client)
     - Calls session.list_tools() to discover all 8 tools
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

from data.db import BasisDB
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
CORS(app)
db   = BasisDB()

# path to MCP server entry point
MCP_SERVER_PATH = str(Path(__file__).parent.parent / "mcp_server" / "server.py")

# system prompt — tells Claude what context it's operating in
SYSTEM_PROMPT = """You are a fixed income analyst assistant for a US Treasury futures basis desk.
You have access to 8 tools that query a live CTD (Cheapest-to-Deliver) basis monitor database.

When answering questions:
- Always call the relevant tools to get live data before answering
- Be concise and precise — this is a trading desk, not a research report
- Lead with the actionable signal, then the supporting data
- Use fixed income terminology correctly (basis points, ticks, implied repo, etc.)
- If the risk flag is ELEVATED or CRITICAL, make that the first thing you say

The active contract is TYM26 (10-year Treasury futures, June 2026 delivery).
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
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    print("CTD Basis Monitor API — http://localhost:5001")
    print(f"MCP server: {MCP_SERVER_PATH}")
    print(f"ANTHROPIC_API_KEY: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'NOT SET'}")
    app.run(host="0.0.0.0", port=5001, debug=False)
