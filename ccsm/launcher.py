"""RuntimeLauncher - start Claude Code in the shared runtime directory.

One runtime, one conversation. Which account it authenticates as is decided by whichever
credential ccsm has published into it (see switcher.py), not by which process is running.
Nothing here patches, injects into, or inspects a running Claude Code process.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import auth
from .credentials import clean_env, harden
from .profiles import OK, REVERIFY, Profile


def preflight(profile: Profile, runtime) -> tuple[bool, str]:
    """Confirm the runtime really authenticates as `profile` before starting a session.

    Refuses rather than falling back, so a launch can never quietly hand the session to a
    different account - an ambient API key, or a credential published by something else.
    """
    harden(runtime)
    st = auth.refresh(profile, runtime, adopt=False)
    if st is None:
        return False, "could not reach the Claude Code CLI to verify this profile"
    if profile.auth_state != OK:
        return False, "the runtime is not authenticated - reverify this profile"
    why = auth.identity_mismatch(profile, st)
    if why:
        profile.auth_state = REVERIFY
        return False, f"identity mismatch - {why}"
    return True, ""


def launch(runtime, args=(), replace: bool = True) -> int:
    """Run Claude Code against the shared runtime. On POSIX this replaces the process."""
    exe = auth.require_claude()
    Path(runtime).mkdir(parents=True, exist_ok=True)
    harden(runtime)
    env = clean_env(runtime)
    argv = [exe, *args]
    if replace and os.name == "posix":
        os.execve(exe, argv, env)  # never returns
    return subprocess.call(argv, env=env)
