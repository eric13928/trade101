"""
Technical pattern screener.

Queries moomoo's server-side screener for stocks currently matching a chart
pattern (default: bull FLAG) on the daily chart, with basic liquidity filters
so results aren't dominated by illiquid penny stocks.
"""
import argparse
import sys

from moomoo import OpenQuoteContext, RET_OK, StockScreenRequest
from moomoo.quote.stock_screen_const import (
    SimpleField, ScrMarket, KlineShapeProperty, KlineShapeType, Period,
    BasicProperty, SimpleProperty, CumulativeProperty, Pattern,
)

# IMPORTANT (confirmed against SKILL.md + live testing):
# The chart-SHAPE engine (kline_shape / FLAG, DOUBLE_BOTTOMS, HEAD_SHOULDERS, etc.)
# only supports DAY or HOUR_1 periods -- intraday minute periods silently return
# zero matches, they are not rejected with an error. Do not pass minute periods here.
SHAPE_ALLOWED_PERIODS = ("DAY", "1HOUR")

# The indicator-signal engine (indicator_pattern / MACD_GOLD_CROSS, RSI_GOLD_CROSS,
# BOLL_BREAK_UPPER, etc. -- crossovers and divergences) DOES support intraday
# periods including MINUTE_5, confirmed live with real non-zero results.
INDICATOR_PATTERN_MAP = {
    "MACD_GOLD_CROSS": Pattern.MACD_GOLD_CROSS,
    "MACD_DEATH_CROSS": Pattern.MACD_DEATH_CROSS,
    "MACD_TOP_DIVERGE": Pattern.MACD_TOP_DIVERGE,
    "MACD_BOTTOM_DIVERGE": Pattern.MACD_BOTTOM_DIVERGE,
    "RSI_GOLD_CROSS": Pattern.RSI_GOLD_CROSS,
    "RSI_DEATH_CROSS": Pattern.RSI_DEATH_CROSS,
    "RSI_TOP_DIVERGE": Pattern.RSI_TOP_DIVERGE,
    "RSI_BOTTOM_DIVERGE": Pattern.RSI_BOTTOM_DIVERGE,
    "KDJ_GOLD_CROSS": Pattern.KDJ_GOLD_CROSS,
    "KDJ_DEATH_CROSS": Pattern.KDJ_DEATH_CROSS,
    "BOLL_BREAK_UPPER": Pattern.BOLL_BREAK_UPPER,
    "BOLL_BREAK_LOWER": Pattern.BOLL_BREAK_LOWER,
    "BOLL_CROSS_MID_UP": Pattern.BOLL_CROSS_MID_UP,
}

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PERIOD_MAP = {
    "1MIN": Period.MINUTE_1,
    "3MIN": Period.MINUTE_3,
    "5MIN": Period.MINUTE_5,
    "15MIN": Period.MINUTE_15,
    "30MIN": Period.MINUTE_30,
    "1HOUR": Period.HOUR_1,
    "DAY": Period.DAY,
}

PATTERN_MAP = {
    "FLAG": KlineShapeType.FLAG,
    "FLAG_DOWN": KlineShapeType.FLAG_DOWN,
    "DOUBLE_BOTTOMS": KlineShapeType.DOUBLE_BOTTOMS,
    "DOUBLE_PEAKS": KlineShapeType.DOUBLE_PEAKS,
    "HEAD_SHOULDERS_BOTTOM": KlineShapeType.HEAD_SHOULDERS_BOTTOM,
    "HEAD_SHOULDERS_PEAK": KlineShapeType.HEAD_SHOULDERS_PEAK,
    "CUP_BOTTOM": KlineShapeType.CUP_BOTTOM,
    "WEDGE": KlineShapeType.WEDGE,
    "SYMMETRY_TRIANGLE": KlineShapeType.SYMMETRY_TRIANGLE,
}

# field id -> (index into results, key) is unnecessary; we just walk each
# result's flat list and match on the property "name" code via the enum value.
_BASIC_CODE_NAME = int(BasicProperty.CODE)
_BASIC_NAME_NAME = int(BasicProperty.NAME)
_PRICE_NAME = int(SimpleProperty.PRICE)
_CHG_NAME = int(SimpleProperty.PRICE_CHANGE_RATE)
_VOLRATIO_NAME = int(SimpleProperty.VOLUME_RATIO)
_MCAP_NAME = int(SimpleProperty.MARKET_CAP)


def _extract(item):
    row = {}
    for r in item.get("results", []):
        prop = r.get("property", {})
        name = prop.get("name")
        val = r.get("sval", r.get("dval", r.get("ival")))
        if name == _BASIC_CODE_NAME:
            row["code"] = val
        elif name == _BASIC_NAME_NAME:
            row["name"] = val
        elif name == _PRICE_NAME:
            row["price"] = val
        elif name == _CHG_NAME:
            row["change_pct"] = val
        elif name == _VOLRATIO_NAME:
            row["volume_ratio"] = val
        elif name == _MCAP_NAME:
            row["market_cap"] = val
    return row


def screen_pattern(pattern="FLAG", market="US", min_price=5.0, min_market_cap=3e8,
                   page_count=100, period="1HOUR"):
    """Chart-SHAPE pattern screen (FLAG, DOUBLE_BOTTOMS, etc). Only DAY/1HOUR
    are valid periods for this engine -- see SHAPE_ALLOWED_PERIODS."""
    shape = PATTERN_MAP.get(pattern.upper())
    if shape is None:
        raise ValueError(f"Unknown pattern: {pattern}, choices: {list(PATTERN_MAP)}")
    if period.upper() not in SHAPE_ALLOWED_PERIODS:
        raise ValueError(
            f"Chart-shape patterns only support {SHAPE_ALLOWED_PERIODS} periods "
            f"(moomoo limitation, not ours) -- got {period}. "
            f"For intraday timing, use screen_indicator_pattern() instead."
        )
    period_enum = PERIOD_MAP.get(period.upper())

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    req = StockScreenRequest()
    req.page_from = 0
    req.page_count = page_count
    req.add_simple_field(field=SimpleField.MARKET, values=[int(getattr(ScrMarket, market.upper()))])
    req.add_kline_shape(name=KlineShapeProperty.SHAPE_TYPE, period=period_enum, value_set=[int(shape)])
    req.add_simple_property(name=SimpleProperty.PRICE, lower=min_price)
    req.add_simple_property(name=SimpleProperty.MARKET_CAP, lower=min_market_cap)
    req.add_retrieve_basic(name=BasicProperty.CODE)
    req.add_retrieve_basic(name=BasicProperty.NAME)
    req.add_retrieve_simple(name=SimpleProperty.PRICE)
    req.add_retrieve_simple(name=SimpleProperty.PRICE_CHANGE_RATE)
    req.add_retrieve_simple(name=SimpleProperty.VOLUME_RATIO)
    req.add_retrieve_simple(name=SimpleProperty.MARKET_CAP)

    ret, data = ctx.get_stock_screen(req)
    ctx.close()
    if ret != RET_OK:
        print(f"  [warn] screen failed: {data}")
        return []

    _, all_count, items = data
    return [_extract(it) for it in (items or [])]


def screen_indicator_pattern(pattern="MACD_GOLD_CROSS", market="US", min_price=5.0,
                             min_market_cap=3e8, page_count=100, period="5MIN"):
    """Indicator crossover/divergence screen (MACD/RSI/KDJ/Bollinger). Supports
    intraday periods including MINUTE_5 -- confirmed live, unlike screen_pattern()."""
    ind_pattern = INDICATOR_PATTERN_MAP.get(pattern.upper())
    if ind_pattern is None:
        raise ValueError(f"Unknown indicator pattern: {pattern}, choices: {list(INDICATOR_PATTERN_MAP)}")
    period_enum = PERIOD_MAP.get(period.upper())
    if period_enum is None:
        raise ValueError(f"Unknown period: {period}, choices: {list(PERIOD_MAP)}")

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    req = StockScreenRequest()
    req.page_from = 0
    req.page_count = page_count
    req.add_simple_field(field=SimpleField.MARKET, values=[int(getattr(ScrMarket, market.upper()))])
    req.add_indicator_pattern(name=ind_pattern, period_type=period_enum)
    req.add_simple_property(name=SimpleProperty.PRICE, lower=min_price)
    req.add_simple_property(name=SimpleProperty.MARKET_CAP, lower=min_market_cap)
    req.add_retrieve_basic(name=BasicProperty.CODE)
    req.add_retrieve_basic(name=BasicProperty.NAME)
    req.add_retrieve_simple(name=SimpleProperty.PRICE)
    req.add_retrieve_simple(name=SimpleProperty.PRICE_CHANGE_RATE)
    req.add_retrieve_simple(name=SimpleProperty.VOLUME_RATIO)
    req.add_retrieve_simple(name=SimpleProperty.MARKET_CAP)

    ret, data = ctx.get_stock_screen(req)
    ctx.close()
    if ret != RET_OK:
        print(f"  [warn] indicator screen failed: {data}")
        return []

    _, all_count, items = data
    return [_extract(it) for it in (items or [])]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Screen for a chart/indicator pattern via moomoo's screener")
    parser.add_argument("--mode", default="shape", choices=["shape", "indicator"],
                        help="'shape'=chart patterns like FLAG (DAY/1HOUR only); "
                             "'indicator'=crossovers like MACD_GOLD_CROSS (any period incl. intraday)")
    parser.add_argument("--pattern", default="FLAG")
    parser.add_argument("--period", default=None, help="defaults to 1HOUR for shape, 5MIN for indicator")
    parser.add_argument("--market", default="US")
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--min-market-cap", type=float, default=3e8)
    args = parser.parse_args()

    if args.mode == "shape":
        period = args.period or "1HOUR"
        rows = screen_pattern(args.pattern, args.market, args.min_price, args.min_market_cap, period=period)
    else:
        period = args.period or "5MIN"
        rows = screen_indicator_pattern(args.pattern, args.market, args.min_price, args.min_market_cap, period=period)

    if not rows:
        print("No matches.")
    else:
        print(f"{len(rows)} stock(s) currently matching {args.pattern} ({args.market}, {period}):\n")
        for r in rows:
            mcap = r.get("market_cap")
            mcap_str = f"{mcap:,.0f}" if mcap else "-"
            print(f"  {r.get('code'):<10} {r.get('name', ''):<30} "
                  f"price={r.get('price')}  chg={r.get('change_pct')}%  "
                  f"vol_ratio={r.get('volume_ratio')}  mcap={mcap_str}")
