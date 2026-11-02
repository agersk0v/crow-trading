# Crow Trading

Local crypto/stock research and paper trading. Strategies check completed daily
candles; the paper trader manages simulated cash, orders, and positions. No real
orders or broker credentials are used.

## Run

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from
the repository root:

```sh
uv sync --locked
uv run --locked python -m crow_trading.runner examples/paper/paper.json
```

Review the configuration first. The example creates two independent virtual
EUR 1,000 accounts and paper-trades BTC/EUR. The scanner also watches AAPL;
stock paper trading needs additional FX and corporate-action configuration.

The runner scans immediately, processes paper orders, prints trades/account
values, then waits five minutes before repeating. Keep the computer awake and
process running. Press Ctrl+C to stop; run the same command to resume saved
accounts without resetting balances. Disable any old scanner scheduled task
before using this workflow. No tasks are changed automatically.

Daily-bar fills remain delayed: checking every five minutes does not fill orders
at live prices. Read [runner commands](doc/runner.md) and [paper contracts](doc/paper.md).

## Use current quotes for BTC/EUR paper trades

```sh
uv run --locked python -m crow_trading.runner examples/paper/quotes.json
```

This selects a new policy and separate virtual accounts under `data/paper-quotes/`.
Daily strategies produce signals; eligible orders can fill from a fresh later
bid/ask quote in the same cycle. Existing daily-bar accounts are unchanged. Read
[quote execution, fees, timestamp limitations, and testing](doc/quotes.md) first.
No real orders are sent.

## Offline check

```sh
uv run --locked python examples/paper/demo.py --output data/paper-demo
uv run --locked python -m crow_trading.paper inspect --state data/paper-demo/paper-state --section fills
```

Use a new output directory. This synthetic buy/sell example verifies accounting;
it does not demonstrate profitability.

## State and reports

SQLite holds scanner records and paper accounts. Report files are optional:

```sh
uv run --locked python -m crow_trading.scanner export --state data/scanner --output data/scanner-export-001
```

The output directory must be new. See [storage, backups, and safe cleanup](doc/storage.md).
Never edit database rows or referenced market-data files.

## Other commands

- [Historical data](doc/data.md): fetch, validate, inspect Parquet datasets.
- [Strategies](doc/strategies.md): fixed rules and historical signal replay.
- [Backtesting](doc/backtesting.md): frozen experiments, costs, benchmarks, and charts.
- [Standalone scanner](doc/scanner.md): one-shot scans, optional reports, alternate scheduling.
- [Architecture](doc/architecture.md): module boundaries and supported behavior.
- [Implementation plan](doc/implementation.md): next is paper-workflow validation/results, followed by stocks and storage scaling.

## Development

Baseline: Python 3.12 and uv 0.12.23. Dependencies are pinned in `pyproject.toml`
and `uv.lock`. No network, credentials, or market data are needed for tests.

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
```

These checks test the software, not strategy profitability. GitHub Actions runs
the same checks. In a read-only-cache environment, set
`UV_CACHE_DIR=/tmp/crow-uv-cache` first. After intentional dependency changes,
run `uv lock` and review the diff. Keep local data under ignored `data/`; never
commit secrets. Follow [AGENTS.md](AGENTS.md): linked issues/PRs, human merges only.
