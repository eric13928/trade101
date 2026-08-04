"""
Broad "what's moving right now" scanner using moomoo's pre-market ranking.

This is deliberately momentum-first rather than catalyst-first: it finds
stocks moving without needing a known reason. Complements news_scanner.py
(catalyst-first) rather than replacing it -- use news_scanner/get_search_news
to find out *why* something here is moving.
"""
import argparse
import sys

from moomoo import OpenQuoteContext, RET_OK, RankSortDir

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def pre_market_movers(min_price=5.0, min_change_pct=5.0, count=30):
    """Real pre-market movers, filtered to avoid illiquid penny-stock noise
    (unfiltered results are dominated by sub-$1 stocks with 100%+ swings on
    almost no volume -- not tradeable in practice)."""
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    ret, data = ctx.get_us_pre_market_rank(sort_dir=RankSortDir.DESCENDING, count=count)
    ctx.close()
    if ret != RET_OK:
        print(f"  [warn] pre-market rank failed: {data}")
        return []

    _, df = data
    if df is None or df.empty:
        return []

    rows = []
    for _, row in df.iterrows():
        price = row.get("pre_market_price")
        chg = row.get("pre_market_change_ratio")
        if price is None or chg is None or price == "N/A" or chg == "N/A":
            continue
        try:
            price, chg = float(price), float(chg)
        except (TypeError, ValueError):
            continue
        if price < min_price or abs(chg) < min_change_pct:
            continue
        rows.append({
            "ticker": row.get("security"),
            "name": row.get("name"),
            "pre_market_price": price,
            "pre_market_change_pct": chg,
            "pre_market_volume": row.get("pre_market_volume"),
        })
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan US pre-market movers")
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--min-change-pct", type=float, default=5.0)
    parser.add_argument("--count", type=int, default=30)
    args = parser.parse_args()

    rows = pre_market_movers(args.min_price, args.min_change_pct, args.count)
    if not rows:
        print("No qualifying pre-market movers right now "
              "(either nothing moving, or outside the pre-market window).")
    else:
        print(f"{len(rows)} pre-market mover(s):\n")
        for r in rows:
            print(f"  {r['ticker']:<10} {r['name']:<30} "
                  f"price={r['pre_market_price']}  chg={r['pre_market_change_pct']:+.1f}%  "
                  f"vol={r['pre_market_volume']}")
