# Local paper trading (version 1)

This module consumes scanner signals into persistent **simulated** accounts.
There are no broker adapters, production credentials, real orders, APIs, database
servers, containers, or deployments. All state lives in local SQLite and immutable
input files. Existing scanner jobs do not automatically start paper processing.
The [main runner](runner.md) initializes/resumes accounts and repeats scanner →
paper together in one foreground process; existing commands below remain available.

## Test the complete workflow offline
From the repository root after `uv sync --locked`:

```sh
uv run --locked python examples/paper/demo.py --output data/paper-demo
uv run --locked python -m crow_trading.paper inspect --state data/paper-demo/paper-state --section accounts
uv run --locked python -m crow_trading.paper inspect --state data/paper-demo/paper-state --section fills
```

Use a new output directory each time. The demo constructs labelled synthetic
prices, advances an explicit January 2025 clock, runs scanner/worker steps across
separate database connections, and exports `data/paper-demo/report/`. It produces
one buy and one sell: EUR 1,000 starting cash, EUR 51.50 entry spend including
fees, EUR 73.25 net sale proceeds, EUR 1,021.75 final cash. These hand-defined
numbers verify accounting only; they are not investment-performance evidence.

`mode: fixture` is an explicit opt-in for synthetic/offline scanner records. A
normal `mode: paper` account rejects fixture records, even if their timestamps
are recent. `--as-of` is permitted on init/step only in fixture mode. Do not run
the completed demo with arbitrary clocks; recreate it in a new directory to
repeat the controlled timeline. Inspect/export commands do not advance it.

## Start using public-market scanner data
Review both configuration files and all illustrative costs first:

```sh
uv run --locked python -m crow_trading.paper init examples/paper/paper.json
uv run --locked python -m crow_trading.scanner examples/scanner/watchlist.json
uv run --locked python -m crow_trading.paper step examples/paper/paper.json
uv run --locked python -m crow_trading.paper inspect --state data/paper --section accounts
uv run --locked python -m crow_trading.paper inspect --state data/paper --section orders
```

The example creates two independent virtual EUR 1,000 accounts, one per strategy,
using BTC/EUR only. Their money is separate, not a shared EUR 1,000 wallet.
Within each account, cash, reservations, position count, and exposure are shared
across its configured assets. This prevents one strategy's exit from selling a
position owned by another. Source strategies/versions/parameters must match the
watchlist exactly. No fees, precision, or minimum-size defaults are advertised as
current venue rules: verify them before meaningful research.

`init` is idempotent and never adds money again to an existing account. Account
policy is immutable: changing capital, strategy parameters, risk limits, assets,
or mode requires a new account ID or a future explicit migration. Old accounts,
inputs, and reports remain untouched. Paths are relative to the configuration.
FX/action input contents may be refreshed at the configured locations; every
used version is content-hashed and copied to `data/paper/inputs/`. Immutable price archives use hard links when supported,
with verified-copy fallback; see [storage rules](storage.md).

An initial step may produce only ignored holds/exits or expired-signal decisions.
That is valid. A fresh entry creates a pending simulated order and reserves
buying power; it does not create an immediate fill. See the timing model below.

Run a scanner tick and then a paper step when new observations are available.
Both commands exit. The combined runner invokes them every five minutes after
completion; separate one-shot invocations every 15 minutes also suit daily bars. A scanner partial failure does not invent fresh data: the paper worker
validates signals and marks independently. Do not change or activate your
existing scheduled task without deciding explicitly to enable paper processing.
No task, daemon, notification, or unattended deployment is installed by this PR.

## Inspect, export, and control

```sh
uv run --locked python -m crow_trading.paper inspect --state data/paper --section positions
uv run --locked python -m crow_trading.paper inspect --state data/paper --section decisions
uv run --locked python -m crow_trading.paper inspect --state data/paper --section valuations
uv run --locked python -m crow_trading.paper export --state data/paper --output data/paper-export-001
uv run --locked python -m crow_trading.paper stop --state data/paper --account momentum
uv run --locked python -m crow_trading.paper resume --state data/paper --account momentum
uv run --locked python -m crow_trading.paper cancel --state data/paper --order ORDER_ID
```

Replace the order ID using inspect output. Exports require a new directory and
contain `paper-report.json` plus account, position, order, fill, decision, event,
and valuation CSV files. Inspect defaults to accounts; `--section all` includes
the full state. Money/quantities are Decimal strings. Valuations include equity,
realized/unrealized P&L, paid fees/impact, and source/FX fingerprints. Missing or
stale marks leave the prior valuation available with its original timestamp;
the current worker result reports degradation, not a fabricated new value.

Stop is persistent: it cancels pending buys and releases reservations, prevents
new entries, and leaves reconciliation and existing exits active. It does not
liquidate positions automatically. Resume clears the manual stop only; a latched
loss/drawdown halt remains. Review the account and use a new account if you need
a deliberately reset experiment. Previously ignored signals are not replayed
just because you resume. Manual cancellation affects pending orders only.
Controls work directly from the state directory even if market input files are
unavailable. Unknown/filled orders cannot be silently reset or resubmitted.

Exit codes: `0` success, `1` invalid configuration/degraded data/reconciliation
failure, `3` another worker owns the SQLite write lock. Operational results/errors
are JSON on stdout/stderr. There are no external notifications.

## Configuration and account rules
`examples/paper/paper.json` is complete. Required top-level fields:
`schema_version: 1`, `mode` (`paper` or `fixture`), `fill_policy:
next_closed_daily_open`, `state_dir`, `watchlist`, `fx_snapshot`, and `accounts`.

Each account supplies a unique name, one versioned strategy definition, explicit
[backtest settings](backtesting.md#configuration), `max_positions`,
`mark_max_age_seconds`, `signal_max_age_seconds`, and a nonempty asset list.
Each asset references a watchlist name, market-specific costs/quantity limits,
and a corporate-action file when it is a stock. Only full long-position fills
are supported. Partial fills, live broker open/submitted/unknown states, margin,
shorting, leverage, derivatives, deposits, and withdrawals are unsupported.

A signal is considered only by accounts selecting its market identity and exact
strategy definition. Entry/exit/hold output is recomputed from its archived input
window before acceptance. Reject expired, superseded, catch-up, revision, and
mode-mismatched records. A still-fresh signal generated before account creation
may be accepted, but its order creation time is the worker's current receipt time,
never the old candle time. Revisions cancel corresponding pending orders; filled
history is never rewritten. Subsequent revision records are audited, not traded.

Repeated entries while holding or awaiting an order cannot pyramid purchases.
Exits while flat do nothing. A strategy exit cancels an unfilled entry rather
than creating a short position. Each account/asset has at most one pending order.
Order and fill IDs are deterministic and unique; signal decisions are acknowledged
only in the same transaction as their durable outcomes. Temporary input problems
can defer a decision for retry while it remains fresh; permanent risk/eligibility
rejections wait for a genuinely new signal.

## Daily-bar fill model and reservations
Orders remain `pending` until a confirmed daily bar supplies the **first scheduled
open strictly after order creation plus configured delivery delay**. An order
received at 00:16 UTC cannot use that day's earlier 00:00 crypto open. A Friday
after-close stock order can target the following session, skipping holidays and
weekends. The selected bar must close and pass the scanner's publication delay
before its open price is observable to this worker. Missing first-bar coverage
defers execution; it never silently substitutes a later bar.

This is a delayed-confirmation bar simulator, not real-time broker execution.
State changes are booked when the bar is observed; fills retain both simulated
`effective_at` and actual processing `recorded_at`. Orders remain cancellable
until confirmation. Confirmation-time account/risk checks can reject an order
that a real broker might already have filled. It does not model intrabar stops,
order-book liquidity, settlement, or partial fills. Do not present these results
as executable historical quotes or accurate live fills.

The EUR target, fee formula, adverse half-spread/slippage, quantity flooring,
minimums, and entry position cap match the backtest contracts. Entry reserves the
full target/estimated-fee budget in account currency, including pending-position
slots and conservative projected exposure. Reservations do not reduce equity;
they reduce spendable cash. Quantity is finalized at the eligible open. A fill
cannot spend more than its own reservation or borrow cash arriving later; FX
moves that make the target exceed the reservation reject the order. Unused funds
are released on fill/rejection/cancellation. Fees and FX are applied at the
simulated fill time using only rates already available then.

Signal expiry governs initial acceptance, not the legitimate wait for an accepted
order's next session; a queued Friday order need not expire on Saturday. Missing
observations keep it pending with a reported error. Cancel it explicitly if you
want to abandon it. Source rebootstrap or outages cannot backdate a new order or
skip missing execution evidence.

## Marking, risk, FX, and stock actions
Shared equity is cash plus marked holdings. New entries include existing holdings
and reservations in count/exposure checks and require current expected-session
marks for all held assets. Missing/stale prices or FX block new entries and defer
unpriceable fills; they do not replace prices with zero. Historical fill checks
use opening/previously closed prices available at the simulated open. Marking and
risk controls are discrete observations, not guarantees of maximum loss.

Loss/drawdown thresholds latch an account halt, cancel pending entries, and queue
full exits for holdings. Exposure breaches queue exits without clearing them
merely because exposure later falls. Exits obey fee/minimum/cash constraints too;
failed liquidation attempts are explicit, not assumed successes. Risk orders are
created when the worker observes the breach, never retroactively at an old close.
Stops may therefore fill much later through gaps. Peak starts at initial cash.

FX uses the existing EUR-triangulated, timestamped snapshot contract and maximum
age checks. The shipped identity file works only when no non-EUR conversion is
needed; it invents no USD/DKK rates. Additional FX conversion commission is not
modelled. Stocks require source-backed, unadjusted prices and verified complete
no-action coverage for their cached history. Coverage may encompass the requested
subset, but splits/dividends and adjusted-price fills remain rejected. Yahoo's
`auto_adjust=False` alone is not proof of raw historical prices. Supply valid FX,
action evidence, and market-specific costs before enabling AAPL or other stocks.

To compare with Phase 3, align costs, precision, data/FX inputs, and total effective
delivery delay (scanner publication plus worker receipt and any configured delay).
A zero-delay historical backtest is not equivalent to receiving a signal after
midnight in paper processing. Paper accounts also share cash across assets while
current backtests isolate symbols; compare individual fills/assumptions before
comparing headline returns. Neither synthetic nor paper success establishes edge.

## Storage, migrations, and recovery
`paper.sqlite3` schema version 1 stores immutable account policy, cash/reservations,
positions, decisions, pending/terminal orders, full fills, valuations, and an
append-only event ledger. Migration from an empty version-0 database is atomic.
Unrelated databases or unsupported versions are rejected without overwriting
user tables. Existing scanner schema/state/reports are read-only and unchanged.

A short scanner read transaction captures committed signals and immutable snapshot
references. Paper acknowledgement is its own unique account/signal decision, so
there is no unsafe cross-database write/ack gap. History is reread and deduplicated
rather than relying on a reset-sensitive source cursor. This is intended for local
personal watchlists, not unbounded high-volume streams.

One SQLite write transaction owns each worker batch; no leases or external
in-flight submission states are needed. Concurrent workers fail busy, and process
exit releases the lock. Before processing and before commit, reconciliation
checks cash deltas, reservations, positions/cost basis, accepted order references,
and fill/order states. An uncertain commit acknowledgement is resolved by reading
that state on restart; unique IDs prevent replayed purchases. Inconsistency blocks
processing for investigation instead of guessing or silently repairing money.

Archive references preserve every used Parquet/FX/action input under `inputs/`.
A failed transaction can leave harmless unreferenced files. Do not edit database
rows or published input files. Back up the entire state directory while idle;
local storage grows without pruning. See [backup and safe cleanup](storage.md). Keep the scanner state and source provenance
when moving the workflow. No PostgreSQL, Kubernetes, or object-storage migration
is part of this phase.

## Validation evidence
Offline tests exercise hand-calculated trades/fees/FX, reserved buying power,
shared position/exposure limits, isolated strategy accounts, stock-session timing,
future-open guards, stale inputs, revisions, stop/resume/cancel, pending order
recovery, loss halts, process interruption, uncertain commit acknowledgement,
actual process exit, migrations, read-only scanner handoff, and CLI exports.
The documented synthetic demo was executed. Default tests use no network or
credentials. No real broker integration or active schedule was tested or enabled.

## Internal organization

`engine.py` owns initialization and step transactions. `portfolio.py` owns marks
and valuation records; `orders.py` owns simulated execution; `risk.py` and
`signals.py` own entry/risk checks and strategy decisions. These modules use the
same SQLite transaction and existing policy/version contracts. The cleanup does
not migrate state or change fills. Valuation totals read only the account's
fills and decode each record once.
