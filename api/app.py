"""
api/app.py

Flask API serving dashboard endpoints for the CTD Basis Monitor.

Each endpoint calls MCP tool functions directly as Python imports
so the dashboard loads instantly on page open.

Usage:
  python -m api.app
  Server: http://localhost:5001
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

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

# Auto-seed database if empty (needed for ephemeral disks like Render free tier)
def _auto_seed():
    try:
        status = db.get_status()
        if status.get("total_rows", 0) == 0:
            from data.seed import seed
            seed(days=90, reset=True)
    except Exception:
        pass

_auto_seed()


# ------------------------------------------------------------------
# Dashboard endpoints
# ------------------------------------------------------------------

def _contract() -> str:
    return request.args.get("contract", "TYM26")


@app.get("/api/status")
def status():
    try:
        return jsonify(db.get_status())
    except Exception:
        return jsonify({"status": "error", "message": "Database unavailable"}), 500


@app.get("/api/basket")
def basket():
    return jsonify(get_current_basket(_contract()))


@app.get("/api/history/<cusip>")
def history(cusip):
    try:
        days = int(request.args.get("days", 90))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid 'days' parameter"}), 400
    return jsonify(get_basis_history(cusip, _contract(), days))


@app.get("/api/percentile")
def percentile():
    try:
        days = int(request.args.get("days", 90))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid 'days' parameter"}), 400
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
    try:
        shifts = [int(x) for x in shifts_raw.split(",")] if shifts_raw and shifts_raw.strip() else None
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid 'shifts' parameter — expected comma-separated integers"}), 400
    return jsonify(run_scenario_grid(_contract(), shifts))


@app.get("/api/threshold")
def threshold():
    return jsonify(get_ctd_transition_threshold(_contract()))


@app.get("/api/carry/<cusip>")
def carry(cusip):
    repo_rate = request.args.get("repo_rate")
    try:
        rate = float(repo_rate) if repo_rate else None
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid 'repo_rate' parameter"}), 400
    return jsonify(get_carry_roll(cusip, _contract(), rate))


# ------------------------------------------------------------------
# Serve frontend — single-process deployment
# ------------------------------------------------------------------

UI_DIR = str(Path(__file__).parent.parent / "ui")


@app.route("/")
def serve_ui():
    return send_from_directory(UI_DIR, "index.html")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    print("CTD Basis Monitor — http://localhost:5001")
    app.run(host="127.0.0.1", port=5001, debug=False)
