import os
import time
from flask import Flask, request, jsonify
from alpaca_trade_api import REST

# =========================
# CONFIG
# =========================

ALPACA_API_KEY_ID = os.getenv("ALPACA_API_KEY_ID")
ALPACA_API_SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY")
ALPACA_ENV = os.getenv("ALPACA_ENV", "paper")  # "paper" or "live"

if not ALPACA_API_KEY_ID or not ALPACA_API_SECRET_KEY:
    raise RuntimeError("Missing Alpaca API credentials in environment variables.")

ALPACA_BASE_URL = "https://paper-api.alpaca.markets" if ALPACA_ENV == "paper" else "https://api.alpaca.markets"

MAX_POSITION_DOLLARS = float(os.getenv("MAX_POSITION_DOLLARS", "500"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.15"))  # 15% default
STOP_LIMIT_SLIP_PCT = float(os.getenv("STOP_LIMIT_SLIP_PCT", "0.005"))  # limit 0.5% below stop

# Optional allowlist. Leave empty to allow all crypto pairs.
# Example: "BTC/USD,ETH/USD,SOL/USD"
ALLOWED_PAIRS = os.getenv("ALLOWED_PAIRS", "")
if ALLOWED_PAIRS.strip():
    ALLOWED_PAIRS = {p.strip().upper() for p in ALLOWED_PAIRS.split(",")}
else:
    ALLOWED_PAIRS = set()

alpaca = REST(
    key_id=ALPACA_API_KEY_ID,
    secret_key=ALPACA_API_SECRET_KEY,
    base_url=ALPACA_BASE_URL,
)

app = Flask(__name__)

# Keep track of stop-loss order IDs so we can cancel/replace cleanly
STOP_ORDERS = {}  # { "BTC/USD": "order_id", ... }


# =========================
# SYMBOL NORMALIZATION
# =========================

def normalize_tv_symbol(tv_symbol: str) -> str:
    """
    TradingView examples you might send:
      "BTCUSD", "ETHUSD", "SOLUSDT", "COINBASE:BTCUSD", "BINANCE:SOLUSDT", "BTC/USD"
    Alpaca tradable pair examples:
      "BTC/USD", "ETH/USD", "SOL/USDT"
    """
    s = (tv_symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]  # drop exchange prefix

    if "/" in s:
        return s

    # common quotes
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            base = s[:-len(q)]
            return f"{base}/{q}"

    # fallback: assume USD quote if unknown
    return f"{s}/USD"


def is_allowed_pair(pair: str) -> bool:
    return (not ALLOWED_PAIRS) or (pair.upper() in ALLOWED_PAIRS)


# =========================
# DATA HELPERS
# =========================

def get_last_price(pair: str) -> float | None:
    try:
        t = alpaca.get_latest_trade(pair)
        return float(t.price)
    except Exception:
        return None


def get_position_qty(pair: str) -> float:
    """
    Note: Alpaca positions may show BTCUSD instead of BTC/USD.
    We'll try both formats.
    """
    # 1) Try asset-like symbol (BTCUSD)
    asset_sym = pair.replace("/", "")
    try:
        pos = alpaca.get_position(asset_sym)
        return float(pos.qty)
    except Exception:
        pass

    # 2) Try pair itself (some SDKs support it)
    try:
        pos = alpaca.get_position(pair)
        return float(pos.qty)
    except Exception:
        return 0.0


def round_price(p: float) -> float:
    # Simple rounding: more decimals for small prices
    if p >= 1000:
        return round(p, 2)
    if p >= 1:
        return round(p, 4)
    return round(p, 8)


# =========================
# ORDER HELPERS
# =========================

def cancel_stop_if_any(pair: str):
    oid = STOP_ORDERS.pop(pair, None)
    if not oid:
        return
    try:
        alpaca.cancel_order(oid)
    except Exception:
        pass


def place_stop_loss(pair: str, qty: float, ref_price: float) -> dict:
    """
    Crypto supports stop_limit (not stop). time_in_force must be gtc or ioc. :contentReference[oaicite:5]{index=5}
    We place:
      stop_price = ref_price * (1 - STOP_LOSS_PCT)
      limit_price = stop_price * (1 - STOP_LIMIT_SLIP_PCT)
    """
    stop_price = round_price(ref_price * (1.0 - STOP_LOSS_PCT))
    limit_price = round_price(stop_price * (1.0 - STOP_LIMIT_SLIP_PCT))

    try:
        o = alpaca.submit_order(
            symbol=pair,
            side="sell",
            type="stop_limit",
            time_in_force="gtc",     # crypto-supported :contentReference[oaicite:6]{index=6}
            qty=qty,                 # crypto supports fractional qty :contentReference[oaicite:7]{index=7}
            stop_price=stop_price,
            limit_price=limit_price,
        )
        STOP_ORDERS[pair] = o.id
        return {"status": "stop_placed", "stop_price": stop_price, "limit_price": limit_price, "stop_order_id": o.id}
    except Exception as e:
        return {"status": "stop_error", "error": str(e), "stop_price": stop_price, "limit_price": limit_price}


def submit_buy(pair: str) -> dict:
    pair = pair.upper()
    if not is_allowed_pair(pair):
        return {"status": "skipped", "reason": "pair not allowed", "pair": pair}

    # Don’t stack; only 1 position per pair
    if get_position_qty(pair) > 0:
        return {"status": "skipped", "reason": "already long", "pair": pair}

    # Cancel old stop if any (shouldn't exist if flat, but keeps state clean)
    cancel_stop_if_any(pair)

    # Market buy (notional) – crypto supports notional fractionals :contentReference[oaicite:8]{index=8}
    try:
        alpaca.submit_order(
            symbol=pair,
            side="buy",
            type="market",
            time_in_force="ioc",      # crypto-supported :contentReference[oaicite:9]{index=9}
            notional=MAX_POSITION_DOLLARS,
        )
    except Exception as e:
        return {"status": "error", "side": "buy", "pair": pair, "error": str(e)}

    # Try briefly to read qty + price for stop placement
    qty = 0.0
    price = None
    for _ in range(4):  # ~1 second total
        time.sleep(0.25)
        qty = get_position_qty(pair)
        price = get_last_price(pair)
        if qty > 0 and price and price > 0:
            break

    if qty <= 0 or not price or price <= 0:
        return {
            "status": "submitted_entry_only",
            "side": "buy",
            "pair": pair,
            "notional": MAX_POSITION_DOLLARS,
            "warning": "entry submitted; qty/price not available yet for stop placement",
        }

    stop_res = place_stop_loss(pair, qty, price)
    return {
        "status": "submitted",
        "side": "buy",
        "pair": pair,
        "notional": MAX_POSITION_DOLLARS,
        "qty": qty,
        "ref_price": price,
        "stop": stop_res,
    }


def submit_sell(pair: str) -> dict:
    pair = pair.upper()
    if not is_allowed_pair(pair):
        return {"status": "skipped", "reason": "pair not allowed", "pair": pair}

    cancel_stop_if_any(pair)

    qty = get_position_qty(pair)
    if qty <= 0:
        return {"status": "skipped", "reason": "no position", "pair": pair}

    try:
        alpaca.submit_order(
            symbol=pair,
            side="sell",
            type="market",
            time_in_force="ioc",  # crypto-supported :contentReference[oaicite:10]{index=10}
            qty=qty,
        )
        return {"status": "submitted", "side": "sell", "pair": pair, "qty": qty}
    except Exception as e:
        return {"status": "error", "side": "sell", "pair": pair, "error": str(e)}


# =========================
# WEBHOOK
# =========================

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    TradingView should POST JSON like:
      { "ticker": "BTCUSD", "signal": "BUY" }
      { "ticker": "ETHUSD", "signal": "SELL" }
    We'll normalize to Alpaca pairs like BTC/USD, ETH/USD.
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    ticker = (data or {}).get("ticker")
    signal = (data or {}).get("signal")

    if not ticker or not signal:
        return jsonify({"ok": False, "error": "Missing 'ticker' or 'signal'"}), 400

    pair = normalize_tv_symbol(ticker)
    sig = str(signal).strip().upper()

    if sig == "BUY":
        result = submit_buy(pair)
    elif sig == "SELL":
        result = submit_sell(pair)
    else:
        return jsonify({"ok": False, "error": f"Unknown signal: {sig}"}), 400

    return jsonify({"ok": True, "pair": pair, "signal": sig, "result": result}), 200


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "env": ALPACA_ENV})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
