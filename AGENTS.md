# Agent instructions

## Read first
- [Product](doc/product.md): scope and acceptance criteria.
- [Architecture](doc/architecture.md): boundaries and data rules.
- [Implementation](doc/implementation.md): ordered tasks.
- Current state: historical data, pure strategies, daily watchlist scanning, and offline single-asset backtesting. See [scanner contracts](doc/scanner.md). Local paper trading is implemented with simulated accounts/orders only; see [paper contracts](doc/paper.md). See [backtesting limits](doc/backtesting.md). The foreground [main runner](doc/runner.md) combines scanner/paper cycles with persistent-account resume and console summaries; it does not change fill timing or install schedules.

## GitHub rules
- Use GitHub issues and pull requests for all changes.
- Create or use an issue, work on a feature branch, and open a linked pull request.
- Never merge a pull request, enable auto-merge, or merge/push changes directly into the default branch. Only the human user merges.
- Do not bypass these rules through Git commands, APIs, automation, or another agent.
- Do not change repository settings, branch protections, or publish releases.

## Paper/runner maintenance
- Preserve `paper.engine.initialize` and `step` as transaction entry points. Portfolio, orders, risk, and signal decisions live in focused modules.
- Runner summaries use incremental activity reads; never weaken ledger reconciliation or treat console cursors as financial acknowledgements.
- Share synthetic market fixtures via `tests/helpers/paper.py`, not imports between test modules.

## Work rules
- Inspect relevant files and existing tests before editing.
- Make the smallest change that satisfies the issue. Preserve unrelated user changes.
- Use Python 3.12+, typed interfaces, simple modules, and deterministic tests.
- Pin dependencies and document reproducible commands.
- Add tests for behavior changes. Run available lint, type checks, and relevant tests; report failures and checks not run.
- Update README and relevant `doc/` files in every implementation PR. Keep `doc/implementation.md` and this file current; state unsupported scenarios explicitly.
- Do not silently alter datasets, frozen experiment plans, or published results. Bump strategy/engine/scanner/paper versions when their rules or semantics change.

## Safety rules
- Research and paper trading only. Do not enable live orders or connect production broker credentials without a separate explicit user request.
- No leverage, margin, shorting, derivatives, or automatic deposits/withdrawals in v1.
- €50 is a per-position simulation target, not trading permission. Enforce configurable position, exposure, and loss limits.
- Never commit or log secrets, tokens, credentials, or personal account details. Use environment variables or an ignored local `.env`.
- Do not activate scheduled tasks, external notifications, or deployments as part of scanner implementation without a separate explicit request.
- Strategies must not call broker APIs. Backtests must not call real brokers.
- Use closed candles and prices available after the signal. Prevent look-ahead and survivorship bias.
- Do not claim profitability from in-sample results. Report costs, sample size, drawdown, benchmarks, and out-of-sample results.
- Keep auditable trade and FX records for Danish tax reporting. Label tax estimates as informational; withdrawal is not the taxable-event trigger.
