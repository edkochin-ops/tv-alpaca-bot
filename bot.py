import os
import time
import traceback
from flask import Flask, request, jsonify
from alpaca_trade_api import REST

# =====================
# CRYPTO-ONLY SCALPER BOT (1m)
# - Flexible pairs (BTCUSD, BINANCE:SOLUSDT, BTC/USD, etc.)
# - Market BUY using notional (fractional)
# - Places TP (limit) + SL (stop_limit) immediately after fill visibility
# - Tight defaults for 1m: TP 0.6%, SL 0.9%
# - Rejects unsubstituted TradingView placeholders ({{TICKER}}, {{ticker}}, etc.)
# - /health endpoint
# =====================

def must_env(name: str) -> str:
    v = os.getenv(name, "")
    if not v.strip():
        raise RuntimeError(f"Missing env var: {name}")
    return v.strip()

API_KEY = must_env("ALPACA_API_KEY_ID")
API_SECRET = must_env("ALPACA_API_SECRET_KEY")
ENV = os.getenv("ALPACA_ENV", "paper").strip().lower()
BASE_URL = "https://paper-api.alpaca.markets" if ENV == "paper" else "https://api.alpaca.markets"

MAX_NOTIONAL = float(os.getenv("MAX_POSITION_DOLLARS", "500"))

# Tight 1m defaults (override via Render env vars)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.006"))        # +0.6%
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.009"))            # -0.9%
STOP_LIMIT_SLIP = float(os.getenv("STOP_LIMIT_SLIP_PCT", "0.0015"))   # 0.15%

alpaca = REST(API_KEY, API_SECRET, BASE_URL)
app = Flask(__name__)

# Track exit orders per pair: {"BTC/USD": {"tp": id, "sl": id}}
EXITS: dict[str, dict[str, str]] = {}


# =====================
# Helpers
# =====================

def normalize(tv_symbol: str) -> str:
    """
    Accepts:
      "BTCUSD", "ETHUSD", "SOLUSDT", "COINBASE:BTCUSD", "BINANCE:SOLUSDT", "BTC/USD"
    Returns:
      "BTC/USD", "SOL/USDT", etc.
    """
    s = (tv_symbol or "").upper().strip()
    if ":" in s:
        s = s.split(":", 1)[1]
    if "/" in s:
        return s
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}/{q}"
    return f"{s}/USD"

def asset_sym(pair: str) -> str:
    # Alpaca positions typically show BTCUSD, ETHUSD, etc.
    return pair.replace("/", "")

def get_qty(pair: str) -> float:
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

def place_exits(pair: str, position_qty: float, entry: float) -> dict:
    # Take Profit (limit)
    tp_price = r(entry * (1 + TAKE_PROFIT_PCT))
    tp = alpaca.submit_order(
        symbol=pair,
        side="sell",
        type="limit",
        time_in_force="gtc",
        qty=position_qty,
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
        qty=position_qty,
        stop_price=sl_stop,
        limit_price=sl_limit,
    )

    EXITS[pair] = {"tp": tp.id, "sl": sl.id}
    return {"tp": tp_price, "sl": sl_stop}


# =====================
# Actions
# =====================

def do_buy(pair: str) -> dict:
    # Avoid stacking on the same pair
    if get_qty(pair) > 0:
        return {"status": "skipped", "reason": "already long"}

    # Clean any stale exits
    cancel_exits(pair)

    # Market buy with notional (fractional)
    alpaca.submit_order(
        symbol=pair,
        side="buy",
        type="market",
        time_in_force="ioc",
        notional=MAX_NOTIONAL,
    )

    # Minimal wait for position/price visibility
    q = 0.0
    p = None
    for _ in range(6):  # ~1.2s total
        time.sleep(0.2)
        q = get_qty(pair)
        p = get_price(pair)
        if q > 0 and p:
            exits = place_exits(pair, q, p)
            return {"status": "bought", "qty": q, "entry": p, "exits": exits}

    return {"status": "entry_sent_no_exits"}

def do_sell(pair: str) -> dict:
    # Cancel exits first to prevent double-sell
    cancel_exits(pair)

    q = get_qty(pair)
    if q <= 0:
        return {"status": "skipped", "reason": "no position"}

    alpaca.submit_order(
        symbol=pair,
        side="sell",
        type="market",
        time_in_force="ioc",
        qty=q,
    )
    return {"status": "sold", "qty": q}


# =====================
# Routes
# =====================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "env": ENV,
        "max_notional": MAX_NOTIONAL,
        "tp": TAKE_PROFIT_PCT,
        "sl": STOP_LOSS_PCT,
        "slip": STOP_LIMIT_SLIP
    }), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True) or {}
        print("RAW PAYLOAD:", data)

        ticker = data.get("ticker", "")
        signal = (data.get("signal") or "").upper().strip()

        if not ticker or signal not in ("BUY", "SELL"):
            return jsonify({"ok": False, "error": "Bad payload. Need {ticker, signal: BUY|SELL}"}), 400

        ticker_str = str(ticker)
        # Reject unsubstituted TradingView placeholders like {{ticker}} or {{TICKER}}
        if "{{" in ticker_str or "}}" in ticker_str:
            return jsonify({
                "ok": False,
                "error": "Ticker placeholder not substituted. In TradingView alert message use {{ticker}} (lowercase)."
            }), 400

        pair = normalize(ticker_str)

        res = do_buy(pair) if signal == "BUY" else do_sell(pair)
        return jsonify({"ok": True, "pair": pair, "signal": signal, "result": res}), 200

    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        return jsonify({"ok": False, "error": str(e), "trace": tb[-1500:]}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
