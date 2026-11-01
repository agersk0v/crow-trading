# Crow Trading

Local Python research and paper trading for crypto and stock strategies. The
current implementation fetches, validates, and caches daily historical data.
See [data commands and contracts](doc/data.md). Trading is not implemented.
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
pyarrow. Default tests require no network or credentials.

GitHub Actions runs these same checks on Python 3.12. Keep local data under the
ignored `data/` directory and secrets in environment variables or an ignored
`.env` file. Live trading is outside the MVP scope.
