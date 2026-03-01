from flask import Flask, request, jsonify
import requests
import os
import json
from datetime import datetime
from eth_account import Account
from py_clob_client.client import ClobClient

# ==========================================================
# CONFIG
# ==========================================================
YES_TOKEN = "53822162563147299519165214885693344405498185564842997386824738830845754444209"
ORDER_SIZE = 0.01      # SAFE TEST SIZE
MAX_LOSS = -3.0
DRY_RUN = False        # Set True to simulate

# ==========================================================
# POLYMARKET INIT
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
        DRY_RUN = True
else:
    print("⚠️ No private key found. Running DRY_RUN.")
    DRY_RUN = True

# ==========================================================
# STATE
# ==========================================================
trades = []
position_size = 0.0
strategy_state = "IDLE"
last_signal = {"action": None, "time": None}

if os.path.exists("trades.json"):
    with open("trades.json", "r") as f:
        trades = json.load(f)

        for t in trades:
            if t["action"] == "BUY":
                position_size += t["size"]
            elif t["action"] == "SELL":
                position_size -= t["size"]

    if position_size > 0:
        strategy_state = "LONG"

app = Flask(__name__)

# ==========================================================
# ORDER FUNCTION
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
# PRICE
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
            buy_stack.append(t)
        elif t["action"] == "SELL" and buy_stack:
            entry = buy_stack.pop(0)
            pnl += (t["price"] - entry["price"]) * entry["size"]

    return round(pnl, 4)


def compute_win_stats():
    wins = 0
    losses = 0
    buy_stack = []

    for t in trades:
        if t["action"] == "BUY":
            buy_stack.append(t)
        elif t["action"] == "SELL" and buy_stack:
            entry = buy_stack.pop(0)
            if t["price"] > entry["price"]:
                wins += 1
            else:
                losses += 1

    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0

    return wins, losses, round(win_rate, 2)

# ==========================================================
# LOG TRADE
# ==========================================================
def log_trade(action, price, size):
    trades.append({
        "action": action,
        "price": price,
        "size": size,
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
    realized = compute_pnl()
    wins, losses, win_rate = compute_win_stats()

    avg_entry = 0.0
    unrealized = 0.0

    open_buys = [t for t in trades if t["action"] == "BUY"]

    if position_size > 0 and open_buys:
        total_cost = sum(t["price"] * t["size"] for t in open_buys)
        total_size = sum(t["size"] for t in open_buys)

        if total_size > 0:
            avg_entry = total_cost / total_size
            unrealized = (yes - avg_entry) * total_size

    total_pnl = realized + unrealized
    pnl_color = "lime" if total_pnl >= 0 else "red"

    return f"""
    <h2>Gold Polymarket Bot</h2>
    <p><b>Mode:</b> {"DRY RUN" if DRY_RUN else "LIVE"}</p>
    <p><b>Strategy State:</b> {strategy_state}</p>
    <p><b>YES Price:</b> {yes}</p>
    <p><b>Position Size:</b> {round(position_size,4)}</p>
    <p><b>Avg Entry:</b> {round(avg_entry,4)}</p>
    <p><b>Total PnL:</b> <span style='color:{pnl_color}'>${round(total_pnl,4)}</span></p>
    <p><b>Wins:</b> {wins} | <b>Losses:</b> {losses} | <b>Win Rate:</b> {win_rate}%</p>
    <p><a href='/health'>Health Endpoint</a></p>
    """

# ==========================================================
# WEBHOOK
# ==========================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    global position_size, strategy_state, last_signal

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

    if action == "BUY" and strategy_state == "IDLE":
        aggressive_price = min(0.99, yes + 0.01)
        response = place_order("BUY", aggressive_price, ORDER_SIZE)

        if "error" not in response:
            log_trade("BUY", aggressive_price, ORDER_SIZE)
            position_size += ORDER_SIZE
            strategy_state = "LONG"

    elif action == "SELL" and strategy_state == "LONG":
        aggressive_price = max(0.01, yes - 0.01)
        response = place_order("SELL", aggressive_price, ORDER_SIZE)

        if "error" not in response:
            log_trade("SELL", aggressive_price, ORDER_SIZE)
            position_size -= ORDER_SIZE
            strategy_state = "IDLE"

    return jsonify({
        "status": "ok",
        "state": strategy_state,
        "position_size": position_size,
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
        "position_size": position_size,
        "pnl": compute_pnl(),
        "state": strategy_state
    })
