# Intraday Scout — Version 2 roadmap

Version 1 delivers BUY / SELL / WAIT signals from 1m/5m-style rules on a fixed watchlist.
This document tracks what v2 should add (not implemented in v1).

## High priority

1. **Nifty / Bank Nifty trend filter** — skip long stock breakouts when the index is selling off.
2. **VIX / volatility day flag** — tighten rules or label “high risk day”.
3. **Yesterday high/low levels** — breakouts into prior-day levels vs mid-range noise.
4. **Time-of-day rules** — stricter after 14:30; special handling 9:15–9:45 opening noise.
5. **Liquidity filter** — min turnover / volume on F&O names only.
6. **Mini chart** — last 20–30 one-minute candles with signal marker on the Scout page.

## Medium priority

7. **Sector context** — banks weak → downgrade bank-stock longs.
8. **News / event disable** — no scouts on result day for a symbol.
9. **Failed breakout** — break then back inside range → SELL / avoid.
10. **Exit hints** — target zone and trail-stop suggestions, not entry only.
11. **Signal track record** — log outcomes vs entry for calibration.
12. **User watchlist editor** — UI to add/remove symbols (v1 uses config list).

## Nice to have

13. WebSocket-built candles (shared tick cache, no REST minute history per scan).
14. Order-book / depth imbalance.
15. Push notifications (Telegram / SMS) in addition to dashboard.
16. Stock futures symbols alongside cash.
17. Paper-trading mode for scout signals.
