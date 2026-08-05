"""
Combines catalyst scanning (news/earnings/ratings/pre-market movers) with a
fast 1-minute entry-signal check (entry_signal.py: structure w/ false-breakout
guard, volume, trend, VWAP, momentum, not-overbought, Bollinger
squeeze/breakout, key levels, volume profile, money flow -- 10 checks, ALL
required) to produce a list of genuinely confirmed day-trade candidates,
sized against your risk config.

Nothing here places any trade. It only proposes candidates for review.
"""
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


def _size_note(ticker, price, config, ctx):
    if not price:
        return "size: n/a (no price)"
    try:
        stop_info = risk_manager.calculate_structural_stop(ticker, ctx=ctx)
        stop = stop_info["stop_price"]
    except Exception as e:
        return f"size: n/a (stop calc failed: {e})"
    if stop >= price:
        return f"size: n/a (stop ${stop:.2f} is above current price ${price:.2f} -- invalidated)"
    result = risk_manager.calculate_position_size(price, stop, config)
    return (f"size: {result['shares']} sh (${result['actual_risk_dollars']} risk, "
            f"stop @ ${stop:.2f}, target @ ${result['target_price_2r']})")


def run(keywords=None):
    keywords = keywords or news_scanner.DEFAULT_KEYWORDS
    config = risk_manager.load_config()

    can_trade, cadence_reason = risk_manager.can_trade_today()
    print("=" * 70)
    print(f"TRADING STATUS: {'OK TO TRADE' if can_trade else 'BLOCKED'} — {cadence_reason}")
    print("=" * 70 + "\n")

    print("Scanning news + earnings + analyst ratings for catalysts...")
    hits, earnings_hits, rating_hits = news_scanner.scan(keywords, resolve_tickers=True)
    catalysts = news_scanner.confirmed_candidates(hits, earnings_hits, rating_hits)

    print("Scanning pre-market movers...")
    mover_rows = movers_scanner.pre_market_movers()
    for row in mover_rows:
        ticker = row["ticker"]
        catalysts.setdefault(ticker, f"pre-market mover ({row['pre_market_change_pct']:+.1f}%, no known catalyst)")

    print(f"Running 1-minute entry-signal check on {len(catalysts)} candidate(s) "
          f"(10 checks, all required)...")
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

    confirmed = []
    watching = []
    for ticker, reason in catalysts.items():
        result = entry_signal.check_entry(ticker, ctx=ctx)
        if result.get("confirmed"):
            confirmed.append((ticker, reason, result))
        else:
            watching.append((ticker, reason, result))
        time.sleep(1.5)  # paces both the kline call and the capital-flow call inside check_entry

    print("\n" + "=" * 70)
    print(f"CONFIRMED ({len(confirmed)}) — catalyst + ALL entry checks pass")
    print("=" * 70)
    if not confirmed:
        print("None right now.")
    for ticker, reason, result in confirmed:
        d = result["detail"]
        print(f"  {ticker}  price={d['current_price']}  pole={d['pole_pct']}%  rvol={d['rvol']}x  flow={d['flow_direction']}")
        print(f"    catalyst: {reason}")
        print(f"    {_size_note(ticker, d['current_price'], config, ctx)}")

    print("\n" + "=" * 70)
    print(f"WATCHING ({len(watching)}) — has a catalyst, entry not confirmed yet")
    print("=" * 70)
    if not watching:
        print("None right now.")
    for ticker, reason, result in watching:
        if "checks" in result:
            failed = [name for name, ok in result["checks"].items() if not ok]
            print(f"  {ticker}  missing: {', '.join(failed)}  <-  {reason}")
        else:
            print(f"  {ticker}  ({result.get('reason', 'no data')})  <-  {reason}")

    ctx.close()
    return confirmed, watching


if __name__ == "__main__":
    run()
