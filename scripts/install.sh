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
# Native git hooks (commit-msg, pre-commit): wired on ONE repo, the target (argument, default: the repo this runs in),
# never globally. Linked worktrees share the repo config. The hooks come from the target's own synced copy.
if root="$(git -C "${1:-.}" rev-parse --show-toplevel 2> /dev/null)"; then
  if [ -d "$root/.ai-toolkit/hooks/git" ]; then
    chmod +x "$root"/.ai-toolkit/hooks/git/*
    git -C "$root" config --local core.hooksPath "$root/.ai-toolkit/hooks/git"
    echo "native git hooks: $root core.hooksPath=$root/.ai-toolkit/hooks/git"
  else
    warn "$root has no .ai-toolkit/hooks/git: run sync.sh into it first, then re-run install.sh"
  fi
else
  warn "not in a git repository: run install.sh <repo> to wire its native git hooks"
fi
echo "ok. Dispatch launches spokes through $here/bin/claude-spoke (terminal create --command)."
echo "Optional, to verify in WP1: Orca > Settings > Agents > Claude command = $here/bin/claude-spoke"
