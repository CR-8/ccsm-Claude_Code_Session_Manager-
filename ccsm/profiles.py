"""ProfileStore - local metadata for the isolated Claude Code profiles.

Metadata only. Secrets stay with Claude Code inside each profile's
CLAUDE_CONFIG_DIR (see credentials.py).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_PROFILES = 5
SCHEMA_VERSION = 1

OK = "OK"
REVERIFY = "REVERIFY"
UNKNOWN = "UNKNOWN"

MASK = "•"


def ccsm_home() -> Path:
    """Root for ccsm's own data. CCSM_HOME overrides (used by tests)."""
    override = os.environ.get("CCSM_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or "~/AppData/Roaming"
    elif sys.platform == "darwin":
        base = "~/Library/Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "ccsm"


def runtime_dir() -> Path:
    """The CLAUDE_CONFIG_DIR whose credential a switch publishes into.

    Defaults to the Claude Code install you actually use, because switching a private
    sandbox would have no effect on the session you are sitting in. Resolution order:

      CCSM_RUNTIME        explicit override
      CLAUDE_CONFIG_DIR   the config dir of the session invoking ccsm, when it is set
      ~/.claude           Claude Code's own default

    The conversation, history and tool state live here and survive a switch; only the
    credential inside it changes.
    """
    for var in ("CCSM_RUNTIME", "CLAUDE_CONFIG_DIR"):
        value = os.environ.get(var)
        if value:
            return Path(value).expanduser()
    return Path("~/.claude").expanduser()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", (name or "").strip().lower()).strip("-.")[:32] or "profile"


def mask_email(email: str | None) -> str:
    """Masked identity for display. Never shows the full local part."""
    if not email:
        return "Unavailable"
    local, at, domain = email.partition("@")
    if not at:
        return local[:1] + MASK * 5
    dots = MASK * min(5, max(3, len(local) - 2))
    tail = local[-1:] if len(local) > 2 else ""
    return f"{local[:1]}{dots}{tail}@{domain}"


@dataclass
class Profile:
    id: str
    name: str
    email: str | None = None
    org: str | None = None
    plan: str | None = None
    auth_method: str | None = None
    auth_state: str = UNKNOWN
    last_verified: float | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def enroll_dir(self) -> Path:
        """A scratch CLAUDE_CONFIG_DIR used only to run /login for this account.

        Not where Claude Code runs. The credential this produces is harvested into the
        credential store; the directory itself is disposable.
        """
        return ccsm_home() / "enroll" / self.id


class ProfileStore:
    def __init__(self) -> None:
        self.profiles: list[Profile] = []
        self.active: str | None = None
        self.load()

    @property
    def home(self) -> Path:
        return ccsm_home()

    @property
    def path(self) -> Path:
        return self.home / "profiles.json"

    def load(self) -> None:
        self.profiles, self.active = [], None
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except OSError:
            return
        except ValueError:
            # Keep a corrupt file instead of silently overwriting it on the next save.
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".corrupt"))
            except OSError:
                pass
            return
        known = {f.name for f in dataclasses.fields(Profile)}
        self.profiles = [
            Profile(**{k: v for k, v in p.items() if k in known})
            for p in raw.get("profiles", [])
            if isinstance(p, dict) and p.get("id")
        ]
        self.active = raw.get("active")

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        data = {
            "version": SCHEMA_VERSION,
            "active": self.active,
            "profiles": [dataclasses.asdict(p) for p in self.profiles],
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, ident: str | None) -> Profile | None:
        if not ident:
            return None
        key = ident.strip().lower()
        for p in self.profiles:
            if p.id == key or p.name.lower() == key:
                return p
        return None

    def add(self, name: str) -> Profile:
        if len(self.profiles) >= MAX_PROFILES:
            raise ValueError(f"profile limit reached ({MAX_PROFILES})")
        name = (name or "").strip()
        if not name:
            raise ValueError("a profile name is required")
        pid = slugify(name)
        if any(p.id == pid for p in self.profiles):
            raise ValueError(f"profile '{pid}' already exists")
        p = Profile(id=pid, name=name)
        p.enroll_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(p.enroll_dir, 0o700)
        self.profiles.append(p)
        return p

    def remove(self, profile: Profile) -> None:
        self.profiles = [p for p in self.profiles if p.id != profile.id]
        if self.active == profile.id:
            self.active = None

    def set_active(self, profile_id: str | None) -> None:
        self.active = profile_id
