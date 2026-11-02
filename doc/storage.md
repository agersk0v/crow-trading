# Storage and retention

Default example paths are below; configuration can change them. All state is
local. No pruning, record deletion, or scheduled cleanup runs automatically.

| Location | Purpose | Retention |
| --- | --- | --- |
| `data/scanner/scanner.sqlite3` | Progress, signals, canonical JSON reports, failure summaries | Keep; scanner authority and paper input |
| `data/scanner/snapshots/` | Original fetches and versioned cumulative datasets | Keep referenced snapshots and lineage |
| `data/paper/paper.sqlite3` | Account policy, cash, positions, orders, fills, decisions, ledger, latest valuations | Keep; financial authority |
| `data/paper/inputs/` | Market data, FX, and stock-action evidence used by paper processing | Keep with the accounts |
| `runner-lock.sqlite3` in each state root | Main-runner ownership | Leave in place; file existence does not mean an active lock |
| Scanner report/export directories | Generated JSON/Markdown and failure summaries | Optional; regenerate from scanner DB |
| Paper CSV/JSON exports | Generated account/audit projections | Optional; regenerate from paper DB |
| Demo directories | Synthetic, isolated examples | Remove when unused; do not confuse with active state |
| `.venv`, test/type/lint caches | Dependencies and development caches | Rebuild with uv/check commands |

## Report storage

New reports store JSON in SQLite. Markdown is generated only when exported; the
legacy `report_md` column remains for existing reports, whose bytes are preserved.
Default scans create no report files and do not inspect old report directories.
For files, use a new destination:

```sh
uv run --locked python -m crow_trading.scanner export --state data/scanner --output data/scanner-export-001
uv run --locked python -m crow_trading.paper export --state data/paper --output data/paper-export-001
```

Scanner export includes numbered JSON/Markdown reports and `failure-streaks.json`.
Consecutive identical failure-only scans retain one original report plus first/last
observation times and a count. Changed errors, configuration, healthy progress, or
recovery end the streak. Each failed scan still returns an error and is visible
in console output. Counts are scan invocations, not network retry attempts. Run
IDs on repeated failures identify the first report; exported summaries describe
later occurrences. Individual intermediate failure timestamps are not retained.

Set watchlist `export_reports: true` or pass scanner `--export-reports` only if
automatic files are wanted. That opt-in mode checks/repairs all numbered report
files and preserves conflicting edits by failing rather than overwriting them.
Deleting those files with auto-export enabled recreates them. Auto-export files
are original report observations; export a new directory for current streak totals.

Scanner schema 1 upgrades transactionally to 2 on a successful scanner operation
(including explicit export). Existing reports/signals/progress remain intact;
the new table holds failure summaries. Paper reads scanner schemas 1 and 2 without
modifying either. Paper schema/policy and strategy/fill semantics are unchanged.
Older scanner versions cannot open schema 2: back up before upgrading and use a
matching code/state backup for rollback. Scanner version 2 may perform a one-time
policy recheck; unchanged signal IDs remain deduplicated.

## Market-data copies

A changed scanner history still creates a cumulative Parquet snapshot; no
partitioned storage migration is included. Raw overlapping fetches and prior
versions preserve revision/replay evidence, so growth is not bounded.

Paper input archives use hard links to validated immutable scanner snapshots on
the same filesystem when supported. Two names then share physical file data;
unlinking one name leaves the other intact. If linking is unavailable, the normal
verified-copy path is used. Existing copies are not rewritten or deduplicated.
Never edit either link in place: it changes the shared file. Fingerprint checks
still reject corrupt inputs. Backup tools may preserve links or create separate
copies, so directory sizes can overstate physical usage.

Paper processing still needs scanner records and their referenced snapshot paths;
an archived paper link alone is not permission to delete the scanner source.
Signals, reports, pending orders, and lineage can refer to older snapshots. There
is no supported command to find/delete all unreferenced snapshots yet; crash
orphans may remain. Do not guess which historical input is safe to remove.

## Backup and cleanup

1. Stop the runner and any standalone scheduled scans/workers. Wait for all
   commands to exit; do not copy a database during an active transaction.
2. Copy both complete state directories, configuration files, FX/action source
   files, and any external data referenced by frozen experiments. Preserve the
   directory layout: some configuration/source references depend on paths.
3. Keep the code revision and lockfile with the backup. Verify a restored copy in
   isolation using paper inspect/export and reconciliation before normal use.

Without touching authoritative state, you can remove unused generated exports,
demo outputs, development caches, and rotated scanner-task logs. Disable automatic
report export first if you do not want report files recreated. Keep any outputs
that are your only preserved published research results or needed audit exports.

Never delete active lock files, databases, individual financial rows, or referenced
snapshots/inputs as cleanup. A whole retired experiment may be archived separately
once no active workflow references it. Financial records have no age-based expiry.
Task-wrapper logs still have no automatic rotation; rotate only while the task is
stopped. No tax-retention period is asserted by these software storage rules.
