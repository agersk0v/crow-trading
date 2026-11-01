# Crow Trading: Architecture

## Principles
Python-first, modular monolith, local execution, minimal infrastructure, typed boundaries, reproducible research. Separate **data ingestion**, **signal generation**, **simulation**, **risk**, and **execution**. Backtests must never call real brokers.

## Logical flow
```text
  CCXT / exchange archives       yfinance / stock provider
              |                              |
              +---------- ingestion ---------+
                             |
                 normalized UTC OHLCV
                             |
                   validation + Parquet
                             |
              strategies -> signal intents
                             |
                       risk policy
                             |
             +---------------+---------------+
             |                               |
        backtest fills                paper fills (later)
             |                               |
             +---------------+---------------+
                             |
                   ledger + analytics
                             |
                    reports / exports

  Future only: broker adapters for Kraken and IBKR
  must require separate explicit live-trading authorization.
```

## Modules (proposed)
```text
crow_trading/
  config.py               # validated configuration; safe defaults
  models.py               # Candle, Signal, OrderIntent, Fill, Trade
  data/
    crypto.py             # CCXT and optional historical archives
    stocks.py             # yfinance research source
    normalize.py          # UTC OHLCV schema and validation
    storage.py            # Parquet + source metadata
  strategies/
    base.py               # pure strategy protocol
    momentum.py
    mean_reversion.py
  backtesting/
    engine.py             # order/fill, portfolio, cash simulation
    metrics.py
    reports.py
  execution/
    paper.py              # deterministic simulated broker
    risk.py               # per-position, exposure and loss controls
    # live adapters deferred until explicitly authorized
  accounting/
    ledger.py             # auditable trade records
    tax_export.py         # records and FX history, not tax filing
tests/
docs/                     # optional generated research reports
doc/                      # maintained project plans
```
The package layout is illustrative; implementation may adapt it. Keep the requested `doc/` directory as the canonical specification.

## Core data contracts
- **Candle:** symbol, venue/source, timestamp (UTC), timeframe, open, high, low, close, volume, currency, adjusted flag, completeness.
- **Signal:** symbol, strategy ID/version, evaluated candle timestamp, action, reason, optional target quantity; no exchange credentials.
- **Order intent:** symbol, side, notional/quantity, requested timestamp, execution mode, idempotency key.
- **Fill:** symbol, side, executed quantity and price, commission and currency, execution timestamp, venue, order ID.
- **Ledger:** all fills and cash changes, cost basis inputs, FX conversion source/rate, fees, balances.

## Market-specific behavior
- **Crypto:** 24/7 market; exchange-specific OHLCV conventions, trading fees, minimum amounts and step sizes; Kraken OHLC history is limited, so use an archival provider for longer 4-hour studies and cross-check execution venue prices.
- **Stocks:** exchange sessions/holidays, splits, dividends, delistings, share precision, minimum commission, USD/EUR/DKK conversion and adjusted data. yfinance is a prototyping source, not a guaranteed production-grade intraday history.
- A daily close strategy must simulate fills **after** the signal exists. Do not assume filling at the same closing price unless modeling an order submitted before the close using only then-available information.
- Use comparable calendars and clearly labeled measurement horizons when cross-market benchmarking.

## Backtesting integrity
- Lock experiment definitions before looking at untouched test periods.
- Prevent missing-data forward-fills that create fictional tradable candles.
- Handle no fills, insufficient history, stale prices, wide spreads, and partial or rejected orders.
- Report results both before tax and with separately estimated tax scenarios. Never subtract an arbitrary constant tax rate from portfolio equity as if tax occurred on each candle.

## Security and deployment
- Local-only for MVP; no open ports or external dashboard required.
- Public market-data requests need no trading credentials.
- Never store or commit secrets; live credentials, if added later, are scoped to trading without withdrawal permissions.
- Live execution must require explicit opt-in, a disabled-by-default config flag, preflight checks, persistent order reconciliation, and a kill switch.

## Technology candidates
Python 3.12+, pandas, NumPy, CCXT, yfinance, Parquet/pyarrow, pytest, Ruff, and Backtesting.py (or a custom deterministic simulator if fill semantics cannot meet requirements). IBKR/ib_async and Kraken trading clients are **deferred** integrations, not MVP dependencies. SQLite may store structured trade events; Streamlit is optional, not required.
