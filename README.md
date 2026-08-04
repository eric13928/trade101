# Trading101

Semi-automated day-trading signal system built on top of moomoo OpenD — catalyst
scanning, a 1-minute entry-signal confirmation stack, risk management, and
bracket order placement (entry + stop-loss + target). Paper trading only;
live trades always require explicit human confirmation.

## Scripts (in `scripts/`)

- `get_quote.py` — simple snapshot quote lookup
- `news_scanner.py` — catalyst scanning: FDA/trial news, earnings beats, analyst upgrades
- `movers_scanner.py` — pre-market movers (momentum-first candidate source)
- `technical_screener.py` — moomoo's server-side chart-pattern/indicator screener (daily/hourly + intraday indicator crossovers)
- `flow_confirm.py` — money-flow direction check (buy vs. sell pressure)
- `entry_signal.py` — 1-minute entry-signal confirmation: structure (pole/flag/breakout), volume, trend, VWAP, momentum, overbought filter, Bollinger squeeze, key levels, money flow (all required)
- `risk_manager.py` — position sizing, structural stop-loss (swing low + ATR buffer), trading-day cadence (cash-account settlement safe)
- `signal_engine.py` — combines all of the above into one candidate scan
- `place_bracket_order.py` — entry + real stop-loss + real target order placement

## Config

- `risk_config.json` — account equity, risk per trade, reward target, trading cadence. Edit as the account grows.

## Status

Built and unit-tested against live (but after-hours/closed-market) data. Not
yet validated during real market hours — that's the next milestone.
