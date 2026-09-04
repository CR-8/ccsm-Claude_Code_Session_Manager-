# Contributing

Thanks for looking. ccsm is small on purpose — standard library only, no runtime
dependencies, seven modules.

## Running things

```sh
python tests/test_ccsm.py          # or: python -m pytest tests
python -m pyflakes ccsm tests
```

The suite needs no network, no Claude Code install, and no accounts. It pins
`CCSM_RUNTIME` into a temp tree **before importing ccsm**, so no test can reach a real
`~/.claude`. If you add a test that touches a runtime, go through the `fresh()` helper —
never construct a path to the default config dir.

`tests/acceptance_real_accounts.py` is separate and not part of CI: it spends real quota
on two real subscription accounts.

## What a change needs

- **A test that fails without it.** Especially for anything in `switcher.py` or
  `credentials.py` — that code moves live credentials around, and every guard there
  exists because something went wrong once.
- **pyflakes clean**, and the existing style: no new dependencies, no speculative
  abstractions, comments that say *why* rather than restating the code.

## Things that are load-bearing, not incidental

Please don't "simplify" these without reading why they exist:

- **Harvest before publish.** Claude Code refreshes OAuth tokens in place. Skipping the
  harvest means republishing a stale token later and sending the user to a browser they
  did not need.
- **`accountUuid` is the identity, not `organizationUuid`.** The latter is absent on real
  credentials. A missing identity must never be read as "same account".
- **A switch writes two files or neither.** An account is a credential *and* an
  `oauthAccount` block. Publishing one without the other produces a session whose requests
  go to one account while its identity reports another.
- **`.claude.json` is never rebuilt.** Only the `oauthAccount` key changes; every other
  key, and the file's permissions, are preserved.
- **`clean_env` deliberately does not set `CLAUDE_CONFIG_DIR` for the default directory.**
  Setting it to `~/.claude` is *not* a no-op — it moves where Claude Code looks for
  `.claude.json` and loses the account identity and project history.

## Out of scope

Automatic rotation, quota-limit switching, or anything that patches Claude Code. See
[SECURITY.md](SECURITY.md).

## Most useful contributions right now

1. **macOS Keychain backend.** `KeychainCredentialStore` refuses rather than guessing.
   Working out how Claude Code names its Keychain item per config dir would unblock every
   macOS user.
2. **Validating Linux/WSL.** The architecture should hold; nobody has confirmed the
   `.claude.json` location rule there.
3. **The real-account acceptance test.** Two paid accounts, one session, switch mid-task,
   and check both Usage pages. That is the one claim still unproven.
