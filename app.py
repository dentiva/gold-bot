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

# Load persisted trades
if os.path.exists("trades.json"):
    with open("trades.json", "r") as f:
        trades = json.load(f)
        for t in trades:
            if t["action"] == "BUY":
                open_positions += ORDER_SIZE
            elif t["action"] == "SELL":
                open_positions -= ORDER_SIZE

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
        return float(r.json()["price"])
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

        thresholds = {
            "gold_current": round(current, 1),
            "buy_trigger": round(current - atr, 0),
            "sell_trigger": round(current + 0.5 * atr, 0),
            "yes_buy_at": round(yes_now - 0.04, 3),
            "yes_sell_at": round(yes_now + 0.02, 3),
            "updated_at": datetime.now().strftime("%H:%M:%S")
        }

    except Exception as e:
        print("Threshold error:", e)

compute_thresholds()

# ==========================================================
# PNL
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
    <h2>Gold Polymarket Bot</h2>
    <p>Mode: {"DRY RUN" if DRY_RUN else "LIVE"}</p>
    <p>YES Price: {yes}</p>
    <p>Open Positions: {open_positions}</p>
    <p>Trades: {len(trades)}</p>
    <p>PnL: ${pnl}</p>
    <p><a href='/health'>Health Endpoint</a></p>
    """

# ==========================================================
# WEBHOOK
# ==========================================================
@app.route("/webhook", methods=["POST"])
def webhook():
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
        return jsonify({"status": "no price"})

    pnl = compute_pnl()

    if pnl <= MAX_LOSS:
        bot_suspended = True
        return jsonify({"status": "suspended"})

    if action == "BUY":
        if open_positions < MAX_POSITION:
            log_trade("BUY", yes)
            open_positions += ORDER_SIZE

    elif action == "SELL":
        if open_positions > 0:
            log_trade("SELL", yes)
            open_positions -= ORDER_SIZE

    return jsonify({
        "status": "ok",
        "action": action,
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
        "thresholds": thresholds
    })
