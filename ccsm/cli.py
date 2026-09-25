"""ccsm entry point. The TUI is the product; these commands are the scriptable bits."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from . import __version__, auth, tui, usage
from .credentials import open_store, read_account
from .launcher import launch, preflight
from .profiles import ProfileStore, ccsm_home, mask_email, runtime_dir
from .switcher import SwitchError, adopt, switch

USAGE = f"""ccsm {__version__} - Claude Code Session Manager

  ccsm                       open the profile manager
  ccsm list                  print profiles and their authentication state
  ccsm switch <name>         make <name> the active account - takes effect on the
                             next model request, in the session already running
  ccsm adopt <name>          enrol the credential already signed in, no browser
  ccsm where                 show which config dir a switch would publish into
  ccsm run [args]            start Claude Code in the shared runtime
  ccsm statusline [-- cmd]   Claude Code's statusLine command: record the 5-hour and
                             weekly limits per account; `-- cmd` keeps an existing one
  ccsm uninstall [--yes]     delete all ccsm profile data and stored credentials
  ccsm --version | --help

Each profile is one independently authenticated Claude account. Sign in once per
account; after that, switching republishes a stored credential and never opens a browser.
"""


def cmd_list() -> int:
    store = open_store(ccsm_home())
    profiles = ProfileStore()
    if not profiles.profiles:
        print("No profiles yet. Run `ccsm` and press a to add one.")
        return 0
    ok, why = store.available()
    if not ok:
        print(f"ccsm: {why}", file=sys.stderr)
    else:
        for profile in profiles.profiles:
            auth.quick_refresh(profile, store)
        profiles.save()
    for profile in profiles.profiles:
        marker = "*" if profile.id == profiles.active else " "
        print(tui.enc(f"{marker} {profile.id:<14}{mask_email(profile.email):<24}"
                      f"{(profile.plan or '-'):<9}{profile.auth_state:<10}"
                      f"{tui.ago(profile.last_verified)}"))
    return 0


def cmd_where() -> int:
    print(f"claude  : {auth.claude_version() or 'not found'} "
          f"(verified against {auth.VERIFIED_CLAUDE_VERSION})")
    print(f"runtime : {runtime_dir()}")
    print(f"store   : {ccsm_home() / 'store'}")
    src = ("CCSM_RUNTIME" if os.environ.get("CCSM_RUNTIME") else
           "CLAUDE_CONFIG_DIR" if os.environ.get("CLAUDE_CONFIG_DIR") else
           "the Claude Code default")
    print(f"chosen by: {src}")
    return 0


def cmd_adopt(args: list[str]) -> int:
    """Enrol whatever account is already signed in, without a browser round trip."""
    if not args:
        print("ccsm: adopt needs a name for the profile", file=sys.stderr)
        return 2
    runtime = runtime_dir()
    try:
        profile = adopt(ProfileStore(), open_store(ccsm_home()), runtime, args[0])
    except (SwitchError, ValueError, OSError) as exc:
        print(f"ccsm: {exc}", file=sys.stderr)
        return 1
    print(tui.enc(f"ccsm: adopted the account in {runtime} as '{profile.id}' "
                  f"({mask_email(profile.email)})"))
    return 0


def cmd_switch(args: list[str]) -> int:
    if not args:
        print("ccsm: switch needs a profile name", file=sys.stderr)
        return 2
    profiles = ProfileStore()
    target = profiles.get(args[0])
    if not target:
        print(f"ccsm: no profile named '{args[0]}'", file=sys.stderr)
        return 1
    caution = auth.version_warning()
    if caution:
        print(f"ccsm: note - {caution}", file=sys.stderr)
    try:
        result = switch(profiles, target, open_store(ccsm_home()), runtime_dir())
    except SwitchError as exc:
        print(f"ccsm: SWITCH FAILED - {exc}", file=sys.stderr)
        print("      the runtime was left on a verified credential; "
              "no request will run as an unverified account.", file=sys.stderr)
        return 1
    for note in result["notes"]:
        print(f"  {note}", file=sys.stderr)
    if result["already_active"]:
        print(f"ccsm: {target.name} is already active")
        return 0
    print(tui.enc(f"ccsm: active account is now {target.name} "
                  f"({mask_email(target.email)}) - it applies to the next model request"))
    return 0


def cmd_run(args: list[str]) -> int:
    profiles = ProfileStore()
    active = profiles.get(profiles.active)
    if active is None:
        print("ccsm: no active profile - run `ccsm switch <name>` first", file=sys.stderr)
        return 1
    ok, why = preflight(active, runtime_dir())
    profiles.save()
    if not ok:
        print(f"ccsm: refusing to start as '{active.id}' - {why}", file=sys.stderr)
        return 1
    print(tui.enc(f"ccsm -> {active.name} ({mask_email(active.email)})"), file=sys.stderr)
    return launch(runtime_dir(), args)


def cmd_statusline(args: list[str]) -> int:
    """Record the rate-limit reading Claude Code hands its statusline, then print a line.

    `ccsm statusline -- <command>` passes the same input on to an existing statusline and
    prints its output instead, so the user keeps theirs.
    """
    raw = sys.stdin.buffer.read()
    runtime = runtime_dir()
    try:
        windows = usage.record_limits(json.loads(raw.decode("utf-8")), runtime)
    except Exception:  # a statusline is drawn after every response; it must never fail
        windows = None
    command = args[1:] if args[:1] == ["--"] else args
    if command:
        try:
            out = subprocess.run(command, input=raw, capture_output=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = b""
        sys.stdout.buffer.write(out)
        sys.stdout.flush()
        return 0
    parts = [mask_email((read_account(runtime) or {}).get("emailAddress"))]
    parts += [f"{label} {usage.left(windows[key], ' left')}"
              for key, label in usage.WINDOWS if key in (windows or {})]
    print(tui.enc(" · ".join(parts)))
    return 0


def cmd_uninstall(args: list[str]) -> int:
    home = ccsm_home()
    if not home.exists():
        print(f"Nothing to remove at {home}")
    else:
        if "--yes" not in args:
            print(f"This deletes {home}, including every stored credential. "
                  f"Your Claude Code config dir ({runtime_dir()}) is left alone.")
            if input("Type 'remove' to confirm: ").strip() != "remove":
                print("Cancelled.")
                return 1
        shutil.rmtree(home, ignore_errors=True)
        print(f"Removed {home}")
    print("Now remove the command itself:  pipx uninstall ccsm   (or: pip uninstall ccsm)")
    return 0


def main(argv: list[str] | None = None) -> int:
    tui.setup_terminal()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return tui.run()
    head, rest = args[0], args[1:]
    if head in ("-v", "--version"):
        print(f"ccsm {__version__}")
        return 0
    if head in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    try:
        if head == "list":
            return cmd_list()
        if head == "switch":
            return cmd_switch(rest)
        if head == "adopt":
            return cmd_adopt(rest)
        if head == "where":
            return cmd_where()
        if head == "run":
            return cmd_run(rest)
        if head == "statusline":
            return cmd_statusline(rest)
        if head == "uninstall":
            return cmd_uninstall(rest)
    except auth.AuthError as exc:
        print(f"ccsm: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(USAGE, file=sys.stderr)
    return 2
