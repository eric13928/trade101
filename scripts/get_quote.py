"""Fetch a live snapshot quote for a given stock code via local moomoo OpenD."""
import sys
from moomoo import OpenQuoteContext, RET_OK

CODE = sys.argv[1] if len(sys.argv) > 1 else "CA.CCO"

ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
ret, data = ctx.get_market_snapshot([CODE])
if ret == RET_OK:
    row = data.iloc[0]
    change = row["last_price"] - row["prev_close_price"]
    change_pct = change / row["prev_close_price"] * 100
    print(f"{row['name']} ({row['code']})")
    print(f"  Last:   {row['last_price']:.2f}  ({change:+.2f}, {change_pct:+.2f}%)")
    print(f"  Open:   {row['open_price']:.2f}")
    print(f"  High:   {row['high_price']:.2f}")
    print(f"  Low:    {row['low_price']:.2f}")
    print(f"  Prev:   {row['prev_close_price']:.2f}")
    print(f"  Volume: {row['volume']:,.0f}")
    print(f"  Updated: {row['update_time']}")
else:
    print(f"Failed to get quote for {CODE}: {data}")
ctx.close()
