# Implementation

Status: phases 0–4, the scanner milestone, and the main runner are implemented; simulation limits remain explicit below. Complete phases in order through linked issues and PRs. AI agents must never merge.

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
- [ ] Multi-asset/shared portfolio backtesting is not implemented; backtest comparisons use independent single-symbol accounts. Local paper accounts support shared cash/risk across assets.

## 3.5. Daily watchlist scanner (issue #9)
- [x] Typed crypto/stock watchlists and versioned strategy configuration.
- [x] UTC/session-aware due checks, publication delays, closed-bar warm-up, overlap updates, and traceable data revisions.
- [x] SQLite locking, atomic progress/report commit, restart repair, and duplicate prevention.
- [x] Separate fresh, catch-up, revision, and fixture results with local JSON/Markdown reports and partial-failure status.
- [x] Single-run CLI, offline demo, provider smoke check, and scheduler-ready Windows/cron instructions.
- Delivered: [scanner commands/contracts](scanner.md). No task is activated automatically. Windows Task Scheduler integration remains unverified on the Linux development host.

## 4. Local paper trading (issue #11)
- [x] Read scanner signals without modifying its state; preserve strategy/data identity and reject stale, revised, catch-up, or mode-mismatched records.
- [x] Persist isolated strategy accounts with shared multi-asset cash/reservations/risk inside each account.
- [x] Implement idempotent full simulated orders/fills, ledger, restart reconciliation, and manual stop/resume/cancel.
- [x] Respect actual receipt time, later-session opens, public-data delays, market minimums, fees, FX, and no-action stock coverage.
- [x] Provide one-shot worker, inspect/export/control CLI, immutable input archives, schema guards, and synthetic end-to-end tests.
- Delivered: [local paper workflow](paper.md). No real orders, broker APIs, active schedules, remote APIs, or deployment infrastructure.
- Remaining: real-time quote execution, partial fills/liquidity/settlement, and split/dividend accounting are unsupported. Daily-bar confirmations are approximations, not real broker fills.

## 4.5. Main runner (issue #13)
- [x] Foreground command initializes/resumes accounts and runs scanner → paper immediately.
- [x] Configurable five-minute delay after completion, no overlapping/catch-up cycles, ownership locks on both state directories.
- [x] Print new orders/fills and account summaries with valuation freshness and explicit errors.
- [x] Test offline buy/sell/restart, failures, concurrency, process-death lock release, and interrupt rollback.
- Delivered: [runner commands/contracts](runner.md). Daily strategies and fill semantics unchanged; no active tasks or services installed.

## Next steps

The milestones below are planned, not implemented. Start with phase 5; create a
linked issue and PR for each bounded change. Do not change strategy parameters,
reset existing accounts, or activate live trading as part of these milestones.

### 5. Quote-based crypto paper execution — next
- [ ] Keep strategy evaluation on completed daily candles; start with Kraken BTC/EUR only.
- [ ] Fetch timestamped public bid/ask quotes separately from daily strategy data. Reject stale, invalid, or unavailable quotes; never use a quote observed before order creation for a new fill.
- [ ] Add an explicit quote-based paper fill policy: buy at ask, sell at bid, apply configured fees/slippage and market quantity/minimum rules without double-counting spread. Full fills remain a documented approximation; no liquidity or partial-fill claim.
- [ ] Use fresh quotes for paper holdings valuation and risk observation. Fetch network data outside financial write transactions; archive the evidence used for fills/marks.
- [ ] Preserve the existing daily-bar policy for existing accounts. Version the new policy and require a new account ID rather than silently converting saved experiments.
- [ ] Test timing, stale quotes, fees, reservations, risk limits, restart/deduplication, and provider outages with deterministic offline fixtures. Update commands and contracts.
- Done when: a daily signal can create a simulated order and fill from a valid subsequent quote in the same runner cycle, without waiting for another daily candle to close or placing a real order.

### 6. Validate the ongoing paper workflow and results
- [ ] Add an offline end-to-end quote-policy demo covering entry, holding, exit, outages, and restart.
- [ ] Provide a concise per-account results view: initial/current equity, realized/unrealized P&L, fees, drawdown, trade count, valuation freshness, and observation period. Reuse existing ledger/valuation data where sufficient; explicitly add any missing evidence.
- [ ] Define a repeatable public-data observation procedure and manually verify the runner on the intended Windows machine. Keep scheduling opt-in.
- [ ] Compare paper and backtest outcomes only where data, costs, timing, and portfolio assumptions align; explain differences rather than asserting equivalent fills or profitability.
- Done when: the workflow survives interruption/outages without duplicate funding or trades, reconciles after restart, and explains each result from recorded inputs. A short run is functional validation, not proof of strategy quality.

### 7. Extend realistic paper execution to stocks
- [ ] Select a suitable stock quote source and verify its timestamps, delays, usage terms, sessions, and availability before integration; do not assume daily Yahoo research data is a live executable quote.
- [ ] Supply timestamped FX and explicit venue costs/minimums. Continue rejecting adjusted-price fills and unsupported corporate-action windows.
- [ ] Implement and test split/dividend position and cash accounting before supporting affected holdings; define how missing action coverage blocks execution.
- [ ] Validate holidays, session boundaries, overnight gaps, stale quotes, FX conversion, and action handling with offline fixtures before adding a documented stock example.
- Done when: a stock paper account can demonstrate a reconciled buy/hold/sell cycle with traceable quote/FX/action evidence and explicit remaining settlement/liquidity limitations.

### 8. Measure and control storage growth
- [ ] Measure snapshot size, signal/report history, and reconciliation time on representative watchlists before selecting a storage redesign. Investigate earlier if observed growth affects operation.
- [ ] If needed, introduce versioned partitioned/shared history without changing old fingerprints or breaking frozen experiments and paper evidence; provide a tested migration path.
- [ ] Add a reference-aware dry-run inventory before any pruning command. Preserve active state, pending-order inputs, lineage, financial records, and published experiment dependencies; make deletion an explicit user action.
- Done when: measured growth has a documented budget and any migration/pruning tool proves that retained experiments and accounts still replay/reconcile. No financial records expire automatically.

## Deferred: live trading
Do not implement or activate live trading during MVP work. A separate explicit request must resolve Danish broker eligibility, API terms, fees, precision, tax reporting, credentials, reconciliation, and kill-switch requirements.

See [product requirements](product.md), [architecture](architecture.md), and [agent rules](../AGENTS.md).
