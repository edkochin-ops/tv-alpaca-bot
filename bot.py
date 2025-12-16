import os
import time
import traceback
from flask import Flask, request, jsonify
from alpaca_trade_api import REST

# =====================
# CONFIG (safe)
# =====================

def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()

API_KEY = env("ALPACA_API_KEY_ID")
API_SECRET = env("ALPACA_API_SECRET_KEY")
ENV = os.getenv("ALPACA_ENV", "paper").strip().lower()
BASE_URL = "https://paper-api.alpaca.markets" if ENV == "paper" else "https://api.alpaca.markets"

MAX_NOTIONAL = float(os.getenv("MAX_POSITION_DOLLARS", "500"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.012"))          # 1.2% (1m)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.008"))      # 0.8% (1m)
STOP_LIMIT_SLIP = float(os.getenv("STOP_LIMIT_SLIP_PCT", "0.002"))  # 0.2%

alpaca = REST(API_KEY, API_SECRET, BASE_URL)
app = Flask(__name__)

# Track exit orders per pair: {"BTC/USD": {"tp": id, "sl": id}}
EXITS: dict[str, dict[str, str]] = {}

# =====================
# HELPERS
# =====================

def normalize(symbol: str) -> str:
    s = (symbol or "").upper().strip()
    if ":" in s:
        s = s.split(":", 1)[1]
    if "/" in s:
        return s
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}/{q}"
    return f"{s}/USD"

def asset_sym(pair: str) -> str:
    return pair.replace("/", "")

def get_qty(pair: str) -> float:
    # Positions often appear as BTCUSD
    try:
        return float(alpaca.get_position(asset_sym(pair)).qty)
    except Exception:
        return 0.0

def get_price(pair: str) -> float | None:
    try:
        return float(alpaca.get_latest_trade(pair).price)
    except Exception:
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
        except Exception:
            pass

def place_exits(pair: str, qty: float, entry: float) -> dict:
    # Take Profit (limit)
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

# =====================
# CORE ACTIONS
# =====================

def do_buy(pair: str) -> dict:
    if get_qty(pair) > 0:
        return {"status": "skipped", "reason": "already long"}

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

def do_sell(pair: str) -> dict:
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

# =====================
# ROUTES (never crash)
# =====================

@app.route("/health", methods=["GET"])
def health():
    try:
        return jsonify({
            "status": "ok",
            "env": ENV,
            "max_notional": MAX_NOTIONAL,
            "tp": TAKE_PROFIT_PCT,
            "sl": STOP_LOSS_PCT
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True) or {}
        ticker = data.get("ticker")
        signal = (data.get("signal") or "").upper().strip()

        if not ticker or signal not in ("BUY", "SELL"):
            return jsonify({"ok": False, "error": "Bad payload. Need {ticker, signal: BUY|SELL}"}), 400

        pair = normalize(ticker)

        if signal == "BUY":
            res = do_buy(pair)
        else:
            res = do_sell(pair)

        return jsonify({"ok": True, "pair": pair, "signal": signal, "result": res}), 200

    except Exception as e:
        # Return the actual exception so you can see it immediately
        tb = traceback.format_exc()
        print(tb)
        return jsonify({"ok": False, "error": str(e), "trace": tb[-1500:]}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
