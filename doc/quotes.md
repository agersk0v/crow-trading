# Quote-based BTC/EUR paper trading

This is simulated trading using public Kraken bid/ask prices. Strategies still
use completed daily candles. No exchange orders, broker credentials, or real
funds are involved. Only Kraken BTC/EUR is supported by this execution policy.

## Run

Review fees, quantity limits, and risk settings in the new example first:

```sh
uv run --locked python -m crow_trading.runner examples/paper/quotes.json
```

The runner initializes/resumes `momentum-quotes` and `mean-reversion-quotes`, each
with its own virtual EUR 1,000, under `data/paper-quotes/`. It uses the existing
watchlist/scanner state and runs every five minutes after completion. Do not run
a second runner or an old scanner scheduled task on the same scanner state.
Press Ctrl+C to stop; rerun the command to resume. No tasks are installed.

For a single cycle, initialize once and scan before stepping:

```sh
uv run --locked python -m crow_trading.paper init examples/paper/quotes.json
uv run --locked python -m crow_trading.scanner examples/scanner/watchlist.json
uv run --locked python -m crow_trading.paper step examples/paper/quotes.json
uv run --locked python -m crow_trading.paper inspect --state data/paper-quotes --section fills
```

The original `examples/paper/paper.json` remains daily-bar based. Do not edit old
accounts to convert them: the new policy requires new IDs. Existing policy hashes,
orders, fixtures, and financial records remain compatible. Paper database schema
is still 1; no schema migration is required for this policy.

## Cycle and execution

1. Fetch a public quote outside the financial write transaction.
2. Validate saved policy/ledger, mark held BTC at bid, apply risk checks, and
   process daily signals. Accept eligible orders and reserve buying power in a
   committed transaction.
3. If orders are pending, fetch another quote after that commit. Without pending
   orders, reuse the still-valid first observation for final marks.
4. Read committed scanner signals again. In another transaction re-check policy,
   cash, reservations, stops, revisions, exits, quote age, and risk before filling.
   Reconcile and save final valuations/results.

A newly accepted order can fill in the same cycle only from a quote **requested
strictly after** creation plus delivery delay. Pre-order/cached quotes cannot
fill it. Delayed orders remain pending until a later qualifying observation;
the worker does not sleep waiting for the deadline. Orders created after the last
observation, including newly queued risk exits, must await a later quote.

Stops/cancellations committed during the network request are honored. Source
revisions/exits committed before the post-fetch scanner snapshot are honored too.
That snapshot is the observed scanner state; there is no cross-database atomic
transaction with changes occurring after it. SQLite financial locks and unique
IDs protect duplicate workers/retries; no external in-flight order is assumed.

Accepted orders can outlive signal expiry while awaiting valid execution. Missing
or invalid evidence leaves them pending with visible errors, rather than filling
at an invented or daily fallback price. Stop/cancel them explicitly when needed.

## Prices, costs, and risk

- Buy at ask plus configured adverse slippage; sell at bid minus slippage.
- Require `spread_bps: "0"` in both account settings and asset costs: the actual
  spread is already included. Fees, precision, minimum order sizes, and position
  limits remain explicit configuration. Example costs are illustrative, not a
  guarantee of current Kraken rules; verify them before meaningful research.
- Floor quantity to its step and apply minimum quantity/notional rules. A fill
  cannot exceed its original reservation, borrow from other reservations, or
  increase its spending authority. FX/cost changes exceeding that budget reject
  the order and release its reservation.
- Value holdings at bid in account currency, before a future exit fee/slippage.
  Bid-based equity drives loss/drawdown/exposure checks; no mark is replaced by
  zero when unavailable. The last valuation remains visibly stale/unavailable.
- Execution impact is reported relative to quote midpoint, including actual
  half-spread and slippage. It is a diagnostic, not an extra cash charge.

Full fills are approximations: liquidity, quote size, partial fills, market impact,
settlement, and actual exchange execution are not modelled. Five-minute sampling
is not continuous monitoring and risk thresholds do not guarantee maximum losses.

## Quote timestamps and policy

Top-level configuration selects `fill_policy: "public_bid_ask_v1"` plus:

```json
"quote_policy": {
  "max_age_seconds": 30,
  "max_request_seconds": 10,
  "allow_receipt_timestamp": true
}
```

Maximum age accepts 1–300 seconds; maximum request latency accepts 1–60 seconds.
Policy changes require new accounts. Each quote records market/provider, bid/ask,
request start, local receipt, optional provider time, mode, and fingerprint.
Reject nonpositive/nonfinite/crossed/wrong-market quotes, future or naive times,
clock regressions, excessive request latency, and stale provider/receipt times.

Kraken ticker commonly supplies no exchange timestamp. With the explicit receipt
fallback enabled, freshness bounds **local receipt age and request latency**;
it does not prove the age of exchange-side prices or rule out upstream caching.
No exchange timestamp is fabricated. Set `allow_receipt_timestamp: false` to
require one; missing timestamps then produce degraded processing, not a fallback.
Runner fill/holding lines show quote receipt time, timestamp basis, and bid mark
side. `current` means the last cycle valued the account with acceptable inputs,
not that the displayed price remains current indefinitely.

## Evidence and validation

Quotes used for entry checks, fills, or holdings marks are content-addressed under
`data/paper-quotes/inputs/quotes/<fingerprint>.json`. Financial records reference
the evidence, side, prices, fees, and effective/recorded time. Quote fills use the
worker's simulated execution time for both fill timestamps; provider/receipt times
remain separate evidence. They are never represented as daily candles.

Identical observations share one file, including across strategy accounts. New
observations used for ongoing marks/risk add evidence/history even when prices
are unchanged. No pruning is performed; include these files in backups. See
[storage](storage.md). JSON/CSV exports support both policy types in one database.

Offline acceptance cases are in `tests/test_quotes.py`:

```sh
uv run --locked pytest tests/test_quotes.py
```

They use an injected advancing clock and explicit fixture quote feed. Fixture
mode never defaults to public network access. `--as-of` is not supported for quote
steps because a single frozen time cannot model post-order observations; daily
fixture commands remain unchanged. A richer standalone quote demo/results view
is the next roadmap milestone.
