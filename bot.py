import os
from flask import Flask, request, jsonify
from alpaca_trade_api import REST

# =========================
# CONFIGURATION
# =========================

ALPACA_API_KEY_ID = os.getenv("ALPACA_API_KEY_ID")
ALPACA_API_SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY")
ALPACA_ENV = os.getenv("ALPACA_ENV", "paper")  # "paper" or "live"

if not ALPACA_API_KEY_ID or not ALPACA_API_SECRET_KEY:
    raise RuntimeError("Missing Alpaca API credentials in environment variables.")

if ALPACA_ENV == "paper":
    ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
else:
    ALPACA_BASE_URL = "https://api.alpaca.markets"

# Max dollar size per ticker (change this to your comfort level)
MAX_POSITION_DOLLARS = float(os.getenv("MAX_POSITION_DOLLARS", "500"))

# Optional: whitelist tickers you allow the bot to trade, e.g. "SPY,QQQ,TSLA"
ALLOWED_TICKERS = os.getenv("ALLOWED_TICKERS", "")
if ALLOWED_TICKERS:
    ALLOWED_TICKERS = [t.strip().upper() for t in ALLOWED_TICKERS.split(",")]
else:
    ALLOWED_TICKERS = []  # means "allow all"

app = Flask(__name__)

alpaca = REST(
    key_id=ALPACA_API_KEY_ID,
    secret_key=ALPACA_API_SECRET_KEY,
    base_url=ALPACA_BASE_URL,
)


# =========================
# HELPER FUNCTIONS
# =========================

def is_allowed_symbol(symbol: str) -> bool:
    if not ALLOWED_TICKERS:
        return True
    return symbol.upper() in ALLOWED_TICKERS


def get_last_price(symbol: str) -> float:
    """Get last traded price from Alpaca."""
    trade = alpaca.get_latest_trade(symbol)
    return float(trade.price)


def get_position_qty(symbol: str) -> float:
    """Return current position quantity (0 if flat)."""
    try:
        pos = alpaca.get_position(symbol)
        return float(pos.qty)
    except Exception:
        # No position
        return 0.0


def calc_order_qty(symbol: str, max_dollars: float) -> int:
    """Calculate whole-share quantity based on allowed dollar risk."""
    price = get_last_price(symbol)
    if price <= 0:
        return 0
    qty = int(max_dollars // price)
    return max(qty, 1)


def submit_buy(symbol: str):
    """Submit market BUY if not already long."""
    symbol = symbol.upper()
    if not is_allowed_symbol(symbol):
        return {"status": "skipped", "reason": "symbol not allowed", "symbol": symbol}

    existing_qty = get_position_qty(symbol)
    if existing_qty > 0:
        return {"status": "skipped", "reason": "already long", "symbol": symbol, "qty": existing_qty}

    qty = calc_order_qty(symbol, MAX_POSITION_DOLLARS)
    if qty <= 0:
        return {"status": "skipped", "reason": "qty <= 0", "symbol": symbol}

    alpaca.submit_order(
        symbol=symbol,
        qty=qty,
        side="buy",
        type="market",
        time_in_force="day",
    )
    return {"status": "submitted", "side": "buy", "symbol": symbol, "qty": qty}


def submit_sell(symbol: str):
    """Submit market SELL to flatten an existing long position."""
    symbol = symbol.upper()
    if not is_allowed_symbol(symbol):
        return {"status": "skipped", "reason": "symbol not allowed", "symbol": symbol}

    existing_qty = get_position_qty(symbol)
    if existing_qty <= 0:
        return {"status": "skipped", "reason": "no long position", "symbol": symbol}

    qty = int(abs(existing_qty))

    alpaca.submit_order(
        symbol=symbol,
        qty=qty,
        side="sell",
        type="market",
        time_in_force="day",
    )
    return {"status": "submitted", "side": "sell", "symbol": symbol, "qty": qty}


# =========================
# WEBHOOK ENDPOINT
# =========================

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    This is the URL TradingView will call.
    Expected JSON from TradingView alert:
    {
        "ticker": "SPY",
        "signal": "BUY"
    }
    or:
    {
        "ticker": "SPY",
        "signal": "SELL"
    }
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    if not data:
        return jsonify({"ok": False, "error": "Empty payload"}), 400

    symbol = data.get("ticker")
    signal = data.get("signal")

    if not symbol or not signal:
        return jsonify({"ok": False, "error": "Missing 'ticker' or 'signal'"}), 400

    symbol = symbol.upper()
    signal = signal.upper()

    if signal == "BUY":
        result = submit_buy(symbol)
    elif signal == "SELL":
        result = submit_sell(symbol)
    else:
        return jsonify({"ok": False, "error": f"Unknown signal: {signal}"}), 400

    return jsonify({"ok": True, "symbol": symbol, "signal": signal, "result": result}), 200


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "env": ALPACA_ENV})


if __name__ == "__main__":
    # Render gives you a PORT environment variable; use it.
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
