"""
Money-flow confirmation for a candidate ticker.

Replaces the ORDERINFLOW_XL / VWAP path through get_indicator_calc_result,
which is confirmed broken for US stocks (missing required cumulative-volume
fields). get_capital_flow is a separate, dedicated server-computed endpoint
that works for US stocks and gives a genuine institutional-buying-pressure
proxy: is real money currently flowing into or out of this stock.
"""
import argparse
import sys

from moomoo import OpenQuoteContext, RET_OK, PeriodType

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def flow_direction(code, lookback_bars=10, ctx=None):
    """Returns dict with recent net in_flow trend for a stock: positive =
    money flowing in (bullish confirmation), negative = flowing out.
    Pass an existing ctx to reuse one connection across many tickers
    (opening a fresh connection per call is slow when checking a whole list)."""
    owns_ctx = ctx is None
    if owns_ctx:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    ret, data = ctx.get_capital_flow(code, period_type=PeriodType.INTRADAY)
    if owns_ctx:
        ctx.close()
    if ret != RET_OK:
        return {"code": code, "ok": False, "error": str(data)}
    if data is None or data.empty:
        return {"code": code, "ok": False, "error": "no data"}

    recent = data.tail(lookback_bars)
    latest_flow = recent["in_flow"].iloc[-1]
    trend = recent["in_flow"].iloc[-1] - recent["in_flow"].iloc[0]

    return {
        "code": code,
        "ok": True,
        "latest_in_flow": float(latest_flow),
        "trend_over_window": float(trend),
        "direction": "inflow" if latest_flow > 0 else "outflow",
        "strengthening": trend > 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check money-flow direction for a stock")
    parser.add_argument("code", help="e.g. US.AAPL")
    parser.add_argument("--lookback-bars", type=int, default=10)
    args = parser.parse_args()

    result = flow_direction(args.code, args.lookback_bars)
    if not result["ok"]:
        print(f"Error: {result['error']}")
    else:
        print(f"{result['code']}: {result['direction']} "
              f"(latest net flow ${result['latest_in_flow']:,.0f}, "
              f"{'strengthening' if result['strengthening'] else 'weakening'} "
              f"over last {args.lookback_bars} bars)")
