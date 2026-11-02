# Main runner

One foreground command loads the configured strategy rules, initializes or resumes
paper accounts, and runs scanner → paper immediately, then repeats. All money and
orders are simulated. Rules are not generated or optimized automatically.

## Start, stop, and resume

From the repository root, install dependencies once with `uv sync --locked`.
Review `examples/paper/paper.json`, its referenced watchlist, costs, and risk limits.
The example funds two independent EUR 1,000 accounts and paper-trades BTC/EUR only.
AAPL is scanned but is not enabled in the example paper accounts.

Disable any existing scheduled scanner task before starting this workflow. Do not
run multiple schedulers or runners against the same state. No task is installed,
disabled, or activated automatically.

```sh
uv run --locked python -m crow_trading.runner examples/paper/paper.json
```

The first cycle starts immediately. Each cycle scans first and immediately runs
paper processing, even if the scan has no changes or some providers fail. Existing
signal freshness, risk, and ledger checks still apply. The runner waits **300
seconds after the cycle finishes** before starting another. A slow cycle delays
the next one; cycles never overlap or queue missed runs. To choose another delay:

```sh
uv run --locked python -m crow_trading.runner examples/paper/paper.json --interval 60
```

Keep the computer awake and this terminal/process running. Press **Ctrl+C** to
exit. Start the same command again to resume saved cash, positions, pending orders,
manual stops, and risk halts. Restarting never tops up or resets an existing
account. Configuration is loaded at startup; restart to load edits. Account policy
changes still require a new account ID under the existing paper contracts.

Stopping the runner pauses processing; it does not cancel orders or liquidate
holdings. For a persistent entry stop while processing continues, use another
terminal between cycles:

```sh
uv run --locked python -m crow_trading.paper stop --state data/paper --account momentum
uv run --locked python -m crow_trading.paper resume --state data/paper --account momentum
```

Manual controls and inspect/export retain their existing CLI. A brief database
lock may require retrying a control. Resume clears only the manual stop, not a
latched risk halt. See [paper controls](paper.md).

## Output

Console lines are timestamped in UTC. Each cycle prints scanner status, new orders,
new simulated fills, a fill count, account summaries, and any errors. An **ORDER**
is a request awaiting execution; a **FILL** records an actual simulated purchase
or sale. Fill lines include quantity, symbol, side, quote price/currency, account
currency fee, and effective/recorded timestamps. Those timestamps can differ.

Each account summary shows its currency, cash, reserved cash, marked holdings,
total equity, valuation time/freshness, pending count, and stop/risk-halt flags.
Reserved cash is already part of cash: never add it to equity. Total equity is
cash plus holdings. A failed valuation leaves the old value explicitly **stale**;
without a valuation, holdings/total are **unavailable**. A current valuation still
uses the existing daily-mark model, not a live quote.

Existing order/fill history is not re-announced when starting. Console output is
session activity, not a durable notification service: an interruption between a
committed trade and printing can omit its announcement. The SQLite ledger and
`paper inspect`/`paper export` remain the audit record.

## Failures and limits

Provider failures, degraded account results, temporary file I/O errors, and
operational database contention are visible and retried after the normal delay.
Scanner failure does not bypass paper data validation. Invalid configuration,
immutable-policy changes, backward clocks, reconciliation failures, and other
unclassified data/program errors stop the runner for investigation. No fabricated
balances or fills are used to keep running.

A separate SQLite ownership lock in each scanner/paper state directory prevents
another main runner sharing either directory. Locks release on normal shutdown
or process death; leftover `runner-lock.sqlite3` files do not mean a runner is
active. One-shot commands retain their own short transaction locks. Do not delete
lock files while a runner is active. Local SQLite storage is required.

Exit codes: `0` Ctrl+C shutdown, `1` fatal error, `3` another runner owns the state.
Financial transactions commit or roll back independently of console delivery.

Five-minute checks do not create five-minute strategies or immediate fills. Daily
closed candles and the **next-closed-daily-open** fill model are unchanged. Faster
quote-based execution is a separate improvement. No broker, real funds, API,
external notification, hosted infrastructure, or background service is enabled.

## Reporting reads

Runner summaries fetch account balances, valuations, pending counts, and only new
order/fill activity. Session-local row cursors advance after successful output;
a busy summary read leaves its activity available for the next cycle. Startup
sets cursors to existing history, preserving the no-replay behavior. These are
console cursors, not financial acknowledgements. Do not manually delete or
rewrite database rows while running.

Full ledger reconciliation still runs before processing and reporting, including
historical accounts outside the selected configuration. It still reads history;
this cleanup reduces reporting copies rather than making total cycle cost
independent of history. Full inspect/export commands remain available.
