"""
Premarket-only catalyst-first alert scanner.

Regular-hours monitor.py waits for full technical confirmation (structure,
VWAP, levels, volume profile, money flow) before alerting -- that logic
needs regular-session bars and can't run premarket. Momentum-based screens
(movers_scanner.py, or any "biggest % gainers" list, including moomoo's own
premarket screener) are inherently reactive: a stock only ranks there AFTER
it's already moved, which is why it's often already up 50-100% by the time
you spot it there.

This scanner is catalyst-first instead: it surfaces the news/earnings/rating
event itself the moment it's detected, before price has necessarily caught
up. There is NO technical confirmation behind these alerts -- they are raw,
unconfirmed catalyst notices, meant purely for speed. Manual judgment and
execution only; this never places a trade.

Runs in bounded chunks (like monitor.py) and refuses to run once regular
market hours begin -- monitor.py takes over from there. Uses moomoo's own
PRE_MARKET_BEGIN market-state value (get_global_state) rather than a
hand-rolled timezone calculation, so it doesn't drift with DST.
"""
import argparse
import json
import os
import sys
import time

from moomoo import OpenQuoteContext, RET_OK

import news_scanner

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ALERTED_FILE = os.path.join(os.path.dirname(__file__), "data", "premarket_alerted.json")
LOOP_PACING_SECONDS = 5  # how often to re-poll catalyst sources within one chunk

MARKET_STATE_FIELD = {"US": "market_us"}
PREMARKET_STATES = {"PRE_MARKET_BEGIN"}


def load_alerted():
    if os.path.exists(ALERTED_FILE):
        with open(ALERTED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_alerted(alerted):
    os.makedirs(os.path.dirname(ALERTED_FILE), exist_ok=True)
    with open(ALERTED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(alerted), f)


def is_premarket_now(ctx, market="US"):
    field = MARKET_STATE_FIELD.get(market)
    if field is None:
        return False
    ret, data = ctx.get_global_state()
    if ret != RET_OK:
        return False
    return data.get(field) in PREMARKET_STATES


def get_catalyst_candidates(market, keywords, ctx):
    hits, earnings_hits, rating_hits = news_scanner.scan(keywords, resolve_tickers=True, market=market)
    catalysts = news_scanner.confirmed_candidates(hits, earnings_hits, rating_hits)
    return news_scanner.filter_tradeable_candidates(catalysts, ctx)


def print_catalyst_alert(ticker, reason, ctx):
    ret, snap = ctx.get_market_snapshot([ticker])
    price = None
    if ret == RET_OK and snap is not None and not snap.empty:
        price = snap.iloc[0].get("last_price")
    print("\n" + "-" * 70)
    print(f"*** PREMARKET CATALYST (unconfirmed): {ticker} ***")
    print(f"  reason: {reason}")
    print(f"  price: {price if price is not None else 'n/a'}")
    print("  ^ no technical confirmation behind this -- catalyst only, for speed")
    print("-" * 70)


def run_premarket_scan(market="US", duration_seconds=120, loop_pacing=LOOP_PACING_SECONDS):
    alerted = load_alerted()
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

    if not is_premarket_now(ctx, market):
        print(f"{market} is not in premarket right now -- nothing to do. "
              f"Use monitor.py for regular-hours confirmed signals instead.")
        ctx.close()
        return []

    print(f"Starting premarket catalyst scan: market={market}  duration={duration_seconds}s  "
          f"already-alerted={len(alerted)} ticker(s) from prior chunks")

    start = time.time()
    new_alerts = []
    loop_num = 0
    while time.time() - start < duration_seconds:
        if not is_premarket_now(ctx, market):
            print("Regular session has started -- stopping premarket scan.")
            break
        loop_num += 1
        candidates = get_catalyst_candidates(market, news_scanner.DEFAULT_KEYWORDS, ctx)
        new_this_loop = [t for t in candidates if t not in alerted]
        print(f"--- Loop {loop_num} ({(time.time() - start) / 60:.1f} min elapsed) -- "
              f"{len(candidates)} candidate(s), {len(new_this_loop)} new ---")
        for ticker in new_this_loop:
            print_catalyst_alert(ticker, candidates[ticker], ctx)
            alerted.add(ticker)
            new_alerts.append(ticker)
            save_alerted(alerted)
        remaining = duration_seconds - (time.time() - start)
        if remaining > loop_pacing:
            time.sleep(loop_pacing)
        else:
            break

    ctx.close()
    print(f"\nPremarket scan chunk complete after {loop_num} loop(s). "
          f"{len(new_alerts)} new catalyst alert(s): {new_alerts or 'none'}")
    print(f"Total alerted across all chunks so far: {len(alerted)}")
    return new_alerts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Premarket catalyst-first alert scan (no technical confirmation)")
    parser.add_argument("--market", default="US", choices=["US"])
    parser.add_argument("--duration-seconds", type=int, default=120)
    parser.add_argument("--loop-pacing", type=float, default=LOOP_PACING_SECONDS)
    parser.add_argument("--reset-alerts", action="store_true",
                        help="Clear premarket-alerted memory before starting (e.g. for a new day)")
    args = parser.parse_args()
    if args.reset_alerts:
        save_alerted(set())
        print("Cleared premarket-alerted memory.")
    run_premarket_scan(args.market, args.duration_seconds, args.loop_pacing)
