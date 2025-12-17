import os, time, threading, traceback
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from alpaca_trade_api import REST

# =====================
# CONFIG
# =====================
API_KEY = os.getenv("ALPACA_API_KEY_ID", "").strip()
API_SECRET = os.getenv("ALPACA_API_SECRET_KEY", "").strip()
ENV = os.getenv("ALPACA_ENV", "paper").strip().lower()
BASE_URL = "https://paper-api.alpaca.markets" if ENV == "paper" else "https://api.alpaca.markets"
if not API_KEY or not API_SECRET:
    raise RuntimeError("Missing ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY")

alpaca = REST(API_KEY, API_SECRET, BASE_URL)
app = Flask(__name__)

# Universe (hard)
ALLOWED_BASES = {"BTC", "ETH", "SOL"}

# Account sizing
MAX_NOTIONAL = float(os.getenv("MAX_POSITION_DOLLARS", "20000"))  # per trade notional

# Execution protections (your values)
MAX_IOC_SLIP_PCT = 0.0015      # 0.15%
COOLDOWN_SECONDS = 60
TV_PRICE_MAX_DEV = 0.0025      # 0.25%

# Exits (1m tuned)
TP1_PCT = 0.006   # +0.6%
TP2_PCT = 0.012   # +1.2%
SL_PCT  = 0.009   # -0.9%
STOP_LIMIT_SLIP = 0.0015  # 0.15% below stop for stop_limit

TP1_FRAC = 0.40
TP2_FRAC = 0.40
# remainder = 0.20

# Daily governors
DAILY_PROFIT_STOP = float(os.getenv("DAILY_PROFIT_STOP", "1100"))
DAILY_LOSS_STOP   = float(os.getenv("DAILY_LOSS_STOP", "-600"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "6"))
MAX_LOSERS_PER_DAY = int(os.getenv("MAX_LOSERS_PER_DAY", "2"))

# State (in-memory; Render restarts reset it)
STATE = {
    "day": None,
    "start_equity": None,
    "disabled": False,
    "trades": 0,
    "losers": 0,
}
LAST_BUY_TS = {}     # pair -> epoch seconds
EXIT_ORDERS = {}     # pair -> {"tp1":id,"tp2":id,"sl":id}

# =====================
# HELPERS
# =====================
def utc_day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def get_equity() -> float:
    a = alpaca.get_account()
    return float(a.equity)

def reset_day_if_needed():
    k = utc_day_key()
    if STATE["day"] != k:
        STATE["day"] = k
        STATE["start_equity"] = get_equity()
        STATE["disabled"] = False
        STATE["trades"] = 0
        STATE["losers"] = 0

def daily_pnl() -> float:
    if STATE["start_equity"] is None:
        STATE["start_equity"] = get_equity()
    return get_equity() - float(STATE["start_equity"])

def normalize(tv_symbol: str) -> str:
    s = (tv_symbol or "").upper().strip()
    if ":" in s:
        s = s.split(":", 1)[1]
    if "/" in s:
        return s
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}/{q}"
    return f"{s}/USD"

def base_of(pair: str) -> str:
    return pair.split("/")[0].upper()

def allowed_pair(pair: str) -> bool:
    return base_of(pair) in ALLOWED_BASES

def asset_sym(pair: str) -> str:
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

def too_far_from_tv(cur: float, tv: float) -> bool:
    return abs(cur - tv) / tv > TV_PRICE_MAX_DEV

def cancel_order(order_id: str):
    try:
        alpaca.cancel_order(order_id)
    except Exception:
        pass

def cancel_exits(pair: str):
    ids = EXIT_ORDERS.pop(pair, None)
    if not ids:
        return
    for oid in ids.values():
        cancel_order(oid)

def place_or_replace_stop(pair: str, qty: float, ref_price: float):
    # Replace stop with current remaining qty
    ids = EXIT_ORDERS.get(pair, {})
    if "sl" in ids:
        cancel_order(ids["sl"])

    stop_price = r(ref_price * (1 - SL_PCT))
    limit_price = r(stop_price * (1 - STOP_LIMIT_SLIP))
    o = alpaca.submit_order(
        symbol=pair,
        side="sell",
        type="stop_limit",
        time_in_force="gtc",
        qty=qty,
        stop_price=stop_price,
        limit_price=limit_price,
    )
    EXIT_ORDERS.setdefault(pair, {})["sl"] = o.id

def place_take_profits(pair: str, qty: float, ref_price: float):
    tp1_qty = r(qty * TP1_FRAC)
    tp2_qty = r(qty * TP2_FRAC)

    tp1_price = r(ref_price * (1 + TP1_PCT))
    tp2_price = r(ref_price * (1 + TP2_PCT))

    o1 = alpaca.submit_order(
        symbol=pair, side="sell", type="limit", time_in_force="gtc",
        qty=tp1_qty, limit_price=tp1_price
    )
    o2 = alpaca.submit_order(
        symbol=pair, side="sell", type="limit", time_in_force="gtc",
        qty=tp2_qty, limit_price=tp2_price
    )
    EXIT_ORDERS.setdefault(pair, {})["tp1"] = o1.id
    EXIT_ORDERS.setdefault(pair, {})["tp2"] = o2.id

def marketable_ioc_limit_buy(pair: str, notional: float, cur_price: float):
    qty = notional / cur_price
    limit_price = r(cur_price * (1 + MAX_IOC_SLIP_PCT))
    alpaca.submit_order(
        symbol=pair, side="buy", type="limit", time_in_force="ioc",
        qty=r(qty), limit_price=limit_price
    )
    return {"qty_req": qty, "limit": limit_price}

def marketable_ioc_limit_sell(pair: str, qty: float, cur_price: float):
    limit_price = r(cur_price * (1 - MAX_IOC_SLIP_PCT))
    alpaca.submit_order(
        symbol=pair, side="sell", type="limit", time_in_force="ioc",
        qty=r(qty), limit_price=limit_price
    )
    return {"limit": limit_price}

def enforce_daily_governors():
    reset_day_if_needed()
    pnl = daily_pnl()

    if STATE["disabled"]:
        return False, {"status": "disabled_for_day", "pnl": pnl}

    if pnl >= DAILY_PROFIT_STOP:
        STATE["disabled"] = True
        return False, {"status": "disabled_profit_target_hit", "pnl": pnl}

    if pnl <= DAILY_LOSS_STOP:
        STATE["disabled"] = True
        return False, {"status": "disabled_loss_stop_hit", "pnl": pnl}

    if STATE["trades"] >= MAX_TRADES_PER_DAY:
        STATE["disabled"] = True
        return False, {"status": "disabled_max_trades_hit", "pnl": pnl}

    if STATE["losers"] >= MAX_LOSERS_PER_DAY:
        STATE["disabled"] = True
        return False, {"status": "disabled_max_losers_hit", "pnl": pnl}

    return True, {"pnl": pnl, "trades": STATE["trades"], "losers": STATE["losers"]}

# =====================
# Background reconciler (keeps stop qty correct after partial TPs)
# =====================
def reconcile_loop():
    while True:
        try:
            # For each tracked pair, if position qty changed, update stop qty
            for pair in list(EXIT_ORDERS.keys()):
                q = get_qty(pair)
                if q <= 0:
                    # flat -> cancel remaining exits
                    cancel_exits(pair)
                    continue

                # ensure stop exists and matches remaining qty (replace each cycle)
                ref = get_price(pair)
                if ref:
                    place_or_replace_stop(pair, q, ref)

        except Exception:
            # swallow; keep loop alive
            pass
        time.sleep(8)  # lightweight

threading.Thread(target=reconcile_loop, daemon=True).start()

# =====================
# ACTIONS
# =====================
def do_buy(pair: str, tv_price: float | None):
    ok, info = enforce_daily_governors()
    if not ok:
        return info

    if not allowed_pair(pair):
        return {"status": "skipped", "reason": "pair_not_allowed", "pair": pair}

    now = time.time()
    last = LAST_BUY_TS.get(pair, 0)
    if now - last < COOLDOWN_SECONDS:
        return {"status": "skipped", "reason": "cooldown", "wait": round(COOLDOWN_SECONDS - (now-last), 2)}

    if get_qty(pair) > 0:
        return {"status": "skipped", "reason": "already_long"}

    cur = get_price(pair)
    if not cur:
        return {"status": "error", "reason": "no_current_price"}

    if tv_price and tv_price > 0 and too_far_from_tv(cur, tv_price):
        return {"status": "skipped", "reason": "tv_price_deviation", "current": cur, "tv_price": tv_price}

    # Clean any old exits
    cancel_exits(pair)

    # Entry
    entry = marketable_ioc_limit_buy(pair, MAX_NOTIONAL, cur)
    LAST_BUY_TS[pair] = now
    STATE["trades"] += 1

    # Wait briefly for position to appear, then place exits
    q = 0.0
    ref = None
    for _ in range(10):  # ~2s
        time.sleep(0.2)
        q = get_qty(pair)
        ref = get_price(pair)
        if q > 0 and ref:
            place_take_profits(pair, q, ref)
            place_or_replace_stop(pair, q, ref)
            return {"status": "bought", "entry": entry, "qty": q, "ref_price": ref, "guards": info}

    return {"status": "buy_sent_no_exits_yet", "entry": entry, "guards": info}

def do_sell(pair: str):
    # manual SELL: cancel exits then marketable sell
    q = get_qty(pair)
    if q <= 0:
        cancel_exits(pair)
        return {"status": "skipped", "reason": "no_position"}

    cancel_exits(pair)
    cur = get_price(pair)
    if not cur:
        return {"status": "error", "reason": "no_current_price"}

    res = marketable_ioc_limit_sell(pair, q, cur)
    return {"status": "sold", "qty": q, "exit": res}

# =====================
# ROUTES
# =====================
@app.route("/health", methods=["GET"])
def health():
    reset_day_if_needed()
    return jsonify({
        "status": "ok",
        "env": ENV,
        "allowed": sorted(list(ALLOWED_BASES)),
        "max_notional": MAX_NOTIONAL,
        "pnl": daily_pnl(),
        "disabled": STATE["disabled"],
        "trades": STATE["trades"],
        "losers": STATE["losers"],
    }), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True) or {}
        ticker = data.get("ticker", "")
        signal = (data.get("signal") or "").upper().strip()
        tv_price_raw = data.get("tv_price", None)

        if not ticker or signal not in ("BUY", "SELL"):
            return jsonify({"ok": False, "error": "Bad payload. Need {ticker, signal: BUY|SELL, tv_price(optional)}"}), 400

        # reject unsubstituted placeholders
        t = str(ticker)
        if "{{" in t or "}}" in t:
            return jsonify({"ok": False, "error": "Unsubstituted ticker placeholder. Use {{ticker}}."}), 400

        pair = normalize(t)

        tv_price = None
        if tv_price_raw is not None:
            try:
                tv_price = float(tv_price_raw)
            except Exception:
                tv_price = None

        if signal == "BUY":
            result = do_buy(pair, tv_price)
        else:
            result = do_sell(pair)

        return jsonify({"ok": True, "pair": pair, "signal": signal, "result": result}), 200

    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"ok": False, "error": str(e), "trace": tb[-1500:]}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
