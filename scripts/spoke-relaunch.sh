#!/usr/bin/env bash
#
# spoke-relaunch.sh — relaunch a crashed-pane spoke deterministically (issue #233).
#
# When a spoke's tmux pane dies (a crash, an accidental close, a machine
# sleep/wake that killed the window) the WORKTREE survives — its branch, its
# `.ai-toolkit/spoke-run-id`, its task contract and ledger skeleton are all still
# on disk. Re-opening it used to be a hand-rolled `tmux new-window` that a human
# had to reconstruct (the #89/#dead-pane recovery note), easy to get wrong: a new
# spoke_run_id splits the telemetry, a missing OTEL prefix drops the trace, a
# forgotten seed prompt leaves the agent unanchored.
#
# This script formalizes that recovery. It resolves the existing worktree, REUSES
# its spoke_run_id (so the relaunched run continues the same Langfuse session),
# rebuilds the exact launch command (the shared native-OTel prefix + pinned
# model/effort + a relaunch-aware seed prompt pointing at the persisted task
# contract and ledger skeleton), opens a fresh tmux window, and stamps a
# `relaunch` lifecycle span (which feeds #231's relaunch_count).
#
# Run from anywhere inside the repo (on the HUB — a spoke never relaunches
# itself). Resolves the target against the live `git worktree list`, so pass the
# issue number, slug, branch, or path — whichever you remember.
#
# Usage:
#   scripts/spoke-relaunch.sh <issue|slug|branch|path> [--no-terminal]
#
#   <issue|slug|branch|path>  anything that identifies the crashed worktree
#   --no-terminal             don't spawn a tmux window; print the launch command
#
set -euo pipefail

WT_PROG="spoke-relaunch"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

# --- guard: role, not directory (issue #26) -------------------------------------
# Relaunch runs on the hub. A spoke carries WT_SPOKE on every command it runs;
# refuse there so a spoke can never relaunch itself into a nested window.
[ -z "${WT_SPOKE:-}" ] \
  || wt_die "this is the spoke session for '$WT_SPOKE' — relaunches run on the hub, not from inside a spoke."

# Span start clock for the relaunch lifecycle/script spans emitted at the end.
WT_T0="$(wt_now_ms)"

# --- args --------------------------------------------------------------------
TARGET=""
SPAWN_TERMINAL=1
for arg in "$@"; do
  case "$arg" in
    --no-terminal) SPAWN_TERMINAL=0 ;;
    -*)            wt_die "unknown option: $arg (supported: --no-terminal)" ;;
    *)
      [ -z "$TARGET" ] || wt_die "unexpected extra argument: $arg"
      TARGET="$arg"
      ;;
  esac
done
[ -n "$TARGET" ] || wt_die "usage: spoke-relaunch.sh <issue|slug|branch|path> [--no-terminal]"

# --- resolve the existing worktree -------------------------------------------
REPO_ROOT="$(wt_main_root)" || wt_die "could not locate the main worktree"
if ! WT_DIR="$(wt_resolve "$TARGET" "$REPO_ROOT")"; then
  wt_warn "no single worktree matches '$TARGET'. Task worktrees:"
  wt_print_worktrees "$REPO_ROOT"
  exit 1
fi

# --- reuse the spoke identity (do NOT mint a new one) ------------------------
SPOKE_RUN_ID_FILE="$WT_DIR/.ai-toolkit/spoke-run-id"
[ -f "$SPOKE_RUN_ID_FILE" ] \
  || wt_die "no .ai-toolkit/spoke-run-id in $WT_DIR — not a spoke worktree, nothing to relaunch."
SPOKE_RUN_ID="$(cat "$SPOKE_RUN_ID_FILE")"
[ -n "$SPOKE_RUN_ID" ] || wt_die "empty spoke-run-id in $WT_DIR — cannot relaunch without the identity."
echo "→ worktree            $WT_DIR"
echo "→ spoke_run_id        $SPOKE_RUN_ID (reused)"

# Branch + issue for the window name and seed prompt. `--abbrev-ref` on a detached
# worktree prints "HEAD"; the leading-number slug parse then yields "HEAD", which
# only affects the seed-prompt text, not the reused identity.
BRANCH="$(git -C "$WT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
SLUG="${BRANCH##*/}"
ISSUE="${SLUG%%-*}"
WT_TAG="$ISSUE"

# --- ledger skeleton: reuse the persisted one (issue #235 seeder) ------------
# The #235 skeleton was written at spawn and survives a pane crash, so the ledger
# is intact on disk; the relaunch seed prompt points the agent back at it. Warn
# (don't fail) if it is genuinely absent — the seed prompt falls back to
# /source-task, which re-anchors from the live issue.
LEDGER_SKELETON="$WT_DIR/.ai-toolkit/ledger-skeleton.md"
if [ -f "$LEDGER_SKELETON" ]; then
  echo "→ ledger skeleton     .ai-toolkit/ledger-skeleton.md (intact)"
else
  wt_warn "no .ai-toolkit/ledger-skeleton.md — the spoke will re-seed via /source-task."
fi

# --- relaunch-aware seed prompt ----------------------------------------------
TASK_MD="$WT_DIR/.ai-toolkit/task.md"
if [ -f "$TASK_MD" ]; then
  PROMPT="Your spoke pane was relaunched (issue #${ISSUE}); the worktree, branch and spoke_run_id are intact. Read your task contract at .ai-toolkit/task.md and re-seed your task ledger from .ai-toolkit/ledger-skeleton.md (one entry per subtask x ANCHOR/RED/GREEN/REVIEW/PUSH). Check git log and your pushed branch to see which subtasks already landed, then resume the solo-cycle from the first unfinished step. Honor the task's Gate: line. If task.md is missing or the issue changed, run /source-task ${ISSUE} to re-anchor."
else
  PROMPT="Your spoke pane was relaunched. Run /source-task ${TARGET} to re-anchor from the live issue, then resume the solo-cycle."
fi

# --- pin model/effort from config (issue #142), matching worktree-new --------
WT_CONFIG="${AI_TOOLKIT_CONFIG:-$REPO_ROOT/settings/ai-toolkit.yml}"
if [ -f "$SCRIPT_DIR/spoke-model.env" ]; then
  # shellcheck disable=SC1091
  . "$SCRIPT_DIR/spoke-model.env"
elif [ -f "$SCRIPT_DIR/ai_toolkit_config.py" ] && [ -f "$WT_CONFIG" ]; then
  eval "$(python3 "$SCRIPT_DIR/ai_toolkit_config.py" spoke-env "$WT_CONFIG" 2>/dev/null || true)"
fi
WT_AGENT_MODEL="${WT_AGENT_MODEL:-${WT_AGENT_MODEL_DEFAULT:-claude-opus-4-8[1m]}}"
WT_AGENT_EFFORT="${WT_AGENT_EFFORT:-${WT_AGENT_EFFORT_DEFAULT:-max}}"

# --- native-OTel launch prefix (shared with worktree-new via worktree-lib) ---
# Reuses the SAME spoke_run_id so the relaunched pane streams into the existing
# Langfuse session rather than starting a new trace. The endpoint `:=` defaults
# stay in this shell for the bridge/collector preflights below (the helper runs in
# a subshell). AI_TOOLKIT_OTEL=0 is a clean full opt-out.
wt_resolve_telemetry_config "$WT_CONFIG"
AI_TOOLKIT_OTEL="${AI_TOOLKIT_OTEL:-${AI_TOOLKIT_OTEL_DEFAULT:-1}}"
OTEL_PREFIX=""
if [ "${AI_TOOLKIT_OTEL:-}" = "1" ]; then
  OTEL_BODY_DIR="$WT_DIR/.ai-toolkit/raw-bodies"
  mkdir -p "$OTEL_BODY_DIR"
  : "${OTEL_EXPORTER_OTLP_ENDPOINT:=http://localhost:4317}"
  : "${BETA_TRACING_ENDPOINT:=http://localhost:4418}"
  : "${AI_TOOLKIT_OTEL_SPAN_ENDPOINT:=${AI_TOOLKIT_OTEL_SPAN_ENDPOINT_DEFAULT:-http://localhost:4318}}"
  OTEL_PREFIX="$(wt_native_otel_prefix "$SPOKE_RUN_ID" "$OTEL_BODY_DIR")"
fi

AGENT_CMD="${OTEL_PREFIX}WT_SPOKE=$(printf '%q' "$WT_TAG") CLAUDE_EFFORT=$(printf '%q' "$WT_AGENT_EFFORT") claude --model $(printf '%q' "$WT_AGENT_MODEL")"
[ -n "$PROMPT" ] && AGENT_CMD="$AGENT_CMD $(printf '%q' "$PROMPT")"

# Bring the collector + bridge up (idempotent) before the pane streams, so the
# relaunched spoke auto-populates Langfuse with no manual step.
wt_otel_collector_preflight "$REPO_ROOT"
wt_otel_bridge_preflight "$REPO_ROOT"

# --- spawn the tmux window ---------------------------------------------------
if [ "$SPAWN_TERMINAL" -eq 1 ]; then
  SPAWNED=0
  if command -v tmux >/dev/null 2>&1; then
    win_name="${BRANCH##*/}"
    sess="$(wt_tmux_session "$REPO_ROOT")"
    if tmux has-session -t "=$sess" 2>/dev/null || tmux new-session -d -s "$sess" -c "$REPO_ROOT" 2>/dev/null; then
      # `exec $SHELL` keeps the window alive after claude exits (matches worktree-new).
      win="$(tmux new-window -t "=$sess:" -P -F '#{window_id}' -n "$win_name" -c "$WT_DIR" \
             "$AGENT_CMD; exec ${SHELL:-zsh}")"
      tmux set-window-option -t "$win" automatic-rename off
      tmux set-window-option -t "$win" allow-rename off
      echo "→ relaunched tmux window '$win_name' ($win) in session $sess"
      if [ -n "${TMUX:-}" ]; then
        echo "  tmux switch-client -t '${sess}:${win_name}'"
      else
        echo "  tmux attach -t '${sess}' \\; select-window -t '${sess}:${win_name}'"
      fi
      SPAWNED=1
    fi
  fi
  if [ "$SPAWNED" -eq 0 ]; then
    echo
    echo "  Start the agent in a new terminal window:"
    echo "    cd \"$WT_DIR\" && $AGENT_CMD"
  fi
else
  echo
  echo "  Relaunch command (--no-terminal); run it in the worktree:"
  echo "    cd \"$WT_DIR\" && $AGENT_CMD"
fi

# The one-shot preflights above only cover this instant; re-arm the watchdog so it
# keeps the collector+bridge alive for the relaunched pane's lifetime (idempotent).
wt_otel_watch_arm "$REPO_ROOT"

# --- telemetry: relaunch lifecycle marker + script run-node ------------------
# Attributed to the spoke (emitted with the worktree as CWD), carrying the REUSED
# spoke_run_id. The `relaunch` phase lifecycle span is what #231's relaunch_count
# aggregates. No-op unless AI_TOOLKIT_TELEMETRY=1.
wt_emit_lifecycle "worktree-relaunch" "relaunch" "success" "$WT_T0" "$WT_DIR"
wt_emit_script "spoke-relaunch" "success" "$WT_T0" "$WT_DIR"

echo "✓ spoke-relaunch: relaunched #${ISSUE} (spoke_run_id $SPOKE_RUN_ID)"
