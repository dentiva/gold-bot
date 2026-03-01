# ══════════════════════════════════════════════════════════════════
#  IMPORTS
# ══════════════════════════════════════════════════════════════════
from flask import Flask, request, jsonify
import threading, requests, time, os
import numpy as np
import yfinance as yf
from datetime import datetime

# ══════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════
YES_TOKEN    = "53822162563147299519165214885693344405498185564842997386824738830845754444209"
ORDER_SIZE   = 1        # $1 USDC per rotation
MAX_LOSS     = -3.0     # Circuit breaker — stop trading if PnL < -$3
DRY_RUN      = True     # ← flip False when ready for real orders

# ══════════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════════
yes_history    = []   # rolling YES prices for stddev
trades         = []   # all executed trades
open_positions = 0    # YES tokens currently held
bot_suspended  = False
thresholds     = {}

# ══════════════════════════════════════════════════════════════════
#  POLYMARKET PRICE
# ══════════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════════
#  DYNAMIC THRESHOLD ENGINE
# ══════════════════════════════════════════════════════════════════
def compute_thresholds():
    global thresholds
    try:
        df      = yf.download("GC=F", period="30d", interval="1d", progress=False)
        closes  = df["Close"].values.flatten()
        highs   = df["High"].values.flatten()
        lows    = df["Low"].values.flatten()

        tr      = np.maximum(highs[1:] - lows[1:],
                  np.maximum(abs(highs[1:] - closes[:-1]),
                             abs(lows[1:]  - closes[:-1])))
        atr     = float(np.mean(tr[-14:]))
        current = float(closes[-1])

        yes_now = get_yes_price() or 0.89
        yes_std = float(np.std(yes_history)) if len(yes_history) > 10 else 0.04

        thresholds = {
            "gold_current" : round(current, 1),
            "atr"          : round(atr, 1),
            "buy_trigger"  : round(current - 1.0 * atr, 0),
            "sell_trigger" : round(current + 0.5 * atr, 0),
            "stop_loss"    : round(current - 2.0 * atr, 0),
            "yes_current"  : round(yes_now, 3),
            "yes_std"      : round(yes_std, 4),
            "yes_buy_at"   : round(yes_now - 1.0 * yes_std, 3),
            "yes_sell_at"  : round(yes_now + 0.5 * yes_std, 3),
            "updated_at"   : datetime.now().strftime("%H:%M:%S"),
        }

        print(f"\n{'━'*55}")
        print(f"📊 THRESHOLDS AUTO-COMPUTED @ {thresholds['updated_at']}")
        print(f"{'━'*55}")
        print(f"  Gold  : ${current:,.1f}  |  Daily ATR: ${atr:,.1f}")
        print(f"  YES   : {yes_now:.3f}    |  Std Dev  : {yes_std:.4f}")
        print(f"")
        print(f"  🟢 BUY  when Gold < ${thresholds['buy_trigger']:,.0f}  &  YES < {thresholds['yes_buy_at']:.3f}")
        print(f"  🔴 SELL when Gold > ${thresholds['sell_trigger']:,.0f}  &  YES > {thresholds['yes_sell_at']:.3f}")
        print(f"  🛑 STOP when Gold < ${thresholds['stop_loss']:,.0f}  (invalidation)")
        print(f"{'━'*55}")

    except Exception as e:
        print(f"⚠️  Threshold compute failed: {e}")

# ══════════════════════════════════════════════════════════════════
#  POLYMARKET CLIENT
# ══════════════════════════════════════════════════════════════════
def get_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    creds = ApiCreds(
        api_key        = os.environ.get("POLY_API_KEY"),
        api_secret     = os.environ.get("POLY_SECRET"),
        api_passphrase = os.environ.get("POLY_PASSPHRASE"),
    )
    return ClobClient(
        "https://clob.polymarket.com",
        key      = os.environ.get("POLYMARKET_PRIVATE_KEY"),
        chain_id = 137,
        creds    = creds,
        funder   = os.environ.get("POLYMARKET_FUNDER"),
    )

def place_buy_order(yes_price):
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        client = get_client()
        order  = OrderArgs(
            token_id = YES_TOKEN,
            price    = round(yes_price + 0.001, 4),
            size     = ORDER_SIZE,
            side     = BUY
        )
        return client.post_order(client.create_order(order), OrderType.GTC)
    except Exception as e:
        return {"error": str(e)}

def place_sell_order(yes_price):
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL
        client = get_client()
        order  = OrderArgs(
            token_id = YES_TOKEN,
            price    = round(yes_price - 0.001, 4),
            size     = ORDER_SIZE,
            side     = SELL
        )
        return client.post_order(client.create_order(order), OrderType.GTC)
    except Exception as e:
        return {"error": str(e)}

# ══════════════════════════════════════════════════════════════════
#  PnL TRACKER
# ══════════════════════════════════════════════════════════════════
def log_trade(action, price):
    trades.append({
        "action" : action,
        "price"  : price,
        "size"   : ORDER_SIZE,
        "time"   : datetime.now().strftime("%H:%M:%S")
    })

def compute_pnl():
    pnl, buy_queue = 0.0, []
    for t in trades:
        if t["action"] == "BUY":
            buy_queue.append(t["price"])
        elif t["action"] == "SELL" and buy_queue:
            pnl += (t["price"] - buy_queue.pop(0)) * t["size"]
    return round(pnl, 4)

# ══════════════════════════════════════════════════════════════════
#  WEBHOOK HANDLER
# ══════════════════════════════════════════════════════════════════
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def tradingview_webhook():
    global open_positions, bot_suspended

    data   = request.json
    gold   = float(data.get("price", 0))
    action = data.get("action", "").upper()
    ts     = datetime.now().strftime("%H:%M:%S")
    yes    = get_yes_price()
    pnl    = compute_pnl()
    t      = thresholds

    print(f"\n{'━'*55}")
    print(f"🔔 [{ts}] {action} | Gold: ${gold:,.1f} | YES: {yes:.3f} | PnL: ${pnl:.3f}")

    if pnl <= MAX_LOSS:
        bot_suspended = True
        print(f"   🚨 MAX LOSS ${MAX_LOSS} HIT — bot suspended! Manual review needed.")
        return jsonify({"status": "suspended", "pnl": pnl})

    if action == "BUY":
        if yes and yes < t.get("yes_buy_at", 0.85):
            upside = round((t.get("yes_sell_at", 0.91) - yes) / yes * 100, 1)
            print(f"   🟢 BUY CONDITIONS MET")
            print(f"      YES at    : {yes:.3f} ({yes*100:.1f}¢)")
            print(f"      Target    : {t.get('yes_sell_at', 0.91):.3f}")
            print(f"      Upside    : +{upside}%")
            if not DRY_RUN:
                result = place_buy_order(yes)
                if "error" not in str(result):
                    log_trade("BUY", yes)
                    open_positions += ORDER_SIZE
                    print(f"   ✅ BUY placed! Open positions: {open_positions}")
                else:
                    print(f"   ❌ BUY failed: {result}")
            else:
                print(f"   [DRY RUN] Would BUY ${ORDER_SIZE} YES at {yes:.3f}")
        else:
            print(f"   ⏳ Gold dipped but YES not cheap enough ({yes:.3f} > {t.get('yes_buy_at', 0.85):.3f}) — waiting")

    elif action == "SELL":
        if open_positions <= 0:
            print(f"   ⚠️  No open positions to sell — skipping")
        elif yes and yes > t.get("yes_sell_at", 0.91):
            profit = round((yes - t.get("yes_buy_at", 0.85)) * ORDER_SIZE, 4)
            print(f"   🔴 SELL CONDITIONS MET")
            print(f"      YES at    : {yes:.3f} ({yes*100:.1f}¢)")
            print(f"      Est profit: +${profit:.4f}")
            if not DRY_RUN:
                result = place_sell_order(yes)
                if "error" not in str(result):
                    log_trade("SELL", yes)
                    open_positions -= ORDER_SIZE
                    print(f"   ✅ SOLD! Total PnL: ${compute_pnl():.4f} | Positions left: {open_positions}")
                else:
                    print(f"   ❌ SELL failed: {result}")
            else:
                print(f"   [DRY RUN] Would SELL ${ORDER_SIZE} YES at {yes:.3f}")
        else:
            print(f"   ⏳ Gold rallied but YES not high enough ({yes:.3f}) — holding")

    elif action == "STOP":
        print(f"   🛑 STOP LOSS triggered at ${gold:,.1f}")
        print(f"      Open positions : {open_positions} (holding — wait for recovery or manual exit)")
        print(f"      No new BUYs until next threshold refresh")

    print(f"{'━'*55}")
    return jsonify({
        "status"         : "ok",
        "action"         : action,
        "gold"           : gold,
        "yes"            : yes,
        "open_positions" : open_positions,
        "pnl"            : compute_pnl(),
        "trades"         : len(trades)
    })

@app.route('/health', methods=['GET'])
def health():
    yes = get_yes_price()
    return jsonify({
        "status"         : "alive",
        "dry_run"        : DRY_RUN,
        "yes_price"      : yes,
        "open_positions" : open_positions,
        "pnl"            : compute_pnl(),
        "trades"         : trades[-5:],
        "thresholds"     : thresholds,
        "bot_suspended"  : bot_suspended,
        "timestamp"      : datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

# ══════════════════════════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    
    compute_thresholds()
    
    print("━" * 55)
    print("🤖  GOLD ROTATION BOT — RAILWAY DEPLOYMENT")
    print("━" * 55)
    print(f"  Mode     : {'⚠️  LIVE TRADING' if not DRY_RUN else '🧪 DRY RUN'}")
    print(f"  Size     : ${ORDER_SIZE} per rotation")
    print(f"  Bankroll : $9 USDC")
    print(f"  Max Loss : ${abs(MAX_LOSS)} circuit breaker")
    print("━" * 55)

    threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=PORT,
            debug=False,
            use_reloader=False
        ),
        daemon=True
    ).start()

    cycle = 0
    while True:
        time.sleep(60)
        cycle += 1
        yes = get_yes_price()
        pnl = compute_pnl()

        if cycle % 60 == 0:
            print(f"\n🔄 Hourly refresh...")
            compute_thresholds()

        status = "🚨 SUSPENDED" if bot_suspended else "✅"
        yes_str = f"{yes:.3f} ({yes*100:.1f}¢)" if yes else "N/A"
        print(
            f"[{datetime.now().strftime('%H:%M')}] {status} | "
            f"YES: {yes_str} | "
            f"Buy<${thresholds.get('buy_trigger','?'):,.0f} | "
            f"Sell>${thresholds.get('sell_trigger','?'):,.0f} | "
            f"Pos: {open_positions} | "
            f"PnL: ${pnl:.3f} | "
            f"Trades: {len(trades)}"
        )
