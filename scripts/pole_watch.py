"""
Mid/large-cap pole-stage alert + live analysis.

Different philosophy from monitor.py: that system waits for full 6-check
confirmation (a real, sustained breakout) before ever alerting -- safe, but
by definition late. This one alerts the moment a pole forms (the early,
initial move), before any flag or breakout has happened, specifically so
the entry isn't a chase. There is no auto-confirm here -- once alerted, the
point is to look at the live indicator picture together and make the call,
not wait for a checklist to turn green on its own.

Candidates: mid/large-cap US stocks -- a real $2B+ market-cap floor, AND
a $10-$50 price band -- with a genuine catalyst (earnings beat or analyst
rating upgrade). The price band was added after RGA (a real $2B+ company)
showed up at $245/share, needing ~$8-10k to size a single position
properly -- more than the account this is built around. Market cap alone
doesn't keep share price in a tradeable range, so both filters apply
together. Same catalyst-first theme as the rest of this project, just
recalibrated: large-cap moves are much smaller than the small-cap system
was built for, so the pole threshold here is far lower than
entry_signal.py's 2% default -- starting at 0.5%, a first guess that will
likely need adjusting once we see real data, same as every other threshold
in this project so far.

Runs on 3-minute bars, not entry_signal.py's default 1-minute -- large/mid
caps move slower and cleaner than the small caps that system was tuned
for, and 1-minute noise was visibly showing up here (RSI/MACD flipping
within minutes on real live data). The existing window sizes (10-bar pole
lookback, 9-period EMA, etc.) are unchanged and now just cover 3x the
calendar time, which fits the slower pace without needing new numbers.

Runs in bounded chunks, loopable the same way as monitor.py and
premarket_scan.py ("run the scan and repeat after its done").
"""
import argparse
import json
import os
import sys
import time

from moomoo import OpenQuoteContext, RET_OK

import entry_signal
import news_scanner
import risk_manager

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ALERTED_FILE = os.path.join(os.path.dirname(__file__), "data", "pole_watch_alerted.json")
CHECK_PACING_SECONDS = 1.5
CANDIDATE_REFRESH_SECONDS = 300

MIN_MARKET_CAP = 2_000_000_000  # $2B -- mid-cap floor and up (not a price band, an actual cap check)
POLE_MIN_PCT_LARGECAP = 0.5     # first guess, much lower than entry_signal's 2% small-cap default -- expect to tune
MIN_PRICE = 10.0   # same $10-$50 band as premarket_scan.py -- keeps share prices affordable enough to
MAX_PRICE = 50.0   # size a real position without needing $5k+ per trade (RGA at $245/share needed ~$8-10k)


def load_alerted():
    if os.path.exists(ALERTED_FILE):
        with open(ALERTED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)  # ticker -> window_key of the pole last alerted
    return {}


def save_alerted(alerted):
    os.makedirs(os.path.dirname(ALERTED_FILE), exist_ok=True)
    with open(ALERTED_FILE, "w", encoding="utf-8") as f:
        json.dump(alerted, f)


def get_midlargecap_candidates(market, ctx):
    """Earnings beats + rating upgrades, same as the small-cap system, but
    filtered by real market cap instead of a price band -- $2-$15 was a
    price filter, not a market-cap one, and a cheap share price doesn't
    mean a small company (or vice versa)."""
    earnings_hits = news_scanner.get_earnings_beats(ctx, market=market)
    rating_hits = news_scanner.get_rating_upgrades(ctx, market=market)

    candidates = {}
    for e in earnings_hits:
        candidates[e["ticker"]] = f"earnings beat ({e['beat_ratio']:.1f}% beat, {e['earning_day_chg']:+.1f}% day)"
    for r in rating_hits:
        candidates.setdefault(
            r["ticker"],
            f"analyst upgrade ({r['institution']}: {r['last_rating']} -> {r['rating']}, target ${r['target_price']})"
        )
    candidates = {k: v for k, v in candidates.items() if k.startswith(f"{market}.")}
    if not candidates:
        return candidates

    codes = list(candidates.keys())
    ret, snap = ctx.get_market_snapshot(codes)
    cap_by_code = {}
    price_by_code = {}
    if ret == RET_OK and snap is not None and not snap.empty:
        for _, row in snap.iterrows():
            code = row.get("code")
            cap_by_code[code] = row.get("total_market_val")
            price_by_code[code] = row.get("last_price")

    # total_market_val and last_price both come back in the ticker's own local
    # currency (HKD for HK.*, not USD) -- MIN_MARKET_CAP/MIN_PRICE/MAX_PRICE
    # are USD figures, so convert before comparing, same fix as the
    # price-band bug found earlier.
    local_min_cap = risk_manager.usd_to_local(MIN_MARKET_CAP, market)
    local_min_price = risk_manager.usd_to_local(MIN_PRICE, market)
    local_max_price = risk_manager.usd_to_local(MAX_PRICE, market)

    filtered = {}
    for code, reason in candidates.items():
        cap = cap_by_code.get(code)
        price = price_by_code.get(code)
        if cap is None or cap != cap:  # drop NaN
            continue
        if cap < local_min_cap:
            continue
        if price is None or price < local_min_price or price > local_max_price:
            continue
        filtered[code] = reason
    return filtered


def compute_pole_analysis(ticker, reason, ctx):
    """The 'we discuss it together' piece -- pulls the live indicator
    picture and builds a plain-language synthesis, not just raw numbers.
    Returns {'pole_pct': ..., 'text': ...} or None if no pole (or not
    enough data) right now. Does NOT print -- the caller decides whether
    this is worth alerting on (dedup) before printing anything."""
    full_bars = entry_signal.fetch_bars(ticker, ctx, granularity="3M")
    if full_bars is None:
        return None
    trading_day = entry_signal.select_trading_day(full_bars, "US")
    bars = entry_signal.regular_session_bars(full_bars, market="US", trading_day=trading_day)
    if bars is None:
        return None

    structure = entry_signal.detect_pole_flag_breakout(bars, pole_min_pct=POLE_MIN_PCT_LARGECAP)
    if not structure["pole_ok"]:
        return None

    ema = entry_signal.compute_ema(bars["close"])
    vwap = entry_signal.compute_vwap(bars)
    macd_line, macd_signal = entry_signal.compute_macd(bars["close"])
    rsi = entry_signal.compute_rsi(bars["close"])
    rvol = entry_signal.fetch_volume_ratio(ticker, ctx)

    name = ticker
    ret, snap = ctx.get_market_snapshot([ticker])
    if ret == RET_OK and snap is not None and not snap.empty:
        name = snap.iloc[0].get("name") or ticker

    price = structure["current_price"]
    above_vwap = vwap is not None and price > vwap
    above_ema = price > ema
    macd_rising = macd_line > macd_signal

    lean = "leans bullish" if sum([above_vwap, above_ema, macd_rising, rsi < 70]) >= 3 else \
           "mixed signals" if sum([above_vwap, above_ema, macd_rising]) >= 1 else "leans weak"

    # Objective, threshold-based cautions -- explicitly tagged so they're never
    # buried in the raw numbers. RSI/RVOL thresholds match what's been used all
    # day when talking through these alerts (overbought >70, thin volume <1x).
    cautions = []
    if rsi >= 70:
        cautions.append(f"RSI {round(rsi, 1)} is overbought -- stretched, more prone to pulling back")
    elif rsi <= 30:
        cautions.append(f"RSI {round(rsi, 1)} is oversold")
    if rvol is not None and rvol < 1.0:
        cautions.append(f"RVOL {round(rvol, 2)}x is BELOW average -- move isn't backed by real volume")
    if macd_line < 0:
        cautions.append(f"MACD is negative ({round(macd_line, 4)}) -- actual negative momentum, not just cooling")

    lines = [
        "\n" + "-" * 70,
        f">>> POLE FORMED: {ticker} ({name}) ({lean}) <<<",
        f"  catalyst: {reason}",
        f"  as of {structure['current_time']}: price={price}  watch level (flag high)={structure['flag_high']}",
        f"  pole: {structure['pole_pct']}%  from ${structure['pole_low']} @ {structure['pole_low_time']}  "
        f"to ${structure['pole_high']} @ {structure['pole_high_time']}",
        f"  VWAP={round(vwap, 4) if vwap is not None else 'n/a'} ({'above' if above_vwap else 'below'})  "
        f"EMA9={round(float(ema), 4)} ({'above' if above_ema else 'below'})",
        f"  MACD={round(macd_line, 4)} vs signal={round(macd_signal, 4)} ({'rising' if macd_rising else 'falling'})  "
        f"RSI={round(rsi, 1)}  RVOL={round(rvol, 2) if rvol is not None else 'n/a'}x",
    ] + [f"  ⚠ CAUTION: {c}" for c in cautions] + [
        "  ^ no auto-confirmation -- this is early-stage, for us to look at together, not a signal to act on alone",
        "-" * 70,
    ]
    # Identifies the specific bars defining THIS pole, not just its size --
    # dedup keys off this instead of pole_pct growth, so a second, distinct
    # leg (failed first pole, pullback, then a fresh move of similar or even
    # smaller size) still alerts instead of being silently swallowed by a
    # "must be 50% bigger than last time" rule that only made sense for the
    # same move continuing to extend.
    window_key = f"{structure['pole_low_time']}|{structure['pole_high_time']}"
    return {"pole_pct": structure["pole_pct"], "window_key": window_key, "text": "\n".join(lines)}


def run_pole_watch(market="US", duration_seconds=120, check_pacing=CHECK_PACING_SECONDS,
                   candidate_refresh=CANDIDATE_REFRESH_SECONDS):
    alerted = load_alerted()
    start = time.time()
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

    print(f"Starting pole watch: market={market}  duration={duration_seconds}s  "
          f"already-alerted={len(alerted)} ticker(s) from prior chunks")
    candidates = get_midlargecap_candidates(market, ctx)
    print(f"Initial candidate list: {len(candidates)} ticker(s) (mid/large-cap, earnings+rating catalysts)")
    last_refresh = time.time()
    new_alerts = []

    pass_num = 0
    while time.time() - start < duration_seconds:
        pass_num += 1
        elapsed_min = (time.time() - start) / 60
        print(f"\n--- Pass {pass_num} ({elapsed_min:.1f} min elapsed) ---")

        if time.time() - last_refresh > candidate_refresh:
            print("Refreshing candidate list...")
            candidates = get_midlargecap_candidates(market, ctx)
            last_refresh = time.time()
            print(f"Candidate list now: {len(candidates)} ticker(s)")

        checked_this_pass = 0
        for ticker, reason in candidates.items():
            if time.time() - start >= duration_seconds:
                break
            try:
                analysis = compute_pole_analysis(ticker, reason, ctx)
            except Exception as e:
                print(f"  {ticker}: error ({e})")
                continue
            checked_this_pass += 1
            if analysis is not None:
                window_key = analysis["window_key"]
                if alerted.get(ticker) != window_key:
                    print(analysis["text"])
                    alerted[ticker] = window_key
                    new_alerts.append(ticker)
                    save_alerted(alerted)
            time.sleep(check_pacing)

        print(f"  (checked {checked_this_pass} candidate(s) this pass)")
        if time.time() - start >= duration_seconds:
            break

    ctx.close()
    print(f"\nPole watch chunk complete after {pass_num} pass(es). "
          f"{len(new_alerts)} new pole alert(s) this chunk: {new_alerts or 'none'}")
    return new_alerts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mid/large-cap pole-stage alert + live indicator analysis")
    parser.add_argument("--market", default="US", choices=["US", "HK"])
    parser.add_argument("--duration-seconds", type=int, default=120)
    parser.add_argument("--check-pacing", type=float, default=CHECK_PACING_SECONDS)
    parser.add_argument("--candidate-refresh", type=int, default=CANDIDATE_REFRESH_SECONDS)
    parser.add_argument("--reset-alerts", action="store_true",
                        help="Clear alerted-tickers memory before starting (e.g. for a new day)")
    args = parser.parse_args()
    if args.reset_alerts:
        save_alerted({})
        print("Cleared pole-watch alerted memory.")
    run_pole_watch(args.market, args.duration_seconds, args.check_pacing, args.candidate_refresh)
