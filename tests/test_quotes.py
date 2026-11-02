"""Causal quote-policy execution with public-shaped fixtures and real SQLite."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crow_trading.backtesting.models import Costs
from crow_trading.data.models import DataError
from crow_trading.paper.config import read_config
from crow_trading.paper.engine import initialize, step
from crow_trading.paper.operations import control, export, inspect
from crow_trading.paper.orders import fill_order
from crow_trading.paper.quotes import (
    MarketQuote,
    PublicQuotes,
    QuotePolicy,
    archive_quote,
)
from crow_trading.paper.state import State
from crow_trading.scanner.config import Instrument, digest
from tests.helpers.paper import T0, D, Harness


class Clock:
    def __init__(self) -> None:
        self.instant = T0

    def __call__(self) -> datetime:
        result = self.instant
        self.instant += timedelta(seconds=1)
        return result


class Feed:
    def __init__(self, clock: Clock, bid: str = "9", ask: str = "10") -> None:
        self.clock = clock
        self.bid, self.ask = D(bid), D(ask)
        self.calls = 0

    def __call__(self, item: Instrument) -> MarketQuote:
        self.calls += 1
        return MarketQuote(
            item.symbol,
            item.venue,
            self.bid,
            self.ask,
            self.clock(),
            self.clock(),
            mode="fixture",
        )


def harness(
    root: Path,
    *,
    costs: Costs | None = None,
    risk_settings: dict[str, Any] | None = None,
    **policy: Any,
) -> Harness:
    h = Harness(root, costs=costs)
    settings = QuotePolicy(**policy)
    item = replace(h.items[0], name="btc-eur", symbol="BTC/EUR", venue="kraken")
    h.items = (item,)
    h.watchlist = replace(h.watchlist, instruments=h.items)
    h.account = replace(
        h.account,
        name="momentum-quotes",
        settings=replace(h.account.settings, **(risk_settings or {})),
        assets=(replace(h.account.assets[0], instrument=item),),
        quote_policy=settings,
    )
    h.config = replace(
        h.config, watchlist=h.watchlist, accounts=(h.account,), quote_policy=settings
    )
    h.scan()
    initialize(h.config, T0)
    return h


def tick(h: Harness, clock: Clock, feed: Feed) -> dict[str, Any]:
    return step(h.config, clock=clock, quote_fetcher=feed)


def test_same_cycle_buy_exit_quotes_costs_evidence_and_restart(tmp_path: Path) -> None:
    h, clock = harness(tmp_path), Clock()
    feed = Feed(clock)
    result = tick(h, clock, feed)
    assert result["status"] == "success"
    assert feed.calls == 2
    fills = h.rows("fills")
    assert len(fills) == 1
    buy = json.loads(fills[0]["payload"])
    assert buy["side"] == "buy" and D(buy["fill_price_quote"]) == 10
    assert D(buy["quantity"]) == 5 and D(buy["fee_account"]) == D("1.5")
    assert D(buy["execution_impact_account"]) == D("2.5")
    assert D(h.rows("accounts")[0]["cash"]) == D("948.5")
    valuation = json.loads(h.rows("valuations")[0]["payload"])
    assert D(valuation["holdings"]) == 45 and D(valuation["equity"]) == D("993.5")
    mark = next(iter(valuation["marks"].values()))
    assert (
        mark["valuation_side"] == "bid" and mark["quote_timestamp_basis"] == "receipt"
    )
    quote_path = (
        h.config.state_dir / "inputs/quotes" / (buy["quote_fingerprint"] + ".json")
    )
    quote = json.loads(quote_path.read_text())
    order = h.rows("orders")[0]
    assert datetime.fromisoformat(quote["requested_at"]) > datetime.fromisoformat(
        order["created_at"]
    )
    assert buy["dataset_fingerprint"] is None  # Never disguise a quote as a candle.
    assert tick(h, clock, feed)["status"] == "success"
    assert len(h.rows("fills")) == 1
    # Jan 26 closes below the strategy's exit threshold; same-cycle sell at bid.
    h.scan(2)
    clock.instant = T0 + timedelta(days=2)
    feed.bid, feed.ask = D(12), D(13)
    assert tick(h, clock, feed)["status"] == "success"
    sell = json.loads(h.rows("fills")[1]["payload"])
    assert sell["side"] == "sell" and D(sell["fill_price_quote"]) == 12
    assert D(h.rows("accounts")[0]["cash"]) == D("1006.9")
    assert inspect(h.config.state_dir)["reconciliation_errors"] == {}
    export(h.config.state_dir, tmp_path / "export")
    assert "quote_fingerprint" in (tmp_path / "export/fills.csv").read_text()


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"bid": D(0)}, "positive"),
        ({"ask": D("NaN")}, "finite"),
        ({"bid": D(11)}, "uncrossed"),
        ({"symbol": "ETH/EUR"}, "identity"),
        ({"venue": "other"}, "identity"),
        ({"mode": "paper"}, "mode"),
        ({"requested_at": T0 + timedelta(seconds=4)}, "future"),
        ({"received_at": T0 + timedelta(seconds=8)}, "future"),
        ({"provider_at": T0 + timedelta(seconds=4)}, "future provider"),
        ({"provider_at": T0 - timedelta(seconds=60)}, "stale"),
        ({"requested_at": T0 - timedelta(seconds=60)}, "latency"),
        ({"requested_at": datetime(2025, 1, 25)}, "timezone"),
    ],
)
def test_invalid_quotes_rejected(
    tmp_path: Path, changes: dict[str, Any], error: str
) -> None:
    h = harness(tmp_path)
    quote = MarketQuote(
        "BTC/EUR", "kraken", D(9), D(10), T0, T0 + timedelta(seconds=1), mode="fixture"
    )
    with pytest.raises(DataError, match=error):
        replace(quote, **changes).validate(
            h.items[0], T0 + timedelta(seconds=3), QuotePolicy(), "fixture"
        )


def test_receipt_age_provider_timestamp_and_delay_boundaries(tmp_path: Path) -> None:
    h = harness(tmp_path)
    quote = MarketQuote(
        "BTC/EUR", "kraken", D(9), D(10), T0, T0 + timedelta(seconds=1), mode="fixture"
    )
    with pytest.raises(DataError, match="stale"):
        quote.validate(h.items[0], T0 + timedelta(seconds=32), QuotePolicy(), "fixture")
    with pytest.raises(DataError, match="timestamp is required"):
        quote.validate(
            h.items[0],
            T0 + timedelta(seconds=2),
            QuotePolicy(allow_receipt_timestamp=False),
            "fixture",
        )
    provider = replace(quote, provider_at=T0)
    provider.validate(
        h.items[0],
        T0 + timedelta(seconds=2),
        QuotePolicy(allow_receipt_timestamp=False),
        "fixture",
    )
    assert provider.timestamp_basis == "provider"
    with pytest.raises(DataError, match="follow order"):
        quote.validate(
            h.items[0], T0 + timedelta(seconds=2), QuotePolicy(), "fixture", after=T0
        )
    quote.validate(
        h.items[0],
        T0 + timedelta(seconds=2),
        QuotePolicy(),
        "fixture",
        after=T0 - timedelta(microseconds=1),
    )


@pytest.mark.parametrize("action", ["stop", "cancel", "revision", "exit", "risk"])
def test_changes_during_http_are_honored(tmp_path: Path, action: str) -> None:
    h, clock = harness(tmp_path), Clock()
    base = Feed(clock)

    def feed(item: Instrument) -> MarketQuote:
        # Every network call must be outside the financial write lock.
        with State(h.config.state_dir) as state:
            state.reconcile(h.account.name)
        if base.calls == 1:
            orders = h.rows("orders")
            assert len(orders) == 1
            if action == "stop":
                control(h.config.state_dir, "stop", h.account.name, now=clock())
            elif action == "cancel":
                control(
                    h.config.state_dir, "cancel", order_id=orders[0]["id"], now=clock()
                )
            elif action == "risk":
                with State(h.config.state_dir) as state:
                    state.connection.execute(
                        "UPDATE accounts SET risk_halted=1,risk_reason='loss_limit'"
                    )
                    state.commit()
            elif action == "exit":
                h.scan(2)
                clock.instant = T0 + timedelta(days=2)
            else:
                from crow_trading.scanner.state import State as ScannerState

                with ScannerState(h.watchlist.state_dir) as state:
                    record = json.loads(
                        state.connection.execute(
                            "SELECT record_json FROM signals WHERE strategy_key=?",
                            (digest(h.account.strategy),),
                        ).fetchone()[0]
                    )
                    record.update(id="superseding", classification="revision")
                    state.add_signal(record)
                    state.commit()
        return base(item)

    result = step(h.config, clock=clock, quote_fetcher=feed)
    assert not h.rows("fills")
    assert D(h.rows("accounts")[0]["cash"]) == 1000
    assert not h.rows("orders")[0]["status"] == "filled"
    assert result["status"] in ("success", "degraded")


def test_cached_pre_order_quotes_cannot_fill(tmp_path: Path) -> None:
    h, clock = harness(tmp_path), Clock()
    fixed = MarketQuote(
        "BTC/EUR", "kraken", D(9), D(10), T0, T0 + timedelta(seconds=1), mode="fixture"
    )

    def cached(item: Instrument) -> MarketQuote:
        clock.instant += timedelta(seconds=2)
        return fixed

    result = step(h.config, clock=clock, quote_fetcher=cached)
    assert result["status"] == "degraded"
    assert len(h.rows("orders")) == 1 and not h.rows("fills")
    assert h.rows("orders")[0]["status"] == "pending"
    assert tick(h, clock, Feed(clock))["status"] == "success"
    assert len(h.rows("fills")) == 1


def test_delivery_delay_waits_for_later_tick(tmp_path: Path) -> None:
    h, clock = harness(tmp_path), Clock()
    h.account = replace(
        h.account, settings=replace(h.account.settings, delivery_delay_seconds=60)
    )
    h.config = replace(h.config, accounts=(h.account,))
    # New policy uses a new clean account; do not rewrite initialized policy.
    h.account = replace(h.account, name="delayed-quotes")
    h.config = replace(h.config, accounts=(h.account,))
    initialize(h.config, T0)
    feed = Feed(clock)
    assert tick(h, clock, feed)["status"] == "success"
    assert not h.rows("fills")
    clock.instant += timedelta(seconds=60)
    assert tick(h, clock, feed)["status"] == "success"
    assert len(h.rows("fills")) == 1


def test_outage_and_stale_holding_marks_do_not_fabricate_values(tmp_path: Path) -> None:
    h, clock = harness(tmp_path), Clock()
    feed = Feed(clock)
    assert tick(h, clock, feed)["status"] == "success"
    previous = h.rows("valuations")

    def offline(item: Instrument) -> MarketQuote:
        raise OSError("public provider offline")

    result = step(h.config, clock=clock, quote_fetcher=offline)
    assert result["status"] == "degraded"
    assert h.rows("valuations") == previous and len(h.rows("fills")) == 1
    assert result["accounts"][0]["valuation_as_of"] is None
    assert tick(h, clock, feed)["status"] == "success"


def test_interrupted_fill_rolls_back_and_retry_fills_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, clock = harness(tmp_path), Clock()
    feed = Feed(clock)

    def interrupted(*args: Any, **kwargs: Any) -> None:
        fill_order(*args, **kwargs)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr("crow_trading.paper.quote_engine.fill_order", interrupted)
        with pytest.raises(KeyboardInterrupt):
            tick(h, clock, feed)
    assert not h.rows("fills") and D(h.rows("accounts")[0]["cash"]) == 1000
    assert h.rows("orders")[0]["status"] == "pending"
    assert tick(h, clock, feed)["status"] == "success"
    assert len(h.rows("fills")) == 1


@pytest.mark.parametrize("commit_number", [1, 2])
def test_uncertain_commit_acknowledgement_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit_number: int
) -> None:
    h, clock = harness(tmp_path), Clock()
    feed = Feed(clock)
    real = State.commit
    calls = 0

    def lost_ack(self: State) -> None:
        nonlocal calls
        calls += 1
        real(self)
        if calls == commit_number:
            raise OSError("lost commit acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(State, "commit", lost_ack)
        with pytest.raises(OSError, match="acknowledgement"):
            tick(h, clock, feed)
    assert tick(h, clock, feed)["status"] == "success"
    assert len(h.rows("orders")) == len(h.rows("fills")) == 1
    assert inspect(h.config.state_dir)["reconciliation_errors"] == {}


def test_archive_dedup_and_conflict(tmp_path: Path) -> None:
    quote = MarketQuote("BTC/EUR", "kraken", D(9), D(10), T0, T0, mode="fixture")
    identity = archive_quote(tmp_path, quote)
    assert archive_quote(tmp_path, quote) == identity
    assert len(list((tmp_path / "inputs/quotes").glob("*.json"))) == 1
    path = tmp_path / "inputs/quotes" / (identity + ".json")
    path.write_text("corrupt")
    with pytest.raises(DataError, match="conflict"):
        archive_quote(tmp_path, quote)


def test_legacy_policy_hash_and_new_config_guards(tmp_path: Path) -> None:
    path = Path(__file__).parents[1] / "examples/paper/paper.json"
    legacy = read_config(path)
    policy = legacy.accounts[0].policy("paper")
    assert policy["paper_version"] == "1" and "quote_policy" not in policy
    raw = json.loads(path.read_text())
    raw.update(
        fill_policy="public_bid_ask_v1",
        quote_policy={
            "max_age_seconds": 30,
            "max_request_seconds": 10,
            "allow_receipt_timestamp": True,
        },
    )
    raw["watchlist"] = str((path.parent / raw["watchlist"]).resolve())
    raw["fx_snapshot"] = str((path.parent / raw["fx_snapshot"]).resolve())
    test = tmp_path / "quotes.json"
    test.write_text(json.dumps(raw))
    with pytest.raises(DataError, match="zero synthetic spread"):
        read_config(test)
    for account in raw["accounts"]:
        account["settings"]["costs"]["spread_bps"] = "0"
        account["assets"][0]["costs"]["spread_bps"] = "0"
    test.write_text(json.dumps(raw))
    new = read_config(test)
    assert new.accounts[0].policy("paper")["paper_version"] == "2"
    assert digest(legacy.accounts[0].policy("paper")) == digest(policy)


def test_fixture_never_uses_public_network_or_fixed_clock(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(DataError, match="offline quote fetcher"):
        step(h.config)
    with pytest.raises(DataError, match="advancing clock"):
        step(h.config, T0)


def test_slippage_and_quantity_rounding_without_synthetic_spread(
    tmp_path: Path,
) -> None:
    costs = Costs(D("0.02"), D(1), D(0), D(100), D("0.3"), D(0), D(0))
    h, clock = harness(tmp_path, costs=costs), Clock()
    assert tick(h, clock, Feed(clock))["status"] == "success"
    fill = json.loads(h.rows("fills")[0]["payload"])
    assert D(fill["quantity"]) == D("4.8")
    assert D(fill["fill_price_quote"]) == D("10.1")
    assert D(fill["fee_account"]) == D("1.9696")
    assert D(fill["cash_after"]) == D("949.5504")


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("minimum_quantity", "6", "below_minimum_quantity"),
        ("minimum_notional_quote", "100", "below_minimum_notional"),
    ],
)
def test_market_minimums_block_entry(
    tmp_path: Path, field: str, value: str, reason: str
) -> None:
    costs = Costs(D("0.01"), D(1), D(0), D(0), D(1), D(0), D(0))
    h, clock = harness(tmp_path, costs=replace(costs, **{field: D(value)})), Clock()
    tick(h, clock, Feed(clock))
    assert not h.rows("orders") and not h.rows("fills")
    assert h.rows("decisions")[0]["reason"] == reason


def test_insufficient_cash_and_shared_exposure_limits(tmp_path: Path) -> None:
    for name, settings in (
        ("cash", {"initial_cash": D(49)}),
        ("exposure", {"max_exposure": D("0.01")}),
    ):
        h, clock = harness(tmp_path / name, risk_settings=settings), Clock()
        tick(h, clock, Feed(clock))
        assert not h.rows("orders") and not h.rows("fills")
        assert D(h.rows("accounts")[0]["reserved"]) == 0


def test_live_bid_loss_halts_and_exits_without_retroactive_quote(
    tmp_path: Path,
) -> None:
    h, clock = harness(tmp_path, risk_settings={"max_loss": D("0.01")}), Clock()
    feed = Feed(clock)
    assert tick(h, clock, feed)["status"] == "success"
    feed.bid, feed.ask = D(1), D(2)
    assert tick(h, clock, feed)["status"] == "success"
    assert h.rows("accounts")[0]["risk_halted"]
    assert len(h.rows("fills")) == 2
    sell = json.loads(h.rows("fills")[1]["payload"])
    assert sell["side"] == "sell" and D(sell["fill_price_quote"]) == 1
    assert tick(h, clock, Feed(clock))["status"] == "success"
    assert len(h.rows("fills")) == 2


def test_fx_gap_cannot_exceed_original_reservation(tmp_path: Path) -> None:
    h, clock = harness(tmp_path, risk_settings={"account_currency": "USD"}), Clock()

    def fx(rate: str) -> None:
        h.fx.write_text(
            json.dumps(
                {
                    "base": "EUR",
                    "source": "fixture FX",
                    "rates": {
                        "USD": [
                            {
                                "available_at": (
                                    clock.instant - timedelta(seconds=1)
                                ).isoformat(),
                                "eur_per_unit": rate,
                            }
                        ]
                    },
                }
            )
        )

    fx("0.5")
    feed = Feed(clock)

    def gap(item: Instrument) -> MarketQuote:
        if feed.calls == 1:
            fx("0.25")
        return feed(item)

    assert step(h.config, clock=clock, quote_fetcher=gap)["status"] == "success"
    assert not h.rows("fills")
    assert h.rows("orders")[0]["reason"] == "reservation_exceeded"
    assert D(h.rows("accounts")[0]["cash"]) == 1000
    assert D(h.rows("accounts")[0]["reserved"]) == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"max_age_seconds": 0},
        {"max_age_seconds": True},
        {"max_age_seconds": 301},
        {"max_request_seconds": 0},
        {"max_request_seconds": 61},
        {"allow_receipt_timestamp": "yes"},
    ],
)
def test_quote_policy_validation(fields: dict[str, Any]) -> None:
    with pytest.raises(DataError):
        QuotePolicy(**fields)


@pytest.mark.parametrize("stamp", [None, 1737764160000])
def test_public_quote_adapter_timestamp_and_resource_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: int | None
) -> None:
    h, clock = harness(tmp_path), Clock()
    options: dict[str, Any] = {}

    class Session:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Client:
        id = "kraken"
        session = Session()

        def load_markets(self) -> None:
            pass

        def fetch_ticker(self, symbol: str) -> dict[str, Any]:
            return {"symbol": symbol, "bid": 9, "ask": 10, "timestamp": stamp}

    client = Client()

    def constructor(config: dict[str, Any]) -> Client:
        options.update(config)
        return client

    monkeypatch.setattr("crow_trading.paper.quotes.ccxt.kraken", constructor)
    quote = PublicQuotes(clock)(replace(h.items[0], provider="ccxt"))
    assert quote.timestamp_basis == ("receipt" if stamp is None else "provider")
    assert client.session.closed and options["requests_trust_env"]
    assert options["enableRateLimit"] and options["timeout"] == 10000


def test_runner_quote_path_and_console_marks(tmp_path: Path) -> None:
    from crow_trading.runner.engine import run

    h, clock = harness(tmp_path), Clock()
    lines: list[str] = []

    def stop(seconds: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(
            h.config,
            now=clock,
            wait=stop,
            emit=lines.append,
            fetcher=h.fetch,
            quote_fetcher=Feed(clock),
        )
    assert sum(" FILL " in line for line in lines) == 1
    assert any("mark_side=bid" in line and "current" in line for line in lines)
    assert any("basis=receipt" in line for line in lines)


def test_mixed_daily_and_quote_accounts_export_and_reconcile(tmp_path: Path) -> None:
    old = Harness(tmp_path)
    old.scan()
    initialize(old.config, T0)
    old.step()
    old.scan(2)
    old.step(2)
    old_fills = old.rows("fills")
    assert len(old_fills) == 1
    h, clock = harness(tmp_path), Clock()
    assert tick(h, clock, Feed(clock))["status"] == "success"
    report = inspect(h.config.state_dir)
    assert not report["reconciliation_errors"] and len(report["fills"]) == 2
    assert report["fills"][0] == old_fills[0]
    export(h.config.state_dir, tmp_path / "mixed-export")
    assert "quote_fingerprint" in (tmp_path / "mixed-export/fills.csv").read_text()
    assert old.account.policy("fixture")["paper_version"] == "1"


def test_quote_clock_regression_never_backdates_account(tmp_path: Path) -> None:
    h, clock = harness(tmp_path), Clock()
    feed = Feed(clock)

    def backwards(item: Instrument) -> MarketQuote:
        quote = feed(item)
        if feed.calls == 2:
            clock.instant = T0 - timedelta(days=1)
        return quote

    with pytest.raises(DataError, match="backwards"):
        step(h.config, clock=clock, quote_fetcher=backwards)
    assert not h.rows("fills")
    assert datetime.fromisoformat(h.rows("accounts")[0]["last_clock"]) >= T0
