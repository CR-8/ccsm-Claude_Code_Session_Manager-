# Security

ccsm stores Claude OAuth credentials and writes into Claude Code's configuration. Please
read this before running it, and before reporting an issue.

## Reporting a vulnerability

Open a **private** security advisory through GitHub
(*Security → Report a vulnerability*) rather than a public issue. Include your OS, your
Claude Code version (`claude --version`), and what you observed. **Never paste a token,
a `.credentials.json`, or a `.claude.json` into a report** — those files carry live
credentials. A redacted key listing is enough.

## What ccsm handles

Earlier drafts of the README claimed ccsm "never handles your tokens". That was false and
has been corrected. Accurately:

**ccsm locally stores and atomically publishes Claude Code credential files.** It never
transmits credentials, exposes token values through the UI, logs token values, or puts
them in command-line arguments or environment variables.

| Path | ccsm does |
|---|---|
| `<ccsm home>/store/<profile>.json` | writes — one account bundle per profile, mode `0600` in a `0700` directory |
| `<runtime>/.credentials.json` | reads and atomically replaces — the OAuth token |
| `.claude.json` → `oauthAccount` only | reads and surgically replaces — the account identity |
| `.claude.json.ccsm-backup` | writes once, before the first modification |
| `<runtime>/.credentials.json.lock` | **reads only** — Claude Code's own refresh lock |
| `<ccsm home>/enroll/<profile>/.credentials.json` | writes a stored credential for one `/usage` read, then deletes it — see below |
| `<ccsm home>/limits.json` | writes — percentages and reset times per `accountUuid`, no token material |

`<ccsm home>` is `%APPDATA%\ccsm` (Windows) or `$XDG_CONFIG_HOME/ccsm` (Linux/WSL).

**Reading limits.** When the manager opens, and on `R`, it runs `claude -p /usage` once per
account. Claude Code, not ccsm, sends that account's credential to Anthropic, exactly as
typing `/usage` in a session does. `/usage` is a local command and spends no quota.
- **The live account** is read in the runtime itself.
- **Every other account** is read in its own enrolment directory, with its stored
  credential copied in for the duration.
- **A refreshed token is kept.** If Claude Code refreshes the token there, the refreshed
  token goes back to the store: matched by `accountUuid`, and only if its expiry is later.
  Then the copy is deleted.
- **No switch can overlap a read.** The whole read holds ccsm's switch lock.

## Threat model

**ccsm assumes your user account is not already compromised.** Anything that can read
`~/.claude/.credentials.json` can read ccsm's store too — ccsm holds the same class of
secret in the same trust domain, it just holds more than one of them. It is not a vault
and does not defend against local malware or another process running as you.

What it does defend against:

- **Silent account substitution.** `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL` and the Bedrock/Vertex/Foundry switches
  are scrubbed from every child process. Without that, an unauthenticated runtime falls
  through to whatever key is in your environment and reports itself logged in as a
  *different account*. See `AUTH_ENV` in `ccsm/credentials.py`.
- **Publishing an unverified identity.** A switch verifies that the runtime authenticates
  as the target `accountUuid` before marking it active, and rolls both files back if not.
- **Destroying a login you did not enrol.** A switch refuses to publish over a credential
  ccsm holds no copy of.
- **Mis-attributing a credential.** Harvest requires a positive `accountUuid` match. A
  missing identity is never treated as agreement.
- **Stranding a rotated token.** OAuth refresh tokens rotate, so a token Claude Code
  refreshes during a `/usage` read goes back to the store before its copy is deleted. The
  older token an enrolment `/login` left behind never replaces a newer stored one.
  Quitting the manager waits for a read in progress.
- **World-readable windows.** Credential writes go through a temp file opened at `0600`,
  then `os.replace`.

## Known risks, stated plainly

- **ccsm depends on undocumented Claude Code behaviour.** That the OAuth credential is
  read per request, where `.claude.json` lives, and the shape of `oauthAccount` are all
  reverse-engineered, not contracted. Anthropic can change any of them in any release.
- **It was verified against one build on one OS** — Claude Code 2.1.259 on Windows. ccsm
  warns when your version differs. Linux/WSL is unvalidated; macOS is refused outright.
- **A switch writes to `.claude.json`**, which holds your project history, trust settings
  and possibly MCP server configuration. ccsm changes only the `oauthAccount` key and
  backs the file up first, but the blast radius of a bug there is your Claude Code config.
- **Billing attribution is unproven.** That switching changes which account Anthropic
  *bills* has never been demonstrated end to end. See "Known limitations" in the README.
- **On Windows there is no `chmod`.** The store inherits your user-profile ACL. Treat
  `<ccsm home>` as exactly as sensitive as `~/.claude`.

## Out of scope

ccsm will not implement, and pull requests adding these will be declined:

- automatic account rotation, or switching in response to a rate limit or quota exhaustion
- any attempt to evade subscription limits or usage controls
- patching, injecting into, or otherwise modifying Claude Code itself

Switching is an explicit user action. That is a design constraint, not an oversight.
