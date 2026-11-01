"""Local reports separate fresh rule matches, history, revisions, and fixtures."""

from typing import Any


def safe(value: object) -> str:
    return (
        str(value)
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("|", "\\|")
        .replace("<", "&lt;")
    )


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Daily scanner report",
        "",
        f"As of **{report['as_of']}** (UTC).",
        "",
        (
            "Rule matches only; no orders or portfolio checks. "
            "Freshness expires at the times shown."
        ),
        "",
        "## Fresh entry rule matches",
        "",
    ]
    for row in report["fresh_entries"]:
        lines.append(
            f"- {safe(row['instrument'])}: {safe(row['strategy']['id'])}; "
            f"expires {row['expires_at']}"
        )
    if not report["fresh_entries"]:
        lines.append("None.")
    if report["fixture_entries"]:
        lines += ["", "## Fixture entry matches (not live data)", ""]
        for row in report["fixture_entries"]:
            lines.append(f"- {safe(row['instrument'])}: {safe(row['strategy']['id'])}")
    lines += [
        "",
        "## All new signal records",
        "",
        (
            "| Instrument | Candle open (UTC) | Strategy | Intent | "
            "Classification | Reason |"
        ),
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["signals"]:
        label = row["classification"] + (
            " / fixture" if row["data_mode"] == "snapshot" else ""
        )
        lines.append(
            "| "
            + " | ".join(
                safe(x)
                for x in (
                    row["instrument"],
                    row["candle_timestamp"],
                    row["strategy"]["id"],
                    row["intent"],
                    label,
                    row["reason"],
                )
            )
            + " |"
        )
    lines += ["", "## Data updates", ""]
    for item in report["updates"]:
        lines.append(
            f"- {safe(item['instrument'])}: {len(item['revised_candles'])} "
            f"revised candles; policy_changed={item['policy_changed']}; "
            f"snapshot `{item['dataset_fingerprint']}`."
        )
    if not report["updates"]:
        lines.append("None.")
    lines += ["", "## Instrument status", ""]
    for item in report["instruments"]:
        lines.append(
            f"- {safe(item['instrument'])}: {safe(item['status'])}; "
            f"freshness={safe(item.get('freshness', 'unavailable'))}; "
            f"provider={safe(item['identity']['provider'])}"
            + (f" — {safe(item['error'])}" if item.get("error") else "")
        )
    return "\n".join(lines) + "\n"
