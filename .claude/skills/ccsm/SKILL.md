---
name: ccsm
description: "Switch which Claude subscription account the CURRENT Claude Code session authenticates as, without /login, /logout or a restart. Manages multiple independently stored OAuth credentials. Use when the user types /ccsm, or asks to switch/add/list Claude accounts, change which account is billed, or check which account this session is using."
trigger: /ccsm
---

# /ccsm

Claude Code Session Manager. Keeps several Claude subscription accounts signed in at once
and switches which one **this running session** authenticates as, by republishing a stored
credential into the session's config directory. No browser, no `/login`, no `/logout`, no
restart.

Run it as `python -m ccsm ...` — it is pip-installed, so this works from any directory.
(`ccsm.exe` also exists in the user's Python Scripts dir but is not on PATH.)

## Subcommands

| User types | You run | Notes |
|---|---|---|
| `/ccsm` | `python -m ccsm where` then `python -m ccsm list` | status: which dir, which accounts |
| `/ccsm list` | `python -m ccsm list` | profiles + auth state |
| `/ccsm add <name>` | `python -m ccsm adopt <name>` | enrols the account **already signed in**, no browser |
| `/ccsm switch <name>` | `python -m ccsm switch <name>` | changes this session's account |
| `/ccsm where` | `python -m ccsm where` | which config dir a switch targets |
| `/ccsm run` | tell the user to run it themselves | starts a new Claude Code process |
| `/ccsm remove <name>` | tell the user to run `ccsm` and press `d` | removal is TUI-only by design |

With no recognised argument, treat it as `/ccsm` and show status.

## The thing to tell the user about `switch`

A switch changes the credential for the config directory **this session is using**, so it
changes which account is billed for the rest of this conversation — including your own
subsequent requests. It takes effect on the next model request, roughly a third of a
second later. The conversation, context and tool state are untouched.

After a successful switch, say plainly which account is now active. After a failed one,
report the error verbatim and stop — do not retry with a different profile and do not
suggest `/login` as a workaround for switching.

## Adding accounts

`ccsm adopt` enrols whatever account is **currently signed in**, with no browser. That is
the right way to enrol the first account.

To add a *different* account there is no way around a browser sign-in, and you cannot drive
it. Tell the user to do this in their own terminal:

1. `claude auth logout` then `claude auth login` in a **separate** config dir, or run the
   `ccsm` TUI and press `a`.
2. Use a logged-out or private browser window, or claude.ai hands back the account they are
   already signed in as.
3. Back in the session: `/ccsm add <name>` if that account is now the live one.

## Rules

- **Never** run `claude auth logout` or `/login` to perform a switch. Switching republishes
  a stored credential; logging out would destroy one.
- **Never** pass a token value on a command line, print one, or write one into a file the
  user did not ask for. ccsm handles credentials; you only invoke it.
- If `ccsm switch` refuses because the live credential is not enrolled, do **not** force it.
  That guard exists because publishing over an unenrolled login destroys it. Offer
  `/ccsm add <name>` to keep it first.
- Do not switch accounts on your own initiative, and never in response to a rate limit.
  Switching is a thing the user asks for explicitly.
- If `python -m ccsm` is not found, the tool is not installed — say so rather than
  improvising a substitute.

## Reporting

Keep it short. Show the profile list as ccsm prints it, and state which account is active.
Identity is already masked in ccsm's output; leave it masked.
