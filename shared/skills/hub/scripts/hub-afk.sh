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
#   AFK_TICK_SECONDS=300         supervisor poll interval — the BACKSTOP tick (#176). Parked
#                                spokes announce (spoke-ready.sh / the Notification hook) and
#                                SIGUSR1 the supervisor, so an answer no longer waits a full
#                                tick; the tick stays authoritative for everything silence-
#                                shaped (reap, resume, reconcile, dispatch, drain-done).
#   AFK_WATCHDOG_SECONDS=60      watchdog poll interval (respawn a crashed supervisor)
#   AFK_SPOKE_MAX_MINUTES=180    wall-clock ceiling per spoke before a reap
#   AFK_IDLE_MINUTES=30          a spoke idle this long with no marker AND not waiting → reap
#   AFK_ANSWERER_CMD             the answerer command (default: claude -p --model claude-opus-4-8)
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
#   AFK_PLANNER_TIMEOUT=120      seconds bounding each batch-plan.sh call (#170)
#   AFK_GH_TIMEOUT=30            seconds bounding each gh call (scope resolve, arm auth) (#170)
#   AFK_TIMEOUT_KILL_AFTER=10    SIGKILL grace after SIGTERM when a bounded call expires (#170)
#   AFK_PHASE_MAX_SECONDS=900    cap on one phase's heartbeat stamping — a land/answer that
#                                runs past this is HUNG, so stamping stops and the epoch ages
#                                so the watchdog respawns the wedged tree (#202 B); 0 disables
#   AFK_LAND_RETRY_MAX=1         retries of a stranded ready+blocked land before a visible
#                                escalation, per issue per window (#202 D)
#   AFK_STALE_TICKS=4            heartbeat age (x AFK_TICK_SECONDS) that flags a wedged supervisor
#                                — scaled to the 300s tick so the wedge threshold stays ~20min
#                                (4x300s), not the ~50min a 120s-era default of 10 would stretch to
#   AFK_DISPATCH_MAX_FAILURES=3  consecutive worktree-new.sh failures before an issue is blocked
#   AFK_ARM_PRECHECK=1           arm-precondition gate (=0 skips live/dirty/branch/gh-auth checks)
#   AFK_AUTH_PROBE_CMD           reap-time auth probe (default: a bounded headless claude no-op)
#   CLAUDE_PROJECTS_DIR          transcript root (default: $HOME/.claude/projects)
#   AFK_REMOTE_HOST / AFK_REMOTE_REPO / AFK_REMOTE_SESSION / AFK_REMOTE_DRAIN_CMD
#                                --remote target config (or a sourced AFK_REMOTE_CONF file)
#
# Usage:
#   hub-afk.sh <duration>        # e.g. 90, 30m, 1h, 1h30m — drain for that long, then stop
#   hub-afk.sh until <HH:MM>     # drain until the next HH:MM, then stop
#   hub-afk.sh drain             # drain until the backlog is empty + nothing in flight
#   hub-afk.sh --remote          # launch a detached `drain` on a configured always-on Mac
#   hub-afk.sh --status          # report the window: off / draining-idle / STALLED / DRAIN DEAD
#   hub-afk.sh --off             # stop the supervisor + watchdog (clears the state file)
#   hub-afk.sh --reconcile       # re-arm an armed-but-crashed drain (idempotent resume);
#                                #   run at hub session start after a process/machine restart
#   hub-afk.sh --once            # run a single tick and exit (tests / external cron)
#   hub-afk.sh --watchdog        # the keeper loop: respawn the supervisor if it crashes
#                                #   (auto-spawned on arm; rarely run by hand)
#
# Run it on the hub (main checkout, on the default branch). Read-only against the work
# except for dispatching, answering, landing, and reaping spokes. --remote runs the drain
# on a different machine instead (see docs/remote-afk.md).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${AFK_TICK_SECONDS:=300}"
: "${AFK_SPOKE_MAX_MINUTES:=180}"
: "${AFK_IDLE_MINUTES:=30}"
: "${AFK_ANSWERER_EFFORT:=high}"
# Timeouts on the tick's external calls (#170 ST1): a wedged planner / gh must never
# freeze the whole supervisor — a bounded call that times out logs and means "retry next
# tick", never a trusted empty result.
: "${AFK_PLANNER_TIMEOUT:=120}"
: "${AFK_GH_TIMEOUT:=30}"

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

# --- bounded external calls (issue #170 ST1) ----------------------------------
# One wedged external call (a hung `batch-plan.sh`, a stuck `gh`) used to freeze the whole
# supervisor forever behind a live pid. Every external call the tick makes is now run under
# a REAL time bound on both platforms: the coreutils `timeout`/`gtimeout` when installed,
# and a portable bash fallback otherwise (the default macOS hub ships neither). Callers
# treat any nonzero exit (a timeout or a real failure) as "retry next tick".
# AFK_TIMEOUT_KILL_AFTER (default 10) is the SIGKILL grace after the SIGTERM on expiry.
: "${AFK_TIMEOUT_KILL_AFTER:=10}"

# _afk_timeout_bin -> the installed timeout binary (timeout | gtimeout), or empty.
_afk_timeout_bin() {
  if command -v timeout >/dev/null 2>&1; then printf 'timeout\n'
  elif command -v gtimeout >/dev/null 2>&1; then printf 'gtimeout\n'
  fi
}

# _afk_kill_tree <pid> <signal> -> signal <pid> and all its descendants leaf-first, so a
# wrapped command's grandchildren die with it: killing only the direct child (e.g. the
# `bash batch-plan.sh` whose real work is `gh api … | python`) would orphan a grandchild
# that keeps the output pipe open and hangs the whole substitution. `pgrep -P` matches by
# numeric parent pid (never argv), so the repo's non-ASCII `pgrep -f` hazard doesn't apply;
# wt_pgrep (issue #189) forces LC_ALL=C as belt-and-suspenders and excludes this
# supervisor's own pid, so a kill-tree walk can never turn on itself.
_afk_kill_tree() {
  local pid="$1" sig="$2" child
  for child in $(wt_pgrep -P "$pid"); do _afk_kill_tree "$child" "$sig"; done
  kill "-$sig" "$pid" 2>/dev/null || true
}

# _afk_descendant_pids <pid> -> every descendant pid of <pid> (recursively, one per line),
# collected via `pgrep -P` (numeric parent, ASCII-safe). Snapshotted BEFORE a kill so a
# SIGKILL escalation can still reach a TERM-ignoring child after the parent — and thus the
# `pgrep -P` chain — is gone (#202 E review). Does not include <pid> itself. Goes through
# wt_pgrep (issue #189) so every process probe shares the one LC_ALL=C, self-excluding path.
_afk_descendant_pids() {
  local pid="$1" child
  for child in $(wt_pgrep -P "$pid"); do
    printf '%s\n' "$child"
    _afk_descendant_pids "$child"
  done
}

# _afk_with_timeout <seconds> <cmd...> -> run <cmd...> bounded to <seconds>. The timeout
# binary (with a `-k` SIGKILL grace) when installed; else a portable fallback: background
# the command, a killer tree-kills it (TERM then KILL) on expiry, and we wait for it — so
# the bound is real even where coreutils is absent. Returns the command's exit code.
_afk_with_timeout() {
  local secs="$1"; shift
  local tb grace="${AFK_TIMEOUT_KILL_AFTER:-10}"
  case "$grace" in '' | *[!0-9]*) grace=10 ;; esac
  tb="$(_afk_timeout_bin)"
  if [ -n "$tb" ]; then "$tb" -k "$grace" "$secs" "$@"; return $?; fi
  local cmd_pid killer rc
  "$@" &
  cmd_pid=$!
  ( sleep "$secs"; _afk_kill_tree "$cmd_pid" TERM; sleep "$grace"; _afk_kill_tree "$cmd_pid" KILL ) \
    </dev/null >/dev/null 2>&1 &
  killer=$!
  wait "$cmd_pid" 2>/dev/null; rc=$?
  _afk_kill_tree "$killer" TERM   # the command finished first — cancel the pending killer
  wait "$killer" 2>/dev/null
  return "$rc"
}

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

# _afk_atomic_write <file> <content> -> write <content>\n to <file> atomically: a temp file in
# the SAME directory (so the rename is a same-filesystem atomic swap) then `mv` over the
# target. A reader — the watchdog, a second --status shell, a respawn racing a live supervisor
# — then always observes a COMPLETE old-or-new file, never a half-written truncation (#202 G).
# The temp name carries $$ so two concurrent writers don't clobber each other's temp; the last
# rename wins atomically. Best-effort: a failure cleans up the temp and returns nonzero, so the
# caller's `|| true` posture (a write must never abort a tick) is preserved.
_afk_atomic_write() {
  local file="$1" content="$2" tmp="$1.tmp.$$"
  if printf '%s\n' "$content" > "$tmp" 2>/dev/null && mv -f "$tmp" "$file" 2>/dev/null; then
    return 0
  fi
  rm -f "$tmp" 2>/dev/null || true
  return 1
}

afk_write_state() { _afk_atomic_write "$(afk_state_file)" "$1" || true; }
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
  _afk_atomic_write "$(afk_landed_count_file)" "$(( $(afk_read_landed_count) + 1 ))" || true
}
_afk_clear_landed_count() { rm -f "$(afk_landed_count_file)" 2>/dev/null || true; }
_afk_clear_drain_complete() { rm -f "$(afk_drain_complete_file)" 2>/dev/null || true; }
# _afk_emit_drain_complete -> hand the final tally to hub-notify at drain-stop:
# write the count to .afk-drain-complete, then reset the counter so the next
# window starts fresh. Best-effort; a write failure never aborts the stop path.
_afk_emit_drain_complete() {
  _afk_atomic_write "$(afk_drain_complete_file)" "$(afk_read_landed_count)" || true
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
# afk_write_heartbeat_pid <pid> -> stamp "<pid> <now>[ wake1]". A backgrounded stamper subshell
# must record the SUPERVISOR's pid (its parent), not its own (#202 B), so the pid stays the truth
# afk_supervisor_state cross-checks; afk_write_heartbeat is the common "stamp my own pid" case.
# The optional trailing wake-capability token (#207) is appended only when this supervisor has
# armed its USR1 trap (_AFK_WAKE_TOKEN set below), so afk-notify-wake never SIGUSR1s — and thus
# never KILLs — a pre-#176 supervisor whose default SIGUSR1 action is terminate.
afk_write_heartbeat_pid() {
  _afk_atomic_write "$(afk_heartbeat_file)" "$1 $(afk_now)${_AFK_WAKE_TOKEN:+ $_AFK_WAKE_TOKEN}" || true
}
afk_write_heartbeat() { afk_write_heartbeat_pid "$$"; }
afk_read_heartbeat()  { local f; f="$(afk_heartbeat_file)"; [ -f "$f" ] && head -n1 "$f" 2>/dev/null || true; }
afk_clear_heartbeat() { rm -f "$(afk_heartbeat_file)" 2>/dev/null || true; }
# afk_heartbeat_epoch <heartbeat-line> -> the 2nd field (last-tick epoch) of a "<pid> <epoch>
# [wake1]" heartbeat. Extract field 2 explicitly — NOT the last field: `${hb##* }` returns the
# `wake1` token on a three-field line (#207) and would strand every staleness/age check.
afk_heartbeat_epoch() { local rest="${1#* }"; printf '%s\n' "${rest%% *}"; }

# --- last-action record (issue #202 B) ----------------------------------------
# A one-line label of the supervisor's most recent MEANINGFUL action (dispatch/answer/land/
# reap/resume #N), stamped at each pass boundary and surfaced by --status as "(last action …)"
# so an operator can tell idle-but-healthy from wedged at a glance without a process-tree
# autopsy. Best-effort; never aborts a tick. Cleared on a fresh arm.
_afk_last_action_file() {
  if [ -n "${AFK_LAST_ACTION:-}" ]; then printf '%s\n' "$AFK_LAST_ACTION"; return; fi
  printf '%s\n' "$(_afk_state_dir)/last-action"
}
_afk_set_last_action() {
  local dir; dir="$(_afk_state_dir)"; mkdir -p "$dir" 2>/dev/null || true
  _afk_atomic_write "$(_afk_last_action_file)" "$1" || true
}
_afk_read_last_action() { local f; f="$(_afk_last_action_file)"; [ -f "$f" ] && head -n1 "$f" 2>/dev/null || true; }
_afk_clear_last_action() { rm -f "$(_afk_last_action_file)" 2>/dev/null || true; }

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
  tick="$(afk_heartbeat_epoch "$hb")"
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
# _afk_escalate_blocked <wt> <issue> <reason> — the ONE supervisor escalation path
# (issue #236). It runs the gate-broker core _escalate_blocked (marker emit + durable
# local-record fallback + deny span) AND mirrors the transition onto the GitHub issue's
# status label. The supervisor is the single WRITER of a supervisor-driven blocked
# transition: it escalates when a spoke is reaped or a land fails, by which point the
# spoke may be torn down, so the hub-notify watch loop can't be relied on to flip the
# label. Best-effort throughout (wt_gh_set_status_label self-gates on disabled / gh-absent
# / a numeric issue), so a failed gh never fails a tick.
_afk_escalate_blocked() {
  _escalate_blocked "$1" "$2" "$3"
  case "$2" in
    '' | *[!0-9]*) ;;  # non-numeric (ad-hoc) issue — no GitHub issue to mirror onto
    *) command -v wt_gh_set_status_label >/dev/null 2>&1 \
         && wt_gh_set_status_label "$2" "status:blocked" || true ;;
  esac
}

# reap_spoke (kill window + escalate blocked/<issue>) is retired by #241: the reaper never
# abandons a spoke — see _warn_parked_last / _afk_revive_or_park_last, which revive-first and
# warn-and-park-LAST instead. (_afk_escalate_blocked remains for the auth halt + auto_land.)

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

# _afk_pushed_but_unmarked <wt> <issue> -> true when the spoke did its work, PUSHED it, and is
# clean — but carries NO completion/park marker at the tip (issue #200's two-phase gap: the
# branch push landed but the ready/<issue> emission failed, leaving origin ahead with no
# signal). Requires HEAD == @{upstream} (fully pushed), a clean tree, a commit above the base,
# and no ready/accept/blocked/gate tag at the tip. Used to give the reaper an ACCURATE,
# actionable reason (re-run the marker / land by hand) instead of the misleading "likely hung".
# It deliberately does NOT auto-emit ready — a clean-pushed-no-marker tip is also the shape of
# a spoke idle BETWEEN subtasks, so auto-completing it could land incomplete work; a crashed
# such spoke is safely revived by recover_dead_panes (resume re-emits the mark after verifying).
_afk_pushed_but_unmarked() {
  local wt="$1" issue="$2" head up kind
  head="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  up="$(git -C "$wt" rev-parse -q --verify '@{upstream}' 2>/dev/null)" || return 1
  [ "$head" = "$up" ] || return 1
  [ -z "$(git -C "$wt" status --porcelain 2>/dev/null)" ] || return 1
  _spoke_has_commits "$wt" || return 1
  for kind in ready accept blocked gate; do
    [ "$(git -C "$wt" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)" = "$head" ] && return 1
  done
  return 0
}

# _spoke_has_work <wt> -> true when the worktree holds anything worth preserving on a crash:
# a commit above the branch point (_spoke_has_commits) OR a dirty tree (uncommitted WIP). The
# dead-pane recovery pass (issue #202 C) revives a crashed pane that has_work and re-dispatches
# one that does not — so an in-progress-but-uncommitted spoke is never torn down.
_spoke_has_work() {
  local wt="$1"
  _spoke_has_commits "$wt" && return 0
  [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]
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

# the once-per-window re-dispatch stamp (issue #202 C): a clean crashed worktree is torn
# down and re-dispatched at most ONCE per armed window (a second clean crash escalates to a
# human, so a persistently-crashing infra dep can't loop redispatch→crash forever). Cleared
# on a fresh arm alongside the resume markers.
_afk_redispatched_marker()  { printf '%s\n' "$(_afk_state_dir)/redispatched-$1"; }
_afk_already_redispatched() { [ -f "$(_afk_redispatched_marker "$1")" ]; }
_afk_mark_redispatched() {
  local m; m="$(_afk_redispatched_marker "$1")"
  mkdir -p "$(dirname "$m")" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$m" 2>/dev/null || true
}
_clear_redispatch_markers() { rm -f "$(_afk_state_dir)"/redispatched-* 2>/dev/null || true; }

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
  _afk_set_last_action "resume #$issue"
  if ! _afk_open_spoke_window "$wt" "$issue" "$(_afk_resume_command "$wt" "$issue")"; then
    log "  could not open a resume window for #$issue"
    return 1
  fi
  _afk_mark_resumed "$issue"
  stamp_progress_epoch "$issue"   # a deliberate revival resets the reap ceiling (#133)
  # Reset the IDLE clock too (#202 C review): recover_dead_panes resumes then reap_pass runs
  # in the SAME tick, and _spoke_idle_seconds measures the STALE transcript mtime (the fresh
  # window has not written yet) — not the progress epoch — so a resumed idle-crashed spoke
  # would be re-reaped as "live pane, likely hung" and its just-restored work blocked. The
  # answer-attempt epoch is the idle clock's exclusion, so stamping it reads the revived spoke
  # busy until its new session writes a transcript.
  stamp_answer_attempt "$issue"
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

# --- #241 §7/§8: revive-first, warned-parked-LAST, never abandon -----------------
# The reaper no longer kills a stuck spoke into blocked/<issue>. Every former reap TAKES a
# revival first (kill any hung/crashed pane + relaunch `claude --continue`); only a spoke whose
# revival was ALREADY tried this window downgrades to warned-and-parked-LAST (warn + journal +
# arm the warned-retry backoff, retried at low frequency), NEVER killed or abandoned.

# _warn_parked_last <wt> <issue> <reason> [park_kind=reap] -> the never-abandon replacement for
# reap_spoke: keep the spoke in rotation on the warned-retry backoff. NO window kill, NO
# blocked/<issue>. It HONORS the backoff — it warns + journals only when the spoke is DUE, and
# parks LAST SILENTLY inside the backoff window — so a permanently-stuck spoke is retried (and
# re-warned) at LOW frequency, not warned + gh-commented every 5-minute tick. reversible: the
# spoke's committed work is intact.
_warn_parked_last() {
  local wt="$1" issue="$2" reason="$3" park="${4:-reap}"
  _afk_warned_due "$issue" || return 0   # inside the backoff → parked LAST silently this tick
  log "→ warn-park-LAST #$issue: $reason"
  _afk_set_last_action "warn-park #$issue"
  broker_warn_continue "$wt" "$issue" "$park" "$reason" reversible
}

# _revive_spoke <wt> <issue> -> kill any hung/crashed window and relaunch the spoke via
# `claude --continue` under the same spoke_run_id, resetting the reap + idle clocks (#133/#202
# C: the fresh window hasn't written a transcript yet, so stamp the answer-attempt epoch or the
# same-tick reap_pass re-reaps it as idle). Marks the once-per-window revival. rc 1 when the
# window could not be opened (the caller warns + retries next tick).
_revive_spoke() {
  local wt="$1" issue="$2"
  log "→ revive #$issue: killing any hung/crashed pane and relaunching (claude --continue)"
  _afk_set_last_action "revive #$issue"
  _kill_spoke_window "$issue"
  if ! _afk_open_spoke_window "$wt" "$issue" "$(_afk_resume_command "$wt" "$issue")"; then
    log "  could not open a revive window for #$issue"
    return 1
  fi
  _afk_mark_resumed "$issue"
  stamp_progress_epoch "$issue"
  stamp_answer_attempt "$issue"
  # #241 §10: a revival is a taken decision the morning review sees — journal it (a successful
  # revival is not a loud warned record, just an auditable journal line + span).
  broker_journal_decision "$issue" revive "revived a hung/crashed pane (killed + relaunched claude --continue)" reversible
  _afk_emit_span "$wt" afk-revive success
  return 0
}

# _afk_revive_or_park_last <wt> <issue> <reason> -> revive-first, then warned-parked-LAST. If a
# revival was already tried this window (_afk_already_resumed) OR the relaunch cannot start, the
# spoke is warned-and-parked-LAST rather than reaped — retried at low frequency, never abandoned.
_afk_revive_or_park_last() {
  local wt="$1" issue="$2" reason="$3"
  if _afk_already_resumed "$issue"; then
    _warn_parked_last "$wt" "$issue" "$reason — revival already tried this window; parked LAST, retried at low frequency"
    return 0
  fi
  _revive_spoke "$wt" "$issue" \
    || _warn_parked_last "$wt" "$issue" "$reason — revival launch could not be started; retrying"
}

# _afk_warn_pushed_but_unmarked <wt> <issue> -> #200/#241: a clean-pushed tip with no completion
# marker is warned-and-parked-LAST with an ACTIONABLE reason, NOT auto-marked. The shape is
# AMBIGUOUS — genuinely finished (the marker just failed) vs idle BETWEEN subtasks (more work to
# come, cf. _afk_pushed_but_unmarked's own caution) — so auto-emitting ready/<issue> could
# auto-LAND incomplete work onto main (hard to reverse). Never abandoned: the loud warning
# surfaces it for the human to re-run --ready or land by hand.
_afk_warn_pushed_but_unmarked() {
  local wt="$1" issue="$2"
  _warn_parked_last "$wt" "$issue" \
    "pushed-but-unmarked (#200): clean tip, no ready/$issue marker — if finished, re-run 'spoke-push.sh --ready $issue' or land by hand" \
    markready
}

# _reap_or_resume <wt> <issue> -> #241 §7/§8: revive-first, never block. A finished-but-unmarked
# spoke (#200) is auto-marked. Every other stuck spoke — over-ceiling runaway, hung LIVE pane
# (a frozen claude is a revival case, not a block), or crashed pane — is revived; a spoke whose
# revival was already tried this window is warned-and-parked-LAST, never reaped/abandoned.
_reap_or_resume() {
  local wt="$1" issue="$2"
  # #200/#241: a live pane at a clean-pushed tip with no marker is warned-and-parked-LAST with an
  # actionable reason (NOT auto-marked/auto-landed — the shape is ambiguous with idle-between-subtasks).
  if _spoke_pane_alive "$wt" && _afk_pushed_but_unmarked "$wt" "$issue"; then
    _afk_warn_pushed_but_unmarked "$wt" "$issue"
    return 0
  fi
  if _spoke_over_any_ceiling "$issue" "$(afk_now)"; then
    _afk_revive_or_park_last "$wt" "$issue" "time ceiling: ran >${AFK_SPOKE_MAX_MINUTES}m without finishing"
  elif _spoke_pane_alive "$wt"; then
    # #241 §8: a live-but-frozen claude is a REVIVAL case (kill the hung pane + relaunch), not a
    # terminal block. answer attempts must not reset the reap clock, so this is a revival, not a re-answer.
    _afk_revive_or_park_last "$wt" "$issue" "went idle >${AFK_IDLE_MINUTES}m with a live pane and no marker — likely hung"
  elif ! _spoke_has_commits "$wt"; then
    _afk_revive_or_park_last "$wt" "$issue" "pane crashed with no committed work to preserve"
  elif _afk_already_resumed "$issue"; then
    _warn_parked_last "$wt" "$issue" "pane crashed again after an auto-resume — parked LAST, retried at low frequency"
  else
    resume_spoke "$wt" "$issue" \
      || _warn_parked_last "$wt" "$issue" "pane crashed and the auto-resume could not be launched — retrying"
  fi
}

# _warn_all_inflight <reason> -> WARN every in-flight spoke not already at a terminal marker
# (#241 §9). Called while the drain is halted on dead auth: an auth failure is NOT the spoke's
# fault, so it is warned (loud, re-fired by hub-notify), NEVER blocked — the drain resumes
# servicing it once auth recovers. Replaces the pre-#241 _block_all_inflight (which parked them).
_warn_all_inflight() {
  local reason="$1" path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "done" ] && continue
    broker_warn "$issue" "$reason"
  done < <(inflight_worktrees)
}

# _afk_service_auth_halt -> service a raised _AFK_AUTH_FAILED WITHOUT stopping the drain (#241 §9).
# Auth is the one true external blocker, but it only HALTS DISPATCH (the _AFK_AUTH_FAILED
# short-circuits), warns the in-flight spokes loudly + repeatedly, RE-PROBES auth each tick, and
# CLEARS the flag (resuming the drain) the moment auth recovers. The re-probe is the bounded
# headless-claude no-op _afk_auth_is_dead already uses.
_afk_service_auth_halt() {
  log "/afk: subscription auth failed — dispatch HALTED (re-run /login on the host); re-probing each tick, NOT stopping the drain (#241 §9)"
  _warn_all_inflight "subscription auth failed — dispatch halted; re-run /login on the host (retrying auth each tick)"
  if ! _afk_auth_is_dead; then
    _AFK_AUTH_FAILED=0
    log "/afk: auth recovered — resuming the drain"
  fi
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
You're in a dedicated worktree for issue #$n. Your task contract is on disk at
.ai-toolkit/task.md (worktree-new.sh fetched it at spawn) — read it; no need to run
/source-task (that stays for re-anchor: run /source-task $n only if task.md is missing
or the issue was edited after spawn — that re-fetches the live issue). Before touching
code, break the task into a task ledger (TaskCreate, or
TodoWrite on older runtimes) — one todo per subtask × the solo-cycle steps that apply
(ANCHOR/RED/GREEN/REVIEW/PUSH), exactly one in_progress.

Honor the issue's Gate: line. If it is \`plan\` (the default for non-trivial work): the
PLAN gate comes first — explore the code, then print the full implementation plan (files,
approach, test strategy, open questions) as a normal visible message. Then emit the gate
marker AND hand it your plan, so the hub reads it from a scripted artifact rather than
parsing your transcript: write the plan to a gitignored scratch file (e.g.
\`.ai-toolkit/gate-plan.md\` — the .ai-toolkit/ dir is gitignored, so it never dirties your
tree or blocks the ready gate) and pass it with
\`bash .ai-toolkit/scripts/spoke-ready.sh --gate $n --plan-file .ai-toolkit/gate-plan.md\`
(or inline a short plan with \`--gate $n -m "<plan>"\`). That parks you at the gate; WAIT for
approval before writing code (before GREEN). If the gate is \`none\`, run autonomous straight through.

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
    # Bound the gh call (#170 ST1): a hung `gh issue view` used to freeze the tick. A
    # timeout / failure logs and leaves the scope unknown, which fails CLOSED below
    # (`--inflight *` holds back every ready issue) — never a silent empty scope.
    if ! body="$(_afk_with_timeout "$AFK_GH_TIMEOUT" gh issue view "$issue" --json body -q .body 2>/dev/null)"; then
      log "  gh issue view #$issue timed out or failed — treating its scope as unknown (exclusive)"
      body=""
    fi
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

# --- dispatch-failure ceiling (issue #170 ST6) --------------------------------
# A worktree-new.sh that keeps failing for one issue (a malformed issue, a wedged infra
# dep) used to be retried silently every tick forever. Count consecutive failures per issue
# in the state dir; at AFK_DISPATCH_MAX_FAILURES (default 3) record a durable local block
# (the _afk_record_blocked_locally pattern, surfaced by --status) and skip that issue for
# the rest of the window. A success clears the counter. Cleared on a fresh arm.
: "${AFK_DISPATCH_MAX_FAILURES:=3}"
_afk_dispatch_fail_file() { printf '%s\n' "$(_afk_state_dir)/dispatch-fail-$1.count"; }
_afk_read_dispatch_failures() {
  local f n; f="$(_afk_dispatch_fail_file "$1")"
  n="$( [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' )"
  case "$n" in '' | *[!0-9]*) n=0 ;; esac
  printf '%s\n' "$n"
}
# _afk_incr_dispatch_failures <issue> -> bump and echo the new consecutive-failure count.
_afk_incr_dispatch_failures() {
  local issue="$1" n dir
  dir="$(_afk_state_dir)"; mkdir -p "$dir" 2>/dev/null || true
  n=$(( $(_afk_read_dispatch_failures "$issue") + 1 ))
  _afk_atomic_write "$(_afk_dispatch_fail_file "$issue")" "$n" || true
  printf '%s\n' "$n"
}
_afk_clear_dispatch_failures() { rm -f "$(_afk_dispatch_fail_file "$1")" 2>/dev/null || true; }
_afk_clear_dispatch_fail_counts() { rm -f "$(_afk_state_dir)"/dispatch-fail-*.count 2>/dev/null || true; }
# _afk_dispatch_max_failures -> the ceiling, guarded to a positive integer. dispatch_batch
# computes this once per tick and compares each issue's count against the cached value.
_afk_dispatch_max_failures() {
  local max="${AFK_DISPATCH_MAX_FAILURES:-3}"
  case "$max" in '' | *[!0-9]* | 0) max=3 ;; esac
  printf '%s\n' "$max"
}

# dispatch_batch -> plan the next concurrent batch (batch-plan.sh, capped) and spawn a
# spoke for each issue not already in flight, seeded with the ultra kickoff and staggered
# so first-push suites don't all hit at once. A missing planner or dispatcher logs and is
# a no-op (the next tick retries).
dispatch_batch() {
  [ "$_AFK_AUTH_FAILED" -eq 1 ] && return 0   # auth is dead — don't spawn spokes into it
  local bp wt_new inflight args=() batch n cap stagger spawned=0 fails max
  bp="$(_afk_find_script "${BATCH_PLAN:-}" batch-plan.sh)" || { log "batch-plan.sh not found — skipping dispatch"; return 0; }
  wt_new="$(_afk_find_script "${WT_NEW:-}" worktree-new.sh)" || { log "worktree-new.sh not found — skipping dispatch"; return 0; }
  inflight="$(inflight_issues)"
  while IFS= read -r line; do args+=("$line"); done < <(_inflight_scope_args)
  # Bound total live spokes: batch-plan truncates so (in-flight + dispatched) ≤ cap.
  cap="$(_afk_dispatch_cap)"
  stagger="$(_afk_dispatch_stagger)"
  args+=("--cap" "$cap")
  # Bound the planner (#170 ST1): a wedged batch-plan.sh used to hang the tick. A timeout
  # or nonzero exit logs and skips dispatch THIS tick (retry next tick) — never a silent
  # empty batch that would look like "nothing to dispatch".
  if ! batch="$(_afk_with_timeout "$AFK_PLANNER_TIMEOUT" bash "$bp" "${args[@]+"${args[@]}"}" 2>/dev/null)"; then
    log "batch-plan.sh timed out or failed — skipping dispatch this tick (retry next tick)"
    return 0
  fi
  max="$(_afk_dispatch_max_failures)"
  for n in $batch; do
    # Dispatch is a LONG phase (a bounded planner, per-spawn staggers, worktree spawns);
    # stamp the heartbeat each iteration so the wedged-supervisor watchdog (#170 ST2) never
    # mistakes a busy dispatch for a hang and kills a working supervisor mid-spawn.
    afk_write_heartbeat
    printf '%s\n' "$inflight" | grep -qxF "$n" && continue   # already in flight (idempotent)
    # Ceiling (#170 ST6): an issue that already failed to dispatch AFK_DISPATCH_MAX_FAILURES
    # times this window is durably blocked — skip it instead of retrying forever. Uses the
    # cached `max` (computed once above) rather than recomputing it per issue.
    [ "$(_afk_read_dispatch_failures "$n")" -ge "$max" ] && continue
    # Stagger consecutive spawns (before the 2nd onward), so the co-located Langfuse
    # isn't hit by several first-push full suites at the same instant.
    [ "$spawned" -gt 0 ] && [ "$stagger" -gt 0 ] && sleep "$stagger" 2>/dev/null || true
    log "→ dispatch #$n"
    _afk_set_last_action "dispatch #$n"
    # --mode afk stamps the spoke's trace as drain-driven (#102); a hand-dispatched
    # spoke defaults to attended in worktree-new.sh.
    if bash "$wt_new" "$n" --type feature --mode afk --prompt "$(kickoff_for "$n")"; then
      stamp_dispatch_epoch "$n"
      _afk_clear_dispatch_failures "$n"   # a success resets the consecutive-failure count
      spawned=$(( spawned + 1 ))
    else
      fails="$(_afk_incr_dispatch_failures "$n")"
      if [ "$fails" -ge "$max" ]; then
        # #241 §5: no durable BLOCK — warn (backoff-gated, low frequency) and skip this window; a
        # fresh arm retries. There is no spoke worktree yet, so pass an empty wt.
        log "  dispatch of #$n failed $fails times — warn-parking (retries next window, see --status)"
        _warn_parked_last "" "$n" "dispatch (worktree-new.sh) failed $fails consecutive times — retried at low frequency" dispatch
      else
        log "  dispatch of #$n failed ($fails/$max) — will retry next tick"
      fi
    fi
  done
}

# --- auto-land + reap passes --------------------------------------------------

# The heartbeat must reflect PROGRESS, not merely child existence (#202 B): a land/answer
# that HANGS keeps its child alive, so stamping "while the child runs" kept the epoch fresh
# forever and defeated the stale-tick watchdog. So a single phase's stamping is BOUNDED to
# AFK_PHASE_MAX_SECONDS (a generous multiple of any legit phase); once a phase runs past it,
# stamping stops so the epoch ages, --status reads STALLED, and the watchdog respawns the
# wedged tree. A phase that COMPLETES always gets a final stamp (completion IS progress), so
# a merely slow-but-finishing land never triggers a false respawn. 0 disables the cap.
: "${AFK_PHASE_MAX_SECONDS:=900}"
_afk_phase_max_seconds() {
  local s="${AFK_PHASE_MAX_SECONDS:-900}"
  case "$s" in '' | *[!0-9]*) s=900 ;; esac
  printf '%s\n' "$s"
}

# _afk_heartbeat_stamper <ppid> -> the backgrounded stamp loop shared by the fg runner: every
# AFK_LAND_HEARTBEAT_SECONDS stamp the SUPERVISOR's pid (<ppid>, passed explicitly so a
# reparented orphan can't stamp the wrong pid), until the supervisor dies (orphan guard —
# `kill -0 <ppid>` fails once the parent is gone, so a stamper that outlived a killed
# supervisor stops instead of racing the respawn with a dead pid) or the phase runs past the
# AFK_PHASE_MAX_SECONDS cap (the hang surfaces). Returns when either bound is hit.
_afk_heartbeat_stamper() {
  local ppid="$1" interval maxs elapsed=0
  interval="${AFK_LAND_HEARTBEAT_SECONDS:-30}"; case "$interval" in '' | *[!0-9]* | 0) interval=30 ;; esac
  maxs="$(_afk_phase_max_seconds)"
  while :; do
    kill -0 "$ppid" 2>/dev/null || return 0                     # supervisor gone — stop (orphan guard)
    { [ "$maxs" -ne 0 ] && [ "$elapsed" -ge "$maxs" ]; } && return 0   # phase hung — stop stamping
    afk_write_heartbeat_pid "$ppid"
    sleep "$interval" 2>/dev/null || true
    elapsed=$(( elapsed + interval ))
  done
}

# _afk_run_with_heartbeat <cmd...> -> run <cmd...> (backgrounded) while stamping the heartbeat
# every AFK_LAND_HEARTBEAT_SECONDS, so the epoch stays honest through the longest tick phase
# (#133 item 4) — but BOUNDED to AFK_PHASE_MAX_SECONDS so a hung child surfaces (#202 B). The
# stamp loop runs in THIS shell, so a killed supervisor stops stamping outright (no orphan).
# Returns the command's exit code (a failed land must still escalate).
_afk_run_with_heartbeat() {
  local child rc slept elapsed=0 interval="${AFK_LAND_HEARTBEAT_SECONDS:-30}" maxs
  case "$interval" in '' | *[!0-9]* | 0) interval=30 ;; esac
  maxs="$(_afk_phase_max_seconds)"
  "$@" &
  child=$!
  while kill -0 "$child" 2>/dev/null; do
    # Stamp PROGRESS, not child-existence: stop refreshing once the phase runs past the cap
    # so a hung land ages the epoch and the watchdog respawns the tree (#202 B).
    { [ "$maxs" -eq 0 ] || [ "$elapsed" -lt "$maxs" ]; } && afk_write_heartbeat
    # Re-check the child every second within the stamp interval — a full-interval
    # sleep would hold the tick up to AFK_LAND_HEARTBEAT_SECONDS after a fast land.
    slept=0
    while [ "$slept" -lt "$interval" ] && kill -0 "$child" 2>/dev/null; do
      sleep 1 2>/dev/null || true
      slept=$(( slept + 1 ))
    done
    elapsed=$(( elapsed + slept ))
  done
  wait "$child"; rc=$?
  afk_write_heartbeat   # the child COMPLETED — progress — so always stamp (a slow-but-done land is not wedged)
  return "$rc"
}

# _afk_run_with_heartbeat_fg <cmd...> -> the same guarantee as _afk_run_with_heartbeat, but
# for a command that must run in the CURRENT shell because it sets a variable the caller reads
# (answer_pass's decide_and_act raises the process-global _AFK_AUTH_FAILED; backgrounding it
# would lose the assignment in a subshell). So the STAMPER is backgrounded instead
# (_afk_heartbeat_stamper), carrying the supervisor's pid + the orphan/phase-cap guards.
# Returns the command's exit code.
_afk_run_with_heartbeat_fg() {
  local stamper rc
  _afk_heartbeat_stamper "$$" &
  stamper=$!
  "$@"; rc=$?
  kill "$stamper" 2>/dev/null || true
  wait "$stamper" 2>/dev/null || true
  afk_write_heartbeat   # the command returned — progress — stamp the supervisor's pid
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

# --- stranded ready+blocked land-retry budget (issue #202 D) ------------------
# A finished tip carrying BOTH ready/<issue> and blocked/<issue> hit a TRANSIENT land
# failure (a diverged-merge blip, a momentary push rejection). The tip is final, so the
# spoke never commits fresh work for reconcile_markers to clear the stale block — and the
# old "skip a blocked-at-tip issue" logic skip-landed it EVERY tick forever (recovered by
# hand with a manual `blocked/<N>` delete). auto_land now RETRIES the land up to
# AFK_LAND_RETRY_MAX times (per issue, this window); once the budget is spent it escalates
# VISIBLY (a durable local record --status surfaces) instead of spinning silently.
: "${AFK_LAND_RETRY_MAX:=1}"
_afk_land_retry_file() { printf '%s\n' "$(_afk_state_dir)/land-retry-$1.count"; }
_afk_read_land_retries() {
  local f n; f="$(_afk_land_retry_file "$1")"
  n="$( [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' )"
  case "$n" in '' | *[!0-9]*) n=0 ;; esac
  printf '%s\n' "$n"
}
_afk_incr_land_retries() {
  local issue="$1" n dir
  dir="$(_afk_state_dir)"; mkdir -p "$dir" 2>/dev/null || true
  n=$(( $(_afk_read_land_retries "$issue") + 1 ))
  _afk_atomic_write "$(_afk_land_retry_file "$issue")" "$n" || true
}
_afk_clear_land_retries() { rm -f "$(_afk_land_retry_file "$1")" 2>/dev/null || true; }
_afk_clear_land_retry_counts() { rm -f "$(_afk_state_dir)"/land-retry-*.count 2>/dev/null || true; }
_afk_land_retry_max() {
  local max="${AFK_LAND_RETRY_MAX:-1}"
  case "$max" in '' | *[!0-9]*) max=1 ;; esac
  printf '%s\n' "$max"
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
# The reasoning code-review verdict is the /afk test-gutting gate (#143), ON by default again
# (#183): auto_land lands ONLY on a clean APPROVE verdict — a REQUEST_CHANGES (the reviewer
# flagged gutting) or no review at all escalates to blocked/<issue> instead. It defaulted OFF
# under #152 because the #143 gate false-positive-escalated clean lands whose spokes left no
# verdict artifact in the reader's format (reviews that finished in <1s), bricking the whole
# drain (#151). That failure class is now closed at the SOURCE by #172: every ready/<issue>
# emission through spoke-ready requires an APPROVE artifact bound to the tip, so a ready with
# no clean verdict can only be a hand-crafted bypass — escalating it is the intended gate, not
# a false positive. Set AFK_REVIEW_GATE=0 to opt back out (restore the #152 land-anything
# behavior); the mechanical anti-gutting scan stays the advisory residual signal either way.
auto_land() {
  local wt_land path issue verdict max tries land_log land_rc
  wt_land="$(_afk_find_script "${WT_LAND:-}" worktree-land.sh)" || { log "worktree-land.sh not found — skipping land"; return 0; }
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    _ready_at_tip "$path" "$issue" || continue
    # #241: pace the whole per-issue land attempt on the warned-retry backoff. A prior land
    # failure / unclean-review / retry-exhausted warn armed the backoff; while it is pending this
    # spoke is skipped (parked LAST), so a permanently-conflicted land is re-attempted at LOW
    # frequency (worktree-land is expensive) — not every tick. A fresh (never-warned) spoke is
    # always due, so the first land attempt is never delayed. Cleared on a successful land below.
    _afk_warned_due "$issue" || continue
    if _blocked_at_tip "$path" "$issue"; then
      # ready+blocked at a finished tip = a TRANSIENT land failure. Retry the land up to
      # AFK_LAND_RETRY_MAX times, then escalate VISIBLY — never skip-land it forever (#202 D).
      max="$(_afk_land_retry_max)"; tries="$(_afk_read_land_retries "$issue")"
      if [ "$tries" -ge "$max" ]; then
        # #241 §5: no longer terminal — warn + retry on the warned-retry backoff (low frequency),
        # never park blocked/<issue>. The land is re-attempted on later ticks/windows.
        log "  land #$issue still fails after $tries retry attempt(s) — warn-parking on the backoff (#241)"
        _warn_parked_last "$path" "$issue" "land retried $tries time(s) and still fails at a finished tip — retrying at low frequency" land
        continue
      fi
      log "  retry land #$issue — ready+blocked coexist at a finished tip (transient land failure); clearing blocked/$issue and re-landing (attempt $(( tries + 1 ))/$max)"
      _afk_incr_land_retries "$issue"
      git -C "$path" tag -d "blocked/$issue" >/dev/null 2>&1 || true
      git -C "$path" push origin ":refs/tags/blocked/$issue" >/dev/null 2>&1 || true
      # fall through to the land attempt below
    fi
    if [ "${AFK_LAND_FOREIGN:-1}" = "0" ] && [ -z "$(read_dispatch_epoch "$issue")" ]; then
      log "  skip land #$issue — foreign (no dispatch epoch) and AFK_LAND_FOREIGN=0"
      continue
    fi
    if [ "${AFK_REVIEW_GATE:-1}" != "0" ]; then
      verdict="$(_afk_review_verdict "$path")"
      if [ "$verdict" != "APPROVE" ]; then
        # #241 §6: never silent block. Per AFK_REVIEW_GATE_ON_UNCLEAN:
        #   retry (DEFAULT, safe) — warn + retry; do NOT auto-land, since a ready/<issue> with an
        #     unclean/missing verdict is a #172-bypass and landing it ships possibly-test-gutted
        #     code to main. The loud warning surfaces it for the human.
        #   land — warn LOUDLY + land anyway (records the unclean verdict for post-review), the
        #     operator's explicit opt-in to §6's land-with-warning (the "mint a hub-side review
        #     first" step is not implementable here). UPGRADE: mint a hub-side review attempt.
        if [ "${AFK_REVIEW_GATE_ON_UNCLEAN:-retry}" = "land" ]; then
          broker_warn "$issue" "LANDING despite an unclean review verdict (${verdict:-no review}) — possible test-gutting; review post-hoc"
          # outward: the land merges+pushes to shared main (others pull it, CI fires) — not merely a scope change.
          broker_journal_decision "$issue" review "landed despite unclean review verdict (${verdict:-no review})" outward
          # fall through to land
        else
          _warn_parked_last "$path" "$issue" "code-review verdict not clean (${verdict:-no review}) — warn + retry; set AFK_REVIEW_GATE_ON_UNCLEAN=land to land with a warning" review
          continue
        fi
      fi
    fi
    log "→ land #$issue"
    _afk_set_last_action "land #$issue"
    # Capture the land's output to a per-issue log (#198): the old >/dev/null discarded exactly
    # what an operator needs when a land half-completes. mkdir so the log write can't fail on a
    # not-yet-created state dir. _afk_run_with_heartbeat returns worktree-land's exit code.
    land_log="$(_afk_state_dir)/land-$issue.log"; mkdir -p "$(_afk_state_dir)" 2>/dev/null || true
    _afk_run_with_heartbeat bash "$wt_land" "$issue" --skip-tests >"$land_log" 2>&1; land_rc=$?
    if [ "$land_rc" -eq 0 ]; then
      log "  landed #$issue"
      _afk_clear_land_retries "$issue"   # a successful land resets the retry budget (#202 D)
      _afk_clear_warned "$issue"         # #241: progress → drop the land's warned-retry backoff
      _afk_incr_landed   # tally for the drain-complete notification (#150)
    elif [ "$land_rc" -eq 3 ]; then
      # Sentinel (#198 / #202 I): main ADVANCED but a teardown step failed — the code IS
      # shipped, so NEVER stamp blocked over merged work. Tally it and point at the log.
      log "  landed #$issue but teardown incomplete (worktree-land exit 3) — see $land_log; NOT escalating (main already advanced)"
      _afk_clear_land_retries "$issue"
      _afk_clear_warned "$issue"         # #241: shipped → drop the warned-retry backoff
      _afk_incr_landed
    else
      # #241 §5: an auto-land failure (merge conflict / push rejection) warns + retries on the
      # backoff instead of parking blocked/<issue>. The land is re-attempted on later ticks.
      _warn_parked_last "$path" "$issue" "auto-land failed (merge conflict or push rejection, exit $land_rc) — retrying at low frequency (see $land_log)" land
    fi
  done < <(inflight_worktrees)
}

# answer_pass -> auto-answer every waiting spoke. reap_pass -> reap every hung/overrun one.
answer_pass() {
  local path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    # Keep the heartbeat stamping THROUGH the answerer (a high-effort headless `claude`
    # that can run for minutes) so a legitimately long answer never trips the wedged-
    # supervisor respawn (#170 ST2). The foreground variant preserves decide_and_act's
    # _AFK_AUTH_FAILED assignment (a backgrounded command would lose it in its subshell).
    if [ "$(slot_state "$path" "$issue")" = "waiting" ]; then
      _afk_set_last_action "answer #$issue"
      _afk_run_with_heartbeat_fg decide_and_act "$path" "$issue"
    fi
  done < <(inflight_worktrees)
}
# _afk_auth_is_dead -> true when a bounded headless `claude` no-op reports an auth failure:
# the subscription token is dead so every spoke is stalled on auth, not individually hung.
# Detection mirrors the answerer's (is_auth_failure #170 ST7): a NONZERO exit AND an
# auth-failure signature together — a healthy probe (exit 0), or a nonzero exit without an
# auth signature (a transient blip, `claude` not on PATH), reads as alive so a hiccup never
# halts the drain. AFK_AUTH_PROBE_CMD overrides the probe (tests); AFK_AUTH_PROBE_TIMEOUT
# bounds it so a wedged probe can't itself freeze the reap.
_afk_auth_is_dead() {
  local cmd raw rc
  cmd="${AFK_AUTH_PROBE_CMD:-claude -p --no-session-persistence --model claude-opus-4-8 ok}"
  raw="$(_afk_with_timeout "${AFK_AUTH_PROBE_TIMEOUT:-30}" bash -c "$cmd" 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] && is_auth_failure "$raw"
}

reap_pass() {
  local path issue probed=0
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "reap" ] || continue
    # Auth probe before the FIRST reap this tick (#170 ST7): if the subscription token is
    # dead, every idle spoke is stalled on auth, not hung — reaping them one-by-one would
    # block live work into dead auth. Probe once; on a real auth failure raise the global
    # stop flag and bail, letting the main loop's halt-all path block them together.
    if [ "$probed" -eq 0 ]; then
      probed=1
      afk_write_heartbeat   # the probe is a bounded `claude` call — keep the epoch fresh (#170 ST2)
      if _afk_auth_is_dead; then
        _AFK_AUTH_FAILED=1
        log "/afk: auth probe failed during reap — halting instead of reaping spokes into dead auth"
        return 0
      fi
    fi
    _reap_or_resume "$path" "$issue"
  done < <(inflight_worktrees)
}

# --- dead-pane recovery each tick (issue #202 C) ------------------------------
# reap_pass only visits a spoke once the idle ceiling elapses, so a pane that CRASHES with
# work sat stranded for hours overnight (recovered by hand ~4x). recover_dead_panes runs
# EVERY tick and acts on the crash directly — no idle wait:
#   * dead pane + work (commits or dirty WIP) → resume in place ONCE (never reap work);
#     a second crash after the resume escalates (blocked/<issue>, needs a human).
#   * dead pane + clean (nothing to preserve) → tear the empty worktree down so the issue
#     RE-DISPATCHES (not escalated); a second clean crash after that escalates.
# A live pane and a terminal/parked spoke (done/waiting) are left untouched — reap_pass owns
# the idle/hung decision, auto_land owns done, the answerer owns waiting.

# _redispatch_dead_pane <wt> <issue> -> tear down a clean, empty crashed worktree so its
# issue returns to the backlog and re-dispatches next tick. Kills the window, then removes
# the worktree via worktree-done.sh (--force since the pane is dead; --no-code skips the
# editor-workspace edit). Records the once-per-window stamp on success. AFK_REDISPATCH_CMD
# overrides the teardown for tests. rc 1 when the teardown can't run (caller escalates).
_redispatch_dead_pane() {
  local wt="$1" issue="$2" wt_done
  log "→ redispatch #$issue: pane crashed with no work to preserve — tearing down the empty worktree so it re-dispatches"
  _kill_spoke_window "$issue"
  if [ -n "${AFK_REDISPATCH_CMD:-}" ]; then
    bash -c "$AFK_REDISPATCH_CMD"; _afk_mark_redispatched "$issue"; return 0
  fi
  wt_done="$(_afk_find_script "${WT_DONE:-}" worktree-done.sh)" \
    || { log "  worktree-done.sh not found — cannot re-dispatch #$issue"; return 1; }
  if bash "$wt_done" "$issue" --force --no-code >/dev/null 2>&1; then
    _afk_mark_redispatched "$issue"
    return 0
  fi
  log "  worktree-done.sh failed for #$issue — leaving the worktree in place"
  return 1
}

recover_dead_panes() {
  local path issue state
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    state="$(slot_state "$path" "$issue")"
    case "$state" in done | waiting) continue ;; esac   # terminal / parked — not a crash
    _spoke_pane_alive "$path" && continue                # live pane — reap_pass owns idle/hung
    # An over-ceiling runaway always blocks (as reap_pass does) — resume/re-dispatch never
    # applies. Checked first so a crashed-but-over-ceiling spoke is not revived here only to
    # be blocked by reap_pass in the same tick (the hard ceiling ignores fresh progress).
    # #241 §7: revive-first, warned-parked-LAST — never reap/block/abandon a crashed pane.
    if _spoke_over_any_ceiling "$issue" "$(afk_now)"; then
      _afk_revive_or_park_last "$path" "$issue" "time ceiling: ran >${AFK_SPOKE_MAX_MINUTES}m without finishing"
    elif _spoke_has_work "$path"; then
      if _afk_already_resumed "$issue"; then
        _warn_parked_last "$path" "$issue" "pane crashed again after an auto-resume — parked LAST, retried at low frequency"
      else
        resume_spoke "$path" "$issue" \
          || _warn_parked_last "$path" "$issue" "pane crashed and the auto-resume could not be launched — retrying"
      fi
    elif _afk_already_redispatched "$issue"; then
      _warn_parked_last "$path" "$issue" "pane crashed clean again after a re-dispatch — parked LAST, retried at low frequency"
    else
      _redispatch_dead_pane "$path" "$issue" \
        || _warn_parked_last "$path" "$issue" "pane crashed clean and the worktree teardown failed — retrying"
    fi
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

# --- afk:* status labels on GitHub issues (issue #223) ------------------------
# Behind AFK_GH_STATUS_LABELS=1, the drain maintains ONE afk:* status label per open issue
# reflecting its scheduling disposition, so the GitHub issue LIST answers "what's running /
# why is this waiting" at a glance. The label set (AFK_STATUS_LABELS overrides it for tests):
#   afk:in-flight | afk:queued | afk:blocked-by-scope | afk:exclusive
# Deliberately WITHOUT the cross-issue "blocks #N" detail — that stays in `--explain`; the
# per-issue label is only the issue's own state. Disposition comes from batch-plan's
# `--explain-labels` (the SAME renderer the terminal view uses), so the two never drift.
#
# afk_sync_status_labels is a per-tick RECONCILE: it also strips the label from any issue no
# longer open/in-flight (closed/landed, now held, or dep-blocked), so "stripped on close" is
# satisfied from within the drain — worktree-land.sh / worktree-done.sh are never edited. The
# design cautions the issue calls out: update IN PLACE (swap the one label, never comment),
# write only on CHANGE (skip the gh edit when unchanged, bounding API use per tick), and stay
# BEST-EFFORT (a gh failure logs and continues, never breaks a tick).
: "${AFK_STATUS_LABELS:=afk:in-flight afk:queued afk:blocked-by-scope afk:exclusive}"

afk_status_labels_enabled() { [ "${AFK_GH_STATUS_LABELS:-}" = "1" ]; }

# _afk_seed_status_labels -> create the afk:* label set in the repo once per window (a marker
# in the state dir dedups). `gh label create --force` is idempotent (updates an existing
# label rather than erroring). Best-effort: a create failure never aborts a tick.
_afk_status_labels_seed_marker() { printf '%s\n' "$(_afk_state_dir)/status-labels-seeded"; }
_afk_seed_status_labels() {
  local m lbl; m="$(_afk_status_labels_seed_marker)"
  [ -f "$m" ] && return 0
  for lbl in $AFK_STATUS_LABELS; do
    _afk_with_timeout "$AFK_GH_TIMEOUT" gh label create "$lbl" --force >/dev/null 2>&1 || true
  done
  mkdir -p "$(_afk_state_dir)" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$m" 2>/dev/null || true
}
_afk_clear_status_labels_seed() { rm -f "$(_afk_status_labels_seed_marker)" 2>/dev/null || true; }

# afk_sync_status_labels -> reconcile every open issue's afk:* label to its scheduling
# disposition (and strip stale ones). A no-op unless AFK_GH_STATUS_LABELS=1. Best-effort
# throughout: a missing tool, a failed planner, or a failed gh edit logs and returns 0.
afk_sync_status_labels() {
  afk_status_labels_enabled || return 0
  command -v gh >/dev/null 2>&1 || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  local bp; bp="$(_afk_find_script "${BATCH_PLAN:-}" batch-plan.sh)" || return 0
  # Desired: `<num>\t<afk:label|->` per open issue, seeded with the live in-flight set so a
  # running spoke's issue is labelled afk:in-flight. A planner failure means "unknown" —
  # skip this tick rather than strip every label on a transient blip.
  local ifargs=() n desired
  while IFS= read -r n; do [ -n "$n" ] && ifargs+=("--inflight-issue" "$n"); done < <(inflight_issues)
  if ! desired="$(_afk_with_timeout "$AFK_PLANNER_TIMEOUT" bash "$bp" --explain-labels ${ifargs[@]+"${ifargs[@]}"} 2>/dev/null)"; then
    log "  afk labels: batch-plan --explain-labels failed — skipping label sync this tick"
    return 0
  fi
  [ -n "$desired" ] || return 0
  # Current issues across ALL states (a closed/landed issue must lose its label too). A plain
  # --state all list (not a `--search label:` query, whose default-state semantics are
  # unreliable) keeps this unambiguous; python filters to the afk:* holders. --limit bounds
  # the payload: a recently-closed issue needing a strip is always in the newest slice.
  local current
  if ! current="$(_afk_with_timeout "$AFK_GH_TIMEOUT" gh issue list --state all --limit 200 --json number,state,labels 2>/dev/null)"; then
    current="[]"
  fi
  _afk_seed_status_labels
  # Diff desired-vs-current in python (JSON label parsing is unpleasant in bash): emit one
  # `<num>\t<add|->\t<remove-csv|->` line per issue that actually CHANGES (write-on-change).
  local plan
  plan="$(_AFK_DESIRED="$desired" _AFK_CURRENT="$current" _AFK_LABELS="$AFK_STATUS_LABELS" python3 <<'PYEOF'
import json
import os

afk = set(os.environ.get("_AFK_LABELS", "").split())
desired = {}
for line in os.environ.get("_AFK_DESIRED", "").splitlines():
    if not line.strip():
        continue
    num, _tab, lab = line.partition("\t")
    num, lab = num.strip(), lab.strip()
    if num:
        desired[num] = lab  # an afk:* label, or "-" for held/dep-blocked (no label)

try:
    holders = json.loads(os.environ.get("_AFK_CURRENT", "") or "[]")
except Exception:
    holders = []
present = {}
for item in holders if isinstance(holders, list) else []:
    num = str(item.get("number"))
    present[num] = [l.get("name") for l in (item.get("labels") or []) if l.get("name") in afk]

for num in set(desired) | set(present):
    want = desired.get(num, "-")  # absent from the open backlog ⇒ strip whatever it carries
    have = present.get(num, [])
    if want == "-":
        if have:
            print(f"{num}\t-\t{','.join(have)}")
        continue
    if have == [want]:
        continue  # already correct — no gh call
    remove = [l for l in have if l != want]
    print(f"{num}\t{want}\t{','.join(remove) if remove else '-'}")
PYEOF
)"
  local issue add remove args
  while IFS=$'\t' read -r issue add remove; do
    [ -n "$issue" ] || continue
    args=()
    [ "$add" != "-" ] && args+=("--add-label" "$add")
    [ "$remove" != "-" ] && args+=("--remove-label" "$remove")
    [ "${#args[@]}" -gt 0 ] || continue
    if _afk_with_timeout "$AFK_GH_TIMEOUT" gh issue edit "$issue" ${args[@]+"${args[@]}"} >/dev/null 2>&1; then
      log "  afk label #$issue → ${add}${remove:+ (was $remove)}"
    else
      log "  afk labels: gh issue edit #$issue failed (best-effort) — continuing"
    fi
  done < <(printf '%s\n' "$plan")
}

# --- the supervisor tick + stop condition -------------------------------------

# supervise_tick -> one full pass: reconcile stale markers, dispatch the next batch, answer
# parked spokes, land the ready ones, reap the hung ones, then reconcile the afk:* status
# labels (#223). Each pass re-surveys the in-flight set, so a spoke that changed state
# earlier in the tick is seen fresh.
supervise_tick() {
  reconcile_markers
  dispatch_batch
  answer_pass
  # If the answer pass detected a dead subscription token, skip land + reap this tick:
  # both would shell out to a `claude`/suite that is just as dead. The main loop blocks
  # the in-flight spokes and stops.
  [ "$_AFK_AUTH_FAILED" -eq 1 ] && return 0
  auto_land
  recover_dead_panes
  reap_pass
  # Reflect the tick's final disposition on GitHub (best-effort, no-op unless the flag is on).
  afk_sync_status_labels
}

# --- event-driven wake (issue #176) -------------------------------------------
# A writer (spoke-ready.sh / the Notification hook) drops an event file in the spool and
# SIGUSR1s this pid; the trap flips _AFK_WOKEN and the interruptible sleep returns early,
# so a parked spoke is serviced in seconds, not a full tick. Events are WAKE-UPS, not
# state: on wake we re-derive via slot_state, so a duplicate/stale/lost event is safe (a
# lost one is caught by the next full sweep).
_AFK_WOKEN=0
_afk_on_usr1() { _AFK_WOKEN=1; }
trap _afk_on_usr1 USR1
# Advertise wake-capability ONLY now that the USR1 trap is armed (#207): the heartbeat writer
# appends this token, so afk-notify-wake signals this supervisor iff it can safely absorb the
# signal. Set at the trap site (not at the top of the file) so "token present" structurally
# tracks "trap installed" — a pre-#176 supervisor never runs this line, stamps a bare
# two-field heartbeat, and is left un-signalled rather than killed.
_AFK_WAKE_TOKEN="wake1"

# afk_interruptible_sleep <secs> -> a sleep that a USR1 cuts short. A trapped signal
# interrupts the `wait` builtin (not a bare `sleep`), so background the timer and wait on
# it; the trap runs and wait returns, then kill the timer if it is still running. The tiny
# race — a USR1 delivered after `sleep &` but before `wait` — degrades to servicing on the
# next full tick, exactly the design's guaranteed-safe "lost event" path.
afk_interruptible_sleep() {
  local secs="$1" t
  sleep "$secs" & t=$!
  wait "$t" 2>/dev/null || true
  kill "$t" 2>/dev/null || true
}

# service_event_wake -> the targeted pass an event signal triggers: drain the spool (log
# the announcers) then answer + land. It runs only the announce-driven passes — a wake
# means a spoke ANNOUNCED, never that one went silent — so the silence-shaped work
# (dispatch, reap, reconcile, drain-done) stays on the full tick. answer_pass / auto_land
# already self-limit to waiting / ready spokes, so re-deriving over the whole in-flight set
# is a safe superset of "service only the named spokes" (slot_state is authoritative).
service_event_wake() {
  local issues; issues="$(afk_drain_event_issues)"
  [ -n "$issues" ] && log "/afk: event wake — servicing $(printf '%s' "$issues" | tr '\n' ' ')"
  answer_pass
  # Same auth short-circuit as supervise_tick: a dead token means auto_land would shell
  # into dead auth; the main loop's post-service check halts + blocks the in-flight set.
  [ "$_AFK_AUTH_FAILED" -eq 1 ] && return 0
  auto_land
}

# _afk_issue_poisoned <issue> -> true when an open+ready issue is NOT dispatchable this
# window: it hit the dispatch-failure ceiling (a malformed issue / wedged infra dep) or it
# carries a durable local block record. batch-plan keeps returning such an issue every tick,
# so without discounting it afk_done never sees an empty batch and a drain with only poisoned
# issues idles forever (#202 F).
_afk_issue_poisoned() {
  local issue="$1"
  [ "$(_afk_read_dispatch_failures "$issue")" -ge "$(_afk_dispatch_max_failures)" ] && return 0
  [ -f "$(_afk_blocked_record "$issue")" ] && return 0
  return 1
}

# afk_done <state> <now> -> true when the supervisor should stop: the window was turned off
# (no state) or a clock-bound window expired. In DRAIN mode only, it also stops when the
# backlog is drained (the planner returns no DISPATCHABLE issue AND nothing is in flight) --
# a clock-bound window ignores an empty backlog and ticks until its clock, so window_expired
# is its sole completion path (#222).
afk_done() {
  local state="$1" now="$2" bp inflight_count batch tok remaining=""
  [ -n "$state" ] || return 0
  window_expired "$state" "$now" && return 0
  # The backlog-drained stop below is drain-mode-only: a non-expired clock-bound (numeric)
  # state keeps ticking regardless of the backlog, so it never self-completes on tick one
  # when the whole backlog is empty / held / poisoned (#222).
  [ "$state" = drain ] || return 1
  inflight_count="$(inflight_issues | grep -c '^[0-9]' || true)"
  [ "$inflight_count" -eq 0 ] || return 1
  bp="$(_afk_find_script "${BATCH_PLAN:-}" batch-plan.sh)" || return 1
  # A planner ERROR is not an empty backlog (#170 ST3): declare drain-done only when
  # batch-plan EXITS 0 and prints an empty batch. A nonzero exit (a `gh` blip, a timeout)
  # means "could not determine the backlog" — return "not done" so a transient failure
  # never ends the whole drain with a false "done" + drain-complete notification.
  if ! batch="$(_afk_with_timeout "$AFK_PLANNER_TIMEOUT" bash "$bp" 2>/dev/null)"; then
    log "/afk: batch-plan.sh timed out or failed during the done-check — not declaring done (retry next tick)"
    return 1
  fi
  # Discount poisoned issues (#202 F): a dispatch-ceiling-skipped / locally-blocked issue is
  # not dispatchable, so a batch of only poisoned issues is a drained backlog, not live work.
  for tok in $batch; do
    case "$tok" in *[!0-9]*) remaining="$remaining $tok"; continue ;; esac   # non-issue token — keep
    _afk_issue_poisoned "$tok" && continue
    remaining="$remaining $tok"
  done
  [ -z "$(printf '%s' "$remaining" | tr -d '[:space:]')" ]
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
# Heartbeat-age staleness (#170 ST2, the #107 UPGRADE): a supervisor whose pid is alive but
# has not stamped a tick in AFK_STALE_TICKS x AFK_TICK_SECONDS is wedged on a hung call, not
# working — the watchdog kills and respawns it. answer_pass + auto_land keep stamping through
# their long phases (_afk_run_with_heartbeat[_fg]), so a busy supervisor never reads as wedged.
: "${AFK_STALE_TICKS:=4}"

# _afk_heartbeat_wedged -> true when the heartbeat epoch is older than
# AFK_STALE_TICKS x AFK_TICK_SECONDS. The caller (watchdog_tick) has already confirmed the
# heartbeat pid is a LIVE process, so a stale epoch here means the supervisor is wedged. A
# missing / unparseable epoch is NOT wedged (the pid-liveness `stale` path owns that case).
_afk_heartbeat_wedged() {
  local hb tick age stale_ticks limit
  hb="$(afk_read_heartbeat)"; [ -n "$hb" ] || return 1
  tick="$(afk_heartbeat_epoch "$hb")"
  case "$tick" in '' | *[!0-9]*) return 1 ;; esac
  stale_ticks="${AFK_STALE_TICKS:-10}"
  case "$stale_ticks" in '' | *[!0-9]*) stale_ticks=10 ;; esac
  limit=$(( stale_ticks * AFK_TICK_SECONDS ))
  age=$(( $(afk_now) - tick ))
  [ "$age" -gt "$limit" ]
}

# _afk_kill_wedged_supervisor -> terminate the wedged supervisor AND its whole hung call tree
# so a respawn does not leave two supervisors racing on the per-run state — and, crucially, so
# the hung CHILD (the answerer `claude`, a stuck `batch-plan`/`gh`) dies with it instead of
# surviving to collide with the respawn (#202 E). Kills leaf-first via _afk_kill_tree (the
# same descendant walk the bounded-call killer uses): SIGTERM the tree, a bounded grace, then
# SIGKILL the tree if the supervisor ignored TERM. Best-effort; the pid is known live
# (watchdog_tick checked). AFK_WEDGE_KILL_CMD overrides the whole kill for tests.
# Pid-recycling guard (#170 review): a supervisor that died without clearing its heartbeat
# leaves a pid the OS may recycle onto an unrelated process; kill only a pid whose command
# still looks like a hub-afk supervisor (AFK_WEDGE_PID_MATCH, default "hub-afk"), never a
# random recycled process. Set AFK_WEDGE_PID_MATCH= (empty) to skip the check.
_afk_kill_wedged_supervisor() {
  if [ -n "${AFK_WEDGE_KILL_CMD:-}" ]; then bash -c "$AFK_WEDGE_KILL_CMD"; return 0; fi
  local hb pid waited grace match cmdline descendants p
  hb="$(afk_read_heartbeat)"; pid="${hb%% *}"
  case "$pid" in '' | *[!0-9]*) return 0 ;; esac
  match="${AFK_WEDGE_PID_MATCH-hub-afk}"
  if [ -n "$match" ]; then
    cmdline="$(LC_ALL=C ps -o command= -p "$pid" 2>/dev/null)"
    case "$cmdline" in
      *"$match"*) : ;;
      *) log "  wedged-supervisor pid $pid is not a '$match' process (recycled?) — not killing"; return 0 ;;
    esac
  fi
  # Snapshot the descendant tree BEFORE the TERM (#202 E review): the supervisor bash traps
  # only USR1, so it dies promptly on TERM — after which its children reparent to init and
  # `pgrep -P "$pid"` finds NOTHING, leaving a TERM-ignoring child (a wedged `claude`, the very
  # target here) unreachable for the SIGKILL escalation. Capture the pids now so we can KILL
  # any survivor by pid even after the parent is gone.
  descendants="$(_afk_descendant_pids "$pid")"
  _afk_kill_tree "$pid" TERM   # the supervisor + its hung children, leaf-first
  grace="${AFK_WEDGE_KILL_GRACE:-3}"; case "$grace" in '' | *[!0-9]*) grace=3 ;; esac
  waited=0
  while [ "$waited" -lt "$grace" ] && kill -0 "$pid" 2>/dev/null; do
    sleep 1 2>/dev/null || true; waited=$(( waited + 1 ))
  done
  # SIGKILL any survivor from the pre-TERM snapshot (the root, or a descendant that ignored
  # TERM) — by pid, so a child that outlived its reparented-away parent still dies.
  for p in $pid $descendants; do
    kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null || true
  done
  return 0
}

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
#   live      — a supervisor is alive and recently stamped the heartbeat; nothing to do.
#   respawned — the window is armed but the supervisor is gone (dead pid) OR wedged (live
#               pid, stale heartbeat, #170 ST2): respawn it, first killing a wedged one.
watchdog_tick() {
  case "$(afk_supervisor_state)" in
    off)  printf 'off\n' ;;
    live)
      if _afk_heartbeat_wedged; then
        log "/afk watchdog: supervisor pid alive but heartbeat stale >$(( ${AFK_STALE_TICKS:-10} * AFK_TICK_SECONDS ))s — killing the wedged supervisor and respawning"
        _afk_kill_wedged_supervisor
        _afk_watchdog_respawn
        printf 'respawned\n'
      else
        printf 'live\n'
      fi ;;
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

# --- restart-survival re-arm (issue #202 A) -----------------------------------
# The supervisor and its watchdog are children of the hub session's shell, so a process
# or machine teardown kills BOTH and nothing re-arms — .afk-state stays armed with no
# process draining, and every in-flight spoke runs on with no answerer/lander (the
# overnight ~10h strand). afk_reconcile is the idempotent resume the hub runs when it
# comes back up (`hub-afk.sh --reconcile`): safe to call at every session start.
#   off   — no window armed; nothing to re-arm.
#   live  — a supervisor is already stamping the heartbeat; NO-OP (never stack a second,
#           which would clobber the per-run state).
#   stale — armed but no live supervisor (crashed / killed with the shell): re-run the
#           SAME arm preconditions + telemetry preflight a fresh arm runs (so a spoke
#           checkout / dirty tree / dead pipeline refuses exactly as arming would — the
#           base-branch precondition makes this a no-op in a spoke worktree), then
#           relaunch the supervisor via the detached no-arg resume (re-adopting the
#           in-flight spokes, never re-arming a fresh window) and ensure the watchdog.
# The resumed supervisor's first tick recovers stranded spokes (reconcile + dead-pane +
# land passes), so re-arm is the single entry point that self-heals a teardown.
afk_reconcile() {
  local repo_root="${1:-${MAIN_ROOT:-}}"
  case "$(afk_supervisor_state)" in
    off)  log "/afk reconcile: no window armed — nothing to re-arm"; return 0 ;;
    live) log "/afk reconcile: a supervisor is already live — nothing to do"; return 0 ;;
  esac
  log "/afk reconcile: window armed but no live supervisor — re-arming (resume)"
  if ! afk_arm_preconditions "$repo_root"; then
    log "/afk reconcile: preconditions not met — not re-arming (see above)"
    return 1
  fi
  if ! afk_telemetry_preflight "$repo_root"; then
    log "/afk reconcile: telemetry preflight failed — not re-arming (see above)"
    return 1
  fi
  _afk_watchdog_respawn   # detached no-arg resume — re-adopts the in-flight spokes
  _afk_spawn_watchdog     # keep exactly one watchdog alive (idempotent)
  log "/afk reconcile: re-armed — supervisor resumed, watchdog ensured"
  return 0
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

# afk_ensure_port <port> <launch-fn> <repo_root> [recover-fn] -> ensure something LISTENs on
# <port>: a no-op when already up; otherwise, when the port is DOWN, first run the optional
# <recover-fn> <repo_root> (issue #202 H — tear down a crashed/stopped container so its --name
# doesn't clash with the relaunch), then run <launch-fn> <repo_root> and re-probe up to
# AFK_PORT_WAIT_TRIES times (so a slow container start isn't a false DOWN). rc 1 when the port
# is still down after the launch — the caller turns that into a refuse-to-arm.
afk_ensure_port() {
  local port="$1" launch="$2" repo_root="$3" recover="${4:-}" tries="${AFK_PORT_WAIT_TRIES:-10}" i=0
  wt_port_listening "$port" && return 0
  [ -n "$recover" ] && "$recover" "$repo_root"   # e.g. wt_collector_recover_dead: docker rm a dead container
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
  # Recover a crashed/stopped lf-collector (docker exit 255 observed) before relaunching: an
  # Exited/Dead container still owns the `lf-collector` name, so a bare relaunch fails the
  # --name clash and re-arm is blocked forever (#202 H). wt_collector_recover_dead tears down
  # only a NON-running container (a healthy one is left untouched) so the relaunch is clean.
  if ! afk_ensure_port 4317 wt_collector_launch "$repo_root" wt_collector_recover_dead; then
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

# --- arm preconditions (issue #170 ST4) ---------------------------------------
# Mirror the telemetry preflight's refuse-to-arm posture for the drain's OWN prerequisites:
# a second supervisor clobbers per-run state, a dirty tree / off-base HEAD means the drain
# would land on top of uncommitted or wrong-branch work, and dead `gh` auth fails every
# dispatch/land/answer. Each is checked BEFORE writing state, so a bad precondition refuses
# loudly (never a half-armed window). AFK_ARM_PRECHECK=0 opts the whole gate out (tests, or
# an operator who has vetted the state by hand); it is on by default.

# afk_arm_preconditions <repo_root> -> rc 0 when every precondition holds, else log which
# one failed and return 1 (main turns that into a refuse-to-arm, exit 2).
afk_arm_preconditions() {
  local repo_root="$1" base cur
  [ "${AFK_ARM_PRECHECK:-1}" = "0" ] && return 0
  if [ "$(afk_supervisor_state)" = "live" ]; then
    log "/afk: refusing to arm — a supervisor is already live (heartbeat pid running); run /afk --off first (a second supervisor clobbers per-run state)"
    return 1
  fi
  # --untracked-files=no: refuse on uncommitted TRACKED changes (a drain lands on top of
  # the base branch), but tolerate untracked/generated files a routine hub sync leaves
  # behind — those never conflict with a merge and shouldn't block the drain (#170 review).
  if [ -n "$(git -C "$repo_root" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    log "/afk: refusing to arm — the working tree has uncommitted tracked changes; commit or stash first (an unattended drain lands on top of the base branch)"
    return 1
  fi
  base="$(_afk_default_ref "$repo_root")"; base="${base#origin/}"
  cur="$(git -C "$repo_root" branch --show-current 2>/dev/null)"
  # An empty `cur` is a DETACHED HEAD — refuse (a drain must arm from the base branch, else
  # auto_land's commits are orphaned with no branch advancing, #170 review). Only skip the
  # check when the base itself can't be resolved (nothing to compare against).
  if [ -n "$base" ] && [ "$cur" != "$base" ]; then
    log "/afk: refusing to arm — HEAD is on '${cur:-a detached HEAD}', not the base branch '$base'; check out $base before draining"
    return 1
  fi
  if ! _afk_with_timeout "$AFK_GH_TIMEOUT" gh auth status >/dev/null 2>&1; then
    log "/afk: refusing to arm — 'gh auth status' failed; run 'gh auth login' (dispatch/land/answer all need GitHub)"
    return 1
  fi
  return 0
}

# --- CLI ----------------------------------------------------------------------

# _afk_status_state_line <state> <now> -> echo the window's state line, distinguishing the
# three cases an operator kept confusing at a glance (#202 B), so idle-vs-hung is a read, not
# a process-tree autopsy:
#   DRAIN DEAD — armed but the supervisor pid is gone (crashed); the watchdog respawns it.
#   STALLED    — pid alive but the heartbeat epoch is older than a tick+grace: wedged on a
#                hung call, not working (bounded stamping stops refreshing a hung phase).
#   draining/on — idle: the heartbeat is recent (a plain tick sleep); report the next tick +
#                the last action so a healthy idle drain is obviously alive.
# The idle vs STALLED boundary is AFK_TICK_SECONDS + one stamp interval of grace: a healthy
# supervisor stamps at least every tick (and every ~30s through a long phase), so a heartbeat
# older than that means it is not ticking. A drain with no parseable heartbeat epoch (rare)
# falls back to the plain draining/remaining line.
_afk_status_state_line() {
  local state="$1" now="$2" rem age hb tick idle_limit nxt last
  if [ "$(afk_supervisor_state)" = "stale" ]; then
    age="$(_afk_heartbeat_age_minutes)"
    if [ -n "$age" ]; then
      echo "/afk: DRAIN DEAD — supervisor process not found, last tick ${age}m ago (run /afk --off to clear, or the watchdog will respawn it)"
    else
      echo "/afk: DRAIN DEAD — supervisor process not found, no heartbeat (run /afk --off to clear, or the watchdog will respawn it)"
    fi
    return 0
  fi
  last="$(_afk_read_last_action)"
  hb="$(afk_read_heartbeat)"; tick="$(afk_heartbeat_epoch "$hb")"
  case "$tick" in '' | *[!0-9]*) tick="" ;; esac
  if [ -n "$tick" ]; then
    age=$(( now - tick ))
    idle_limit=$(( AFK_TICK_SECONDS + ${AFK_LAND_HEARTBEAT_SECONDS:-30} ))
    if [ "$age" -gt "$idle_limit" ]; then
      echo "/afk: STALLED — no progress in $(( age / AFK_TICK_SECONDS )) ticks (${age}s stale, last action: ${last:-none}); a wedged supervisor is killed + respawned by the watchdog"
      return 0
    fi
    nxt=$(( AFK_TICK_SECONDS - age )); [ "$nxt" -lt 0 ] && nxt=0
    if [ "$state" = "drain" ]; then
      echo "/afk: draining — idle, next tick in ${nxt}s (last action: ${last:-none})"; return 0
    fi
    if window_expired "$state" "$now"; then echo "/afk: window elapsed (supervisor will stop on its next tick)"; return 0; fi
    rem="$(minutes_remaining "$state" "$now")"
    echo "/afk: on — idle, ${rem}m remaining, next tick in ${nxt}s (last action: ${last:-none})"
    return 0
  fi
  # No parseable heartbeat epoch but the pid is live (rare) — the plain lines.
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
    --status | --off | --once | --reconcile | -h | --help) ;;
    *) [[ "${BASH_SOURCE[0]}" == "${0}" ]] && _afk_exec_self_copy "$@" ;;
  esac

  # Subcommands that do not start the LOCAL supervisor loop.
  case "${1:-}" in
    --status)    _status; return 0 ;;
    --off)       afk_clear_state; echo "/afk: off (state cleared; the supervisor + watchdog stop on their next tick)"; return 0 ;;
    --watchdog)  watchdog_loop; return $? ;;
    --reconcile) afk_reconcile "$MAIN_ROOT"; return $? ;;
    -h|--help)   sed -n '2,82p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
  esac

  local once=0
  if [ "${1:-}" = "--once" ]; then
    once=1
  elif [ -n "${1:-}" ]; then
    # A window spec: compute + persist the end bound before the first tick.
    local end
    end="$(compute_end_epoch "$@" "$(afk_now)")" || { log "unrecognized window: '$*' (use <duration>, 'until HH:MM', or 'drain')"; return 2; }
    # Arm preconditions BEFORE arming (#170 ST4): refuse loudly on a live supervisor, a
    # dirty tree, an off-base HEAD, or dead gh auth — the drain's own prerequisites, checked
    # the same refuse-to-arm way the telemetry preflight checks the pipeline's.
    afk_arm_preconditions "$MAIN_ROOT" || return 2
    # Telemetry preflight BEFORE arming: an unattended drain must not dispatch spokes into
    # a dead telemetry pipeline (the dashboard is the SSOT). Refuse to arm — write no state,
    # never reach the loop — when collector/bridge/auth can't be wired (#108).
    afk_telemetry_preflight "$MAIN_ROOT" || return 2
    afk_write_state "$end"
    _clear_dispatch_epochs   # fresh window ⇒ empty "dispatched by this run" set
    _clear_progress_state    # fresh window ⇒ no stale progress / answer-attempt epochs
    _clear_resume_markers    # fresh window ⇒ every spoke gets its one auto-resume again
    _clear_redispatch_markers # fresh window ⇒ every clean crash gets its one re-dispatch again (#202 C)
    _afk_clear_landed_count  # fresh window ⇒ the landed tally starts at zero (#150)
    _afk_clear_drain_complete # ...and drop any un-consumed completion signal from a prior drain
    _clear_blocked_records   # fresh window ⇒ --status shows only THIS run's durable blocks
    _afk_clear_dispatch_fail_counts # fresh window ⇒ every issue's dispatch ceiling resets (#170)
    _afk_clear_land_retry_counts # fresh window ⇒ every issue's land-retry budget resets (#202 D)
    _afk_clear_last_action   # fresh window ⇒ no stale last-action label from a prior drain (#202 B)
    _afk_clear_status_labels_seed # fresh window ⇒ re-seed the afk:* label set once (#223)
    log "/afk: armed ($([ "$end" = drain ] && echo 'drain — until the backlog is empty' || echo "until $(wt_date_ymd "$end") $(date -r "$end" +%H:%M 2>/dev/null || date -d "@$end" +%H:%M)"))"
  else
    # No window spec and not --once: a RESUME of the persisted window (a watchdog respawn or
    # a manual re-run). Refuse if a supervisor is ALREADY live — a second one clobbers the
    # per-run state (#202 B, the arm-precondition dedup extended to the resume path). The
    # arm path already refuses this via afk_arm_preconditions; AFK_ARM_PRECHECK=0 opts out.
    if [ "${AFK_ARM_PRECHECK:-1}" != "0" ] && [ "$(afk_supervisor_state)" = "live" ]; then
      log "/afk: refusing to resume — a supervisor is already live (heartbeat pid running); run /afk --off first (a second supervisor clobbers per-run state)"
      return 2
    fi
  fi

  while :; do
    afk_write_heartbeat   # stamp this tick before working, so a crash mid-tick is visible
    # Keep exactly one watchdog alive (idempotent: a no-op while one runs, respawns it if
    # it died). Doing this each tick — not just at arm — means the supervisor and watchdog
    # heal each other: neither is a single silent point of failure (#107). Skipped for
    # --once (a one-shot cron tick must not leave a background keeper behind).
    [ "$once" -eq 0 ] && _afk_spawn_watchdog
    # A wake (USR1 during the last sleep) runs the targeted announce-driven pass; a full
    # tick (the sleep ran out, or --once) runs the whole sweep. Either way slot_state
    # re-derives, so the two never disagree — a wake is just an early, narrower tick.
    if [ "$_AFK_WOKEN" -eq 1 ] && [ "$once" -eq 0 ]; then
      _AFK_WOKEN=0
      service_event_wake
    else
      supervise_tick
    fi
    if [ "$_AFK_AUTH_FAILED" -eq 1 ]; then
      # #241 §9: auth no longer STOPS the drain. Halt dispatch (the short-circuits already do),
      # warn the in-flight spokes, re-probe, and resume the moment auth recovers. Never break.
      _afk_service_auth_halt
    fi
    [ "$once" -eq 1 ] && break
    if afk_done "$(afk_read_state)" "$(afk_now)"; then
      log "/afk: done"; _afk_emit_drain_complete; afk_clear_state; break
    fi
    # Stamp AFTER the tick's work — including afk_done's up-to-AFK_PLANNER_TIMEOUT planner call,
    # which runs unstamped — so the epoch is fresh going into the idle sleep and a healthy idle
    # drain reads `idle` (age ≤ tick), never a false STALLED (#202 B review). Skip the wait when
    # a signal already arrived (during the pass) so the pending event is serviced immediately.
    if [ "$_AFK_WOKEN" -ne 1 ]; then
      afk_write_heartbeat
      afk_interruptible_sleep "$AFK_TICK_SECONDS"
    fi
  done
  return 0
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
