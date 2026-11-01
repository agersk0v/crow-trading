# Daily baseline strategies

Rules and defaults are fixed for version 1 before evaluating results. These are
research baselines, not trained models or evidence of profitability. There is no
parameter search, sizing, portfolio accounting, or execution in this phase.

## Shared contract
- Pure strategies consume a symbol, validated daily `Candle` history, and an
  explicit timezone-aware `as_of`. They perform no I/O and use no wall clock.
  `history_required` declares the maximum trailing bars needed, including `t`;
  replay and latest pass at most that many bars to the same evaluator.
- Input must be ordered, non-overlapping, closed, finite OHLCV. Dataset coverage
  and market calendars are checked by the Phase 1 loader/runner. Strategies count
  observed bars, including stock-session gaps; they never synthesize dates.
  Prefix invariance checks algorithmic causality only: provider revisions or
  retrospective price adjustments are not point-in-time historical guarantees.
- Evaluated candle `t` participates only through its close. Reference windows
  end at `t-1`. Empty history returns `None`; insufficient history returns a
  `hold` intent with reason `insufficient_history`.
- Signals include symbol, strategy ID/version, evaluated candle's UTC open,
  earliest availability (`closed_at`), intent, and reason. Availability is the
  theoretical bar-close boundary, not proof the provider delivered data then.
  A later execution layer must also account for retrieval/processing delay.
- `enter_long` requests entry eligibility, `exit_long` requests closing an
  existing long, and `hold` requests no change. They are position-independent
  conditions, not orders. Repeated entry/exit conditions can repeat intents;
  later risk/execution code must check actual positions and prevent duplicates.
- No shorting; no signal can execute before it is available. Phase 3 will model
  fills after signals with fees, slippage, cash, and risk limits.

## Momentum breakout (`momentum`, version `1`)
Defaults: `entry_lookback=20`, `exit_lookback=10`, both positive integers.
Require `max(entry_lookback, exit_lookback) + 1` bars including `t`.

- Entry: `close[t] > max(high[t-entry_lookback:t])`.
- Exit: `close[t] < min(low[t-exit_lookback:t])`.
- Otherwise hold. Equality at either boundary is hold. Entry has precedence,
  although the two conditions cannot overlap for valid OHLCV windows.
- Example with both lookbacks 2: prior highs 11, 12 and lows 9, 8 give entry
  above 12, exit below 8, and hold at 12 or 8.

## Mean reversion (`mean-reversion`, version `1`)
Defaults: `lookback=20`, `entry_deviation=0.05`. Lookback is a positive integer;
entry deviation is finite and strictly between 0 and 1.
Require `lookback + 1` bars including `t`. Let `mean` be the arithmetic average of
`close[t-lookback:t]` (excluding `t`).

- Entry: `close[t] < mean * (1 - entry_deviation)`.
- Exit: `close[t] >= mean`.
- Otherwise hold. Equality at the entry threshold is hold; equality at mean is
  exit. No position state is read or updated.
- Example with lookback 2, prior closes 100, 100, and deviation 0.05: entry below
  95, hold at 95 or 99, and exit at 100 or above.

## Commands
After `uv sync --locked`, run from the repository root (PowerShell or POSIX).
Fetch/cache data first using [data commands](data.md). Use enough history: the
small three-bar Phase 1 smoke dataset produces warm-up holds with default rules.
These examples fetch a longer explicit window; provider history limits apply.

```sh
uv run --locked python -m crow_trading.data fetch-crypto --symbol "BTC/EUR" --start 2026-09-01 --end 2026-10-23 --currency EUR --venue kraken --output data/btc-strategies.parquet
uv run --locked python -m crow_trading.data fetch-stock --symbol AAPL --start 2026-09-01 --end 2026-10-23 --currency USD --calendar XNYS --output data/aapl-strategies.parquet
uv run --locked python -m crow_trading.strategies data/btc-strategies.parquet --strategy momentum --output data/btc-momentum.json
uv run --locked python -m crow_trading.strategies data/aapl-strategies.parquet --strategy momentum --output data/aapl-momentum.json
uv run --locked python -m crow_trading.strategies data/btc-strategies.parquet --strategy mean-reversion --output data/btc-reversion.json
uv run --locked python -m crow_trading.strategies data/aapl-strategies.parquet --strategy mean-reversion --output data/aapl-reversion.json
uv run --locked python -m crow_trading.strategies data/btc-strategies.parquet --strategy momentum --mode latest
```

Strategy commands run offline. Replay emits one intent per eligible closed bar,
including warm-up holds. Latest emits only the last eligible bar, using the same
calculation and history. `--as-of 2026-10-21T00:00:00Z` restricts either mode to
bars closed by that time; without it, the cutoff is the last cached bar's close.
No eligible bars means an empty signal list. Unclosed bars supplied directly to
a strategy fail instead of being silently used. New data must be fetched into a
new cache file before latest mode can evaluate it; no scheduling is implemented.

Override momentum parameters with `--entry-lookback` and `--exit-lookback`, or
mean-reversion parameters with `--lookback` and `--entry-deviation`. Irrelevant
or invalid parameters fail. Freeze your chosen parameters before inspecting an
untouched test period; do not tune from these example runs.

JSON output records schema version, full dataset fingerprint and request,
strategy ID/version/parameters, mode, cutoff, and timestamped signals. It contains
no generated-at timestamp, so identical inputs/options give identical bytes.
Changing future data changes the full dataset fingerprint but cannot change
past signal records. Outputs create new files only, refusing to overwrite an
existing path, including the input Parquet. Omitting `--output` prints JSON.
Source Parquet files are never modified. A signal report is not a backtest or a
profitability estimate.

## Validation evidence
On 2026-11-01 both default strategies replayed the documented BTC/EUR dataset
(52 closed daily bars) and AAPL dataset (37 sessions). Each produced 20 warm-up
holds, then evaluated its rules. Latest mode matched the final replay signal,
repeated CLI runs produced identical JSON bytes, and input Parquet files stayed
unchanged. These are functional checks, not performance or profitability results.
