#!/usr/bin/env bash
# afk-danger-guard — PreToolUse deny-WALL for afk spokes launched under bypassPermissions (#261).
#
# WHY IT EXISTS
#   afk spokes now launch under `--permission-mode bypassPermissions` (worktree-new.sh --mode afk),
#   so NO permission dialog is ever raised — the whole dialog-answering bug family (#240/#246/#253/
#   #254/#259) is removed at the root. Safety inverts from "prompt-then-approve" to a PreToolUse
#   deny-hook WALL: a deny-hook STILL fires and its permissionDecision:"deny" is HONORED even under
#   bypass. This shim IS that wall for the ambiguous/dangerous cases the static allow-list doesn't
#   clear.
#
# WHAT IT DOES — a thin shim. All logic lives in gate-broker.sh (afk_danger_guard_decide), the one
#   source of truth, so it stays unit-tested and shares the drain's classifiers:
#     • Tier 2 classify_danger DENY (boundary crossing) -> permissionDecision:"deny" (+ #241 journal)
#     • Tier 1 classify_permission APPROVE (benign self-op) -> nothing (bypass runs it)
#     • Tier 3 toolless LLM judge on the residue -> DANGEROUS/fail-closed => deny; SAFE => nothing
#
# GATE — active ONLY for an afk (bypass) spoke: an issue-numbered branch AND .ai-toolkit/mode that
#   is NOT positively `attended` (fail-safe: a missing/unreadable/ambiguous mode keeps the wall
#   ACTIVE, since a bypass spoke with the wall off is the one unacceptable state). An attended
#   session and the hub checkout are never walled (there the human is the wall).
#
# DISCIPLINE — best-effort, ALWAYS exit 0. A PreToolUse hook must never fail a session; every gap
#   (no gate-broker, no python3, no git) degrades to a silent no-op. It emits ONLY a deny (never an
#   allow), so a degraded no-op simply lets the normal flow proceed.
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Locate gate-broker.sh across both layouts: a synced target/spoke ($CLAUDE_PROJECT_DIR points at
# the worktree root; the skill syncs to .claude/skills/hub/scripts/) and the shared/ source tree
# (this file at shared/hooks/, gate-broker at shared/skills/hub/scripts/). First hit wins; no hit
# -> silent no-op (a deny-only hook that can't load its core just doesn't wall).
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
command -v afk_danger_guard_decide >/dev/null 2>&1 || exit 0

# The PreToolUse payload flows in on stdin; afk_danger_guard_decide reads it (cat) and prints the
# deny verdict or nothing. Never let its rc fail the hook.
afk_danger_guard_decide || true
exit 0
