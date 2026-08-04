"""
News catalyst scanner.

Polls moomoo's news search for a set of catalyst keywords (FDA approval,
earnings beats, trial results, etc.) and reports newly-seen headlines along
with the stock(s) they're tagged to. Keeps a local cache of already-seen
article URLs so repeat runs only surface new hits.
"""
import argparse
import json
import os
import re
import sys
import time

import moomoo
from moomoo import OpenQuoteContext, RET_OK, NewsSubType

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "seen_news.json")

# Keyword news search covers catalysts that have no structured API (FDA, trials).
# Earnings beats are pulled separately via get_earnings_beat_rank, which has
# reliable tickers attached, instead of keyword-searching for "beats earnings".
DEFAULT_KEYWORDS = [
    "FDA approval",
    "FDA clearance",
    "phase 3 trial",
    "clinical trial results",
]

# Trigger words that usually separate a company name from the rest of a
# headline, e.g. "Acme Corp Announces FDA Approval of..." -> "Acme Corp"
_NAME_SPLIT_RE = re.compile(
    r"\s+(?:Announces?|Reports?|Wins?|Obtains?|Receives?|Files?|Says?|"
    r"Releases?|Launches?|Achieves?|Discloses?|Provides?|Presents?|"
    r"Regarding|Notice)\b",
    re.IGNORECASE,
)

MAX_TICKER_GUESSES_PER_RUN = 10


def load_seen():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


def guess_company_name(title):
    """Best-effort extraction of a likely company name from a headline."""
    head = _NAME_SPLIT_RE.split(title, maxsplit=1)[0].strip(" :-‑–")
    if not head or len(head) > 60:
        # fallback: first few words
        head = " ".join(title.split()[:4])
    return head


def guess_ticker(ctx, title, budget):
    """Try to resolve a headline to a US ticker via moomoo's symbol search.
    Returns (ticker_or_None, candidate_name). Marked as a guess, not fact."""
    if budget[0] <= 0:
        return None, None
    candidate = guess_company_name(title)
    if not candidate:
        return None, None
    budget[0] -= 1
    time.sleep(3.1)  # search_quote shares the 10-req/30s limit
    ret, data = ctx.get_search_quote(candidate, 10)
    if ret != RET_OK or data is None or data.empty:
        return None, candidate
    us_stocks = data[(data["market"] == "US") & (data["sec_type"] == "STOCK")]
    pick = us_stocks.iloc[0] if not us_stocks.empty else data.iloc[0]
    return pick.get("code"), candidate


def get_rating_upgrades(ctx, count=20):
    """Structured analyst-upgrade feed (real tickers, no keyword guessing needed)."""
    ret, data, _next_page, all_count = ctx.get_rating_change(
        market=moomoo.Market.US, change_type=moomoo.RatingChangeType.UPGRADE, count=count, page=None,
    )
    if ret != RET_OK:
        print(f"  [warn] rating change failed: {data}")
        return []
    if data is None or data.empty:
        return []
    hits = []
    for _, row in data.iterrows():
        rating, last_rating = row.get("rating"), row.get("last_rating")
        if rating == last_rating:
            continue  # target-price-only revision, not an actual tier change -- skip
        hits.append({
            "ticker": row.get("security"),
            "name": row.get("name"),
            "institution": row.get("institution_name"),
            "rating": rating,
            "last_rating": last_rating,
            "target_price": row.get("target_price"),
            "date": row.get("recommendation_date"),
        })
    return hits


def get_earnings_beats(ctx, market="US", min_beat_ratio=5.0, min_day_chg=3.0):
    """Pull structured earnings-beat data, keeping only beats that actually
    moved the stock (filters out beats the market shrugged off)."""
    ret, data = ctx.get_earnings_beat_rank(
        moomoo.Market.US if market == "US" else market,
        moomoo.BeatType.EPS,
        count=30,
        term=moomoo.BeatTerm.LATEST,
        sort_field=moomoo.EarningsBeatSortField.RELEASED_DATE,
    )
    if ret != RET_OK:
        print(f"  [warn] earnings beat rank failed: {data}")
        return []
    _, df = data
    if df is None or df.empty:
        return []
    hits = []
    for _, row in df.iterrows():
        beat_ratio = row.get("beat_ratio")
        day_chg = row.get("earning_day_chg")
        if beat_ratio is None or day_chg is None:
            continue
        if beat_ratio <= min_beat_ratio or day_chg <= min_day_chg:
            continue
        hits.append({
            "ticker": row.get("security"),
            "name": row.get("name"),
            "released_date": row.get("released_date"),
            "beat_ratio": row.get("beat_ratio"),
            "earning_day_chg": row.get("earning_day_chg"),
        })
    return hits


TRADEABLE_MARKETS = ("US.", "HK.")  # matches this account's quote/trade permissions


def confirmed_candidates(hits, earnings_hits, rating_hits=None):
    """Tickers safe to feed into technical monitoring: earnings beats and analyst
    upgrades (both structured, reliable) plus news items with moomoo's own
    confirmed related_securities tag. Guessed tickers are excluded — shown as
    FYI only, never auto-promoted. Also restricted to markets this account can
    actually trade (US/HK)."""
    candidates = {}
    for e in earnings_hits:
        candidates[e["ticker"]] = f"earnings beat ({e['beat_ratio']:.1f}% beat, {e['earning_day_chg']:+.1f}% day)"
    for r in (rating_hits or []):
        candidates.setdefault(
            r["ticker"],
            f"analyst upgrade ({r['institution']}: {r['last_rating']} -> {r['rating']}, target ${r['target_price']})"
        )
    for h in hits:
        for code in (h["related_securities"] or []):
            candidates.setdefault(code, f"news: {h['title'][:70]}")
    return {k: v for k, v in candidates.items() if k.startswith(TRADEABLE_MARKETS)}


def scan(keywords, max_count=10, resolve_tickers=True):
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    seen = load_seen()
    new_hits = []
    budget = [MAX_TICKER_GUESSES_PER_RUN]

    for kw in keywords:
        ret, data = ctx.get_search_news(kw, max_count, news_sub_type=NewsSubType.ALL)
        if ret != RET_OK:
            print(f"  [warn] search failed for '{kw}': {data}")
            continue
        if data is None or data.empty:
            continue
        for _, row in data.iterrows():
            key = row.get("url") or f"{row.get('title')}|{row.get('publish_time')}"
            if key in seen:
                continue
            seen.add(key)
            related = row.get("related_securities") or []
            guessed_ticker, guessed_name = (None, None)
            if not related and resolve_tickers:
                guessed_ticker, guessed_name = guess_ticker(ctx, row.get("title", ""), budget)
            new_hits.append({
                "keyword": kw,
                "title": row.get("title"),
                "publish_time": row.get("publish_time"),
                "source": row.get("source"),
                "related_securities": related,
                "guessed_ticker": guessed_ticker,
                "guessed_from": guessed_name,
                "url": row.get("url"),
            })
        time.sleep(3.1)  # stay under the 10-req/30s search_news limit

    earnings_hits = get_earnings_beats(ctx) if resolve_tickers else []
    rating_hits = get_rating_upgrades(ctx) if resolve_tickers else []

    ctx.close()
    save_seen(seen)
    return new_hits, earnings_hits, rating_hits


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan moomoo news for catalyst keywords")
    parser.add_argument("--keywords", nargs="*", default=DEFAULT_KEYWORDS)
    parser.add_argument("--max-count", type=int, default=10)
    parser.add_argument("--no-resolve", action="store_true",
                        help="Skip ticker-guessing and earnings-beat lookup (faster, news only)")
    args = parser.parse_args()

    hits, earnings_hits, rating_hits = scan(args.keywords, args.max_count, resolve_tickers=not args.no_resolve)

    if earnings_hits:
        print(f"=== {len(earnings_hits)} recent US earnings beat(s) (structured data, real tickers) ===\n")
        for e in earnings_hits:
            print(f"{e['ticker']}  {e['name']}  beat_ratio={e['beat_ratio']}  "
                  f"day_chg={e['earning_day_chg']}  released={e['released_date']}")
        print()

    if rating_hits:
        print(f"=== {len(rating_hits)} recent US analyst upgrade(s) (structured data, real tickers) ===\n")
        for r in rating_hits:
            print(f"{r['ticker']}  {r['name']}  {r['institution']}: {r['last_rating']} -> {r['rating']}  "
                  f"target=${r['target_price']}  date={r['date']}")
        print()

    if not hits:
        print("No new catalyst headlines found this run.")
    else:
        print(f"=== {len(hits)} new headline(s) ===\n")
        for h in hits:
            ticker_note = ""
            if h["related_securities"]:
                ticker_note = f"  [ticker: {h['related_securities']}]"
            elif h["guessed_ticker"]:
                ticker_note = f"  [guessed ticker: {h['guessed_ticker']} from \"{h['guessed_from']}\" — unverified]"
            print(f"[{h['keyword']}] {h['title']}{ticker_note}")
            print(f"  Source: {h['source']}  |  Published: {h['publish_time']}")
            print(f"  {h['url']}")
            print()

    candidates = confirmed_candidates(hits, earnings_hits, rating_hits)
    print("=== Confirmed candidates for technical monitoring (guessed tickers excluded) ===")
    if not candidates:
        print("None this run.")
    else:
        for ticker, reason in candidates.items():
            print(f"  {ticker}  <-  {reason}")
