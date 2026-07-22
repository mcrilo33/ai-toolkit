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
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --no-terminal) SPAWN_TERMINAL=0 ;;
    --force)       FORCE=1 ;;
    -*)            wt_die "unknown option: $arg (supported: --no-terminal, --force)" ;;
    *)
      [ -z "$TARGET" ] || wt_die "unexpected extra argument: $arg"
      TARGET="$arg"
      ;;
  esac
done
[ -n "$TARGET" ] || wt_die "usage: spoke-relaunch.sh <issue|slug|branch|path> [--no-terminal] [--force]"

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

# The spoke role tag (WT_SPOKE) is the ORIGINAL tag worktree-new.sh minted the
# worktree with: it names the dir as `<repo>-<tag>`, so stripping that prefix round-
# trips the exact tag (an issue number, or an ad-hoc slug). `symbolic-ref` prints the
# branch when on one and is EMPTY on a detached HEAD (unlike `--abbrev-ref`, which
# yields the literal "HEAD"); every branch-derived value — the tmux window name, the
# seed-prompt issue — therefore falls back to the dir tag on a detached HEAD instead of
# the useless "HEAD" (which would mis-tag markers and defeat the double-launch guard).
WT_TAG="$(basename "$WT_DIR")"
WT_TAG="${WT_TAG#"$(basename "$REPO_ROOT")-"}"
BRANCH="$(git -C "$WT_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [ -n "$BRANCH" ]; then
  WIN_NAME="${BRANCH##*/}"   # exactly what worktree-new.sh named the window
  ISSUE="${WIN_NAME%%-*}"
  BRANCH_KNOWN=1
else
  # Detached HEAD: the original branch slug (the live window's name) is unrecoverable, so
  # WIN_NAME is only a best-effort tag and the double-launch guard cannot match a live
  # window by name. BRANCH_KNOWN=0 downgrades the guard to a warning (see below).
  WIN_NAME="$WT_TAG"
  ISSUE="$WT_TAG"
  BRANCH_KNOWN=0
fi
# A non-numeric leading slug (an ad-hoc worktree) still references the tag as the issue.
case "$ISSUE" in
  '' | *[!0-9]*) ISSUE="$WT_TAG" ;;
esac

# The tmux session (one per project, #39) — derived ONCE and reused by the guard below and the
# window spawn near the end, so the two can never target different sessions. Empty when not
# spawning a terminal or tmux is absent.
SESS=""
if [ "$SPAWN_TERMINAL" -eq 1 ] && command -v tmux >/dev/null 2>&1; then
  SESS="$(wt_tmux_session "$REPO_ROOT")"
fi

# --- double-launch guard (before any side effect) ----------------------------
# Relaunch is for a DEAD pane; a second claude on the same branch + reused spoke_run_id would
# race the ref (concurrent commits/pushes — the tripwire-corruption class this repo warns about).
# Run this BEFORE the OTel docker preflights below so a refusal never leaves containers running.
if [ -n "$SESS" ] && [ "$FORCE" -eq 0 ]; then
  if [ "$BRANCH_KNOWN" -eq 0 ]; then
    # Detached HEAD: the live window's branch-slug name is unknown, so the guard can neither
    # confirm nor deny a live pane by name. Recovery is the script's primary job (an unattended
    # hub/afk relaunch only fires when it already believes the pane is dead), and a detached
    # worktree is NOT on its branch — so the branch-ref race the guard exists to prevent cannot
    # happen from here anyway. WARN and PROCEED rather than block (which would strand the exact
    # automated recovery this exists to do); --force silences the warning.
    wt_warn "detached-HEAD worktree — cannot verify no live pane by window name; proceeding. If a claude is still running there, Ctrl-C now."
  elif tmux list-windows -t "=$SESS" -F '#{window_name}' 2>/dev/null | grep -qxF "$WIN_NAME"; then
    # `list-windows` on a missing session is a no-op (no match), so a first-ever relaunch passes.
    wt_die "a tmux window '$WIN_NAME' already exists in session '$SESS' — the pane may be live. Close it first, or pass --force to relaunch anyway."
  fi
fi

# --- ledger skeleton: reuse the persisted one (issue #235 seeder) ------------
# The #235 skeleton was written at spawn and survives a pane crash, so the ledger
# is intact on disk; the relaunch seed prompt points the agent back at it. Warn
# (don't fail) if it is genuinely absent — the seed prompt falls back to
# /source-task, which re-anchors from the live issue.
LEDGER_SKELETON="$WT_DIR/.ai-toolkit/ledger-skeleton.md"
if [ -f "$LEDGER_SKELETON" ]; then
  echo "→ ledger skeleton     .ai-toolkit/ledger-skeleton.md (intact)"
  LEDGER_CLAUSE="re-seed your task ledger from .ai-toolkit/ledger-skeleton.md (one entry per subtask x ANCHOR/RED/GREEN/REVIEW/PUSH)"
else
  wt_warn "no .ai-toolkit/ledger-skeleton.md — the spoke re-derives its ledger from task.md."
  LEDGER_CLAUSE="rebuild your task ledger from the task contract (one entry per subtask x ANCHOR/RED/GREEN/REVIEW/PUSH)"
fi

# --- relaunch-aware seed prompt ----------------------------------------------
# The ledger clause tracks whether the skeleton is actually on disk, so the agent is
# never told to read a file the script just reported absent.
TASK_MD="$WT_DIR/.ai-toolkit/task.md"
if [ -f "$TASK_MD" ]; then
  PROMPT="Your spoke pane was relaunched (issue #${ISSUE}); the worktree, branch and spoke_run_id are intact. Read your task contract at .ai-toolkit/task.md and ${LEDGER_CLAUSE}. Check git log and your pushed branch to see which subtasks already landed, then resume the solo-cycle from the first unfinished step. Honor the task's Gate: line. If task.md is missing or the issue changed, run /source-task ${ISSUE} to re-anchor."
else
  PROMPT="Your spoke pane was relaunched. Run /source-task ${ISSUE} to re-anchor from the live issue, then resume the solo-cycle."
fi

# --- pin model/effort via the shared helper (issue #142/#233) ----------------
# Same resolution as worktree-new.sh so a relaunched spoke never runs on a
# different/stale model than a freshly-spawned one for the same issue.
WT_CONFIG="${AI_TOOLKIT_CONFIG:-$REPO_ROOT/settings/ai-toolkit.yml}"
wt_resolve_agent_model "$SCRIPT_DIR" "$WT_CONFIG"

# --- native-OTel launch prefix (shared with worktree-new via worktree-lib) ---
# Reuses the SAME spoke_run_id so the relaunched pane streams into the existing
# Langfuse session rather than starting a new trace. wt_native_otel_prefix builds the
# whole launch prefix. The span-sink endpoint is ALSO set in THIS shell: the
# wt_emit_lifecycle/wt_emit_script calls below run telemetry.sh's emit, whose OTLP sink
# fires only when AI_TOOLKIT_OTEL_SPAN_ENDPOINT is set (the helper defaults it only in its
# own subshell, which cannot leak back). AI_TOOLKIT_OTEL=0 is a clean full opt-out.
wt_resolve_telemetry_config "$WT_CONFIG"
AI_TOOLKIT_OTEL="${AI_TOOLKIT_OTEL:-${AI_TOOLKIT_OTEL_DEFAULT:-1}}"
OTEL_PREFIX=""
if [ "${AI_TOOLKIT_OTEL:-}" = "1" ]; then
  OTEL_BODY_DIR="$WT_DIR/.ai-toolkit/raw-bodies"
  mkdir -p "$OTEL_BODY_DIR"
  wt_default_span_endpoint
  # repo=<name> (issue #343): the cross-project dimension, resolved like the spawn path so a
  # relaunched spoke stamps the same repo as a fresh one; the collector lifts it onto live spans.
  OTEL_PREFIX="$(wt_native_otel_prefix "$SPOKE_RUN_ID" "$OTEL_BODY_DIR" "$(wt_repo_name "$REPO_ROOT")")"
fi

AGENT_CMD="${OTEL_PREFIX}WT_SPOKE=$(printf '%q' "$WT_TAG") CLAUDE_EFFORT=$(printf '%q' "$WT_AGENT_EFFORT") claude --model $(printf '%q' "$WT_AGENT_MODEL")"
# Re-apply the same in-process budget ceiling a fresh spawn would (worktree-new.sh),
# so a relaunched unattended spoke keeps its cap. A pre-formed multi-arg string,
# appended verbatim (not %q-quoted); unset for ordinary attended relaunches.
[ -n "${WT_AGENT_BUDGET_ARGS:-}" ] && AGENT_CMD="$AGENT_CMD ${WT_AGENT_BUDGET_ARGS}"
# PROMPT is always assigned above (both branches), so it is appended unconditionally.
AGENT_CMD="$AGENT_CMD $(printf '%q' "$PROMPT")"

# Bring the collector + bridge up (idempotent) before the pane streams, so the
# relaunched spoke auto-populates Langfuse with no manual step.
wt_otel_collector_preflight "$REPO_ROOT"
wt_otel_bridge_preflight "$REPO_ROOT"

# --- spawn the tmux window ---------------------------------------------------
if [ "$SPAWN_TERMINAL" -eq 1 ]; then
  SPAWNED=0
  if [ -n "$SESS" ]; then
    # On-branch this window name was collision-checked by the guard above; on a detached HEAD the
    # guard could only warn (the name is unknown), so no name-vetting guarantee holds there.
    win_name="$WIN_NAME"
    sess="$SESS"  # the single derivation from above, reused so the guard and spawn never diverge
    if tmux has-session -t "=$sess" 2>/dev/null || tmux new-session -d -s "$sess" -c "$REPO_ROOT" 2>/dev/null; then
      # `exec $SHELL` keeps the window alive after claude exits (matches worktree-new). Guard
      # the command substitution: under `set -e` a bare `win=$(...)` that fails (a tmux hiccup,
      # or the session killed between has-session and new-window) would abort the whole script
      # BEFORE the manual-launch fallback — so branch on it and let a failure fall through.
      if win="$(tmux new-window -t "=$sess:" -P -F '#{window_id}' -n "$win_name" -c "$WT_DIR" \
                "$AGENT_CMD; exec ${SHELL:-zsh}")"; then
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
