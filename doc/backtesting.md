# Offline backtesting (engine version 1)

## Supported workflow
Freeze a reviewed configuration and its exact local data/FX/action snapshots,
then evaluate fixed parameters on chronological training, validation, and test
windows. Each window starts a new single-symbol account. No broker, network,
automatic parameter search, watchlist scanner, scheduler, or real orders exist.
A frozen hash preserves assumptions; it cannot prove a human never inspected
test data. Commit the plan before evaluating the untouched period and do not
reuse that period for tuning. Provider snapshots/adjustments are not guarantees
of historical point-in-time availability.

The first stock implementation supports **unadjusted prices in verified
no-corporate-action windows only**. Missing action coverage, adjusted datasets,
splits, or dividends fail explicitly. Do not invent a no-action assertion to
make a real dataset pass. Split/dividend accounting, liquidity/partial fills,
settlement, tax calculations, and shared multi-asset accounts remain unsupported.

## Reproducible examples
Run from the repository root after `uv sync --locked` (PowerShell or POSIX):

```sh
uv run --locked python examples/backtesting/make_demo.py --output data/backtest-demo
uv run --locked python -m crow_trading.backtesting freeze data/backtest-demo/crypto.json --output data/backtest-demo/crypto-plan.json
uv run --locked python -m crow_trading.backtesting run data/backtest-demo/crypto-plan.json --output data/backtest-demo/crypto-run
uv run --locked python -m crow_trading.backtesting freeze data/backtest-demo/stock.json --output data/backtest-demo/stock-plan.json
uv run --locked python -m crow_trading.backtesting run data/backtest-demo/stock-plan.json --output data/backtest-demo/stock-run
```

Both examples use synthetic prices and FX, not market observations or performance
evidence. They exercise the same momentum implementation on crypto daily bars
and XNYS stock sessions, with a EUR account, EUR 50 target, EUR 1,000 initial
cash, explicit fees/spread/slippage, and independently reset splits. The generator
writes its complete settings; inspect/edit them **before** freezing. To try mean
reversion, use `"strategy": {"id": "mean-reversion", "parameters": {"lookback":
20, "entry_deviation": 0.05}}` in a new configuration and freeze a new plan.

All output paths must be new. Freeze and run refuse existing destinations.
Freeze never evaluates strategy performance. Its plan includes effective strategy
parameters, versions, protocol, dataset metadata/fingerprint, FX source, and
raw input SHA-256 hashes.
Run verifies the plan and input hashes before simulation. To change assumptions
or snapshots, create a new plan and report directory; never edit a frozen plan.
Paths in a plan are relative to its directory, so preserve that layout when
moving artifacts. The plan and snapshots are sufficient to rerun with the locked
code/dependencies; retain the Git commit alongside published research.

Every run writes `status.json`: success only after complete exports, or rejected
with an error and nonzero exit status. Unsupported/missing inputs cannot produce
a success report. An export failure can leave partial CSV/chart artifacts;
`status.json` is authoritative and `report.json` is written last. Freeze failures
emit a rejected JSON error to stderr and do not create a plan.

On cloud machines with a read-only home, use writable cache directories:
`export UV_CACHE_DIR=/tmp/crow-uv-cache` and
`export MPLCONFIGDIR=/tmp/crow-mpl-cache` in POSIX shells. These cache settings
are unnecessary for normal Windows installs. No TLS verification is disabled.

## Configuration
The generated `crypto.json` and `stock.json` are complete examples. Required keys:
- `experiment_id`, `market_kind`, `dataset`, and `fx_snapshot`.
- `strategy`: ID (`momentum` or `mean-reversion`) and explicit `parameters`.
- `settings`: account currency, initial cash, EUR target and position cap,
  exposure/loss/drawdown fractions, delivery delay (integer 0–3,600 seconds), FX
  maximum age (positive integer hours), and market-specific `costs`.
- `windows`: exactly `training`, `validation`, `test`, ordered, non-overlapping
  `[start, end)` exchange-local dates (UTC dates for crypto), inside the cache.
  Gaps between windows are permitted; windows with no sessions fail.
- Stocks also require `corporate_actions`, a JSON coverage snapshot for the
  entire cached date window, including indicator warm-up history.

Values representing money, prices, quantities, costs, and fractions are decimal
strings; calculations use Decimal. Returns/risk fractions use 0.10 for 10%.
Each run trades one symbol and one position at a time; comparing several runs
does not create a portfolio with shared cash or exposure limits.

### Market costs and sizing
`costs` requires `fee_rate`, `fixed_fee_quote`, `spread_bps`, `slippage_bps`,
`quantity_step`, `minimum_quantity`, and `minimum_notional_quote`. Supply actual
venue/broker assumptions; the demo's numbers are illustrative, not fee quotes.

For raw open `P`, fills use `P * (1 + spread_bps/20000 + slippage_bps/10000)`
for buys and `P * (1 - spread_bps/20000 - slippage_bps/10000)` for sells. Spread
is a full bid/ask width; each side pays half. A fee is `quantity * fill_price *
fee_rate + fixed_fee_quote`, converted at that open's available FX rate.

The entry target is EUR-denominated regardless of account currency. Convert the
EUR target and quote price to account currency, divide, and round quantity down
to a multiple of `quantity_step`. Target and position cap exclude fees. Fractional
stock shares are permitted only when explicitly configured; use step `1` for
whole shares. No automatic size reduction to fit cash or risk limits occurs.
Reject orders below market minimums, above the entry position cap, or lacking
cash including fees. Reject an exit if minimums or its fee would violate cash
constraints. Rejections leave balances unchanged and are recorded in the ledger.
No leverage, shorting, pyramiding, deposits, withdrawals, or partial fills.

### FX and stock action inputs
FX JSON has this shape (rates shown here are illustrative):

```json
{
  "base": "EUR",
  "source": "provider, retrieval details, availability convention",
  "rates": {
    "USD": [
      {"available_at": "2025-01-01T00:00:00Z", "eur_per_unit": "0.92"}
    ]
  }
}
```

EUR has an implicit rate of 1. For each conversion/mark, take the most recent
point with `available_at <= event time` and enforce `fx_max_age_hours`; never use
a future point. Convert currency A to B via EUR. Equal-currency conversion is
identity. Preflight all evaluation opens and closes, even if no trades result.
Supply rates for every required non-EUR currency, including a non-EUR account.
No additional FX conversion commission is assumed. Use actual publication times
in `available_at`, not an earlier fixing date when the rate was still unknown.
Retain original source evidence; the tool does not verify the source's claims.

A stock action snapshot must have `symbol`, `start`, `end` matching the cached
request, nonempty `source`, `coverage_complete: true`, `prices_unadjusted: true`, and
`events: []`. A nonempty
event list is rejected. Only assert no events using a documented source for
real data, and verify the price basis from source evidence. In particular,
Yahoo `auto_adjust=False` alone is not proof of historical raw prices: provider
OHLC can still reflect splits or later revisions. The action snapshot must also
attest that the cached prices are unadjusted. Synthetic examples state that
their prices/rates and lack of actions were constructed. Adjusted stock data is rejected to prevent using
retrospective adjusted prices as executable historical quotes.

## Timing, state, and risk
Strategies read only current/past closed bars. Earlier cached history may warm
indicators in any split, but no pre-window signal becomes a trade and no cash,
positions, pending orders, or risk state carries between splits. The first
eligible strategy signal is generated at the first in-window close.

A signal can fill only on a later bar index whose open is at or after
`available_at + delivery_delay_seconds`. With zero delay, crypto's next open
coincides with the previous close: this is an idealized bar model, not a promise
of actual data delivery. Even a one-second delay misses that open and waits for
the following daily open. Stock calendars naturally skip nights/weekends and
holidays. Queue non-hold intents chronologically; repeated entries while long
and exits while flat are rejected. Pending intents at the window end are
recorded unfilled; they never move into the next split.

Entry exposure is market value at the raw open divided by equity after entry
fees/impact. At each close, compare marked exposure with `max_exposure`; breach
queues a full exit, without permanently halting later entries. At each close,
`equity <= initial_cash * (1-max_loss)` or `equity <= peak_equity *
(1-max_drawdown)` permanently halts new entries for that split and queues an
exit. Peak starts at initial cash. Risk exits cancel ordinary pending intents
and retain their original timing while waiting for an eligible open. They obey
market minimums/fees too. Stops are close-observed and can fill later through a
gap: these are controls, not guaranteed maximum losses or intraday stop orders.

## Reports and definitions
Each split writes strategy and buy-and-hold `*-ledger.csv`, `*-trades.csv`, and
`*-equity.csv`, plus an `*-equity-drawdown.svg` chart. `report.json` embeds the
frozen plan and all metrics/benchmark assumptions. CSV ledgers retain every
fill, rejected/skipped/cancelled/unfilled intent, quantity/cash delta, fee,
execution-impact cost, applied FX, cost basis, and realized P&L. Equity snapshots
contain cash, holdings, realized/unrealized P&L, and drawdown. No-trade reports
have header-only fill CSVs and explicit zero/undefined metrics.

- Cash is held in account currency. Cost basis includes entry fees and actual
  account-currency spend. Each sell closes the entire position. Realized P&L is
  net sale proceeds minus that basis; FX gains/losses are included.
- Terminal open positions are marked at the final close and available FX. They
  are not forcibly liquidated and hypothetical exit costs are not deducted.
- Net P&L is final marked equity minus starting cash; net return divides by
  starting cash. Gross cost-addback P&L adds paid fees and adverse execution
  impact to the same realized path. It is not a zero-cost counterfactual rerun
  and does not recover lost opportunities or compound avoided costs.
- Drawdown is `(prior peak equity - equity)/prior peak equity`, including initial
  cash and each close. Maximum drawdown is the largest such fraction.
- Exposure is the fraction of in-window close observations with a position.
  Turnover is absolute filled notional in account currency divided by initial
  equity. Neither metric includes assumed intrabar holding duration.
- A closed trade is a completed buy/sell pair. Win rate counts positive closed
  net P&L over all closed trades; zero P&L is breakeven. Profit factor is positive
  closed P&L divided by absolute negative closed P&L. No closed trades gives
  null win rate; no losses gives null profit factor with an explicit reason.
- Annualized net return is reported only for horizons of at least 365 elapsed
  days and positive final equity. Otherwise it is null, not extrapolated.
- Buy-and-hold makes one EUR-target allocation at the first in-window open,
  using identical entry limits, precision, fees, spread, FX, and valuation. It
  keeps remaining cash and has no strategy/risk exits; this allocation policy
  is intentionally not an all-in or shared-portfolio benchmark. Entry rejection
  is recorded. Cash benchmark remains initial account-currency cash with no
  interest. All comparisons use identical windows and initial funding.

## Validation
Deterministic tests cover hand-calculated fills/costs/FX P&L, next-open timing,
rounding and market minimums, insufficient cash, duplicate/absent positions,
exposure/loss/drawdown controls, delayed risk exits, missing/stale inputs,
corporate-action rejection, warm-up/isolation, terminal positions, no trades,
benchmarks, immutable plans/outputs, and reproducible CSV/JSON/SVG artifacts.
The documented crypto and stock fixture workflows were executed locally.
Run `uv run --locked pytest` and the README's lint/format/type checks. Historical
reports, including any apparently positive result, never guarantee future profit.
