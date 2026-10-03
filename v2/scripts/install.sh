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
[ -z "$miss" ] || die "missing prerequisite(s):$miss"

cur="$(orca --version | grep -Eo '[0-9]+(\.[0-9]+)+' | head -1)"
[ "$(printf '%s\n%s\n' "$ORCA_MIN_VERSION" "$cur" | sort -V | head -1)" = "$ORCA_MIN_VERSION" ] \
  || die "orca $cur is older than $ORCA_MIN_VERSION"
grep -qE 'TERM_PROGRAM.*Orca|ORCA_PANE_KEY' "$HOME/.zshrc" 2> /dev/null \
  || warn "$HOME/.zshrc does not skip tmux autostart in Orca terminals; Orca cannot see agents inside tmux"
jq -e --arg p "$HOME/orca/workspaces" '.projects[$p].hasTrustDialogAccepted' "$HOME/.claude.json" > /dev/null 2>&1 \
  || warn "Claude does not trust $HOME/orca/workspaces: new worktrees will show the trust dialog"

chmod +x "$here/bin/claude-spoke" "$here"/scripts/*.sh
echo "ok. For native OTel per spoke, set Orca > Settings > Agents > Claude command to:"
echo "  $here/bin/claude-spoke"
