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
# SELF-CLEARING — THREE UN-BLOCK PATHS
#   1. The broker's gate-answer path deletes the tag (_consume_gate_tag).
#   2. The tip advances past the gate commit (the tag is no longer at the tip).
#   3. SELF-HEAL (issue #204): the tag can OUTLIVE the approval — a broker inject
#      that registered late, a wedge respawn started outside the broker, or ANY
#      attended / manual reply typed in the pane, none of which run the broker's
#      _consume_gate_tag. So before denying, the guard checks for a POSITIVE sign
#      the approval already arrived — the session transcript shows a genuine user
#      turn after the PLAN-gate park, or the AI_TOOLKIT_PLAN_GATE_OVERRIDE
#      break-glass is set — and, if so, ALLOWS and best-effort drops the stale
#      LOCAL tag (which is what this guard reads) so later calls are a no-op. This
#      only ADDS allow-cases on evidence approval landed; absent that evidence the
#      deny stands, so a genuinely-parked spoke is never loosened.
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

# approval_in_transcript <input> — rc 0 when the session transcript (payload
# .transcript_path) shows a GENUINE human/hub reply AFTER the assistant turn that ran
# `spoke-ready.sh --gate` — i.e. the PLAN-gate approval reply already landed. A (re-)park
# supersedes an earlier approval. "Genuine" is a TYPED prompt submission
# (promptSource == "typed"): a human typing in the pane, or the broker's tmux inject,
# both land as "typed". Every synthetic user turn the harness injects — tool_results,
# <task-notification>/<system-reminder>, skill/meta turns (isMeta), SDK/system prompts —
# carries a different promptSource (or none), so it can NOT masquerade as approval and
# tear the gate down (a #117-class hole this guard exists to close). Fail-CLOSED (rc 1):
# no transcript_path, no python3, an unreadable transcript, a CC build that omits
# promptSource, or no typed post-park turn all keep the deny — only a PROVEN typed reply
# un-blocks. Mirrors the broker's _gate_answer_landed so both sides read the same signal.
approval_in_transcript() {
  local input="$1" tp
  tp=$(json_field "$input" "transcript_path")
  [ -n "$tp" ] || return 1
  [ -f "$tp" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  _PGG_JSONL="$tp" python3 2>/dev/null <<'PYEOF'
import json, os, sys

parked = False
approved = False
try:
    with open(os.environ["_PGG_JSONL"], encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            ttype = obj.get("type")
            content = (obj.get("message") or {}).get("content")
            if ttype == "assistant":
                for block in content if isinstance(content, list) else []:
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("name") == "Bash"
                            and "spoke-ready.sh --gate" in ((block.get("input") or {}).get("command") or "")):
                        parked = True       # a (re-)park supersedes any earlier approval
                        approved = False
            elif ttype == "user" and parked:
                # ONLY a typed prompt submission is a genuine reply — harness-injected user
                # turns (tool_results, notifications, skill/meta, SDK/system) are not.
                if obj.get("promptSource") == "typed" and not obj.get("isMeta"):
                    approved = True
except Exception:
    sys.exit(1)
sys.exit(0 if approved else 1)
PYEOF
}

# self_heal_approved <input> — rc 0 when a POSITIVE approval signal is present: the
# break-glass env override, or a genuine post-park user turn in the transcript.
self_heal_approved() {
  [ -n "${AI_TOOLKIT_PLAN_GATE_OVERRIDE:-}" ] && return 0
  approval_in_transcript "$1"
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
Present your plan as a message and WAIT — this un-blocks the moment the approval lands: the \
broker (or the reviewer replying in your pane) answers the gate, the guard sees that reply \
and self-heals, or your tip advances past the gate commit. Reads, searches, git status/diff, \
and spoke-ready.sh stay allowed. (Stuck after a real approval? export \
AI_TOOLKIT_PLAN_GATE_OVERRIDE=1 to break glass.)"
