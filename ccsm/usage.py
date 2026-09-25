"""UsageProvider - only what Claude Code actually records, never an estimate.

Two local sources, both written by Claude Code itself:

- its stats cache inside the runtime, which records past activity (not entitlement);
- the rate-limit reading it hands its statusline command after each response - the
  5-hour and weekly windows, as a percentage used and a reset time. `ccsm statusline`
  files that reading under the account signed in at the time.

ccsm never asks the network for either, and never estimates. Everything else reads
`Unavailable`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .credentials import _write_private, local_state, read_account
from .profiles import ccsm_home

UNAVAILABLE = "Unavailable"
STATS_FILE = "stats-cache.json"
LIMITS_FILE = "limits.json"
WINDOWS = (("five_hour", "5h"), ("seven_day", "week"))


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


def load_limits() -> dict:
    """{accountUuid: {"windows": {...}, "seen": epoch}} as last filed by record_limits."""
    try:
        data = json.loads((ccsm_home() / LIMITS_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_limits(payload, runtime, now: float | None = None) -> dict | None:
    """File the rate-limit reading from Claude Code's statusline input under the account
    signed in at `runtime`. Returns the windows filed, or None if nothing was."""
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    account = read_account(runtime)
    if not isinstance(limits, dict) or account is None:
        return None
    windows = {}
    for key, _ in WINDOWS:
        w = limits.get(key)
        if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)) \
                and isinstance(w.get("resets_at"), (int, float)):
            windows[key] = {"used_percentage": float(w["used_percentage"]),
                            "resets_at": int(w["resets_at"])}
    if not windows:
        return None
    uuid = account["accountUuid"]
    data = load_limits()
    # Until the first response after a switch, Claude Code still hands the statusline the
    # previous account's reading. Numbers already filed under another account are theirs.
    if any(isinstance(rec, dict) and rec.get("windows") == windows
           for other, rec in data.items() if other != uuid):
        return None
    data[uuid] = {"windows": windows, "seen": time.time() if now is None else now}
    _write_private(ccsm_home() / LIMITS_FILE, data)
    return windows


def left(window: dict, suffix: str = "") -> str:
    """'58%' left in a window, or 'reset' once it has rolled over since the reading."""
    if window["resets_at"] <= time.time():
        return "reset"
    return f"{max(0.0, 100.0 - window['used_percentage']):.0f}%{suffix}"


def clock(ts: float) -> str:
    """Local wall-clock time, with the weekday once it is more than a day away."""
    fmt = "%H:%M" if abs(ts - time.time()) < 86400 else "%a %H:%M"
    return time.strftime(fmt, time.localtime(ts))


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
    rec = load_limits().get((read_account(config_dir) or {}).get("accountUuid"))
    windows = rec.get("windows", {}) if isinstance(rec, dict) else {}
    if windows:
        session_value = " · ".join(
            f"{label} {left(windows[key], ' left')}" for key, label in WINDOWS if key in windows)
        session_detail = " ".join(
            f"The {label} window resets {clock(windows[key]['resets_at'])}."
            for key, label in WINDOWS if key in windows) + (
            f" Claude Code's own reading for the account signed in here, recorded by "
            f"`ccsm statusline` at {clock(rec['seen'])}.")
    else:
        session_value = UNAVAILABLE
        session_detail = (
            "Claude Code reports the 5-hour and weekly limits only to its statusline. Set "
            "`ccsm statusline` as the statusLine command and ccsm records them per "
            "account. It never estimates them."
        )
    if plan:
        session_detail += f" Credential tier for this profile: {plan}."

    return [
        Metric("session", "5h / weekly limit", session_value, session_detail),
        Metric("daily", "Daily activity", _fmt(daily), source(daily)),
        Metric("7day", "7-day activity", _fmt(week), source(week)),
        Metric("additional", "Paid / additional", cost_value, cost_detail),
    ]
