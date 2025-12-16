import os
import time
from flask import Flask, request, jsonify
from alpaca_trade_api import REST

API_KEY = os.getenv("ALPACA_API_KEY_ID")
API_SECRET = os.getenv("ALPACA_API_SECRET_KEY")
ENV = os.getenv("ALPACA_ENV", "paper")
BASE_URL = "https://paper-api.alpaca.markets" if ENV == "paper" else "https://api.alpaca.markets"

MAX_NOTIONAL = float(os.getenv("MAX_POSITION_DOLLARS", "500"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.012"))          # 1.2%
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.008"))      # 0.8%
STOP_LIMIT_SLIP = float(os.getenv("STOP_LIMIT_SLIP_PCT", "0.002"))  # 0.2%

alpaca = REST(API_KEY, API_SECRET, BASE_URL)
app = Flask(__name__)

# Track exit orders per pair: {"BTC/USD": {"tp": id, "sl": id}}
EXITS = {}

def normalize(symbol: str) -> str:
    s = symbol.upper()
    if ":" in s:
        s = s.split(":")[1]
    if "/" in s:
        return s
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q):
            return f"{s[:-len(q)]}/{q}"
    return f"{s}/USD"

def asset_sym(pair: str) -> str:
    return pair.replace("/", "")

def get_qty(pair: str) -> float:
    try:
        return float(alpaca.get_position(asset_sym(pair)).qty)
    except:
        return 0.0

def get_price(pair: str) -> float | None:
    try:
        return float(alpaca.get_latest_trade(pair).price)
    except:
        return None

def r(p: float) -> float:
    return round(p, 8 if p < 1 else 4)

def cancel_exits(pair: str):
    ids = EXITS.pop(pair, None)
    if not ids:
        return
    for oid in ids.values():
        try:
            alpaca.cancel_order(oid)
        except:
            pass

def place_exits(pair: str, qty: float, entry: float):
    # Take Profit
    tp_price = r(entry * (1 + TAKE_PROFIT_PCT))
    tp = alpaca.submit_order(
        symbol=pair,
        side="sell",
        type="limit",
        time_in_force="gtc",
        qty=qty,
        limit_price=tp_price,
    )

    # Stop Loss (stop_limit)
    sl_stop = r(entry * (1 - STOP_LOSS_PCT))
    sl_limit = r(sl_stop * (1 - STOP_LIMIT_SLIP))
    sl = alpaca.submit_order(
        symbol=pair,
        side="sell",
        type="stop_limit",
        time_in_force="gtc",
        qty=qty,
        stop_price=sl_stop,
        limit_price=sl_limit,
    )

    EXITS[pair] = {"tp": tp.id, "sl": sl.id}
    return {"tp": tp_price, "sl": sl_stop}

def buy(pair: str):
    if get_qty(pair) > 0:
        return {"status": "skipped", "reason": "already long"}

    # clean any stale exits
    cancel_exits(pair)

    alpaca.submit_order(
        symbol=pair,
        side="buy",
        type="market",
        time_in_force="ioc",
        notional=MAX_NOTIONAL
    )

    qty = 0.0
    price = None
    for _ in range(6):  # ~1.5s
        time.sleep(0.25)
        qty = get_qty(pair)
        price = get_price(pair)
        if qty > 0 and price:
            exits = place_exits(pair, qty, price)
            return {"status": "bought", "qty": qty, "entry": price, "exits": exits}

    return {"status": "entry_sent_no_exits"}

def sell(pair: str):
    cancel_exits(pair)
    qty = get_qty(pair)
    if qty <= 0:
        return {"status": "skipped", "reason": "no position"}

    alpaca.submit_order(
        symbol=pair,
        side="sell",
        type="market",
        time_in_force="ioc",
        qty=qty
    )
    return {"status": "sold", "qty": qty}

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    pair = normalize(data.get("ticker", ""))
    signal = data.get("signal", "").upper()

    if signal == "BUY":
        res = buy(pair)
    elif signal == "SELL":
        res = sell(pair)
    else:
        return jsonify({"ok": False, "error": "bad signal"}), 400

    return jsonify({"ok": True, "pair": pair, "result": res}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "env": ENV})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
