"""
Fast (1-minute) entry-signal detection for short (~10 min) day trades.

moomoo's own pattern-shape engine (FLAG etc.) only supports Daily/Hourly bars
-- confirmed unusable for this holding period. So this module detects the
pole -> flag -> breakout structure directly from 1-minute candles ourselves.

REQUIRED (all 6 must pass for CONFIRMED) -- each measures something
genuinely independent, not a restatement of another check:

  - Structure (incl. false-breakout guard): pole -> flag -> breakout that
    holds for 2 consecutive closed bars, not just one -- a single-bar spike
    that immediately reverses (a real, common failure mode -- see RRR from
    earlier testing) no longer counts as confirmed
  - Volume: the breakout bar must show real relative volume, not thin trading
  - VWAP: price must be above session VWAP (the single most-watched
    intraday reference level for professional day traders)
  - Key levels: price above today's pre-market high (and the opening-range
    high, once the first 15 minutes have passed) -- standard day-trading
    reference points our own pole/flag logic doesn't otherwise know about
  - Volume profile: no disproportionately heavy volume-at-price "wall" sitting
    just above current price -- a High Volume Node close overhead means real
    prior supply likely to reject the breakout, a different signal from RVOL
    (which is time-based -- "is trading active right now" -- not price-based)
  - Money flow: buy pressure via get_capital_flow (flow_confirm.py) -- the
    only check based on actual buy/sell aggression, not derived from price

INFORMATIONAL ONLY (computed, shown, but never block a signal) -- found to
be either redundant with the required checks or working against the
strategy's own premise:

  - Trend (EMA9) and Momentum (MACD): both near-guaranteed to already be
    true if structure+volume confirm a real breakout -- not independent
    evidence, just derived restatements of the same price action
  - Bollinger squeeze/breakout: measures nearly the same "coiled ->
    expanding" idea as structure, via different math -- a second proof of
    the same thing, not new information (confirmed live: on a real test
    run, structure and bollinger failed on 100% of candidates together,
    exactly as expected from two checks measuring the same phenomenon)
  - Not overbought (RSI): a mean-reversion concept that actively fights a
    momentum/breakout strategy -- the strongest breakouts are often already
    overbought, so filtering them out works against the strategy's premise
    rather than protecting it
  - Recent change (adaptive 1-3 min window): distinguishes a move that's
    STILL happening right now from one that already happened and went flat
    -- a real gap in pole_pct, which only sees the 10-min high/low range and
    can't tell whether the move was 9 minutes ago or 30 seconds ago.
    Informational for now since it's newly added and not yet validated
    against live data -- candidate for promotion to required once observed.

A candidate only counts as CONFIRMED if all 6 REQUIRED checks pass.
Informational checks are always reported for context but never gate.
"""
import argparse
import sys

import pandas as pd
from moomoo import OpenQuoteContext, RET_OK, KLType, AuType, Session, SubType

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
FLAG_WINDOW_BARS = 20        # widened from 5 -- gap-driven catalysts (earnings pops at the open) often
                             # consolidate for 20-30 min, not 5; a 5-min window was too narrow to see
                             # the real flag shape, catching an arbitrary recent slice instead
FLAG_MAX_RETRACE_PCT = 65.0  # loosened from 50 -- a longer flag window naturally has more room to
                             # wobble before it's genuinely broken, not reversing
BREAKOUT_SUSTAIN_BARS = 2    # breakout must hold for this many consecutive closed bars, not just one (false-breakout guard)
EMA_PERIOD = 9
RVOL_MIN_MULT = 1.5          # breakout bar must have >=1.5x moomoo's own volume_ratio baseline
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIOD = 14
RSI_OVERBOUGHT_CEILING = 75  # skip if already this extended -- more likely to snap back than continue
BOLL_PERIOD = 20
BOLL_STDDEV_MULT = 2.0
BOLL_SQUEEZE_LOOKBACK = 30    # window to judge whether current bandwidth is unusually narrow
BOLL_SQUEEZE_PERCENTILE = 25  # bandwidth must be in the narrowest 25% of the lookback to count as a squeeze
VOLUME_PROFILE_BINS = 20
OVERHEAD_ZONE_PCT = 2.0    # check for a volume "wall" within this % above current price
OVERHEAD_HVN_MULT = 1.5    # a bin counts as a wall if its volume exceeds this multiple of the average bin
RECENT_CHANGE_TARGET_BARS = 3  # distinguishes "still actively moving" from "moved earlier, now flat" --
                               # a real gap in pole_pct, which only sees the 10-min high/low range and
                               # can't tell whether the move happened just now or 9 minutes ago
# MACD/RSI/Bollinger-squeeze-lookback need real warm-up to be meaningful, not
# just the bare minimum bars -- pull extra history for these, structure/volume
# checks still just look at the tail end of the same fetch.
BARS_NEEDED = max(POLE_LOOKBACK_BARS + 5, (MACD_SLOW + MACD_SIGNAL) * 3,
                  BOLL_PERIOD + BOLL_SQUEEZE_LOOKBACK)

# Regular-session time windows per market, in each exchange's own local time
# (bar timestamps come back already in local exchange time). HK has a lunch
# break (no trading 12:00-13:00) -- getting this wrong would silently mix
# lunch-break gap artifacts into every indicator calculation, so sessions are
# a list of (start, end) segments, not a single start/end pair.
MARKET_SESSIONS = {
    "US": {"regular": [("09:30:00", "16:00:00")], "opening_range_end": "09:45:00"},
    "HK": {"regular": [("09:30:00", "12:00:00"), ("13:00:00", "16:00:00")], "opening_range_end": "09:45:00"},
}


def fetch_bars(code, ctx):
    """Uses moomoo's LIVE subscription-based kline (subscribe + get_cur_kline)
    -- NOT request_history_kline, which was confirmed via live testing to
    return stale, disconnected data (~1 year behind real-time) in this
    environment, while get_cur_kline (after subscribing) matches genuine
    live snapshot prices. This was a serious bug: every indicator in this
    module had been evaluating stale data regardless of what time of day
    was scanned -- explains why nothing ever confirmed in earlier testing.

    subscribe() is idempotent (safe to call even if already subscribed,
    won't double-charge quota) and backfills the current session's bars,
    not just future ticks from the moment of subscribing. Extended-hours
    (pre-market) is only supported for US tickers per moomoo's own SDK docs
    ("only for subscribing US stocks") -- HK gets regular-session-only live
    data, consistent with what we already knew about HK's session model."""
    market = code.split(".")[0] if "." in code else "US"
    if market == "US":
        ret, err = ctx.subscribe([code], [SubType.K_1M], extended_time=True, session=Session.ALL)
    else:
        ret, err = ctx.subscribe([code], [SubType.K_1M])
    if ret != RET_OK:
        return None

    ret, df = ctx.get_cur_kline(code, 1000, KLType.K_1M, AuType.QFQ)
    if ret != RET_OK or df is None or df.empty:
        return None
    return df.reset_index(drop=True)


def select_trading_day(full_bars, market="US", min_bars=POLE_LOOKBACK_BARS + FLAG_WINDOW_BARS):
    """Picks the most recent trading day that actually has enough
    regular-session bars -- not just the latest calendar date, which can be
    sparse/partial (confirmed real case: a simulated-data quirk left one HK
    trading day with only 7 bars covering 09:30-09:36). Shared by
    regular_session_bars() and compute_key_levels() so both always agree on
    which day they're analyzing -- calling this independently in each would
    risk them silently picking different days from the same fetch."""
    segments = MARKET_SESSIONS[market]["regular"]
    df = full_bars.copy()
    df["date_part"] = df["time_key"].str[:10]
    times = df["time_key"].str[11:19]
    in_session = pd.Series(False, index=df.index)
    for start, end in segments:
        in_session |= (times >= start) & (times < end)
    rth_only = df[in_session]

    for day in sorted(rth_only["date_part"].unique(), reverse=True):
        if len(rth_only[rth_only["date_part"] == day]) >= min_bars:
            return day
    return None


def regular_session_bars(full_bars, market="US", count=BARS_NEEDED, trading_day=None):
    """Filters the full (extended-hours-inclusive) fetch down to one trading
    day's regular session -- what indicator math should actually be computed
    on. Market-aware: HK's session is two segments (excludes the 12:00-13:00
    lunch break), not one continuous block like the US."""
    segments = MARKET_SESSIONS[market]["regular"]
    if trading_day is None:
        trading_day = select_trading_day(full_bars, market)
    if trading_day is None:
        return None

    df = full_bars.copy()
    df["date_part"] = df["time_key"].str[:10]
    times = df["time_key"].str[11:19]
    in_session = pd.Series(False, index=df.index)
    for start, end in segments:
        in_session |= (times >= start) & (times < end)
    rth = df[(df["date_part"] == trading_day) & in_session]
    if len(rth) < POLE_LOOKBACK_BARS + FLAG_WINDOW_BARS:
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


def compute_recent_change(bars, target_bars=RECENT_CHANGE_TARGET_BARS):
    """Adaptive short-term price change: uses up to target_bars of lookback,
    but reports whatever window is actually available rather than hiding
    real data behind a binary available/unavailable gate. Returns None only
    when there's truly nothing to compare against (fewer than 2 bars total)
    -- otherwise always a real, honestly-labeled number, e.g. a '1-min
    change' at minute 1 growing to the full target_bars once enough
    history exists."""
    if len(bars) < 2:
        return None
    actual_bars = min(target_bars, len(bars) - 1)
    current_price = bars["close"].iloc[-1]
    past_price = bars["close"].iloc[-(actual_bars + 1)]
    change_pct = (current_price - past_price) / past_price * 100 if past_price else 0
    return {
        "change_pct": round(float(change_pct), 2),
        "window_bars": actual_bars,
    }


def compute_volume_profile(bars, num_bins=VOLUME_PROFILE_BINS):
    """Approximate volume-at-price profile from OHLCV bars -- each bar's
    volume spread evenly across the price bins it spans. NOT true tick-level
    volume profile (that needs individual trade prices, which we don't pull),
    but a standard, reasonable approximation when tick data isn't available.
    Also limited to whatever lookback window `bars` covers (roughly the last
    1-2 hours of 1-min bars), not a full session or multi-day profile."""
    price_min = float(bars["low"].min())
    price_max = float(bars["high"].max())
    if price_max <= price_min:
        return None
    bin_edges = [price_min + i * (price_max - price_min) / num_bins for i in range(num_bins + 1)]
    bin_volume = [0.0] * num_bins

    def bin_index(price):
        idx = int((price - price_min) / (price_max - price_min) * num_bins)
        return min(max(idx, 0), num_bins - 1)

    for _, bar in bars.iterrows():
        low, high, vol = bar["low"], bar["high"], bar["volume"]
        lo_idx, hi_idx = bin_index(low), bin_index(high)
        span = hi_idx - lo_idx + 1
        for i in range(lo_idx, hi_idx + 1):
            bin_volume[i] += vol / span

    poc_idx = bin_volume.index(max(bin_volume))
    poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2
    avg_bin_volume = sum(bin_volume) / num_bins

    return {
        "bin_edges": bin_edges,
        "bin_volume": bin_volume,
        "poc_price": round(float(poc_price), 4),
        "avg_bin_volume": avg_bin_volume,
        "bin_index": bin_index,
    }


def check_overhead_resistance(profile, current_price, zone_pct=OVERHEAD_ZONE_PCT, hvn_mult=OVERHEAD_HVN_MULT):
    """Is there a disproportionately heavy volume 'wall' in the price zone
    just above current price -- real prior supply likely to reject a
    breakout, distinct from RVOL (which only measures activity over time,
    not concentration at a specific price)."""
    if profile is None:
        return {"clear": True, "note": "no profile data"}
    zone_top = current_price * (1 + zone_pct / 100)
    idx_fn = profile["bin_index"]
    lo_idx, hi_idx = idx_fn(current_price), idx_fn(zone_top)
    zone_bins = profile["bin_volume"][lo_idx:hi_idx + 1]
    if not zone_bins:
        return {"clear": True, "note": "no bins in zone"}
    max_zone_volume = max(zone_bins)
    is_wall = max_zone_volume > profile["avg_bin_volume"] * hvn_mult
    return {
        "clear": not is_wall,
        "max_zone_volume": round(max_zone_volume, 1),
        "avg_bin_volume": round(profile["avg_bin_volume"], 1),
    }


def fetch_volume_ratio(code, ctx):
    """Relative volume, sourced from moomoo's own server-side snapshot field
    (volume_ratio) instead of a hand-rolled bar comparison. Verified live:
    this is a genuine time-of-day-aware ratio (moomoo/futu's standard
    "volume ratio" concept, matching values shown in their own app) computed
    against moomoo's own historical data -- not the last-bar-vs-recent-20-bar
    comparison this used to do, which was unreliable because the "last bar"
    is often still mid-formation (partial volume) when the check runs.
    Returns None if the snapshot call fails or the field is missing."""
    ret, snap = ctx.get_market_snapshot([code])
    if ret != RET_OK or snap is None or snap.empty:
        return None
    value = snap.iloc[0].get("volume_ratio")
    return float(value) if value is not None and value == value else None  # NaN check


def compute_vwap(bars):
    """Anchored to whatever bars we have (not necessarily full session open) --
    a reasonable approximation, not exact session VWAP if run mid-day on a
    fresh lookback window. Returns None (not NaN) if the window has zero
    total volume -- happens on illiquid/halted names -- so the caller can
    fail the check explicitly instead of silently comparing against NaN."""
    total_volume = bars["volume"].sum()
    if not total_volume:
        return None
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    return (typical * bars["volume"]).sum() / total_volume


def compute_key_levels(full_bars, current_price, market="US", trading_day=None):
    """Pre-market high and opening-range (first 15 min) high -- standard
    day-trading reference levels our pole/flag logic doesn't otherwise know
    about. Not every candidate will have both available (no pre-market
    volume, or too early in the session for the opening range to exist yet)
    -- in that case the corresponding check is skipped rather than failed,
    since that's missing context, not evidence against the trade.

    Takes the same full_bars fetch_bars() already pulled (extended_time+ALL)
    -- no separate API call. trading_day should be passed in from the same
    select_trading_day() call regular_session_bars() used, so both agree on
    which day they're analyzing rather than each independently guessing
    (and potentially disagreeing) via the naive "latest calendar date"."""
    df = full_bars
    if df is None or df.empty:
        return {"premarket_high": None, "opening_range_high": None, "level_ok": True, "note": "no data"}

    if trading_day is None:
        trading_day = select_trading_day(full_bars, market)
    if trading_day is None:
        return {"premarket_high": None, "opening_range_high": None, "level_ok": True, "note": "no valid trading day found"}

    session_open = MARKET_SESSIONS[market]["regular"][0][0]
    opening_range_end = MARKET_SESSIONS[market]["opening_range_end"]

    df = df.copy()
    df["date_part"] = df["time_key"].str[:10]
    day_bars = df[df["date_part"] == trading_day]

    times = day_bars["time_key"].str[11:19]  # "HH:MM:SS"
    premarket = day_bars[times < session_open]
    opening_range = day_bars[(times >= session_open) & (times < opening_range_end)]

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
    that's layered on separately in check_entry().

    False-breakout guard: flag_high is computed from a window that EXCLUDES
    the most recent BREAKOUT_SUSTAIN_BARS bars, and breakout only counts as
    confirmed if ALL of those recent bars closed above it -- not just the
    single latest one. A one-bar spike above the level that immediately
    reverses on the next bar (a real, common failure mode -- see the RRR
    example from earlier testing) no longer counts as a breakout."""
    sustain_bars = bars.iloc[-BREAKOUT_SUSTAIN_BARS:]
    flag_window = bars.iloc[-(FLAG_WINDOW_BARS + BREAKOUT_SUSTAIN_BARS):-BREAKOUT_SUSTAIN_BARS]
    pole_start = POLE_LOOKBACK_BARS + FLAG_WINDOW_BARS + BREAKOUT_SUSTAIN_BARS
    pole_window = bars.iloc[-pole_start:-(FLAG_WINDOW_BARS + BREAKOUT_SUSTAIN_BARS)] if len(bars) >= pole_start else bars

    pole_low = pole_window["low"].min()
    pole_high = pole_window["high"].max()
    pole_pct = (pole_high - pole_low) / pole_low * 100 if pole_low else 0

    flag_high = flag_window["high"].max()
    flag_low = flag_window["low"].min()
    retrace_pct = (pole_high - flag_low) / (pole_high - pole_low) * 100 if pole_high > pole_low else 100

    current_price = bars["close"].iloc[-1]
    breakout_sustained = bool((sustain_bars["close"] > flag_high).all())

    return {
        "pole_pct": round(float(pole_pct), 2),
        "pole_ok": pole_pct >= POLE_MIN_PCT,
        "retrace_pct": round(float(retrace_pct), 2),
        "flag_ok": retrace_pct <= FLAG_MAX_RETRACE_PCT,
        "flag_high": round(float(flag_high), 4),
        "current_price": round(float(current_price), 4),
        "breakout": breakout_sustained,
    }


def check_entry(code, ctx=None):
    """6 REQUIRED checks: structure (incl. false-breakout guard), volume,
    VWAP, key levels, volume profile (overhead resistance), money flow --
    each measures something genuinely independent. 'confirmed' is True only
    if all 6 pass. Plus 4 INFORMATIONAL checks (trend, momentum,
    not-overbought, Bollinger) computed and returned for context but not
    required -- they were found to be either redundant with the required
    checks or, in RSI's case, actively working against a momentum/breakout
    strategy. See the inline comments above required_checks/
    informational_checks in this function for the full reasoning.

    Market (session timing) is derived from the ticker's own prefix
    (US.xxx / HK.xxx), not passed separately -- avoids any risk of the
    ticker and its session config getting out of sync."""
    market = code.split(".")[0] if "." in code else "US"
    if market not in MARKET_SESSIONS:
        market = "US"

    owns_ctx = ctx is None
    if owns_ctx:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

    full_bars = fetch_bars(code, ctx)
    trading_day = select_trading_day(full_bars, market) if full_bars is not None else None
    bars = regular_session_bars(full_bars, market=market, trading_day=trading_day) if full_bars is not None else None
    if bars is None:
        if owns_ctx:
            ctx.close()
        return {"code": code, "confirmed": False, "reason": "not enough 1-min bar history available"}

    structure = detect_pole_flag_breakout(bars)

    ema = compute_ema(bars["close"], EMA_PERIOD)
    trend_ok = structure["current_price"] > ema

    vwap = compute_vwap(bars)
    vwap_ok = vwap is not None and structure["current_price"] > vwap

    rvol = fetch_volume_ratio(code, ctx)
    volume_ok = rvol is not None and rvol >= RVOL_MIN_MULT

    macd_line, macd_signal = compute_macd(bars["close"])
    momentum_ok = macd_line > macd_signal

    rsi = compute_rsi(bars["close"])
    not_overbought_ok = rsi < RSI_OVERBOUGHT_CEILING

    boll = compute_bollinger_squeeze_breakout(bars["close"])
    bollinger_ok = boll["was_squeezed"] and boll["breakout"]

    recent_change = compute_recent_change(bars)
    recent_change_ok = recent_change is not None and recent_change["change_pct"] > 0

    levels = compute_key_levels(full_bars, structure["current_price"], market=market, trading_day=trading_day)
    levels_ok = levels["level_ok"]

    profile = compute_volume_profile(bars)
    overhead = check_overhead_resistance(profile, structure["current_price"])
    overhead_ok = overhead["clear"]

    flow = flow_confirm.flow_direction(code, ctx=ctx)
    flow_ok = flow.get("ok") and flow["direction"] == "inflow"

    if owns_ctx:
        ctx.close()

    # Required: each measures something genuinely independent (setup shape,
    # real participation, the most-watched intraday level, prior overhead
    # supply, and actual buy/sell aggression). All must pass.
    required_checks = {
        "structure": structure["pole_ok"] and structure["flag_ok"] and structure["breakout"],
        "volume": volume_ok,
        "vwap": vwap_ok,
        "levels": levels_ok,
        "volume_profile": overhead_ok,
        "flow": flow_ok,
    }
    # Informational only, NOT required -- kept for context, but don't block:
    # - trend (EMA9) and momentum (MACD) are near-redundant with structure/volume
    #   (a real breakout is already almost guaranteed to satisfy both)
    # - bollinger squeeze/breakout measures nearly the same "coiled -> expanding"
    #   idea as structure, just via a different formula -- a second proof of the
    #   same thing, not independent evidence
    # - not_overbought (RSI) is a mean-reversion concept that actively fights a
    #   momentum/breakout strategy -- the strongest breakouts are often already
    #   overbought, so filtering them out works against the strategy's own premise
    informational_checks = {
        "trend": trend_ok,
        "momentum": momentum_ok,
        "not_overbought": not_overbought_ok,
        "bollinger": bollinger_ok,
        "recent_change": recent_change_ok,
    }
    checks = {**required_checks, **informational_checks}

    return {
        "code": code,
        "confirmed": all(required_checks.values()),
        "checks": checks,
        "required_checks": required_checks,
        "informational_checks": informational_checks,
        "detail": {
            "pole_pct": structure["pole_pct"],
            "retrace_pct": structure["retrace_pct"],
            "breakout": structure["breakout"],
            "current_price": structure["current_price"],
            "flag_high": structure["flag_high"],
            "ema9": round(float(ema), 4),
            "vwap": round(float(vwap), 4) if vwap is not None else None,
            "rvol": round(rvol, 2) if rvol is not None else None,
            "macd": round(macd_line, 4),
            "macd_signal": round(macd_signal, 4),
            "rsi": round(rsi, 1),
            "boll_upper": boll["upper_band"],
            "boll_squeezed": boll["was_squeezed"],
            "premarket_high": levels["premarket_high"],
            "opening_range_high": levels["opening_range_high"],
            "poc_price": profile["poc_price"] if profile else None,
            "overhead_max_zone_volume": overhead.get("max_zone_volume"),
            "overhead_avg_bin_volume": overhead.get("avg_bin_volume"),
            "flow_direction": flow.get("direction", "n/a"),
            "recent_change_pct": recent_change["change_pct"] if recent_change else None,
            "recent_change_window_min": recent_change["window_bars"] if recent_change else None,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check 1-minute entry signal for a ticker")
    parser.add_argument("code", help="e.g. US.AAPL")
    args = parser.parse_args()

    result = check_entry(args.code)
    print(f"{result['code']}: {'CONFIRMED' if result['confirmed'] else 'not confirmed'}")
    if "checks" in result:
        print("  Required:")
        for name, passed in result["required_checks"].items():
            print(f"    [{'x' if passed else ' '}] {name}")
        print("  Informational only (not required):")
        for name, passed in result["informational_checks"].items():
            print(f"    [{'x' if passed else ' '}] {name}")
        d = result["detail"]
        print(f"  pole={d['pole_pct']}%  retrace={d['retrace_pct']}%  breakout={d['breakout']}  "
              f"price={d['current_price']}  flag_high={d['flag_high']}")
        print(f"  EMA9={d['ema9']}  VWAP={d['vwap']}  RVOL={d['rvol']}x")
        print(f"  MACD={d['macd']}  MACD_signal={d['macd_signal']}  RSI={d['rsi']}  flow={d['flow_direction']}")
        print(f"  BollUpper={d['boll_upper']}  squeezed={d['boll_squeezed']}  "
              f"premarket_high={d['premarket_high']}  opening_range_high={d['opening_range_high']}")
        print(f"  POC={d['poc_price']}  overhead_max_zone_vol={d['overhead_max_zone_volume']}  "
              f"avg_bin_vol={d['overhead_avg_bin_volume']}")
        print(f"  RecentChange({d['recent_change_window_min']}min)={d['recent_change_pct']}%")
    else:
        print(f"  {result.get('reason')}")
