#!/usr/bin/env sh
# ccsm installer.  ./install.sh   |   ./install.sh --uninstall
set -e
cd "$(dirname "$0")"

PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(sys.version_info<(3,9))' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || { echo "ccsm: needs Python 3.9+ on PATH" >&2; exit 1; }

if [ "$1" = "--uninstall" ]; then
  if command -v ccsm >/dev/null 2>&1; then ccsm uninstall --yes || true; fi
  rm -rf "$HOME/.claude/skills/ccsm"
  pipx uninstall ccsm 2>/dev/null || "$PY" -m pip uninstall -y ccsm
  echo "ccsm removed."
  exit 0
fi

if command -v pipx >/dev/null 2>&1; then
  pipx install --force .
else
  "$PY" -m pip install --user --upgrade .
fi

# Install the /ccsm slash command for every session, not just this directory.
SKILLS="$HOME/.claude/skills/ccsm"
if [ -f .claude/skills/ccsm/SKILL.md ]; then
  mkdir -p "$SKILLS" && cp .claude/skills/ccsm/SKILL.md "$SKILLS/SKILL.md"
  echo "Installed the /ccsm skill to $SKILLS"
fi

echo
if command -v ccsm >/dev/null 2>&1; then
  echo "Installed. Run: ccsm"
else
  SCRIPTS=$("$PY" -c 'import sysconfig;print(sysconfig.get_path("scripts",sysconfig.get_preferred_scheme("user")))')
  echo "Installed, but 'ccsm' is not on PATH yet."
  echo "  run it now:      $SCRIPTS/ccsm"
  echo "  or add to PATH:  $SCRIPTS"
fi
