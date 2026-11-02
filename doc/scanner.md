# Daily watchlist scanner (version 1)

One command fetches completed daily bars, evaluates configured strategies, and
writes local JSON/Markdown reports. It creates no orders, portfolios, alerts,
or active scheduled tasks. Strategies are unchanged; this module automates their
data-and-signal workflow before Phase 4 paper execution.

## Try it offline
From the repository root after `uv sync --locked`:

```sh
uv run --locked python examples/scanner/make_demo.py --output data/scanner-demo
uv run --locked python -m crow_trading.scanner data/scanner-demo/watchlist.json
uv run --locked python -m crow_trading.scanner data/scanner-demo/watchlist.json
```

The first command creates clock-relative synthetic crypto and stock snapshots.
The first scan writes `data/scanner-demo/state/reports/000001.json` and `.md`.
The second scan normally reports `no_changes`, zero new signals, and no new files.
These commands work in PowerShell and POSIX shells. Use a new demo directory if
one already exists. Snapshot fixtures never appear in `fresh_entries`; any fresh
fixture matches are separated into `fixture_entries`. A static fixture stops
working when newer due candles are missing; recreate it in a new directory.

## Public-provider watchlist
Review `examples/scanner/watchlist.json`, then run:

```sh
uv run --locked python -m crow_trading.scanner examples/scanner/watchlist.json
```

This example fetches BTC/EUR from Kraken and AAPL from Yahoo, using both version-1
baseline strategies and 60 warm-up bars. State lives at `data/scanner/`, relative
to the example configuration. No API keys or broker credentials are needed.
Network access must permit the public providers and their metadata endpoints.
Provider coverage, availability, rate limits, and research-data limitations still
apply. See [data contracts](data.md) and [strategy definitions](strategies.md).

Configuration paths are relative to the JSON file, not the scheduler's working
directory. Required top-level fields are `schema_version: 1`, `state_dir`,
`attempts` (1–5), `provider_interval_seconds` (0–60), and `instruments` (1–100).
Keep state on a local filesystem supporting SQLite locks and atomic hard links
(NTFS/ext4); do not share it across network drives or synchronization services.

Each instrument specifies:
- Unique `name` (letters, numbers, `_`, `-`), `kind`, `provider`, `venue`, `symbol`,
  `currency`, `calendar`, and boolean `adjusted`. Provider is `ccxt` for crypto,
  `yfinance` for stocks, or `snapshot` for offline files. Stock `venue: yahoo`
  identifies the provider; the actual exchange and timezone are recorded from
  returned metadata. Pick the correct stock calendar explicitly.
- `history_bars`: bootstrap/required retained history, at least each strategy's
  maximum warm-up (21 with the current defaults). These count sessions, not days.
- `overlap_bars`: how much recent provider history to recheck on updates (5 in
  the example). Must be between 1 and `history_bars`.
- `max_catchup_bars`: maximum new bars to process after downtime (90 by default).
  Excess backlog fails explicitly; increase the limit and retry if the provider
  can supply it. Never invent missing history. An intentional new state directory
  re-bootstraps at the latest eligible bar; retain the old state as an archive.
- `finalization_delay_seconds`: wait after each calendar close (900 crypto,
  1,800 stocks in the example), configurable from 0 to 86,400 seconds.
- `max_signal_age_seconds`: freshness measured from the actual bar close
  (64,800 = 18 hours in the example). It must exceed the intended publication
  delay to permit fresh matches. Expiry is also capped at the next expected
  publication deadline, even with a longer configured maximum age.
- `strategies`: unique IDs, installed versions, and parameters. Defaults are
  explicit in the example; unsupported versions and invalid parameters fail.
  Snapshot mode additionally requires a local `snapshot` Parquet path.

Instrument identity includes market/provider/venue/symbol/currency/calendar and
adjustment mode; renaming its display label does not create duplicate signals.
Changing market identity creates a new independent stream. Strategy definitions
and operational policy are versioned/audited separately.

## Daily timing and catch-up
Invoke the CLI every 15 minutes. It computes the newest bar whose calendar close
plus publication delay has passed. Unchanged bars/configuration are skipped
without network requests or duplicate report files. Crypto uses UTC days; stocks
use their exchange's sessions, DST, holidays, and early closes. Weekends do not
create fictional stock candles. Normal no-new-session ticks can show stale
freshness without being provider failures.

On first startup, fetch the configured warm-up and evaluate only the latest
eligible candle. After downtime, evaluate every newly due bar up to the configured
catch-up bound. Only the latest eligible, unexpired bar can be `fresh`; earlier
ones are `catchup`. Previously processed bars with changed data/parameters are
`revision`, never new fresh recommendations. Holds and exits are retained too.
Long fetches cannot publish an expired entry as fresh: expiry is checked again
before the batch commits. Every report records scan cutoff, completion time,
signal availability, expiry, source, configuration, and input fingerprints.

Reports are immutable **delta reports**, containing new/revised records rather
than repeating all previous candidates. Read the reports from the relevant day;
do not treat an old file as a current dashboard. `fresh_entries` means fresh at
that report's `as_of` time only; always check `expires_at` when reading it later.
An entry is a rule match, not a purchase recommendation or profitability claim.
Repeated entry conditions on different days can repeat intents; no holdings or
position sizing are checked here. Both strategies run on configured watchlist
assets only; there is no market-wide discovery or automatic parameter tuning.

## Updates, revisions, and recovery
Bootstrap downloads the required history. Later updates request recent overlap
plus new bars, merge them with retained data, and validate complete coverage.
Provider revisions are detected **only in the fetched window**. Use `--refresh`
to recheck without waiting for another bar; increase `overlap_bars` for a wider
review. Older unrefetched provider revisions remain unknown. Some providers may
refetch a requested window internally rather than support true incremental I/O.

Every raw fetch and changed merged dataset gets a new immutable Parquet snapshot.
Lineage records previous/fetched fingerprints and changed timestamps. Existing
research caches, frozen backtests, and original snapshots are never overwritten.
Affected published signals are reevaluated using currently configured strategies.
Signal identity uses instrument, candle, strategy version/parameters, and the
relevant trailing candle-content hash. Full snapshot fingerprints are also
retained; appending unrelated history does not duplicate an unchanged signal.
Policy-only changes are audited without repeating recommendations.

`scanner.sqlite3` atomically stores progress, deduplicated signal records, and the
full JSON/Markdown report payload. A SQLite write transaction prevents overlapping
scans from processing the same state concurrently. Locks release on process exit;
there is no stale PID lock to delete. Each asset uses a savepoint, so a failed
provider cannot commit partial records for that asset or suppress healthy ones.

After commit, report files are materialized atomically. On restart, missing files
are restored from the committed database before further work. A crash before
commit retries that batch; it cannot lose an already-published report. Uncommitted
snapshot files may remain as harmless orphans. Conflicting edited report files
are preserved and stop the scan: move the modified file aside to keep your notes,
then rerun to restore the canonical report. Do not edit the database or delete
referenced snapshots. Back up the entire state directory while the scanner is
idle. Storage grows with history; pruning/retention is not implemented.

Transient provider/network errors retry with bounded 1/2/4/8-second backoff
according to `attempts`. Validation/unsupported-request errors do not retry.
Crypto clients are reused within a scan and retain CCXT rate limiting; a provider
spacing interval applies across calls and retries. Existing adapter timeouts also
apply. Successful instruments stay committed on partial failure and are skipped
on retry; failed instruments remain due. No cached stale data is silently used
as a fresh provider response.

Exit codes: `0` success/no changes, `1` invalid config or scan/export/partial
failure, `3` another scan owns the state. Errors appear per instrument in reports
and CLI output. If all instruments fail, the run still records a failure report.

## Windows Task Scheduler
Windows integration is documented but was not executed in the Linux development
environment. No active task is installed by this PR. First verify the manual
public-provider scan and review its reports on your own machine.

`examples/scanner/run-scan.ps1` sets the working directory, invokes the installed
`$env:USERPROFILE\.local\bin\uv.exe` by absolute path, logs stdout/stderr to
`data\scanner-task.log`, and preserves the CLI exit code. Change the uv path in
the helper if your installation differs. Try the wrapper manually:

```powershell
powershell.exe -NoProfile -File "C:\Users\jokke\Projects\crow-trading\examples\scanner\run-scan.ps1" -Repository "C:\Users\jokke\Projects\crow-trading"
```

When you choose to activate automation, run this in PowerShell with your actual
repository path. It registers a task for the current user; it does not provision
a server, credentials, or a machine that remains online:

```powershell
$Repo = "C:\Users\jokke\Projects\crow-trading"
$Shell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Arguments = '-NoProfile -File "' + $Repo + '\examples\scanner\run-scan.ps1" -Repository "' + $Repo + '"'
$Action = New-ScheduledTaskAction -Execute $Shell -Argument $Arguments -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Crow Trading Scanner" -Action $Action -Trigger $Trigger -Settings $Settings
```

The computer must be awake, connected, and the configured user session available.
Start-when-available plus scanner catch-up handles missed invocations within the
configured bound. Inspect Task Scheduler's Last Run Result and the log for codes
1/3. Logs have no automatic rotation. To stop automation:

```powershell
Disable-ScheduledTask -TaskName "Crow Trading Scanner"
```

## Optional cron equivalent
On a POSIX host, first create the log directory and confirm the absolute uv path.
A crontab entry can invoke the same command without a long-running daemon:

```cron
*/15 * * * * cd /absolute/path/crow-trading && /absolute/path/uv run --locked python -m crow_trading.scanner examples/scanner/watchlist.json >> data/scanner-task.log 2>&1
```

Cron activation was not performed. Scanner SQLite locking remains the safeguard
against overlap. All scheduling uses UTC/calendar instants inside the CLI, so
local scheduler timezone changes do not shift market-close interpretation.

## Validation evidence
Offline tests cover mixed watchlists, calendar/DST/holiday/half-day boundaries,
float64-stable deduplication, revisions and parameter changes, incremental overlap,
catch-up/freshness deadlines, partial failures/retries, locking, actual subprocess
exit, before/after-commit interruption, report repair/conflicts, CLI modes, and
historical-replay agreement. Default tests require no network or credentials.
On 2026-11-01 the public watchlist fetched/evaluated 60 Kraken BTC/EUR daily bars
and 60 Yahoo AAPL sessions; Friday's stock data was labelled stale on Sunday.
Synthetic/demo CLI runs verified fixture separation and a duplicate-free no-op
second invocation. These checks establish functionality, not investment value.

## Combined foreground runner

To run scanner and paper together, use the [main runner](runner.md). It scans
immediately, processes paper even after no-change/partial scans, and repeats five
minutes after each completed cycle. Disable your existing scanner scheduled task
before using it; do not run both scheduling approaches on the same state. No task
is changed automatically. Daily candle due checks and strategy rules are unchanged.
