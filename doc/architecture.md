# Architecture

## Structure
Use a local Python modular monolith. Keep `doc/` as the canonical specification. No server or dashboard is required.

Flow: sources → normalized/validated UTC OHLCV → cache → strategy signals → risk checks → simulated fills → ledger → reports.

Implemented modules under `src/crow_trading/`:
- `data/`: typed candles/metadata, CCXT/yfinance adapters, calendar-aware validation, archive import, immutable Parquet storage.
- `strategies/`: typed signal/protocol, pure momentum and mean reversion, causal replay/latest runner, offline JSON CLI.
- `scanner/`: typed watchlists, calendar due checks, provider I/O/retries, overlap snapshot merges, transactional SQLite progress/report payloads, expiry-aware JSON/Markdown reports, and a single-run CLI.
- `backtesting/models.py`: Decimal settings, costs, and chronological windows.
- `backtesting/market.py`: as-of EUR-triangulated FX and corporate-action support checks.
- `backtesting/ledger.py`, `risk.py`, `engine.py`: cash/cost basis, entry limits, close-observed risk stops, next-open simulation.
- `backtesting/experiment.py`: frozen configuration/input hashes and protocol versions.
- `backtesting/metrics.py`, `reports.py`: ledger/equity CSV, metrics/benchmarks JSON, and standalone SVG charts.
- Deterministic tests in `tests/`; synthetic examples in `examples/backtesting/`.
- `paper/`: immutable account config, separate transactional SQLite accounting, read-only scanner handoff, archived inputs, daily-bar paper adapter, risk/reservations, reconciliation, and local inspect/control/export commands.
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
- Evaluate closed candles only. Default to next-bar execution after a signal exists.
- Never fabricate tradable candles by forward-filling missing data.
- Handle empty data, insufficient history/cash, stale prices, wide spreads, partial fills, and rejected orders.
- Crypto: model 24/7 trading, venue fees, minimum sizes, and quantity steps. Kraken OHLC history is limited; use archives for longer studies and cross-check venue prices.
- Stocks: model sessions/holidays, splits, dividends, delistings, share precision, minimum commissions, adjusted prices, and USD/EUR/DKK FX. yfinance intraday history is limited.
- Compare markets over explicit, comparable horizons. Freeze experiment parameters before untouched-test evaluation.
- Keep tax scenarios separate; do not apply a flat tax deduction to every candle.

## Scanner contracts
An external scheduler starts the single-run CLI, or the foreground main runner
invokes scanner and paper sequentially; no scheduler or portfolio logic lives in strategies. SQLite commits progress, signal identities, and report content together under an exclusive write transaction. Missing report projections are repaired; conflicting user edits are preserved. Instrument savepoints isolate provider failures. Immutable source/merged snapshots retain lineage; updates detect revisions only in their fetched overlap.

Fresh candidate summaries exclude historical catch-up, revisions, expired records, and offline fixtures. Full snapshot fingerprints preserve provenance while trailing-content hashes prevent duplicates when unrelated history grows. Market calendars and UTC deadlines drive scheduling regardless of the host timezone. See [scanner](scanner.md) for exact state, timing, retry, and recovery contracts. Windows integration is documented, not executed on this Linux host.

## Paper contracts
Each paper account selects exactly one strategy and shares cash/risk across its allowed assets. Different strategy accounts have separate virtual funding. Scanner records are read in a committed snapshot; paper decisions acknowledge them atomically with orders and reservations in a separate SQLite database. No source progress/report data is changed.

The version-1 adapter simulates full fills at the first scheduled open after actual order creation, confirming only after that daily bar is published. It never uses an earlier open. Inputs, effective/recorded fill times, fees, FX, balances, cost basis, and decisions remain auditable. Unknown states fail reconciliation before more orders are processed. Manual stop cancels entries but leaves exits/reconciliation active. See [paper contracts](paper.md) for timing approximations, supported states, migrations, controls, and limitations.

## Simulation limits
Each dataset, evaluation window, and benchmark has an independent one-symbol account. No multi-asset portfolio or shared exposure accounting is implied. Indicators may warm from earlier bars, but cash and holdings reset per window. Full fills occur at eligible bar opens; no intrabar execution or guaranteed stop prices.

Stock action coverage is mandatory. Only unadjusted, attested no-action windows are supported; splits/dividends and adjusted-price execution fail. FX snapshots must supply rates already available at each fill/mark and pass age checks. See [backtesting contracts](backtesting.md).

## Dependencies
Pinned runtime dependencies: CCXT, yfinance, pandas, exchange-calendars, pyarrow, and Matplotlib. Python 3.12+, Ruff, mypy, and pytest support development; `uv.lock` records transitive versions. Simulation arithmetic uses standard-library Decimal. No broker clients are introduced.

Live execution needs a separate explicit request, disabled-by-default opt-in, preflight checks, reconciliation, exposure caps, a kill switch, and no withdrawal permissions. Follow [agent rules](../AGENTS.md).

See [runner contracts](runner.md) for shutdown, retry, locking, and output limits.
