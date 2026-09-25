<div align="center">

# ccsm

**Switch which Claude subscription account a running Claude Code session uses.**

Same process. Same conversation. No `/login`, no `/logout`, no restart.

[![tests](https://github.com/REPLACE-ME/ccsm/actions/workflows/tests.yml/badge.svg)](https://github.com/REPLACE-ME/ccsm/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-0B6E5F)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9%2B-0B6E5F)](pyproject.toml)
[![dependencies](https://img.shields.io/badge/dependencies-none-0B6E5F)](pyproject.toml)

[Documentation](https://REPLACE-ME.github.io/ccsm/) ·
[Install](#installation) ·
[Quick start](#quick-start) ·
[Commands](#command-reference) ·
[Security](SECURITY.md)

</div>

```
  ccsm  claude code session manager                                         v0.1.0
  ────────────────────────────────────────────────────────────────────────────────
  live Personal · runtime ~/.claude

       PROFILE         IDENTITY                  PLAN     AUTH      VERIFIED

  ▸ ●  Personal        m•••••e@gmail.com         Max      OK        2m ago
       Company         m•••••y@acme.com          Max      OK        1h ago
       Friend          Unavailable               —        UNKNOWN   never

       claude.ai · id personal

  ↑↓ move   ⏎ switch   l run   a add   A adopt   r reverify   ? help   q quit
```

Sign in to each account **once**. After that `A → B → A → C` costs no browser. Measured
switch latency: **~360 ms**.

---

## ⚠️ Read this before installing

ccsm stores your Claude OAuth credentials and **writes into Claude Code's own config**. It
depends on undocumented behaviour, reverse-engineered from **Claude Code 2.1.259 on
Windows**.

| | |
|---|---|
| **Windows** | Verified |
| **Linux / WSL** | Expected to work — **not validated** |
| **macOS** | **Not supported.** Refuses rather than guessing at the Keychain |
| **Billing attribution** | **Unproven.** Switching changes the credential; that Anthropic *bills* the other account has never been demonstrated end to end |

A switch modifies `~/.claude.json` — your project history, trust settings and MCP config
live there. ccsm changes only the `oauthAccount` key and writes `~/.claude.json.ccsm-backup`
before its first modification, but that is the blast radius if something is wrong. ccsm
warns when your Claude Code version differs from the one it was verified against.

Full threat model: **[SECURITY.md](SECURITY.md)**.

---

## Installation

Requires **Python 3.9+** and the **Claude Code CLI** on your `PATH`.

```bash
git clone https://github.com/REPLACE-ME/ccsm
cd ccsm

./install.sh          # Linux / WSL
.\install.ps1         # Windows PowerShell
```

This installs the `ccsm` command and the `/ccsm` skill, so the slash commands work in every
Claude Code session. If `ccsm` is not on your `PATH` afterwards, the installer prints the
exact directory to add.

**Uninstall** — removes stored credentials, the command and the skill. Your Claude Code
config directory is left alone.

```bash
./install.sh --uninstall        # Windows:  .\install.ps1 -Uninstall
```

## Quick start

**1. Enrol the account you are already signed in as.** No browser needed.

```bash
ccsm adopt personal
```

**2. Enrol a second account.** This one does need a browser — open the manager and press
`a`. Use a private window, or claude.ai will hand back the account you are already signed
in as.

```bash
ccsm
```

**3. Confirm both are stored.**

```console
$ ccsm list
* personal      s*****0@gmail.com       pro      OK        2m ago
  work          w*****k@acme.com        max      OK        1m ago
```

**4. Switch.** Run it in a second terminal while a Claude Code session is open and that
session changes account on its next request — or type `/ccsm switch work` inside the
session itself.

```bash
ccsm switch work
```

## Command reference

| Command | Description |
|---|---|
| `ccsm` | Open the keyboard-driven profile manager |
| `ccsm list` | Profiles with masked identity, plan, auth state, last verification |
| `ccsm switch <name>` | Make `<name>` active — applies to the next model request |
| `ccsm adopt <name>` | Enrol the account already signed in, no browser |
| `ccsm where` | Claude Code version, target config dir, store location |
| `ccsm run [args]` | Start Claude Code as the active profile; args pass through |
| `ccsm uninstall [--yes]` | Delete all ccsm data including stored credentials |
| `ccsm --version` / `--help` | |

**Inside Claude Code:** `/ccsm`, `/ccsm list`, `/ccsm add <name>`, `/ccsm switch <name>`,
`/ccsm where`.

**Manager keys:** `↑↓` / `j` `k` move · `⏎` switch · `l` run · `a` add · `A` adopt ·
`r` reverify · `d` remove · `u` usage · `R` refresh · `?` help · `q` quit. Digits move to a
row and never switch. On a narrow terminal the keybar shows what fits; `?` lists every key.

**Environment:** `CCSM_HOME` (data dir) · `CCSM_RUNTIME` (force the target config dir) ·
`CCSM_CLAUDE_BIN` (Claude Code executable) · `NO_COLOR`

Full reference, including where every file lives:
**[the docs site](https://REPLACE-ME.github.io/ccsm/)**.

## How it works

Claude Code reads its OAuth credential **while it builds each model request**, not once at
startup. Replace the credential and the next request goes out as a different account, in
the same process, on the same conversation.

The catch is that **an account is two files, not one**:

| File | Holds |
|---|---|
| `~/.claude/.credentials.json` | the OAuth token — what authenticates requests |
| `~/.claude.json` → `oauthAccount` | the identity — `accountUuid`, email, org, tier |

Publish one without the other and you get a session whose requests go to one account while
everything identity-shaped reports another. ccsm stores and publishes both together, or
neither.

### What a switch does

```
1  take ccsm's switch lock
2  wait out Claude Code's OAuth refresh lock, if held
3  read the live credential
4  save it back to the profile it belongs to        ← harvest
5  read the target profile's stored bundle
6  publish credential AND oauthAccount atomically
7  verify the runtime reports the target accountUuid
8  mark the target active
9  release the lock
```

**Step 3–4 is the one that is easy to skip.** Claude Code refreshes OAuth tokens in place,
so the live credential drifts ahead of the store. Publish over it without harvesting and
the outgoing profile keeps a stale token — sending you back to a browser you did not need.

Anything after step 6 that fails rolls **both** files back exactly as they were, leaves the
previous profile active, and fails closed.

### Race conditions

- **In-flight requests are safe.** A request's credential is fixed into its headers when the
  request is built, so publishing mid-flight cannot corrupt one — it takes effect on the
  next request.
- **Claude Code's refresh lock is respected.** ccsm waits on `<credentials path>.lock`
  rather than publishing over a refresh in progress.
- **ccsm serialises its own switches** with `switch.lock`, with a 120-second staleness break
  so a killed process cannot wedge it.

## Platform support

The credential backend sits behind one interface, so macOS lands as a second implementation
rather than a rewrite:

```
CredentialStore
├── FileCredentialStore       Windows, Linux/WSL   ← implemented
└── KeychainCredentialStore   macOS                ← contributions welcome
```

## Development

```bash
python tests/test_ccsm.py      # 27 tests · no network · no accounts · no Claude Code
python -m pyflakes ccsm tests
```

The suite pins `CCSM_RUNTIME` into a temp tree **before importing ccsm**, so no test can
reach a real `~/.claude`.

`tests/acceptance_real_accounts.py` is separate and not part of CI — it spends real quota on
two real subscription accounts.

| Module | Responsibility |
|---|---|
| `profiles.py` | Profile metadata, atomic JSON, 5-profile cap, identity masking, paths |
| `credentials.py` | `CredentialStore` backends, account bundles, `.claude.json` surgery |
| `switcher.py` | The nine-step switch, locking, harvest, verification, rollback |
| `auth.py` | `claude auth` wrappers, state classification, version guard |
| `launcher.py` | Preflight and launching into the runtime |
| `usage.py` | Supported metrics only, `Unavailable` otherwise |
| `tui.py` | Raw ANSI, keyboard, warm palette |

Standard library only. No runtime dependencies.

## Known limitations

- **Billing attribution is unproven.** A proven `A → B → A` round trip exists — one process,
  one session id, credential and identity moving together — but that Anthropic bills the
  switched account has not been demonstrated. `tests/acceptance_real_accounts.py` is that
  test; it needs two paid accounts.
- **Switching is undocumented behaviour.** Claude Code does not promise the credential file
  is re-read per request. It is today, and ccsm depends on it. The supported equivalent is
  typing `/login`.
- **Expect a cache cost on switch.** Prompt caches are scoped per organization, so the first
  request after a switch re-reads the whole conversation uncached.
- **Enrolling a second account needs a browser**, in a logged-out or private window.
  `ccsm adopt` only captures the account already signed in.
- **The runtime is shared across profiles**, which is what makes the conversation survive.
  Session history and local stats are therefore not per-account.
- **No rotation, ever.** Switching is a key the user presses. ccsm will not react to a rate
  limit or schedule anything.

## Contributing

See **[CONTRIBUTING.md](CONTRIBUTING.md)**. The three most useful things right now:

1. **A macOS Keychain backend** — unblocks every macOS user.
2. **Validating Linux/WSL** — the architecture should hold; nobody has confirmed it.
3. **Running the real-account acceptance test** — the one claim still unproven.

Security issues: please open a private GitHub advisory rather than a public issue, and never
paste a credential file into a report.

## License

[MIT](LICENSE). Not affiliated with Anthropic.
