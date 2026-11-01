# Product

## Goal
Compare crypto and stock swing strategies after realistic costs. Support local research and paper trading for a Danish user. Finding no edge is a valid result.

## Current capabilities
- Public daily market-data ingestion, validation, and immutable Parquet caches.
- Pure momentum and mean-reversion intents with causal historical replay.
- Offline single-asset backtests with costs, FX, risk checks, isolated evaluation windows, ledger, benchmarks, and charts.
- Stock simulation supports unadjusted prices only and requires a complete no-corporate-action coverage snapshot. Splits/dividends and adjusted-price fills are rejected, not ignored.
- FX uses explicit timestamped snapshots; no rates or corporate-action facts are invented. Demo datasets/rates are synthetic and do not measure investment performance.
- No watchlist scanning, scheduling, notifications, paper execution, or live trading yet.

See [data](data.md), [strategies](strategies.md), and [backtesting](backtesting.md) for commands and precise limitations.

## MVP requirements
- Assets: BTC/EUR, ETH/EUR, and a small liquid stock sample.
- Start with daily bars; add intraday only when history and data quality support it.
- Ingest crypto through CCXT/exchange archives and stocks through yfinance for research.
- Validate and cache UTC OHLCV with source, adjustment, and completeness metadata.
- Run the same pure momentum-breakout and mean-reversion strategies on both markets.
- Simulate long-only positions targeting €50 each, with configurable capital and risk limits.
- Model fees, spread, slippage, cash, trading calendars, quantity precision, corporate actions, and FX.
- Evaluate predeclared parameters using training, validation, and untouched test periods.
- Report gross/net P&L, meaningful annualized returns, drawdown, exposure, turnover, profit factor, trade counts, win/loss statistics, and buy-and-hold/cash benchmarks.
- Paper trading must reuse strategy, risk, and ledger contracts; require no trading credentials.
- Export fills, quantities, prices, fees, asset IDs, timestamps, realized P&L, and FX provenance for EUR/DKK tax records. Keep tax scenarios separate from investment results.

## Acceptance
- Fetch, validate, cache, and reload both crypto pairs and at least one stock with provenance.
- Produce reproducible reports: trades CSV, configuration, dataset fingerprints, metrics, and drawdown chart.
- Test closed candles, look-ahead prevention, position caps, empty/missing data, non-trading days, and fees.
- Paper execution survives restarts without duplicate orders or real-money trades.

## Excluded
Live orders, leverage, shorting, derivatives, deposits/withdrawals, copy trading, high-frequency trading, AI price prediction, portfolio optimization, hosted infrastructure, and tax filing. See [agent rules](../AGENTS.md).

## Unresolved
Choose stock universe/exchanges, data licensing, starting capital, loss budget, holding limits, and exact venue costs before affected experiments. Broker eligibility and live integration require a separate future request.
