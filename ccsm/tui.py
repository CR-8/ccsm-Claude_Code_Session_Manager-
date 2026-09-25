"""TUI - keyboard-first profile switcher.

Raw ANSI on the alternate screen. No curses, no third-party toolkit, so the
same code runs on macOS, Linux/WSL and Windows terminals.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, auth, usage
from .credentials import bundle_credentials, clean_env, open_store
from .launcher import launch, preflight
from .profiles import (MAX_PROFILES, OK, REVERIFY, ProfileStore, ccsm_home, mask_email,
                       runtime_dir)
from .switcher import SwitchError, adopt, capture, owner_of, switch

# --- warm neutral palette (256-colour, deliberately quiet) -------------------

_PALETTE = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "coral": "\x1b[38;5;173m",   # Anthropic clay, used sparingly
    "warm": "\x1b[38;5;180m",    # sand
    "text": "\x1b[38;5;252m",
    "sel": "\x1b[38;5;223m",     # selected row, warm cream
    "dim": "\x1b[38;5;245m",
    "rule": "\x1b[38;5;238m",
    "ok": "\x1b[38;5;108m",      # muted sage
    "warn": "\x1b[38;5;179m",    # amber
}
C = dict(_PALETTE)

DOT = "●"
ARROW = "▸"
RULE = "─"
MID = "·"
DASH = "—"
ELL = "…"
CARET = "█"

_FALLBACK = {DOT: "*", ARROW: ">", RULE: "-", "⏎": "Enter", "↑": "^",
             "↓": "v", "•": "*", ELL: "~", MID: "-", DASH: "-",
             CARET: "_", "→": "->"}
_UNICODE = True


def setup_terminal() -> None:
    """Enable ANSI on Windows and decide on colour / glyph fallbacks."""
    global C, _UNICODE
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        C = {k: "" for k in _PALETTE}
    try:
        "".join(_FALLBACK).encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        _UNICODE = False


def enc(s: str) -> str:
    return s if _UNICODE else "".join(_FALLBACK.get(ch, ch) for ch in s)


def pad(s: str | None, n: int) -> str:
    s = s or ""
    return (s[: n - 1] + ELL) if len(s) > n else s.ljust(n)


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    d = max(0.0, time.time() - ts)
    if d < 10:
        return "just now"
    for cutoff, div, unit in ((90, 1, "s"), (5400, 60, "m"), (129600, 3600, "h")):
        if d < cutoff:
            return f"{int(d / div)}{unit} ago"
    return f"{int(d / 86400)}d ago"


def tilde(path) -> str:
    try:
        return str(Path("~") / Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def plan_label(plan: str | None) -> str:
    return (plan or DASH).replace("_", " ").title()


# --- keyboard ---------------------------------------------------------------

if os.name == "nt":
    import msvcrt

    _NT_ARROWS = {"H": "up", "P": "down", "K": "left", "M": "right"}

    def read_key() -> str:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return _NT_ARROWS.get(msvcrt.getwch(), "")
        if ch == "\x03":
            raise KeyboardInterrupt
        return {"\r": "enter", "\n": "enter", "\x1b": "esc",
                "\x08": "backspace"}.get(ch, ch)

else:
    import select
    import termios
    import tty

    _VT_ARROWS = {"[A": "up", "[B": "down", "[D": "left", "[C": "right"}

    def read_key() -> str:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                if select.select([fd], [], [], 0.05)[0]:
                    return _VT_ARROWS.get(sys.stdin.read(2), "")
                return "esc"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        if ch == "\x03":
            raise KeyboardInterrupt
        return {"\r": "enter", "\n": "enter", "\x7f": "backspace",
                "\x08": "backspace"}.get(ch, ch)


def enter_screen() -> None:
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    sys.stdout.flush()


def leave_screen() -> None:
    sys.stdout.write("\x1b[?25h\x1b[?1049l")
    sys.stdout.flush()


# --- app --------------------------------------------------------------------

KEYBAR = (
    ("↑↓", "move"), ("⏎", "switch"), ("l", "run"), ("a", "add"), ("A", "adopt"),
    ("r", "reverify"),
    ("d", "remove"), ("u", "usage"), ("R", "refresh"), ("q", "quit"),
)
USAGE_KEYBAR = (("↑↓", "metric"), ("R", "refresh"), ("esc", "back"), ("q", "quit"))


class App:
    def __init__(self) -> None:
        self.store = ProfileStore()
        self.creds = open_store(ccsm_home())
        self.sel = 0
        self.metric = 0
        self.view = "list"
        self.metrics: list = []
        self.msg = ""
        self.msg_kind = "dim"

    # -- helpers

    @property
    def current(self):
        return self.store.profiles[self.sel] if self.store.profiles else None

    def note(self, text: str, kind: str = "dim") -> None:
        self.msg, self.msg_kind = text, kind

    def width(self) -> int:
        return max(58, min(shutil.get_terminal_size((84, 24)).columns, 100))

    # -- rendering

    def draw(self, prompt: tuple[str, str] | None = None) -> None:
        w = self.width()
        hairline = RULE * (w - 4)
        lines = ["", self._header(w), f"  {C['rule']}{hairline}{C['reset']}",
                 f"  {self._live()}", ""]
        lines += self._usage_body() if self.view == "usage" else self._list_body()
        lines.append("")
        if prompt:
            label, buf = prompt
            lines.append(f"  {C['coral']}{label}{C['reset']} {C['sel']}{buf}{CARET}{C['reset']}")
        else:
            msg = self.msg if len(self.msg) <= w - 4 else self.msg[: w - 5] + ELL
            lines.append(f"  {C[self.msg_kind]}{msg}{C['reset']}" if msg else "")
        lines += ["", self._keybar(USAGE_KEYBAR if self.view == "usage" else KEYBAR)]
        sys.stdout.write(
            "\x1b[H" + "".join(enc(ln) + "\x1b[K\r\n" for ln in lines) + "\x1b[J"
        )
        sys.stdout.flush()

    def _header(self, w: int) -> str:
        left, right = "ccsm  claude code session manager", f"v{__version__}"
        gap = " " * max(1, w - 4 - len(left) - len(right))
        return (f"  {C['bold']}{C['coral']}ccsm{C['reset']}  "
                f"{C['dim']}claude code session manager{gap}{right}{C['reset']}")

    def _live(self) -> str:
        """Who the runtime is signed in as right now - read from disk, not profiles.json,
        because a /login run there changes the account without ccsm knowing."""
        ok, why = self.creds.available()
        if not ok:
            return f"{C['warn']}{why}{C['reset']}"
        live = self.creds.read_live(runtime_dir())
        where = f"{C['dim']}runtime {tilde(runtime_dir())} {MID} {C['reset']}"
        if bundle_credentials(live) is None:
            return f"{where}{C['dim']}signed out{C['reset']}"
        owner = owner_of(self.creds, self.store, live)
        if owner is None:
            return (f"{where}{C['warn']}signed in as an account ccsm does not hold "
                    f"- A adopts it{C['reset']}")
        return f"{where}{C['dim']}live {C['reset']}{C['sel']}{owner.name}{C['reset']}"

    def _keybar(self, bar) -> str:
        parts = [f"{C['warm']}{k}{C['reset']} {C['dim']}{label}{C['reset']}" for k, label in bar]
        return "  " + "   ".join(parts)

    def _list_body(self) -> list[str]:
        if not self.store.profiles:
            return [
                f"  {C['dim']}No profiles yet.{C['reset']}",
                f"  {C['warm']}A{C['reset']} {C['dim']}keeps the account already signed in "
                f"here - no browser.{C['reset']}",
                f"  {C['warm']}a{C['reset']} {C['dim']}adds another account with a browser "
                f"sign-in (up to {MAX_PROFILES}).{C['reset']}",
            ]
        head = (f"      {C['dim']}{pad('PROFILE', 14)}{pad('IDENTITY', 24)}"
                f"{pad('PLAN', 9)}{pad('AUTH', 10)}VERIFIED{C['reset']}")
        return [head, ""] + [self._row(i, p) for i, p in enumerate(self.store.profiles)]

    def _row(self, i: int, p) -> str:
        cursor = f"{C['coral']}{ARROW}{C['reset']}" if i == self.sel else " "
        active = f"{C['coral']}{DOT}{C['reset']}" if p.id == self.store.active else " "
        name_c = C["sel"] if i == self.sel else C["text"]
        state_c = {OK: C["ok"], REVERIFY: C["warn"]}.get(p.auth_state, C["dim"])
        return (f"  {cursor} {active}  {name_c}{pad(p.name, 14)}{C['reset']}"
                f"{C['dim']}{pad(mask_email(p.email), 24)}{C['reset']}"
                f"{C['warm']}{pad(plan_label(p.plan), 9)}{C['reset']}"
                f"{state_c}{pad(p.auth_state, 10)}{C['reset']}"
                f"{C['dim']}{ago(p.last_verified)}{C['reset']}")

    def _usage_body(self) -> list[str]:
        p = self.current
        lines = [f"  {C['dim']}usage {MID} {C['reset']}{C['sel']}{p.name}{C['reset']}", ""]
        for i, m in enumerate(self.metrics):
            cursor = f"{C['coral']}{ARROW}{C['reset']}" if i == self.metric else " "
            value_c = C["dim"] if m.value == usage.UNAVAILABLE else C["warm"]
            label_c = C["sel"] if i == self.metric else C["text"]
            lines.append(f"  {cursor}  {label_c}{pad(m.label, 20)}{C['reset']}"
                         f"{value_c}{m.value}{C['reset']}")
        lines.append("")
        for chunk in self._wrap(self.metrics[self.metric].detail, self.width() - 8):
            lines.append(f"     {C['dim']}{chunk}{C['reset']}")
        return lines

    @staticmethod
    def _wrap(text: str, width: int) -> list[str]:
        out: list[str] = []
        line = ""
        for word in text.split():
            if line and len(line) + len(word) + 1 > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        return out + [line] if line else out

    # -- input

    def prompt(self, label: str) -> str:
        buf = ""
        while True:
            self.draw(prompt=(label, buf))
            key = read_key()
            if key == "enter":
                return buf.strip()
            if key == "esc":
                return ""
            if key == "backspace":
                buf = buf[:-1]
            elif len(key) == 1 and key.isprintable() and len(buf) < 32:
                buf += key

    def confirm(self, question: str) -> bool:
        self.draw(prompt=(f"{question} (y/N)", ""))
        return read_key().lower() == "y"

    def busy(self, text: str) -> None:
        self.note(text + ELL)
        self.draw()

    # -- actions

    def run_foreground(self, argv, banner: str, config_dir) -> None:
        """Leave the TUI, run an interactive Claude Code command, come back."""
        leave_screen()
        print(f"\n  {C['coral']}{enc(banner)}{C['reset']}\n")
        try:
            subprocess.call(argv, env=clean_env(config_dir))
        except OSError as exc:
            print(f"  {C['warn']}{exc}{C['reset']}")
        try:
            input(f"\n  {C['dim']}press enter to return to ccsm{C['reset']} ")
        except EOFError:
            pass
        enter_screen()

    def do_add(self) -> None:
        if len(self.store.profiles) >= MAX_PROFILES:
            self.note(f"profile limit reached ({MAX_PROFILES})", "warn")
            return
        name = self.prompt("profile name")
        if not name:
            self.note("")
            return
        try:
            profile = self.store.add(name)
        except (ValueError, OSError) as exc:
            self.note(str(exc), "warn")
            return
        self.store.save()
        self.sel = self.store.profiles.index(profile)
        self._sign_in(profile, f"Signing in '{profile.name}'. Browser OAuth signs in one "
                               "account at a time - finish with the account you want here.")

    def do_adopt(self) -> None:
        name = self.prompt("adopt the signed-in account as")
        if not name:
            self.note("")
            return
        self.busy(f"adopting {name}")
        try:
            profile = adopt(self.store, self.creds, runtime_dir(), name)
        except (SwitchError, auth.AuthError, ValueError, OSError) as exc:
            self.note(str(exc), "warn")
            return
        self.sel = self.store.profiles.index(profile)
        self.note(f"{profile.name} stored as {mask_email(profile.email)}", "ok")

    def do_reverify(self) -> None:
        profile = self.current
        who = f" ({profile.email})" if profile.email else ""
        self._sign_in(profile, f"Reverifying '{profile.name}'{who}. Only this profile is "
                               "touched - every other stored credential is left alone.")

    def _sign_in(self, profile, banner: str) -> None:
        """Enrol or reverify one account: /login into its scratch dir, then store it."""
        ok, why = self.creds.available()
        if not ok:
            self.note(why, "warn")
            return
        try:
            argv = auth.login_argv(profile.email)
        except auth.AuthError as exc:
            self.note(str(exc), "warn")
            return
        self.run_foreground(argv, banner, profile.enroll_dir)
        auth.refresh(profile, profile.enroll_dir)
        if profile.auth_state == OK and capture(self.creds, profile, profile.enroll_dir):
            self.store.save()
            if self.store.active is None:
                self.do_switch(silent=True)
            self.note(f"{profile.name} stored as {mask_email(profile.email)}", "ok")
        else:
            profile.auth_state = REVERIFY
            self.store.save()
            self.note(f"{profile.name} was not stored - press r to retry", "warn")

    def do_remove(self) -> None:
        profile = self.current
        if not self.confirm(f"remove '{profile.name}' and delete its runtime data?"):
            self.note("")
            return
        self.busy(f"removing {profile.name}")
        auth.logout(profile.enroll_dir)
        shutil.rmtree(profile.enroll_dir, ignore_errors=True)
        try:
            self.creds.drop(profile.id)
        except NotImplementedError:
            pass
        self.store.remove(profile)
        self.store.save()
        self.sel = max(0, min(self.sel, len(self.store.profiles) - 1))
        self.note(f"removed {profile.name}", "ok")

    def do_switch(self, silent: bool = False) -> None:
        """Publish this profile's stored credential into the live runtime."""
        profile = self.current
        self.busy(f"switching to {profile.name}")
        try:
            result = switch(self.store, profile, self.creds, runtime_dir())
        except (SwitchError, auth.AuthError) as exc:
            self.note(f"SWITCH FAILED - {exc}", "warn")
            return
        if silent:
            return
        note = "; ".join(result["notes"])
        tail = f" ({note})" if note else ""
        if result["already_active"]:
            self.note(f"{profile.name} is already active{tail}")
        else:
            self.note(f"{profile.name} active as {mask_email(profile.email)}"
                      f" - applies to the next request{tail}", "ok")

    def do_launch(self) -> None:
        """Start Claude Code in the shared runtime as whichever profile is active."""
        active = self.store.get(self.store.active)
        if active is None:
            self.note("no active profile - press enter to switch to one first", "warn")
            return
        self.busy(f"verifying {active.name}")
        try:
            ok, why = preflight(active, runtime_dir())
        except auth.AuthError as exc:
            self.note(str(exc), "warn")
            return
        self.store.save()
        if not ok:
            self.note(why, "warn")
            return
        leave_screen()
        print(enc(f"{C['dim']}ccsm → {active.name} ({mask_email(active.email)}) "
                  f"{MID} {runtime_dir()}{C['reset']}"))
        raise SystemExit(launch(runtime_dir()))

    def do_refresh_all(self) -> None:
        if not self.store.profiles:
            return
        for profile in self.store.profiles:
            self.busy(f"checking {profile.name}")
            if profile.id == self.store.active:
                auth.refresh(profile, runtime_dir(), adopt=False)
            else:
                auth.quick_refresh(profile, self.creds)
        self.store.save()
        self.note("refreshed", "ok")

    def open_usage(self) -> None:
        self.metrics = usage.collect(runtime_dir())
        self.metric = 0
        self.view = "usage"
        self.note("")

    # -- loop

    def handle(self, key: str) -> bool:
        if self.view == "usage":
            if key in ("q", "esc", "u"):
                self.view = "list"
            elif key == "up":
                self.metric = (self.metric - 1) % len(self.metrics)
            elif key == "down":
                self.metric = (self.metric + 1) % len(self.metrics)
            elif key == "R":
                self.metrics = usage.collect(runtime_dir())
            return key != "q"

        n = len(self.store.profiles)
        if key in ("q", "esc"):
            return False
        if key == "R":
            self.do_refresh_all()
        elif key == "a":
            self.do_add()
        elif key == "A":
            self.do_adopt()
        elif not n:
            pass
        elif key == "up":
            self.sel = (self.sel - 1) % n
        elif key == "down":
            self.sel = (self.sel + 1) % n
        elif key == "enter":
            self.do_switch()
        elif key == "l":
            self.do_launch()
        elif key == "r":
            self.do_reverify()
        elif key == "d":
            self.do_remove()
        elif key == "u":
            self.open_usage()
        return True

    def run(self) -> int:
        setup_terminal()
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("ccsm: needs an interactive terminal (try `ccsm list`)", file=sys.stderr)
            return 1
        for profile in self.store.profiles:
            auth.quick_refresh(profile, self.creds)
        self.store.save()
        enter_screen()
        try:
            while True:
                self.draw()
                if self.handle(read_key()) is False:
                    return 0
        except KeyboardInterrupt:
            return 130
        finally:
            leave_screen()


def run() -> int:
    return App().run()
