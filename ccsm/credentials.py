"""CredentialStore - where each account's identity is kept, and how one is published.

A Claude account in Claude Code is TWO files, not one:

  <config dir>/.credentials.json   the OAuth token, which authenticates requests
  .claude.json -> oauthAccount     the identity: accountUuid, email, org, tier

Swapping only the first leaves a half-switched session: requests go out on the new
token while everything identity-shaped still reports the old account. ccsm therefore
stores and publishes both together, as a bundle.

Where `.claude.json` lives is asymmetric and easy to get wrong:

  runtime == ~/.claude (default)   ~/.claude.json      BESIDE the directory
  runtime == anything else         <runtime>/.claude.json  INSIDE it

Nothing here logs, prints or returns a token value except to hand it straight back to
Claude Code's own credential location.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CREDENTIALS_FILE = ".credentials.json"
CONFIG_FILE = ".claude.json"
ACCOUNT_KEY = "oauthAccount"
KEYCHAIN_SERVICE = "Claude Code-credentials"
BACKUP_SUFFIX = ".ccsm-backup"
TMP_SUFFIX = ".ccsm-tmp"
BUNDLE_VERSION = 1

# Claude Code takes this lockfile while refreshing an OAuth token, and raises
# OAuthRefreshLockContendedError ("another process is refreshing") on contention.
LIVE_LOCK_SUFFIX = ".lock"

DEFAULT_CONFIG_DIR = Path("~/.claude").expanduser()

# Environment that can silently point Claude Code at a *different* account or provider.
AUTH_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_VERTEX_PROJECT_ID",
)

# Non-secret keys we are willing to read out of a credential.
SAFE_KEYS = ("expiresAt", "refreshTokenExpiresAt", "subscriptionType", "rateLimitTier")


def clean_env(config_dir, base: dict | None = None) -> dict:
    """Child environment pinned to one runtime, with ambient auth removed.

    Setting CLAUDE_CONFIG_DIR to the default directory is NOT the same as leaving it
    unset: Claude Code keeps `.claude.json` beside `~/.claude/` normally but inside the
    config dir once the variable is set, so forcing it makes Claude Code read an empty
    config and lose the account identity, project history and trust settings.
    """
    env = dict(os.environ if base is None else base)
    for key in AUTH_ENV:
        env.pop(key, None)
    if Path(config_dir) == DEFAULT_CONFIG_DIR:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


# --- paths -------------------------------------------------------------------

def credentials_path(config_dir) -> Path:
    return Path(config_dir) / CREDENTIALS_FILE


def config_json_path(config_dir) -> Path:
    """Where Claude Code keeps .claude.json for this config dir. See module docstring."""
    directory = Path(config_dir)
    if directory == DEFAULT_CONFIG_DIR:
        return directory.parent / CONFIG_FILE
    return directory / CONFIG_FILE


# --- bundles -----------------------------------------------------------------

def is_claude_credential(cred) -> bool:
    """Shape check only - guards against publishing or storing junk."""
    return (isinstance(cred, dict)
            and isinstance(cred.get("claudeAiOauth"), dict)
            and bool(cred["claudeAiOauth"].get("accessToken")))


def is_account(account) -> bool:
    return isinstance(account, dict) and bool(account.get("accountUuid"))


def make_bundle(credentials=None, account=None) -> dict:
    return {"version": BUNDLE_VERSION, "credentials": credentials, "account": account}


def bundle_credentials(bundle):
    if not isinstance(bundle, dict):
        return None
    cred = bundle.get("credentials")
    return cred if is_claude_credential(cred) else None


def bundle_account(bundle):
    if not isinstance(bundle, dict):
        return None
    account = bundle.get("account")
    return account if is_account(account) else None


def account_uuid(bundle) -> str | None:
    """The canonical account identity. organizationUuid is optional and unreliable."""
    account = bundle_account(bundle)
    return account.get("accountUuid") if account else None


def account_email(bundle) -> str | None:
    account = bundle_account(bundle)
    return account.get("emailAddress") if account else None


def same_account(a, b) -> bool:
    """True only on positive proof that two bundles are the same account.

    A missing identity is never treated as agreement - that was the bug that let a
    foreign credential be stored against the wrong profile.
    """
    ua, ub = account_uuid(a), account_uuid(b)
    return bool(ua) and ua == ub


def summarize(bundle) -> dict:
    """Non-secret view. Never contains token material."""
    cred = bundle_credentials(bundle) or (bundle if is_claude_credential(bundle) else None)
    if cred is None:
        return {"present": False}
    oauth = cred["claudeAiOauth"]
    out = {"present": True}
    for key in SAFE_KEYS:
        if oauth.get(key) is not None:
            out[key] = oauth[key]
    account = bundle_account(bundle)
    if account:
        out["accountUuid"] = account.get("accountUuid")
        out["emailAddress"] = account.get("emailAddress")
    return out


def expired(state: dict) -> bool:
    """True only on positive evidence of expiry - never on a guess."""
    ms = state.get("refreshTokenExpiresAt")
    return bool(ms) and ms / 1000.0 < time.time()


def local_state(config_dir) -> dict:
    """Non-secret credential metadata for a config dir."""
    path = credentials_path(config_dir)
    if not path.exists():
        return {"present": False}
    try:
        return summarize(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError):
        return {"present": True, "unreadable": True}


def harden(config_dir) -> None:
    """Best-effort POSIX permissions on a runtime directory."""
    if os.name != "posix":
        return
    try:
        os.chmod(config_dir, 0o700)
        path = credentials_path(config_dir)
        if path.exists():
            os.chmod(path, 0o600)
    except OSError:
        pass


# --- careful writes ----------------------------------------------------------

def _write_private(path: Path, payload: dict) -> None:
    """Atomic write at mode 0600, with no world-readable window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def backup_config_json(config_dir) -> Path | None:
    """Copy .claude.json aside once, before ccsm ever modifies it.

    Written only if no backup exists, so the very first copy is the pristine
    pre-ccsm state and later switches cannot overwrite it.
    """
    path = config_json_path(config_dir)
    if not path.exists():
        return None
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)          # preserves mode and timestamps
    return backup


def read_account(config_dir):
    """The oauthAccount block from this runtime's .claude.json, or None."""
    try:
        data = json.loads(config_json_path(config_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    account = data.get(ACCOUNT_KEY) if isinstance(data, dict) else None
    return account if is_account(account) else None


def write_account(config_dir, account) -> None:
    """Replace ONLY the oauthAccount key in .claude.json.

    Every other key is preserved byte-for-byte in value. The file is never rebuilt from
    scratch, never truncated in place, and permissions are carried over. Passing None
    removes the key rather than writing a null.
    """
    path = config_json_path(config_dir)
    backup_config_json(config_dir)

    existed = path.exists()
    if existed:
        raw = path.read_text("utf-8")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"refusing to modify unreadable {path}: {exc}")
        if not isinstance(data, dict):
            raise ValueError(f"refusing to modify {path}: not a JSON object")
        indent = 2 if raw.startswith("{\n") else None
    else:
        data, indent = {}, 2

    if account is None:
        data.pop(ACCOUNT_KEY, None)
    else:
        data[ACCOUNT_KEY] = account

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    tmp.write_text(json.dumps(data, indent=indent), encoding="utf-8")
    if existed:
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


# --- stores ------------------------------------------------------------------

class CredentialStore:
    """One account bundle per profile, plus publish/harvest against a live runtime."""

    name = "abstract"

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def get(self, profile_id: str):
        raise NotImplementedError

    def put(self, profile_id: str, bundle: dict) -> None:
        raise NotImplementedError

    def drop(self, profile_id: str) -> None:
        raise NotImplementedError

    def read_live(self, runtime) -> dict:
        raise NotImplementedError

    def write_live(self, runtime, bundle: dict) -> None:
        raise NotImplementedError

    def restore_live(self, runtime, bundle) -> None:
        raise NotImplementedError

    def live_busy(self, runtime) -> bool:
        raise NotImplementedError


class FileCredentialStore(CredentialStore):
    """Credential files. Proven on Windows; the same shape is expected on Linux/WSL."""

    name = "file"

    def __init__(self, home) -> None:
        self.slots = Path(home) / "store"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def _slot(self, profile_id: str) -> Path:
        return self.slots / f"{profile_id}.json"

    def get(self, profile_id):
        try:
            raw = json.loads(self._slot(profile_id).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if is_claude_credential(raw):
            # Pre-bundle store entry: a bare credential with no identity attached.
            return make_bundle(raw, None)
        return raw if bundle_credentials(raw) else None

    def put(self, profile_id, bundle):
        if bundle_credentials(bundle) is None:
            raise ValueError("refusing to store a bundle without a Claude credential")
        self.slots.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.slots, 0o700)
        _write_private(self._slot(profile_id), bundle)

    def drop(self, profile_id):
        self._slot(profile_id).unlink(missing_ok=True)

    def read_live(self, runtime) -> dict:
        """Always returns a bundle; either half may be None."""
        try:
            cred = json.loads(credentials_path(runtime).read_text("utf-8"))
        except (OSError, ValueError):
            cred = None
        if not is_claude_credential(cred):
            cred = None
        return make_bundle(cred, read_account(runtime))

    def write_live(self, runtime, bundle):
        cred = bundle_credentials(bundle)
        if cred is None:
            raise ValueError("refusing to publish a bundle without a Claude credential")
        Path(runtime).mkdir(parents=True, exist_ok=True)
        _write_private(credentials_path(runtime), cred)
        harden(runtime)
        account = bundle_account(bundle)
        if account is not None:
            write_account(runtime, account)

    def restore_live(self, runtime, bundle) -> None:
        """Put the runtime back exactly as `bundle` describes it, including emptiness."""
        cred = bundle_credentials(bundle)
        if cred is None:
            credentials_path(runtime).unlink(missing_ok=True)
        else:
            _write_private(credentials_path(runtime), cred)
            harden(runtime)
        write_account(runtime, bundle_account(bundle))

    def live_busy(self, runtime):
        path = credentials_path(runtime)
        return path.with_name(path.name + LIVE_LOCK_SUFFIX).exists()


class KeychainCredentialStore(CredentialStore):
    """macOS placeholder - deliberately not implemented rather than guessed at."""

    name = "keychain"
    REASON = ("macOS support is not implemented. Claude Code keeps credentials in the "
              "login Keychain, keyed to the config directory, and the naming is "
              "undocumented. Set CCSM_FORCE_FILE_STORE=1 to try the file backend at "
              "your own risk.")

    def available(self) -> tuple[bool, str]:
        return False, self.REASON

    def _no(self, *_a, **_k):
        raise NotImplementedError(self.REASON)

    get = put = drop = read_live = write_live = restore_live = live_busy = _no


def keychain_present() -> bool:
    """macOS only: does a Claude Code Keychain item exist? Presence only, never the secret."""
    if sys.platform != "darwin" or not shutil.which("security"):
        return False
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_store(home) -> CredentialStore:
    """The backend for this platform."""
    if sys.platform == "darwin" and not os.environ.get("CCSM_FORCE_FILE_STORE"):
        return KeychainCredentialStore()
    return FileCredentialStore(home)
