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
#   parsed from the branch slug (feature/173-foo → 173). That single condition is
#   self-limiting: the hub sits on the default branch (slug `main` → no leading
#   number → no-op), and a gate tag at the tip only exists in a spoke actually
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
# SELF-CLEARING, NO NEW MACHINERY
#   The gate answer path already deletes the tag (_consume_gate_tag); once it is
#   gone — or once the tip advances past the gate commit — the tag is no longer
#   at the tip and this guard is a no-op again.
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

# is_gated_commit <command> — a `git commit` at a command boundary. Same
# boundary-awareness as utils.sh's is_git_commit (start/`;`/`&`/`|`/backtick/`$(`,
# env-assignment prefixes) but with a `-[cC] <value>` alternative LEADING the
# option group so a `git -c core.pager=cat commit` value cannot orphan and hide
# the verb — the bypass is_git_commit misses and spoke-main-guard closes the same
# way (see its GATE_RE). Match-only + fail-open, so the tolerant heuristic is safe.
is_gated_commit() {
  printf '%s' "$1" | grep -qE '(^|[;&|`]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git([[:space:]]+(-[cC][[:space:]]+[^[:space:]]+|-[^[:space:]]+|--[^[:space:]]+))*[[:space:]]+commit\b'
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

# ── Parse the issue number from the branch slug (feature/173-foo → 173) ─
ROOT=$(project_root_from_payload "$INPUT")
BRANCH=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
SLUG="${BRANCH##*/}"
ISSUE=$(printf '%s' "$SLUG" | sed 's/^\([0-9]*\).*/\1/')
[ -n "$ISSUE" ] || exit 0

# ── Parked iff gate/<issue> is AT the branch tip ─────────────────────
TIP=$(git -C "$ROOT" rev-parse -q --verify HEAD 2>/dev/null || true)
[ -n "$TIP" ] || exit 0
GATE=$(git -C "$ROOT" rev-parse -q --verify "refs/tags/gate/${ISSUE}^{commit}" 2>/dev/null || true)
[ "$GATE" = "$TIP" ] || exit 0

deny "You are parked at your PLAN gate (gate/${ISSUE} at the branch tip) awaiting review. \
Edits (Edit/Write/NotebookEdit) and git commit are blocked until the gate is answered. \
Present your plan as a message and WAIT — the hub deletes gate/${ISSUE} when it replies (or \
your tip advances past the gate commit) to un-block. Reads, searches, git status/diff, and \
spoke-ready.sh stay allowed."
