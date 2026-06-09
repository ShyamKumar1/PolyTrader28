#!/usr/bin/env python3
"""
dashboard.py — PolyTrader28 Live Web Dashboard
================================================
Flask-based web dashboard that displays real-time bot status, positions,
trade history, and an equity curve chart.

Can be run:
  1. Standalone:  python dashboard.py  (connects to the same SQLite DB)
  2. Integrated:  The main bot launches this in a background thread.

Endpoints:
  GET  /                     — HTML dashboard page
  GET  /api/status           — Bot status JSON
  GET  /api/trades?limit=50  — Recent trades JSON
  GET  /api/equity?range=7d  — Equity curve data JSON
  GET  /api/opportunities    — Detected opportunities JSON
  GET  /api/strategy-stats   — Per-strategy stats JSON
  GET  /api/today-stats      — Today's performance JSON
  POST /api/stop             — Stop the bot
  POST /api/start            — Start the bot
  POST /api/pause            — Pause trading
  POST /api/resume           — Resume trading

Usage:
    python dashboard.py           # Run standalone on port 8050
    python polymarket_bot.py      # Runs dashboard in background
"""

import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Any, Optional

from flask import Flask, jsonify, request

# Ensure the project root is on the path
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import config
from utils.logger import logger
from models import (
    get_current_bankroll, get_trade_stats, get_open_trades,
    get_recent_trades, get_equity_snapshots,
    get_opportunities, get_strategy_stats, get_today_stats,
)


# ---------------------------------------------------------------------------
# Flask app setup
# ---------------------------------------------------------------------------
app = Flask(__name__)

# Allow CORS for local development
@app.after_request
def _add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ---------------------------------------------------------------------------
# Bot control reference (set by polymarket_bot.py when integrated)
# ---------------------------------------------------------------------------
_bot_ref: Optional[Any] = None
"""Reference to the running PolyTraderBot instance, if integrated."""


def set_bot_instance(bot_instance: object) -> None:
    """
    Set the bot instance reference so the dashboard can control it.

    Args:
        bot_instance: The PolyTraderBot instance running the main loop.
    """
    global _bot_ref
    _bot_ref = bot_instance
    logger.info("Dashboard linked to bot instance")


# ---------------------------------------------------------------------------
# Authentication decorator for control endpoints
# ---------------------------------------------------------------------------

def _require_auth(f):
    """
    Decorator that checks DASHBOARD_API_KEY on /api/stop and /api/start.

    If DASHBOARD_API_KEY is empty (not configured), auth is skipped.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # If no API key configured, allow without auth
        if not config.dashboard_auth_enabled:
            return f(*args, **kwargs)

        # Check for API key in header or query param
        api_key = (
            request.headers.get("X-API-Key", "")
            or request.args.get("api_key", "")
        )
        if api_key != config.DASHBOARD_API_KEY:
            return jsonify({"error": "Unauthorized. Provide valid X-API-Key header."}), 401

        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db_connection() -> sqlite3.Connection:
    """Open a fresh read-only-ish connection for inline queries."""
    from models import DB_PATH
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _compute_daily_drawdown() -> float:
    """Compute max drawdown % from today's equity snapshots."""
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
        cursor.execute(
            "SELECT bankroll_usdc FROM equity_snapshots "
            "WHERE timestamp >= ? ORDER BY timestamp ASC",
            (today_start,),
        )
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return 0.0
        peak = rows[0]["bankroll_usdc"]
        max_dd = 0.0
        for row in rows:
            val = row["bankroll_usdc"]
            if val > peak:
                peak = val
            dd = (peak - val) / peak * 100 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return round(max_dd, 2)
    except Exception:
        return 0.0


def _compute_consecutive_wins() -> int:
    """Count consecutive winning trades from most recent closed trade."""
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT win FROM trades "
            "WHERE exit_price IS NOT NULL AND win IS NOT NULL "
            "ORDER BY timestamp DESC"
        )
        rows = cursor.fetchall()
        conn.close()
        streak = 0
        for row in rows:
            if row["win"]:
                streak += 1
            else:
                break
        return streak
    except Exception:
        return 0


def _format_time_held(ts_iso: str) -> str:
    """Format ISO timestamp to human-readable time held."""
    try:
        opened = datetime.fromisoformat(ts_iso)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - opened
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m {secs % 60}s"
        hours = mins // 60
        mins = mins % 60
        if hours < 24:
            return f"{hours}h {mins}m"
        days = hours // 24
        return f"{days}d {hours % 24}h"
    except Exception:
        return "—"


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def api_status():
    """
    GET /api/status
    Returns current bot status as JSON.

    If integrated with a running bot, gets live status from the bot instance.
    Otherwise, returns database-derived status.
    """
    global _bot_ref
    bankroll = get_current_bankroll()
    stats = get_trade_stats()
    open_trades_list = get_open_trades()

    # Build open positions
    positions = []
    for t in open_trades_list:
        entry = t["entry_price"]
        current = entry  # In production, fetch live mid-price
        pnl_pct = 0.0
        if entry > 0:
            pnl_pct = round(((current - entry) / entry) * 100, 2)
        positions.append({
            "market": t["market"],
            "side": t["side"],
            "strategy": t.get("strategy", "—"),
            "entry": round(entry, 4),
            "current": round(current, 4),
            "pnl_pct": pnl_pct,
            "trade_id": t["id"],
            "time_held": _format_time_held(t["timestamp"]),
        })

    # Daily P&L
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    daily_pnl = 0.0
    try:
        recent = get_recent_trades(limit=500)
        for t in recent:
            if t["timestamp"] >= today_start and t["profit_usdc"] is not None:
                daily_pnl += t["profit_usdc"]
    except Exception:
        pass

    # Get running state from bot reference if available
    is_running = True
    is_paused = False
    uptime = 0
    if _bot_ref is not None:
        try:
            is_running = _bot_ref.is_running
            is_paused = _bot_ref.is_paused
            status = _bot_ref.get_status()
            uptime = status.get("uptime_seconds", 0)
        except Exception:
            pass

    # Count opportunities
    opps_detected = 0
    opps_executed = 0
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS cnt FROM opportunities")
        row = cursor.fetchone()
        if row:
            opps_detected = row["cnt"]
        cursor.execute("SELECT COUNT(*) AS cnt FROM opportunities WHERE executed = 1")
        row = cursor.fetchone()
        if row:
            opps_executed = row["cnt"]
        conn.close()
    except Exception:
        pass

    # Compute max position size from config
    max_position_size = bankroll * (config.MAX_POSITION_PCT / 100.0)

    # Get markets tracked from bot reference
    markets_tracked = []
    if _bot_ref is not None:
        try:
            markets_tracked = [
                m.get("question", "Unknown")[:60]
                for m in getattr(_bot_ref, '_cached_15m_markets', [])
            ]
        except Exception:
            pass

    return jsonify({
        "bankroll_usdc": round(bankroll, 2),
        "open_positions": positions,
        "win_rate": stats.get("win_rate") or 0.0,
        "total_trades": stats.get("total_trades") or 0,
        "daily_pnl_usdc": round(daily_pnl, 2),
        "is_running": is_running,
        "is_paused": is_paused,
        "mode": "live" if config.is_live else "dry_run",
        "uptime_seconds": uptime,
        "opportunities_detected": opps_detected,
        "opportunities_executed": opps_executed,
        "consecutive_wins": _compute_consecutive_wins(),
        "daily_drawdown_pct": _compute_daily_drawdown(),
        "max_position_size": round(max_position_size, 2),
        "markets_tracked": markets_tracked,
        "last_update": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/trades", methods=["GET"])
def api_trades():
    """
    GET /api/trades?limit=50
    Returns the most recent trades as JSON.
    """
    try:
        limit = int(request.args.get("limit", 50))
        limit = max(1, min(500, limit))
    except (ValueError, TypeError):
        limit = 50

    trades = get_recent_trades(limit=limit)
    result = []
    for t in trades:
        result.append({
            "timestamp": t["timestamp"],
            "market": t["market"],
            "side": t["side"],
            "strategy": t["strategy"],
            "entry_price": t["entry_price"],
            "exit_price": t["exit_price"],
            "quantity": t["quantity"],
            "profit_usdc": t["profit_usdc"],
            "win": bool(t["win"]) if t["win"] is not None else None,
            "is_dry_run": bool(t["is_dry_run"]),
        })

    return jsonify(result)


@app.route("/api/equity", methods=["GET"])
def api_equity():
    """
    GET /api/equity?range=7d
    Returns equity curve data (timestamps and bankroll values).

    Query params:
        range: "1d", "7d", "30d", or "all" (default "7d")
    """
    range_param = request.args.get("range", "7d")
    range_map = {
        "1h": 1, "6h": 1, "1d": 1, "7d": 7, "30d": 30, "all": 365 * 5,
    }
    range_days = range_map.get(range_param, 7)

    snapshots = get_equity_snapshots(range_days=range_days)
    result = {
        "timestamps": [s["timestamp"] for s in snapshots],
        "bankroll": [s["bankroll_usdc"] for s in snapshots],
    }
    return jsonify(result)


@app.route("/api/opportunities", methods=["GET"])
def api_opportunities():
    """GET /api/opportunities?limit=30 — Recent detected opportunities."""
    try:
        limit = int(request.args.get("limit", 30))
        limit = max(1, min(500, limit))
    except (ValueError, TypeError):
        limit = 30
    opps = get_opportunities(limit=limit)
    return jsonify([{
        "timestamp": o["timestamp"],
        "market": o["market"],
        "strategy": o["strategy"],
        "edge_pct": o["edge_pct"],
        "yes_price": o["yes_price"],
        "no_price": o["no_price"],
        "executed": bool(o["executed"]),
        "reason": o["reason"],
    } for o in opps])


@app.route("/api/strategy-stats", methods=["GET"])
def api_strategy_stats():
    """GET /api/strategy-stats — Per-strategy breakdown."""
    return jsonify(get_strategy_stats())


@app.route("/api/today-stats", methods=["GET"])
def api_today_stats():
    """GET /api/today-stats — Today's performance."""
    stats = get_today_stats()
    stats["bankroll"] = get_current_bankroll()
    return jsonify(stats)


@app.route("/api/stop", methods=["POST"])
@_require_auth
def api_stop():
    """
    POST /api/stop
    Stops the bot gracefully. Requires DASHBOARD_API_KEY if configured.
    """
    global _bot_ref
    if _bot_ref is None:
        return jsonify({"error": "Bot instance not available"}), 503

    try:
        _bot_ref.stop()
        logger.warning("Dashboard: bot stop requested")
        return jsonify({"success": True, "message": "Bot stopping..."})
    except Exception as exc:
        logger.error("Dashboard stop failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/start", methods=["POST"])
@_require_auth
def api_start():
    """
    POST /api/start
    Resumes the bot after a stop. Requires DASHBOARD_API_KEY if configured.
    """
    global _bot_ref
    if _bot_ref is None:
        return jsonify({"error": "Bot instance not available"}), 503

    try:
        _bot_ref.resume()
        logger.warning("Dashboard: bot start requested")
        return jsonify({"success": True, "message": "Bot resuming..."})
    except Exception as exc:
        logger.error("Dashboard start failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/pause", methods=["POST"])
@_require_auth
def api_pause():
    """
    POST /api/pause
    Pauses trading (bot continues monitoring but won't enter new trades).
    """
    global _bot_ref
    if _bot_ref is None:
        return jsonify({"error": "Bot instance not available"}), 503

    try:
        _bot_ref.pause()
        return jsonify({"success": True, "message": "Trading paused"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/resume", methods=["POST"])
@_require_auth
def api_resume():
    """
    POST /api/resume
    Resumes trading after a pause.
    """
    global _bot_ref
    if _bot_ref is None:
        return jsonify({"error": "Bot instance not available"}), 503

    try:
        _bot_ref.resume()
        return jsonify({"success": True, "message": "Trading resumed"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Frontend: Single HTML page
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """
    Serve the main dashboard HTML page.

    The HTML is fully self-contained with inline CSS and JS.
    Chart.js is loaded from CDN.
    """
    return HTML_PAGE


# ---------------------------------------------------------------------------
# Dashboard HTML template (inline, fully self-contained)
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PolyTrader28 — Trading Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
/* ═══════════════════════════════════════════════════════════════════════════
   CSS Variables & Reset
   ═══════════════════════════════════════════════════════════════════════════ */
:root {
  --bg-primary: #0a0a0f;
  --bg-card: #12121a;
  --bg-card-hover: #18182a;
  --bg-elevated: #1a1a28;
  --border: #1e1e2e;
  --border-light: #2a2a3e;

  --green: #00e676;
  --green-bg: rgba(0, 230, 118, 0.12);
  --green-dim: rgba(0, 230, 118, 0.06);
  --red: #ff5252;
  --red-bg: rgba(255, 82, 82, 0.12);
  --red-dim: rgba(255, 82, 82, 0.06);
  --amber: #ffc107;
  --amber-bg: rgba(255, 193, 7, 0.12);
  --blue: #448aff;
  --blue-bg: rgba(68, 138, 255, 0.12);

  --text-primary: #e0e0e0;
  --text-secondary: #9e9e9e;
  --text-muted: #616161;

  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;

  --nav-height: 64px;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html { font-size: 14px; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }

body {
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: var(--font-sans);
  line-height: 1.5;
  min-height: 100vh;
  overflow-x: hidden;
  padding-top: var(--nav-height);
}

/* ═══ Scrollbar ═══════════════════════════════════════════════════════════ */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-light); border-radius: 2px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* ═══ Top Navigation Bar ══════════════════════════════════════════════════ */
.nav {
  position: fixed;
  top: 0; left: 0; right: 0;
  height: var(--nav-height);
  z-index: 1000;
  background: rgba(10, 10, 15, 0.85);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
}

.nav-left {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-shrink: 0;
}

.nav-logo {
  font-size: 18px;
  font-weight: 800;
  color: var(--text-primary);
  letter-spacing: -0.5px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.nav-logo .logo-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  background: linear-gradient(135deg, var(--green), #00bcd4);
  border-radius: 6px;
  font-size: 14px;
  color: #000;
  font-weight: 700;
}
.nav-logo .logo-sub {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-muted);
  letter-spacing: 0.3px;
  margin-left: 4px;
}

.nav-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}
.nav-status .dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.nav-status .dot.running { background: var(--green); box-shadow: 0 0 8px rgba(0,230,118,0.5); }
.nav-status .dot.paused  { background: var(--amber); box-shadow: 0 0 8px rgba(255,193,7,0.5); }
.nav-status .dot.stopped { background: var(--red); box-shadow: 0 0 8px rgba(255,82,82,0.5); }

.nav-center {
  text-align: center;
  flex: 1;
  padding: 0 24px;
}
.nav-bankroll {
  font-size: 26px;
  font-weight: 700;
  font-family: var(--font-mono);
  color: var(--text-primary);
  letter-spacing: -0.5px;
  line-height: 1.2;
}
.nav-bankroll .currency {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-muted);
  margin-left: 4px;
}
.nav-pnl {
  font-size: 13px;
  font-weight: 600;
  font-family: var(--font-mono);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.nav-pnl.positive { color: var(--green); }
.nav-pnl.negative { color: var(--red); }

.nav-right {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-shrink: 0;
}

.nav-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.5px;
  text-transform: uppercase;
}
.nav-badge.dry-run {
  background: var(--amber-bg);
  color: var(--amber);
  border: 1px solid rgba(255,193,7,0.25);
}
.nav-badge.live {
  background: var(--green-bg);
  color: var(--green);
  border: 1px solid rgba(0,230,118,0.25);
}

.nav-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 11px;
  color: var(--text-secondary);
}

.nav-meta .connector {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.nav-meta .connector .dot {
  width: 5px; height: 5px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 4px rgba(0,230,118,0.4);
}

/* ═══ Page Container ══════════════════════════════════════════════════════ */
.page {
  max-width: 1440px;
  margin: 0 auto;
  padding: 20px 24px 100px 24px;
}

/* ═══ Key Metrics Row ═════════════════════════════════════════════════════ */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 20px;
}
@media (max-width: 1000px) {
  .metrics-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 600px) {
  .metrics-grid { grid-template-columns: 1fr; }
}

.metric-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 16px 18px;
  transition: border-color 0.2s, background 0.2s;
  cursor: default;
}
.metric-card:hover {
  border-color: var(--border-light);
  background: var(--bg-card-hover);
}
.metric-card .metric-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.8px;
  color: var(--text-muted);
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.metric-card .metric-label .icon {
  font-size: 14px;
  opacity: 0.5;
}
.metric-card .metric-value {
  font-size: 24px;
  font-weight: 700;
  font-family: var(--font-mono);
  color: var(--text-primary);
  line-height: 1.2;
}
.metric-card .metric-value.green { color: var(--green); }
.metric-card .metric-value.red { color: var(--red); }
.metric-card .metric-value.amber { color: var(--amber); }
.metric-card .metric-sub {
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: 4px;
}

/* Progress bar inside metric */
.metric-progress {
  margin-top: 8px;
  height: 4px;
  background: var(--bg-elevated);
  border-radius: 2px;
  overflow: hidden;
}
.metric-progress .bar {
  height: 100%;
  border-radius: 2px;
  transition: width 0.5s ease;
}
.metric-progress .bar.green { background: var(--green); }
.metric-progress .bar.red { background: var(--red); }
.metric-progress .bar.amber { background: var(--amber); }

/* ═══ Two-Column Layout ═══════════════════════════════════════════════════ */
.main-grid {
  display: grid;
  grid-template-columns: 1.9fr 1.1fr;
  gap: 16px;
  margin-bottom: 20px;
}
@media (max-width: 1000px) {
  .main-grid { grid-template-columns: 1fr; }
}

/* ═══ Cards ═══════════════════════════════════════════════════════════════ */
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  overflow: hidden;
  transition: border-color 0.2s;
}
.card:hover {
  border-color: var(--border-light);
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px 10px 18px;
  border-bottom: 1px solid var(--border);
}
.card-title {
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-title .badge-count {
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-size: 10px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 10px;
  font-family: var(--font-mono);
}
.card-body {
  padding: 0;
}

.card-section {
  padding: 16px 18px;
  border-bottom: 1px solid var(--border);
}
.card-section:last-child {
  border-bottom: none;
}

/* ═══ Tables ══════════════════════════════════════════════════════════════ */
.table-wrap {
  overflow-x: auto;
  max-height: 340px;
  overflow-y: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
thead th {
  text-align: left;
  padding: 10px 14px;
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.8px;
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  background: var(--bg-card);
  z-index: 2;
}

tbody td {
  padding: 9px 14px;
  border-bottom: 1px solid rgba(30, 30, 46, 0.5);
  white-space: nowrap;
  font-size: 12.5px;
}
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: rgba(255,255,255,0.02); }
tbody tr:nth-child(even) td { background: rgba(255,255,255,0.015); }
tbody tr:nth-child(even):hover td { background: rgba(255,255,255,0.035); }

td .mono {
  font-family: var(--font-mono);
  font-size: 12px;
}

td .green { color: var(--green); }
td .red { color: var(--red); }
td .amber { color: var(--amber); }
td .muted { color: var(--text-muted); }

/* Empty state */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 36px 20px;
  color: var(--text-muted);
  text-align: center;
}
.empty-state .empty-icon {
  font-size: 32px;
  opacity: 0.3;
  margin-bottom: 8px;
}
.empty-state .empty-text {
  font-size: 13px;
  font-weight: 500;
}
.empty-state .empty-sub {
  font-size: 11px;
  margin-top: 2px;
  opacity: 0.7;
}

/* ═══ Badges ══════════════════════════════════════════════════════════════ */
.badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.3px;
  text-transform: uppercase;
}
.badge-win {
  background: var(--green-bg);
  color: var(--green);
}
.badge-loss {
  background: var(--red-bg);
  color: var(--red);
}
.badge-open {
  background: var(--blue-bg);
  color: var(--blue);
}
.badge-missed {
  background: rgba(97, 97, 97, 0.2);
  color: var(--text-muted);
}
.badge-executed {
  background: var(--green-bg);
  color: var(--green);
}

/* ═══ Sidebar Content ═════════════════════════════════════════════════════ */
.strategy-card {
  padding: 14px 0;
}
.strategy-card + .strategy-card {
  border-top: 1px solid var(--border);
}
.strategy-card .strat-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 8px;
}
.strategy-card .strat-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.strategy-card .strat-stat {
  text-align: center;
  padding: 8px 4px;
  background: rgba(255,255,255,0.02);
  border-radius: var(--radius-sm);
}
.strategy-card .strat-stat .ss-value {
  font-size: 16px;
  font-weight: 700;
  font-family: var(--font-mono);
}
.strategy-card .strat-stat .ss-label {
  font-size: 10px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-top: 2px;
}

/* Risk metric row */
.risk-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 0;
  font-size: 13px;
}
.risk-row .risk-label {
  color: var(--text-secondary);
}
.risk-row .risk-value {
  font-family: var(--font-mono);
  font-weight: 600;
  font-size: 13px;
}

/* Markets list */
.market-list {
  max-height: 120px;
  overflow-y: auto;
  font-size: 12px;
}
.market-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  color: var(--text-secondary);
}
.market-item::before {
  content: '';
  width: 4px; height: 4px;
  border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
}

/* ═══ Equity Chart ════════════════════════════════════════════════════════ */
.chart-section {
  margin-bottom: 20px;
}
.chart-container {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 16px 18px;
  transition: border-color 0.2s;
}
.chart-container:hover {
  border-color: var(--border-light);
}
.chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
  flex-wrap: wrap;
  gap: 8px;
}
.chart-range-group {
  display: flex;
  gap: 4px;
}
.chart-range-btn {
  font-family: var(--font-sans);
  font-size: 11px;
  font-weight: 600;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s;
  background: transparent;
  color: var(--text-muted);
}
.chart-range-btn:hover {
  border-color: var(--border-light);
  color: var(--text-secondary);
  background: rgba(255,255,255,0.03);
}
.chart-range-btn.active {
  background: var(--green-bg);
  border-color: rgba(0,230,118,0.3);
  color: var(--green);
}
.chart-wrap {
  position: relative;
  height: 260px;
  width: 100%;
}

/* ═══ Opportunity Log ═════════════════════════════════════════════════════ */
.opp-section {
  margin-bottom: 20px;
}
.opp-toggle {
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 8px;
}
.opp-toggle .chevron {
  transition: transform 0.25s ease;
  font-size: 10px;
  color: var(--text-muted);
}
.opp-toggle.collapsed .chevron {
  transform: rotate(-90deg);
}
.opp-body {
  overflow: hidden;
  transition: max-height 0.35s ease, opacity 0.25s ease;
  max-height: 600px;
  opacity: 1;
}
.opp-body.hidden {
  max-height: 0;
  opacity: 0;
}

/* ═══ Control Bar ═════════════════════════════════════════════════════════ */
.control-bar {
  position: fixed;
  bottom: 20px;
  right: 24px;
  z-index: 999;
  display: flex;
  gap: 8px;
  background: rgba(10,10,15,0.8);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 10px 14px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.5);
}

.ctrl-btn {
  font-family: var(--font-sans);
  font-size: 11px;
  font-weight: 700;
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all 0.15s ease;
  border: 1px solid transparent;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  background: transparent;
}
.ctrl-btn:active {
  transform: scale(0.96);
}
.ctrl-btn.ctrl-stop {
  border-color: rgba(255,82,82,0.3);
  color: var(--red);
}
.ctrl-btn.ctrl-stop:hover {
  background: var(--red-bg);
  border-color: var(--red);
}
.ctrl-btn.ctrl-pause {
  border-color: rgba(255,193,7,0.3);
  color: var(--amber);
}
.ctrl-btn.ctrl-pause:hover {
  background: var(--amber-bg);
  border-color: var(--amber);
}
.ctrl-btn.ctrl-resume {
  border-color: rgba(0,230,118,0.3);
  color: var(--green);
}
.ctrl-btn.ctrl-resume:hover {
  background: var(--green-bg);
  border-color: var(--green);
}

/* ═══ Confirmation Modal ══════════════════════════════════════════════════ */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  backdrop-filter: blur(4px);
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s ease;
}
.modal-overlay.show {
  opacity: 1;
  pointer-events: all;
}
.modal-box {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 28px 32px;
  max-width: 400px;
  width: 90%;
  text-align: center;
}
.modal-box h3 {
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 10px;
}
.modal-box p {
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 22px;
  line-height: 1.5;
}
.modal-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
}
.modal-actions button {
  font-family: var(--font-sans);
  font-size: 12px;
  font-weight: 600;
  padding: 10px 24px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all 0.15s;
  border: 1px solid var(--border);
}
.modal-actions .modal-cancel {
  background: transparent;
  color: var(--text-secondary);
}
.modal-actions .modal-cancel:hover {
  background: rgba(255,255,255,0.05);
}
.modal-actions .modal-confirm {
  background: var(--red-bg);
  border-color: rgba(255,82,82,0.3);
  color: var(--red);
}
.modal-actions .modal-confirm:hover {
  background: var(--red);
  color: #fff;
}
.modal-actions .modal-confirm.amber {
  background: var(--amber-bg);
  border-color: rgba(255,193,7,0.3);
  color: var(--amber);
}
.modal-actions .modal-confirm.amber:hover {
  background: var(--amber);
  color: #000;
}
.modal-actions .modal-confirm.green {
  background: var(--green-bg);
  border-color: rgba(0,230,118,0.3);
  color: var(--green);
}
.modal-actions .modal-confirm.green:hover {
  background: var(--green);
  color: #000;
}

/* ═══ Loading Skeleton ════════════════════════════════════════════════════ */
@keyframes shimmer {
  0% { background-position: -200px 0; }
  100% { background-position: calc(200px + 100%) 0; }
}
.skeleton {
  background: linear-gradient(90deg, var(--bg-card) 25%, var(--bg-elevated) 50%, var(--bg-card) 75%);
  background-size: 200px 100%;
  animation: shimmer 1.5s ease-in-out infinite;
  border-radius: 4px;
  color: transparent !important;
  user-select: none;
}

/* ═══ Page fade-in ════════════════════════════════════════════════════════ */
@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}
.page > * {
  animation: fadeInUp 0.4s ease forwards;
  opacity: 0;
}
.page > *:nth-child(1) { animation-delay: 0.05s; }
.page > *:nth-child(2) { animation-delay: 0.10s; }
.page > *:nth-child(3) { animation-delay: 0.15s; }
.page > *:nth-child(4) { animation-delay: 0.20s; }
.page > *:nth-child(5) { animation-delay: 0.25s; }

/* ═══ Responsive ══════════════════════════════════════════════════════════ */
@media (max-width: 1200px) {
  .nav-bankroll { font-size: 22px; }
  .nav-meta .connector-text { display: none; }
}
@media (max-width: 768px) {
  body { padding-top: 56px; }
  :root { --nav-height: 56px; }
  .page { padding: 12px 12px 80px 12px; }
  .nav { padding: 0 12px; }
  .nav-logo .logo-sub { display: none; }
  .nav-bankroll { font-size: 18px; }
  .nav-meta { gap: 8px; }
  .nav-meta .uptime-text { display: none; }
  .chart-wrap { height: 180px; }
  .control-bar {
    bottom: 12px;
    right: 12px;
    left: 12px;
    justify-content: center;
  }
  .ctrl-btn { flex: 1; text-align: center; font-size: 10px; padding: 8px 8px; }
}
</style>
</head>
<body>

<!-- ═══════════════════════════════════════════════════════════════════════
     Top Navigation Bar
     ═══════════════════════════════════════════════════════════════════════ -->
<nav class="nav" id="topNav">
  <div class="nav-left">
    <div class="nav-logo">
      <span class="logo-icon">P</span>
      PolyTrader28
      <span class="logo-sub">Terminal</span>
    </div>
    <div class="nav-status" id="navStatus">
      <span class="dot stopped" id="navDot"></span>
      <span id="navStatusText">Connecting...</span>
    </div>
  </div>

  <div class="nav-center">
    <div class="nav-bankroll" id="navBankroll">
      <span id="navBankrollVal">$0.00</span>
      <span class="currency">USDC</span>
    </div>
    <div class="nav-pnl" id="navPnl">▲ +$0.00 today</div>
  </div>

  <div class="nav-right">
    <span class="nav-badge dry-run" id="navMode">DRY RUN</span>
    <div class="nav-meta">
      <span class="uptime-text" id="navUptime">0h 0m</span>
      <span class="connector"><span class="dot"></span><span class="connector-text">Binance</span></span>
      <span class="connector"><span class="dot"></span><span class="connector-text">Polymarket</span></span>
    </div>
  </div>
</nav>

<!-- ═══════════════════════════════════════════════════════════════════════
     Page Content
     ═══════════════════════════════════════════════════════════════════════ -->
<div class="page">

  <!-- ═══ Key Metrics Row ══════════════════════════════════════════════ -->
  <div class="metrics-grid">
    <!-- Active Positions -->
    <div class="metric-card">
      <div class="metric-label"><span class="icon">&#9679;</span> Active Positions</div>
      <div class="metric-value" id="metricPositions">0 / 3</div>
      <div class="metric-progress"><div class="bar" id="metricPosBar" style="width:0%"></div></div>
    </div>

    <!-- Win Rate -->
    <div class="metric-card">
      <div class="metric-label"><span class="icon">&#9650;</span> Win Rate</div>
      <div class="metric-value" id="metricWinRate">0.0%</div>
      <div class="metric-sub" id="metricWinSub">0W / 0L</div>
    </div>

    <!-- Opportunities -->
    <div class="metric-card">
      <div class="metric-label"><span class="icon">&#9889;</span> Opportunities</div>
      <div class="metric-value amber" id="metricOpps">0</div>
      <div class="metric-sub" id="metricOppsSub">0 detected &middot; 0 executed</div>
    </div>

    <!-- Daily Drawdown -->
    <div class="metric-card">
      <div class="metric-label"><span class="icon">&#9660;</span> Daily Drawdown</div>
      <div class="metric-value" id="metricDrawdown">0.00%</div>
      <div class="metric-progress"><div class="bar green" id="metricDdBar" style="width:0%"></div></div>
    </div>
  </div>

  <!-- ═══ Main Two-Column Layout ══════════════════════════════════════ -->
  <div class="main-grid">

    <!-- ─── Left Column ────────────────────────────────────────────── -->
    <div class="left-col" style="display:flex;flex-direction:column;gap:16px;">

      <!-- Active Positions Table -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Active Positions <span class="badge-count" id="posCount">0</span></span>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Market</th><th>Side</th><th>Strategy</th><th>Entry</th><th>Current</th><th>P&amp;L %</th><th>Held</th>
            </tr></thead>
            <tbody id="positionsBody">
              <tr><td colspan="7">
                <div class="empty-state">
                  <div class="empty-icon">&#9654;</div>
                  <div class="empty-text">No open positions</div>
                  <div class="empty-sub">Waiting for trade signals...</div>
                </div>
              </td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Recent Trades Table -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Recent Trades <span class="badge-count" id="tradeCount">0</span></span>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Time</th><th>Market</th><th>Side</th><th>Strategy</th><th>Entry &rarr; Exit</th><th>P&amp;L</th><th>Result</th>
            </tr></thead>
            <tbody id="tradesBody">
              <tr><td colspan="7">
                <div class="empty-state">
                  <div class="empty-icon">&#9632;</div>
                  <div class="empty-text">No trades yet</div>
                  <div class="empty-sub">Trade history will appear here</div>
                </div>
              </td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ─── Right Column ───────────────────────────────────────────── -->
    <div class="right-col" style="display:flex;flex-direction:column;gap:16px;">

      <!-- Strategy Performance -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Strategy Performance</span>
        </div>
        <div class="card-body" id="strategyCards">
          <!-- Populated by JS -->
          <div class="empty-state">
            <div class="empty-icon">&#9881;</div>
            <div class="empty-text">Loading strategies...</div>
          </div>
        </div>
      </div>

      <!-- Risk Metrics -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Risk Metrics</span>
        </div>
        <div class="card-body" style="padding:12px 18px;">
          <div class="risk-row">
            <span class="risk-label">Max Position Size</span>
            <span class="risk-value" id="riskMaxPos">$0.00</span>
          </div>
          <div class="risk-row">
            <span class="risk-label">Position Utilization</span>
            <span class="risk-value" id="riskUtilization">0.0%</span>
          </div>
          <div class="risk-row">
            <span class="risk-label">Win Streak</span>
            <span class="risk-value green" id="riskStreak">0</span>
          </div>
          <div class="risk-row">
            <span class="risk-label">Total Trades</span>
            <span class="risk-value" id="riskTotalTrades">0</span>
          </div>
        </div>
      </div>

      <!-- Markets Tracked -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Markets Tracked <span class="badge-count" id="marketCount">0</span></span>
        </div>
        <div class="card-body" style="padding:10px 18px;">
          <div class="market-list" id="marketList">
            <div class="market-item" style="color:var(--text-muted);font-style:italic;">Loading markets...</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ Equity Curve Chart ═════════════════════════════════════════ -->
  <div class="chart-section">
    <div class="chart-container">
      <div class="chart-header">
        <span class="card-title" style="border:none;padding:0;">Equity Curve</span>
        <div class="chart-range-group">
          <button class="chart-range-btn" data-range="1h">1H</button>
          <button class="chart-range-btn" data-range="6h">6H</button>
          <button class="chart-range-btn active" data-range="7d">1D</button>
          <button class="chart-range-btn" data-range="7d">7D</button>
          <button class="chart-range-btn" data-range="30d">30D</button>
          <button class="chart-range-btn" data-range="all">ALL</button>
        </div>
      </div>
      <div class="chart-wrap">
        <canvas id="equityChart"></canvas>
      </div>
    </div>
  </div>

  <!-- ═══ Opportunity Log ════════════════════════════════════════════ -->
  <div class="opp-section">
    <div class="card">
      <div class="card-header opp-toggle" id="oppToggle">
        <span class="card-title" style="cursor:pointer;">
          Opportunity Log
          <span class="badge-count" id="oppCount">0</span>
          <span class="chevron" id="oppChevron">&#9660;</span>
        </span>
      </div>
      <div class="opp-body" id="oppBody">
        <div class="table-wrap" style="max-height:300px;">
          <table>
            <thead><tr>
              <th>Time</th><th>Market</th><th>Strategy</th><th>Edge %</th><th>YES</th><th>NO</th><th>Executed</th><th>Reason</th>
            </tr></thead>
            <tbody id="oppBodyTable">
              <tr><td colspan="8">
                <div class="empty-state">
                  <div class="empty-icon">&#9632;</div>
                  <div class="empty-text">No opportunities detected yet</div>
                  <div class="empty-sub">Market scans will appear here</div>
                </div>
              </td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

</div> <!-- /page -->

<!-- ═══════════════════════════════════════════════════════════════════════
     Control Bar
     ═══════════════════════════════════════════════════════════════════════ -->
<div class="control-bar">
  <button class="ctrl-btn ctrl-stop" onclick="showConfirm('stop')">&#9632; Stop</button>
  <button class="ctrl-btn ctrl-pause" onclick="showConfirm('pause')">&#9646;&#9646; Pause</button>
  <button class="ctrl-btn ctrl-resume" onclick="showConfirm('resume')">&#9654; Resume</button>
</div>

<!-- ═══════════════════════════════════════════════════════════════════════
     Confirmation Modal
     ═══════════════════════════════════════════════════════════════════════ -->
<div class="modal-overlay" id="confirmOverlay">
  <div class="modal-box">
    <h3 id="confirmTitle">Confirm Action</h3>
    <p id="confirmMsg">Are you sure?</p>
    <div class="modal-actions">
      <button class="modal-cancel" onclick="closeConfirm()">Cancel</button>
      <button class="modal-confirm" id="confirmBtn" onclick="executeConfirm()">Confirm</button>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════════════════
     JavaScript
     ═══════════════════════════════════════════════════════════════════════ -->
<script>
// ═══════════════════════════════════════════════════════════════════════════
// State & Constants
// ═══════════════════════════════════════════════════════════════════════════

const CHART_COLOR_GREEN  = '#00e676';
const CHART_COLOR_RED    = '#ff5252';
const CHART_COLOR_GRID   = '#1e1e2e';
const CHART_COLOR_FILL   = 'rgba(0, 230, 118, 0.08)';

let equityChart = null;
let currentEquityRange = '7d';

let pendingAction = null;
let state = {
  status: null,
  trades: [],
  opportunities: [],
  strategyStats: { price_lag: {}, complete_set: {} },
  yesterdayWins: null,
  yesterdayLosses: null,
};

// ═══════════════════════════════════════════════════════════════════════════
// Format Helpers
// ═══════════════════════════════════════════════════════════════════════════

function fmtUsdc(val) {
  if (val === null || val === undefined) return '$0.00';
  const sign = val >= 0 ? '' : '-';
  return sign + '$' + Math.abs(val).toFixed(2);
}

function fmtDelta(val) {
  if (val === null || val === undefined) return '+$0.00';
  const sign = val >= 0 ? '+' : '';
  return sign + '$' + val.toFixed(2);
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString('en-US', {hour12: false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
}

function fmtPct(val, decimals) {
  decimals = decimals !== undefined ? decimals : 2;
  if (val === null || val === undefined) return '0.00%';
  const sign = val >= 0 ? '+' : '';
  return sign + val.toFixed(decimals) + '%';
}

function uptimeStr(seconds) {
  if (!seconds || seconds < 0) return '0h 0m';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return h + 'h ' + m + 'm';
  if (m > 0) return m + 'm ' + s + 's';
  return s + 's';
}

function timeAgo(iso) {
  if (!iso) return '—';
  const now = new Date();
  const d = new Date(iso);
  const diff = Math.floor((now - d) / 1000);
  if (diff < 5) return 'just now';
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

// ═══════════════════════════════════════════════════════════════════════════
// DOM Updates
// ═══════════════════════════════════════════════════════════════════════════

function updateNav(status) {
  if (!status) return;

  // Bankroll
  document.getElementById('navBankrollVal').textContent = fmtUsdc(status.bankroll_usdc).replace('$', '');

  // P&L
  const pnlEl = document.getElementById('navPnl');
  const pnl = status.daily_pnl_usdc || 0;
  pnlEl.textContent = (pnl >= 0 ? '▲' : '▼') + ' ' + fmtDelta(pnl) + ' today';
  pnlEl.className = 'nav-pnl ' + (pnl >= 0 ? 'positive' : 'negative');

  // Status dot
  const dot = document.getElementById('navDot');
  const txt = document.getElementById('navStatusText');
  let dotClass = 'dot stopped';
  let label = 'Stopped';
  if (status.is_running) {
    if (status.is_paused) {
      dotClass = 'dot paused';
      label = 'Paused';
    } else {
      dotClass = 'dot running';
      label = 'Running';
    }
  }
  dot.className = dotClass;
  txt.textContent = label;

  // Mode badge
  const modeEl = document.getElementById('navMode');
  const isLive = status.mode === 'live';
  modeEl.textContent = isLive ? 'LIVE' : 'DRY RUN';
  modeEl.className = 'nav-badge ' + (isLive ? 'live' : 'dry-run');

  // Uptime
  document.getElementById('navUptime').textContent = uptimeStr(status.uptime_seconds || 0);
}

function updateMetrics(status) {
  if (!status) return;

  const posCount = status.open_positions ? status.open_positions.length : 0;
  const maxPos = 3;
  const pct = Math.min(100, (posCount / maxPos) * 100);

  // Active Positions
  document.getElementById('metricPositions').textContent = posCount + ' / ' + maxPos;
  const posBar = document.getElementById('metricPosBar');
  posBar.style.width = pct + '%';
  posBar.className = 'bar ' + (pct >= 100 ? 'red' : pct >= 66 ? 'amber' : 'green');

  // Win Rate
  const wr = status.win_rate || 0;
  const wrEl = document.getElementById('metricWinRate');
  wrEl.textContent = wr.toFixed(1) + '%';
  wrEl.className = 'metric-value ' + (wr >= 50 ? 'green' : 'red');
  document.getElementById('metricWinSub').textContent = (status.total_trades || 0) + ' total trades';

  // Opportunities
  const oppsDetected = status.opportunities_detected || 0;
  const oppsExecuted = status.opportunities_executed || 0;
  document.getElementById('metricOpps').textContent = oppsDetected;
  document.getElementById('metricOppsSub').textContent =
    oppsDetected + ' detected \u00b7 ' + oppsExecuted + ' executed';

  // Drawdown
  const dd = status.daily_drawdown_pct || 0;
  const ddEl = document.getElementById('metricDrawdown');
  ddEl.textContent = dd.toFixed(2) + '%';
  ddEl.className = 'metric-value ' + (dd < 5 ? 'green' : dd < 10 ? 'amber' : 'red');
  const ddBar = document.getElementById('metricDdBar');
  ddBar.style.width = Math.min(100, dd * 5) + '%';
  ddBar.className = 'bar ' + (dd < 5 ? 'green' : dd < 10 ? 'amber' : 'red');
}

function updatePositions(status) {
  const body = document.getElementById('positionsBody');
  const countEl = document.getElementById('posCount');

  if (!status || !status.open_positions || status.open_positions.length === 0) {
    countEl.textContent = '0';
    body.innerHTML = '<tr><td colspan="7"><div class="empty-state">' +
      '<div class="empty-icon">&#9654;</div>' +
      '<div class="empty-text">No open positions</div>' +
      '<div class="empty-sub">Waiting for trade signals...</div></div></td></tr>';
    return;
  }

  const positions = status.open_positions;
  countEl.textContent = positions.length;

  body.innerHTML = positions.map(p => {
    const isGreen = p.pnl_pct >= 0;
    const color = isGreen ? 'green' : 'red';
    const sign = p.pnl_pct >= 0 ? '+' : '';
    return '<tr>' +
      '<td class="mono">' + escHtml(p.market) + '</td>' +
      '<td>' + (p.side === 'YES' ? '<span class="green">YES</span>' : '<span class="red">NO</span>') + '</td>' +
      '<td style="color:var(--text-secondary);font-size:11px;">' + escHtml(p.strategy || '—') + '</td>' +
      '<td class="mono">' + fmtUsdc(p.entry) + '</td>' +
      '<td class="mono">' + fmtUsdc(p.current) + '</td>' +
      '<td class="mono ' + color + '">' + sign + p.pnl_pct.toFixed(2) + '%</td>' +
      '<td style="color:var(--text-muted);font-size:11px;font-family:var(--font-mono);">' + (p.time_held || '—') + '</td>' +
      '</tr>';
  }).join('');
}

function updateTrades(trades) {
  const body = document.getElementById('tradesBody');
  const countEl = document.getElementById('tradeCount');

  if (!trades || trades.length === 0) {
    countEl.textContent = '0';
    body.innerHTML = '<tr><td colspan="7"><div class="empty-state">' +
      '<div class="empty-icon">&#9632;</div>' +
      '<div class="empty-text">No trades yet</div>' +
      '<div class="empty-sub">Trade history will appear here</div></div></td></tr>';
    return;
  }

  countEl.textContent = trades.length;

  body.innerHTML = trades.map(t => {
    const isWin = t.win === true;
    const isLoss = t.win === false;

    let badge = '';
    if (isWin) badge = '<span class="badge badge-win">WIN</span>';
    else if (isLoss) badge = '<span class="badge badge-loss">LOSS</span>';
    else badge = '<span class="badge badge-open">OPEN</span>';

    const profitColor = t.profit_usdc !== null && t.profit_usdc !== undefined
      ? (t.profit_usdc >= 0 ? 'green' : 'red')
      : 'muted';
    const profitStr = t.profit_usdc !== null && t.profit_usdc !== undefined
      ? fmtUsdc(t.profit_usdc)
      : '—';

    const entryStr = t.entry_price !== null && t.entry_price !== undefined
      ? t.entry_price.toFixed(4)
      : '—';
    const exitStr = t.exit_price !== null && t.exit_price !== undefined
      ? t.exit_price.toFixed(4)
      : '—';

    return '<tr>' +
      '<td style="color:var(--text-muted);font-family:var(--font-mono);font-size:11px;">' + fmtTime(t.timestamp) + '</td>' +
      '<td class="mono">' + escHtml(t.market || '—') + '</td>' +
      '<td>' + (t.side === 'YES' ? '<span class="green">YES</span>' : t.side === 'NO' ? '<span class="red">NO</span>' : escHtml(t.side || '—')) + '</td>' +
      '<td style="color:var(--text-secondary);font-size:11px;">' + escHtml(t.strategy || '—') + '</td>' +
      '<td class="mono" style="font-size:11px;">' + entryStr + ' &rarr; ' + exitStr + '</td>' +
      '<td class="mono ' + profitColor + '">' + profitStr + '</td>' +
      '<td>' + badge + '</td>' +
      '</tr>';
  }).join('');
}

function updateStrategyCards(strategyStats) {
  const container = document.getElementById('strategyCards');
  if (!strategyStats) return;

  const strategies = [
    { key: 'price_lag', label: 'Price-Lag', desc: 'Time arbitrage vs Binance' },
    { key: 'complete_set', label: 'Complete-Set', desc: 'Risk-free YES+NO arb' },
  ];

  let html = '';
  for (const s of strategies) {
    const data = strategyStats[s.key] || {};
    const trades = data.total_trades || 0;
    const wins = data.wins || 0;
    const wr = data.win_rate || 0;
    const profit = data.total_profit || 0;
    const profitColor = profit >= 0 ? 'green' : 'red';

    html += '<div class="strategy-card">' +
      '<div class="strat-name">' + s.label + ' <span style="font-weight:400;font-size:11px;color:var(--text-muted);">' + s.desc + '</span></div>' +
      '<div class="strat-stats">' +
        '<div class="strat-stat"><div class="ss-value">' + trades + '</div><div class="ss-label">Trades</div></div>' +
        '<div class="strat-stat"><div class="ss-value" style="color:' + (wr >= 50 ? 'var(--green)' : 'var(--red)') + '">' + wr.toFixed(1) + '%</div><div class="ss-label">Win Rate</div></div>' +
        '<div class="strat-stat"><div class="ss-value ' + profitColor + '">' + fmtUsdc(profit) + '</div><div class="ss-label">P&amp;L</div></div>' +
      '</div></div>';
  }

  container.innerHTML = html;
}

function updateRiskMetrics(status) {
  if (!status) return;
  const posCount = status.open_positions ? status.open_positions.length : 0;
  const totalTrades = status.total_trades || 0;
  const streak = status.consecutive_wins || 0;

  document.getElementById('riskMaxPos').textContent = fmtUsdc(status.max_position_size || 0);
  document.getElementById('riskUtilization').textContent = (posCount / 3 * 100).toFixed(1) + '%';
  document.getElementById('riskStreak').textContent = streak;
  document.getElementById('riskStreak').className = 'risk-value ' + (streak >= 3 ? 'green' : streak > 0 ? '' : '');
  document.getElementById('riskTotalTrades').textContent = totalTrades;
}

function updateMarkets(status) {
  const list = document.getElementById('marketList');
  const countEl = document.getElementById('marketCount');

  // Use real markets from the bot, not hardcoded lists
  const markets = (status && status.markets_tracked && status.markets_tracked.length > 0)
    ? status.markets_tracked
    : [];

  countEl.textContent = markets.length;

  if (markets.length === 0) {
    list.innerHTML = '<div class="market-item" style="color:var(--text-muted);font-style:italic;">No markets — Polymarket API may be blocked</div>';
    return;
  }

  list.innerHTML = markets.map(m =>
    '<div class="market-item" title="' + escHtml(m) + '">' + escHtml(m) + '</div>'
  ).join('');
}

function updateOppLog(opportunities) {
  const body = document.getElementById('oppBodyTable');
  const countEl = document.getElementById('oppCount');

  if (!opportunities || opportunities.length === 0) {
    countEl.textContent = '0';
    body.innerHTML = '<tr><td colspan="8"><div class="empty-state">' +
      '<div class="empty-icon">&#9632;</div>' +
      '<div class="empty-text">No opportunities detected yet</div>' +
      '<div class="empty-sub">Market scans will appear here</div></div></td></tr>';
    return;
  }

  countEl.textContent = opportunities.length;

  body.innerHTML = opportunities.map(o => {
    const badge = o.executed
      ? '<span class="badge badge-executed">YES</span>'
      : '<span class="badge badge-missed">MISSED</span>';
    return '<tr>' +
      '<td style="color:var(--text-muted);font-family:var(--font-mono);font-size:11px;">' + fmtTime(o.timestamp) + '</td>' +
      '<td class="mono">' + escHtml(o.market || '—') + '</td>' +
      '<td style="color:var(--text-secondary);font-size:11px;">' + escHtml(o.strategy || '—') + '</td>' +
      '<td class="mono" style="color:var(--amber)">' + (o.edge_pct !== null && o.edge_pct !== undefined ? o.edge_pct.toFixed(2) + '%' : '—') + '</td>' +
      '<td class="mono">' + (o.yes_price !== null && o.yes_price !== undefined ? o.yes_price.toFixed(4) : '—') + '</td>' +
      '<td class="mono">' + (o.no_price !== null && o.no_price !== undefined ? o.no_price.toFixed(4) : '—') + '</td>' +
      '<td>' + badge + '</td>' +
      '<td style="color:var(--text-muted);font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;">' + escHtml(o.reason || '—') + '</td>' +
      '</tr>';
  }).join('');
}

function escHtml(str) {
  if (!str) return '—';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ═══════════════════════════════════════════════════════════════════════════
// Fetch Functions
// ═══════════════════════════════════════════════════════════════════════════

async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } catch (e) {
    console.error('Status fetch failed:', e);
    return null;
  }
}

async function fetchTrades() {
  try {
    const res = await fetch('/api/trades?limit=20');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } catch (e) {
    console.error('Trades fetch failed:', e);
    return [];
  }
}

async function fetchEquity(range) {
  try {
    const res = await fetch('/api/equity?range=' + (range || currentEquityRange));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } catch (e) {
    console.error('Equity fetch failed:', e);
    return null;
  }
}

async function fetchOpportunities() {
  try {
    const res = await fetch('/api/opportunities?limit=30');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } catch (e) {
    console.error('Opportunities fetch failed:', e);
    return [];
  }
}

async function fetchStrategyStats() {
  try {
    const res = await fetch('/api/strategy-stats');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } catch (e) {
    console.error('Strategy stats fetch failed:', e);
    return null;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Equity Chart
// ═══════════════════════════════════════════════════════════════════════════

function initChart(data) {
  const ctx = document.getElementById('equityChart').getContext('2d');
  equityChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data ? data.timestamps.map(fmtDate) : [],
      datasets: [{
        label: 'Bankroll (USDC)',
        data: data ? data.bankroll : [],
        borderColor: CHART_COLOR_GREEN,
        backgroundColor: CHART_COLOR_FILL,
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#12121a',
          titleColor: '#e0e0e0',
          bodyColor: '#00e676',
          borderColor: '#1e1e2e',
          borderWidth: 1,
          cornerRadius: 6,
          padding: 12,
          displayColors: false,
          callbacks: {
            title: function(items) {
              return items[0].label;
            },
            label: function(ctx) {
              return '$' + parseFloat(ctx.parsed.y).toFixed(2);
            }
          }
        }
      },
      scales: {
        x: {
          display: true,
          grid: { color: CHART_COLOR_GRID, drawBorder: false },
          ticks: {
            color: '#616161',
            maxTicksLimit: 8,
            font: { size: 10, family: "'JetBrains Mono', monospace" }
          }
        },
        y: {
          display: true,
          grid: { color: CHART_COLOR_GRID, drawBorder: false },
          ticks: {
            color: '#616161',
            font: { size: 10, family: "'JetBrains Mono', monospace" },
            callback: function(v) { return '$' + v.toFixed(0); }
          }
        }
      },
      interaction: {
        intersect: false,
        mode: 'index'
      },
      animation: {
        duration: 400
      }
    }
  });
}

function updateChart(data) {
  if (!data) return;
  if (!equityChart) {
    initChart(data);
    return;
  }
  equityChart.data.labels = data.timestamps.map(fmtDate);
  equityChart.data.datasets[0].data = data.bankroll;
  equityChart.update('none');
}

async function loadEquity(range) {
  if (range) currentEquityRange = range;

  // Update active button
  document.querySelectorAll('.chart-range-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.range === currentEquityRange);
  });

  const data = await fetchEquity(currentEquityRange);
  updateChart(data);
}

// ═══════════════════════════════════════════════════════════════════════════
// Bot Control
// ═══════════════════════════════════════════════════════════════════════════

const CONFIRM_CONFIG = {
  stop: {
    title: 'Stop Bot',
    msg: 'Are you sure you want to STOP the bot? All open positions will be closed and trading will halt.',
    btnClass: ''
  },
  pause: {
    title: 'Pause Trading',
    msg: 'Pause the bot? New positions will not be opened, but existing positions will be monitored.',
    btnClass: 'amber'
  },
  resume: {
    title: 'Resume Trading',
    msg: 'Resume normal bot operation? The bot will continue scanning and trading.',
    btnClass: 'green'
  }
};

function showConfirm(action) {
  const cfg = CONFIRM_CONFIG[action];
  if (!cfg) return;
  pendingAction = action;
  document.getElementById('confirmTitle').textContent = cfg.title;
  document.getElementById('confirmMsg').textContent = cfg.msg;
  const btn = document.getElementById('confirmBtn');
  btn.textContent = cfg.title;
  btn.className = 'modal-confirm' + (cfg.btnClass ? ' ' + cfg.btnClass : '');
  document.getElementById('confirmOverlay').classList.add('show');
}

function closeConfirm() {
  pendingAction = null;
  document.getElementById('confirmOverlay').classList.remove('show');
}

async function executeConfirm() {
  const action = pendingAction;
  closeConfirm();
  if (!action) return;

  try {
    const res = await fetch('/api/' + action, { method: 'POST' });
    const data = await res.json();
    if (!data.success) {
      alert('Error: ' + (data.error || 'Unknown error'));
    }
  } catch (e) {
    alert('Network error: ' + e.message);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Main Update Loop
// ═══════════════════════════════════════════════════════════════════════════

function updateAll(status, trades, opps, stratStats) {
  updateNav(status);
  updateMetrics(status);
  updatePositions(status);
  updateTrades(trades);
  updateRiskMetrics(status);
  updateMarkets(status);
  if (stratStats) updateStrategyCards(stratStats);
  if (opps !== undefined) updateOppLog(opps);
}

// ═══════════════════════════════════════════════════════════════════════════
// Polling
// ═══════════════════════════════════════════════════════════════════════════

let lastStatus = null;
let lastTrades = null;
let lastOpps = null;
let lastStratStats = null;

async function pollStatus() {
  const data = await fetchStatus();
  if (data) {
    lastStatus = data;
  }
}

async function pollTrades() {
  const data = await fetchTrades();
  if (data) {
    lastTrades = data;
  }
}

async function pollOpportunities() {
  const data = await fetchOpportunities();
  if (data) {
    lastOpps = data;
  }
}

async function pollStrategyStats() {
  const data = await fetchStrategyStats();
  if (data) {
    lastStratStats = data;
  }
}

function renderAll() {
  updateAll(lastStatus, lastTrades, lastOpps, lastStratStats);
}

// ═══════════════════════════════════════════════════════════════════════════
// Chart Range Buttons
// ═══════════════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.chart-range-btn').forEach(btn => {
    btn.addEventListener('click', function() {
      loadEquity(this.dataset.range);
    });
  });

  // Opportunity log toggle
  const oppToggle = document.getElementById('oppToggle');
  const oppBody = document.getElementById('oppBody');
  const oppChevron = document.getElementById('oppChevron');
  if (oppToggle) {
    oppToggle.addEventListener('click', function(e) {
      e.stopPropagation();
      const isHidden = oppBody.classList.toggle('hidden');
      oppToggle.classList.toggle('collapsed', isHidden);
    });
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// Initialization
// ═══════════════════════════════════════════════════════════════════════════

async function init() {
  // Initial data load
  await Promise.all([
    pollStatus(),
    pollTrades(),
    pollOpportunities(),
    pollStrategyStats(),
  ]);

  renderAll();
  loadEquity('7d');

  // Fast polling (1s): status
  setInterval(async () => {
    await pollStatus();
    renderAll();
  }, 1000);

  // Trades (2s)
  setInterval(async () => {
    await pollTrades();
    renderAll();
  }, 2000);

  // Opportunities (5s)
  setInterval(async () => {
    await pollOpportunities();
    renderAll();
  }, 5000);

  // Strategy stats (10s)
  setInterval(async () => {
    await pollStrategyStats();
    renderAll();
  }, 10000);

  // Equity chart (30s)
  setInterval(() => loadEquity(currentEquityRange), 30000);
}

init();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def run_dashboard(host: str = "0.0.0.0", port: int = 8050, debug: bool = False) -> None:
    """
    Start the Flask dashboard server.

    This can be called from the main bot (in a daemon thread) or run standalone.

    Args:
        host:  Host to bind to (default: 0.0.0.0).
        port:  Port to listen on (default: 8050).
        debug: Enable Flask debug mode (default: False).
    """
    logger.info("Dashboard starting on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=debug, use_reloader=False)


def start_dashboard_thread() -> threading.Thread:
    """
    Start the dashboard in a background daemon thread.

    Returns:
        The background thread object.
    """
    thread = threading.Thread(
        target=run_dashboard,
        name="Dashboard",
        daemon=True,
        kwargs={"host": "0.0.0.0", "port": 8050, "debug": False},
    )
    thread.start()
    logger.info("Dashboard thread started on port 8050")
    return thread


if __name__ == "__main__":
    print("=" * 60)
    print("  PolyTrader28 — Trading Terminal")
    print("  Running standalone on http://localhost:8050")
    print("=" * 60)
    print()
    print("  NOTE: Run polymarket_bot.py to also start the trading engine.")
    print("  This dashboard reads from the same SQLite database.")
    print()
    run_dashboard(debug=True)
