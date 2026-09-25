"""UsageProvider - only what Claude Code actually records, never an estimate.

Three sources, all Claude Code's own:

- its stats cache inside the runtime, which records past activity (not entitlement);
- the rate-limit reading it hands its statusline command after each response - the
  5-hour and weekly windows, as a percentage used and a reset time. `ccsm statusline`
  files that reading under the account signed in at the time;
- its `/usage` command, run per account by `fetch_limits` when the manager opens. ccsm
  itself never talks to the network; Claude Code does, as it would for /usage.

Nothing is estimated. Everything else reads `Unavailable`.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import auth
from .credentials import (_write_private, account_uuid, bundle_credentials,
                          credentials_path, local_state, read_account, same_account)
from .profiles import ccsm_home
from .switcher import ccsm_lock, owner_of

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
    return _file(account["accountUuid"], windows, "statusline", now)


_FILE_LOCK = threading.Lock()


def _file(uuid: str, windows: dict, source: str, now: float | None = None) -> dict | None:
    """Store one account's reading. `statusline` keeps what the statusline last passed for
    this account, which is what it goes on passing for a while after a switch."""
    # ponytail: in-process lock only; a statusline and the manager writing in the same
    # instant can drop one reading, and the next response or open writes it again.
    with _FILE_LOCK:
        data = load_limits()
        if source == "statusline" and any(
                isinstance(rec, dict) and rec.get("statusline", rec.get("windows")) == windows
                for other, rec in data.items() if other != uuid):
            return None   # the previous account's numbers, still in the session after a switch
        old = data.get(uuid) if isinstance(data.get(uuid), dict) else {}
        data[uuid] = {"windows": windows, "seen": time.time() if now is None else now,
                      "source": source,
                      "statusline": windows if source == "statusline"
                      else old.get("statusline", old.get("windows"))}
        _write_private(ccsm_home() / LIMITS_FILE, data)
    return windows


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")
_USAGE_LINES = (("five_hour", r"Current session"), ("seven_day", r"Current week \(all models\)"))


def _reset_at(text: str, now: float) -> int | None:
    """'Sep 25, 9:10pm' or '9:10pm' in local time, as Claude Code's /usage prints it."""
    m = re.match(r"(?:([A-Za-z]{3})\w* (\d{1,2}),? )?(\d{1,2})(?::(\d{2}))?\s*([ap]m)",
                 text.strip(), re.I)
    if not m or (m.group(1) and m.group(1).lower() not in _MONTHS):
        return None
    mon, day, hour, minute, half = m.groups()
    hour = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    today = time.localtime(now)
    year, month, mday = today.tm_year, today.tm_mon, today.tm_mday
    if mon:
        month, mday = _MONTHS.index(mon.lower()) + 1, int(day)
    ts = time.mktime((year, month, mday, hour, int(minute or 0), 0, 0, 0, -1))
    if ts < now - 86400:     # a date with no year that has passed is next year's
        ts = time.mktime((year + 1, month, mday, hour, int(minute or 0), 0, 0, 0, -1))
    elif not mon and ts < now:
        ts += 86400
    return int(ts)


def parse_usage(text: str, now: float | None = None) -> dict:
    """The 5-hour and weekly windows from `/usage` text, in the statusline's shape."""
    now = time.time() if now is None else now
    windows = {}
    for key, label in _USAGE_LINES:
        m = re.search(label + r":\s*(\d+(?:\.\d+)?)% used(?:\W+resets\s+([^\n(]+))?", text or "")
        if m:
            windows[key] = {"used_percentage": float(m.group(1)),
                            "resets_at": _reset_at(m.group(2), now) if m.group(2) else None}
    return windows


def _scratch_harvest(store, profile, scratch) -> None:
    """Keep any token Claude Code refreshed in the scratch dir, then drop the copy there.

    OAuth refresh tokens rotate: once Claude Code has used one, only the new one works. So
    a refreshed credential must reach the store before the scratch copy is thrown away.
    """
    live, stored = store.read_live(scratch), store.get(profile.id)
    if bundle_credentials(live) is None or not same_account(stored, live):
        return   # not this account's token: not ours to take or delete
    # A refresh issues a later expiry. An enrolment dir can also hold the older copy its
    # /login left behind, which must never replace a newer stored token.
    if _expiry(live) > _expiry(stored):
        store.put(profile.id, live)
    credentials_path(scratch).unlink(missing_ok=True)


def _expiry(bundle) -> int:
    oauth = (bundle_credentials(bundle) or {}).get("claudeAiOauth") or {}
    return int(oauth.get("expiresAt") or 0)


def fetch_limits(profiles, store, runtime, timeout: int = 25) -> dict:
    """Run Claude Code's `/usage` for every enrolled account and file what it reports.

    The live account runs in the runtime itself: copying its credential elsewhere and
    letting that copy refresh would strand the running session on a spent token. Every
    other account runs in its own enrolment dir with its stored credential, harvested back
    afterwards. Returns {profile_id: windows, or a reason it has none}.
    """
    ok, why = store.available()
    if not ok:
        return {p.id: why for p in profiles.profiles}
    live = owner_of(store, profiles, store.read_live(runtime))
    jobs, out = [], {}
    for p in profiles.profiles:
        uuid = account_uuid(store.get(p.id))
        if uuid is None:
            out[p.id] = "no stored credential"
        else:
            jobs.append((p, uuid, Path(runtime) if p is live else p.enroll_dir))
    if not jobs:
        return out
    # Held throughout, so no switch can publish a token that a scratch run is refreshing.
    with ccsm_lock(profiles.home, timeout + 30):
        for p, _, where in jobs:
            if p is not live:
                _scratch_harvest(store, p, where)   # left behind by a run that was killed
                store.write_live(where, store.get(p.id))
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            texts = list(pool.map(lambda job: auth.usage_text(job[2], timeout), jobs))
        now = time.time()
        for (p, uuid, where), text in zip(jobs, texts):
            if p is not live:
                _scratch_harvest(store, p, where)
            elif account_uuid(store.read_live(runtime)) != uuid:
                out[p.id] = "the live account changed while reading"
                continue
            windows = parse_usage(text or "", now)
            if windows:
                out[p.id] = _file(uuid, windows, "usage", now)
            else:
                out[p.id] = "Claude Code did not report limits" if text is not None \
                    else "could not run Claude Code"
    return out


def left(window: dict, suffix: str = "") -> str:
    """'58%' left in a window, or 'reset' once it has rolled over since the reading."""
    if window.get("resets_at") and window["resets_at"] <= time.time():
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
            for key, label in WINDOWS if (windows.get(key) or {}).get("resets_at")) + (
            f" Claude Code's own reading for the account signed in here, from "
            f"{'/usage' if rec.get('source') == 'usage' else 'its statusline'} "
            f"at {clock(rec['seen'])}.")
    else:
        session_value = UNAVAILABLE
        session_detail = (
            "No limit reading for this account yet. The manager asks Claude Code's /usage "
            "for every account when it opens, and R asks again. ccsm never estimates them."
        )
    if plan:
        session_detail += f" Credential tier for this profile: {plan}."

    return [
        Metric("session", "5h / weekly limit", session_value, session_detail),
        Metric("daily", "Daily activity", _fmt(daily), source(daily)),
        Metric("7day", "7-day activity", _fmt(week), source(week)),
        Metric("additional", "Paid / additional", cost_value, cost_detail),
    ]
