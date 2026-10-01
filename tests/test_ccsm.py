"""Checks for the logic that would fail quietly.

Run either way:  python tests/test_ccsm.py   |   python -m pytest tests
No network, no subprocess, no Claude Code install required, and no test ever touches a
real ~/.claude: CCSM_RUNTIME is pinned inside a temp tree before ccsm is imported.
"""

import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="ccsm-test-")
os.environ["CCSM_HOME"] = _TMP
os.environ["CCSM_FORCE_FILE_STORE"] = "1"      # exercise the file backend on any platform
# runtime_dir() defaults to the real ~/.claude. Pin it inside the temp tree so no test
# can ever publish over a working login.
os.environ["CCSM_RUNTIME"] = str(Path(_TMP) / "runtime")
os.environ.pop("CLAUDE_CONFIG_DIR", None)

from ccsm import auth, tui, usage  # noqa: E402
from ccsm.credentials import (  # noqa: E402
    AUTH_ENV, DEFAULT_CONFIG_DIR, FileCredentialStore, KeychainCredentialStore,
    account_uuid, bundle_credentials, clean_env, config_json_path, expired,
    is_claude_credential, make_bundle, open_store, read_account, same_account,
    summarize, write_account,
)
from ccsm.profiles import (  # noqa: E402
    MAX_PROFILES, OK, REVERIFY, UNKNOWN, Profile, ProfileStore, ccsm_home, mask_email,
    runtime_dir, slugify,
)
from ccsm.switcher import (  # noqa: E402
    SwitchError, adopt, ccsm_lock, harvest, owner_of, switch,
)

UUID_A = "aaaaaaaa-0000-4000-8000-00000000000a"
UUID_B = "bbbbbbbb-0000-4000-8000-00000000000b"


def cred(token, org=None, refresh_days=30):
    far = int((time.time() + refresh_days * 86400) * 1000)
    out = {"claudeAiOauth": {"accessToken": token, "refreshToken": "r-" + token,
                             "expiresAt": far, "refreshTokenExpiresAt": far,
                             "subscriptionType": "max"}}
    if org is not None:                    # organizationUuid is OPTIONAL in real life
        out["organizationUuid"] = org
    return out


def account(uuid, email):
    return {"accountUuid": uuid, "emailAddress": email, "organizationName": "Org",
            "userRateLimitTier": "default_claude_ai"}


def bundle(token, uuid, email, org=None):
    return make_bundle(cred(token, org), account(uuid, email))


def token_of(b):
    c = bundle_credentials(b)
    return c["claudeAiOauth"]["accessToken"] if c else None


def fresh(tag, seed_config=None):
    """Isolated ccsm home + runtime with two enrolled accounts, 'personal' active."""
    os.environ["CCSM_HOME"] = str(Path(_TMP) / tag)
    rt = Path(_TMP) / tag / "runtime"
    os.environ["CCSM_RUNTIME"] = str(rt)
    rt.mkdir(parents=True, exist_ok=True)
    if seed_config is not None:
        config_json_path(rt).write_text(json.dumps(seed_config, indent=2), encoding="utf-8")
    profiles = ProfileStore()
    creds = open_store(ccsm_home())
    a, b = profiles.add("personal"), profiles.add("company")
    a.email, b.email = "a@x.com", "b@y.com"
    profiles.save()
    creds.put("personal", bundle("A1", UUID_A, "a@x.com", org="org-a"))
    creds.put("company", bundle("B1", UUID_B, "b@y.com"))     # no organizationUuid
    switch(profiles, a, creds, rt, check_identity=False)
    return profiles, creds, a, b, rt


# ---------- identity + storage basics ----------

def test_slug_and_mask():
    assert slugify("Work Account!") == "work-account"
    assert slugify("   ") == "profile"
    masked = mask_email("sshreyansh8070@gmail.com")
    assert masked.startswith("s") and masked.endswith("@gmail.com")
    assert "shreyansh" not in masked and "8070" not in masked
    assert mask_email(None) == "Unavailable"


def test_store_roundtrip_limit_and_active():
    os.environ["CCSM_HOME"] = str(Path(_TMP) / "store")
    profiles = ProfileStore()
    for i in range(MAX_PROFILES):
        profiles.add(f"p{i}")
    profiles.save()
    for bad in ("overflow", "p0"):
        try:
            profiles.add(bad)
            raise AssertionError(f"add({bad!r}) should have been rejected")
        except ValueError:
            pass
    again = ProfileStore()
    assert [p.id for p in again.profiles] == [f"p{i}" for i in range(MAX_PROFILES)]
    p0 = again.get("P0")
    assert p0 is not None and p0.enroll_dir.is_dir()
    again.set_active("p0")
    again.save()
    assert ProfileStore().active == "p0"
    again.remove(p0)
    again.save()
    assert ProfileStore().get("p0") is None
    assert ProfileStore().active is None, "active must not point at a removed profile"


def test_clean_env_scrubs_ambient_auth():
    base = {k: "leak" for k in AUTH_ENV}
    base["PATH"] = "/bin"
    env = clean_env("/tmp/cfg", base)
    assert not (set(AUTH_ENV) & set(env)), "ambient auth leaked into a runtime"
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/cfg" and env["PATH"] == "/bin"


def test_clean_env_leaves_the_default_config_dir_alone():
    """Forcing CLAUDE_CONFIG_DIR=~/.claude makes Claude Code read a different, empty config."""
    env = clean_env(DEFAULT_CONFIG_DIR, {"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/stale"})
    assert "CLAUDE_CONFIG_DIR" not in env, \
        "the default dir must be left to Claude Code's own resolution"
    assert clean_env("/somewhere/else", {})["CLAUDE_CONFIG_DIR"] == "/somewhere/else"


def test_config_json_lives_beside_the_default_dir_and_inside_any_other():
    assert config_json_path(DEFAULT_CONFIG_DIR) == DEFAULT_CONFIG_DIR.parent / ".claude.json"
    other = Path(_TMP) / "elsewhere"
    assert config_json_path(other) == other / ".claude.json"


def test_credential_store_rejects_junk_and_hides_tokens():
    os.environ["CCSM_HOME"] = str(Path(_TMP) / "junk")
    store = FileCredentialStore(ccsm_home())
    for bad in (make_bundle(None, account(UUID_A, "a@x.com")), {"nope": 1}, "string", None):
        try:
            store.put("x", bad)
            raise AssertionError(f"stored junk: {bad!r}")
        except (ValueError, TypeError, AttributeError):
            pass
    assert not is_claude_credential({"claudeAiOauth": {"accessToken": ""}})
    good = bundle("SECRET-TOKEN", UUID_A, "a@x.com")
    store.put("x", good)
    assert token_of(store.get("x")) == "SECRET-TOKEN"
    view = summarize(good)
    assert view["subscriptionType"] == "max" and view["accountUuid"] == UUID_A
    assert "SECRET-TOKEN" not in json.dumps(view), "summary must never carry token material"
    assert not expired(view)
    assert expired({"refreshTokenExpiresAt": 1000})
    assert not expired({"present": True}), "unknown expiry must not be guessed as expired"


def test_keychain_backend_is_honestly_unavailable():
    ok, why = KeychainCredentialStore().available()
    assert ok is False and "macOS" in why
    try:
        KeychainCredentialStore().get("x")
        raise AssertionError("keychain backend must not pretend to work")
    except NotImplementedError:
        pass


# ---------- account identity ----------

def test_identity_uses_account_uuid_not_optional_organization_uuid():
    """organizationUuid is absent on real credentials; accountUuid is the canonical key."""
    no_org = bundle("T", UUID_A, "a@x.com")
    assert bundle_credentials(no_org).get("organizationUuid") is None
    assert account_uuid(no_org) == UUID_A

    other = bundle("U", UUID_B, "b@y.com")
    assert not same_account(no_org, other), "two org-less accounts must not compare equal"
    assert same_account(no_org, bundle("REFRESHED", UUID_A, "a@x.com"))
    assert not same_account(make_bundle(cred("X"), None), make_bundle(cred("Y"), None)), \
        "a missing identity is never proof of sameness"


# ---------- .claude.json is high-risk ----------

def test_write_account_preserves_every_other_key_and_permissions():
    d = Path(_TMP) / "cfgsafe"
    d.mkdir(parents=True, exist_ok=True)
    path = config_json_path(d)
    original = {"projects": {"C:/work": {"history": [1, 2, 3]}},
                "mcpServers": {"x": {"command": "y", "env": {"K": "V"}}},
                "numStartups": 42, "oauthAccount": account(UUID_A, "a@x.com")}
    path.write_text(json.dumps(original, indent=2), encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)
    before_mode = stat.S_IMODE(path.stat().st_mode)

    write_account(d, account(UUID_B, "b@y.com"))

    after = json.loads(path.read_text("utf-8"))
    assert after["projects"] == original["projects"], "project history was altered"
    assert after["mcpServers"] == original["mcpServers"], "mcp config was altered"
    assert after["numStartups"] == 42
    assert after["oauthAccount"]["accountUuid"] == UUID_B, "identity was not replaced"
    assert set(after) == set(original), "keys were added or dropped"
    assert stat.S_IMODE(path.stat().st_mode) == before_mode, "permissions changed"
    backup = path.with_name(path.name + ".ccsm-backup")
    assert backup.exists(), "no backup was taken"
    assert json.loads(backup.read_text("utf-8"))["oauthAccount"]["accountUuid"] == UUID_A, \
        "backup is not the pre-ccsm state"


def test_write_account_never_rebuilds_an_unreadable_file():
    d = Path(_TMP) / "cfgbad"
    d.mkdir(parents=True, exist_ok=True)
    path = config_json_path(d)
    path.write_text("{ not json at all", encoding="utf-8")
    try:
        write_account(d, account(UUID_A, "a@x.com"))
        raise AssertionError("clobbered a file it could not parse")
    except ValueError as exc:
        assert "unreadable" in str(exc)
    assert path.read_text("utf-8") == "{ not json at all", "the file was modified anyway"


def test_write_account_leaves_no_temp_file_behind():
    d = Path(_TMP) / "cfgtmp"
    d.mkdir(parents=True, exist_ok=True)
    write_account(d, account(UUID_A, "a@x.com"))
    leftovers = [p.name for p in d.iterdir() if p.name.endswith(".ccsm-tmp")]
    assert not leftovers, f"atomic replace left a temp file: {leftovers}"
    assert read_account(d)["accountUuid"] == UUID_A


# ---------- the switch ----------

def test_switch_publishes_both_files_and_leaves_other_profiles_alone():
    profiles, creds, a, b, rt = fresh("basic")
    assert token_of(creds.read_live(rt)) == "A1"
    assert read_account(rt)["accountUuid"] == UUID_A
    before = creds.get("company")

    switch(profiles, b, creds, rt, check_identity=False)

    assert token_of(creds.read_live(rt)) == "B1", "credential was not published"
    assert read_account(rt)["accountUuid"] == UUID_B, "identity was not published"
    assert profiles.active == "company"
    assert creds.get("personal") is not None, "switching away must not delete a credential"
    assert creds.get("company") == before, "the target's stored bundle was mutated"


def test_round_trip_carries_credential_and_identity_together():
    profiles, creds, a, b, rt = fresh("roundtrip")
    for target, tok, uuid in ((b, "B1", UUID_B), (a, "A1", UUID_A),
                              (b, "B1", UUID_B), (a, "A1", UUID_A)):
        switch(profiles, target, creds, rt, check_identity=False)
        assert token_of(creds.read_live(rt)) == tok
        assert read_account(rt)["accountUuid"] == uuid, "identity drifted from the token"
    assert token_of(creds.get("personal")) == "A1"
    assert token_of(creds.get("company")) == "B1"


def test_existing_claude_json_survives_a_full_round_trip():
    seed = {"projects": {"C:/repo": {"lastCost": 1.25}}, "numStartups": 7,
            "oauthAccount": account(UUID_A, "a@x.com")}
    profiles, creds, a, b, rt = fresh("preserve", seed_config=seed)
    switch(profiles, b, creds, rt, check_identity=False)
    switch(profiles, a, creds, rt, check_identity=False)
    after = json.loads(config_json_path(rt).read_text("utf-8"))
    assert after["projects"] == seed["projects"], "project history did not survive"
    assert after["numStartups"] == 7
    assert after["oauthAccount"]["accountUuid"] == UUID_A


def test_harvest_prevents_republishing_a_stale_credential():
    """Claude Code refreshes A in place, so A1 must never come back."""
    profiles, creds, a, b, rt = fresh("stale")
    creds.write_live(rt, bundle("A2", UUID_A, "a@x.com", org="org-a"))   # refreshed
    switch(profiles, b, creds, rt, check_identity=False)
    assert token_of(creds.get("personal")) == "A2", "the refreshed token was not harvested"
    switch(profiles, a, creds, rt, check_identity=False)
    assert token_of(creds.read_live(rt)) == "A2", "republished a stale credential"


def test_harvest_refuses_a_foreign_account_by_account_uuid():
    profiles, creds, a, b, rt = fresh("foreign")
    # Someone ran /login in the runtime; a different account, carrying no
    # organizationUuid at all - the case that used to slip through.
    creds.write_live(rt, bundle("STRANGER", UUID_B, "b@y.com"))
    note = harvest(creds, profiles, rt)
    assert note and "not harvested" in note
    assert token_of(creds.get("personal")) == "A1", "another account's token was stored"


def test_switch_refuses_to_overwrite_an_unenrolled_account():
    profiles, creds, a, b, rt = fresh("guard")
    creds.write_live(rt, bundle("SOMEONE-ELSES-LOGIN",
                                "cccccccc-0000-4000-8000-0000000000cc", "stranger@z.com"))
    try:
        switch(profiles, b, creds, rt, check_identity=False)
        raise AssertionError("overwrote an account ccsm does not hold")
    except SwitchError as exc:
        assert "adopt" in str(exc)
    assert token_of(creds.read_live(rt)) == "SOMEONE-ELSES-LOGIN", "a login was destroyed"


def test_failed_verification_rolls_back_both_files():
    seed = {"projects": {"keep": 1}, "oauthAccount": account(UUID_A, "a@x.com")}
    profiles, creds, a, b, rt = fresh("verify", seed_config=seed)
    real_status = auth.status
    auth.status = lambda *_a, **_k: {"loggedIn": True, "email": "someone-else@z.com"}
    try:
        switch(profiles, b, creds, rt)
        raise AssertionError("a mismatched identity must fail the switch")
    except SwitchError as exc:
        assert "switch failed" in str(exc)
    finally:
        auth.status = real_status

    assert token_of(creds.read_live(rt)) == "A1", "credential was not rolled back"
    assert read_account(rt)["accountUuid"] == UUID_A, "identity was not rolled back"
    assert json.loads(config_json_path(rt).read_text("utf-8"))["projects"] == {"keep": 1}
    assert profiles.active == "personal", "a failed switch must not change the active profile"
    assert b.auth_state == REVERIFY


def test_switch_refuses_a_profile_stored_without_an_identity():
    """Pre-bundle store entries cannot be verified, so they must not be published blind."""
    profiles, creds, a, b, rt = fresh("legacy")
    creds.put("company", make_bundle(cred("B-LEGACY"), None))
    try:
        switch(profiles, b, creds, rt)
        raise AssertionError("published a bundle with no account identity")
    except SwitchError as exc:
        assert "re-enrol" in str(exc)
    assert token_of(creds.read_live(rt)) == "A1"
    assert profiles.active == "personal"


def test_switch_to_unenrolled_profile_is_refused():
    profiles, creds, a, b, rt = fresh("unenrolled")
    creds.drop("company")
    try:
        switch(profiles, b, creds, rt, check_identity=False)
        raise AssertionError("switched to a profile with no stored credential")
    except SwitchError as exc:
        assert "no stored credential" in str(exc)
    assert profiles.active == "personal"
    assert token_of(creds.read_live(rt)) == "A1"


def test_switch_lock_is_exclusive():
    profiles, creds, a, b, rt = fresh("lock")
    with ccsm_lock(profiles.home):
        try:
            switch(profiles, b, creds, rt, timeout=0.3, check_identity=False)
            raise AssertionError("two switches ran at once")
        except SwitchError as exc:
            assert "already in progress" in str(exc)
    switch(profiles, b, creds, rt, check_identity=False)   # lock released


def test_switch_waits_for_claude_code_refresh_lock():
    profiles, creds, a, b, rt = fresh("refresh")
    lock = rt / ".credentials.json.lock"
    lock.write_text("held", encoding="utf-8")
    try:
        switch(profiles, b, creds, rt, timeout=0.3, check_identity=False)
        raise AssertionError("published while Claude Code was refreshing")
    except SwitchError as exc:
        assert "refreshing" in str(exc)
    finally:
        lock.unlink(missing_ok=True)
    assert token_of(creds.read_live(rt)) == "A1"


def test_adopt_enrols_the_live_account_and_refuses_what_it_would_destroy():
    profiles, creds, a, b, rt = fresh("adopt")
    real_status = auth.status
    auth.status = lambda *_a, **_k: {"loggedIn": True, "email": "c@z.com"}
    try:
        creds.write_live(rt, bundle("C1", "cccccccc-0000-4000-8000-0000000000cc", "c@z.com"))
        assert "A adopts it" in tui.App()._live(), "an unenrolled live account went unflagged"

        for name, why in (("company", "different account"), ("PERSONAL", "different account")):
            try:
                adopt(profiles, creds, rt, name)
                raise AssertionError(f"adopted over {name}, which holds another account")
            except SwitchError as exc:
                assert why in str(exc)
        assert token_of(creds.get("company")) == "B1", "an enrolled credential was overwritten"

        work = adopt(profiles, creds, rt, "Work")
        assert token_of(creds.get("work")) == "C1" and work.email == "c@z.com"
        assert profiles.active == "personal", "adopting must not claim an existing active"
        assert "Work" in tui.App()._live(), "the live line does not name the adopted profile"
        assert adopt(profiles, creds, rt, "WORK") is work, "re-adopting the same account failed"
        try:
            adopt(profiles, creds, rt, "other")
            raise AssertionError("enrolled one account under two names")
        except SwitchError as exc:
            assert "already enrolled as 'work'" in str(exc)
    finally:
        auth.status = real_status

    try:
        adopt(profiles, creds, rt / "empty", "nobody")
        raise AssertionError("adopted from a runtime with no credential")
    except SwitchError as exc:
        assert "no Claude credential" in str(exc)


def test_tui_never_draws_past_the_terminal_width():
    """A line as wide as the terminal wraps, and every redraw after it lands one row off."""
    import contextlib
    import io
    import re

    profiles, creds, a, b, rt = fresh("fit")
    b.name = "A Profile Name Longer Than Its Column"
    b.org = "b@y.com's Organization"     # how Claude names a personal org
    profiles.save()
    switch(profiles, b, creds, rt, check_identity=False)
    saved = os.environ.get("COLUMNS")
    try:
        for cols in (44, 60, 80, 120):
            os.environ["COLUMNS"] = str(cols)
            app = tui.App()
            assert app.sel == 1, "the cursor should start on the active profile"
            for view in ("list", "usage", "help"):
                if view == "usage":
                    app.open_usage()
                app.view = view
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    app.draw()
                text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.getvalue())
                widest = max(len(ln) for ln in text.split("\r\n"))
                assert widest < cols, f"{view} drew {widest} columns into {cols}"
                assert "b@y.com" not in text, "the detail line unmasked an identity"

        app.view = "list"
        app.handle("k")
        assert app.sel == 0
        app.handle("2")
        assert app.sel == 1 and app.store.active == "company", "a digit must never switch"
        app.handle("?")
        assert app.view == "help" and app.handle("x") and app.view == "list"
    finally:
        if saved is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = saved


def test_tui_messages_say_what_to_do_and_whose_usage_it_is():
    import contextlib
    import io
    import re

    profiles, creds, a, b, rt = fresh("labels")
    real = auth.status, auth.version_warning, tui.read_key, os.environ.get("COLUMNS")
    calls = []
    auth.status = lambda *_a, **_k: {"loggedIn": True}
    auth.version_warning = lambda: calls.append(1) or "UNVERIFIED-VERSION"
    tui.read_key = lambda: "n"
    os.environ["COLUMNS"] = "60"

    def screen(action):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            action()
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.getvalue())
        return " ".join(ln.strip() for ln in text.split("\r\n"))

    try:
        app = tui.App()
        app.sel = 1
        app.do_switch()
        assert "UNVERIFIED-VERSION" in app.msg, "the TUI switch hid the version caution"
        app.sel = 0
        app.do_switch()
        assert "UNVERIFIED-VERSION" not in app.msg and len(calls) == 1, "cautioned twice"

        shown = screen(app.do_remove)
        assert "adopt it again" in shown, "removing the live account gave no warning"
        assert app.store.get("personal") is not None, "declining must keep the profile"

        app.open_usage()
        assert "shared by every profile" in screen(app.draw), "usage attributed to a profile"
        app.view = "list"

        creds.write_live(rt, bundle("X", "cccccccc-0000-4000-8000-0000000000cc", "c@z.com"))
        app.sel = 1
        app.do_switch()
        assert "to keep it before switching" in screen(app.draw), "the error's advice was cut"
    finally:
        auth.status, auth.version_warning, tui.read_key = real[:3]
        if real[3] is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = real[3]


def test_owner_of_identifies_by_account_uuid():
    profiles, creds, a, b, rt = fresh("owner")
    assert owner_of(creds, profiles, bundle("X", UUID_B, "b@y.com")).id == "company"
    assert owner_of(creds, profiles, make_bundle(cred("X"), None)) is None


def test_runtime_dir_never_silently_uses_the_real_claude_home():
    saved = os.environ.get("CCSM_RUNTIME")
    try:
        os.environ["CCSM_RUNTIME"] = str(Path(_TMP) / "explicit")
        assert runtime_dir() == Path(_TMP) / "explicit"
        del os.environ["CCSM_RUNTIME"]
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_TMP) / "session")
        assert runtime_dir() == Path(_TMP) / "session", "must follow the calling session"
        del os.environ["CLAUDE_CONFIG_DIR"]
        assert runtime_dir() == DEFAULT_CONFIG_DIR, "default is Claude Code's own"
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        if saved:
            os.environ["CCSM_RUNTIME"] = saved


# ---------- auth classification ----------

def test_auth_classification_and_identity_guard():
    p = Profile(id="cls", name="cls")
    auth.apply_status(p, {"loggedIn": True, "email": "a@b.com", "orgName": "Org",
                          "subscriptionType": "max", "authMethod": "claude.ai"})
    assert (p.auth_state, p.email, p.plan) == (OK, "a@b.com", "max")
    auth.apply_status(p, {"loggedIn": False, "authMethod": "none"})
    assert p.auth_state == REVERIFY

    p.auth_state = OK
    auth.apply_status(p, {"loggedIn": True, "email": "other@c.com"}, adopt=False)
    assert p.email == "a@b.com", "adopt=False must not rewrite who the profile is"

    assert auth.identity_mismatch(p, {"loggedIn": True, "email": "other@c.com"})
    assert not auth.identity_mismatch(p, {"loggedIn": True, "email": "A@B.com"})
    assert auth.identity_mismatch(p, {"loggedIn": True, "authMethod": "api_key"}), \
        "an API-key fallback is a different account and must be refused"
    assert not auth.identity_mismatch(p, {"loggedIn": False})


def test_quick_refresh_reads_the_store_not_a_config_dir():
    os.environ["CCSM_HOME"] = str(Path(_TMP) / "quick")
    store = FileCredentialStore(ccsm_home())
    p = Profile(id="qr", name="qr")
    auth.quick_refresh(p, store)
    assert p.auth_state == UNKNOWN, "a never-verified profile must not be called REVERIFY"

    store.put("qr", bundle("T", UUID_A, "a@x.com"))
    auth.quick_refresh(p, store)
    assert p.auth_state == OK and p.plan == "max"

    store.put("qr", make_bundle(cred("T", refresh_days=-1), account(UUID_A, "a@x.com")))
    auth.quick_refresh(p, store)
    assert p.auth_state == REVERIFY, "an expired refresh token must be caught locally"


# ---------- usage ----------

def test_usage_reports_unavailable_instead_of_estimating():
    d = Path(_TMP) / "usage"
    d.mkdir(parents=True, exist_ok=True)
    empty = {m.key: m for m in usage.collect(d)}
    assert set(empty) == {"session", "daily", "7day", "additional"}
    assert all(m.value == usage.UNAVAILABLE for m in empty.values())

    (d / "stats-cache.json").write_text(json.dumps({
        "lastComputedDate": "2026-07-18",
        "dailyActivity": [{"date": f"2026-07-{n:02d}", "messageCount": 10, "sessionCount": 1}
                          for n in range(1, 11)],
        "dailyModelTokens": [{"date": f"2026-07-{n:02d}",
                              "tokensByModel": {"claude-opus-5": 1000}} for n in range(1, 11)],
        "modelUsage": {"claude-opus-5": {"costUSD": 0}},
    }), encoding="utf-8")

    m = {x.key: x for x in usage.collect(d)}
    assert m["session"].value == usage.UNAVAILABLE, "window usage must never be invented"
    assert m["additional"].value == usage.UNAVAILABLE
    assert "1,000 tokens" in m["daily"].value and "10 msgs" in m["daily"].value
    assert "7,000 tokens" in m["7day"].value and "70 msgs" in m["7day"].value
    assert "2026-07-18" in m["daily"].detail
    assert not any("%" in x.value for x in m.values()), "no fabricated percentages"


if __name__ == "__main__":
    import shutil

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    try:
        for fn in tests:
            fn()
            print(f"ok  {fn.__name__}")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(tests)} passed")
