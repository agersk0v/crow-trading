# Historical daily data

## Commands
Run from the repository root after `uv sync --locked`. Commands work in
PowerShell and POSIX shells; quote symbols containing `/` as shown. No trading
credentials or running service are required.

```sh
uv run --locked python -m crow_trading.data fetch-crypto --symbol "BTC/EUR" --start 2026-10-20 --end 2026-10-23 --currency EUR --venue kraken --output data/btc-eur.parquet
uv run --locked python -m crow_trading.data fetch-crypto --symbol "ETH/EUR" --start 2026-10-20 --end 2026-10-23 --currency EUR --venue kraken --output data/eth-eur.parquet
uv run --locked python -m crow_trading.data fetch-stock --symbol AAPL --start 2026-10-19 --end 2026-10-23 --currency USD --calendar XNYS --output data/aapl.parquet
uv run --locked python -m crow_trading.data inspect data/aapl.parquet
```

`inspect` validates and reloads an existing file offline. It prints the request,
actual coverage, row count, provenance, and SHA-256 content fingerprint. Every
fetch/import command also saves and reloads its output before reporting success.
Failures return exit code 1; invalid CLI arguments return exit code 2.

## Contracts
- Only `1d` candles. Dates describe a half-open `[start, end)` window; end is
  excluded. Crypto dates are UTC; stock dates are exchange-local session dates.
- Crypto timestamps are midnight UTC interval opens, closing at the next UTC
  midnight. Stock daily labels are converted to scheduled session opens in UTC;
  `closed_at` is the exchange session close, including early closes and DST.
- Expected crypto bars include every date. Stock coverage uses the selected
  exchange calendar and excludes weekends/holidays. Choose the correct calendar
  for the instrument; a matching timezone alone cannot verify that choice.
  XNYS is used here for AAPL's regular US-equity session schedule.
- Requests containing an unclosed or future session fail before fetching. Every
  expected closed candle must exist. Empty responses, missing bars, duplicates,
  unordered rows, invalid OHLCV, or mismatched source metadata fail explicitly.
  Gaps are never filled. Prices must be finite and positive; volume finite and
  nonnegative; low <= open/close <= high.
- Default stock prices are unadjusted. Add `--adjusted` to request Yahoo's
  `auto_adjust=True` series. The flag is retained in metadata. These research
  bars are not historical executable quotes; corporate-action accounting and
  point-in-time adjustment analysis belong to backtesting work.
- Each dataset stores its symbol, currency, kind, timeframe, calendar,
  adjustment flag, source/venue/version, source timezone, retrieval time,
  normalization version, actual coverage, and candle completeness semantics.
- Cache files are immutable. Saving identical content/provenance to the same
  path is a no-op and preserves the original retrieval time. Changed prices,
  provenance, or settings require a different output path. Retrieval time is
  excluded from the fingerprint; candles and the other metadata are included.
  Reload verifies schema, coverage, and fingerprint. SHA-256 detects accidental
  changes; it does not authenticate a provider. A temporary file plus exclusive
  hard-link creation prevents partial files and silent concurrent replacement.

## Provider limits
CCXT loads public market metadata, respects exchange rate limits, and paginates
OHLCV. Transient network/rate-limit errors retry up to three attempts with 1/2
second backoff; unsupported requests fail without retries. Requests honor the
inherited proxy and CA configuration and keep TLS verification enabled.

Kraken returns a limited recent OHLC history (up to 720 bars); requesting older
coverage fails validation. CCXT cannot create unavailable historical data. Use
an archive with explicit provenance for longer studies; never silently switch
venues. Some exchanges omit no-trade days or use different candle boundaries;
these fail the strict UTC daily contract and require a deliberate separate
normalization decision.

Yahoo/yfinance is a research source with history, licensing, availability, and
rate-limit constraints. Provider errors fail the request; retry later when
throttled. No automatic venue substitution occurs. Intraday data is unsupported.

## Archive import
Prepare a UTF-8 CSV with this header and one row for every expected closed bar:

```csv
timestamp,open,high,low,close,volume
2025-01-01T00:00:00+00:00,100,110,90,105,12
```

Timestamps must have explicit timezones and are normalized to UTC. Crypto rows
must represent UTC-midnight opens; stock rows must represent the chosen
calendar's actual session opens. Input rows must be ordered and cover exactly
the requested window. Normalize vendor-specific archives to this format first,
without inventing candles. Keep original downloads and conversion details;
provide their source URL/version/license and conversion notes in provenance.

```sh
uv run --locked python -m crow_trading.data import-archive --kind crypto --symbol "BTC/EUR" --start 2025-01-01 --end 2025-01-02 --currency EUR --venue kraken --input data/archive.csv --provenance "SOURCE_URL; vendor version; conversion notes" --output data/archive.parquet
```

For stocks use `--kind stock --calendar XNYS` (or the actual exchange calendar)
and the correct currency/adjustment flag. The importer hashes the original CSV
bytes, records the declared venue and provenance, validates every row, and saves
the same Parquet contract. It does not verify the archive's authenticity or
cross-check venues automatically. Do that explicitly before research use.

## Validation evidence
Offline CI uses fixture/mocked providers; it never downloads market data.
On 2026-11-01 the public commands above fetched, validated, cached, and reloaded:

| Source | Instrument | Requested dates (end excluded) | Closed bars |
| --- | --- | --- | --- |
| CCXT 4.5.15 / Kraken | BTC/EUR | 2026-10-20 to 2026-10-23 | 3 |
| CCXT 4.5.15 / Kraken | ETH/EUR | 2026-10-20 to 2026-10-23 | 3 |
| yfinance 0.2.66 / Yahoo NMS | AAPL | 2026-10-19 to 2026-10-23 | 4 |

These checks demonstrate ingestion and offline reload, not strategy performance
or provider correctness. Artifacts remain in ignored local `data/`. Repeat the
commands with a new explicit recent window/output path after Kraken's history
limit moves beyond these dates. Network access requires the public provider
hosts (including `api.kraken.com`, Yahoo chart/cookie endpoints) and package
registries; no broker secrets are needed. In the managed command sandbox,
network commands may require the supported network permission mechanism.

Yahoo returned HTTP 429 for a separate generic HTTP probe during validation;
the actual yfinance request succeeded. If the provider throttles a later run,
retain the failure and retry later rather than silently using another source.

## Public execution quotes

[Quote-based paper execution](quotes.md) uses public Kraken BTC/EUR bid/ask data
separately from these historical daily datasets. It records request/receipt times,
optional provider time, explicit receipt fallback, and immutable quote evidence.
Quotes do not become candles or change historical backtest inputs. Stocks are not
supported by this quote policy.
