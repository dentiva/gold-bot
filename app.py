from flask import Flask, request, jsonify
import requests
import os
import json
import numpy as np
import yfinance as yf
from datetime import datetime

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
bot_suspended = False
thresholds = {}
yes_history = []
last_signal = {"action": None, "time": None}

# Load persisted trades if exists
if os.path.exists("trades.json"):
    with open("trades.json", "r") as f:
        trades = json.load(f)
        for t in trades:
            if t["action"] == "BUY":
                open_positions += ORDER_SIZE
            elif t["action"] == "SELL":
                open_positions -= ORDER_SIZE

# ==========================================================
# APP INIT
# ==========================================================
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


def compute_thresholds():
    global thresholds
    try:
        df = yf.download("GC=F", period="30d", interval="1d", progress=False)

        closes = df["Close"].values.flatten()
        highs  = df["High"].values.flatten()
        lows   = df["Low"].values.flatten()

        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                abs(highs[1:] - closes[:-1]),
                abs(lows[1:] - closes[:-1])
            )
        )

        atr = float(np.mean(tr[-14:]))
        current = float(closes[-1])

        yes_now = get_yes_price()
        if yes_now is None:
            return

        yes_std = float(np.std(yes_history)) if len(yes_history) > 10 else 0.04

        thresholds = {
            "gold_current": round(current, 1),
            "buy_trigger": round(current - atr, 0),
            "sell_trigger": round(current + 0.5 * atr, 0),
            "yes_buy_at": round(yes_now - yes_std, 3),
            "yes_sell_at": round(yes_now + 0.5 * yes_std, 3),
            "updated_at": datetime.now().strftime("%H:%M:%S")
        }

    except Exception as e:
        print("Threshold computation failed:", e)


compute_thresholds()

# ==========================================================
# PNL
# ==========================================================
def compute_pnl():
    pnl = 0.0
    buy_queue = []

    for t in trades:
        if t["action"] == "BUY":
            buy_queue.append(t["price"])
        elif t["action"] == "SELL" and buy_queue:
            pnl += (t["price"] - buy_queue.pop(0)) * t["size"]

    return round(pnl, 4)


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
# LANDING PAGE
# ==========================================================
@app.route("/")
def home():
    yes = get_yes_price()
    pnl = compute_pnl()

    return f"""
    <html>
        <head>
            <title>Gold Rotation Bot</title>
            <meta http-equiv="refresh" content="15">
            <style>
                body {{
                    font-family: Arial;
                    background-color: #0f172a;
                    color: #e2e8f0;
                    padding: 40px;
                }}
                .card {{
                    background: #1e293b;
                    padding: 20px;
                    border-radius: 10px;
                    margin-bottom: 20px;
                }}
                h1 {{ color: #38bdf8; }}
            </style>
        </head>
        <body>
            <h1>Gold Polymarket Rotation Bot</h1>

            <div class="card">
                <h2>Status</h2>
                <p><b>Mode:</b> {"DRY RUN" if DRY_RUN else "LIVE TRADING"}</p>
                <p><b>Bot Suspended:</b> {bot_suspended}</p>
                <p><b>YES Price:</b> {yes}</p>
                <p><b>Open Positions:</b> {open_positions}</p>
                <p><b>Total Trades:</b> {len(trades)}</p>
                <p><b>PnL:</b> ${pnl}</p>
            </div>

            <div class="card">
                <h2>Thresholds</h2>
                <pre>{json.dumps(thresholds, indent=2)}</pre>
            </div>

            <div class="card">
                <h2>Endpoints</h2>
                <p>POST /webhook</p>
                <p>GET /health</p>
            </div>

        </body>
    </html>
    """

# ==========================================================
# WEBHOOK
# ==========================================================
@app.route("/webhook", methods=["POST"])
def tradingview_webhook():
    global open_positions, bot_suspended, last_signal

    data = request.json
    action = data.get("action", "").upper()
    now = datetime.now()

    # Duplicate protection
    if last_signal["action"] == action and last_signal["time"]:
        if (now - last_signal["time"]).seconds < 30:
            return jsonify({"status": "duplicate ignored"})

    last_signal = {"action": action, "time": now}

    yes = get_yes_price()
    if yes is None:
        return jsonify({"status": "yes_price_unavailable"})

    pnl = compute_pnl()

    if pnl <= MAX_LOSS:
        bot_suspended = True
        return jsonify({"status": "suspended", "pnl": pnl})

    if action == "BUY":
        if open_positions >= MAX_POSITION:
            return jsonify({"status": "max_exposure"})
        if yes < thresholds.get("yes_buy_at", 0):
            if not DRY_RUN:
                log_trade("BUY", yes)
            open_positions += ORDER_SIZE

    elif action == "SELL":
        if open_positions > 0 and yes > thresholds.get("yes_sell_at", 1):
            if not DRY_RUN:
                log_trade("SELL", yes)
            open_positions -= ORDER_SIZE

    return jsonify({
        "status": "ok",
        "action": action,
        "yes": yes,
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
        "dry_run": DRY_RUN,
        "positions": open_positions,
        "pnl": compute_pnl(),
        "trades": len(trades),
        "thresholds": thresholds
    })
