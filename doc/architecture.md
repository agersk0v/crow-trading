# Architecture

## Structure
Use a local Python modular monolith. Keep `doc/` as the canonical specification. No server or dashboard is required.

Flow: sources → normalized/validated UTC OHLCV → cache → strategy signals → risk checks → simulated fills → ledger → reports.

Implemented modules under `src/crow_trading/`:
- `data/`: typed candles/metadata, CCXT/yfinance adapters, calendar-aware validation, archive import, immutable Parquet storage.
- `strategies/`: typed signal/protocol, pure momentum and mean reversion, causal replay/latest runner, offline JSON CLI.
- `scanner/`: typed watchlists, calendar due checks, provider I/O/retries, overlap snapshot merges, transactional SQLite progress/JSON reports, expiry-aware JSON/Markdown reports, and a single-run CLI.
- `backtesting/models.py`: Decimal settings, costs, and chronological windows.
- `backtesting/market.py`: as-of EUR-triangulated FX and corporate-action support checks.
- `backtesting/ledger.py`, `risk.py`, `engine.py`: cash/cost basis, entry limits, close-observed risk stops, next-open simulation.
- `backtesting/experiment.py`: frozen configuration/input hashes and protocol versions.
- `backtesting/metrics.py`, `reports.py`: ledger/equity CSV, metrics/benchmarks JSON, and standalone SVG charts.
- Deterministic tests in `tests/`; synthetic examples in `examples/backtesting/`.
- `paper/engine.py`: initialization and policy dispatch; daily step transaction boundaries.
- `paper/quotes.py`, `quote_source.py`, `quote_engine.py`: typed public quotes, bid/ask valuation/evidence, and reserve → observe → revalidate/fill transactions. HTTP runs outside financial write locks.
- `paper/portfolio.py`: portfolio queries, price/quantity arithmetic, marks, and valuation records.
- `paper/orders.py`: order creation, full fills, and pending-order reconciliation.
- `paper/risk.py`, `signals.py`: shared entry/risk checks and scanner-signal validation/decisions.
- `paper/config.py`, `state.py`, `source.py`, `operations.py`: immutable policy, SQLite accounting/reconciliation, read-only scanner inputs/daily-bar adapter, and inspect/control/export.
- `runner/reporting.py`: focused account summaries and incremental session activity; full financial reconciliation remains unchanged.
- `runner/`: foreground scanner → paper orchestration, five-minute completion-relative
  delay, ownership locks for both state roots, and concise session activity output.
- Real broker and accounting/tax integrations remain future work. No live adapters.

## Contracts
- Candle: symbol, venue/source, UTC timestamp, timeframe, OHLCV, currency, adjustment flag, completeness.
- Signal: symbol, strategy ID/version, evaluated candle timestamp, earliest availability, entry/exit/hold intent, reason. No size or broker state.
- Order intent: symbol, side, quantity/notional, requested timestamp, mode, idempotency key.
- Fill: symbol, side, quantity, price, commission/currency, timestamp, venue, order ID.
- Ledger: intent/status/reason, fills, cash/quantity deltas, balances, fees, cost basis, realized P&L, and applied FX. Equity snapshots separately record unrealized P&L and drawdown.
- Experiment: immutable JSON plan with complete configuration, three windows, strategy/engine versions, dataset fingerprint, and input file hashes. Paths are relative to the plan.

## Invariants
- Strategies consume validated data and return signals; never call brokers.
- Strategies evaluate closed candles only. Execution uses the account's immutable daily-bar or quote policy; quote observations must follow order creation/delay.
- Never fabricate tradable candles by forward-filling missing data.
- Handle empty data, insufficient history/cash, stale prices, wide spreads, and rejected orders. Partial fills are unsupported.
- Crypto: model 24/7 trading, venue fees, minimum sizes, and quantity steps. Kraken OHLC history is limited; use archives for longer studies and cross-check venue prices.
- Stocks: support sessions/holidays, quantity constraints, fees, and timestamped FX. Only attested unadjusted no-action windows are executable; splits/dividends and adjusted fills are rejected. Delisting/survivorship coverage is not established by a price snapshot.
- Compare markets over explicit, comparable horizons. Freeze experiment parameters before untouched-test evaluation.
- Keep tax scenarios separate; do not apply a flat tax deduction to every candle.

## Scanner contracts
An external scheduler starts the single-run CLI, or the foreground main runner
invokes scanner and paper sequentially; no scheduler or portfolio logic lives in strategies. SQLite commits progress, signal identities, and JSON reports together under an exclusive write transaction. Identical failure-only scans update a compact streak summary. Optional report files are rendered/exported on request; automatic repair/conflict checks run only when explicitly enabled. Instrument savepoints isolate provider failures. Immutable source/merged snapshots retain lineage; updates detect revisions only in their fetched overlap.

Fresh candidate summaries exclude historical catch-up, revisions, expired records, and offline fixtures. Full snapshot fingerprints preserve provenance while trailing-content hashes prevent duplicates when unrelated history grows. Market calendars and UTC deadlines drive scheduling regardless of the host timezone. See [scanner](scanner.md) for exact state, timing, retry, and recovery contracts. Windows integration is documented, not executed on this Linux host.

## Paper contracts
Each paper account selects exactly one strategy and shares cash/risk across its allowed assets. The optional version-2 quote policy supports Kraken BTC/EUR only and requires new IDs; original daily-bar policy hashes remain unchanged. Different strategy accounts have separate virtual funding. Scanner records are read in a committed snapshot; paper decisions acknowledge them atomically with orders and reservations in a separate SQLite database. No source progress/report data is changed.

The version-1 adapter simulates full fills at the first scheduled open after actual order creation, confirming only after that daily bar is published. It never uses an earlier open. Inputs, effective/recorded fill times, fees, FX, balances, cost basis, and decisions remain auditable. Unknown states fail reconciliation before more orders are processed. Manual stop cancels entries but leaves exits/reconciliation active. See [paper contracts](paper.md) for daily policy and controls, and [quote execution](quotes.md) for public timestamp limitations, evidence, and two-stage processing.

## Backtesting simulation limits
Each backtest dataset, evaluation window, and benchmark has an independent one-symbol account. No multi-asset portfolio or shared exposure accounting is implied. Indicators may warm from earlier bars, but cash and holdings reset per window. Full fills occur at eligible bar opens; no intrabar execution or guaranteed stop prices.

Stock action coverage is mandatory. Only unadjusted, attested no-action windows are supported; splits/dividends and adjusted-price execution fail. FX snapshots must supply rates already available at each fill/mark and pass age checks. See [backtesting contracts](backtesting.md).

## Dependencies
Pinned runtime dependencies: CCXT, yfinance, pandas, exchange-calendars, pyarrow, and Matplotlib. Python 3.12+, Ruff, mypy, and pytest support development; `uv.lock` records transitive versions. Simulation arithmetic uses standard-library Decimal. No broker clients are introduced.

Live execution needs a separate explicit request, disabled-by-default opt-in, preflight checks, reconciliation, exposure caps, a kill switch, and no withdrawal permissions. Follow [agent rules](../AGENTS.md).

See [runner contracts](runner.md) for shutdown, retry, locking, and output limits.

See [storage ownership, schema upgrades, and retention](storage.md). Paper input
archives may share immutable file data through hard links, with verified-copy
fallback; full cumulative history snapshots and reconciliation remain.
