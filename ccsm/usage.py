"""UsageProvider - only what Claude Code actually records, never an estimate.

Claude Code exposes no rate-limit window, quota, or percentage to the CLI, so
ccsm does not invent one. The single legitimate local source is Claude Code's
own stats cache inside the profile's config dir, which records past activity
(not entitlement). Everything else reads `Unavailable`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .credentials import local_state

UNAVAILABLE = "Unavailable"
STATS_FILE = "stats-cache.json"


@dataclass
class Metric:
    key: str
    label: str
    value: str
    detail: str


def _stats(config_dir) -> dict | None:
    try:
        data = json.loads((Path(config_dir) / STATS_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _si(n: int) -> str:
    if n < 10_000:
        return f"{n:,}"
    if n < 1_000_000:
        return f"{n / 1_000:.1f}K"
    return f"{n / 1_000_000:.1f}M"


def _window(stats: dict | None, days: int) -> dict | None:
    if not stats:
        return None
    activity = [x for x in (stats.get("dailyActivity") or []) if isinstance(x, dict)][-days:]
    tokens = [x for x in (stats.get("dailyModelTokens") or []) if isinstance(x, dict)][-days:]
    if not activity and not tokens:
        return None
    return {
        "days": max(len(activity), len(tokens)),
        "messages": sum(int(x.get("messageCount") or 0) for x in activity),
        "sessions": sum(int(x.get("sessionCount") or 0) for x in activity),
        "tokens": sum(
            int(v) for x in tokens for v in (x.get("tokensByModel") or {}).values()
        ),
    }


def _fmt(window: dict | None) -> str:
    if not window:
        return UNAVAILABLE
    parts = []
    if window["tokens"]:
        parts.append(f"{_si(window['tokens'])} tokens")
    if window["messages"]:
        parts.append(f"{window['messages']:,} msgs")
    if window["sessions"]:
        parts.append(f"{window['sessions']:,} sessions")
    return " \u00b7 ".join(parts) if parts else UNAVAILABLE


def _cost(stats: dict | None) -> tuple[str, str]:
    models = (stats or {}).get("modelUsage") or {}
    total = sum(
        float(v.get("costUSD") or 0) for v in models.values() if isinstance(v, dict)
    )
    if total <= 0:
        return UNAVAILABLE, (
            "No API-billed cost recorded in this profile. Subscription usage is not "
            "billed per token and Claude Code does not report plan limits to the CLI."
        )
    return f"${total:,.2f}", "Cost recorded by Claude Code for API-billed usage in this profile."


def collect(config_dir) -> list[Metric]:
    """The four supported metric rows for a runtime directory, in display order.

    Scoped to the runtime, not to an account: the stats cache belongs to the directory
    Claude Code runs in, and that directory is shared across profiles by design so the
    conversation survives a switch. Labelled accordingly rather than attributed to
    whichever account happens to be active.
    """
    stats = _stats(config_dir)
    asof = (stats or {}).get("lastComputedDate")
    plan = local_state(config_dir).get("rateLimitTier")

    def source(window: dict | None) -> str:
        if not window:
            return f"No Claude Code stats cache in {config_dir}."
        day = "day" if window["days"] == 1 else "days"
        stamp = f", as of {asof}" if asof else ""
        return (
            f"Last {window['days']} recorded {day} of local activity from Claude Code's "
            f"stats cache{stamp}. Activity for this runtime, not one account's quota."
        )

    daily, week = _window(stats, 1), _window(stats, 7)
    cost_value, cost_detail = _cost(stats)
    session_detail = (
        "Rate-limit window usage is only shown by /usage inside a Claude Code session. "
        "The CLI exposes no window data, and ccsm will not estimate it."
    )
    if plan:
        session_detail += f" Credential tier for this profile: {plan}."

    return [
        Metric("session", "Session / window", UNAVAILABLE, session_detail),
        Metric("daily", "Daily activity", _fmt(daily), source(daily)),
        Metric("7day", "7-day activity", _fmt(week), source(week)),
        Metric("additional", "Paid / additional", cost_value, cost_detail),
    ]
