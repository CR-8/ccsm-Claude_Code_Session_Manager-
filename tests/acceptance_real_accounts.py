"""Real-account acceptance test: does switching change which account is actually billed?

NOT part of the unit suite. This spends real quota on two real subscription accounts and
needs a network. Run it deliberately:

    python tests/acceptance_real_accounts.py <profile-a> <profile-b>

It performs steps 4-9 of the acceptance protocol automatically:

    one Claude Code process, three turns in one conversation,
    ccsm switch A -> B -> A between them, never /login, /logout or a restart.

It proves the LOCAL half by itself (PID, session id, which credential each turn ran under)
and prints exactly what you must check out-of-band, on the two accounts themselves, to
settle the REMOTE half. It deliberately does not read local token counters to guess at
billing - that is not evidence of attribution.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ccsm import auth                                        # noqa: E402
from ccsm.credentials import clean_env, open_store, summarize  # noqa: E402
from ccsm.profiles import ProfileStore, ccsm_home, runtime_dir  # noqa: E402

MARKER = "CCSM-ACCEPTANCE"
WORDS = 400


def die(msg):
    print(f"\nPRECONDITION FAILED: {msg}")
    raise SystemExit(2)


def runtime_identity():
    """Which account the runtime is currently authenticated as. No model request."""
    st = auth.status(runtime_dir())
    if not st:
        return {"email": None, "org": None, "loggedIn": False}
    return {"email": st.get("email"), "org": st.get("orgName"),
            "loggedIn": bool(st.get("loggedIn")), "plan": st.get("subscriptionType")}


def ccsm_switch(name):
    proc = subprocess.run([sys.executable, "-m", "ccsm", "switch", name],
                          capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        die(f"`ccsm switch {name}` failed:\n{out}")
    return out


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    name_a, name_b = argv[0], argv[1]
    repeat = int(argv[2]) if len(argv) > 2 else 1

    profiles = ProfileStore()
    store = open_store(ccsm_home())
    ok, why = store.available()
    if not ok:
        die(why)

    a, b = profiles.get(name_a), profiles.get(name_b)
    if a is None or b is None:
        die(f"need two enrolled profiles; have {[p.id for p in profiles.profiles] or 'none'}")
    if a.id == b.id:
        die("the two profiles must be different")

    cred_a, cred_b = store.get(a.id), store.get(b.id)
    if cred_a is None or cred_b is None:
        die(f"both profiles need a stored credential "
            f"({a.id}: {cred_a is not None}, {b.id}: {cred_b is not None})")
    if cred_a.get("organizationUuid") == cred_b.get("organizationUuid"):
        die("both profiles hold the same account - enrol two different accounts")

    print("=" * 78)
    print("STEP 1-3  both accounts enrolled and independently stored")
    for p, c in ((a, cred_a), (b, cred_b)):
        print(f"  {p.id:<12} {p.email or '(unknown)':<32} plan={summarize(c).get('subscriptionType')}"
              f"  org={str(c.get('organizationUuid'))[:8]}...")

    # --- STEP 4: establish A, then start exactly one process -------------------
    print(f"\nSTEP 4    establishing '{a.id}' and starting ONE Claude Code process")
    ccsm_switch(a.id)
    ident = runtime_identity()
    print(f"  runtime authenticates as: {ident['email']}  (plan {ident.get('plan')})")

    proc = subprocess.Popen(
        [auth.require_claude(), "-p", "--input-format", "stream-json",
         "--output-format", "stream-json", "--verbose", "--allowedTools", ""],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=clean_env(runtime_dir()),
        text=True, bufsize=1)
    pid = proc.pid
    events = []
    threading.Thread(target=lambda: [events.append(x) for x in proc.stdout], daemon=True).start()

    def results():
        n = 0
        for line in list(events):
            try:
                if json.loads(line).get("type") == "result":
                    n += 1
            except ValueError:
                pass
        return n

    def last_result():
        for line in reversed(list(events)):
            try:
                obj = json.loads(line)
                if obj.get("type") == "result":
                    return obj
            except ValueError:
                pass
        return {}

    turns = []

    def turn(label, expect_profile):
        want = results() + 1
        ident = runtime_identity()
        prompt = (f"Write about {WORDS} words on an ordinary topic of your choice. "
                  f"Start the reply with the exact marker {MARKER}-{label}.")
        proc.stdin.write(json.dumps({
            "type": "user", "parent_tool_use_id": None,
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}) + "\n")
        proc.stdin.flush()
        end = time.time() + 180
        while time.time() < end and results() < want:
            time.sleep(0.3)
        res = last_result()
        turns.append({"label": label, "expect": expect_profile.id,
                      "identity": ident, "session": res.get("session_id"),
                      "error": res.get("is_error"), "detail": str(res.get("result"))[:70],
                      "completed": results() >= want})
        print(f"  turn {label}: credential={ident['email']}  session={res.get('session_id')}  "
              f"{'ERROR: ' + str(res.get('result'))[:60] if res.get('is_error') else 'ok'}")

    print(f"\nSTEP 5-9  three turns, switching between them ({repeat} request(s) per phase)")
    for i in range(repeat):
        turn(f"1{'' if repeat == 1 else chr(97 + i)}", a)
    print(f"  -> ccsm switch {b.id}")
    ccsm_switch(b.id)
    for i in range(repeat):
        turn(f"2{'' if repeat == 1 else chr(97 + i)}", b)
    print(f"  -> ccsm switch {a.id}")
    ccsm_switch(a.id)
    for i in range(repeat):
        turn(f"3{'' if repeat == 1 else chr(97 + i)}", a)

    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

    # --- local verdict ---------------------------------------------------------
    sessions = {t["session"] for t in turns if t["session"]}
    emails = [t["identity"]["email"] for t in turns]
    expected = [t["expect"] for t in turns]
    by_profile = {a.id: a.email, b.id: b.email}
    matched = all(
        (e or "").lower() == (by_profile.get(x) or "").lower() for e, x in zip(emails, expected))
    errored = [t["label"] for t in turns if t["error"]]

    print("\n" + "=" * 78)
    print("LOCAL EVIDENCE (necessary, not sufficient)")
    checks = [
        ("L1 one process, never restarted", proc.pid == pid, f"PID {pid}"),
        ("L2 one conversation", len(sessions) == 1, f"{len(sessions)} session id(s): {sessions}"),
        ("L3 credential per turn matches plan", matched, " -> ".join(str(e) for e in emails)),
        ("L4 both credentials still stored", store.get(a.id) is not None
         and store.get(b.id) is not None, f"{a.id}, {b.id}"),
        ("L5 every turn completed", not errored and all(t["completed"] for t in turns),
         "errors: " + (", ".join(errored) if errored else "none")),
    ]
    for label, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}]  {label:<38} {detail}")
    local_pass = all(c[1] for c in checks)
    print(f"\n  LOCAL: {'PASS' if local_pass else 'FAIL'}")

    if errored:
        print("\n  NOTE: a turn failing right after a switch is the interesting failure mode -")
        print("        it would mean the backend rejected a mid-conversation credential change.")

    # --- what only you can check ----------------------------------------------
    print("\n" + "=" * 78)
    print("REMOTE EVIDENCE - you must check this; ccsm cannot and will not infer it")
    a_turns = [t["label"] for t in turns if t["expect"] == a.id]
    b_turns = [t["label"] for t in turns if t["expect"] == b.id]
    print(f"""
  Expected attribution:
    {a.email or a.id:<34} turns {', '.join(a_turns)}
    {b.email or b.id:<34} turns {', '.join(b_turns)}

  Check, for EACH account, out of band - not from local Claude Code files:
    1. Sign in to claude.ai as that account -> Settings -> Usage.
    2. Compare against the reading you took BEFORE this run.

  PASS  {a.email or a.id} moved by {len(a_turns)} request(s) worth, and
        {b.email or b.id} moved by {len(b_turns)} request(s) worth.
  FAIL  all requests landed on one account (the switch never reached the backend),
        or a turn errored after a switch (the backend refused the new credential).

  If the deltas are too small to read, re-run with a repeat count:
      python tests/acceptance_real_accounts.py {a.id} {b.id} 5
""")
    return 0 if local_pass else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
