"""
Risk management: position sizing, reward targets, and trading-day cadence
(alternate-day trading to keep cash-account funds settled, no PDT concerns
since this is a cash account).

All the actual numbers live in risk_config.json -- edit that file as the
account grows, no code changes needed.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime

from moomoo import OpenQuoteContext, RET_OK, KLType, AuType

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "risk_config.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "trade_log.json")


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"trades": []}  # each entry: {"date": "YYYY-MM-DD", "ticker": ..., "risk_dollars": ...}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def calculate_position_size(entry_price, stop_price, config=None):
    """Shares to buy so a stop-out loses exactly risk_per_trade_dollars,
    capped by actual buying power (can't spend more than the account has)."""
    config = config or load_config()
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0:
        raise ValueError("entry_price and stop_price can't be equal")

    risk_dollars = config["risk_per_trade_dollars"]
    shares_by_risk = int(risk_dollars / risk_per_share)

    equity = config["account_equity"]
    shares_by_buying_power = int(equity / entry_price)

    shares = min(shares_by_risk, shares_by_buying_power)
    capped_by_buying_power = shares_by_buying_power < shares_by_risk

    return {
        "shares": max(shares, 0),
        "cost": round(shares * entry_price, 2),
        "actual_risk_dollars": round(shares * risk_per_share, 2),
        "target_price_2r": round(entry_price + (entry_price - stop_price) * 2, 2) if entry_price > stop_price
                            else round(entry_price - (stop_price - entry_price) * 2, 2),
        "capped_by_buying_power": capped_by_buying_power,
    }


def calculate_structural_stop(code, lookback_bars=15, buffer_atr_mult=0.5, ktype=KLType.K_1M, ctx=None):
    """Real stop-loss placement: below the recent swing low, PLUS a volatility
    buffer, instead of sitting exactly at the 'obvious' level other traders
    (and stop-hunting algos) are watching. Buffer size scales with the
    stock's own recent volatility (ATR) rather than a flat cents/percent
    amount, so quiet stocks get a tight buffer and volatile ones get a wider
    one -- both proportionate to what's actually normal noise for that stock.
    Pass an existing ctx to reuse one connection across many tickers."""
    owns_ctx = ctx is None
    if owns_ctx:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    ret, df, _page_key = ctx.request_history_kline(
        code, start=None, end=None, ktype=ktype, autype=AuType.QFQ, max_count=lookback_bars + 1
    )
    if owns_ctx:
        ctx.close()
    if ret != RET_OK or df is None or len(df) < 2:
        raise RuntimeError(f"Could not fetch kline for {code} to compute stop: {df}")

    bars = df.tail(lookback_bars + 1).reset_index(drop=True)  # +1 for prev-close on first TR
    swing_low = bars["low"].iloc[1:].min()

    true_ranges = []
    for i in range(1, len(bars)):
        high, low, prev_close = bars["high"].iloc[i], bars["low"].iloc[i], bars["close"].iloc[i - 1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    atr = sum(true_ranges) / len(true_ranges)

    buffer = buffer_atr_mult * atr
    stop_price = swing_low - buffer

    return {
        "code": code,
        "swing_low": round(float(swing_low), 4),
        "atr": round(float(atr), 4),
        "buffer": round(float(buffer), 4),
        "stop_price": round(float(stop_price), 4),
        "bars_used": len(true_ranges),
    }


def can_trade_today(config=None, today=None):
    """Enforces the alternate-day cadence and per-day trade cap."""
    config = config or load_config()
    today = today or date.today().isoformat()
    state = _load_state()

    todays_trades = [t for t in state["trades"] if t["date"] == today]
    if len(todays_trades) >= config["max_trades_per_trading_day"]:
        return False, f"Already made {len(todays_trades)} trade(s) today (max {config['max_trades_per_trading_day']}/day)."

    if config.get("alternate_day_cadence"):
        trade_dates = sorted({t["date"] for t in state["trades"]}, reverse=True)
        if trade_dates and trade_dates[0] == _previous_trading_day_str(today):
            return False, f"Traded on {trade_dates[0]} -- today is a scheduled off-day so those funds settle."

    return True, "OK to trade."


def _previous_trading_day_str(today_str):
    # Simple calendar-day lookback (not market-holiday-aware) -- good enough
    # given trades are already infrequent under this cadence.
    from datetime import timedelta
    d = datetime.strptime(today_str, "%Y-%m-%d").date()
    return (d - timedelta(days=1)).isoformat()


def record_trade(ticker, risk_dollars, today=None):
    today = today or date.today().isoformat()
    state = _load_state()
    state["trades"].append({"date": today, "ticker": ticker, "risk_dollars": risk_dollars})
    _save_state(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Risk-management check / position sizing")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_size = sub.add_parser("size", help="calculate position size for a trade")
    p_size.add_argument("entry_price", type=float)
    p_size.add_argument("stop_price", type=float)

    sub.add_parser("check", help="can we trade today?")

    p_stop = sub.add_parser("stop", help="calculate structural stop-loss for a ticker")
    p_stop.add_argument("code", help="e.g. US.AAPL")
    p_stop.add_argument("--lookback-bars", type=int, default=20)
    p_stop.add_argument("--buffer-atr-mult", type=float, default=0.5)

    args = parser.parse_args()

    if args.cmd == "size":
        result = calculate_position_size(args.entry_price, args.stop_price)
        print(f"Buy {result['shares']} shares  (cost ${result['cost']}, "
              f"risking ${result['actual_risk_dollars']})")
        print(f"2:1 reward target: ${result['target_price_2r']}")
        if result["capped_by_buying_power"]:
            print("Note: capped by available buying power, not your risk limit.")
    elif args.cmd == "check":
        ok, reason = can_trade_today()
        print(("OK" if ok else "BLOCKED") + f" -- {reason}")
    elif args.cmd == "stop":
        r = calculate_structural_stop(args.code, args.lookback_bars, args.buffer_atr_mult)
        print(f"{r['code']}: swing low ${r['swing_low']}, ATR ${r['atr']} "
              f"(from {r['bars_used']} bars), buffer ${r['buffer']}")
        print(f"Structural stop: ${r['stop_price']}")
