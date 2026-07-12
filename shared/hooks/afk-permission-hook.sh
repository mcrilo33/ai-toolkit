#!/usr/bin/env bash
# afk-permission-hook — PreToolUse hook that answers a spoke's permission decision
# PROGRAMMATICALLY, before any tmux dialog is ever shown (issue #253).
#
# WHAT IT REPLACES
#   The /afk drain used to answer spoke permission dialogs by SCRAPING the tmux pane and
#   typing keystrokes — the brittle surface behind the #240/#246/#238 bug family (every new
#   dialog shape / glyph / timing window broke the scraper). This hook moves the COMMON case
#   OFF the pane: it runs the SAME classify_permission verdict the drain already trusts and
#   auto-approves a benign scoped self-op, so no dialog is shown and there is nothing to scrape.
#
# WHAT IT DOES — a thin shim. All logic lives in gate-broker.sh (afk_permission_hook_decide),
#   the one source of truth, so it stays unit-tested and identical to the drain's classifier:
#     • APPROVE  — print hookSpecificOutput.permissionDecision:"allow" (+ journal per #241)
#     • anything else — print nothing (exit 0): NEVER a deny. An ESCALATE or an un-gated
#       context falls through to the normal permission flow, so the scope-guard hooks' denies
#       stay authoritative and the rare genuine escalation still routes to the drain reasoner.
#
# SELF-LIMIT — the decision fn auto-approves ONLY inside a live /afk drain (a running
#   .afk-heartbeat supervisor) on an issue-numbered spoke branch, so an attended session and
#   the hub checkout are never silently auto-approved. See afk_permission_hook_decide.
#
# DISCIPLINE — best-effort, ALWAYS exit 0. A PreToolUse allow-only hook must never fail a
#   session; every gap (no gate-broker, no python3, no git) degrades to a silent no-op → the
#   user's normal permission prompt stays the backstop.
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Locate gate-broker.sh across both layouts: a synced target/spoke ($CLAUDE_PROJECT_DIR points
# at the worktree root; the skill syncs to .claude/skills/hub/scripts/) and the shared/ source
# tree (this file at shared/hooks/, gate-broker at shared/skills/hub/scripts/). First hit wins;
# no hit → silent no-op (the hook can never block, so a missing core just means "don't help").
GB=""
for cand in \
  "${CLAUDE_PROJECT_DIR:-}/.claude/skills/hub/scripts/gate-broker.sh" \
  "$HOOK_DIR/../../skills/hub/scripts/gate-broker.sh" \
  "$HOOK_DIR/../skills/hub/scripts/gate-broker.sh"; do
  if [ -n "$cand" ] && [ -f "$cand" ]; then GB="$cand"; break; fi
done
[ -n "$GB" ] || exit 0

# shellcheck source=/dev/null
source "$GB" 2>/dev/null || exit 0
command -v afk_permission_hook_decide >/dev/null 2>&1 || exit 0

# The PreToolUse payload flows in on stdin; afk_permission_hook_decide reads it (cat) and prints
# the allow verdict or nothing. Never let its rc fail the hook.
afk_permission_hook_decide || true
exit 0
