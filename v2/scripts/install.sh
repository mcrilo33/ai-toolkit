#!/usr/bin/env bash
# One-time machine setup: checks the prerequisites and prints what Orca needs to launch the OTel
# shim. It installs no PATH shim and never writes Orca's settings (that needs the user's OK).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=lib.sh
. "$here/scripts/lib.sh"
load_env

miss=""
for t in orca gh git jq python3; do command -v "$t" > /dev/null || miss="$miss $t"; done
case "$miss" in *" jq"*) warn "jq is required: the Claude hooks fail closed (deny every tool call) without it" ;; esac
[ -z "$miss" ] || die "missing prerequisite(s):$miss"

cur="$(orca --version | grep -Eo '[0-9]+(\.[0-9]+)+' | head -1)"
[ "$(printf '%s\n%s\n' "$ORCA_MIN_VERSION" "$cur" | sort -V | head -1)" = "$ORCA_MIN_VERSION" ] \
  || die "orca $cur is older than $ORCA_MIN_VERSION"
grep -qE 'TERM_PROGRAM.*Orca|ORCA_PANE_KEY' "$HOME/.zshrc" 2> /dev/null \
  || warn "$HOME/.zshrc does not skip tmux autostart in Orca terminals; Orca cannot see agents inside tmux"
chmod +x "$here/bin/claude-spoke" "$here"/scripts/*.sh
# Native git hooks (commit-msg, pre-commit) for the checkout this runs in; linked worktrees share the config.
if [ -d "$here/hooks/git" ]; then
  chmod +x "$here"/hooks/git/*
  if root="$(git rev-parse --show-toplevel 2> /dev/null)"; then
    git -C "$root" config core.hooksPath "$here/hooks/git"
    echo "native git hooks: $root core.hooksPath=$here/hooks/git"
  else
    warn "not in a git repository: run install.sh from the main checkout to wire the native git hooks"
  fi
fi
echo "ok. Dispatch launches spokes through $here/bin/claude-spoke (terminal create --command)."
echo "Optional, to verify in WP1: Orca > Settings > Agents > Claude command = $here/bin/claude-spoke"
