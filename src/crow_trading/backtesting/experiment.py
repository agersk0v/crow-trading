"""Freeze configuration and exact input snapshots before evaluating any split."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crow_trading.backtesting.market import FXBook, check_actions
from crow_trading.backtesting.models import (
    Settings,
    Window,
    parse_settings,
    parse_windows,
)
from crow_trading.data.models import DataError, Dataset
from crow_trading.data.storage import fingerprint, load, metadata_dict
from crow_trading.strategies import MeanReversion, Momentum, Strategy

ENGINE_VERSION = "1"
PROTOCOL = (
    "fixed parameters; chronological training/validation/test; isolated cash; "
    "prior bars warm indicators only"
)


def read_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise DataError("expected a JSON object")
    return raw


def digest(raw: object) -> str:
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Inputs:
    dataset: Dataset
    settings: Settings
    windows: tuple[Window, ...]
    strategy: Strategy
    fx: FXBook
    actions: dict[str, Any] | None


def inputs(config: dict[str, Any], directory: Path) -> Inputs:
    if not config.get("experiment_id"):
        raise DataError("experiment_id is required")
    dataset = load(directory / config["dataset"])
    settings = parse_settings(config["settings"])
    windows = parse_windows(config["windows"])
    if config["market_kind"] != dataset.metadata.request.kind:
        raise DataError("market_kind does not match the dataset")
    for window in windows:
        req = dataset.metadata.request
        if not req.start <= window.start < window.end <= req.end:
            raise DataError("evaluation windows must lie inside cached coverage")
    definition = config["strategy"]
    strategy: Strategy
    if definition["id"] == "momentum":
        strategy = Momentum(**definition["parameters"])
    elif definition["id"] == "mean-reversion":
        strategy = MeanReversion(**definition["parameters"])
    else:
        raise DataError("unknown strategy")
    fx = FXBook(read_json(directory / config["fx_snapshot"]), settings.fx_max_age_hours)
    actions_path = config.get("corporate_actions")
    actions = read_json(directory / actions_path) if actions_path else None
    check_actions(dataset, actions)
    return Inputs(dataset, settings, windows, strategy, fx, actions)


def snapshot_hashes(config: dict[str, Any], directory: Path) -> dict[str, str]:
    return {
        key: file_digest(directory / config[key])
        for key in ("dataset", "fx_snapshot", "corporate_actions")
        if config.get(key)
    }


def freeze(config_path: Path, destination: Path) -> dict[str, Any]:
    config = read_json(config_path)
    prepared = inputs(config, config_path.parent)
    normalized = dict(config)
    normalized["strategy"] = {
        "id": prepared.strategy.strategy_id,
        "parameters": prepared.strategy.parameters(),
    }
    for key in ("dataset", "fx_snapshot", "corporate_actions"):
        if config.get(key):
            absolute = (config_path.parent / config[key]).resolve()
            normalized[key] = Path(
                os.path.relpath(absolute, destination.parent.resolve())
            ).as_posix()
    content = {
        "schema_version": 1,
        "engine_version": ENGINE_VERSION,
        "configuration": normalized,
        "strategy_version": prepared.strategy.version,
        "input_hashes": snapshot_hashes(config, config_path.parent),
        "dataset_fingerprint": fingerprint(prepared.dataset),
        "dataset_metadata": metadata_dict(prepared.dataset),
        "fx_source": prepared.fx.source,
        "evaluation_protocol": PROTOCOL,
    }
    plan = {**content, "plan_hash": digest(content)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(plan, sort_keys=True, indent=2) + "\n")
    return plan


def read_plan(path: Path) -> tuple[dict[str, Any], Inputs]:
    plan = read_json(path)
    content = {key: value for key, value in plan.items() if key != "plan_hash"}
    if digest(content) != plan.get("plan_hash"):
        raise DataError(
            "frozen plan hash mismatch; create a new plan for changed assumptions"
        )
    if (
        plan["schema_version"] != 1
        or plan["engine_version"] != ENGINE_VERSION
        or plan["evaluation_protocol"] != PROTOCOL
    ):
        raise DataError("unsupported plan/engine/protocol version")
    config = plan["configuration"]
    if snapshot_hashes(config, path.parent) != plan["input_hashes"]:
        raise DataError("input snapshot changed after the plan was frozen")
    prepared = inputs(config, path.parent)
    if (
        fingerprint(prepared.dataset) != plan["dataset_fingerprint"]
        or prepared.strategy.version != plan["strategy_version"]
        or metadata_dict(prepared.dataset) != plan["dataset_metadata"]
        or prepared.fx.source != plan["fx_source"]
    ):
        raise DataError("dataset or strategy version differs from the frozen plan")
    return plan, prepared
