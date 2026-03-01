from flask import Flask, request, jsonify
import requests
import os
import json
from datetime import datetime, timedelta
from eth_account import Account
from py_clob_client.client import ClobClient

# ==========================================================
# CONFIG
# ==========================================================
YES_TOKEN = "53822162563147299519165214885693344405498185564842997386824738830845754444209"
ORDER_SIZE = 1
MAX_LOSS = -3.0
MAX_POSITION = 3
DRY_RUN = True  # IMPORTANT: Start in True

# ==========================================================
# POLYMARKET INITIALIZATION
# ==========================================================
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY")
POLY_API_KEY = os.environ.get("POLY_API_KEY")
POLY_SECRET = os.environ.get("POLY_SECRET")
POLY_PASSPHRASE = os.environ.get("POLY_PASSPHRASE")

clob = None

if POLY_PRIVATE_KEY:
    try:
        Account.from_key(POLY_PRIVATE_KEY)

        clob = ClobClient(
            "https://clob.polymarket.com",
            key=POLY_PRIVATE_KEY,
            chain_id=137
        )

        clob.set_api_creds({
            "apiKey": POLY_API_KEY,
            "apiSecret": POLY_SECRET,
            "apiPassphrase": POLY_PASSPHRASE,
        })

        print("✅ Polymarket client initialized")

    except Exception as e:
        print("❌ Polymarket init failed:", e)
        clob = None
else:
    print("⚠️ No POLY_PRIVATE_KEY found. Running in DRY_RUN mode.")
    DRY_RUN = True

# ==========================================================
# STATE
# ==========================================================
trades = []
open_positions = 0
strategy_state = "IDLE"
cooldown_until = None
last_signal = {"action": None, "time": None}

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
# POLYMARKET ORDER FUNCTION
# ==========================================================
def place_order(side, price, size):
    if DRY_RUN or not clob:
        return {"status": "dry_run"}

    try:
        order = clob.create_order(
            token_id=YES_TOKEN,
            price=price,
            size=size,
            side=side
        )

        signed = clob.sign_order(order)
        response = clob.post_order(signed)

        return response

    except Exception as e:
        return {"error": str(e)}

# ==========================================================
# PRICE FETCH
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

    unrealized = 0.0
    avg_entry = 0.0

    open_buys = [t["price"] for t in trades if t["action"] == "BUY"]

    if open_positions > 0 and open_buys:
        avg_entry = sum(open_buys[-open_positions:]) / open_positions
        unrealized = (yes - avg_entry) * open_positions

    total_pnl = realized_pnl + unrealized
    pnl_color = "lime" if total_pnl >= 0 else "red"

    return f"""
    <h2>Gold Polymarket Bot</h2>
    <p><b>Mode:</b> {"DRY RUN" if DRY_RUN else "LIVE"}</p>
    <p><b>Strategy State:</b> {strategy_state}</p>
    <p><b>YES Price:</b> {yes}</p>
    <p><b>Open Positions:</b> {open_positions}</p>
    <p><b>Avg Entry:</b> {round(avg_entry,4) if avg_entry else 0}</p>
    <p><b>Total PnL:</b> <span style='color:{pnl_color}'>${round(total_pnl,4)}</span></p>
    <p><b>Wins:</b> {wins} | <b>Losses:</b> {losses} | <b>Win Rate:</b> {win_rate}%</p>
    <p><a href='/health'>Health Endpoint</a></p>
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

    if last_signal["action"] == action and last_signal["time"]:
        if (now - last_signal["time"]).seconds < 30:
            return jsonify({"status": "duplicate ignored"})

    last_signal = {"action": action, "time": now}

    yes = get_yes_price()
    if yes is None:
        return jsonify({"status": "no_price"})

    pnl = compute_pnl()

    if pnl <= MAX_LOSS:
        strategy_state = "SUSPENDED"
        return jsonify({"status": "suspended"})

    if action == "BUY" and strategy_state == "IDLE":
        response = place_order("BUY", yes, ORDER_SIZE)

        if "error" not in response:
            log_trade("BUY", yes)
            open_positions += ORDER_SIZE
            strategy_state = "LONG"

    elif action == "SELL" and strategy_state == "LONG":
        response = place_order("SELL", yes, ORDER_SIZE)

        if "error" not in response:
            log_trade("SELL", yes)
            open_positions -= ORDER_SIZE
            strategy_state = "IDLE"

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
        "mode": "DRY_RUN" if DRY_RUN else "LIVE",
        "positions": open_positions,
        "pnl": compute_pnl(),
        "state": strategy_state
    })
