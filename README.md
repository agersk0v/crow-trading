# Crow Trading

Local Python research and paper trading for crypto and stock strategies. The
current implementation caches daily historical data and evaluates momentum
and mean-reversion signals, and simulates historical single-asset trades offline.
See [data commands](doc/data.md), [strategy rules](doc/strategies.md), and
[backtesting commands and limitations](doc/backtesting.md). Paper/live execution
is not implemented.
Read [AGENTS.md](AGENTS.md) and the [implementation plan](doc/implementation.md)
before making changes. Work through issues and PRs; only the human user merges.

## Development

Use Python 3.12 (the baseline tested in CI) and uv 0.12.23. The package declares
Python 3.12+ compatibility; later Python versions are not yet tested in CI.
Install uv using its [official instructions](https://docs.astral.sh/uv/getting-started/installation/).
If the default cache directory is read-only (as in this cloud environment),
first run `export UV_CACHE_DIR=/tmp/crow-uv-cache`.
Run all commands from the repository root:

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
```

`uv sync` creates `.venv` and installs the package and development tools. uv uses
`.python-version` to select Python 3.12 and can download it if needed. No API keys,
broker credentials, services, or market data are required. Tests verify package installation, provider adapters, validation, and storage
using offline fixtures. Public-provider smoke checks are documented separately.

`pyproject.toml` pins development tools; `uv.lock` locks transitive dependencies.
After an intentional dependency change, run `uv lock`, review the lockfile diff,
and rerun the checks. Do not manually edit the lockfile. Runtime dependencies include CCXT, yfinance, pandas, exchange-calendars, and
pyarrow, and Matplotlib. Default tests require no network or credentials.

GitHub Actions runs these same checks on Python 3.12. Keep local data under the
ignored `data/` directory and secrets in environment variables or an ignored
`.env` file. Live trading is outside the MVP scope.

## Try an offline backtest

Create synthetic demo data and a configuration, freeze its exact inputs, then run:

```sh
uv run --locked python examples/backtesting/make_demo.py --output data/backtest-demo
uv run --locked python -m crow_trading.backtesting freeze data/backtest-demo/crypto.json --output data/backtest-demo/crypto-plan.json
uv run --locked python -m crow_trading.backtesting run data/backtest-demo/crypto-plan.json --output data/backtest-demo/crypto-run
```

Open `data/backtest-demo/crypto-run/report.json` and the three
`*-equity-drawdown.svg` charts. Repeat with `stock.json`, `stock-plan.json`, and
`stock-run` for the FX/stock-session example. All output paths must be new.
These fixtures are synthetic functional examples, not market-performance results.
Use documented verified inputs and market assumptions for actual research.
