
from flask import Flask, request, jsonify
import requests, os, json
import numpy as np
import yfinance as yf
from datetime import datetime

YES_TOKEN    = "53822162563147299519165214885693344405498185564842997386824738830845754444209"
ORDER_SIZE   = 1
MAX_LOSS     = -3.0
MAX_POSITION = 3
DRY_RUN      = True

trades = []
open_positions = 0
bot_suspended = False
thresholds = {}
yes_history = []
last_signal = {"action": None, "time": None}

if os.path.exists("trades.json"):
    with open("trades.json", "r") as f:
        trades = json.load(f)
        for t in trades:
            if t["action"] == "BUY":
                open_positions += ORDER_SIZE
            elif t["action"] == "SELL":
                open_positions -= ORDER_SIZE

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
    df      = yf.download("GC=F", period="30d", interval="1d", progress=False)
    closes  = df["Close"].values.flatten()
    highs   = df["High"].values.flatten()
    lows    = df["Low"].values.flatten()

    tr      = np.maximum(highs[1:] - lows[1:],
              np.maximum(abs(highs[1:] - closes[:-1]),
                         abs(lows[1:]  - closes[:-1])))
    atr     = float(np.mean(tr[-14:]))
    current = float(closes[-1])

    yes_now = get_yes_price()
    if yes_now is None:
        raise Exception("YES price unavailable")

    yes_std = float(np.std(yes_history)) if len(yes_history) > 10 else 0.04

    thresholds = {
        "gold_current" : round(current, 1),
        "buy_trigger"  : round(current - 1.0 * atr, 0),
        "sell_trigger" : round(current + 0.5 * atr, 0),
        "yes_buy_at"   : round(yes_now - 1.0 * yes_std, 3),
        "yes_sell_at"  : round(yes_now + 0.5 * yes_std, 3),
        "updated_at"   : datetime.now().strftime("%H:%M:%S"),
    }

def compute_pnl():
    pnl, buy_queue = 0.0, []
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

app = Flask(__name__)
compute_thresholds()

@app.route('/webhook', methods=['POST'])
def tradingview_webhook():
    global open_positions, bot_suspended, last_signal

    data   = request.json
    action = data.get("action", "").upper()
    gold   = float(data.get("price", 0))
    now    = datetime.now()

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
        if yes < thresholds["yes_buy_at"]:
            if not DRY_RUN:
                log_trade("BUY", yes)
            open_positions += ORDER_SIZE

    elif action == "SELL":
        if open_positions > 0 and yes > thresholds["yes_sell_at"]:
            if not DRY_RUN:
                log_trade("SELL", yes)
            open_positions -= ORDER_SIZE

    
    return jsonify({
        "status": "ok",
        "action": action,
        "yes": yes,
        "positions": open_positions,
        "pnl": compute_pnl()})
   @app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head><title>Gold Bot</title></head>
    <body style="font-family: Arial; max-width: 800px; margin: 50px auto; padding: 20px;">
        <h1>🤖 Gold Bot</h1>
        <p>Automated gold trading bot with Polymarket integration</p>
        <h2>Available Endpoints:</h2>
        <ul>
            <li><a href="/health">/health</a> - Health check and bot status</li>
            <li>/webhook - TradingView webhook (POST)</li>
        </ul>
        <p>Status: Running ✅</p>
    </body>
    </html>
    '''

 })

@app.route('/health')
def health():
    return jsonify({
        "status": "alive",
        "dry_run": DRY_RUN,
        "positions": open_positions,
        "pnl": compute_pnl(),
        "trades": len(trades),
        "thresholds": thresholds
    })


