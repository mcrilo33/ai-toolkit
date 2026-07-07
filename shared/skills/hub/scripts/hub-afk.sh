#!/usr/bin/env bash
# hub-afk.sh — unattended backlog-drain supervisor for the planning hub (issue #71).
#
# The single toggle that ties the parallel-worktrees workflow together: for a bounded
# window (or until the backlog is empty), keep the backlog draining as fast as the
# dependency graph allows, with ZERO human input. It is the sole unattended
# backlog-drain supervisor.
#
# Each supervisor tick does four mechanical things and exactly ONE reasoning thing:
#   1. PLAN + DISPATCH the next concurrent batch via batch-plan.sh (#70), seeding each
#      spoke with the standard ultra kickoff. Already-in-flight spokes are picked up.
#   2. AUTO-ANSWER every spoke parked on a question / gate (⚠ WAITING ON INPUT): extract
#      the prompt from its transcript, hand it to an ANSWERER (a headless `claude -p` with
#      a thinking budget) that follows the `afk-answering` rule, and inject the returned
#      answer into the spoke's tmux pane. This single reasoning step is the sanctioned
#      exception to the scripted control plane; everything else stays scripted. Each
#      decision is emitted as a telemetry span so it surfaces on the #35 dashboard.
#   3. AUTO-LAND every ready/<issue> via worktree-land.sh (suite + merge + push + teardown
#      + close). A land frees a dependent's blocker and its scope, so the next tick's plan
#      unlocks more. A failed land emits blocked/<issue> and the drain continues.
#   4. REAP hung / idle / over-ceiling spokes (blocked/<issue>), so a doom-loop can't burn
#      the whole window.
# It STOPS at the time bound, or — in drain mode — when the backlog is empty and nothing
# is in flight. No report artifact is written: the observability dashboard is the single
# source of truth for what happened.
#
# Knobs (env, with defaults):
#   AFK_TICK_SECONDS=120         supervisor poll interval
#   AFK_WATCHDOG_SECONDS=60      watchdog poll interval (respawn a crashed supervisor)
#   AFK_SPOKE_MAX_MINUTES=180    wall-clock ceiling per spoke before a reap
#   AFK_IDLE_MINUTES=30          a spoke idle this long with no marker AND not waiting → reap
#   AFK_ANSWERER_CMD             the answerer command (default: claude -p --model claude-fable-5)
#   AFK_ANSWERER_EFFORT=high     thinking budget for the answerer (exported as CLAUDE_EFFORT)
#   AFK_NOW                      override "now" (epoch seconds) — testing/cron
#   AFK_STATE                    state-file path (default: <git-common-dir>/.afk-state)
#   AFK_HEARTBEAT                heartbeat-file path (default: <git-common-dir>/.afk-heartbeat)
#   AFK_RULE_FILE                path to the afk-answering rule (auto-resolved otherwise)
#   WT_NEW / WT_LAND / SPOKE_READY / BATCH_PLAN   override the resolved sibling scripts
#   AFK_WT_LIB                   override the sourced worktree-lib.sh
#   AI_TOOLKIT_OTEL              telemetry opt-out: =0 disables the preflight (#108);
#                                unset/anything else ⇒ enabled (the SSOT-for-unattended default)
#   AFK_TELEMETRY_CONF           optional conf file for LANGFUSE_BASIC_AUTH / LANGFUSE_HOST
#                                when env leaves auth unset (env wins) [default: ~/.afk-telemetry]
#   AFK_PORT_WAIT_TRIES/SLEEP    collector/bridge re-probe attempts + interval after a launch
#   CLAUDE_PROJECTS_DIR          transcript root (default: $HOME/.claude/projects)
#   AFK_REMOTE_HOST / AFK_REMOTE_REPO / AFK_REMOTE_SESSION / AFK_REMOTE_DRAIN_CMD
#                                --remote target config (or a sourced AFK_REMOTE_CONF file)
#
# Usage:
#   hub-afk.sh <duration>        # e.g. 90, 30m, 1h, 1h30m — drain for that long, then stop
#   hub-afk.sh until <HH:MM>     # drain until the next HH:MM, then stop
#   hub-afk.sh drain             # drain until the backlog is empty + nothing in flight
#   hub-afk.sh --remote          # launch a detached `drain` on a configured always-on Mac
#   hub-afk.sh --status          # report the active window, "off", or "STALE" (crashed)
#   hub-afk.sh --off             # stop the supervisor + watchdog (clears the state file)
#   hub-afk.sh --once            # run a single tick and exit (tests / external cron)
#   hub-afk.sh --watchdog        # the keeper loop: respawn the supervisor if it crashes
#                                #   (auto-spawned on arm; rarely run by hand)
#
# Run it on the hub (main checkout, on the default branch). Read-only against the work
# except for dispatching, answering, landing, and reaping spokes. --remote runs the drain
# on a different machine instead (see docs/remote-afk.md).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${AFK_TICK_SECONDS:=120}"
: "${AFK_SPOKE_MAX_MINUTES:=180}"
: "${AFK_IDLE_MINUTES:=30}"
: "${AFK_ANSWERER_EFFORT:=high}"

# Raised to 1 when the answerer's own `claude` reports an auth failure (the
# subscription token could not refresh): a process-global the main loop reads to halt
# instead of spinning into dead auth. decide_and_act runs in the same shell as the
# loop, so the assignment propagates up.
_AFK_AUTH_FAILED=0

# --- source worktree-lib.sh (the shared date/time + worktree helpers) ---------
# Resolution covers both layouts: the ai-toolkit checkout (scripts/worktree-lib.sh,
# four levels up from this hub script) and a synced target (co-located flat in
# .ai-toolkit/scripts/). AFK_WT_LIB wins for tests.
_AFK_TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null || true)"
for _cand in \
  "${AFK_WT_LIB:-}" \
  "$SCRIPT_DIR/worktree-lib.sh" \
  "$SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/scripts/worktree-lib.sh}" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/worktree-lib.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

# --- source gate-broker.sh (the shared gate-broker core, issue #155) ----------
# detect / extract / reason / classify / inject / log + broker_service_gate. /afk's answerer
# is the unattended adapter over this core; decide_and_act is a thin wrapper onto it. It also
# provides log() and afk_now(). Resolution mirrors worktree-lib above; AFK_GATE_BROKER wins
# for tests.
for _cand in \
  "${AFK_GATE_BROKER:-}" \
  "$SCRIPT_DIR/gate-broker.sh" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/shared/skills/hub/scripts/gate-broker.sh}" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/gate-broker.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

# --- window spec → end epoch (the pure time layer) ----------------------------

# parse_duration <spec> -> seconds, or empty (rc 1) when the spec is not a duration.
# Accepts a bare number (minutes), <N>h, <N>m, and <N>h<M>m. Zero / empty / malformed
# returns empty so compute_end_epoch can reject it rather than starting a 0-length window.
parse_duration() {
  local spec="$1" total=0 h="" m=""
  case "$spec" in
    *[!0-9hm]* | '') return 1 ;;                 # only digits, 'h', 'm'
    *h*m) h="${spec%%h*}"; m="${spec#*h}"; m="${m%m}" ;;
    *h)   h="${spec%h}"; m=0 ;;
    *m)   h=0; m="${spec%m}" ;;
    *)    h=0; m="$spec" ;;                       # bare number ⇒ minutes
  esac
  case "$h$m" in '' | *[!0-9]*) return 1 ;; esac   # guard empty fields (e.g. "hm")
  total=$(( h * 3600 + m * 60 ))
  [ "$total" -gt 0 ] || return 1
  printf '%s\n' "$total"
}

# compute_end_epoch <spec...> <now> -> the end epoch, or the literal `drain`, on stdout;
# rc 1 on an unrecognized spec. The spec is everything before the trailing now epoch:
#   drain                  -> `drain`            (no clock bound)
#   until <HH:MM>          -> next HH:MM at/after now
#   <duration>             -> now + parsed seconds
compute_end_epoch() {
  local now spec secs target
  now="${@: -1}"                                   # last arg is the now epoch
  set -- "${@:1:$#-1}"                             # everything before it is the spec
  spec="$*"
  case "$1" in
    drain) printf 'drain\n'; return 0 ;;
    until)
      [ -n "${2:-}" ] || return 1
      target="$(wt_epoch_at "$(wt_date_ymd "$now")" "$2")" || return 1
      [ -n "$target" ] || return 1
      [ "$target" -le "$now" ] && target=$(( target + 86400 ))
      printf '%s\n' "$target"; return 0 ;;
    *)
      secs="$(parse_duration "$spec")" || return 1
      printf '%s\n' "$(( now + secs ))"; return 0 ;;
  esac
}

# window_expired <state> <now> -> true (rc 0) when a clock-bound window has elapsed.
# `drain` and an empty state never expire by the clock.
window_expired() {
  local state="$1" now="$2"
  case "$state" in '' | drain | *[!0-9]*) return 1 ;; esac
  [ "$now" -ge "$state" ]
}

# minutes_remaining <state> <now> -> whole minutes left in a clock-bound window (>=0),
# or empty for drain / off (no clock bound). Used by --status.
minutes_remaining() {
  local state="$1" now="$2" rem
  case "$state" in '' | drain | *[!0-9]*) return 0 ;; esac
  rem=$(( (state - now) / 60 ))
  [ "$rem" -lt 0 ] && rem=0
  printf '%s\n' "$rem"
}

# --- state file ---------------------------------------------------------------
# The window bound persists out of the work tree (under the git common dir) so it
# survives a supervisor restart and a second shell can flip it off mid-run.

afk_state_file() {
  if [ -n "${AFK_STATE:-}" ]; then printf '%s\n' "$AFK_STATE"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/.afk-state"
}

afk_write_state() { printf '%s\n' "$1" > "$(afk_state_file)"; }
afk_read_state()  { local f; f="$(afk_state_file)"; [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' || true; }
afk_clear_state() { rm -f "$(afk_state_file)" 2>/dev/null || true; afk_clear_heartbeat; }

# --- landed tally + drain-complete hand-off (issue #150) ----------------------
# A completed drain fires ONE "drain complete — <k> landed" notification, but <k>
# is not externally derivable: log() writes to stderr (redirected to /dev/null on
# most launch paths), there is no persisted tally, and .afk-state is cleared on
# stop. So the supervisor keeps its own landed counter HERE, in a file under the
# git common dir — a file, not an in-process var, so it survives the watchdog's
# no-arg supervisor respawn mid-window (#107) — incremented once per successful
# auto_land and reset on a fresh arm. At the clean drain-stop the count is handed
# to hub-notify.sh by writing <git-common-dir>/.afk-drain-complete, which
# hub-notify consumes-and-clears (fires once). AFK_LANDED_COUNT / AFK_DRAIN_COMPLETE
# override the two paths for tests.
afk_landed_count_file() {
  if [ -n "${AFK_LANDED_COUNT:-}" ]; then printf '%s\n' "$AFK_LANDED_COUNT"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/.afk-landed-count"
}
afk_drain_complete_file() {
  if [ -n "${AFK_DRAIN_COMPLETE:-}" ]; then printf '%s\n' "$AFK_DRAIN_COMPLETE"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/.afk-drain-complete"
}
# afk_read_landed_count -> the tally, defaulting to 0 for an absent, empty, or
# partially-written (non-numeric) file so the emit never crashes on a corrupt read.
afk_read_landed_count() {
  local f n; f="$(afk_landed_count_file)"
  n="$( [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' )"
  case "$n" in '' | *[!0-9]*) n=0 ;; esac
  printf '%s\n' "$n"
}
_afk_incr_landed() {
  printf '%s\n' "$(( $(afk_read_landed_count) + 1 ))" > "$(afk_landed_count_file)" 2>/dev/null || true
}
_afk_clear_landed_count() { rm -f "$(afk_landed_count_file)" 2>/dev/null || true; }
_afk_clear_drain_complete() { rm -f "$(afk_drain_complete_file)" 2>/dev/null || true; }
# _afk_emit_drain_complete -> hand the final tally to hub-notify at drain-stop:
# write the count to .afk-drain-complete, then reset the counter so the next
# window starts fresh. Best-effort; a write failure never aborts the stop path.
_afk_emit_drain_complete() {
  printf '%s\n' "$(afk_read_landed_count)" > "$(afk_drain_complete_file)" 2>/dev/null || true
  _afk_clear_landed_count
}

# --- heartbeat (issue #107) ---------------------------------------------------
# Each supervisor tick stamps "<pid> <last_tick_epoch>" here so a second shell (and the
# watchdog) can tell a LIVE supervisor from a stale state file. A silent crash (the
# supervisor exited 0 mid-tick) leaves .afk-state armed with no process behind it, and
# without this --status would echo a `draining` run that is gone (#107). The pid is THIS
# supervisor's; cross-checking its liveness (kill -0) is the truth .afk-state cannot give.
afk_heartbeat_file() {
  if [ -n "${AFK_HEARTBEAT:-}" ]; then printf '%s\n' "$AFK_HEARTBEAT"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/.afk-heartbeat"
}
afk_write_heartbeat() { printf '%s %s\n' "$$" "$(afk_now)" > "$(afk_heartbeat_file)" 2>/dev/null || true; }
afk_read_heartbeat()  { local f; f="$(afk_heartbeat_file)"; [ -f "$f" ] && head -n1 "$f" 2>/dev/null || true; }
afk_clear_heartbeat() { rm -f "$(afk_heartbeat_file)" 2>/dev/null || true; }

# _afk_pid_alive <pid> -> true when <pid> is a live process. An empty / non-numeric pid is
# never alive (guards `kill` against a bareword and a truncated partial heartbeat).
_afk_pid_alive() {
  case "${1:-}" in '' | *[!0-9]*) return 1 ;; esac
  kill -0 "$1" 2>/dev/null
}

# afk_supervisor_state -> off | live | stale: the GROUND TRUTH of whether a supervisor is
# actually running, cross-checking .afk-state against the heartbeat pid (#107):
#   off   — no window armed (.afk-state empty).
#   live  — a window is armed AND the heartbeat pid is a live process.
#   stale — a window is armed but the heartbeat pid is gone, or there is no heartbeat —
#           the supervisor crashed and the state file is lying.
# UPGRADE: also require a recent tick (afk_now - heartbeat epoch < a few tick intervals)
# if pid reuse — the OS recycling a crashed supervisor's pid onto an unrelated process —
# ever produces a false `live`. kill -0 alone is the issue's spec and reuse is rare here.
afk_supervisor_state() {
  [ -n "$(afk_read_state)" ] || { printf 'off\n'; return; }
  local hb pid; hb="$(afk_read_heartbeat)"; pid="${hb%% *}"
  if _afk_pid_alive "$pid"; then printf 'live\n'; else printf 'stale\n'; fi
}

# _afk_heartbeat_age_minutes -> whole minutes since the last tick stamp, or empty when
# there is no heartbeat. Used by --status to report how long ago the supervisor ticked.
_afk_heartbeat_age_minutes() {
  local hb tick; hb="$(afk_read_heartbeat)"; [ -n "$hb" ] || return 0
  tick="${hb##* }"
  case "$tick" in '' | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "$(( ($(afk_now) - tick) / 60 ))"
}

# --- reaping ------------------------------------------------------------------
_kill_spoke_window() {
  local issue="$1" target name
  command -v tmux >/dev/null 2>&1 || return 0
  tmux list-windows -a -F '#{session_name}:#{window_index} #{window_name}' 2>/dev/null \
  | while read -r target name; do
      case "$name" in "${issue}-"* | "$issue") tmux kill-window -t "$target" 2>/dev/null || true ;; esac
    done
}
reap_spoke() {
  local wt="$1" issue="$2" reason="$3"
  log "→ reap #$issue: $reason"
  _kill_spoke_window "$issue"
  _escalate_blocked "$wt" "$issue" "$reason"
}

# --- crash ≠ hang: auto-resume-once a pane-dead spoke (issue #109) -------------
# A reaped spoke is not always hung. The reaper abandoned #103 as "idle, likely hung"
# when its tmux PANE had crashed but its committed work was intact. So before declaring
# blocked we distinguish a DEAD pane (session crashed → re-adopt the worktree ONCE,
# reusing the spoke_run_id) from a LIVE-but-idle pane (truly hung → block).

# _spoke_pane_alive <wt> -> true when a live tmux pane maps to the worktree. Empty target
# (the spoke's pane crashed / its window is gone) ⇒ dead.
_spoke_pane_alive() { [ -n "$(_spoke_pane_target "$1")" ]; }

# _afk_default_ref <wt> -> the ref the spoke branched from, so "has commits" measures work
# ABOVE the branch point. AFK_DEFAULT_BRANCH wins (historical top precedence, kept for
# back-compat — it predates and now aliases AI_TOOLKIT_BASE_BRANCH, which the canonical
# resolver honors); else wt_base_branch (issue #117: config ai-toolkit.base-branch >
# AI_TOOLKIT_BASE_BRANCH > origin/HEAD > … > `main`), sourced via worktree-lib.sh above.
_afk_default_ref() {
  local wt="$1" ref
  [ -n "${AFK_DEFAULT_BRANCH:-}" ] && { printf '%s\n' "$AFK_DEFAULT_BRANCH"; return; }
  if command -v wt_base_branch >/dev/null 2>&1; then
    wt_base_branch "$wt"
    printf '\n'
    return
  fi
  ref="$(git -C "$wt" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)"
  printf '%s\n' "${ref:-main}"
}

# _spoke_has_commits <wt> -> true when HEAD carries work to preserve: a commit ABOVE the
# branch point (merge-base HEAD <default> != HEAD). A worktree is cut from the default
# branch, so a bare "HEAD exists" is always true and would be meaningless; this is the AC1
# "with commits" test. If the base can't be resolved we can't measure it, so we favor
# preserving work (true) — the resume is bounded to once regardless.
_spoke_has_commits() {
  local wt="$1" ref base tip
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  ref="$(_afk_default_ref "$wt")"
  base="$(git -C "$wt" merge-base HEAD "$ref" 2>/dev/null)" || return 0
  [ -n "$base" ] || return 0
  [ "$base" != "$tip" ]
}

# the once-per-window resume stamp: a spoke is auto-resumed at most ONCE per armed window
# (a second crash escalates to a human). Cleared on a fresh arm (_clear_resume_markers).
_afk_resumed_marker()  { printf '%s\n' "$(_afk_state_dir)/resumed-$1"; }
_afk_already_resumed() { [ -f "$(_afk_resumed_marker "$1")" ]; }
_afk_mark_resumed() {
  local m; m="$(_afk_resumed_marker "$1")"
  mkdir -p "$(dirname "$m")" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$m" 2>/dev/null || true
}
_clear_resume_markers() { rm -f "$(_afk_state_dir)"/resumed-* 2>/dev/null || true; }

# _afk_spoke_run_id <wt> -> the spoke's persisted spoke_run_id (worktree-new.sh stamps it
# at .ai-toolkit/spoke-run-id), so a resumed run groups under the SAME spoke in Langfuse.
# Synthesized from the branch + now-clock if the file is missing.
_afk_spoke_run_id() {
  local wt="$1" f id branch
  f="$wt/.ai-toolkit/spoke-run-id"
  [ -f "$f" ] && id="$(head -n1 "$f" 2>/dev/null | tr -d '[:space:]')"
  if [ -z "${id:-}" ]; then
    branch="$(git -C "$wt" branch --show-current 2>/dev/null)"
    id="${branch:-spoke}+$(afk_now)"
  fi
  printf '%s\n' "$id"
}

# _afk_resume_prompt <issue> -> the plain-English first message for the resumed session.
# Deliberately NOT a slash command: `/cycle` is not a real command (the skill is
# solo-cycle), so a seeded `/cycle` would fail and re-strand the spoke.
_afk_resume_prompt() {
  local issue="$1"
  cat <<EOF
Your session crashed and the AFK supervisor restored this window. Your committed work is
intact -- do NOT start over. Run /source-task $issue to re-anchor, re-read your task ledger
and the working tree to see where you left off, then continue the solo flow (RED -> GREEN ->
REVIEW -> PUSH) from there. Push each subtask and emit the ready marker when the issue's
acceptance criteria are all met. Do NOT self-land -- the hub lands #$issue.
EOF
}

# _afk_continue_command <wt> <prompt> -> the `claude --continue '<prompt>'` launch
# command for a re-opened spoke window (crash resume, wedge respawn). Pure (returns the
# string) so it is inspectable in a test. It inline-exports the telemetry the window
# needs to keep reaching the collector — recovery must not fly blind (#108):
# AI_TOOLKIT_OTEL=1, the supervisor's OTLP endpoint, the workflow-span sink
# (AI_TOOLKIT_OTEL_SPAN_ENDPOINT, #126), and the re-pinned spoke_run_id. The
# auth header stays in the inherited env (never on the command line), exactly as
# worktree-new.sh does. `claude --continue` resumes the crashed session in the worktree.
# UPGRADE: replicate worktree-new.sh's full beta-tracing/raw-body env for per-tool parity.
_afk_continue_command() {
  local wt="$1" prompt="$2" run_id endpoint span_endpoint
  run_id="$(_afk_spoke_run_id "$wt")"
  endpoint="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4317}"
  # Workflow-span sink (#126), resume parity with worktree-new.sh: telemetry.sh's
  # cycle step:/script/hook spans are gated on this var and POST over OTLP-HTTP,
  # so it targets the collector's :4318 listener, not the gRPC endpoint above.
  span_endpoint="${AI_TOOLKIT_OTEL_SPAN_ENDPOINT:-http://localhost:4318}"
  printf 'AI_TOOLKIT_OTEL=1 OTEL_EXPORTER_OTLP_ENDPOINT=%s AI_TOOLKIT_OTEL_SPAN_ENDPOINT=%s OTEL_RESOURCE_ATTRIBUTES=%s claude --continue %s\n' \
    "$(printf '%q' "$endpoint")" "$(printf '%q' "$span_endpoint")" \
    "$(printf '%q' "spoke_run_id=$run_id")" "$(printf '%q' "$prompt")"
}

# _afk_resume_command <wt> <issue> -> the launch command for a crash-resumed window:
# a continue with the plain-English re-anchor prompt.
_afk_resume_command() { _afk_continue_command "$1" "$(_afk_resume_prompt "$2")"; }

# _afk_wedge_respawn_command <wt> <issue> <answer> -> the launch command for a pane
# respawned out of a wedged composer (#133): the ANSWER rides verbatim as the
# continuation prompt — the proven manual recovery, no supervisor preamble — so the
# respawn itself delivers what the inject could not. <issue> is unused but keeps the
# (wt, issue, ...) call-site symmetry with _afk_resume_command.
_afk_wedge_respawn_command() { _afk_continue_command "$1" "$3"; }

# _afk_open_spoke_window <wt> <issue> <cmd> -> open a fresh tmux window in the project
# session, cd'd into the worktree, running <cmd>. Mirrors worktree-new.sh's
# project-session window layout. rc 1 when tmux is unavailable or the window can't be
# opened. Shared by the crash resume and the wedge respawn (#133).
_afk_open_spoke_window() {
  local wt="$1" issue="$2" cmd="$3" sess win
  command -v tmux >/dev/null 2>&1 || return 1
  sess="$(wt_tmux_session "${MAIN_ROOT:-$(wt_main_root 2>/dev/null)}")"
  # Name the window with the branch SLUG (the "<issue>-<slug>" worktree-new.sh convention),
  # NOT the full "feature/<issue>-…" branch: _kill_spoke_window only matches "<issue>-"* /
  # "<issue>", so a full-branch name would orphan the reopened window on a later reap.
  win="$(git -C "$wt" branch --show-current 2>/dev/null)"; win="${win##*/}"; win="${win:-$issue}"
  tmux has-session -t "=$sess" 2>/dev/null || tmux new-session -d -s "$sess" -c "$wt" 2>/dev/null
  tmux new-window -t "=$sess:" -n "$win" -c "$wt" "$cmd; exec ${SHELL:-zsh}" 2>/dev/null || return 1
  # Pin the name so the running claude/zsh can't rename the window out of the kill match.
  tmux set-window-option -t "=$sess:$win" automatic-rename off 2>/dev/null || true
  return 0
}

# resume_spoke <wt> <issue> -> re-open the crashed spoke's window running the resume
# command; stamp the once-per-window marker and a success span. rc 1 when the window
# can't be opened (the caller then falls back to blocking).
resume_spoke() {
  local wt="$1" issue="$2"
  log "→ resume #$issue: pane crashed with work intact — re-adopting once"
  if ! _afk_open_spoke_window "$wt" "$issue" "$(_afk_resume_command "$wt" "$issue")"; then
    log "  could not open a resume window for #$issue"
    return 1
  fi
  _afk_mark_resumed "$issue"
  stamp_progress_epoch "$issue"   # a deliberate revival resets the reap ceiling (#133)
  _afk_emit_span "$wt" afk-resume success
  return 0
}

# respawn_wedged_spoke <wt> <issue> <answer> -> recover a wedged composer (an
# unterminated paste no keystroke can submit or clear, #123/#124): kill the spoke's
# window and reopen it running `claude --continue '<answer>'` under the same
# spoke_run_id — the respawn itself delivers the answer, so the park is resolved.
# Delivery is CONFIRMED like an inject: the continued session must start writing its
# transcript, else a window whose `claude` died instantly (dead auth, PATH) would be
# scored success and the answer silently lost. rc 1 when the window can't be
# reopened or never starts writing (the caller escalates).
respawn_wedged_spoke() {
  local wt="$1" issue="$2" answer="$3" before
  log "→ respawn #$issue: composer wedged (unterminated paste) — respawning the pane with the answer"
  before="$(_transcript_mtime "$wt")"
  _kill_spoke_window "$issue"
  if ! _afk_open_spoke_window "$wt" "$issue" "$(_afk_wedge_respawn_command "$wt" "$issue" "$answer")"; then
    log "  could not open a respawn window for #$issue"
    return 1
  fi
  if ! _transcript_advanced "$wt" "$before"; then
    log "  respawned window never started writing its transcript — escalating"
    return 1
  fi
  stamp_progress_epoch "$issue"   # a deliberate revival resets the reap ceiling (#133)
  _afk_emit_span "$wt" afk-wedge-respawn success
  return 0
}

# _reap_or_resume <wt> <issue> -> decide a reaped spoke's fate. An over-ceiling runaway
# always blocks (resume never applies). Otherwise it went idle: crash ≠ hang — a LIVE pane
# is truly hung (block); a DEAD pane with commits is auto-resumed ONCE in place; a dead
# pane with nothing to preserve, or one already resumed this window, is blocked.
_reap_or_resume() {
  local wt="$1" issue="$2"
  if _spoke_over_any_ceiling "$issue" "$(afk_now)"; then
    reap_spoke "$wt" "$issue" "time ceiling: ran >${AFK_SPOKE_MAX_MINUTES}m without finishing"
  elif _spoke_pane_alive "$wt"; then
    reap_spoke "$wt" "$issue" "went idle >${AFK_IDLE_MINUTES}m with a live pane and no marker — likely hung"
  elif ! _spoke_has_commits "$wt"; then
    reap_spoke "$wt" "$issue" "pane crashed with no committed work to preserve — needs a human"
  elif _afk_already_resumed "$issue"; then
    reap_spoke "$wt" "$issue" "pane crashed again after an auto-resume — needs a human"
  else
    resume_spoke "$wt" "$issue" \
      || reap_spoke "$wt" "$issue" "pane crashed and the auto-resume could not be launched — needs a human"
  fi
}

# _block_all_inflight <reason> -> emit blocked/<issue> for every in-flight spoke not
# already at a terminal marker. Called on an auth-failure stop so the dashboard shows
# every affected spoke as blocked (no orphaned window left silently stuck on dead auth)
# rather than just the one whose answerer surfaced the failure.
_block_all_inflight() {
  local reason="$1" path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "done" ] && continue
    _escalate_blocked "$path" "$issue" "$reason"
  done < <(inflight_worktrees)
}

# --- dispatch -----------------------------------------------------------------

# kickoff_for <issue> -> the spoke's first prompt: the standard ultra kickoff (the same
# handoff start-task / next-batch use). Under /afk the spoke runs in its normal attended
# posture — it pauses at its PLAN gate and asks questions as if a human were watching —
# and the supervisor's answerer plays the human. So the kickoff is deliberately the
# everyday one, NOT a "park, never ask" variant.
kickoff_for() {
  local n="$1"
  cat <<EOF
You're in a dedicated worktree for issue #$n. Run /source to anchor to issue #$n and read
it. Before touching code, break the issue body into a task ledger (TaskCreate, or
TodoWrite on older runtimes) — one todo per subtask × the solo-cycle steps that apply
(ANCHOR/RED/GREEN/REVIEW/PUSH), exactly one in_progress.

Honor the issue's Gate: line. If it is \`plan\` (the default for non-trivial work): the
PLAN gate comes first — explore the code, then print the full implementation plan (files,
approach, test strategy, open questions) as a normal visible message, emit the gate marker
(bash .ai-toolkit/scripts/spoke-ready.sh --gate $n) so the hub sees you parked, and WAIT
for approval before writing code (before GREEN). If the gate is \`none\`, run autonomous
straight through.

Then implement following the solo-cycle (/cycle: RED → GREEN → REVIEW → PUSH). Push your
own branch on every subtask without asking; when your ledger shows the issue's acceptance
criteria are all met, push the final subtask and emit the ready marker (bash
.ai-toolkit/scripts/spoke-push.sh --ready $n) — also without asking. Still ask before
genuinely dangerous or irreversible ops (force-push, history rewrites, anything touching
\`main\`, deletions outside the worktree). Do NOT self-land — the hub lands #$n.
EOF
}

# _inflight_scope_args -> repeated `--inflight "<scope>"` flags, one per live spoke, so
# batch-plan holds back a ready issue that collides with work already running. The Scope:
# line is read from each in-flight issue's body (the same source batch-plan reads). When a
# live spoke's scope CANNOT be resolved (gh failed, or the issue has no Scope: line) its
# footprint is unknown, so we emit `--inflight *` (exclusive) and batch-plan holds back
# EVERY ready issue until it lands — failing CLOSED under unattended /afk (#74) rather than
# co-dispatching into an unknown-scope collision.
_inflight_scope_args() {
  local issue body scope
  while IFS= read -r issue; do
    [ -n "$issue" ] || continue
    body="$(gh issue view "$issue" --json body -q .body 2>/dev/null || true)"
    scope="$(printf '%s\n' "$body" | sed -n 's/^[[:space:]]*[Ss]cope:[[:space:]]*//p' | head -1)"
    printf -- '--inflight\n%s\n' "${scope:-*}"
  done < <(inflight_issues)
}

# --- concurrency cap + dispatch stagger (issue #151) --------------------------
# The hub had no ceiling on live spokes, so a wide batch drove load high enough to
# starve the co-located Langfuse (permanent trace loss). Cap the batch and stagger
# spawns so first-push full suites don't all land on the box at once.

# _afk_cores -> logical CPU count (nproc → sysctl → 1). LC_ALL=C guards the
# locale-formatted-number class this repo has been bitten by (ps/date).
_afk_cores() {
  local n
  n="$(LC_ALL=C nproc 2>/dev/null || LC_ALL=C sysctl -n hw.ncpu 2>/dev/null || printf '1')"
  case "$n" in '' | *[!0-9]*) n=1 ;; esac
  [ "$n" -ge 1 ] || n=1
  printf '%s\n' "$n"
}

# _afk_batch_config_env -> the AI_TOOLKIT_BATCH_* lines the config parser emits for
# settings/ai-toolkit.yml, or nothing when the parser/config can't be resolved (a
# synced target ships neither). Best-effort: the caller falls back to its own default.
_afk_batch_config_env() {
  local cfg_py cfg_yml
  cfg_py="$(_afk_find_script "${AFK_CONFIG_PY:-}" ai_toolkit_config.py)" || return 0
  cfg_yml="${AI_TOOLKIT_CONFIG:-${MAIN_ROOT:-$_AFK_TOPLEVEL}/settings/ai-toolkit.yml}"
  [ -f "$cfg_yml" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  python3 "$cfg_py" batch-env "$cfg_yml" 2>/dev/null || true
}

# _afk_dispatch_cap -> the max concurrent spokes. AFK_SPOKE_CAP wins (operator/test
# seam), else the config's concurrency_cap, else auto min(2, cores/4) — always ≥1.
_afk_dispatch_cap() {
  local cap="${AFK_SPOKE_CAP:-}" cores AI_TOOLKIT_BATCH_CAP=""
  if [ -z "$cap" ]; then
    eval "$(_afk_batch_config_env)" 2>/dev/null || true
    cap="${AI_TOOLKIT_BATCH_CAP:-}"
  fi
  if [ -z "$cap" ]; then
    cores="$(_afk_cores)"
    cap=$(( cores / 4 )); [ "$cap" -gt 2 ] && cap=2
  fi
  case "$cap" in '' | *[!0-9]*) cap=1 ;; esac
  [ "$cap" -ge 1 ] || cap=1
  printf '%s\n' "$cap"
}

# _afk_dispatch_stagger -> seconds between consecutive spawns in one batch.
# AFK_DISPATCH_STAGGER wins (0 disables; the test seam), else the config, else 45.
_afk_dispatch_stagger() {
  local s="${AFK_DISPATCH_STAGGER:-}" AI_TOOLKIT_BATCH_STAGGER=""
  if [ -z "$s" ]; then
    eval "$(_afk_batch_config_env)" 2>/dev/null || true
    s="${AI_TOOLKIT_BATCH_STAGGER:-}"
  fi
  [ -n "$s" ] || s=45
  case "$s" in *[!0-9]*) s=45 ;; esac
  printf '%s\n' "$s"
}

# dispatch_batch -> plan the next concurrent batch (batch-plan.sh, capped) and spawn a
# spoke for each issue not already in flight, seeded with the ultra kickoff and staggered
# so first-push suites don't all hit at once. A missing planner or dispatcher logs and is
# a no-op (the next tick retries).
dispatch_batch() {
  [ "$_AFK_AUTH_FAILED" -eq 1 ] && return 0   # auth is dead — don't spawn spokes into it
  local bp wt_new inflight args=() batch n cap stagger spawned=0
  bp="$(_afk_find_script "${BATCH_PLAN:-}" batch-plan.sh)" || { log "batch-plan.sh not found — skipping dispatch"; return 0; }
  wt_new="$(_afk_find_script "${WT_NEW:-}" worktree-new.sh)" || { log "worktree-new.sh not found — skipping dispatch"; return 0; }
  inflight="$(inflight_issues)"
  while IFS= read -r line; do args+=("$line"); done < <(_inflight_scope_args)
  # Bound total live spokes: batch-plan truncates so (in-flight + dispatched) ≤ cap.
  cap="$(_afk_dispatch_cap)"
  stagger="$(_afk_dispatch_stagger)"
  args+=("--cap" "$cap")
  batch="$(bash "$bp" "${args[@]+"${args[@]}"}" 2>/dev/null || true)"
  for n in $batch; do
    printf '%s\n' "$inflight" | grep -qxF "$n" && continue   # already in flight (idempotent)
    # Stagger consecutive spawns (before the 2nd onward), so the co-located Langfuse
    # isn't hit by several first-push full suites at the same instant.
    [ "$spawned" -gt 0 ] && [ "$stagger" -gt 0 ] && sleep "$stagger" 2>/dev/null || true
    log "→ dispatch #$n"
    # --mode afk stamps the spoke's trace as drain-driven (#102); a hand-dispatched
    # spoke defaults to attended in worktree-new.sh.
    if bash "$wt_new" "$n" --type feature --mode afk --prompt "$(kickoff_for "$n")"; then
      stamp_dispatch_epoch "$n"
      spawned=$(( spawned + 1 ))
    else
      log "  dispatch of #$n failed — will retry next tick"
    fi
  done
}

# --- auto-land + reap passes --------------------------------------------------

# _afk_run_with_heartbeat <cmd...> -> run <cmd...> while stamping the heartbeat every
# AFK_LAND_HEARTBEAT_SECONDS (default 30), so the heartbeat EPOCH stays honest through
# the longest tick phase (auto-land's 6-10min suite) instead of freezing at tick top
# (#133 item 4). Honest scope (ST4 review): afk_supervisor_state is currently
# pid-based, so a stale epoch alone cannot flip --status to STALE or trigger a
# watchdog respawn today — this keeps the epoch trustworthy for the operator-facing
# age display and for the #107 UPGRADE (a tick-recency check), which must not
# misread a live land as a dead supervisor when it lands.
# Returns the command's exit code (a failed land must still escalate).
_afk_run_with_heartbeat() {
  local child rc slept interval="${AFK_LAND_HEARTBEAT_SECONDS:-30}"
  case "$interval" in '' | *[!0-9]* | 0) interval=30 ;; esac
  "$@" &
  child=$!
  while kill -0 "$child" 2>/dev/null; do
    afk_write_heartbeat
    # Re-check the child every second within the stamp interval — a full-interval
    # sleep would hold the tick up to AFK_LAND_HEARTBEAT_SECONDS after a fast land.
    slept=0
    while [ "$slept" -lt "$interval" ] && kill -0 "$child" 2>/dev/null; do
      sleep 1 2>/dev/null || true
      slept=$(( slept + 1 ))
    done
  done
  wait "$child"; rc=$?
  afk_write_heartbeat
  return "$rc"
}

# _ready_at_tip <wt_path> <issue> -> true when ready/<issue> points at the branch tip.
# Only a ready/ marker is auto-landed: accept/ awaits a human sign-off and blocked/ is
# already a parked terminal state.
_ready_at_tip() {
  local wt="$1" issue="$2" tip marker
  tip="$(git -C "$wt" rev-parse HEAD 2>/dev/null)" || return 1
  marker="$(git -C "$wt" rev-parse -q --verify "refs/tags/ready/${issue}^{commit}" 2>/dev/null)"
  [ -n "$marker" ] && [ "$marker" = "$tip" ]
}

# _blocked_at_tip <wt_path> <issue> -> true when blocked/<issue> points at the branch tip.
# A deterministic land failure (a genuine merge conflict) escalates blocked/<issue> at the
# tip, right where ready/<issue> still sits — so auto_land skips a blocked-at-tip issue to
# escalate ONCE instead of re-attempting the same failure every tick (the merge→fail→reset→
# merge loop, #144). reconcile_markers clears the tag once the spoke commits fresh work on
# top (it falls behind the tip), so the issue becomes landable again after a real fix.
_blocked_at_tip() {
  local wt="$1" issue="$2" tip marker
  tip="$(git -C "$wt" rev-parse HEAD 2>/dev/null)" || return 1
  marker="$(git -C "$wt" rev-parse -q --verify "refs/tags/blocked/${issue}^{commit}" 2>/dev/null)"
  [ -n "$marker" ] && [ "$marker" = "$tip" ]
}

# _afk_review_verdict <wt> -> the verdict of the spoke's most-recent code-review artifact
# (APPROVE | REQUEST_CHANGES), or empty when no `.review/*.json` exists. Review evidence is
# written per reviewed diff as `.review/<hash>.json` by the review-stamp MCP; the LATEST by
# ISO-8601 `timestamp` wins, so a spoke that earned a REQUEST_CHANGES and then fixed it (a
# newer APPROVE) reads clean. Pure bash + grep (no jq dependency); ISO-8601 Z timestamps
# sort chronologically as plain strings.
# Same-second tie-break (#152): review-stamp's timestamp has 1-second resolution and a review
# can finish in <1s, so an APPROVE and a REQUEST_CHANGES can share the latest second. Such a
# tie resolves CONSERVATIVELY to REQUEST_CHANGES — the gate never lands on an ambiguous
# second — and the outcome does not depend on `.review/*.json` glob order.
# UPGRADE: this trusts an artifact's verdict field WITHOUT checking its HMAC signature (the
#   advisory reviewer-sep push gate is the authenticity layer today) and picks by timestamp,
#   not by binding to the pushed diff. Binding to the tip diff hash (utils.sh review_diff_hash
#   <wt> <base> range) AND verifying the signature would close both the "APPROVE then gut
#   before ready" ordering and the forge-an-APPROVE axis — once hub-afk can share utils.sh's
#   hash + verify recipe without its source-time side effects (set -e + per-hook span arm).
_afk_review_verdict() {
  local wt="$1"
  local dir="$wt/.review" f ts v latest="" verdict=""
  [ -d "$dir" ] || { printf '%s' ""; return 0; }
  for f in "$dir"/*.json; do
    [ -f "$f" ] || continue
    v="$(grep -oE '"verdict"[[:space:]]*:[[:space:]]*"[^"]*"' "$f" | head -1 | sed 's/.*: *"//;s/"$//')"
    [ -n "$v" ] || continue
    ts="$(grep -oE '"timestamp"[[:space:]]*:[[:space:]]*"[^"]*"' "$f" | head -1 | sed 's/.*: *"//;s/"$//')"
    ts="${ts:-0000}"   # a timestamp-less artifact sorts lowest — never wins over a stamped one
    if [ -z "$latest" ] || [[ "$ts" > "$latest" ]]; then
      latest="$ts"; verdict="$v"
    elif [ "$ts" = "$latest" ] && [ "$v" = "REQUEST_CHANGES" ]; then
      verdict="$v"   # conservative tie-break: a same-second REQUEST_CHANGES blocks the land
    fi
  done
  printf '%s' "$verdict"
}

# auto_land -> land every ready/<issue> spoke. The ready/<issue> marker is the readiness
# contract (enforced by _ready_at_tip above), so a foreign ready/<issue> left by a parallel
# session is adopted and landed by default (#95). A failed land (merge conflict) emits
# blocked/<issue> and the drain continues; a landed spoke frees its scope + its dependents'
# blockers for the next tick's plan. Set AFK_LAND_FOREIGN=0 to restore the dispatched-only
# isolation (skip any ready/<issue> with no dispatch epoch) so concurrent sessions don't
# surprise-land each other's work (#74).
#
# Trust the ready-marker green (#144): the ready/<issue> marker IS the green contract — the
# spoke's own ship gate already ran the full suite on this exact tree before emitting it (and
# _ready_at_tip proved marker == tip). So the land runs with --skip-tests: re-running the
# suite at land time is redundant AND self-flakes under a live drain, because a diverged land
# builds a merge commit whose gate re-runs the whole suite and the tripwire / worktree-land /
# test-select tests collide with the concurrent land moving refs (#140). Manual `/land` keeps
# its diverged-merge gate — the trust is applied by this caller, not baked into worktree-land.
# UPGRADE: if trusting a merge commit's untested combined tree ever proves too loose, swap
#   --skip-tests here for a fast merge-sanity check (pytest --collect-only + changed-file
#   tests) on diverged lands only — cheap, and it never runs the ref-colliding suites.
#
# Escalate ONCE, never loop (#144): a deterministic land failure escalates blocked/<issue> at
# the tip, but ready/<issue> still sits there too, so a naive re-survey would re-land → fail →
# reset → re-land forever (#140). auto_land skips any issue already carrying blocked/<issue>
# at its tip (_blocked_at_tip); reconcile_markers revives it once the spoke commits a real fix.
#
# The reasoning code-review verdict is the /afk test-gutting gate (#143), but it is OFF by
# default (#152): the #143 default-on gate false-positive-escalated clean lands whose spokes
# left no verdict artifact in the format the reader wants (reviews that finished in <1s),
# bricking the whole drain (#151). The mechanical anti-gutting scan stays the advisory
# residual signal. Set AFK_REVIEW_GATE=1 to opt back in: auto_land then lands ONLY on a clean
# APPROVE verdict — a REQUEST_CHANGES (the reviewer flagged gutting) or no review at all
# escalates to blocked/<issue> instead.
auto_land() {
  local wt_land path issue verdict
  wt_land="$(_afk_find_script "${WT_LAND:-}" worktree-land.sh)" || { log "worktree-land.sh not found — skipping land"; return 0; }
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    _ready_at_tip "$path" "$issue" || continue
    if _blocked_at_tip "$path" "$issue"; then
      log "  skip land #$issue — already escalated blocked/$issue at the tip (escalate once, no retry loop)"
      continue
    fi
    if [ "${AFK_LAND_FOREIGN:-1}" = "0" ] && [ -z "$(read_dispatch_epoch "$issue")" ]; then
      log "  skip land #$issue — foreign (no dispatch epoch) and AFK_LAND_FOREIGN=0"
      continue
    fi
    if [ "${AFK_REVIEW_GATE:-0}" != "0" ]; then
      verdict="$(_afk_review_verdict "$path")"
      if [ "$verdict" != "APPROVE" ]; then
        _escalate_blocked "$path" "$issue" \
          "code-review verdict not clean (${verdict:-no review}) — possible test-gutting, needs a human"
        continue
      fi
    fi
    log "→ land #$issue"
    if _afk_run_with_heartbeat bash "$wt_land" "$issue" --skip-tests >/dev/null 2>&1; then
      log "  landed #$issue"
      _afk_incr_landed   # tally for the drain-complete notification (#150)
    else
      _escalate_blocked "$path" "$issue" "auto-land failed (merge conflict or push rejection) — needs a human"
    fi
  done < <(inflight_worktrees)
}

# answer_pass -> auto-answer every waiting spoke. reap_pass -> reap every hung/overrun one.
answer_pass() {
  local path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "waiting" ] && decide_and_act "$path" "$issue"
  done < <(inflight_worktrees)
}
reap_pass() {
  local path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "reap" ] || continue
    _reap_or_resume "$path" "$issue"
  done < <(inflight_worktrees)
}

# --- marker reconciliation (issue #109, AC3) ----------------------------------
# Live state wins over a stale marker. When the reaper blocked a spoke whose pane had only
# crashed, the spoke could be auto-resumed and resume committing — leaving blocked/<issue>
# stranded BEHIND the tip, falsely flagging an actively-working spoke (the #103 coexistence).
# Each tick reconciles: a blocked/<issue> tag with fresh commits on top of it is cleared
# (local + best-effort remote) and its durable record dropped. A ready/<issue> behind the
# tip needs no action — slot_state and _ready_at_tip already require a marker AT the tip, so
# a behind-tip ready is already ignored (live state wins).

# _afk_clear_blocked_record <issue> -> drop one issue's durable local block record once its
# branch advances past the stale marker (paired with _afk_record_blocked_locally).
_afk_clear_blocked_record() { rm -f "$(_afk_blocked_record "$1")" 2>/dev/null || true; }

# _blocked_tag_is_stale <wt> <issue> -> true when a local blocked/<issue> tag exists and the
# tip has fresh commits ON TOP of it (the tag is an ancestor of, but not equal to, the tip):
# the spoke resumed and is committing, so the blocked marker no longer reflects live state.
_blocked_tag_is_stale() {
  local wt="$1" issue="$2" tag tip
  tag="$(git -C "$wt" rev-parse -q --verify "refs/tags/blocked/${issue}^{commit}" 2>/dev/null)" || return 1
  [ -n "$tag" ] || return 1
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  [ "$tag" != "$tip" ] || return 1
  git -C "$wt" merge-base --is-ancestor "$tag" "$tip" 2>/dev/null
}

# _clear_stale_blocked_marker <wt> <issue> -> delete a stale blocked/<issue> (local + remote)
# and drop its durable record. Best-effort; never aborts the loop.
_clear_stale_blocked_marker() {
  local wt="$1" issue="$2"
  log "→ reconcile #$issue: branch advanced past blocked/$issue — clearing the stale marker"
  git -C "$wt" tag -d "blocked/$issue" >/dev/null 2>&1 || true
  git -C "$wt" push origin ":refs/tags/blocked/$issue" >/dev/null 2>&1 || true
  _afk_clear_blocked_record "$issue"
  stamp_progress_epoch "$issue"   # the reconciled spoke is deliberately revived (#133)
}

# reconcile_markers -> clear every stale blocked/<issue> across the in-flight set so the
# dashboard, hub-status and the durable record reflect live state, not a marker the spoke
# has since moved past. Run first each tick: slot_state never reads a behind-tip marker as
# done anyway, so this is for the external view + durable record, not the passes' own reads.
reconcile_markers() {
  local path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    _blocked_tag_is_stale "$path" "$issue" && _clear_stale_blocked_marker "$path" "$issue"
  done < <(inflight_worktrees)
}

# --- the supervisor tick + stop condition -------------------------------------

# supervise_tick -> one full pass: reconcile stale markers, dispatch the next batch, answer
# parked spokes, land the ready ones, reap the hung ones. Each pass re-surveys the in-flight
# set, so a spoke that changed state earlier in the tick is seen fresh.
supervise_tick() {
  reconcile_markers
  dispatch_batch
  answer_pass
  # If the answer pass detected a dead subscription token, skip land + reap this tick:
  # both would shell out to a `claude`/suite that is just as dead. The main loop blocks
  # the in-flight spokes and stops.
  [ "$_AFK_AUTH_FAILED" -eq 1 ] && return 0
  auto_land
  reap_pass
}

# afk_done <state> <now> -> true when the supervisor should stop: the window was turned off
# (no state), a clock-bound window expired, or the backlog is drained (the planner returns
# an empty batch AND nothing is in flight).
afk_done() {
  local state="$1" now="$2" bp inflight_count batch
  [ -n "$state" ] || return 0
  window_expired "$state" "$now" && return 0
  inflight_count="$(inflight_issues | grep -c '^[0-9]' || true)"
  [ "$inflight_count" -eq 0 ] || return 1
  bp="$(_afk_find_script "${BATCH_PLAN:-}" batch-plan.sh)" || return 1
  batch="$(bash "$bp" 2>/dev/null || true)"
  [ -z "$(printf '%s' "$batch" | tr -d '[:space:]')" ]
}

# --- watchdog (auto-restart a crashed supervisor, issue #107) ------------------
# A silent supervisor crash (exit 0 mid-tick) leaves .afk-state armed with no process
# draining — an in-flight spoke runs on with no answerer. The watchdog is a thin outer
# keeper that, every AFK_WATCHDOG_SECONDS, respawns the supervisor whenever a window is
# armed (afk_supervisor_state == stale) but no live pid is stamping the heartbeat. The
# respawn is a NO-ARG resume: it reads the persisted .afk-state and re-adopts in-flight
# spokes idempotently (dispatch_batch skips already-in-flight issues), so a restart
# recovers orphans without re-dispatching or re-arming. Exactly one watchdog runs per
# checkout (a pidfile dedups), and it exits when --off clears the state.
: "${AFK_WATCHDOG_SECONDS:=60}"

# _afk_self -> the path the watchdog respawns. When running from an exec'd tmp copy
# (#133), AFK_ORIG_SCRIPT carries the checkout's real path: a respawn deliberately
# relaunches the ORIGINAL — picking up any newer code — and re-copies itself fresh.
_afk_self() { printf '%s\n' "${AFK_ORIG_SCRIPT:-${BASH_SOURCE[0]}}"; }

# _afk_exec_self_copy <argv...> -> re-exec THIS script from a private tmp COPY, so a
# hub sync/land rewriting hub-afk.sh mid-run cannot corrupt the running interpreter
# — bash lazily re-reads the script file past main() on the exit path, and the
# 2026-07-04 drain died there with `line 1465: unexpected token` (#133). Called at
# every loop-entering start (arm, no-arg resume, --watchdog); short-lived
# subcommands skip it. No-op when already running from a copy (AFK_RUNNING_COPY=1)
# or opted out (AFK_SELF_COPY=0); fail-OPEN on a copy failure (run from the
# original rather than refuse to arm). On success the exec never returns.
_afk_exec_self_copy() {
  [ "${AFK_SELF_COPY:-1}" = "0" ] && return 0
  [ "${AFK_RUNNING_COPY:-}" = "1" ] && return 0
  local src dir copy
  src="${BASH_SOURCE[0]}"
  dir="$(mktemp -d "${TMPDIR:-/tmp}/hub-afk-self.XXXXXX" 2>/dev/null)" || return 0
  copy="$dir/hub-afk.sh"
  cp "$src" "$copy" 2>/dev/null || return 0
  export AFK_RUNNING_COPY=1
  export AFK_ORIG_SCRIPT="$src"
  exec bash "$copy" "$@"
}

# _afk_resume_launch -> the shell command the watchdog runs to respawn a crashed
# supervisor: a detached, NO-ARG launch of this script. No window spec ⇒ it resumes the
# persisted window (re-adopting spokes) rather than arming a fresh one. Pure (returns the
# string) so it is inspectable in a test without launching a real supervisor.
# `env -u AFK_RUNNING_COPY` strips the exported recursion guard the running copy would
# otherwise pass down — the respawned supervisor must make its OWN fresh copy of the
# original, not run unprotected from the rewritable file (ST5 review).
_afk_resume_launch() { printf 'nohup env -u AFK_RUNNING_COPY bash %s >/dev/null 2>&1 &' "$(_afk_self)"; }

# _afk_watchdog_respawn -> respawn the supervisor. AFK_RESPAWN_CMD overrides the launch
# for tests; otherwise the no-arg resume above. Best-effort; never aborts the watchdog.
_afk_watchdog_respawn() {
  if [ -n "${AFK_RESPAWN_CMD:-}" ]; then bash -c "$AFK_RESPAWN_CMD"; return 0; fi
  bash -c "$(_afk_resume_launch)"
  return 0
}

# watchdog_tick -> one watchdog check, printing the observed supervisor state:
#   off       — no window armed; the watchdog should stop.
#   live      — a supervisor is alive and stamping the heartbeat; nothing to do.
#   respawned — the window is armed but the supervisor is gone; respawn it.
watchdog_tick() {
  case "$(afk_supervisor_state)" in
    off)  printf 'off\n' ;;
    live) printf 'live\n' ;;
    stale)
      log "/afk watchdog: supervisor gone but window still armed — respawning"
      _afk_watchdog_respawn
      printf 'respawned\n' ;;
  esac
}

# _afk_watchdog_file -> the watchdog pidfile (under the git common dir), recording the
# live watchdog's pid so only one runs. AFK_WATCHDOG_FILE overrides it for tests.
_afk_watchdog_file() {
  if [ -n "${AFK_WATCHDOG_FILE:-}" ]; then printf '%s\n' "$AFK_WATCHDOG_FILE"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/.afk-watchdog"
}
# _afk_watchdog_alive -> true when the recorded watchdog pid is a live process.
_afk_watchdog_alive() {
  local f pid; f="$(_afk_watchdog_file)"; [ -f "$f" ] || return 1
  pid="$(head -n1 "$f" 2>/dev/null | tr -d '[:space:]')"
  _afk_pid_alive "$pid"
}

# _afk_spawn_watchdog -> launch a background watchdog UNLESS one is already alive (so a
# re-arm, or a no-arg resume, never stacks keepers). AFK_WATCHDOG_SPAWN_CMD overrides the
# launch for tests. Best-effort; never aborts the caller.
_afk_spawn_watchdog() {
  _afk_watchdog_alive && return 0
  if [ -n "${AFK_WATCHDOG_SPAWN_CMD:-}" ]; then bash -c "$AFK_WATCHDOG_SPAWN_CMD"; return 0; fi
  # env -u strips the running copy's exported recursion guard: the watchdog is
  # long-lived and must exec its OWN fresh copy of the original (ST5 review).
  nohup env -u AFK_RUNNING_COPY bash "$(_afk_self)" --watchdog >/dev/null 2>&1 &
  # Record the child pid immediately so the next tick's dedup check sees it alive before
  # the watchdog itself writes the pidfile (closes the launch→pidfile startup race).
  printf '%s\n' "$!" > "$(_afk_watchdog_file)" 2>/dev/null || true
  return 0
}

# watchdog_loop -> the --watchdog keeper: record this pid, then each interval respawn the
# supervisor if it has crashed, until the window is turned off (--off). Cooperative: it
# exits on the first `off` it observes, so --off stops it within one watchdog interval.
watchdog_loop() {
  printf '%s\n' "$$" > "$(_afk_watchdog_file)" 2>/dev/null || true
  trap 'rm -f "$(_afk_watchdog_file)" 2>/dev/null || true' EXIT
  while :; do
    [ "$(watchdog_tick)" = "off" ] && { log "/afk watchdog: window off — exiting"; break; }
    sleep "$AFK_WATCHDOG_SECONDS"
  done
}

# --- remote launch (--remote) -------------------------------------------------
# Launch a detached, caffeinate-wrapped backlog drain on a configured always-on Mac over
# SSH (issue #73). The home Mac runs the drain unattended on the SAME Claude subscription
# (its spokes and answerers read ~/.claude); this is the cross-network trigger (a Tailscale
# hostname reachable from any network). Configured by env or a sourced conf file:
#   AFK_REMOTE_HOST      the always-on Mac's (Tailscale) hostname             [required]
#   AFK_REMOTE_REPO      the repo path on that host                           [required]
#   AFK_REMOTE_SESSION   the detached tmux session name               [default: afk]
#   AFK_REMOTE_DRAIN_CMD the command run under caffeinate on the host  [default: the
#                        supervisor script itself — see AFK_REMOTE_DEFAULT_DRAIN]
#   AFK_REMOTE_CONF      a shell snippet sourced for the above defaults [default: ~/.afk-remote]
#   AFK_SSH              the ssh binary (override for tests)           [default: ssh]
#
# The default launched command runs THIS supervisor script directly (hub-afk.sh drain) —
# NOT `claude "/afk drain"`. A bare `claude <prompt>` opens an interactive session and
# would stall unattended on a permission prompt before arming the supervisor; running the
# script is exactly what the /afk skill does locally, and it self-drives to backlog-empty.
# Override AFK_REMOTE_DRAIN_CMD (e.g. for a synced target's .ai-toolkit/ path) as needed.
AFK_REMOTE_DEFAULT_DRAIN="bash shared/skills/hub/scripts/hub-afk.sh drain"

# _load_remote_conf -> source the optional conf file for AFK_REMOTE_* defaults, with an
# explicit env value WINNING over the file (save env, source, restore the saved values).
_load_remote_conf() {
  local conf="${AFK_REMOTE_CONF:-$HOME/.afk-remote}"
  [ -f "$conf" ] || return 0
  local s_host="${AFK_REMOTE_HOST:-}" s_repo="${AFK_REMOTE_REPO:-}" \
        s_session="${AFK_REMOTE_SESSION:-}" s_drain="${AFK_REMOTE_DRAIN_CMD:-}"
  # shellcheck disable=SC1090
  . "$conf" 2>/dev/null || true
  [ -n "$s_host" ] && AFK_REMOTE_HOST="$s_host"
  [ -n "$s_repo" ] && AFK_REMOTE_REPO="$s_repo"
  [ -n "$s_session" ] && AFK_REMOTE_SESSION="$s_session"
  [ -n "$s_drain" ] && AFK_REMOTE_DRAIN_CMD="$s_drain"
  return 0
}

# build_remote_launch_cmd <repo> <session> <drain> -> the command run ON the remote host:
# cd into the repo and start a DETACHED tmux session that runs <drain> under `caffeinate -s`
# (keep the Mac awake for the whole drain). repo + session are single-quoted; <drain> is
# left unquoted so it can carry its own args/flags.
build_remote_launch_cmd() {
  local repo="$1" session="$2" drain="$3"
  printf "cd '%s' && tmux new -d -s '%s' 'caffeinate -s %s'\n" \
    "$repo" "$session" "$drain"
}

# remote_reattach_cmd <host> <session> -> the one-liner the user runs to attach to the
# unattended session (printed after a successful launch). -t forces the tty an attach needs.
remote_reattach_cmd() {
  printf "ssh %s -t 'tmux attach -t %s'\n" "$1" "$2"
}

# remote_launch -> resolve the remote config, SSH-launch the detached drain, CONFIRM the
# tmux session came up (so we never claim success on a silent failure), and print the
# reattach command. rc 2 on missing config, rc 1 on an ssh / confirmation failure.
remote_launch() {
  _load_remote_conf
  local host="${AFK_REMOTE_HOST:-}" repo="${AFK_REMOTE_REPO:-}" \
        session="${AFK_REMOTE_SESSION:-afk}" drain="${AFK_REMOTE_DRAIN_CMD:-$AFK_REMOTE_DEFAULT_DRAIN}" \
        ssh="${AFK_SSH:-ssh}" remote_cmd
  if [ -z "$host" ]; then
    log "/afk --remote: set AFK_REMOTE_HOST (the always-on Mac's Tailscale hostname) — see docs/remote-afk.md"
    return 2
  fi
  if [ -z "$repo" ]; then
    log "/afk --remote: set AFK_REMOTE_REPO (the repo path on $host) — see docs/remote-afk.md"
    return 2
  fi
  remote_cmd="$(build_remote_launch_cmd "$repo" "$session" "$drain")"
  log "→ launching unattended drain on $host (tmux session '$session')"
  # No -t here: tmux new -d detaches, so forcing a tty only triggers ssh's
  # "Pseudo-terminal will not be allocated" warning when the trigger has no tty (cron).
  if ! "$ssh" "$host" "$remote_cmd"; then
    log "/afk --remote: ssh launch failed — is $host reachable (Tailscale up)?"
    return 1
  fi
  if ! "$ssh" "$host" tmux has-session -t "$session" 2>/dev/null; then
    log "/afk --remote: launched but tmux session '$session' not found on $host — check the host"
    return 1
  fi
  log "✓ launched on $host — draining unattended until the backlog is empty"
  remote_reattach_cmd "$host" "$session"
  return 0
}

# --- telemetry preflight (issue #108) -----------------------------------------
# AFK's contract is that the dashboard is the single source of truth for an unattended
# run: a spoke spawned while the otelcol collector (:4317) / Langfuse bridge (:4319) are
# down, or LANGFUSE_BASIC_AUTH is unset, silently loses its telemetry — and #106's
# spoke-side wt_otel_*_preflight only WARNS and launches anyway, a per-spawn line that
# scrolls past with no human watching. So the SUPERVISOR runs ONE loud preflight before
# the first dispatch: resolve + export auth (so every spoke inherits working
# credentials), bring the collector and bridge up idempotently (reusing worktree-lib's
# launchers), and REFUSE TO ARM if any of the three can't be wired. AI_TOOLKIT_OTEL=0 is
# the sole opt-out — unset is treated as enabled, the SSOT-for-unattended default.
#
#   AFK_TELEMETRY_CONF   optional conf file sourced for LANGFUSE_BASIC_AUTH / LANGFUSE_HOST
#                        (env wins each field independently) [default: ~/.afk-telemetry]
#   AFK_PORT_WAIT_TRIES  re-probe attempts after a launch before declaring a port DOWN [10]
#   AFK_PORT_WAIT_SLEEP  seconds between those re-probes (a slow container start)        [1]

# afk_telemetry_enabled -> true unless AI_TOOLKIT_OTEL=0 (the sole opt-out; unset ⇒ on).
afk_telemetry_enabled() { [ "${AI_TOOLKIT_OTEL:-}" != "0" ]; }

# afk_resolve_telemetry_auth -> resolve LANGFUSE_BASIC_AUTH (env first, then the optional
# conf file — env wins, mirroring _load_remote_conf) and EXPORT it + LANGFUSE_HOST so
# every dispatched spoke inherits working credentials. Also exports AI_TOOLKIT_OTEL=1 so
# spokes opt in to native OTel. rc 1 when no auth can be resolved (caller refuses to arm).
afk_resolve_telemetry_auth() {
  local conf="${AFK_TELEMETRY_CONF:-$HOME/.afk-telemetry}"
  # Source the conf for BOTH fields (auth and host) with env winning each independently —
  # the same save-source-restore precedence as _load_remote_conf — so an operator can set
  # auth in the env and still pick host up from the file (and vice versa).
  if [ -f "$conf" ]; then
    local s_auth="${LANGFUSE_BASIC_AUTH:-}" s_host="${LANGFUSE_HOST:-}"
    # shellcheck disable=SC1090
    . "$conf" 2>/dev/null || true
    [ -n "$s_auth" ] && LANGFUSE_BASIC_AUTH="$s_auth"
    [ -n "$s_host" ] && LANGFUSE_HOST="$s_host"
  fi
  [ -n "${LANGFUSE_BASIC_AUTH:-}" ] || return 1
  export LANGFUSE_BASIC_AUTH
  export LANGFUSE_HOST="${LANGFUSE_HOST:-http://localhost:3000}"
  export AI_TOOLKIT_OTEL=1
}

# afk_ensure_port <port> <launch-fn> <repo_root> -> ensure something LISTENs on <port>:
# a no-op when already up; otherwise run <launch-fn> <repo_root> and re-probe up to
# AFK_PORT_WAIT_TRIES times (so a slow container start isn't a false DOWN). rc 1 when the
# port is still down after the launch — the caller turns that into a refuse-to-arm.
afk_ensure_port() {
  local port="$1" launch="$2" repo_root="$3" tries="${AFK_PORT_WAIT_TRIES:-10}" i=0
  wt_port_listening "$port" && return 0
  "$launch" "$repo_root"
  while [ "$i" -lt "$tries" ]; do
    wt_port_listening "$port" && return 0
    i=$((i + 1))
    sleep "${AFK_PORT_WAIT_SLEEP:-1}"
  done
  wt_port_listening "$port"
}

# afk_telemetry_preflight <repo_root> -> the one loud preflight the supervisor runs before
# the first dispatch. A no-op (rc 0) when telemetry is opted out. Otherwise resolve+export
# auth (refuse if none), then bring the collector up BEFORE the bridge (the collector forks
# to the bridge), refusing if either won't come up. rc 0 ⇒ wired; rc 1 ⇒ refuse to arm.
afk_telemetry_preflight() {
  local repo_root="$1"
  afk_telemetry_enabled || return 0
  if ! afk_resolve_telemetry_auth; then
    log "/afk: telemetry preflight FAILED — LANGFUSE_BASIC_AUTH unset and no conf to resolve it; refusing to arm (set it, or AI_TOOLKIT_OTEL=0 to run without telemetry)"
    return 1
  fi
  if ! afk_ensure_port 4317 wt_collector_launch "$repo_root"; then
    log "/afk: telemetry preflight FAILED — otelcol collector down on :4317 and won't come up; refusing to arm (set AI_TOOLKIT_OTEL=0 to run without telemetry)"
    return 1
  fi
  if ! afk_ensure_port 4319 wt_bridge_launch "$repo_root"; then
    log "/afk: telemetry preflight FAILED — Langfuse bridge down on :4319 and won't come up; refusing to arm (set AI_TOOLKIT_OTEL=0 to run without telemetry)"
    return 1
  fi
  log "/afk: telemetry preflight OK — collector :4317, bridge :4319, auth present"
  return 0
}

# afk_have_telemetry_auth -> true when LANGFUSE_BASIC_AUTH is resolvable (env, or a
# non-empty value from the conf file) WITHOUT mutating the caller's environment — the
# READ-ONLY check --status uses. It resolves the conf in a SUBSHELL so a status read has
# no side effect on the parent env, yet agrees EXACTLY with the preflight's
# afk_resolve_telemetry_auth (which sources + requires a non-empty value) — a commented-out
# or empty assignment reports missing in both, so --status never claims OK for a conf the
# supervisor would have refused to arm on.
afk_have_telemetry_auth() {
  [ -n "${LANGFUSE_BASIC_AUTH:-}" ] && return 0
  local conf="${AFK_TELEMETRY_CONF:-$HOME/.afk-telemetry}"
  [ -f "$conf" ] || return 1
  # shellcheck disable=SC1090
  ( . "$conf" 2>/dev/null; [ -n "${LANGFUSE_BASIC_AUTH:-}" ] )
}

# afk_telemetry_status -> a one-line, READ-ONLY telemetry health summary for --status: the
# up/down of the collector (:4317) and bridge (:4319) and whether auth is resolvable, e.g.
#   /afk: telemetry OK (collector up, bridge up, auth present)
#   /afk: telemetry DOWN (collector down, bridge up, auth missing)
# Prints nothing when telemetry is opted out (AI_TOOLKIT_OTEL=0). Probes only — never
# launches anything (unlike the preflight), so a status read is free of side effects.
afk_telemetry_status() {
  afk_telemetry_enabled || return 0
  local c b a overall
  wt_port_listening 4317 && c=up || c=down
  wt_port_listening 4319 && b=up || b=down
  afk_have_telemetry_auth && a=present || a=missing
  if [ "$c" = up ] && [ "$b" = up ] && [ "$a" = present ]; then overall=OK; else overall=DOWN; fi
  printf '/afk: telemetry %s (collector %s, bridge %s, auth %s)\n' "$overall" "$c" "$b" "$a"
}

# --- CLI ----------------------------------------------------------------------

# _afk_status_state_line <state> <now> -> echo the window's state line: STALE (#107) when
# the supervisor pid is gone, else draining / window-elapsed / "Nm remaining".
_afk_status_state_line() {
  local state="$1" now="$2" rem age
  # Cross-check the heartbeat before trusting the state file: a window armed in
  # .afk-state but no live supervisor pid means the process crashed and the state file
  # is lying (#107). Report STALE rather than echoing `draining` / `Nm remaining`.
  if [ "$(afk_supervisor_state)" = "stale" ]; then
    age="$(_afk_heartbeat_age_minutes)"
    if [ -n "$age" ]; then
      echo "/afk: STALE — last tick ${age}m ago, supervisor process not found (run /afk --off to clear, or the watchdog will respawn it)"
    else
      echo "/afk: STALE — no heartbeat, supervisor process not found (run /afk --off to clear, or the watchdog will respawn it)"
    fi
    return 0
  fi
  if [ "$state" = "drain" ]; then echo "/afk: draining (no clock bound — stops when the backlog is empty)"; return 0; fi
  if window_expired "$state" "$now"; then echo "/afk: window elapsed (supervisor will stop on its next tick)"; return 0; fi
  rem="$(minutes_remaining "$state" "$now")"
  echo "/afk: on — ${rem}m remaining (until $(wt_date_ymd "$state") $(date -r "$state" +%H:%M 2>/dev/null || date -d "@$state" +%H:%M))"
}

# afk_blocked_locally_status -> a one-line summary of issues with a DURABLE local block
# record (escalation could not push the tag, #109), or nothing when there are none. The
# operator returning from AFK reads --status, so a block that never reached the dashboard
# must still surface here — never silently dropped.
afk_blocked_locally_status() {
  local dir f issue list=""
  dir="$(_afk_state_dir)"
  [ -d "$dir" ] || return 0
  for f in "$dir"/blocked-*.txt; do
    [ -e "$f" ] || continue
    issue="${f##*/blocked-}"; issue="${issue%.txt}"
    list="${list:+$list, }#$issue"
  done
  [ -n "$list" ] || return 0
  printf '/afk: locally blocked (escalation could not push the tag — needs a human): %s [%s]\n' \
    "$list" "$dir"
}

_status() {
  local state now
  state="$(afk_read_state)"; now="$(afk_now)"
  if [ -z "$state" ]; then
    echo "/afk: off"
    # A durable escalation outlives the drain — surface it even when off, so the operator
    # returning from AFK sees a block that never reached the dashboard (#109).
    afk_blocked_locally_status
    return 0
  fi
  _afk_status_state_line "$state" "$now"
  # For a live (or stale) drain, surface telemetry health too: the dashboard is the SSOT,
  # so the operator must be able to see whether it's actually receiving data (#108). A
  # no-op line when telemetry is opted out (AI_TOOLKIT_OTEL=0).
  afk_telemetry_status
  afk_blocked_locally_status
}

main() {
  # --remote triggers a drain on a DIFFERENT machine and operates only on the remote repo,
  # so it needs no local checkout — handle it before the git-repo guard.
  if [ "${1:-}" = "--remote" ]; then remote_launch; return $?; fi

  MAIN_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { log "not inside a git repository"; return 1; }

  # Long-running entries (arm, no-arg resume, --watchdog) re-exec from a private tmp
  # copy so a mid-run rewrite of this file cannot corrupt the interpreter (#133).
  # Short-lived subcommands (--status/--off/--once/help) run in place, and a SOURCED
  # main (the test harness drives it with stubbed shell functions) never execs — an
  # exec would silently drop every stub defined in the sourcing shell.
  case "${1:-}" in
    --status | --off | --once | -h | --help) ;;
    *) [[ "${BASH_SOURCE[0]}" == "${0}" ]] && _afk_exec_self_copy "$@" ;;
  esac

  # Subcommands that do not start the LOCAL supervisor loop.
  case "${1:-}" in
    --status)   _status; return 0 ;;
    --off)      afk_clear_state; echo "/afk: off (state cleared; the supervisor + watchdog stop on their next tick)"; return 0 ;;
    --watchdog) watchdog_loop; return $? ;;
    -h|--help)  sed -n '2,57p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
  esac

  local once=0
  if [ "${1:-}" = "--once" ]; then
    once=1
  elif [ -n "${1:-}" ]; then
    # A window spec: compute + persist the end bound before the first tick.
    local end
    end="$(compute_end_epoch "$@" "$(afk_now)")" || { log "unrecognized window: '$*' (use <duration>, 'until HH:MM', or 'drain')"; return 2; }
    # Telemetry preflight BEFORE arming: an unattended drain must not dispatch spokes into
    # a dead telemetry pipeline (the dashboard is the SSOT). Refuse to arm — write no state,
    # never reach the loop — when collector/bridge/auth can't be wired (#108).
    afk_telemetry_preflight "$MAIN_ROOT" || return 2
    afk_write_state "$end"
    _clear_dispatch_epochs   # fresh window ⇒ empty "dispatched by this run" set
    _clear_progress_state    # fresh window ⇒ no stale progress / answer-attempt epochs
    _clear_resume_markers    # fresh window ⇒ every spoke gets its one auto-resume again
    _afk_clear_landed_count  # fresh window ⇒ the landed tally starts at zero (#150)
    _afk_clear_drain_complete # ...and drop any un-consumed completion signal from a prior drain
    _clear_blocked_records   # fresh window ⇒ --status shows only THIS run's durable blocks
    log "/afk: armed ($([ "$end" = drain ] && echo 'drain — until the backlog is empty' || echo "until $(wt_date_ymd "$end") $(date -r "$end" +%H:%M 2>/dev/null || date -d "@$end" +%H:%M)"))"
  fi

  while :; do
    afk_write_heartbeat   # stamp this tick before working, so a crash mid-tick is visible
    # Keep exactly one watchdog alive (idempotent: a no-op while one runs, respawns it if
    # it died). Doing this each tick — not just at arm — means the supervisor and watchdog
    # heal each other: neither is a single silent point of failure (#107). Skipped for
    # --once (a one-shot cron tick must not leave a background keeper behind).
    [ "$once" -eq 0 ] && _afk_spawn_watchdog
    supervise_tick
    if [ "$_AFK_AUTH_FAILED" -eq 1 ]; then
      log "/afk: subscription auth failed — blocking in-flight spokes and stopping (re-run /login on the host)"
      _block_all_inflight "subscription auth failed — token could not refresh; re-run /login on the host"
      afk_clear_state; break
    fi
    [ "$once" -eq 1 ] && break
    if afk_done "$(afk_read_state)" "$(afk_now)"; then
      log "/afk: done"; _afk_emit_drain_complete; afk_clear_state; break
    fi
    sleep "$AFK_TICK_SECONDS"
  done
  return 0
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
