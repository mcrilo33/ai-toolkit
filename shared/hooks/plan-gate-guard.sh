#!/usr/bin/env bash
# plan-gate-guard — PreToolUse / beforeShellExecution DENY hook (issue #173).
#
# WHAT IT CLOSES
#   The PLAN-gate park is REQUESTED, not ENFORCED: #117 showed a spoke emitting
#   gate/<N> then self-approving and continuing to code. This guard makes the
#   wait mechanical — the LLM authors the plan; parking becomes physics.
#
# WHEN IT FIRES
#   While a gate/<N> tag sits AT the branch tip of the session's worktree, with N
#   read from the worktree's .ai-toolkit/identity record, else parsed from the branch
#   slug (feature/173-foo → 173) — see lib/identity.sh. That single condition is
#   self-limiting: the hub sits on the default branch (slug `main` → no leading
#   number) and never gets a record (only provision-worktree.sh writes one) → no-op,
#   and a gate tag at the tip only exists in a spoke actually
#   parked at its PLAN gate. No WT_SPOKE check is needed.
#
# WHAT IT DENIES (only while parked)
#   • Edit / Write / NotebookEdit / MultiEdit tool calls — no code may be written.
#   • a `git commit` Bash segment (compound / prefixed forms via is_git_commit).
#
# WHAT IT STILL ALLOWS (the spoke must be able to present its plan and park)
#   • reads / searches / git status / git diff / git log (any non-commit Bash),
#   • spoke-ready.sh (the marker emitter — how the spoke parks and un-parks),
#   • a non-commit git write like `git add` (only `commit` lands code).
#
# SELF-CLEARING — THREE UN-BLOCK PATHS
#   1. spoke-ready.sh --gate consumes its own gate tag when the coordinator approves (#365); the
#      broker's gate-answer path deletes it too (_consume_gate_tag, idempotent).
#   2. The tip advances past the gate commit (the tag is no longer at the tip).
#   3. BREAK-GLASS: AI_TOOLKIT_PLAN_GATE_OVERRIDE is set — the guard ALLOWS and best-effort drops
#      the stale LOCAL tag so later calls are a no-op. There is no transcript heuristic: absent
#      this, the deny stands, so a genuinely-parked spoke is never loosened.
#
# DISCIPLINE — deny-or-silent, fail-open: anything this hook cannot prove is a
#   parked write degrades to SILENT (exit 0). A deny guard must never false-block
#   legitimate work, so a missing git repo, a non-numeric slug, or an absent gate
#   tag all pass through.
#
# Exit 2 = block, Exit 0 = allow.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"
# shellcheck source=lib/identity.sh
source "$HOOK_DIR/lib/identity.sh"

# is_gated_commit <command> — a `git commit` at a command boundary. Same
# boundary-awareness as utils.sh's is_git_commit (start/`;`/`&`/`|`/backtick/`$(`,
# env-assignment prefixes) but with a `-[cC] <value>` alternative LEADING the
# option group so a `git -c core.pager=cat commit` value cannot orphan and hide
# the verb — the bypass is_git_commit misses and spoke-main-guard closes the same
# way (see its GATE_RE). Match-only + fail-open, so the tolerant heuristic is safe.
is_gated_commit() {
  printf '%s' "$1" | grep -qE '(^|[;&|`]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git([[:space:]]+(-[cC][[:space:]]+[^[:space:]]+|-[^[:space:]]+|--[^[:space:]]+))*[[:space:]]+commit\b'
}

# self_heal_approved — rc 0 only for the break-glass env override. The approval itself is recorded
# where it happens (#365): spoke-ready.sh --gate blocks in `orca orchestration ask`, and on an
# approve consumes its OWN gate tag, so the guard never reads a transcript to learn the gate opened.
self_heal_approved() {
  [ -n "${AI_TOOLKIT_PLAN_GATE_OVERRIDE:-}" ]
}

INPUT=$(read_stdin)

# ── Classify the tool call: is it a parked-write we care about? ──────
# A shell command (Bash / Cursor beforeShellExecution) is a write ONLY when it is
# a git commit — everything else (reads, status/diff, spoke-ready.sh) stays
# allowed. An empty command means a file-edit tool; care only about the writers.
COMMAND=$(get_shell_command "$INPUT")
if [ -n "$COMMAND" ]; then
  is_gated_commit "$COMMAND" || exit 0
else
  case "$(get_tool_name "$INPUT")" in
    Edit | Write | NotebookEdit | MultiEdit | edit | create) ;;
    *) exit 0 ;;
  esac
fi

# ── Resolve the issue number: the identity record, else the branch slug ─
ROOT=$(project_root_from_payload "$INPUT")
ISSUE=$(ai_toolkit_identity_issue "$ROOT" || true)
[ -n "$ISSUE" ] || exit 0

# ── Parked iff gate/<issue> is AT the branch tip ─────────────────────
TIP=$(git -C "$ROOT" rev-parse -q --verify HEAD 2>/dev/null || true)
[ -n "$TIP" ] || exit 0
GATE=$(git -C "$ROOT" rev-parse -q --verify "refs/tags/gate/${ISSUE}^{commit}" 2>/dev/null || true)
[ "$GATE" = "$TIP" ] || exit 0

# ── Self-heal: the tag can outlive the approval (issue #204) ─────────
# If a positive approval signal is present (a genuine post-park user turn in the
# transcript, or the break-glass override), the tag is STALE: allow and best-effort
# drop the LOCAL tag (what this guard reads) so later calls no-op. Local-only — the
# broker owns the cosmetic remote delete; a per-call remote push would be too heavy.
if self_heal_approved "$INPUT"; then
  git -C "$ROOT" tag -d "gate/${ISSUE}" >/dev/null 2>&1 || true
  exit 0
fi

deny "You are parked at your PLAN gate (gate/${ISSUE} at the branch tip) awaiting review. \
Edits (Edit/Write/NotebookEdit) and git commit are blocked until the gate is answered. \
Present your plan and WAIT — spoke-ready.sh --gate blocks until the coordinator replies, and \
this un-blocks the moment it approves (the tag is consumed), or your tip advances past the gate \
commit. Reads, searches, git status/diff, and spoke-ready.sh stay allowed. (Stuck after a real approval? export \
AI_TOOLKIT_PLAN_GATE_OVERRIDE=1 to break glass.)"
