# ccsm — Claude Code Session Manager

Keep several Claude subscription accounts signed in at once and switch which one a
**running** Claude Code session authenticates as — same process, same conversation, no
browser after the first sign-in.

> ### Read this before installing
>
> ccsm stores your Claude OAuth credentials and **writes into Claude Code's own config**.
> It depends on undocumented behaviour, reverse-engineered from **Claude Code 2.1.259 on
> Windows**.
>
> | | |
> |---|---|
> | **Windows** | verified |
> | **Linux / WSL** | expected to work, **not validated** |
> | **macOS** | **not supported** — refuses rather than guessing at the Keychain |
> | **Billing attribution** | **unproven** — switching changes the credential; that Anthropic *bills* the other account has never been demonstrated end to end |
>
> A switch modifies `~/.claude.json` — your project history, trust settings and MCP
> config live there. ccsm changes only the `oauthAccount` key and writes
> `~/.claude.json.ccsm-backup` before its first modification, but that is the blast radius
> if something is wrong. ccsm warns when your Claude Code version differs from the one it
> was verified against. See [SECURITY.md](SECURITY.md).

```
  ccsm  claude code session manager                                       v0.1.0
  ────────────────────────────────────────────────────────────────────────────

      PROFILE       IDENTITY                PLAN     AUTH      VERIFIED

  ▸ ●  Personal      m•••••e@gmail.com       Max      OK        2m ago
       Company       m•••••y@acme.com        Max      OK        1h ago
       Friend        Unavailable             —        UNKNOWN   never

  ↑↓ move   ⏎ switch   l run   a add   r reverify   d remove   u usage   q quit
```

Sign in to each account **once**. After that `A → B → A → C` costs no browser, no
`/login`, no `/logout`, and no restart.

## How it works

Claude Code reads its OAuth credential from `<CLAUDE_CONFIG_DIR>/.credentials.json` while
it builds each model request — not once at startup. ccsm keeps one credential per account
and publishes the selected one into the runtime directory Claude Code is already using.
The next request goes out as the new account. Measured switch latency: **~360 ms**.

```
<ccsm home>/
  profiles.json          profile metadata (no secrets)
  store/<id>.json        one credential per enrolled account, 0600, never mutated by a switch
  enroll/<id>/           throwaway CLAUDE_CONFIG_DIR used only to run /login for that account
  switch.lock            ccsm's own switch lock
```

`<ccsm home>` is `%APPDATA%\ccsm` on Windows and `$XDG_CONFIG_HOME/ccsm` (default
`~/.config/ccsm`) on Linux/WSL. `CCSM_HOME` overrides it.

**Which config dir a switch publishes into** is resolved in this order — `CCSM_RUNTIME`,
then `CLAUDE_CONFIG_DIR` if the calling session has it set, then `~/.claude`. The default
is Claude Code's own directory on purpose: switching a private sandbox would have no effect
on the session you are actually sitting in. `ccsm where` prints the answer.

Because of that default, ccsm **refuses to publish over a credential it does not already
hold a copy of** — that is a working login and overwriting it would destroy it silently.
Run `ccsm adopt <name>` to keep it first.

A switch **copies out of the store, never moves**, so every other profile stays
independently authenticated. Nothing is logged out.

## The switch operation

Nine steps, in this order, because the order is what makes it safe:

```
1  take ccsm's switch lock
2  wait out Claude Code's OAuth refresh lock, if held
3  read the live credential
4  save it back to the profile that is currently active   ← harvest
5  read the target profile's stored credential
6  publish it atomically (os.replace)
7  verify the runtime now authenticates as the target
8  mark the target active
9  release the lock
```

**Step 3–4 is the one that matters.** Claude Code refreshes OAuth tokens in place, so the
live credential drifts ahead of the store:

```
personal has refreshed token A2  ·  ccsm's store still holds A1
   → switch to company
   → switch back to personal
   → publish stale A1
   → unnecessary reauthentication
```

Harvesting before publishing is what stops that. It is covered by
`test_harvest_prevents_republishing_a_stale_credential`.

Harvest refuses to guess: if the live credential's `organizationUuid` no longer matches the
active profile's — someone ran `/login` in the runtime directly — it is left alone rather
than stored against the wrong account.

## Switching never reauthenticates

| | |
|---|---|
| **Switch** | Republishes a stored credential. Never opens a browser. |
| **Reverify** | Only for a credential that is genuinely expired or revoked. Opens a browser, for that one profile, and replaces only that profile's stored credential. |

## Failure is loud, never silent

Step 7 runs `claude auth status --json` against the runtime, which reports the account
with **no model request**. If the live identity is not the profile you asked for:

```
ccsm: SWITCH FAILED - switch failed - runtime reports other@example.com, expected me@acme.com
      the runtime was left on a verified credential; no request will run as an unverified account.
```

The previous credential is restored, the active profile is unchanged, and the target is
marked `REVERIFY`. ccsm cannot intercept Claude Code's requests, so what it guarantees is
narrower and worth stating plainly: **the published credential is always one that verified**.

## Race conditions

- **An in-flight request is safe.** A request's credential is fixed into its headers when
  the request is built, so publishing mid-flight cannot corrupt one — it takes effect on
  the next request. Across every test run, no request went out on a half-switched identity.
- **Claude Code's refresh lock is respected.** It takes a lockfile at
  `<credentials path>.lock` while refreshing and raises `OAuthRefreshLockContendedError`
  ("another process is refreshing") on contention. ccsm waits for it rather than
  publishing over a refresh in progress.
- **ccsm serialises its own switches** with `switch.lock`, with a 120-second staleness
  break so a killed process cannot wedge it.

## Platform support

| | Status |
|---|---|
| **Windows** | Proven. The credential file is the real store; `os.replace` is atomic. |
| **Linux / WSL** | Same architecture expected, **not yet separately validated**. |
| **macOS** | **Not supported.** Claude Code keeps credentials in the login Keychain, keyed to the config directory, and the service/account naming is undocumented. `KeychainCredentialStore` deliberately refuses rather than guessing. `CCSM_FORCE_FILE_STORE=1` tries the file backend at your own risk. |

The backend is chosen behind one interface, so macOS lands as a second implementation
rather than a rewrite:

```
CredentialStore
├── FileCredentialStore       Windows, Linux/WSL   ← implemented
└── KeychainCredentialStore   macOS                ← investigation pending
```

## Security model

**ccsm locally stores and atomically publishes Claude Code credential files.** It never
transmits credentials, exposes token values through the UI, logs token values, or puts them
in command-line arguments or environment variables.

That is the accurate claim. ccsm reads, stores, harvests and republishes real OAuth
credentials — that is the whole mechanism — so treat `<ccsm home>` as exactly as sensitive
as `~/.claude`.

- Credentials are stored at `0600` in a `0700` directory, written through a temp file
  opened with mode `0600` so there is no world-readable window, then `os.replace`d.
- A token value is never logged, printed, or put in an argument list or environment
  variable. `summarize()` returns only `expiresAt`, `refreshTokenExpiresAt`,
  `subscriptionType`, `rateLimitTier`. Identity is displayed masked (`m•••••e@gmail.com`).
- The store refuses anything that is not shaped like a Claude credential, so a truncated
  or foreign file cannot be published.
- ccsm has no network code at all.
- **`ANTHROPIC_API_KEY` and friends are scrubbed** from every child process. Without that,
  an unauthenticated runtime falls through to whatever API key is in your environment and
  reports itself happily logged in as a *different account*. See `AUTH_ENV`.
- On Windows the store inherits your user-profile ACL; there is no `chmod` equivalent
  applied. Treat `<ccsm home>` as as sensitive as `~/.claude`.

## Usage view

`u` shows a compact panel. **ccsm never estimates a limit, quota, or percentage** — there
are no progress bars, because Claude Code exposes no rate-limit data to the CLI.

Session/window is always `Unavailable`. Daily and 7-day come from Claude Code's own
`stats-cache.json` and are scoped to the **runtime**, not to an account — the runtime is
shared by design so the conversation survives a switch — and are labelled as activity
recorded, with the cache's own `lastComputedDate`.

## Commands

```
ccsm                        open the manager
ccsm list                   profiles and their authentication state
ccsm switch <name>          make <name> active — applies to the next model request
ccsm adopt <name>           enrol the account already signed in, no browser
ccsm where                  show which config dir a switch would publish into
ccsm run [args]             start Claude Code in the runtime
ccsm uninstall [--yes]      delete all ccsm data, including stored credentials
```

`ccsm switch` is the point of the tool: run it in a second terminal while a session is
open and that session changes account without noticing.

`CCSM_CLAUDE_BIN` overrides the Claude Code executable, `CCSM_HOME` the data directory,
`CCSM_RUNTIME` the config dir a switch targets.

### Inside Claude Code

A `/ccsm` skill is installed at `~/.claude/skills/ccsm/`, so `/ccsm`, `/ccsm list`,
`/ccsm add <name>` and `/ccsm switch <name>` work from inside a session. A switch there
changes the account for the session you are typing in, from its next request onward.

## Install / uninstall

Requires Python 3.9+ and the Claude Code CLI on `PATH`.

```sh
./install.sh          # macOS / Linux / WSL
.\install.ps1         # Windows PowerShell

./install.sh --uninstall      #  .\install.ps1 -Uninstall
```

## Known limitations

- **The acceptance test is not done.** Everything above is verified client-side, including
  a proven `A → B → A` round trip in one process with one session id. What is *not* proven
  is that Anthropic's backend bills the switched account: that needs two real paid
  accounts, one session, a switch mid-task, and a check that B's usage moved and A's did
  not. Treat ccsm as unvalidated against real billing until you run it.
- **Switching is undocumented behaviour.** Claude Code does not promise that the credential
  file is re-read per request. It is today, and ccsm depends on it. The supported
  equivalent is typing `/login` in the session.
- **Expect a cache cost on switch.** Prompt caches are scoped per organization, so the
  first request after a switch re-reads the whole conversation uncached.
- **The runtime is shared across profiles**, which is what makes the conversation survive.
  Session history and local stats are therefore not per-account.
- **Enrolling a second account still needs a browser**, in a logged-out or private window.
  `ccsm adopt` only captures the account that is already signed in.
- **No rotation, ever.** Switching is a key the user presses. ccsm will not react to a rate
  limit, rotate accounts, or schedule anything.

## Layout

| Module | Responsibility |
|---|---|
| `profiles.py` | `ProfileStore` — metadata, atomic JSON, 5-profile cap, masking, paths |
| `credentials.py` | `CredentialStore` + `FileCredentialStore` / `KeychainCredentialStore`, auth-env scrubbing |
| `switcher.py` | the nine-step switch, locking, harvest, verification |
| `auth.py` | `claude auth` wrappers, state classification, identity guard |
| `launcher.py` | preflight and launching into the shared runtime |
| `usage.py` | supported metrics only, `Unavailable` otherwise |
| `tui.py` | raw ANSI, keyboard, warm palette |

Standard library only — no runtime dependencies.

## Tests

```sh
python tests/test_ccsm.py      # or: python -m pytest tests
```

No network, no subprocess, no Claude Code install required. They cover the quiet failures:
the stale-credential republish, harvesting a foreign account, switching to an unenrolled
profile, rollback on failed verification, both locks, a repeated round trip leaving both
credentials intact, and that no token material escapes into a summary.

## Contributing

Bug reports and patches welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The three most
useful things right now are a **macOS Keychain backend**, **validating Linux/WSL**, and
**running the real-account acceptance test**.

Security issues: please use a private GitHub advisory rather than a public issue, and
never paste a credential file into a report. See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).
