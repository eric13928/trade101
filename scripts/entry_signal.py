"""
Fast (1-minute) entry-signal detection for short (~10 min) day trades.

moomoo's own pattern-shape engine (FLAG etc.) only supports Daily/Hourly bars
-- confirmed unusable for this holding period. So this module detects the
pole -> flag -> breakout structure directly from 1-minute candles ourselves,
and requires a full standard confirmation stack before calling anything a
real signal -- these are not optional extras, they're baseline requirements
for any legitimate pattern read:

  - Volume: the breakout bar must show real relative volume, not thin trading
  - Trend: price must be above its short EMA (not fighting the trend)
  - VWAP: price must be above session VWAP (standard intraday bullish filter)
  - Momentum: MACD line above its signal line (move is accelerating, not stalling)
  - Not overbought: RSI below a ceiling (avoid chasing an already-extended move)
  - Bollinger squeeze/breakout: volatility was recently contracted (a squeeze --
    an independent, statistical version of the same "coiling" idea the pole/
    flag check looks for) and price is now breaking above the upper band
  - Key levels: price above today's pre-market high (and the opening-range
    high, once the first 15 minutes have passed) -- standard day-trading
    reference points our own pole/flag logic doesn't otherwise know about
  - Money flow: buy pressure via get_capital_flow (flow_confirm.py)

A candidate only counts as CONFIRMED if the structure AND every confirmation
check passes. Partial matches are reported but never treated as tradeable.
"""
import argparse
import sys

from moomoo import OpenQuoteContext, RET_OK, KLType, AuType, Session

import flow_confirm

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# All thresholds below are a first-draft default, not tuned/validated against
# real results yet -- expect to adjust after watching this run live.
POLE_LOOKBACK_BARS = 10
POLE_MIN_PCT = 2.0          # pole must be at least a 2% move within the lookback
FLAG_MAX_RETRACE_PCT = 50.0  # flag can't give back more than half the pole's gain
EMA_PERIOD = 9
RVOL_LOOKBACK = 20
RVOL_MIN_MULT = 1.5          # breakout bar must have >=1.5x recent avg volume
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIOD = 14
RSI_OVERBOUGHT_CEILING = 75  # skip if already this extended -- more likely to snap back than continue
BOLL_PERIOD = 20
BOLL_STDDEV_MULT = 2.0
BOLL_SQUEEZE_LOOKBACK = 30    # window to judge whether current bandwidth is unusually narrow
BOLL_SQUEEZE_PERCENTILE = 25  # bandwidth must be in the narrowest 25% of the lookback to count as a squeeze
# MACD/RSI/Bollinger-squeeze-lookback need real warm-up to be meaningful, not
# just the bare minimum bars -- pull extra history for these, structure/volume
# checks still just look at the tail end of the same fetch.
BARS_NEEDED = max(POLE_LOOKBACK_BARS + RVOL_LOOKBACK + 5, (MACD_SLOW + MACD_SIGNAL) * 3,
                  BOLL_PERIOD + BOLL_SQUEEZE_LOOKBACK)


def fetch_bars(code, ctx):
    """Single fetch covering pre-market through now -- extended_time+ALL is a
    superset of the plain regular-session query, so this serves both the
    indicator calculations (which use the regular-session subset, see
    regular_session_bars()) and the premarket/opening-range level check,
    instead of hitting the API twice for the same ticker.

    Does NOT pass an explicit date (see compute_key_levels' note below for
    why): this environment's system clock and moomoo's actual market-data
    timeline disagree, so "today" is derived from the data itself."""
    ret, df, _page_key = ctx.request_history_kline(
        code, start=None, end=None, ktype=KLType.K_1M, autype=AuType.QFQ,
        max_count=1000, extended_time=True, session=Session.ALL,
    )
    if ret != RET_OK or df is None or df.empty:
        return None
    return df.reset_index(drop=True)


def regular_session_bars(full_bars, count=BARS_NEEDED):
    """Filters the full (extended-hours-inclusive) fetch down to the latest
    trading day's regular session (09:30-16:00) -- what indicator math should
    actually be computed on, consistent with the original pre-consolidation
    behavior (a plain, non-extended query only ever returned RTH bars)."""
    df = full_bars.copy()
    df["date_part"] = df["time_key"].str[:10]
    latest_day = df["date_part"].max()
    times = df["time_key"].str[11:19]
    rth = df[(df["date_part"] == latest_day) & (times >= "09:30:00") & (times < "16:00:00")]
    if len(rth) < POLE_LOOKBACK_BARS + 5:
        return None
    return rth.tail(count).reset_index(drop=True)


def compute_ema(closes, period=EMA_PERIOD):
    k = 2 / (period + 1)
    ema = closes.iloc[0]
    for price in closes.iloc[1:]:
        ema = price * k + ema * (1 - k)
    return ema


def _ema_series(closes, period):
    k = 2 / (period + 1)
    out = [closes.iloc[0]]
    for price in closes.iloc[1:]:
        out.append(price * k + out[-1] * (1 - k))
    return out


def compute_macd(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    """Returns (macd_line, signal_line) latest values."""
    import pandas as pd
    ema_fast = pd.Series(_ema_series(closes, fast))
    ema_slow = pd.Series(_ema_series(closes, slow))
    macd_series = ema_fast - ema_slow
    signal_series = pd.Series(_ema_series(macd_series, signal))
    return float(macd_series.iloc[-1]), float(signal_series.iloc[-1])


def compute_rsi(closes, period=RSI_PERIOD):
    deltas = closes.diff().dropna()
    gains = deltas.clip(lower=0)
    losses = -deltas.clip(upper=0)
    avg_gain = gains.rolling(period).mean().iloc[-1]
    avg_loss = losses.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def compute_bollinger_squeeze_breakout(closes, period=BOLL_PERIOD, stddev_mult=BOLL_STDDEV_MULT,
                                        squeeze_lookback=BOLL_SQUEEZE_LOOKBACK,
                                        squeeze_percentile=BOLL_SQUEEZE_PERCENTILE):
    """Independent, statistical version of the same 'coiling before a breakout'
    idea the pole/flag check looks for: was volatility (band width) recently
    unusually narrow, and is price now pushing above the upper band."""
    sma = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    upper = sma + stddev_mult * std
    lower = sma - stddev_mult * std
    bandwidth = (upper - lower) / sma

    recent_bandwidth = bandwidth.iloc[-squeeze_lookback:]
    current_bandwidth = bandwidth.iloc[-1]
    squeeze_threshold = recent_bandwidth.quantile(squeeze_percentile / 100)
    was_squeezed = current_bandwidth <= squeeze_threshold

    current_price = closes.iloc[-1]
    current_upper = upper.iloc[-1]
    breakout = current_price > current_upper

    return {
        "was_squeezed": bool(was_squeezed),
        "breakout": bool(breakout),
        "upper_band": round(float(current_upper), 4),
        "bandwidth": round(float(current_bandwidth), 4),
    }


def compute_vwap(bars):
    """Anchored to whatever bars we have (not necessarily full session open) --
    a reasonable approximation, not exact session VWAP if run mid-day on a
    fresh lookback window."""
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    return (typical * bars["volume"]).sum() / bars["volume"].sum()


def compute_key_levels(full_bars, current_price):
    """Pre-market high and opening-range (first 15 min) high -- standard
    day-trading reference levels our pole/flag logic doesn't otherwise know
    about. Not every candidate will have both available (no pre-market
    volume, or too early in the session for the opening range to exist yet)
    -- in that case the corresponding check is skipped rather than failed,
    since that's missing context, not evidence against the trade.

    Takes the same full_bars fetch_bars() already pulled (extended_time+ALL)
    -- no separate API call. "Today" means the most recent trading day
    present in that data, not the system clock (see fetch_bars' docstring)."""
    df = full_bars
    if df is None or df.empty:
        return {"premarket_high": None, "opening_range_high": None, "level_ok": True, "note": "no data"}

    df = df.copy()
    df["date_part"] = df["time_key"].str[:10]
    latest_day = df["date_part"].max()
    day_bars = df[df["date_part"] == latest_day]

    times = day_bars["time_key"].str[11:19]  # "HH:MM:SS"
    premarket = day_bars[times < "09:30:00"]
    opening_range = day_bars[(times >= "09:30:00") & (times < "09:45:00")]

    premarket_high = float(premarket["high"].max()) if not premarket.empty else None
    opening_range_high = float(opening_range["high"].max()) if not opening_range.empty else None

    checks_available = []
    if premarket_high is not None:
        checks_available.append(current_price > premarket_high)
    if opening_range_high is not None:
        checks_available.append(current_price > opening_range_high)

    level_ok = all(checks_available) if checks_available else True  # nothing to check against -> don't block
    return {
        "premarket_high": premarket_high,
        "opening_range_high": opening_range_high,
        "level_ok": level_ok,
        "note": "ok" if checks_available else "no reference levels available yet",
    }


def detect_pole_flag_breakout(bars):
    """Returns dict describing whether the last bars form pole->flag->breakout,
    using only the shape of the bars -- no volume/trend confirmation here,
    that's layered on separately in check_entry()."""
    pole_window = bars.iloc[-(POLE_LOOKBACK_BARS + 5):-5] if len(bars) >= POLE_LOOKBACK_BARS + 5 else bars
    flag_window = bars.iloc[-5:]

    pole_low = pole_window["low"].min()
    pole_high = pole_window["high"].max()
    pole_pct = (pole_high - pole_low) / pole_low * 100 if pole_low else 0

    flag_high = flag_window["high"].max()
    flag_low = flag_window["low"].min()
    retrace_pct = (pole_high - flag_low) / (pole_high - pole_low) * 100 if pole_high > pole_low else 100

    current_price = bars["close"].iloc[-1]
    breakout = current_price > flag_high

    return {
        "pole_pct": round(float(pole_pct), 2),
        "pole_ok": pole_pct >= POLE_MIN_PCT,
        "retrace_pct": round(float(retrace_pct), 2),
        "flag_ok": retrace_pct <= FLAG_MAX_RETRACE_PCT,
        "flag_high": round(float(flag_high), 4),
        "current_price": round(float(current_price), 4),
        "breakout": bool(breakout),
    }


def check_entry(code, ctx=None):
    """Full check: structure, volume, trend, VWAP, momentum, not-overbought,
    Bollinger squeeze/breakout, key levels, money flow -- 9 checks total.
    Returns a dict with every individual check's result plus an overall
    'confirmed' bool that's only True if ALL of them pass."""
    owns_ctx = ctx is None
    if owns_ctx:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

    full_bars = fetch_bars(code, ctx)
    bars = regular_session_bars(full_bars) if full_bars is not None else None
    if bars is None:
        if owns_ctx:
            ctx.close()
        return {"code": code, "confirmed": False, "reason": "not enough 1-min bar history available"}

    structure = detect_pole_flag_breakout(bars)

    ema = compute_ema(bars["close"], EMA_PERIOD)
    trend_ok = structure["current_price"] > ema

    vwap = compute_vwap(bars)
    vwap_ok = structure["current_price"] > vwap

    recent_vol = bars["volume"].iloc[-1]
    avg_vol = bars["volume"].iloc[-(RVOL_LOOKBACK + 1):-1].mean()
    rvol = recent_vol / avg_vol if avg_vol else 0
    volume_ok = rvol >= RVOL_MIN_MULT

    macd_line, macd_signal = compute_macd(bars["close"])
    momentum_ok = macd_line > macd_signal

    rsi = compute_rsi(bars["close"])
    not_overbought_ok = rsi < RSI_OVERBOUGHT_CEILING

    boll = compute_bollinger_squeeze_breakout(bars["close"])
    bollinger_ok = boll["was_squeezed"] and boll["breakout"]

    levels = compute_key_levels(full_bars, structure["current_price"])
    levels_ok = levels["level_ok"]

    flow = flow_confirm.flow_direction(code, ctx=ctx)
    flow_ok = flow.get("ok") and flow["direction"] == "inflow"

    if owns_ctx:
        ctx.close()

    checks = {
        "structure": structure["pole_ok"] and structure["flag_ok"] and structure["breakout"],
        "volume": volume_ok,
        "trend": trend_ok,
        "vwap": vwap_ok,
        "momentum": momentum_ok,
        "not_overbought": not_overbought_ok,
        "bollinger": bollinger_ok,
        "levels": levels_ok,
        "flow": flow_ok,
    }

    return {
        "code": code,
        "confirmed": all(checks.values()),
        "checks": checks,
        "detail": {
            "pole_pct": structure["pole_pct"],
            "retrace_pct": structure["retrace_pct"],
            "breakout": structure["breakout"],
            "current_price": structure["current_price"],
            "flag_high": structure["flag_high"],
            "ema9": round(float(ema), 4),
            "vwap": round(float(vwap), 4),
            "rvol": round(float(rvol), 2),
            "macd": round(macd_line, 4),
            "macd_signal": round(macd_signal, 4),
            "rsi": round(rsi, 1),
            "boll_upper": boll["upper_band"],
            "boll_squeezed": boll["was_squeezed"],
            "premarket_high": levels["premarket_high"],
            "opening_range_high": levels["opening_range_high"],
            "flow_direction": flow.get("direction", "n/a"),
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check 1-minute entry signal for a ticker")
    parser.add_argument("code", help="e.g. US.AAPL")
    args = parser.parse_args()

    result = check_entry(args.code)
    print(f"{result['code']}: {'CONFIRMED' if result['confirmed'] else 'not confirmed'}")
    if "checks" in result:
        for name, passed in result["checks"].items():
            print(f"  [{'x' if passed else ' '}] {name}")
        d = result["detail"]
        print(f"  pole={d['pole_pct']}%  retrace={d['retrace_pct']}%  breakout={d['breakout']}  "
              f"price={d['current_price']}  flag_high={d['flag_high']}")
        print(f"  EMA9={d['ema9']}  VWAP={d['vwap']}  RVOL={d['rvol']}x")
        print(f"  MACD={d['macd']}  MACD_signal={d['macd_signal']}  RSI={d['rsi']}  flow={d['flow_direction']}")
        print(f"  BollUpper={d['boll_upper']}  squeezed={d['boll_squeezed']}  "
              f"premarket_high={d['premarket_high']}  opening_range_high={d['opening_range_high']}")
    else:
        print(f"  {result.get('reason')}")
