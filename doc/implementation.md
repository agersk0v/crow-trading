# Implementation

Status: phases 0–2 implemented; phase 3 supports the explicitly limited simulation workflow below. Complete phases in order through linked issues and PRs. AI agents must never merge.

## 0. Foundation
- [x] Add `pyproject.toml`, Python version policy, dependency locking, `.gitignore`, and README commands.
- [x] Create `src/crow_trading/` and `tests/`.
- [x] Configure Ruff, typing, pytest, and GitHub Actions without secrets.
- Done when: install, lint, type checks, and tests pass locally and in CI.

## 1. Historical data
- [x] Add CCXT ingestion with pagination/rate limits and an archive option; add yfinance daily research data.
- [x] Normalize UTC OHLCV; check closed candles, duplicates, gaps, calendars, unsupported timeframes, and source limitations.
- [x] Cache Parquet with provenance and reproducible retrieval.
- Done when: BTC/EUR, ETH/EUR, and one stock can be fetched, validated, cached, and reloaded with tests.

## 2. Strategies
- [x] Define a pure strategy protocol and fixed-parameter momentum-breakout and mean-reversion strategies.
- [x] Start with daily closed bars; test synthetic series, boundaries, and look-ahead prevention.
- Done when: identical strategy code produces repeatable signals for both markets without broker calls.

## 3. Backtesting
- [x] Implement long-only cash/positions, next-bar fills, €50 position targets, and risk limits.
- [x] Model calendars, fees, spread, slippage, quantity constraints, and timestamped FX. Require stock action coverage; reject unsupported splits/dividends and adjusted-price fills.
- [x] Add ledger, metrics, buy-and-hold/cash benchmarks, and train/validation/untouched-test splits.
- [x] Test fills, costs, insufficient cash, rejections, missing data, and independent portfolios.
- Delivered: offline crypto/stock synthetic examples produce ledger/trades CSV, frozen configuration/input hashes, metrics, benchmark comparisons, equity/drawdown SVG, explicit windows/costs/protocol. See [backtesting](backtesting.md). Synthetic results are functional evidence only.

### Remaining simulation limitations
- [ ] Implement and validate split/dividend accounting before accepting windows containing corporate actions; current runs reject them.
- [ ] Add partial fills, liquidity/volume constraints, corporate-action cash timing, and more detailed FX execution costs if required by later research.
- [ ] Multi-asset/shared portfolio simulation is not implemented; current comparisons use independent single-symbol accounts.

## 4. Paper trading
- [ ] Reuse strategy/risk/ledger contracts with simulated fills only.
- [ ] Persist state, process closed candles, prevent duplicate orders, enforce caps, and support manual stop.
- [ ] Log stale/missing data and compare paper fills with modeled assumptions.
- Done when: end-to-end execution survives restarts, reconstructs every trade, and places zero real orders.

## Deferred: live trading
Do not implement or activate live trading during MVP work. A separate explicit request must resolve Danish broker eligibility, API terms, fees, precision, tax reporting, credentials, reconciliation, and kill-switch requirements.

See [product requirements](product.md), [architecture](architecture.md), and [agent rules](../AGENTS.md).
