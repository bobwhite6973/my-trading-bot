"""
control_api.py — Drop-in HTTP control & status server for your Render arb bot.

HOW TO USE:
1. Copy this file into your my-trading-bot repo root.
2. Add to your bot's main startup (see bottom of this file for example).
3. Set CONTROL_API_SECRET in your Render env vars.
4. Set BOT_CTRL_ARB and ARB_BOT_STATUS_URL in JARVIS .env to point here.

Endpoints:
  GET  /status   → JSON summary of bot state, uptime, last trade, PnL
  POST /control  → {"action": "pause"|"resume"|"stop"|"restart"}
                   Requires header: X-Control-Secret: <your secret>
"""

import json
import os
import time
import threading
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify

log = logging.getLogger("arb.control_api")

app = Flask(__name__)

# ── Shared bot state — your main bot loop writes to this dict ─────────────────
# Import this from control_api and update it from your trading loop.
BOT_STATE = {
    "status": "starting",        # "running" | "paused" | "stopped" | "error"
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_trade_at": None,
    "last_trade_pair": None,
    "last_trade_profit_sol": None,
    "total_pnl_sol": 0.0,
    "trade_count": 0,
    "last_error": None,
    "pairs_monitored": ["SOL/USDC", "JUP/USDC", "ETH/USDC"],
    "active_dexes": ["Raydium", "Orca", "Meteora"],
}

# Control flag — your trading loop should check this
_paused = threading.Event()
_paused.clear()   # not paused by default

def is_paused() -> bool:
    """Call this in your trading loop: if control_api.is_paused(): continue"""
    return _paused.is_set()

def update_state(**kwargs):
    """
    Call this from your trading loop to keep status current.
    Example:
        control_api.update_state(
            status="running",
            last_trade_at=datetime.now(timezone.utc).isoformat(),
            last_trade_pair="SOL/USDC",
            last_trade_profit_sol=0.000312,
            total_pnl_sol=BOT_STATE["total_pnl_sol"] + 0.000312,
            trade_count=BOT_STATE["trade_count"] + 1,
        )
    """
    BOT_STATE.update(kwargs)

# ── Auth helper ───────────────────────────────────────────────────────────────

def _authorized(req) -> bool:
    secret = os.environ.get("CONTROL_API_SECRET", "")
    if not secret:
        return False  # fail-closed: secret is required for prod
    return req.headers.get("X-Control-Secret") == secret

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/status", methods=["GET"])
def status():
    """Public status endpoint — no auth required."""
    uptime_secs = None
    try:
        started = datetime.fromisoformat(BOT_STATE["started_at"])
        uptime_secs = int((datetime.now(timezone.utc) - started).total_seconds())
    except Exception:
        pass

    return jsonify({
        **BOT_STATE,
        "uptime_seconds": uptime_secs,
        "paused": _paused.is_set(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/control", methods=["POST"])
def control():
    """Protected control endpoint — requires X-Control-Secret header."""
    if not _authorized(request):
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    action = body.get("action", "").lower().strip()

    if action in ("pause", "stop"):
        _paused.set()
        BOT_STATE["status"] = "paused"
        log.info(f"Bot PAUSED via control API (action={action})")
        return jsonify({"ok": True, "status": "paused"})

    elif action in ("resume", "start", "restart"):
        _paused.clear()
        BOT_STATE["status"] = "running"
        log.info(f"Bot RESUMED via control API (action={action})")
        return jsonify({"ok": True, "status": "running"})

    else:
        return jsonify({"error": f"Unknown action: '{action}'. Use pause|resume|stop|restart"}), 400


# ── License status ──────────────────────────────────────────────────────────────

LICENSE_URL = "https://raw.githubusercontent.com/bobwhite6973/my-trading-bot/release/keys.json"


def check_license():
    """
    Check the LICENSE_KEY env var against the hosted keys.json.
    Returns (valid: bool, info: dict).
    """
    license_key = os.environ.get("LICENSE_KEY", "").strip()
    if not license_key:
        return True, {"valid": True, "type": "demo", "expires": None, "days_remaining": None}

    try:
        import urllib.request
        req = urllib.request.Request(LICENSE_URL, headers={"User-Agent": "LeverBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            keys_data = json.loads(resp.read().decode())
    except Exception:
        return True, {"valid": True, "type": "cached", "expires": None, "days_remaining": None, "note": "fetch failed, assuming valid"}

    for entry in keys_data:
        if entry.get("key") == license_key:
            expires_str = entry.get("expires")
            days_left = None
            if expires_str:
                try:
                    expires_dt = datetime.fromisoformat(expires_str)
                    now = datetime.now(timezone.utc)
                    if now > expires_dt:
                        return False, {"valid": False, "type": entry.get("type", "trial"), "expires": expires_str, "days_remaining": 0, "error": "License expired"}
                    days_left = (expires_dt - now).days
                except Exception:
                    pass
            return True, {"valid": True, "type": entry.get("type", "full"), "expires": expires_str, "days_remaining": days_left}

    return False, {"valid": False, "type": "invalid", "expires": None, "days_remaining": None, "error": "License key not found"}


@app.route("/license_status", methods=["GET"])
def license_status():
    """Return current license status."""
    valid, info = check_license()
    return jsonify(info)


@app.route("/health", methods=["GET"])
def health():
    """Render health check endpoint."""
    return jsonify({"ok": True}), 200

# ── Startup helper ────────────────────────────────────────────────────────────

def start_in_background(host="0.0.0.0", port=None):
    """
    Start the control API in a background thread alongside your bot's main loop.

    Add this to your bot's main() or startup block:

        import control_api
        control_api.start_in_background()

        # Then in your trading loop:
        while True:
            if control_api.is_paused():
                time.sleep(1)
                continue
            # ... your arb logic ...
            control_api.update_state(status="running", trade_count=...)
    """
    port = port or int(os.environ.get("PORT", 8080))
    t = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True,
        name="control-api"
    )
    t.start()
    log.info(f"Control API listening on :{port}")
    return t
