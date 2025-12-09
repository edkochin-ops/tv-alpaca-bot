import os
import time
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

# Max dollar size per trade (fractional notional orders)
MAX_POSITION_DOLLARS = float(os.getenv("MAX_POSITION_DOLLARS", "500"))

# Optional: whitelist tickers, e.g. "SPY,QQQ,TSLA"
ALLOWED_TICKERS = os.getenv("ALLOWED_TICKERS", "")
if ALLOWED_TICKERS:
    ALLOWED_TICKERS = [t.strip().upper() for t in ALLOWED_TICKERS.split(",")]
else:
    ALLOWED_TICKERS = []  # allow all symbols

app = Flask(__name__)

alpaca = REST(
    key_id=ALPACA_API_KEY_ID,
    secret_key=ALPACA_API_SECRET_KEY,
    base_url=ALPACA_BASE_URL,
)

# Track active stop-loss order IDs per symbol so we can cancel them on manual SELL
STOP_ORDERS = {}  # { "SPY": "order_id", ... }


# =========================
# HELPER FUNCTIONS
# =========================

def is_allowed_symbol(symbol: str) -> bool:
    if not ALLOWED_TICKERS:
        return True
    return symbol.upper() in ALLOWED_TICKERS


def get_position_qty(symbol: str) -> float:
    """Return current position quantity (0 if flat)."""
    try:
        pos = alpaca.get_position(symbol)
        return float(pos.qty)
    except Exception:
        # No position
        return 0.0


def get_last_price(symbol: str):
    """Get last traded price from Alpaca, or None on error."""
    try:
        trade = alpaca.get_latest_trade(symbol)
        return float(trade.price)
    except Exception as e:
        print("Error getting last price for", symbol, ":", e)
        return None


# =========================
# ORDER FUNCTIONS
# =========================

def submit_buy(symbol: str):
    """
    Submit market BUY using fractional notional orders,
    capped by MAX_POSITION_DOLLARS, with a 15% stop-loss.
    No check against today's open; we trust your indicator for entries.
    """
    symbol = symbol.upper()
    if not is_allowed_symbol(symbol):
        return {"status": "skipped", "reason": "symbol not allowed", "symbol": symbol}

    existing_qty = get_position_qty(symbol)
    if existing_qty > 0:
        return {"status": "skipped", "reason": "already long", "symbol": symbol, "qty": existing_qty}

    # 1) Submit the entry order (fractional notional, DAY)
    try:
        entry_order = alpaca.submit_order(
            symbol=symbol,
            notional=MAX_POSITION_DOLLARS,
            side="buy",
            type="market",
            time_in_force="day",  # required for fractional orders
        )
        print("Submitted entry order:", entry_order)
    except Exception as e:
        return {
            "status": "error",
            "side": "buy",
            "symbol": symbol,
            "error": f"entry order failed: {e}",
        }

    # 2) Wait briefly for position/price to update, then place a 15% stop-loss
    qty = 0.0
    price = None

    # Try up to 5 times, waiting 1 second between each
    for i in range(5):
        time.sleep(1)
        qty = get_position_qty(symbol)
        price = get_last_price(symbol)
        print(f"Attempt {i+1}: qty={qty}, price={price}")
        if qty > 0 and price is not None and price > 0:
            break

    if qty <= 0 or price is None or price <= 0:
        # Entry worked but we couldn't reliably get qty/price yet
        return {
            "status": "submitted_entry_only",
            "side": "buy",
            "symbol": symbol,
            "notional": MAX_POSITION_DOLLARS,
            "warning": "could not place stop-loss (no qty or price after retries)",
        }

    stop_price = round(price * 0.85, 2)  # 15% below current price

    try:
        stop_order = alpaca.submit_order(
            symbol=symbol,
            qty=qty,                  # match current position size (can be fractional)
            side="sell",
            type="stop",
            stop_price=stop_price,
            time_in_force="day",
        )
        STOP_ORDERS[symbol] = stop_order.id
        return {
            "status": "submitted_with_stop",
            "side": "buy",
            "symbol": symbol,
            "notional": MAX_POSITION_DOLLARS,
            "qty": qty,
            "entry_price": price,
            "stop_price": stop_price,
            "stop_order_id": stop_order.id,
        }
    except Exception as e:
        return {
            "status": "submitted_entry_only",
            "side": "buy",
            "symbol": symbol,
            "notional": MAX_POSITION_DOLLARS,
            "qty": qty,
            "entry_price": price,
            "stop_error": str(e),
        }


def submit_sell(symbol: str):
    """
    Submit market SELL to flatten an existing long position.
    Cancels any tracked stop-loss for this symbol.
    """
    symbol = symbol.upper()
    if not is_allowed_symbol(symbol):
        return {"status": "skipped", "reason": "symbol not allowed", "symbol": symbol}

    # Cancel any existing stop-loss order for this symbol
    if symbol in STOP_ORDERS:
        stop_id = STOP_ORDERS[symbol]
        try:
            alpaca.cancel_order(stop_id)
            print(f"Canceled stop-loss order {stop_id} for {symbol}")
        except Exception as e:
            print(f"Error canceling stop-loss for {symbol}: {e}")
        STOP_ORDERS.pop(symbol, None)

    existing_qty = get_position_qty(symbol)
    if existing_qty <= 0:
        return {"status": "skipped", "reason": "no long position", "symbol": symbol}

    qty = float(existing_qty)

    try:
        alpaca.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="market",
            time_in_force="day",
        )
        return {"status": "submitted", "side": "sell", "symbol": symbol, "qty": qty}
    except Exception as e:
        return {
            "status": "error",
            "side": "sell",
            "symbol": symbol,
            "error": str(e),
        }


# =========================
# WEB ENDPOINTS
# =========================

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    This is the URL TradingView will call.
    Expected JSON:
      { "ticker": "SPY", "signal": "BUY" }
    or:
      { "ticker": "SPY", "signal": "SELL" }
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    print("Webhook hit:", data)

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

    print("Trade result:", result)
    return jsonify({"ok": True, "symbol": symbol, "signal": signal, "result": result}), 200


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "env": ALPACA_ENV})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
