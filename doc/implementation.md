# Crow Trading: Implementation Plan

**Status:** planning. The initial repository contains documentation; application implementation is not yet started.

## Phase 0: Repository foundation
- [ ] Add `pyproject.toml`, Python version policy, `.gitignore`, README, and dependency lock strategy.
- [ ] Set up `src/crow_trading/` and `tests/`.
- [ ] Configure Ruff, pytest, typing checks, and GitHub Actions CI. No secrets or broker credentials in CI.
- [ ] Document local environment and reproducible commands.

**Done when:** the skeleton installs locally, lint and tests run cleanly, and CI enforces them.

## Phase 1: Historical data
- [ ] Implement crypto OHLCV ingestion using CCXT with rate limits, pagination where supported, and an archive path for older data.
- [ ] Implement stock historical daily OHLCV ingestion using yfinance for research; document intraday limits and adjusted-price behavior.
- [ ] Normalize UTC timestamps, OHLCV schema, symbol/venue metadata, candle completeness, and duplicate/missing-bar checks.
- [ ] Cache datasets in Parquet and make retrieval reproducible.
- [ ] Test closed-candle handling, unsupported timeframes, incomplete histories, and exchange-calendar differences.

**Done when:** BTC/EUR, ETH/EUR, and at least one stock can be fetched, validated, cached, and reloaded with provenance metadata.

## Phase 2: Baseline strategies
- [ ] Define a pure `Strategy` protocol returning signal intents from validated candles.
- [ ] Implement fixed-parameter momentum breakout and mean-reversion variants.
- [ ] Make the evaluation schedule explicit (initially daily bars; later 4-hour crypto and eligible stock intraday sources).
- [ ] Unit-test signals on synthetic series, including boundary and look-ahead cases.

**Done when:** identical strategy code processes both market types and generates repeatable signals without broker calls.

## Phase 3: Backtest and evaluation
- [ ] Add cash/positions, long-only simulated fills, next-bar execution, market-specific calendars, €50 position target, and risk limits.
- [ ] Model fixed/percentage commissions, spread, slippage, lot/fractional-share constraints, and currency conversion.
- [ ] Record an auditable trade ledger and calculate gross/net P&L, drawdown, benchmark performance, turnover, trade count, and win/loss characteristics.
- [ ] Establish training/validation/untouched-test splits and prohibit in-sample-only profitability claims.
- [ ] Add tests for fills, insufficient cash, costs, rejected orders, missing data, and independent portfolio comparisons.

**Done when:** both markets can be compared using the same strategy with explicit assumptions and reproducible, benchmarked reports.

## Phase 4: Paper trading
- [ ] Build a paper execution adapter with the same intents/ledger as the backtester.
- [ ] Poll only completed candles; persist state and enforce risk/exposure/order limits.
- [ ] Add logs, duplicate-order protection, alerts for missing/stale data, and manual stop.
- [ ] Compare paper fills and operational behavior against modeled backtest assumptions.

**Done when:** the system can run end-to-end with **zero real-money orders**, handle restarts safely, and reconstruct every paper trade.

## Phase 5: Optional live broker integrations (future approval required)
- [ ] Re-evaluate the venue and account eligibility for a Danish user.
- [ ] Review exact API terms, fee schedules, minimum trade sizes, tax treatment and reporting.
- [ ] Design live Kraken spot and IBKR stock adapters with explicit permissions.
- [ ] Require user approval for enabling real trades, no withdrawal scope, explicit environment separation, kill switch, exposure caps, error handling and order reconciliation.
- [ ] Perform small-scale controlled validation only after paper-trading evidence.

**Do not implement or activate phase 5 as part of the initial setup.**

## Initial measurable research experiment
- Capital: **€50 notional per position**; total account size and loss budget to be chosen later.
- Assets: BTC/EUR, ETH/EUR, and a small liquid stock sample.
- Timeframe: daily bars first; expand to intraday only with adequate historical depth and quality.
- Hypotheses: momentum breakout vs. mean reversion; predeclare parameters.
- Baselines: per-asset buy-and-hold plus cash, with comparable funding and costs.
- Outputs: trades CSV, run configuration and dataset fingerprints, performance table, and drawdown chart.
- Decision rule: do not proceed to live execution without robust out-of-sample results after realistic costs; finding no edge is acceptable.

## Open decisions
Stock universe and exchanges; stock data licensing; exact starting capital and maximum loss; crypto execution venue; allowed holding time and signals; broker availability and permissions; tax/export requirements.

See [product.md](product.md), [architecture.md](architecture.md), and [../AGENTS.md](../AGENTS.md).
