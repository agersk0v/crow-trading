# AGENTS.md

## Project
Crow Trading is a Python-first, research-oriented trading bot for comparing **crypto and stock swing-trading strategies** with small positions (approximately €50 per trade). The primary goal is to test whether a strategy has a repeatable edge *after realistic costs*, not to promise profits.

Read these documents before changing implementation:
- [Product requirements](doc/product.md)
- [Architecture](doc/architecture.md)
- [Implementation plan](doc/implementation.md)

## Non-negotiable rules
- **Research and paper trading first.** Live orders are out of scope for the initial implementation. Never enable live trading, place real orders, or connect production trade credentials without an explicit new user request.
- **No leverage, margin, shorting, derivatives, or automatic deposits/withdrawals** in v1.
- **€50 is a per-position notional target, not permission to trade.** Enforce configurable position, exposure, and loss limits in the execution layer when it is implemented.
- **Never commit secrets** or log API keys, tokens, personal account details, or raw credentials. Use environment variables and an ignored local `.env` file if needed.
- **Never claim a strategy is profitable** based only on an in-sample backtest. Report fees, spreads, slippage, sample size, drawdowns, and out-of-sample results. No look-ahead or survivorship bias.
- Tax estimates are informational, not tax advice. Keep raw trades and realized P&L available for Danish tax reporting; do not treat withdrawal as the taxable event.
- Do not merge pull requests, change repository settings, or deploy unattended live execution without explicit permission.

## Engineering conventions
- Python-first, typed interfaces, simple modules over microservices. Prefer understandable code and deterministic tests.
- Keep market data, strategy signals, backtesting, risk checks, and broker adapters separate. A strategy **must not** call a broker API.
- Normalize all candles to UTC timestamps with OHLCV columns. Record exchange, symbol, timeframe, timezone, adjustments, and source.
- Only evaluate **closed** candles. Signals generated using candle `t` must not execute at an unavailable earlier price.
- Simulations must account for market-specific trading hours, fees, spreads, slippage, order size/precision, and cash balances. Stocks additionally require corporate-action and FX handling.
- Use small, reviewable changes. Add or update tests when behavior changes. Do not silently modify historical datasets or published experiment parameters.
- Pin dependencies and document reproducible commands, assumptions, and data provenance.

## Delivery workflow
1. Inspect relevant documentation and existing tests.
2. Make the smallest change that satisfies the issue; update documentation if contracts or scope change.
3. Run lint/type checks and tests available in the repository; report what was not run.
4. For backtests, record the data window, cost assumptions, benchmark, and out-of-sample protocol.
5. Use feature branches and pull requests for future implementation work unless explicitly instructed otherwise. Leave merges to the user.

## Current scope
Documentation and planning only. See `doc/implementation.md` for staged delivery and acceptance criteria.
