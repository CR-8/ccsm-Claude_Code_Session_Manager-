"""The switch operation - harvest, publish both files, verify, roll back on any failure.

  1 take ccsm's switch lock          6 publish credential AND oauthAccount atomically
  2 wait out Claude Code's refresh   7 verify the runtime reports the target accountUuid
  3 snapshot the live account        8 mark the target active
  4 harvest it to its own profile    9 release the lock
  5 read the target bundle

Two things here are load-bearing.

Step 3-4 (harvest): Claude Code refreshes OAuth tokens in place, so the live credential
drifts ahead of the store. Publish over it without harvesting and the outgoing profile
keeps a stale token, sending the user back to a browser they did not need.

Step 6-7 (both files, or neither): an account is a credential *and* an oauthAccount block.
Publishing one without the other leaves a session whose requests go to one account while
its identity reports another. If anything after the first write fails, both files are put
back exactly as they were.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import auth
from .credentials import (account_email, account_uuid, bundle_account, bundle_credentials,
                          config_json_path, harden, read_account, same_account)
from .profiles import OK, REVERIFY, Profile, ProfileStore

LOCK_NAME = "switch.lock"
LOCK_STALE_SECONDS = 120
DEFAULT_TIMEOUT = 30.0


class SwitchError(RuntimeError):
    """A switch did not complete. The runtime is left exactly as it was found."""


def _stale(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS
    except OSError:
        return False


@contextmanager
def ccsm_lock(home, timeout: float = DEFAULT_TIMEOUT):
    """Serialise ccsm's own switches. Not Claude Code's lock - that one is waited on."""
    path = Path(home) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()} {int(time.time())}".encode())
            os.close(fd)
            break
        except FileExistsError:
            if _stale(path):
                path.unlink(missing_ok=True)
                continue
            if time.time() >= deadline:
                raise SwitchError("another ccsm switch is already in progress")
            time.sleep(0.1)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def wait_for_refresh(store, runtime, timeout: float = DEFAULT_TIMEOUT) -> float:
    """Block while Claude Code holds its OAuth refresh lock. Returns seconds waited."""
    start = time.time()
    while store.live_busy(runtime):
        if time.time() - start >= timeout:
            raise SwitchError(
                "Claude Code is still refreshing its token - try again in a moment")
        time.sleep(0.1)
    return time.time() - start


def owner_of(store, profiles: ProfileStore, live) -> Profile | None:
    """Which enrolled profile the live bundle belongs to, by accountUuid."""
    if account_uuid(live) is None:
        return None
    for profile in profiles.profiles:
        if same_account(store.get(profile.id), live):
            return profile
    return None


def harvest(store, profiles: ProfileStore, runtime) -> str | None:
    """Save the live bundle back to the profile it actually belongs to.

    Requires positive proof of identity: a live credential is only ever written to a
    profile whose stored accountUuid matches. A missing identity means 'not proven',
    never 'assume yes'.
    """
    live = store.read_live(runtime)
    if bundle_credentials(live) is None:
        return None
    current = profiles.get(profiles.active)
    if current is None:
        return None
    stored = store.get(current.id)
    if stored is None:
        return None
    if not same_account(stored, live):
        return f"live account is not {current.id}'s - not harvested"
    if stored.get("credentials") == live.get("credentials") \
            and stored.get("account") == live.get("account"):
        return None
    store.put(current.id, live)
    return f"harvested a refreshed credential for {current.id}"


def verify(target_bundle, runtime, timeout: int = auth.STATUS_TIMEOUT):
    """Confirm the runtime now *is* the target account. Costs no model request.

    Two independent checks: that the identity we published actually landed on disk, and
    that Claude Code itself reads the runtime as that account.
    """
    want_uuid = account_uuid(target_bundle)
    if want_uuid is None:
        return None, ("this profile was stored before ccsm tracked account identity - "
                      "re-enrol it with `ccsm adopt <name>` before switching")

    landed = read_account(runtime)
    if not landed or landed.get("accountUuid") != want_uuid:
        return None, "the published identity did not land in .claude.json"

    st = auth.status(runtime, timeout)
    if st is None:
        return None, "could not run `claude auth status` to verify the switch"
    if not st.get("loggedIn"):
        return None, "the published credential reports it is not logged in"
    want_email = account_email(target_bundle)
    live_email = st.get("email")
    if want_email and live_email and live_email.lower() != want_email.lower():
        return None, f"runtime reports {live_email}, expected {want_email}"
    return st, None


def switch(profiles: ProfileStore, target: Profile, store, runtime,
           timeout: float = DEFAULT_TIMEOUT, check_identity: bool = True) -> dict:
    """Publish `target` into the live runtime. All-or-nothing. Returns a summary."""
    ok, why = store.available()
    if not ok:
        raise SwitchError(why)

    result = {"profile": target.id, "notes": [], "waited": 0.0, "already_active": False}

    with ccsm_lock(profiles.home, timeout):                                    # 1
        result["waited"] = wait_for_refresh(store, runtime, timeout)           # 2
        note = harvest(store, profiles, runtime)                               # 3, 4
        if note:
            result["notes"].append(note)

        # Snapshot everything a rollback would have to put back.
        previous = store.read_live(runtime)

        if bundle_credentials(previous) is not None \
                and owner_of(store, profiles, previous) is None:
            raise SwitchError(
                f"the account already signed in at {runtime} is not enrolled in ccsm - "
                f"run `ccsm adopt <name>` to keep it before switching")

        if profiles.active == target.id and bundle_credentials(previous) is not None:
            result["already_active"] = True
            return result

        bundle = store.get(target.id)                                          # 5
        if bundle is None:
            raise SwitchError(
                f"'{target.id}' has no stored credential - reverify it before switching")
        if check_identity and bundle_account(bundle) is None:
            raise SwitchError(
                f"'{target.id}' was stored before ccsm tracked account identity - "
                f"re-enrol it with `ccsm adopt {target.id}` before switching")

        try:
            store.write_live(runtime, bundle)                                  # 6
            harden(runtime)
            if check_identity:                                                 # 7
                st, problem = verify(bundle, runtime)
                if problem:
                    raise SwitchError(f"switch failed - {problem}")
                auth.apply_status(target, st)
            else:
                target.auth_state = OK
        except BaseException:
            store.restore_live(runtime, previous)                              # roll back
            target.auth_state = REVERIFY
            profiles.save()
            raise

        result["identity"] = account_email(bundle) or target.email
        profiles.set_active(target.id)                                         # 8
        profiles.save()
    return result                                                              # 9


def capture(store, profile: Profile, source_dir) -> bool:
    """Take the credential and identity a /login just wrote, and store them together."""
    bundle = store.read_live(source_dir)
    if bundle_credentials(bundle) is None:
        return False
    store.put(profile.id, bundle)
    return True


def adopt(profiles: ProfileStore, store, runtime, name: str) -> Profile:
    """Enrol the account already signed in at `runtime`, without a browser round trip."""
    ok, why = store.available()
    if not ok:
        raise SwitchError(why)
    bundle = store.read_live(runtime)
    if bundle_credentials(bundle) is None:
        raise SwitchError(f"no Claude credential in {runtime} to adopt")
    if bundle_account(bundle) is None:
        raise SwitchError(f"{runtime} has a credential but no account identity in "
                          f"{config_json_path(runtime)} - sign in there first")
    profile = profiles.get(name)
    already = owner_of(store, profiles, bundle)
    if already is not None and already is not profile:
        raise SwitchError(f"that account is already enrolled as '{already.id}'")
    if profile is None:
        profile = profiles.add(name)
    elif already is None and account_uuid(store.get(profile.id)):
        # The name holds a different, identified account. Storing over it would destroy
        # that account's only credential. A legacy entry with no identity may be re-enrolled.
        raise SwitchError(f"'{profile.id}' already holds a different account - "
                          f"remove it first or pick another name")
    store.put(profile.id, bundle)
    auth.refresh(profile, runtime)
    # Only claim 'active' when nothing else is: adopting from an enrolment directory
    # does not make that account the one live in the session's config dir.
    if profiles.active is None:
        profiles.set_active(profile.id)
    profiles.save()
    return profile
