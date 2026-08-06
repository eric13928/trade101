"""
Continuous monitoring loop: refreshes the catalyst candidate list
periodically and re-checks each candidate's entry signal repeatedly,
instead of a single one-shot scan -- so a candidate whose pattern
completes at 9:44 gets caught close to 9:44, not whenever we happen to
run signal_engine.py next.

Runs in bounded chunks (default ~2 min, well under the 10-min tool timeout).
For longer monitoring windows, re-invoke this script again -- it persists
which tickers have already been alerted (data/alerted_tickers.json) so
re-launching doesn't spam duplicate alerts for the same confirmed signal.
Use --reset-alerts to start a fresh monitoring session (e.g. a new day).
"""
import argparse
import json
import os
import sys
import time

from moomoo import OpenQuoteContext

import entry_signal
import movers_scanner
import news_scanner
import risk_manager

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ALERTED_FILE = os.path.join(os.path.dirname(__file__), "data", "alerted_tickers.json")
CHECK_PACING_SECONDS = 1.5  # between individual candidate checks -- matches signal_engine.py's tested rate-limit-safe pacing
CANDIDATE_REFRESH_SECONDS = 300  # refresh the catalyst list every 5 min
NEAR_MISS_MAX_MISSING = 1  # "watch close" fires once structure has confirmed and at most this many other required checks remain


def load_alerted():
    if os.path.exists(ALERTED_FILE):
        with open(ALERTED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_alerted(alerted):
    os.makedirs(os.path.dirname(ALERTED_FILE), exist_ok=True)
    with open(ALERTED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(alerted), f)


def get_candidates(market, keywords, ctx):
    hits, earnings_hits, rating_hits = news_scanner.scan(keywords, resolve_tickers=True, market=market)
    catalysts = news_scanner.confirmed_candidates(hits, earnings_hits, rating_hits)
    if market == "US":
        for row in movers_scanner.pre_market_movers():
            catalysts.setdefault(row["ticker"], f"pre-market mover ({row['pre_market_change_pct']:+.1f}%, no known catalyst)")
    before = len(catalysts)
    catalysts = news_scanner.filter_tradeable_candidates(catalysts, ctx)
    dropped = before - len(catalysts)
    if dropped:
        print(f"  (dropped {dropped} penny/OTC candidate(s))")
    return catalysts


def print_alert(ticker, reason, result, ctx, config):
    d = result["detail"]
    print("\n" + "=" * 70)
    print(f"*** CONFIRMED: {ticker} ***")
    print(f"  catalyst: {reason}")
    print(f"  price={d['current_price']}  pole={d['pole_pct']}%  rvol={d['rvol']}x  flow={d['flow_direction']}")
    try:
        stop_info = risk_manager.calculate_structural_stop(ticker, ctx=ctx)
        stop = stop_info["stop_price"]
        size = risk_manager.calculate_position_size(d["current_price"], stop, config, ticker=ticker)
        print(f"  size: {size['shares']} sh ({size['currency']} {size['actual_risk_local']} = "
              f"${size['actual_risk_dollars']} USD risk)")
        print(f"  stop @ {size['currency']} {stop:.2f}  target @ {size['currency']} {size['target_price_2r']}")
    except Exception as e:
        print(f"  size calc failed: {e}")
    print("=" * 70)


def print_near_miss(ticker, reason, result, missing):
    """Advance-warning alert, distinct from print_alert(): structure has
    already confirmed (a real, sustained breakout is happening right now)
    but the candidate isn't fully confirmed yet. Meant to be seen BEFORE the
    final confirmation so a manually-executed trade doesn't lose the whole
    reaction-time gap to reading the alert cold -- by the time it's fully
    confirmed, you're already watching and ready, not starting from zero."""
    d = result["detail"]
    print(f"\n>>> WATCH CLOSE: {ticker} -- structure confirmed, missing only: {', '.join(missing)}")
    print(f"    catalyst: {reason}")
    print(f"    price={d['current_price']}  pole={d['pole_pct']}%  flag_high={d['flag_high']}  "
          f"rvol={d['rvol']}x  flow={d['flow_direction']}")


def run_monitor(market="US", duration_seconds=120, check_pacing=CHECK_PACING_SECONDS,
                candidate_refresh=CANDIDATE_REFRESH_SECONDS):
    config = risk_manager.load_config()
    alerted = load_alerted()
    start = time.time()
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

    print(f"Starting monitor chunk: market={market}  duration={duration_seconds}s  "
          f"already-alerted={len(alerted)} ticker(s) from prior chunks")
    candidates = get_candidates(market, news_scanner.DEFAULT_KEYWORDS, ctx)
    print(f"Initial candidate list: {len(candidates)} ticker(s)")
    last_refresh = time.time()
    new_alerts = []
    near_miss_seen = {}  # ticker -> missing-checks list last alerted, this chunk only (resets on restart)

    pass_num = 0
    while time.time() - start < duration_seconds:
        pass_num += 1
        elapsed_min = (time.time() - start) / 60
        print(f"\n--- Pass {pass_num} ({elapsed_min:.1f} min elapsed) ---")

        if time.time() - last_refresh > candidate_refresh:
            print("Refreshing candidate list...")
            candidates = get_candidates(market, news_scanner.DEFAULT_KEYWORDS, ctx)
            last_refresh = time.time()
            print(f"Candidate list now: {len(candidates)} ticker(s)")

        checked_this_pass = 0
        for ticker, reason in candidates.items():
            if ticker in alerted:
                continue
            if time.time() - start >= duration_seconds:
                break
            try:
                result = entry_signal.check_entry(ticker, ctx=ctx)
            except Exception as e:
                print(f"  {ticker}: error ({e})")
                continue
            checked_this_pass += 1
            if result.get("confirmed"):
                print_alert(ticker, reason, result, ctx, config)
                alerted.add(ticker)
                new_alerts.append(ticker)
                save_alerted(alerted)
            elif "required_checks" in result:
                req = result["required_checks"]
                missing = sorted(k for k, v in req.items() if not v)
                if req.get("structure") and len(missing) <= NEAR_MISS_MAX_MISSING \
                        and near_miss_seen.get(ticker) != missing:
                    print_near_miss(ticker, reason, result, missing)
                    near_miss_seen[ticker] = missing
            time.sleep(check_pacing)

        print(f"  (checked {checked_this_pass} not-yet-alerted candidate(s) this pass)")
        if time.time() - start >= duration_seconds:
            break

    ctx.close()
    print(f"\nMonitor chunk complete after {pass_num} pass(es). "
          f"{len(new_alerts)} new confirmed alert(s) this chunk: {new_alerts or 'none'}")
    print(f"Total alerted across all chunks so far: {len(alerted)}")
    return new_alerts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Continuous entry-signal monitoring loop")
    parser.add_argument("--market", default="US", choices=["US", "HK"])
    parser.add_argument("--duration-seconds", type=int, default=120)
    parser.add_argument("--check-pacing", type=float, default=CHECK_PACING_SECONDS)
    parser.add_argument("--candidate-refresh", type=int, default=CANDIDATE_REFRESH_SECONDS)
    parser.add_argument("--reset-alerts", action="store_true",
                        help="Clear alerted-tickers memory before starting (e.g. for a new day)")
    args = parser.parse_args()
    if args.reset_alerts:
        save_alerted(set())
        print("Cleared alerted-tickers memory.")
    run_monitor(args.market, args.duration_seconds, args.check_pacing, args.candidate_refresh)
