"""AuthManager - authentication state via Claude Code's own supported commands.

`claude auth status --json` run with a profile's CLAUDE_CONFIG_DIR is the
authoritative answer on every platform, so ccsm never has to guess where the
secret lives. `claude auth login` is the only sign-in path we drive, and it is
always scoped to one profile: browser OAuth is single-account, so there is no
bulk reauthentication anywhere in this tool.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .credentials import clean_env, expired, summarize
from .profiles import OK, REVERIFY, UNKNOWN, Profile

STATUS_TIMEOUT = 25

# ccsm depends on undocumented Claude Code behaviour: that the OAuth credential is read
# per request, where .claude.json lives, and the shape of its oauthAccount block. All of
# it was verified against this build. A different one may lay things out differently, and
# a switch writes into the user's real config, so say so rather than assuming.
VERIFIED_CLAUDE_VERSION = "2.1.259"


class AuthError(RuntimeError):
    pass


def claude_bin() -> str | None:
    """Path to the Claude Code executable, or None."""
    override = os.environ.get("CCSM_CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    for candidate in ("~/.local/bin/claude", "~/.claude/local/claude", "/usr/local/bin/claude"):
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return None


def claude_version() -> str | None:
    """The installed Claude Code version, or None if it cannot be determined."""
    exe = claude_bin()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=20, stdin=subprocess.DEVNULL).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+\.\d+\.\d+)", out)
    return match.group(1) if match else None


def version_warning() -> str | None:
    """A caution to show before ccsm writes into a real Claude Code config, or None."""
    found = claude_version()
    if found is None:
        return ("could not determine the Claude Code version; ccsm was verified against "
                f"{VERIFIED_CLAUDE_VERSION}")
    if found.split(".")[:2] != VERIFIED_CLAUDE_VERSION.split(".")[:2]:
        return (f"Claude Code {found} differs from the {VERIFIED_CLAUDE_VERSION} ccsm was "
                f"verified against. The credential and .claude.json layouts are "
                f"undocumented and may have moved. A backup is taken before the first "
                f"write; check it if a switch misbehaves.")
    return None


def require_claude() -> str:
    exe = claude_bin()
    if not exe:
        raise AuthError("Claude Code CLI not found. Install it, or set CCSM_CLAUDE_BIN.")
    return exe


def _first_json(text: str) -> dict | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        value = json.JSONDecoder().raw_decode(text[start:])[0]
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def status(config_dir, timeout: int = STATUS_TIMEOUT) -> dict | None:
    """`claude auth status --json` inside one profile. None if it cannot be run."""
    try:
        proc = subprocess.run(
            [require_claude(), "auth", "status", "--json"],
            env=clean_env(config_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _first_json(proc.stdout or "")


def usage_text(config_dir, timeout: int = 60) -> str | None:
    """Claude Code's own `/usage` report for one config dir, or None if it cannot be run.

    A local command in print mode: it reads the account's limits and sends no prompt to a
    model, so it spends no quota. It may refresh the OAuth token in `config_dir` - callers
    that run it outside the live runtime must harvest the credential afterwards.
    """
    try:
        proc = subprocess.run(
            [require_claude(), "-p", "/usage", "--no-session-persistence"],
            env=clean_env(config_dir),
            cwd=str(config_dir),        # no project's settings or hooks
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, AuthError):
        return None
    return proc.stdout or ""


def _set(profile: Profile, attr: str, value, adopt: bool) -> None:
    if value and (adopt or getattr(profile, attr) is None):
        setattr(profile, attr, value)


def apply_status(profile: Profile, st: dict, adopt: bool = True) -> None:
    """Classify a status payload onto a profile. Pure - no subprocess."""
    if st.get("loggedIn"):
        _set(profile, "email", st.get("email"), adopt)
        _set(profile, "org", st.get("orgName"), adopt)
        _set(profile, "plan", st.get("subscriptionType"), adopt)
        _set(profile, "auth_method", st.get("authMethod"), True)
        profile.auth_state = OK
    else:
        profile.auth_state = REVERIFY
    profile.last_verified = time.time()


def refresh(profile: Profile, config_dir, adopt: bool = True,
            timeout: int = STATUS_TIMEOUT) -> dict | None:
    """Authoritative check of what `config_dir` authenticates as. Marks only this profile."""
    st = status(config_dir, timeout)
    if st is None:
        return None
    apply_status(profile, st, adopt)
    return st


def quick_refresh(profile: Profile, store) -> None:
    """Instant, local-only state check against the stored credential.

    No subprocess and no network. Only ever moves a profile to REVERIFY on positive
    evidence; anything ambiguous stays as it was, or UNKNOWN if never verified.
    """
    ok, _ = store.available()
    if not ok:
        profile.auth_state = UNKNOWN
        return
    st = summarize(store.get(profile.id))
    if not st.get("present"):
        profile.auth_state = REVERIFY if profile.last_verified else UNKNOWN
        return
    profile.auth_state = REVERIFY if expired(st) else OK
    if st.get("subscriptionType"):
        profile.plan = st["subscriptionType"]


def identity_mismatch(profile: Profile, st: dict | None) -> str | None:
    """Why launching this profile would authenticate a different account."""
    if not st or not st.get("loggedIn"):
        return None
    live = st.get("email")
    if live and profile.email and live.lower() != profile.email.lower():
        return f"this runtime now signs in as {live}, not {profile.email}"
    if not live and st.get("authMethod") not in (None, "claude.ai"):
        return f"authenticated via {st.get('authMethod')}, not this profile's Claude account"
    return None


def login_argv(email: str | None = None, console: bool = False) -> list[str]:
    argv = [require_claude(), "auth", "login"]
    if console:
        argv.append("--console")
    if email:
        argv += ["--email", email]
    return argv


def logout(config_dir, timeout: int = 20) -> bool:
    exe = claude_bin()
    if not exe:
        return False
    try:
        return subprocess.run(
            [exe, "auth", "logout"],
            env=clean_env(config_dir),
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
