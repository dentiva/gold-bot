from flask import Flask, request, jsonify
import requests
import os
import json
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta

# ==========================================================
# CONFIG
# ==========================================================
YES_TOKEN    = "53822162563147299519165214885693344405498185564842997386824738830845754444209"
ORDER_SIZE   = 1
MAX_LOSS     = -3.0
MAX_POSITION = 3
DRY_RUN      = True

# ==========================================================
# STATE
# ==========================================================
trades = []
open_positions = 0
strategy_state = "IDLE"
cooldown_until = None
last_signal = {"action": None, "time": None}
yes_history = []

# Load persisted trades
if os.path.exists("trades.json"):
    with open("trades.json", "r") as f:
        trades = json.load(f)
        for t in trades:
            if t["action"] == "BUY":
                open_positions += ORDER_SIZE
            elif t["action"] == "SELL":
                open_positions -= ORDER_SIZE

    if open_positions > 0:
        strategy_state = "LONG"

app = Flask(__name__)

# ==========================================================
# MARKET DATA
# ==========================================================
def get_yes_price():
    try:
        r = requests.get(
            f"https://clob.polymarket.com/last-trade-price?token_id={YES_TOKEN}",
            timeout=3
        )
        price = float(r.json()["price"])
        yes_history.append(price)
        if len(yes_history) > 100:
            yes_history.pop(0)
        return price
    except:
        return None

# ==========================================================
# PNL + STATS
# ==========================================================
def compute_pnl():
    pnl = 0.0
    buy_stack = []

    for t in trades:
        if t["action"] == "BUY":
            buy_stack.append(t["price"])
        elif t["action"] == "SELL" and buy_stack:
            pnl += (t["price"] - buy_stack.pop(0)) * t["size"]

    return round(pnl, 4)


def compute_win_stats():
    wins = 0
    losses = 0
    buy_stack = []

    for t in trades:
        if t["action"] == "BUY":
            buy_stack.append(t["price"])
        elif t["action"] == "SELL" and buy_stack:
            entry = buy_stack.pop(0)
            if t["price"] > entry:
                wins += 1
            else:
                losses += 1

    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0

    return wins, losses, round(win_rate, 2)


def log_trade(action, price):
    trades.append({
        "action": action,
        "price": price,
        "size": ORDER_SIZE,
        "time": datetime.now().strftime("%H:%M:%S")
    })

    with open("trades.json", "w") as f:
        json.dump(trades, f)

# ==========================================================
# DASHBOARD
# ==========================================================
@app.route("/")
def home():
    yes = get_yes_price()
    realized_pnl = compute_pnl()
    wins, losses, win_rate = compute_win_stats()

    # Unrealized
    unrealized = 0.0
    avg_entry = 0.0

    open_buys = [t["price"] for t in trades if t["action"] == "BUY"]

    if open_positions > 0 and open_buys:
        avg_entry = sum(open_buys[-open_positions:]) / open_positions
        unrealized = (yes - avg_entry) * open_positions

    total_pnl = realized_pnl + unrealized
    pnl_color = "lime" if total_pnl >= 0 else "red"

    last_trades = trades[-5:][::-1]

    trades_html = ""
    for t in last_trades:
        color = "lime" if t["action"] == "BUY" else "red"
        trades_html += f"""
        <tr>
            <td style='color:{color}'>{t['action']}</td>
            <td>{t['price']}</td>
            <td>{t['time']}</td>
        </tr>
        """

    return f"""
    <html>
    <head>
        <meta http-equiv="refresh" content="10">
        <style>
            body {{
                background-color: #0f172a;
                color: #e2e8f0;
                font-family: Arial;
                padding: 40px;
            }}
            .card {{
                background: #1e293b;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 20px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
            }}
            td {{
                padding: 8px;
                border-bottom: 1px solid #334155;
            }}
            h1 {{
                color: #38bdf8;
            }}
        </style>
    </head>
    <body>

        <h1>Gold Polymarket Rotation Bot</h1>

        <div class="card">
            <h2>Status</h2>
            <p><b>Mode:</b> {"DRY RUN" if DRY_RUN else "LIVE"}</p>
            <p><b>Strategy State:</b> {strategy_state}</p>
            <p><b>YES Price:</b> {yes}</p>
            <p><b>Open Positions:</b> {open_positions}</p>
            <p><b>Average Entry:</b> {round(avg_entry,4) if avg_entry else 0}</p>
            <p><b>Unrealized PnL:</b> {round(unrealized,4)}</p>
            <p><b>Total PnL:</b> <span style='color:{pnl_color}'>${round(total_pnl,4)}</span></p>
        </div>

        <div class="card">
            <h2>Performance</h2>
            <p><b>Wins:</b> {wins}</p>
            <p><b>Losses:</b> {losses}</p>
            <p><b>Win Rate:</b> {win_rate}%</p>
            <p><b>Total Trades:</b> {len(trades)}</p>
        </div>

        <div class="card">
            <h2>Last 5 Trades</h2>
            <table>
                {trades_html}
            </table>
        </div>

        <div class="card">
            <p><a href='/health'>Health Endpoint</a></p>
        </div>

    </body>
    </html>
    """

# ==========================================================
# WEBHOOK
# ==========================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    global open_positions, strategy_state, cooldown_until, last_signal

    data = request.json
    action = data.get("action", "").upper()
    now = datetime.now()

    # Cooldown check
    if strategy_state == "COOLDOWN":
        if cooldown_until and now < cooldown_until:
            return jsonify({"status": "cooldown_active"})
        else:
            strategy_state = "IDLE"

    # Duplicate protection
    if last_signal["action"] == action and last_signal["time"]:
        if (now - last_signal["time"]).seconds < 30:
            return jsonify({"status": "duplicate ignored"})

    last_signal = {"action": action, "time": now}

    yes = get_yes_price()
    if yes is None:
        return jsonify({"status": "no_price"})

    pnl = compute_pnl()

    # Global suspension
    if pnl <= MAX_LOSS:
        strategy_state = "SUSPENDED"
        return jsonify({"status": "suspended"})

    if action == "BUY" and strategy_state == "IDLE":
        if open_positions < MAX_POSITION:
            log_trade("BUY", yes)
            open_positions += ORDER_SIZE
            strategy_state = "LONG"

    elif action == "SELL" and strategy_state == "LONG":
        if open_positions > 0:
            log_trade("SELL", yes)
            open_positions -= ORDER_SIZE
            strategy_state = "IDLE"

            if pnl < 0:
                strategy_state = "COOLDOWN"
                cooldown_until = now + timedelta(minutes=5)

    return jsonify({
        "status": "ok",
        "state": strategy_state,
        "positions": open_positions,
        "pnl": compute_pnl()
    })

# ==========================================================
# HEALTH
# ==========================================================
@app.route("/health")
def health():
    return jsonify({
        "status": "alive",
        "positions": open_positions,
        "pnl": compute_pnl(),
        "state": strategy_state,
        "trades": len(trades)
    })
