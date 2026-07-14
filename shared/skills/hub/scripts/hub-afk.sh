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
#   AFK_OFF_WAIT_SECONDS=30      `--off --wait` bound: seconds to poll the supervisor pid to
#                                death before giving up (returns nonzero on timeout) (#252)
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
#   AFK_SELFUPDATE_SCOPE         basenames whose land triggers a self-update redeploy (#250);
#                                default: the supervisor's own scripts + the afk-answering rule.
#                                A supervisor-scope land re-syncs + re-execs the drain in place
#                                onto the new code at the next tick boundary (see the self-update
#                                block below); AFK_SYNC_CMD / AFK_SELFUPDATE_SMOKE_CMD are seams.
#   AFK_ARM_PRECHECK=1           arm-precondition gate (=0 skips live/dirty/branch/gh-auth checks)
#   AFK_AUTH_PROBE_CMD           auth probe: reap-time AND the #241 §9 per-tick auth-halt re-probe (default: a bounded headless claude no-op)
#   AFK_NET_PROBE_URL            #249 reachability probe target (default: https://api.anthropic.com)
#   AFK_NET_PROBE_TIMEOUT=10     seconds bounding the #249 reachability probe (via _afk_with_timeout)
#   AFK_NET_PROBE_CMD            override the whole reachability probe (tests); AFK_CURL_BIN overrides `curl`.
#                                Network-down is distinguished from auth-dead ahead of "token dead":
#                                on a blackout the reap pass is SKIPPED this tick and idle clocks are
#                                refreshed, so a merely-offline fleet is not mis-blocked (#249)
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
#   hub-afk.sh --off --wait      # ...and BLOCK until the supervisor has actually exited
#                                #   (bounded by AFK_OFF_WAIT_SECONDS; nonzero on timeout) —
#                                #   for a scripted off->sync->arm recycle (-w is an alias)
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

# The arm-GENERATION token THIS supervisor is bound to (issue #252). Empty until main() arms
# (mint a fresh token) or resumes (adopt the persisted one); the loop steps down the instant the
# on-disk token no longer matches, so a fast off/re-arm recycle can't leave two lineages draining.
_AFK_ARM_EPOCH=""

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

# --- resolve telemetry-ingest-spoke.sh (#231 block-time outcome view build) ----
# On a terminal block the supervisor stamps outcome=blocked and rebuilds the spoke's
# Langfuse view so a never-landing spoke still carries an outcome tag (worktree-land.sh
# owns the landed path). Resolution mirrors worktree-lib above; AFK_INGEST_BIN wins for tests.
_AFK_INGEST_BIN=""
for _cand in \
  "${AFK_INGEST_BIN:-}" \
  "$SCRIPT_DIR/telemetry-ingest-spoke.sh" \
  "$SCRIPT_DIR/../../../../scripts/telemetry-ingest-spoke.sh" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/scripts/telemetry-ingest-spoke.sh}" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/telemetry-ingest-spoke.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then _AFK_INGEST_BIN="$_cand"; break; fi
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
afk_clear_state() { rm -f "$(afk_state_file)" 2>/dev/null || true; afk_clear_heartbeat; afk_clear_arm_epoch; }

# --- arm-generation token (issue #252) ----------------------------------------
# A fast `--off -> re-arm` recycle could leave the OLD (mid-tick-sleep) supervisor draining
# alongside the new one: `--off` cleared `.afk-state`, but the re-arm re-created it before the
# old sleeper woke (it only re-reads state at each tick top), so the sleeper read the NEW window
# and kept ticking -- two lineages that can double-dispatch and double-land. So each supervisor
# binds to an arm-GENERATION token at startup (_AFK_ARM_EPOCH) and steps down the instant the
# on-disk token no longer matches: a FRESH arm mints a new token, a RESUME (watchdog respawn /
# reconcile) ADOPTS the current one, and `--off` clears it (afk_clear_state above). This is
# directive-4's "armed epoch the old supervisor can distinguish from a new arm".
# AFK_ARM_EPOCH_FILE overrides the path for tests; the default lives under the git common dir
# beside .afk-state, so it survives a watchdog respawn exactly as the window bound does.
afk_arm_epoch_file() {
  if [ -n "${AFK_ARM_EPOCH_FILE:-}" ]; then printf '%s\n' "$AFK_ARM_EPOCH_FILE"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/.afk-arm-epoch"
}
afk_read_arm_epoch()  { local f; f="$(afk_arm_epoch_file)"; [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' || true; }
afk_write_arm_epoch() { _afk_atomic_write "$(afk_arm_epoch_file)" "$1" || true; }
afk_clear_arm_epoch() { rm -f "$(afk_arm_epoch_file)" 2>/dev/null || true; }
# afk_new_arm_token -> a fresh generation token "<epoch>.<pid>". The arming pid disambiguates a
# same-second recycle: two arms in one wall-clock second still mint distinct generations.
afk_new_arm_token() { printf '%s.%s\n' "$(afk_now)" "$$"; }
# afk_arm_superseded -> true when the on-disk generation no longer matches the one THIS supervisor
# bound to at startup (a newer arm overwrote it, or `--off` cleared it). A legacy resume with no
# bound token and no epoch file reads empty==empty -> NOT superseded, so a pre-#252 armed window
# still runs after an upgrade.
afk_arm_superseded() { [ "$(afk_read_arm_epoch)" != "$_AFK_ARM_EPOCH" ]; }

# --- synchronous off (issue #252) ---------------------------------------------
# `--off` clears state ASYNCHRONOUSLY — the supervisor only exits at its next tick. A scripted
# recycle (the #250 self-update off->sync->arm) needs a BLOCKING off so it never races the old
# lineage. afk_wait_supervisor_gone <pid> [heartbeat-line] polls the (pre-clear) heartbeat pid to
# death, bounded by AFK_OFF_WAIT_SECONDS: returns 0 the instant the pid is gone (or was never
# alive / non-numeric), 1 on timeout. A wake-capable supervisor (the `wake1` heartbeat token,
# #207) is SIGUSR1-nudged first so it re-checks the loop-top supersede at once — with the
# arm-epoch cleared it steps down within ~a stamp, not a full tick. A pre-#176 supervisor stamps
# no wake token and is left to exit on its own tick (its default USR1 action is terminate).
: "${AFK_OFF_WAIT_SECONDS:=30}"
afk_wait_supervisor_gone() {
  local pid="$1" hb="${2:-}" limit waited
  case "$pid" in '' | *[!0-9]*) return 0 ;; esac
  _afk_pid_alive "$pid" || return 0
  limit="${AFK_OFF_WAIT_SECONDS:-30}"; case "$limit" in '' | *[!0-9]*) limit=30 ;; esac
  case "$hb" in *' wake1'*) kill -USR1 "$pid" 2>/dev/null || true ;; esac
  waited=0
  while [ "$waited" -lt "$limit" ]; do
    _afk_pid_alive "$pid" || return 0
    sleep 1 2>/dev/null || true
    waited=$(( waited + 1 ))
  done
  ! _afk_pid_alive "$pid"
}

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
# --- #231 terminal-outcome + failure-economics counts -------------------------
# The supervisor knows a spoke's terminal state (parked = blocked) and its relaunch history —
# state a landed spoke's clean trace can't distinguish. Post-#241 the reaper never abandons; a
# stuck spoke lands in _warn_parked_last (revive-first, warned-parked-LAST), so THAT is the live
# "disaster" terminal path these best-effort helpers hook, not the retired escalate path. They
# stamp the worktree's .ai-toolkit pointers the view builder reads (outcome tag + blocked/relaunch
# counts); a torn-down / unwritable worktree is a silent no-op, never failing a tick.

# _afk_stamp_outcome <wt> <outcome> -> record the spoke's terminal outcome pointer (#231).
_afk_stamp_outcome() {
  [ -d "$1/.ai-toolkit" ] || return 0
  _afk_atomic_write "$1/.ai-toolkit/outcome" "$2" || true
}

# _afk_bump_count <wt> <basename> -> increment a .ai-toolkit integer count pointer from 0 (#231).
_afk_bump_count() {
  local file="$1/.ai-toolkit/$2" cur
  [ -d "$1/.ai-toolkit" ] || return 0
  cur="$(head -n1 "$file" 2>/dev/null | tr -dc '0-9')"
  [ -n "$cur" ] || cur=0
  _afk_atomic_write "$file" "$(( cur + 1 ))" || true
}

# _afk_build_outcome_view <wt> -> best-effort assemble the spoke's Langfuse view now that its
# terminal outcome is stamped (#231), so a never-landing (blocked) spoke still gets an
# outcome-tagged trace. Bounded + self-gating (telemetry-ingest-spoke.sh no-ops without the OTel
# raw-bodies dir or Langfuse auth); --rebuild refreshes any partial view an earlier tick posted.
_afk_build_outcome_view() {
  [ -n "$_AFK_INGEST_BIN" ] || return 0
  _afk_with_timeout "${AFK_INGEST_TIMEOUT:-120}" \
    bash "$_AFK_INGEST_BIN" "$1" --rebuild >/dev/null 2>&1 || true
}

# _afk_park_terminal <wt> -> stamp outcome=blocked + bump blocked-count + rebuild the view ONCE
# per park episode (#231). _warn_parked_last fires on every DUE tick of a stuck spoke, so a
# .ai-toolkit/blocked-episode marker gates the count bump + the expensive view build to the FIRST
# park of an episode; _afk_clear_park_episode (called on every relaunch) reopens it so a re-park
# after a revival counts again. A spoke that later lands overwrites outcome=landed (the counts
# persist as the disaster-that-eventually-landed economics). No-op for a worktree-less park.
_afk_park_terminal() {
  local flag="$1/.ai-toolkit/blocked-episode"
  [ -d "$1/.ai-toolkit" ] || return 0
  _afk_stamp_outcome "$1" blocked   # idempotent: same value re-written each park tick
  [ -f "$flag" ] && return 0        # this park episode already counted + built
  _afk_bump_count "$1" blocked-count
  : > "$flag" 2>/dev/null || true
  _afk_build_outcome_view "$1"
}

# _afk_clear_park_episode <wt> -> drop the blocked-episode marker on a relaunch (#231), so the
# spoke's NEXT park counts as a fresh block episode.
_afk_clear_park_episode() {
  rm -f "$1/.ai-toolkit/blocked-episode" 2>/dev/null || true
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

# --- #256: the ledger completion signal before a time-ceiling reap ------------
# The wall-clock ceiling used to revive (kill + relaunch) EVERY over-ceiling spoke with no
# check for whether it was essentially DONE — so #241 was reaped at 33/33 todos, all committed
# and pushed, one step from ready. A near-complete task ledger is the "am I done?" signal the
# clean-pushed check (_afk_pushed_but_unmarked) misses when the tree is not a pristine
# HEAD==@{upstream} (a .testmondata-wal artifact, a final unpushed commit). It routes the spoke
# to a finish-up nudge (emit ready / final push) instead of a blind kill.

# AFK_LEDGER_DONE_PCT: a task ledger is "near-complete" when at least this % of its todos are
# completed (default 90 — all-but-a-few of a long ledger, or a fully-complete short one, while a
# low-progress runaway like 5/33=15% stays below). Only a plain 1..100 integer is honored: a
# bareword would break the integer test in _afk_ledger_near_complete, and an out-of-range value
# (>100) would overflow `total * PCT` negative and read EVERY spoke as near-complete — inverting
# AC2. The length bound (<=3 digits) keeps the range compare itself from erroring on a huge
# override; anything outside falls back to the default.
: "${AFK_LEDGER_DONE_PCT:=90}"
case "$AFK_LEDGER_DONE_PCT" in
  '' | *[!0-9]*) AFK_LEDGER_DONE_PCT=90 ;;
  *) { [ "${#AFK_LEDGER_DONE_PCT}" -le 3 ] && [ "$AFK_LEDGER_DONE_PCT" -ge 1 ] \
       && [ "$AFK_LEDGER_DONE_PCT" -le 100 ]; } || AFK_LEDGER_DONE_PCT=90 ;;
esac

# _afk_ledger_done_total <wt> -> "<done> <total>" for the spoke's task ledger, or nothing when no
# ledger is readable. It reconstructs the ledger from the newest transcript exactly as
# hub-status.sh:todos_for_path does — the Tasks system (TaskCreate/TaskUpdate tool_result pairs)
# with the last TodoWrite snapshot as the older-runtime fallback. The parse is DUPLICATED from
# that reader (trimmed to the counts) because #256's Scope confines edits to hub-afk.sh; keep the
# two copies in sync — if the transcript ledger shape changes, update todos_for_path AND this.
_afk_ledger_done_total() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  _AFK_LEDGER_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json
import os

tasks = {}          # task id -> {"status"}, insertion-ordered (Tasks system)
create_uses = set()  # TaskCreate tool_use ids awaiting their tool_result
update_uses = {}     # TaskUpdate tool_use id -> input (taskId fallback)
todos = None         # last TodoWrite snapshot (older-runtime fallback)
try:
    with open(os.environ["_AFK_LEDGER_JSONL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            typ = obj.get("type")
            content = (obj.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            if typ == "assistant":
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    name = block.get("name")
                    if name == "TodoWrite":
                        todos = (block.get("input") or {}).get("todos") or []
                    elif name == "TaskCreate":
                        create_uses.add(block.get("id"))
                    elif name == "TaskUpdate":
                        update_uses[block.get("id")] = block.get("input") or {}
            elif typ == "user":
                tur = obj.get("toolUseResult")
                if not isinstance(tur, dict):
                    continue
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                        continue
                    uid = block.get("tool_use_id")
                    if uid in create_uses:
                        tid = (tur.get("task") or {}).get("id")
                        if tid is not None:
                            tasks[str(tid)] = {"status": "pending"}
                    elif uid in update_uses:
                        tid = str(tur.get("taskId") or update_uses[uid].get("taskId") or "")
                        new = (tur.get("statusChange") or {}).get("to")
                        if tid in tasks and new:
                            if new == "deleted":
                                del tasks[tid]
                            else:
                                tasks[tid]["status"] = new
except Exception:
    pass

if tasks:
    entries = list(tasks.values())
elif todos is not None:
    entries = [t for t in todos if isinstance(t, dict)]
else:
    entries = None

if entries:
    done = sum(1 for t in entries if t.get("status") == "completed")
    print(f"{done} {len(entries)}")
PYEOF
}

# _afk_ledger_near_complete <wt> -> rc 0 when the spoke's task ledger is readable, non-empty, and
# at least AFK_LEDGER_DONE_PCT% of its todos are completed (#256's "essentially done" signal). rc
# 1 on an unreadable / empty ledger or below-threshold progress — so a genuine runaway (no ledger,
# or low progress) is NEVER mistaken for a finishing spoke and stays reapable (AC2).
_afk_ledger_near_complete() {
  local out done total
  out="$(_afk_ledger_done_total "$1")" || return 1
  [ -n "$out" ] || return 1
  done="${out%% *}"; total="${out##* }"
  case "$done$total" in '' | *[!0-9]*) return 1 ;; esac
  [ "$total" -gt 0 ] || return 1
  [ "$(( done * 100 ))" -ge "$(( total * AFK_LEDGER_DONE_PCT ))" ]
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

# --- #255: the finished-turn-idle continue-nudge counter ----------------------
# A spoke that FINISHED its turn and stopped at the input prompt (pane alive, no dialog,
# transcript ends on a completed assistant turn — _transcript_finished_turn_idle) is NUDGED (a
# continue message injected into the LIVE session via the shared hardened injector) rather than
# killed + relaunched. Bounded: after AFK_NUDGE_MAX_ATTEMPTS nudges in one window the reaper
# falls back to the revive, so a spoke that will not resume is never nudged forever. The count
# is per-window (cleared on a fresh arm) like the resume/redispatch stamps.
: "${AFK_NUDGE_MAX_ATTEMPTS:=2}"
# Guard a non-numeric override (matching AFK_REANSWER_CEILING): a bareword would make the
# `[ count -lt $AFK_NUDGE_MAX_ATTEMPTS ]` test error and always fall through to the revive.
case "$AFK_NUDGE_MAX_ATTEMPTS" in '' | *[!0-9]*) AFK_NUDGE_MAX_ATTEMPTS=2 ;; esac
_afk_nudge_count_file() { printf '%s\n' "$(_afk_state_dir)/nudge-$1.count"; }
_afk_read_nudge_count() {
  local f n; f="$(_afk_nudge_count_file "$1")"
  n="$( [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' )"
  case "$n" in '' | *[!0-9]*) n=0 ;; esac
  printf '%s\n' "$n"
}
# _afk_incr_nudge_count <issue> -> bump and echo the new nudge count for this window.
_afk_incr_nudge_count() {
  local issue="$1" n
  mkdir -p "$(_afk_state_dir)" 2>/dev/null || true
  n=$(( $(_afk_read_nudge_count "$issue") + 1 ))
  _afk_atomic_write "$(_afk_nudge_count_file "$issue")" "$n" || true
  printf '%s\n' "$n"
}
_clear_nudge_counts() { rm -f "$(_afk_state_dir)"/nudge-*.count 2>/dev/null || true; }

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

# _afk_nudge_prompt <issue> -> the continue-nudge message for a finished-turn-idle spoke (#255).
# Unlike _afk_resume_prompt (a crash re-anchor for a relaunched session), this rides into the
# SAME live session: the spoke finished its turn and just stopped, so it only needs to be told
# nothing is blocking it and to pick the cycle back up — no re-anchor, no "your session crashed".
_afk_nudge_prompt() {
  local issue="$1"
  cat <<EOF
You finished your turn but stopped mid-cycle without continuing, and nothing is blocking you --
no question or permission dialog is pending. Re-read your task ledger and the working tree, then
continue the solo flow (RED -> GREEN -> REVIEW -> PUSH) from where you left off. Push each
subtask and emit the ready marker when the issue's acceptance criteria are all met. Do NOT
self-land -- the hub lands #$issue.
EOF
}

# _afk_finish_up_prompt <issue> -> the #256 finish-up nudge message for an over-ceiling spoke
# whose task ledger is near-complete: it is essentially DONE, so it is told to do the LAST step
# (verify committed + pushed, then emit ready / the final push), NOT to start fresh work.
_afk_finish_up_prompt() {
  local issue="$1" marker_dir
  # Name the marker-emitter path that EXISTS in the spoke's worktree (#271): `scripts` in the
  # ai-toolkit checkout, `.ai-toolkit/scripts` in a synced target — probed off the hub layout the
  # spoke shares. A hardcoded `.ai-toolkit/scripts/` here hands an ai-toolkit spoke a path the
  # deny-wall approves (textually in-tree) but that then fails to exec — the #271 failure mode.
  marker_dir="$(wt_marker_script_dir "${_AFK_TOPLEVEL:-.}")"
  cat <<EOF
You have run past the AFK time ceiling, but your task ledger shows you are essentially DONE --
almost every todo is complete and nothing is blocking you. Do the LAST step now: make sure your
work is committed and pushed, then emit the ready marker
(bash ${marker_dir}/spoke-push.sh --ready $issue) once the issue's acceptance criteria are
all met. If a final push is still pending, push it first. Do NOT start new work and do NOT
self-land -- the hub lands #$issue.
EOF
}

# _afk_conflict_resolve_prompt <issue> -> the #285 resolution message for a spoke whose land
# hit a DETERMINISTIC merge conflict (a sibling landed edits to a file this spoke also owns).
# The hub cannot resolve it — the spoke must merge the base branch on its side and re-push, so
# the hub re-lands on the fresh tip. Names the marker-emitter path that EXISTS in the spoke's
# worktree (the #271 probe) so the re-emit step doesn't hand it a path the deny-wall approves
# textually but that fails to exec.
_afk_conflict_resolve_prompt() {
  local issue="$1" marker_dir base
  marker_dir="$(wt_marker_script_dir "${_AFK_TOPLEVEL:-.}")"
  base="$(_afk_default_ref "${_AFK_TOPLEVEL:-.}")"; base="${base:-origin/main}"
  cat <<EOF
The hub could NOT land your branch: it CONFLICTS with $base because a sibling task landed
changes to a file you also edited. Your committed work is intact -- resolve the conflict ON
THE SPOKE so the hub can re-land on your fresh tip:
  1. git fetch origin
  2. merge the base branch into yours (git merge $base) and RESOLVE the conflicts
  3. re-run your tests to confirm green
  4. push your branch
  5. re-emit the ready marker: bash ${marker_dir}/spoke-push.sh --ready $issue
Do NOT self-land -- the hub lands #$issue once your tip is mergeable again.
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
  _afk_bump_count "$wt" relaunch-count   # #231: a relaunch — failure economics vs a clean run
  _afk_clear_park_episode "$wt"          # #231: a fresh run may re-park → count the next block anew
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
  _afk_bump_count "$wt" relaunch-count   # #231: a relaunch — failure economics vs a clean run
  _afk_clear_park_episode "$wt"          # #231: a fresh run may re-park → count the next block anew
  _afk_emit_span "$wt" afk-wedge-respawn success
  return 0
}

# --- #243: hang-forensics capture before the reaper's revival kills the pane --------
# A live-but-frozen claude spoke (idle>AFK_IDLE_MINUTES with a live pane) is REVIVED by
# _revive_spoke — which kills the pane and relaunches (#241), DESTROYING the evidence a hang
# autopsy / upstream Claude Code report needs (process state, pane content, the wedged-input
# symptom). So just before the kill, capture a best-effort, BOUNDED bundle. A spoke whose tmux
# window is gone (no pane to observe) has nothing to capture and skips gracefully. Evidence
# collection ONLY — the revive itself is #241's job; this is the microscope, not the fix.
#
# Bundles intentionally OUTLIVE the drain: unlike per-window state (dispatch epochs, resume
# markers, warned records — all cleared on a fresh arm), a bundle is evidence the operator triages
# later, so it is NOT cleared on re-arm and afk_hang_forensics_status counts every bundle on disk.
# UPGRADE: add an age- or count-capped retention/prune policy if the bundle root grows unwieldy —
# each bundle holds a full pane scrollback (capture-pane -S -, itself bounded by tmux's own
# history-limit) plus a sample, so a long unattended run with many revives accumulates disk.

# _afk_hang_forensics_dir -> the bundle root <git-common-dir>/hang-forensics. AFK_HANG_FORENSICS_DIR
# overrides it for tests (mirrors _afk_state_dir).
_afk_hang_forensics_dir() {
  if [ -n "${AFK_HANG_FORENSICS_DIR:-}" ]; then printf '%s\n' "$AFK_HANG_FORENSICS_DIR"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/hang-forensics"
}

# _afk_hang_sample_pid <pane_pid> <descendant_pids...> -> the pid `sample` should profile: the
# AGENT process, NOT the pane's wrapper shell. The pane is launched as `sh -c "<cmd>; exec zsh"`
# (see _afk_open_spoke_window), so pane_pid is a shell blocked in wait4() and claude runs as its
# descendant — sampling pane_pid would only ever show the idle shell's wait loop. Prefer a
# descendant whose command is claude/node; else the first descendant (the pane shell's direct
# child is the launched claude); else pane_pid itself when there are no descendants.
_afk_hang_sample_pid() {
  local pane_pid="$1"; shift
  local pid comm first=""
  for pid in "$@"; do
    [ -n "$first" ] || first="$pid"
    comm="$(LC_ALL=C ps -o comm= -p "$pid" 2>/dev/null)"
    case "$comm" in *[Cc]laude* | *node*) printf '%s\n' "$pid"; return 0 ;; esac
  done
  printf '%s\n' "${first:-$pane_pid}"
}

# _afk_capture_proc_tree <pane_pid> <out> -> write the pane process tree's ps snapshot
# (pid/stat/etime/wchan) plus, on macOS, a short `sample` of the AGENT descendant, to <out>. The
# ps read forces LC_ALL=C (the repo's locale trap: parsing localized columns silently strands the
# fields), and the sample is time-BOUNDED with a tight KILL grace so it can never delay the reap
# past the ~10s budget even under the coreutils-absent timeout fallback. Both best-effort: an empty
# pane_pid or a failed probe leaves an empty/partial file, never an error.
_afk_capture_proc_tree() {
  local pane_pid="$1" out="$2" pids secs target kids k
  case "$pane_pid" in '' | *[!0-9]*) return 0 ;; esac
  kids="$(_afk_descendant_pids "$pane_pid" | tr '\n' ' ')"
  pids="$pane_pid"
  for k in $kids; do pids="$pids,$k"; done
  LC_ALL=C ps -o pid,stat,etime,wchan -p "$pids" > "$out" 2>/dev/null || true
  command -v sample >/dev/null 2>&1 || return 0
  secs="${AFK_HANG_SAMPLE_SECONDS:-2}"; case "$secs" in '' | *[!0-9]*) secs=2 ;; esac
  target="$(_afk_hang_sample_pid "$pane_pid" $kids)"
  AFK_TIMEOUT_KILL_AFTER=2 _afk_with_timeout "$(( secs + 2 ))" sample "$target" "$secs" >> "$out" 2>/dev/null || true
}

# _afk_write_fingerprint <issue> <now> <mtime> <jsonl> -> emit the run fingerprint on stdout: the
# transcript-activity-vs-UI-freeze delta (the hang's tell), elapsed since dispatch, the claude
# version + model, and the OTEL env (the untested heavy-OTEL-logging correlation the issue flags).
# The version + model are read from the SPOKE's transcript (each JSONL line carries both), NOT a
# `claude --version` fork — forking the very binary that may be hung risks wedging the reap tick,
# and the transcript reflects what the spoke actually ran. All best-effort; a missing field records
# `unknown` rather than aborting.
_afk_write_fingerprint() {
  local issue="$1" now="$2" mtime="$3" jsonl="$4" disp elapsed=unknown silence=unknown ver model
  disp="$(read_dispatch_epoch "$issue" | tr -d '[:space:]')"
  case "$disp" in '' | *[!0-9]*) : ;; *) elapsed=$(( now - disp )) ;; esac
  case "$mtime" in '' | *[!0-9]*) : ;; *) silence=$(( now - mtime )) ;; esac
  if [ -n "$jsonl" ]; then
    ver="$(grep -oE '"version"[[:space:]]*:[[:space:]]*"[^"]*"' "$jsonl" 2>/dev/null | tail -1 | sed 's/.*: *"//;s/"$//')"
    model="$(grep -oE '"model"[[:space:]]*:[[:space:]]*"[^"]*"' "$jsonl" 2>/dev/null | tail -1 | sed 's/.*: *"//;s/"$//')"
  fi
  printf 'issue=%s\ncaptured_epoch=%s\ntranscript_mtime=%s\ntranscript_silence_seconds=%s\nelapsed_since_dispatch_seconds=%s\n' \
    "$issue" "$now" "${mtime:-unknown}" "$silence" "$elapsed"
  printf 'claude_version=%s\nmodel=%s\n' "${ver:-unknown}" "${model:-unknown}"
  printf 'AI_TOOLKIT_OTEL=%s\nOTEL_EXPORTER_OTLP_ENDPOINT=%s\nAI_TOOLKIT_OTEL_SPAN_ENDPOINT=%s\nOTEL_RESOURCE_ATTRIBUTES=%s\n' \
    "${AI_TOOLKIT_OTEL:-}" "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" "${AI_TOOLKIT_OTEL_SPAN_ENDPOINT:-}" "${OTEL_RESOURCE_ATTRIBUTES:-}"
}

# _afk_capture_hang_forensics <wt> <issue> -> capture the hang bundle (best-effort, bounded) and
# ECHO its path; echo NOTHING when there is no live pane to capture (a crashed spoke — the AC1
# clean-reap skip). The bundle lands at <hang-forensics-dir>/<issue>-<epoch>/. Called from
# _revive_spoke BEFORE the kill, so the frozen pane + its process tree are still observable.
_afk_capture_hang_forensics() {
  local wt="$1" issue="$2" target pane_pid dir jsonl now mtime
  command -v tmux >/dev/null 2>&1 || return 0
  target="$(_spoke_pane_target "$wt")"
  [ -n "$target" ] || return 0            # no pane (window gone) — nothing to observe, skip gracefully
  now="$(afk_now)"
  dir="$(_afk_hang_forensics_dir)/$issue-$now"
  mkdir -p "$dir" 2>/dev/null || return 0
  pane_pid="$(tmux display-message -p -t "$target" '#{pane_pid}' 2>/dev/null | tr -d '[:space:]')"
  _afk_capture_proc_tree "$pane_pid" "$dir/process-tree.txt"
  tmux capture-pane -p -S - -t "$target" > "$dir/pane.txt" 2>/dev/null || true
  tmux display-message -p -t "$target" \
    'pane_in_mode=#{pane_in_mode} pane_current_command=#{pane_current_command}' \
    > "$dir/pane-meta.txt" 2>/dev/null || true
  jsonl="$(_spoke_jsonl "$wt")"
  if [ -n "$jsonl" ]; then
    tail -n 50 "$jsonl" > "$dir/transcript-tail.jsonl" 2>/dev/null || true
    mtime="$(stat -f %m "$jsonl" 2>/dev/null || stat -c %Y "$jsonl" 2>/dev/null)"
  fi
  _afk_write_fingerprint "$issue" "$now" "${mtime:-}" "$jsonl" > "$dir/fingerprint.txt" 2>/dev/null || true
  printf '%s\n' "$dir"
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
  # Gate on the SAME lane broker_warn_continue arms for this park kind (#274): a land/review park
  # reads/arms auto_land's LAND lane, every other kind the default lane — so the due-check and the
  # arm stay on one clock. Inside the backoff → parked LAST silently this tick.
  _afk_warned_due "$issue" "" "$(_afk_warned_lane "$park")" || return 0
  log "→ warn-park-LAST #$issue: $reason"
  _afk_set_last_action "warn-park #$issue"
  broker_warn_continue "$wt" "$issue" "$park" "$reason" reversible
  # #231: this IS the live disaster-terminal path post-#241 — stamp outcome=blocked + build the
  # view (once per episode) so a never-landing spoke is distinguishable from a clean landing.
  _afk_park_terminal "$wt"
}

# _revive_spoke <wt> <issue> -> kill any hung/crashed window and relaunch the spoke via
# `claude --continue` under the same spoke_run_id, resetting the reap + idle clocks (#133/#202
# C: the fresh window hasn't written a transcript yet, so stamp the answer-attempt epoch or the
# same-tick reap_pass re-reaps it as idle). Marks the once-per-window revival. rc 1 when the
# window could not be opened (the caller warns + retries next tick).
_revive_spoke() {
  local wt="$1" issue="$2" bundle
  log "→ revive #$issue: killing any hung/crashed pane and relaunching (claude --continue)"
  _afk_set_last_action "revive #$issue"
  # #243: capture the hang forensics BEFORE the kill destroys them (a live pane leaves a bundle;
  # a spoke whose window is already gone echoes nothing and is skipped). Best-effort — a failed
  # capture never blocks the revive. (Also fires on the over-ceiling revive path; over-capture is
  # harmless and the fingerprint's silence delta distinguishes a real hang from a merely-slow run.)
  bundle="$(_afk_capture_hang_forensics "$wt" "$issue")"
  _kill_spoke_window "$issue"
  if ! _afk_open_spoke_window "$wt" "$issue" "$(_afk_resume_command "$wt" "$issue")"; then
    log "  could not open a revive window for #$issue"
    return 1
  fi
  _afk_mark_resumed "$issue"
  stamp_progress_epoch "$issue"
  stamp_answer_attempt "$issue"
  # #241 §10: a revival is a taken decision the morning review sees — journal it (a successful
  # revival is not a loud warned record, just an auditable journal line + span). #243: name the
  # forensics bundle in the journal line so the morning review can open it.
  broker_journal_decision "$issue" revive \
    "revived a hung/crashed pane (killed + relaunched claude --continue)${bundle:+ — hang forensics: $bundle}" reversible
  _afk_bump_count "$wt" relaunch-count   # #231: a relaunch — failure economics vs a clean run
  _afk_clear_park_episode "$wt"          # #231: a fresh run may re-park → count the next block anew
  _afk_emit_span "$wt" afk-revive success
  return 0
}

# _afk_nudge_spoke <wt> <issue> -> deliver a continue-nudge into the spoke's LIVE session via the
# shared hardened injector (inject_and_verify: paste-buffer + verified submit — the SAME primitive
# the answerer uses), then journal the taken decision (#255). Unlike a revive, a nudge does NOT
# reset the wall-clock reap ceiling (progress epoch): the caller only reaches here UNDER the
# ceiling, and an answer-attempt-shaped action must not buy a spoke a fresh full ceiling (cf. the
# #241 §8 "answer attempts must not reset the reap clock" note). It DOES stamp the answer-attempt
# epoch so the same-tick / next-tick reap does not immediately re-reap the just-nudged spoke off a
# stale transcript mtime (#202 C). rc 0 when the nudge delivered, else inject_and_verify's rc (the
# caller has already counted the attempt; a failed delivery just retries next tick until the
# budget falls back to a revive). Caller wraps this in _afk_run_with_heartbeat_fg because
# inject_and_verify polls up to AFK_INJECT_VERIFY_SECONDS.
_afk_nudge_spoke() {
  local wt="$1" issue="$2" target rc
  log "→ nudge #$issue: finished-turn-idle — injecting a continue message into the live session (no relaunch)"
  _afk_set_last_action "nudge #$issue"
  target="$(_spoke_pane_target "$wt")"
  if [ -z "$target" ]; then
    log "  no live pane for #$issue — cannot nudge"
    return 1
  fi
  stamp_answer_attempt "$issue"
  inject_and_verify "$wt" "$target" "$(_afk_nudge_prompt "$issue")"; rc=$?
  broker_journal_decision "$issue" nudge \
    "finished-turn-idle: injected a continue-nudge into the live session (no relaunch)" reversible
  if [ "$rc" -eq 0 ]; then _afk_emit_span "$wt" afk-nudge success; else _afk_emit_span "$wt" afk-nudge retry; fi
  return "$rc"
}

# _afk_finish_up_nudge <wt> <issue> -> #256: an over-ceiling spoke whose ledger is near-complete
# gets a FINISH-UP nudge (emit ready / final push) injected into its LIVE session, instead of the
# kill + relaunch a blind ceiling reap would do. Mirrors _afk_nudge_spoke but carries the finish-up
# prompt and journals a DISTINCT `finish-up` decision + span, so the morning review sees "ceiling
# hit -> nudged to finish, not reaped" (AC3). Like the #255 nudge it stamps only the answer-attempt
# epoch, never the progress epoch — a finishing spoke must not buy a fresh full ceiling. rc mirrors
# inject_and_verify (the caller already counted the attempt; a failed delivery retries next tick
# until the shared nudge budget falls back to the revive).
_afk_finish_up_nudge() {
  local wt="$1" issue="$2" target rc
  log "→ finish-up #$issue: over the time ceiling but ledger near-complete — nudging it to emit ready / final push (no relaunch)"
  _afk_set_last_action "finish-up #$issue"
  target="$(_spoke_pane_target "$wt")"
  if [ -z "$target" ]; then
    log "  no live pane for #$issue — cannot nudge"
    return 1
  fi
  stamp_answer_attempt "$issue"
  inject_and_verify "$wt" "$target" "$(_afk_finish_up_prompt "$issue")"; rc=$?
  broker_journal_decision "$issue" finish-up \
    "time ceiling hit but ledger near-complete — nudged to finish (emit ready / final push), not reaped (#256)" reversible
  if [ "$rc" -eq 0 ]; then _afk_emit_span "$wt" afk-finish-up success; else _afk_emit_span "$wt" afk-finish-up retry; fi
  return "$rc"
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

# _afk_finish_up_or_revive <wt> <issue> <reason> -> #256: the ceiling-hit decision. A spoke over
# the wall-clock ceiling is NOT automatically a runaway — if it shows a COMPLETION signal it is
# essentially DONE and one step from ready, so a blind revive (kill + relaunch) throws away
# finished work (the 2026-07-12 #241 incident: reaped at 33/33 todos, all committed + pushed).
# Prefer a finish-up nudge / the pushed-but-unmarked warn over a kill; only a spoke over the
# ceiling with NO completion signal is a true runaway to revive. Both signal branches are
# pane-alive-gated, so at the recover_dead_panes call site (dead pane) they collapse to the
# revive below — crashed-pane behavior is unchanged.
_afk_finish_up_or_revive() {
  local wt="$1" issue="$2" reason="$3"
  # Signal 1: a clean pushed-ahead tip with no marker (#200) — surface it actionably (re-run
  # --ready / land by hand), never kill. (In _reap_or_resume the live case is already caught
  # upstream; keeping it here makes the fn self-contained + correct for both call sites.)
  if _spoke_pane_alive "$wt" && _afk_pushed_but_unmarked "$wt" "$issue"; then
    _afk_warn_pushed_but_unmarked "$wt" "$issue"
    return 0
  fi
  # Signal 2: a near-complete task ledger on a finished-turn-idle pane — nudge it to finish up
  # (emit ready / final push) rather than relaunch. Gated on _transcript_finished_turn_idle so the
  # pane is genuinely at the prompt and the nudge can land (a near-complete-but-hung pane falls
  # through to the revive). Bounded by the SHARED per-window nudge budget (#255) so a spoke that
  # will not finish still falls through to the revive.
  if _spoke_pane_alive "$wt" \
     && _afk_ledger_near_complete "$wt" \
     && _transcript_finished_turn_idle "$wt" \
     && [ "$(_afk_read_nudge_count "$issue")" -lt "$AFK_NUDGE_MAX_ATTEMPTS" ]; then
    _afk_incr_nudge_count "$issue" >/dev/null
    _afk_run_with_heartbeat_fg _afk_finish_up_nudge "$wt" "$issue"
    return 0
  fi
  # No completion signal (low progress, no pushed work) — a true runaway. Revive-first, park-LAST.
  _afk_revive_or_park_last "$wt" "$issue" "$reason"
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
  # #246 defense-in-depth: a spoke still parked on an answerable dialog (a permission prompt, a
  # PLAN gate, or an extractable question) must be ANSWERED, not revived — reviving via
  # `claude --continue` only re-raises the identical dialog (the parked->reaped->revived->parked
  # loop). slot_state already keeps a detected park out of `reap`, so this only fires on a
  # same-tick slot_state flicker (answer_pass and reap_pass re-derive state independently) or a
  # future regression. _spoke_still_parked is a POSITIVE signal, so an ambiguous read falls
  # through to the revive logic below — a genuinely hung, unparked pane is unaffected (#246 item 4).
  # Wrapped in _afk_run_with_heartbeat_fg like answer_pass's identical call (#170 ST2): the
  # answerer is a high-effort headless `claude` that can run for minutes, so without the heartbeat
  # stamper the --watchdog would declare the supervisor wedged and respawn it mid-answer.
  if _spoke_still_parked "$wt" "$issue"; then
    _afk_run_with_heartbeat_fg decide_and_act "$wt" "$issue"
    return 0
  fi
  # #200/#241: a live pane at a clean-pushed tip with no marker is warned-and-parked-LAST with an
  # actionable reason (NOT auto-marked/auto-landed — the shape is ambiguous with idle-between-subtasks).
  if _spoke_pane_alive "$wt" && _afk_pushed_but_unmarked "$wt" "$issue"; then
    _afk_warn_pushed_but_unmarked "$wt" "$issue"
    return 0
  fi
  if _spoke_over_any_ceiling "$issue" "$(afk_now)"; then
    # #256: not automatically a runaway — a near-complete ledger / clean pushed-ahead tip is
    # nudged to finish up (or warned), not blind-revived; only a NO-signal spoke revives.
    _afk_finish_up_or_revive "$wt" "$issue" "time ceiling: ran >${AFK_SPOKE_MAX_MINUTES}m without finishing"
  elif _spoke_pane_alive "$wt" \
       && _transcript_finished_turn_idle "$wt" \
       && [ "$(_afk_read_nudge_count "$issue")" -lt "$AFK_NUDGE_MAX_ATTEMPTS" ]; then
    # #255: the FINISHED-TURN-IDLE class — the spoke finished its turn and stopped at the input
    # prompt (transcript ends on a completed assistant turn, no pending tool_use), distinct from a
    # pane frozen MID-TOOL_USE. It gets a lightweight continue-nudge into the LIVE session, NOT a
    # kill + relaunch — bounded to AFK_NUDGE_MAX_ATTEMPTS nudges per window, after which it falls
    # through to the revive below. Wrapped in _afk_run_with_heartbeat_fg (inject_and_verify polls
    # up to AFK_INJECT_VERIFY_SECONDS) like answer_pass's decide_and_act call.
    _afk_incr_nudge_count "$issue" >/dev/null
    _afk_run_with_heartbeat_fg _afk_nudge_spoke "$wt" "$issue"
  elif _spoke_pane_alive "$wt"; then
    # #241 §8: a live-but-frozen claude (hung mid-tool_use), or a finished-turn-idle spoke past its
    # nudge budget, is a REVIVAL case (kill the hung pane + relaunch), not a terminal block. answer
    # attempts must not reset the reap clock, so this is a revival, not a re-answer.
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
# CLEARS the flag (resuming the drain) the moment auth recovers. The re-probe is _afk_probe_state
# (#249): a network blackout is distinguished from real auth-death so a mere outage never reads as
# recovery (that would resume dispatch into a dead network) — see the tri-state branches below.
_afk_service_auth_halt() {
  # #249: the re-probe is the SECOND _afk_auth_is_dead caller, so it distinguishes network-down
  # the same way. If the network dropped while halted we CANNOT confirm recovery — a bare
  # `! _afk_auth_is_dead` would misread the connection error as "auth recovered" and resume the
  # drain into a dead network. On offline: stay halted, record the outage, refresh idle clocks,
  # re-check next tick. On alive: auth recovered — clear the flag + the outage marker and resume.
  afk_write_heartbeat   # the probes are bounded curl/`claude` calls — keep the epoch fresh
  case "$(_afk_probe_state)" in
    offline)
      _afk_note_offline_tick
      ;;
    alive)  # auth recovered — clear the flag + any outage marker and resume the drain
      _AFK_AUTH_FAILED=0
      clear_offline_since
      log "/afk: auth recovered — resuming the drain"
      ;;
    *)  # auth-dead: network up but the token is still dead — stay halted, warn the fleet again
      log "/afk: subscription auth failed — dispatch HALTED (re-run /login on the host); re-probing each tick, NOT stopping the drain (#241 §9)"
      _warn_all_inflight "subscription auth failed — dispatch halted; re-run /login on the host (retrying auth each tick)"
      ;;
  esac
}

# --- dispatch -----------------------------------------------------------------

# kickoff_for <issue> -> the spoke's first prompt: the standard ultra kickoff (the same
# handoff start-task / next-batch use). Under /afk the spoke runs in its normal attended
# posture — it pauses at its PLAN gate and asks questions as if a human were watching —
# and the supervisor's answerer plays the human. So the kickoff is deliberately the
# everyday one, NOT a "park, never ask" variant.
kickoff_for() {
  local n="$1" marker_dir
  # Name the marker-emitter path that EXISTS in the spoke's worktree (#271): `scripts` in the
  # ai-toolkit checkout, `.ai-toolkit/scripts` in a synced target. The spoke is cut from the hub
  # (_AFK_TOPLEVEL), which shares the layout, so probe it — a hardcoded `.ai-toolkit/scripts/`
  # nudge re-emits the nonexistent path this repo's deny-wall judges and cannot exec.
  marker_dir="$(wt_marker_script_dir "${_AFK_TOPLEVEL:-.}")"
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
\`bash ${marker_dir}/spoke-ready.sh --gate $n --plan-file .ai-toolkit/gate-plan.md\`
(or inline a short plan with \`--gate $n -m "<plan>"\`). That parks you at the gate; WAIT for
approval before writing code (before GREEN). If the gate is \`none\`, run autonomous straight through.

Then implement following the solo-cycle (/cycle: RED → GREEN → REVIEW → PUSH). Push your
own branch on every subtask without asking; when your ledger shows the issue's acceptance
criteria are all met, push the final subtask and emit the ready marker (bash
${marker_dir}/spoke-push.sh --ready $n) — also without asking. Still ask before
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

# --- #285: the conflicted-land resolution lane --------------------------------
# A DETERMINISTIC merge conflict (worktree-land exit WT_LAND_CONFLICT_EXIT=4) is a pure
# function of the two tips: re-running the identical land is futile until one tip moves. So
# auto_land records a per-issue, per-window fingerprint "<branch_tip> <main_tip>" and, while it
# is UNCHANGED, does NOT re-run the expensive land — it routes to a resolution lane instead
# (relaunch a reaped spoke reusing its spoke_run_id, or inject a live one, with a merge the base
# branch -> resolve -> re-push -> re-emit ready instruction). When the spoke resolves and the tip
# moves, the fingerprint no longer matches and auto_land re-lands on the fresh tip.
: "${WT_LAND_CONFLICT_EXIT:=4}"
case "$WT_LAND_CONFLICT_EXIT" in '' | *[!0-9]*) WT_LAND_CONFLICT_EXIT=4 ;; esac

_afk_land_conflict_fp_file() { printf '%s\n' "$(_afk_state_dir)/land-conflict-$1"; }
_afk_read_land_conflict_fp() {
  local f; f="$(_afk_land_conflict_fp_file "$1")"
  [ -f "$f" ] && head -n1 "$f" 2>/dev/null || true
}
_afk_write_land_conflict_fp() {
  local issue="$1" fp="$2"
  mkdir -p "$(_afk_state_dir)" 2>/dev/null || true
  _afk_atomic_write "$(_afk_land_conflict_fp_file "$issue")" "$fp" || true
}
_afk_clear_land_conflict_fp() { rm -f "$(_afk_land_conflict_fp_file "$1")" 2>/dev/null || true; }
_afk_clear_land_conflict_fps() { rm -f "$(_afk_state_dir)"/land-conflict-* 2>/dev/null || true; }

# _afk_land_conflict_fingerprint <wt> -> "<branch_tip> <main_tip>": the pair a conflict is a pure
# function of. main_tip is the local default branch the land merges INTO (_afk_local_default_sha),
# so a sibling land advancing main is detected as a moved fingerprint too.
_afk_land_conflict_fingerprint() {
  local wt="$1" bt mt
  bt="$(git -C "$wt" rev-parse HEAD 2>/dev/null || true)"
  mt="$(_afk_local_default_sha)"
  printf '%s %s\n' "$bt" "$mt"
}
# _afk_land_conflict_unchanged <wt> <issue> -> true when a recorded conflict fingerprint exists
# AND the current tips still match it (an identical re-land would deterministically re-conflict).
_afk_land_conflict_unchanged() {
  local wt="$1" issue="$2" prev
  prev="$(_afk_read_land_conflict_fp "$issue")"
  [ -n "$prev" ] || return 1
  [ "$prev" = "$(_afk_land_conflict_fingerprint "$wt")" ]
}

# The resolution-lane budget is DISTINCT from the crash-resume budget (_afk_resumed_marker): a
# conflict revive must neither consume nor be starved by the once-per-window crash-resume stamp.
# It records the SPOKE branch tip at dispatch, so a re-land triggered by a sibling advancing main
# (which moves the land fingerprint but NOT the spoke's own tip) does NOT re-inject the resolve
# prompt into a spoke already resolving — only a spoke that moved its OWN tip (genuine progress)
# earns a fresh dispatch (#285 review). Per-window (cleared on a fresh arm).
_afk_conflict_resolved_marker() { printf '%s\n' "$(_afk_state_dir)/conflict-resolved-$1"; }
_afk_already_conflict_resolved() { [ -f "$(_afk_conflict_resolved_marker "$1")" ]; }
_afk_read_conflict_resolved_tip() {
  local m; m="$(_afk_conflict_resolved_marker "$1")"
  [ -f "$m" ] && head -n1 "$m" 2>/dev/null || true
}
_afk_mark_conflict_resolved() {
  local issue="$1" tip="${2:-}" m
  m="$(_afk_conflict_resolved_marker "$issue")"
  mkdir -p "$(dirname "$m")" 2>/dev/null || true
  printf '%s\n' "$tip" > "$m" 2>/dev/null || true
}
_afk_clear_conflict_resolved() { rm -f "$(_afk_conflict_resolved_marker "$1")" 2>/dev/null || true; }
_clear_conflict_resolve_markers() { rm -f "$(_afk_state_dir)"/conflict-resolved-* 2>/dev/null || true; }

# _afk_conflict_resolve_relaunch <wt> <issue> -> DEAD/reaped pane: relaunch the spoke reusing its
# spoke_run_id (via _afk_continue_command) with the resolve prompt. Resets the reap + idle clocks
# (the fresh window has not written a transcript yet). rc 1 when the window can't be opened.
_afk_conflict_resolve_relaunch() {
  local wt="$1" issue="$2"
  log "→ conflict-resolve #$issue: relaunching the reaped spoke to merge the base branch + resolve + re-push"
  _afk_set_last_action "conflict-resolve #$issue"
  if ! _afk_open_spoke_window "$wt" "$issue" \
       "$(_afk_continue_command "$wt" "$(_afk_conflict_resolve_prompt "$issue")")"; then
    log "  could not open a conflict-resolve window for #$issue"
    return 1
  fi
  stamp_progress_epoch "$issue"
  stamp_answer_attempt "$issue"
  broker_journal_decision "$issue" conflict-resolve \
    "relaunched the reaped spoke to merge the base branch + resolve the land conflict + re-push" reversible
  _afk_bump_count "$wt" relaunch-count
  _afk_emit_span "$wt" afk-conflict-resolve success
  return 0
}
# _afk_conflict_resolve_inject <wt> <issue> -> LIVE pane: inject the resolve prompt into the
# running session (no relaunch — never kill a working spoke). rc mirrors inject_and_verify.
_afk_conflict_resolve_inject() {
  local wt="$1" issue="$2" target rc
  log "→ conflict-resolve #$issue: injecting merge-base + resolve + re-push into the live session (no relaunch)"
  _afk_set_last_action "conflict-resolve #$issue"
  target="$(_spoke_pane_target "$wt")"
  if [ -z "$target" ]; then
    log "  no live pane for #$issue — cannot inject"
    return 1
  fi
  stamp_answer_attempt "$issue"
  inject_and_verify "$wt" "$target" "$(_afk_conflict_resolve_prompt "$issue")"; rc=$?
  broker_journal_decision "$issue" conflict-resolve \
    "injected merge the base branch + resolve + re-push into the live session (no relaunch)" reversible
  if [ "$rc" -eq 0 ]; then _afk_emit_span "$wt" afk-conflict-resolve success; else _afk_emit_span "$wt" afk-conflict-resolve retry; fi
  return "$rc"
}
# _afk_route_conflict_resolution <wt> <issue> -> dispatch ONE resolution per distinct conflict:
# inject a live pane, relaunch a dead one. On a successful dispatch mark the distinct budget; a
# failed dispatch (or a repeat while the tip is unchanged) warn-parks LAST on the LAND lane so
# the watchdog escalates needs-human-land only after the drain's resolution genuinely fell short.
_afk_route_conflict_resolution() {
  local wt="$1" issue="$2" tip
  tip="$(git -C "$wt" rev-parse HEAD 2>/dev/null || true)"
  # Already dispatched for THIS spoke tip? Then the spoke has not progressed since — a re-land
  # here was triggered by a sibling advancing main, not by the spoke. Warn-park LAST, never
  # re-inject into a spoke already told to resolve; only a moved spoke tip earns a fresh dispatch.
  if _afk_already_conflict_resolved "$issue" && [ "$(_afk_read_conflict_resolved_tip "$issue")" = "$tip" ]; then
    _warn_parked_last "$wt" "$issue" "land conflicts deterministically; resolution already dispatched — waiting for the spoke to merge the base branch + resolve + re-push" land
    return 0
  fi
  if _spoke_pane_alive "$wt"; then
    if _afk_run_with_heartbeat_fg _afk_conflict_resolve_inject "$wt" "$issue"; then
      _afk_mark_conflict_resolved "$issue" "$tip"
    else
      _warn_parked_last "$wt" "$issue" "land conflicts; live-pane resolve-inject did not register — retrying at low frequency" land
    fi
  elif _afk_conflict_resolve_relaunch "$wt" "$issue"; then
    _afk_mark_conflict_resolved "$issue" "$tip"
  else
    _warn_parked_last "$wt" "$issue" "land conflicts and the resolution relaunch could not start — retrying at low frequency" land
  fi
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
  local wt_land path issue verdict max tries land_log land_rc land_before
  wt_land="$(_afk_find_script "${WT_LAND:-}" worktree-land.sh)" || { log "worktree-land.sh not found — skipping land"; return 0; }
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    _ready_at_tip "$path" "$issue" || continue
    # #241/#274: pace the per-issue land attempt on the LAND-lane warned-retry backoff. A prior
    # land failure / unclean-review / retry-exhausted warn armed the LAND lane (auto_land's own
    # park kinds — land, review); while it is pending this spoke is skipped (parked LAST), so a
    # permanently-conflicted land is re-attempted at LOW frequency (worktree-land is expensive) —
    # not every tick. Reading the LAND lane (not the shared file) is the #274 fix: an ANSWER-lane
    # re-answer backoff no longer starves the land of a ready spoke (#269). A fresh (never-warned)
    # spoke is always due, so the first land attempt is never delayed; the ready→done transition
    # clears the lane (slot_state, #274) and a successful land clears it below. Never a silent skip
    # (#274 AC3): log the reason + next-due epoch so "drain still pacing" is distinguishable from
    # "drain abandoned it" (the watchdog's auto-land-skipped escalation).
    if ! _afk_warned_due "$issue" "" land; then
      log "  skip land #$issue — land-lane retry backoff pending (next-due $(_afk_warned_next "$issue" land), now $(afk_now)); retrying at low frequency"
      continue
    fi
    # #285: a recorded DETERMINISTIC conflict whose tips are UNCHANGED would re-conflict
    # identically — do NOT re-run the expensive land; route to the resolution lane instead.
    if _afk_land_conflict_unchanged "$path" "$issue"; then
      log "  skip re-land #$issue — land conflicts deterministically and tips are unchanged; routing to the resolution lane (no identical re-land)"
      _afk_route_conflict_resolution "$path" "$issue"
      continue
    fi
    # The fingerprint moved (spoke resolved + re-pushed, or a sibling advanced main): the stale
    # fingerprint no longer describes the pending land, so drop it and fall through to a fresh
    # attempt. The resolution budget is NOT cleared here — it is keyed on the spoke's own tip
    # (_afk_route_conflict_resolution), so a main-only advance never re-injects (#285 review).
    _afk_clear_land_conflict_fp "$issue"
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
    # Bracket the land with the local default-branch SHA so a supervisor-scope merge is
    # detectable from the pre..post diff (#250 self-update DETECT).
    land_before="$(_afk_local_default_sha)"
    _afk_run_with_heartbeat bash "$wt_land" "$issue" --skip-tests >"$land_log" 2>&1; land_rc=$?
    if [ "$land_rc" -eq 0 ]; then
      log "  landed #$issue"
      _afk_clear_land_retries "$issue"   # a successful land resets the retry budget (#202 D)
      _afk_clear_warned "$issue"         # #241: progress → drop the land's warned-retry backoff
      _afk_incr_landed   # tally for the drain-complete notification (#150)
      _afk_detect_selfupdate "$land_before" "$(_afk_local_default_sha)" "$issue"  # #250
    elif [ "$land_rc" -eq 3 ]; then
      # Sentinel (#198 / #202 I): main ADVANCED but a teardown step failed — the code IS
      # shipped, so NEVER stamp blocked over merged work. Tally it and point at the log.
      log "  landed #$issue but teardown incomplete (worktree-land exit 3) — see $land_log; NOT escalating (main already advanced)"
      _afk_clear_land_retries "$issue"
      _afk_clear_warned "$issue"         # #241: shipped → drop the warned-retry backoff
      _afk_incr_landed
      _afk_detect_selfupdate "$land_before" "$(_afk_local_default_sha)" "$issue"  # #250: shipped ⇒ still deploy
    elif [ "$land_rc" -eq "$WT_LAND_CONFLICT_EXIT" ]; then
      # #285: a DETERMINISTIC merge conflict — record the tip fingerprint and route to the
      # resolution lane (relaunch/inject the spoke to merge the base branch + resolve + re-push).
      # NOT a generic warn-park: re-running the identical land is futile until a tip moves.
      log "  land #$issue conflicts with the base branch (exit $land_rc) — routing to the resolution lane (see $land_log)"
      _afk_write_land_conflict_fp "$issue" "$(_afk_land_conflict_fingerprint "$path")"
      _afk_route_conflict_resolution "$path" "$issue"
    else
      # #241 §5: a TRANSIENT / non-conflict auto-land failure (push rejection, dirty-tree guard,
      # etc. — any non-conflict wt_die exit) warns + retries on the backoff instead of parking
      # blocked/<issue>. The land is re-attempted on later ticks. (Exit 4 = a deterministic
      # conflict is handled above; this branch is every OTHER non-zero exit.)
      _warn_parked_last "$path" "$issue" "auto-land failed (non-conflict, exit $land_rc) — retrying at low frequency (see $land_log)" land
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

# --- reachability probe: network-down as a THIRD outcome (issue #249) ----------
# A connectivity blackout (a hotspot dropout, a lost home connection during a remote drain) makes
# the auth probe above fail for the WRONG reason: a fleet that is merely OFFLINE used to read as
# "subscription token dead" and get mis-blocked, stopping the whole drain. Before concluding "token
# dead", the supervisor asks the only question that matters here — can this host reach the network
# at all — with a bounded curl HEAD. curl exits 0 on ANY HTTP response (even a 401 from the
# unauthenticated API root), and nonzero ONLY when it cannot connect / resolve / times out, so this
# needs no valid credentials; NO `--fail`, or the API's unauthenticated 4xx would misread as down.
# The probe is wrapped in _afk_with_timeout so a black-hole network can't hang the tick (the same
# discipline as the gh / auth probes). AFK_NET_PROBE_CMD overrides the whole probe (tests).
: "${AFK_NET_PROBE_URL:=https://api.anthropic.com}"
: "${AFK_NET_PROBE_TIMEOUT:=10}"
# _afk_network_is_down -> rc 0 (true) when the bounded reachability probe FAILS (no network), rc 1
# (false, "up") when it succeeds OR when the probe cannot run at all (no curl and no override) — an
# unrunnable probe must NEVER read as "down", or a curl-less host would suppress every reap for the
# whole window. AFK_CURL_BIN overrides the binary (default `curl`) so the fail-open is testable.
_afk_network_is_down() {
  local secs="${AFK_NET_PROBE_TIMEOUT:-10}" cmd
  case "$secs" in '' | *[!0-9]*) secs=10 ;; esac
  if [ -z "${AFK_NET_PROBE_CMD:-}" ] && ! command -v "${AFK_CURL_BIN:-curl}" >/dev/null 2>&1; then
    return 1   # cannot probe -> fail open to "up" (normal reaping proceeds)
  fi
  cmd="${AFK_NET_PROBE_CMD:-${AFK_CURL_BIN:-curl} -sI -o /dev/null --max-time $secs ${AFK_NET_PROBE_URL:-https://api.anthropic.com}}"
  ! _afk_with_timeout "$secs" bash -c "$cmd" >/dev/null 2>&1
}

# _afk_probe_state -> the tri-state the two _afk_auth_is_dead callers branch on (#249):
#   offline   — the reachability probe failed: skip the reap pass, ride out the outage.
#   auth-dead — network up AND the auth probe returned an auth signature: block-and-halt (unchanged).
#   alive     — network up AND auth healthy: proceed normally.
# The reachability probe runs FIRST and short-circuits, so a blackout is never mistaken for dead auth.
_afk_probe_state() {
  if _afk_network_is_down; then printf 'offline\n'; return; fi
  if _afk_auth_is_dead; then printf 'auth-dead\n'; return; fi
  printf 'alive\n'
}

# _afk_note_offline_tick -> the shared response to a network-outage tick (#249): record the
# offline-since epoch (idempotent — anchors the consecutive outage for --status), refresh every
# in-flight spoke's idle + soft-ceiling clocks so the blackout never accumulates into a reap/block,
# and log the outage with its running duration. The caller keeps the heartbeat stamped.
_afk_note_offline_tick() {
  stamp_offline_since
  _afk_refresh_offline_clocks
  log "/afk: network unreachable (OFFLINE for $(offline_minutes)m) — skipping the reap pass this tick, refreshing idle clocks, riding out the outage (#249)"
}

reap_pass() {
  local path issue probed=0
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "reap" ] || continue
    # Auth probe before the FIRST reap this tick (#170 ST7): if the subscription token is
    # dead, every idle spoke is stalled on auth, not hung — reaping them one-by-one would
    # block live work into dead auth. Probe once; on a real auth failure raise the global
    # stop flag and bail, letting the main loop's _afk_service_auth_halt WARN them + re-probe
    # (never block/stop — #241 §9).
    if [ "$probed" -eq 0 ]; then
      probed=1
      afk_write_heartbeat   # the probes are bounded curl/`claude` calls — keep the epoch fresh (#170 ST2)
      # #249: distinguish network-down from auth-dead BEFORE concluding "token dead". A blackout
      # means every idle spoke is stalled on a dead NETWORK, not a dead token — reaping them would
      # mis-block a merely-offline fleet. Skip the reap this tick, refresh idle clocks so the outage
      # never accumulates into a reap/block, and re-check next tick. Auth-dead keeps the #170 ST7
      # / #241 §9 behavior (raise the halt flag; the main loop WARNs + re-probes, never blocks/stops).
      case "$(_afk_probe_state)" in
        offline)
          _afk_note_offline_tick
          return 0
          ;;
        auth-dead)
          _AFK_AUTH_FAILED=1
          log "/afk: auth probe failed during reap — halting instead of reaping spokes into dead auth"
          return 0
          ;;
        *)  # alive: network up + auth healthy — clear any outage marker and reap normally
          clear_offline_since
          ;;
      esac
    fi
    _reap_or_resume "$path" "$issue"
    # The #246 park-guard may have run decide_and_act, whose answerer can raise _AFK_AUTH_FAILED
    # (dead subscription token) mid-loop. reap_pass is the last pass, so nothing checks the flag
    # after it — bail the loop now rather than revive the remaining over-ceiling survivors into
    # dead auth (the #170 ST7 harm the top-of-loop probe already guards against for the first reap).
    if [ "$_AFK_AUTH_FAILED" -eq 1 ]; then return 0; fi
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
      # #256: same completion-signal gate as reap_pass. This path only runs for a DEAD pane, so
      # the pane-alive-gated signals collapse to the revive — crashed-pane behavior is unchanged.
      _afk_finish_up_or_revive "$path" "$issue" "time ceiling: ran >${AFK_SPOKE_MAX_MINUTES}m without finishing"
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
  # both would shell out to a `claude`/suite that is just as dead. The main loop then WARNS
  # the in-flight spokes + re-probes (never blocks/stops — #241 §9, _afk_service_auth_halt).
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
  # into dead auth; the main loop's _afk_service_auth_halt then WARNS the in-flight set +
  # re-probes (never blocks/stops — #241 §9).
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

# --- sleep inhibitor (issue #242) ---------------------------------------------
# While a drain is armed the Mac must not sleep: system sleep freezes the supervisor,
# every spoke, tmux, and the OTel stack mid-run, and wall-clock timers (reap ceilings,
# staleness checks) misfire on wake. So arming ties a `caffeinate -is -w <supervisor pid>`
# to the supervisor's LIFETIME: `caffeinate -w <pid>` self-exits the instant that pid dies,
# so /afk off (and any crash) needs NO teardown — this mirrors the --remote path's
# `caffeinate -s` wrap (build_remote_launch_cmd) for the LOCAL arm. AFK_CAFFEINATE_BIN wins
# for tests. On a non-macOS host (no caffeinate) the ensure is a SILENT no-op so arming never
# fails; the loud battery/lid and missing-caffeinate warnings are surfaced separately at arm.
#
# The pidfile records "<caffeinate pid> <supervisor pid>" under the per-run state dir (so the
# tests' AFK_STATE_DIR pin isolates it); AFK_INHIBITOR_FILE overrides it directly.
_afk_inhibitor_file() {
  if [ -n "${AFK_INHIBITOR_FILE:-}" ]; then printf '%s\n' "$AFK_INHIBITOR_FILE"; return; fi
  printf '%s\n' "$(_afk_state_dir)/sleep-inhibit"
}
# _afk_inhibitor_pid -> the recorded caffeinate pid (first field), for --status.
_afk_inhibitor_pid() {
  local f rec; f="$(_afk_inhibitor_file)"; [ -f "$f" ] || return 0
  rec="$(head -n1 "$f" 2>/dev/null)"; printf '%s\n' "${rec%% *}"
}

# _afk_arm_inhibitor <supervisor pid> -> ensure EXACTLY ONE `caffeinate -is -w <pid>` is
# tied to <pid>. Idempotent: the supervisor calls it each tick (tied to $$) and the watchdog
# each interval (tied to the live heartbeat pid), so a killed caffeinate is re-armed and a
# respawn re-ties to the new pid. A non-numeric pid or an absent caffeinate is a silent no-op,
# never a failure that would abort arming (the non-macOS warning is surfaced once at arm time).
#
# Concurrency (supervisor tick vs watchdog tick both arming at once): the fast path no-ops
# when a live inhibitor already ties to THIS pid, so a spawn only happens when there is none.
# When one is needed each caller spawns then CLAIMS the pidfile with a noclobber (O_EXCL)
# create — the first creator wins, every loser reads the winner's live entry and kills its own
# double, so exactly one survives even under real two-process concurrency (not just the
# single-process tests). A stale incumbent (dead caffeinate, or an OLD supervisor pid) is
# dropped and re-claimed.
_afk_arm_inhibitor() {
  local sup_pid="$1" bin f rec cpid spid mine tries
  case "$sup_pid" in '' | *[!0-9]*) return 0 ;; esac
  bin="${AFK_CAFFEINATE_BIN:-caffeinate}"
  command -v "$bin" >/dev/null 2>&1 || return 0
  f="$(_afk_inhibitor_file)"
  # Fast path: a live inhibitor already tied to THIS supervisor pid — no spawn (exactly one).
  if [ -f "$f" ]; then
    rec="$(head -n1 "$f" 2>/dev/null)"; cpid="${rec%% *}"; spid="${rec##* }"
    [ "$spid" = "$sup_pid" ] && _afk_pid_alive "$cpid" && return 0
  fi
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  "$bin" -is -w "$sup_pid" >/dev/null 2>&1 &
  mine=$!
  # Converge on a single pidfile entry. Bounded: no caller writes a stale entry (each writes
  # its own live pid), so once any caller claims, everyone else takes the "peer's live" branch;
  # only the initial pre-existing stale file forces a re-claim, so this settles in <=2 rounds.
  tries=0
  while [ "$tries" -lt 5 ]; do
    tries=$(( tries + 1 ))
    if ( set -C; printf '%s %s\n' "$mine" "$sup_pid" > "$f" ) 2>/dev/null; then
      return 0   # claimed the pidfile — my inhibitor is the one
    fi
    rec="$(head -n1 "$f" 2>/dev/null)"; cpid="${rec%% *}"; spid="${rec##* }"
    # A blank / partial record: a peer won the O_EXCL create microseconds ago but its content
    # bytes have not landed yet (create and printf are two steps). Do NOT delete it — re-read
    # next round; its pid converges and the peer's-live branch below then fires (#242 review).
    case "$spid" in '' | *[!0-9]*) continue ;; esac
    if [ "$spid" = "$sup_pid" ] && _afk_pid_alive "$cpid"; then
      [ "$cpid" = "$mine" ] || kill "$mine" 2>/dev/null || true
      return 0   # a live inhibitor for this supervisor exists (mine or a peer's) — drop my double
    fi
    # Genuinely stale incumbent (a real but dead caffeinate, or an OLD supervisor pid i.e. a
    # respawn re-tie): drop it and re-claim the now-empty pidfile.
    [ "$cpid" != "$mine" ] && _afk_pid_alive "$cpid" && kill "$cpid" 2>/dev/null || true
    rm -f "$f" 2>/dev/null || true
  done
  # UPGRADE: the bounded loop cannot realistically exhaust (see above); on the impossible
  # exhaustion, record mine so the machine still stays awake rather than leave it unrecorded.
  _afk_atomic_write "$f" "$mine $sup_pid" || true
  return 0
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
  local src src_dir dir copy
  src="${BASH_SOURCE[0]}"
  src_dir="$(cd "$(dirname "$src")" && pwd)" || return 0
  dir="$(mktemp -d "${TMPDIR:-/tmp}/hub-afk-self.XXXXXX" 2>/dev/null)" || return 0
  copy="$dir/hub-afk.sh"
  # Copy the WHOLE sibling set (gate-broker.sh, hub-inject.sh, and anything else hub-afk
  # sources transitively), not hub-afk.sh alone -- a helper moved to a new sibling file must
  # ride along or the drain strands with `command not found` (#262). A generic glob, not an
  # enumerated list, so a future sibling needs no registration here. Fail-OPEN: on a glob
  # copy failure fall back to the lone script, and if even that leaves no hub-afk.sh copy,
  # run from the original rather than refuse to arm.
  cp "$src_dir"/*.sh "$dir"/ 2>/dev/null || cp "$src" "$copy" 2>/dev/null || return 0
  [ -f "$copy" ] || cp "$src" "$copy" 2>/dev/null || return 0
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

# --- self-update: deploy the supervisor's own landed code (issue #250) ---------
# A land that merges a change to the supervisor's OWN code (hub-afk.sh, gate-broker.sh,
# the answerer rule, …) used to require an operator recycle: the live drain keeps running
# the OLD synced copy until it is restarted, and a mid-tick sync can corrupt an in-flight
# tick (#135). This block gives the drain a self-update protocol so an afk-fix land
# auto-deploys with zero operator commands:
#   DETECT  — after a successful land, if the merged diff (PRE..POST on the local default
#             branch) touches the supervisor scope, flag a pending self-update.
#   DEPLOY  — at the NEXT tick boundary (never mid-tick), validate + smoke-test the SOURCE,
#             re-sync the gitignored .ai-toolkit/scripts the /afk skill launches, then exec
#             this process in place (a no-arg resume) onto the new code. `exec` preserves
#             $$ so caffeinate (-w $$, #242) and the heartbeat pid survive untouched, and a
#             no-arg launch re-adopts the in-flight spokes (dispatch_batch skips them).
#   FAIL SAFE — the source is validated (bash -n) AND smoke-run (`hub-afk.sh --help`) BEFORE
#             the re-sync, so a broken new version never becomes the synced copy the watchdog
#             would respawn; on any failure the drain stays on the old code with a loud warn
#             + journal, never stranded.
# Each self-deploy is recorded in the #241 decision journal.

# _afk_selfupdate_scope_paths -> the space-separated set of BASENAMES whose land triggers a
# self-update. Matched by basename so a source path (shared/.../hub-afk.sh), its synced copy
# (.ai-toolkit/scripts/hub-afk.sh), and scripts/worktree-land.sh all hit. AFK_SELFUPDATE_SCOPE
# overrides the whole set (tests / operator tuning).
_afk_selfupdate_scope_paths() {
  printf '%s\n' "${AFK_SELFUPDATE_SCOPE:-hub-afk.sh gate-broker.sh hub-notify.sh worktree-lib.sh worktree-land.sh batch-plan.sh afk-answering.md}"
}

# _afk_paths_in_scope <newline-separated paths> -> true when ANY path's basename is in the
# supervisor scope. Pure (no side effects) so the matcher is unit-testable in isolation.
_afk_paths_in_scope() {
  local paths="$1" scope p base
  scope="$(_afk_selfupdate_scope_paths)"
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    base="${p##*/}"
    case " $scope " in *" $base "*) return 0 ;; esac
  done <<EOF
$paths
EOF
  return 1
}

# The pending-self-update flag: a file under the per-run state dir (survives a watchdog
# respawn, exactly as the resume/redispatch markers do) whose body is the issue whose land
# triggered it (for the journal). Set on DETECT, consumed on DEPLOY, cleared on a fresh arm.
_afk_selfupdate_flag_file() { printf '%s\n' "$(_afk_state_dir)/self-update-pending"; }
_afk_mark_selfupdate_pending() {
  local dir; dir="$(_afk_state_dir)"; mkdir -p "$dir" 2>/dev/null || true
  _afk_atomic_write "$(_afk_selfupdate_flag_file)" "${1:-}" || true
}
_afk_selfupdate_pending() { [ -f "$(_afk_selfupdate_flag_file)" ]; }
_afk_read_selfupdate_issue() {
  local f; f="$(_afk_selfupdate_flag_file)"
  [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' || true
}
_afk_clear_selfupdate_pending() { rm -f "$(_afk_selfupdate_flag_file)" 2>/dev/null || true; }

# _afk_local_default_sha [root] -> the SHA at the tip of the local default branch (the ref a
# land advances), or empty. Used to bracket a land: PRE before, POST after, so the merged
# diff is PRE..POST. Resolves the branch the same way arming does (_afk_default_ref).
_afk_local_default_sha() {
  local root="${1:-${MAIN_ROOT:-.}}" ref
  ref="$(_afk_default_ref "$root")"; ref="${ref#origin/}"
  [ -n "$ref" ] || return 0
  git -C "$root" rev-parse -q --verify "refs/heads/$ref" 2>/dev/null || true
}

# _afk_detect_selfupdate <pre_sha> <post_sha> <issue> [root] -> flag a pending self-update when
# the land's merged diff (pre..post) touches the supervisor scope. A no-op when the branch did
# not advance (pre == post / empty), or the diff is out of scope. Idempotent — several in-scope
# lands in one pass just re-mark the same flag.
_afk_detect_selfupdate() {
  local pre="$1" post="$2" issue="$3" root="${4:-${MAIN_ROOT:-.}}" files
  [ -n "$pre" ] && [ -n "$post" ] || return 0
  [ "$pre" != "$post" ] || return 0
  files="$(git -C "$root" diff --name-only "$pre" "$post" 2>/dev/null || true)"
  [ -n "$files" ] || return 0
  if _afk_paths_in_scope "$files"; then
    log "/afk: landed #$issue touched supervisor scope — flagging a self-update (redeploy at the next tick boundary)"
    _afk_mark_selfupdate_pending "$issue"
  fi
}

# _afk_selfupdate_source_scripts [root] -> the source scripts a self-deploy must prove parse
# before it re-syncs: the supervisor's own script, everything it sources, and the siblings it
# shells out to. Absent files are skipped by the validator (a slimmed checkout is not an error).
# NOTE (#250 review finding 5): this is the FIXED set the DEFAULT AFK_SELFUPDATE_SCOPE fully
# covers. AFK_SELFUPDATE_SCOPE only widens DETECTION (which land triggers a redeploy); it does
# NOT extend this validated set. If you add a custom supervisor helper to AFK_SELFUPDATE_SCOPE,
# add its source path here too, or a broken version of it would deploy unvalidated.
_afk_selfupdate_source_scripts() {
  local root="${1:-${MAIN_ROOT:-.}}"
  printf '%s\n' \
    "$root/shared/skills/hub/scripts/hub-afk.sh" \
    "$root/shared/skills/hub/scripts/gate-broker.sh" \
    "$root/shared/skills/hub/scripts/hub-notify.sh" \
    "$root/shared/skills/hub/scripts/batch-plan.sh" \
    "$root/scripts/worktree-lib.sh" \
    "$root/scripts/worktree-land.sh"
}

# _afk_validate_scripts [root] -> true when every present source script parses (bash -n). This
# is the PRIMARY fail-safe: because sync-to-repo copies scripts verbatim, a source that parses
# guarantees the synced copy parses, so the deploy never overwrites the synced copy the watchdog
# respawns with unparseable code. Names the first offender and returns nonzero on any failure.
_afk_validate_scripts() {
  local root="${1:-${MAIN_ROOT:-.}}" f bad=0
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    if ! bash -n "$f" 2>/dev/null; then
      log "/afk self-update: SOURCE $f fails to parse (bash -n) — aborting the deploy"
      bad=1
    fi
  done <<EOF
$(_afk_selfupdate_source_scripts "$root")
EOF
  [ "$bad" -eq 0 ]
}

# _afk_smoke_source [root] -> true when the SOURCE hub-afk.sh loads and runs a trivial subcommand
# (`--help`) without dying. `--help` exercises the full top-of-file sourcing (gate-broker /
# worktree-lib), every top-level statement (set -u traps, `: "${VAR:=…}"` defaults), and all
# function definitions — but is exempt from the self-copy exec — so a version that parses yet
# EXITS at startup is caught here, before the re-sync, rather than after the exec has already
# replaced this process (closing the AC#4 "startup-death traps the watchdog" hole). Bounded so a
# wedged new version can't freeze the deploy. AFK_SELFUPDATE_SMOKE_CMD overrides it for tests.
_afk_smoke_source() {
  if [ -n "${AFK_SELFUPDATE_SMOKE_CMD:-}" ]; then bash -c "$AFK_SELFUPDATE_SMOKE_CMD"; return $?; fi
  local root="${1:-${MAIN_ROOT:-.}}" src
  src="$root/shared/skills/hub/scripts/hub-afk.sh"
  [ -f "$src" ] || return 1
  _afk_with_timeout "${AFK_SELFUPDATE_SMOKE_TIMEOUT:-30}" bash "$src" --help >/dev/null 2>&1
}

# _afk_resync [root] -> regenerate the gitignored .ai-toolkit/scripts the /afk skill launches
# from the freshly-merged source. Bounded; a failure is reported and the deploy aborts (the
# synced copy is left on the previous good code). AFK_SYNC_CMD overrides the sync for tests.
_afk_resync() {
  if [ -n "${AFK_SYNC_CMD:-}" ]; then bash -c "$AFK_SYNC_CMD"; return $?; fi
  local root="${1:-${MAIN_ROOT:-.}}" sync
  sync="$root/scripts/sync-to-repo.sh"
  [ -f "$sync" ] || { log "/afk self-update: sync-to-repo.sh not found at $sync — cannot redeploy"; return 1; }
  _afk_with_timeout "${AFK_SYNC_TIMEOUT:-120}" bash "$sync" "$root" claude >/dev/null 2>&1
}

# _afk_selfupdate_fail <issue> <reason> -> the fail-safe exit from a self-deploy: warn loudly,
# journal the aborted deploy (file + gh — a broken self-deploy is operator-noteworthy), and
# clear the pending flag so the drain does NOT re-attempt a broken deploy every tick (the next
# supervisor-scope land re-arms it). The drain continues on the OLD code, never stranded (AC#4).
_afk_selfupdate_fail() {
  local issue="$1" reason="$2"
  log "/afk: self-update ABORTED — $reason; staying on the old code (drain continues)"
  broker_journal_decision "${issue:-self-update}" self-deploy \
    "self-deploy aborted: $reason — staying on old code" scope
  _afk_clear_selfupdate_pending
}

# _afk_self_deploy -> the DEPLOY half of the #250 self-update protocol, run at a tick boundary
# (never mid-tick). Validate + smoke the source, re-sync the synced scripts, journal, then EXEC
# this process in place onto the new code as a no-arg resume: `exec` preserves $$ so caffeinate
# (-w $$, #242) and the heartbeat pid survive untouched (the watchdog keeps reading `live`), and
# a no-arg launch re-adopts the in-flight spokes (dispatch_batch skips them). On success the exec
# never returns; every failure path falls back to the old code via _afk_selfupdate_fail.
# A version that passes --help but dies only in the drain loop is caught downstream by the
# watchdog crash-loop guard (_afk_watchdog_guarded_respawn), which halts respawns + escalates
# loudly rather than spinning. UPGRADE: keep a pre-deploy backup of the synced hub-afk.sh and
# auto-roll-back on a tripped guard, so the drain self-heals instead of halting for a human.
# _afk_selfupdate_prepare <root> -> the returning (non-exec) part of a self-deploy: validate +
# smoke the source, then re-sync. Split out so _afk_self_deploy can run it UNDER the heartbeat
# stamper (#250 review finding 4): validate+smoke+resync can take up to ~150s, and with no
# heartbeat through it an aggressively-tuned watchdog (a short AFK_TICK_SECONDS) would read the
# supervisor as wedged and SIGKILL it mid-resync, leaving a half-written synced tree the respawn
# then runs. On the failing step it runs _afk_selfupdate_fail (the fallback) and returns nonzero
# so the caller does NOT exec.
_afk_selfupdate_prepare() {
  local root="$1" issue; issue="$(_afk_read_selfupdate_issue)"
  if ! _afk_validate_scripts "$root" || ! _afk_smoke_source "$root"; then
    _afk_selfupdate_fail "$issue" "source failed validation/smoke (bash -n or --help) — merged code is broken"
    return 1
  fi
  if ! _afk_resync "$root"; then
    _afk_selfupdate_fail "$issue" "re-sync (sync-to-repo.sh) failed — synced scripts left on the previous good copy"
    return 1
  fi
  return 0
}
_afk_self_deploy() {
  local root="${AFK_SELFUPDATE_ROOT:-${MAIN_ROOT:-.}}" issue
  issue="$(_afk_read_selfupdate_issue)"
  log "/afk: self-update — validating + redeploying the supervisor on landed #${issue:-?} code"
  _afk_set_last_action "self-deploy #${issue:-?}"
  # Run validate+smoke+resync UNDER the heartbeat stamper so a long (but bounded) prepare can't
  # be mistaken for a wedged supervisor and SIGKILLed mid-resync (#250 review finding 4).
  if ! _afk_run_with_heartbeat_fg _afk_selfupdate_prepare "$root"; then
    return 1   # _afk_selfupdate_prepare already ran the fail-safe fallback (warn + journal + clear)
  fi
  # Record the deploy (file only — routine success, so no per-deploy gh comment) and clear the
  # flag BEFORE the exec: the re-exec'd resume must not read a still-pending flag and redeploy
  # in a loop.
  _broker_journal_line "${issue:-self-update}" self-deploy \
    "redeploying the supervisor onto landed #${issue:-?} code (validated + re-synced)" scope
  _afk_clear_selfupdate_pending
  log "/afk: self-update — code validated + re-synced; re-execing the supervisor in place (resume)"
  if [ -n "${AFK_SELF_DEPLOY_EXEC_CMD:-}" ]; then bash -c "$AFK_SELF_DEPLOY_EXEC_CMD"; return $?; fi
  # env -u AFK_RUNNING_COPY forces a fresh self-copy of the (now re-synced) original; no window
  # arg ⇒ resume. `exec` keeps $$ so the caffeinate -w tie and the heartbeat pid are preserved.
  exec env -u AFK_RUNNING_COPY bash "$(_afk_self)"
}

# watchdog_tick -> one watchdog check, printing the observed supervisor state:
#   off       — no window armed; the watchdog should stop.
#   live      — a supervisor is alive and recently stamped the heartbeat; nothing to do.
#   respawned — the window is armed but the supervisor is gone (dead pid) OR wedged (live
#               pid, stale heartbeat, #170 ST2): respawn it, first killing a wedged one.
# --- respawn crash-loop guard (issue #250 review finding 1) --------------------
# A self-deployed version that PARSES and passes the `--help` smoke but dies only inside the
# drain loop escapes the pre-exec fail-safe: the exec runs it, it crashes, and the watchdog
# respawns the same (now-synced) broken code — forever, silently, with the drain never
# progressing. The pre-exec validate+smoke cannot catch a loop-only runtime bug (it would also
# have escaped the land's own suite), so the watchdog bounds the residual: after AFK_RESPAWN_MAX
# respawns within AFK_RESPAWN_WINDOW seconds it stops respawning and escalates LOUDLY (log +
# journal), converting an invisible respawn spin into a visible, operator-actionable halt. A
# healthy supervisor never respawns, so the window prunes empty and the guard never fires in
# normal operation. UPGRADE: keep a pre-deploy backup of the synced hub-afk.sh and auto-roll-back
# on a tripped guard, so the drain self-heals instead of halting for a human.
: "${AFK_RESPAWN_MAX:=5}"
: "${AFK_RESPAWN_WINDOW:=300}"
_afk_respawn_log_file() { printf '%s\n' "$(_afk_state_dir)/respawn-log"; }
# The tripped-marker debounces the loud escalation to the TRANSITION into a crash-loop, so a
# tripped guard does not re-journal + re-spawn gh every watchdog tick (#250 review WARNING).
_afk_respawn_tripped_marker() { printf '%s\n' "$(_afk_state_dir)/respawn-guard-tripped"; }
_afk_clear_respawn_log() {
  rm -f "$(_afk_respawn_log_file)" "$(_afk_respawn_tripped_marker)" 2>/dev/null || true
}
# _afk_respawn_allowed -> record this respawn and return true when the respawn RATE is under the
# limit; false (a crash-loop) once AFK_RESPAWN_MAX or more respawns fell within the last
# AFK_RESPAWN_WINDOW seconds. Prunes entries older than the window, so a supervisor that stops
# crashing lets the window drain and the guard resets on its own.
_afk_respawn_allowed() {
  local f now win max cutoff line kept="" n=0
  f="$(_afk_respawn_log_file)"; now="$(afk_now)"
  win="${AFK_RESPAWN_WINDOW:-300}"; case "$win" in '' | *[!0-9]*) win=300 ;; esac
  max="${AFK_RESPAWN_MAX:-5}"; case "$max" in '' | *[!0-9]*) max=5 ;; esac
  cutoff=$(( now - win ))
  if [ -f "$f" ]; then
    while IFS= read -r line; do
      case "$line" in '' | *[!0-9]*) continue ;; esac
      [ "$line" -ge "$cutoff" ] && { kept="$kept$line
"; n=$(( n + 1 )); }
    done < "$f"
  fi
  if [ "$n" -ge "$max" ]; then
    printf '%s' "$kept" > "$f" 2>/dev/null || true   # keep the pruned window; do NOT add this one
    return 1
  fi
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s%s\n' "$kept" "$now" > "$f" 2>/dev/null || true
  return 0
}
# _afk_watchdog_guarded_respawn <reason> -> respawn UNLESS the rate shows a crash-loop; on a
# crash-loop, escalate loudly + journal and decline. Returns nonzero when it declined.
_afk_watchdog_guarded_respawn() {
  local trip; trip="$(_afk_respawn_tripped_marker)"
  if _afk_respawn_allowed; then
    rm -f "$trip" 2>/dev/null || true   # recovered — re-arm the one-shot escalation
    _afk_watchdog_respawn
    return 0
  fi
  # Escalate loudly ONCE per crash-loop episode (the transition into the tripped state), not on
  # every tick — else an unattended window re-journals + re-spawns gh ~every AFK_WATCHDOG_SECONDS
  # for hours (#250 review WARNING). A later allowed respawn clears the marker so a fresh episode
  # re-escalates.
  if [ ! -f "$trip" ]; then
    : > "$trip" 2>/dev/null || true
    log "/afk watchdog: CRASH-LOOP — the supervisor respawned >= ${AFK_RESPAWN_MAX:-5} times in ${AFK_RESPAWN_WINDOW:-300}s ($1); halting respawns. Likely a self-deployed version that dies in the drain loop. Run /afk --off, fix the code, and re-arm."
    broker_journal_decision self-update self-deploy \
      "watchdog crash-loop guard tripped ($1): supervisor respawned too fast — halting respawns (likely a bad self-deploy in the drain loop). Needs a human." irreversible
  fi
  return 1
}

watchdog_tick() {
  case "$(afk_supervisor_state)" in
    off)  printf 'off\n' ;;
    live)
      if _afk_heartbeat_wedged; then
        log "/afk watchdog: supervisor pid alive but heartbeat stale >$(( ${AFK_STALE_TICKS:-10} * AFK_TICK_SECONDS ))s — killing the wedged supervisor and respawning"
        _afk_kill_wedged_supervisor
        if _afk_watchdog_guarded_respawn wedged; then printf 'respawned\n'; else printf 'crashloop\n'; fi
      else
        # Re-check the sleep inhibitor each interval alongside the supervisor (#242): re-arm a
        # killed caffeinate, tied to the live supervisor's (heartbeat) pid. Idempotent — a
        # no-op when it is already armed for that pid.
        local hb pid; hb="$(afk_read_heartbeat)"; pid="${hb%% *}"
        _afk_arm_inhibitor "$pid"
        printf 'live\n'
      fi ;;
    stale)
      log "/afk watchdog: supervisor gone but window still armed — respawning"
      if _afk_watchdog_guarded_respawn crashed; then printf 'respawned\n'; else printf 'crashloop\n'; fi ;;
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
# _afk_arm_hub_watchdog -> co-arm the tier-2 hub-watchdog (issue #251) alongside the keeper so
# it runs "OS-level alongside the drain" (AC#1) and is re-armed if it died — the drain keeper
# and the tier-2 watchdog cross-check each other. `hub-watchdog.sh --arm` is singleton-guarded
# (idempotent) and detaches its own nohup daemon, so this is cheap when one already runs.
# Best-effort: a missing script or a failed launch never aborts the drain. Opt-out
# HUB_WATCHDOG_COARM=0; HUB_WATCHDOG_ARM_CMD overrides the launch (the tests' seam), and
# HUB_WATCHDOG_BIN pins the script path.
_afk_arm_hub_watchdog() {
  [ "${HUB_WATCHDOG_COARM:-1}" = "1" ] || return 0
  if [ -n "${HUB_WATCHDOG_ARM_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_ARM_CMD" >/dev/null 2>&1 || true; return 0; fi
  local wd; wd="$(_afk_find_script "${HUB_WATCHDOG_BIN:-}" hub-watchdog.sh)" || return 0
  bash "$wd" --arm >/dev/null 2>&1 || true
}

_afk_spawn_watchdog() {
  _afk_watchdog_alive && return 0
  if [ -n "${AFK_WATCHDOG_SPAWN_CMD:-}" ]; then bash -c "$AFK_WATCHDOG_SPAWN_CMD"; return 0; fi
  # env -u strips the running copy's exported recursion guard: the watchdog is
  # long-lived and must exec its OWN fresh copy of the original (ST5 review).
  nohup env -u AFK_RUNNING_COPY bash "$(_afk_self)" --watchdog >/dev/null 2>&1 &
  # Record the child pid immediately so the next tick's dedup check sees it alive before
  # the watchdog itself writes the pidfile (closes the launch→pidfile startup race).
  printf '%s\n' "$!" > "$(_afk_watchdog_file)" 2>/dev/null || true
  _afk_arm_hub_watchdog   # tier-2 (#251): co-arm the OS-level hub-watchdog alongside the keeper
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

# --- sleep-inhibitor status + arm-time power warnings (issue #242) -------------
# afk_inhibitor_status -> a one-line, READ-ONLY sleep-inhibitor summary for --status: active
# with its caffeinate pid, MISSING when the machine may sleep, or unavailable on a host with no
# caffeinate (non-macOS). Probes only (no launch), so a status read has no side effects.
afk_inhibitor_status() {
  local bin cpid; bin="${AFK_CAFFEINATE_BIN:-caffeinate}"
  if ! command -v "$bin" >/dev/null 2>&1; then
    printf '/afk: sleep-inhibit: unavailable (no caffeinate — non-macOS; the systemd-inhibit equivalent is unwired)\n'
    return 0
  fi
  cpid="$(_afk_inhibitor_pid)"
  if _afk_pid_alive "$cpid"; then
    printf '/afk: sleep-inhibit: active (pid %s)\n' "$cpid"
  else
    printf '/afk: sleep-inhibit: MISSING — machine may sleep\n'
  fi
}

# afk_warn_power -> WARN at arm time when on BATTERY: the `-s` in the inhibitor's `caffeinate
# -is` holds sleep off only on AC power, and a lid-close sleeps regardless — name BOTH limits so
# the operator plugs in and keeps the lid open. Guarded on pmset (absent off macOS) and read
# under LC_ALL=C (the repo's locale trap: an English-keyword parse of a localized `pmset -g
# batt` must force the C locale).
afk_warn_power() {
  local pm batt; pm="${AFK_PMSET_BIN:-pmset}"
  command -v "$pm" >/dev/null 2>&1 || return 0
  batt="$(LC_ALL=C "$pm" -g batt 2>/dev/null)"
  case "$batt" in
    *"Battery Power"*)
      log "/afk: WARNING — on battery power: the sleep inhibitor holds only while on AC power, and a lid-close sleeps regardless; plug in and keep the lid open for an unattended drain" ;;
  esac
}

# _afk_warn_no_inhibitor -> WARN once at arm time when caffeinate is absent (non-macOS): arming
# still PROCEEDS (never fails), but the drain will NOT inhibit sleep — name the Linux equivalent
# so the limitation is surfaced, not silent.
_afk_warn_no_inhibitor() {
  local bin; bin="${AFK_CAFFEINATE_BIN:-caffeinate}"
  command -v "$bin" >/dev/null 2>&1 && return 0
  log "/afk: WARNING — 'caffeinate' not found (non-macOS?); the drain will NOT inhibit system sleep — the equivalent here is 'systemd-inhibit --what=sleep'"
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

# afk_offline_status -> a one-line OFFLINE diagnostic while a network outage is in progress (#249),
# or nothing when reachable. Surfaced by --status so a stuck-offline drain is diagnosable without
# attaching: it names how long the blackout has run, during which the reap pass is paused and the
# idle clocks are refreshed each tick so the outage never accumulates into a reap/block.
afk_offline_status() {
  local mins; mins="$(offline_minutes)"
  [ -n "$mins" ] || return 0
  printf '/afk: OFFLINE for %sm — network unreachable, reaping paused (idle clocks refreshed); re-checked each tick\n' "$mins"
}

# afk_hang_forensics_status -> a one-line summary of the hang-forensics bundles captured before
# a reaper revival (#243), or nothing when none exist. Like the blocked-locally line, a bundle
# outlives the drain, so the operator returning from AFK sees where the hang evidence sits.
afk_hang_forensics_status() {
  local dir count; dir="$(_afk_hang_forensics_dir)"
  [ -d "$dir" ] || return 0
  count="$(find "$dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d '[:space:]')"
  case "$count" in '' | 0) return 0 ;; esac
  printf '/afk: hang-forensics: %s bundle(s) captured [%s]\n' "$count" "$dir"
}

# --- duplicate-lineage detection (issue #252) ---------------------------------
# The heartbeat records only ONE pid, so a SECOND live supervisor from a fast off/re-arm race is
# invisible to afk_supervisor_state. _afk_supervisor_pids emits one pid per live supervisor
# LINEAGE. A single supervisor forks many transient subshells that all inherit its
# `bash <self-copy>/hub-afk.sh <window>` argv, so counting matching pids wildly over-counts;
# instead we DEDUP by the distinct script path (each armed lineage re-execs from its own unique
# `hub-afk-self.XXXXXX/hub-afk.sh` copy, #133), emitting the first pid seen per path. The
# --watchdog keeper (its own copy), the transient subcommands, and any `bash -c` SOURCED /
# one-liner form (the test harness) are excluded. Best-effort + overridable via
# AFK_SUPERVISOR_PIDS_CMD (the file's *_CMD test-hook pattern) — the scan is host-dependent, so
# the warning is warn-only and never acts. Goes through wt_pgrep (LC_ALL=C, the non-ASCII trap).
_afk_supervisor_pids() {
  if [ -n "${AFK_SUPERVISOR_PIDS_CMD:-}" ]; then bash -c "$AFK_SUPERVISOR_PIDS_CMD"; return; fi
  command -v wt_pgrep >/dev/null 2>&1 || return 0
  local self="$$" seen="" pid rest tok path
  while read -r pid rest; do
    case "$pid" in '' | *[!0-9]*) continue ;; esac
    [ "$pid" = "$self" ] && continue   # never count the process running this scan
    case " $rest " in
      *' -c '* | *' --watchdog'* | *' --status'* | *' --off'* | *' --reconcile'* | *' --once'* | *' --help'* | *' -h '*) continue ;;
    esac
    path=""
    for tok in $rest; do case "$tok" in */hub-afk.sh) path="$tok"; break ;; esac; done
    [ -n "$path" ] || continue
    case " $seen " in *" $path "*) continue ;; esac   # this lineage already counted
    seen="$seen $path"
    printf '%s\n' "$pid"
  done < <(wt_pgrep -fl 'hub-afk' 2>/dev/null)
}

# afk_duplicate_supervisor_status -> a one-line WARNING for --status when MORE THAN ONE live
# supervisor lineage is draining this checkout (#252): a fast off/re-arm can leave the old sleeper
# alongside the new one, and the single-pid heartbeat hides it. Prints nothing for 0/1.
afk_duplicate_supervisor_status() {
  local pids n list
  pids="$(_afk_supervisor_pids)"
  n="$(printf '%s\n' "$pids" | grep -c '[0-9]' 2>/dev/null || true)"
  case "$n" in '' | *[!0-9]*) n=0 ;; esac
  [ "$n" -gt 1 ] || return 0
  list="$(printf '%s' "$pids" | tr '\n' ' ' | sed 's/  */ /g;s/^ //;s/ $//')"
  printf '/afk: WARNING — %s live supervisor lineages detected (pids: %s) — a duplicate drain can double-dispatch/double-land; run /afk --off --wait, then re-arm\n' \
    "$n" "$list"
}

_status() {
  local state now
  state="$(afk_read_state)"; now="$(afk_now)"
  # Surface a double-drain hazard first (#252), in both off and armed states — a leftover lineage
  # after an --off is exactly the danger, so it must show even when .afk-state reads off.
  afk_duplicate_supervisor_status
  if [ -z "$state" ]; then
    echo "/afk: off"
    # A durable escalation outlives the drain — surface it even when off, so the operator
    # returning from AFK sees a block that never reached the dashboard (#109).
    afk_blocked_locally_status
    afk_hang_forensics_status   # #243: hang-forensics bundles outlive the drain too
    return 0
  fi
  _afk_status_state_line "$state" "$now"
  # A network outage pauses reaping and refreshes idle clocks (#249) — surface it right under the
  # state line so a stuck-offline drain is obvious without attaching. No-op when reachable.
  afk_offline_status
  # For a live (or stale) drain, surface telemetry health too: the dashboard is the SSOT,
  # so the operator must be able to see whether it's actually receiving data (#108). A
  # no-op line when telemetry is opted out (AI_TOOLKIT_OTEL=0).
  afk_telemetry_status
  # ...and the sleep-inhibitor state (#242): while a drain is armed the Mac must not sleep, so
  # the operator must be able to see whether the inhibitor is actually holding.
  afk_inhibitor_status
  afk_blocked_locally_status
  afk_hang_forensics_status   # #243: surface where a reaper revival stashed the hang evidence
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
    --off)
      # Capture the heartbeat pid BEFORE clearing — afk_clear_state removes the heartbeat.
      local off_wait=0 off_hb off_pid
      case "${2:-}" in --wait | -w) off_wait=1 ;; esac
      off_hb="$(afk_read_heartbeat)"; off_pid="${off_hb%% *}"
      afk_clear_state
      if [ "$off_wait" -eq 1 ]; then
        # Synchronous off (#252): block until the supervisor is actually gone, so a scripted
        # off->sync->arm recycle needs no sleep-guessing. Nonzero on timeout (a stuck supervisor).
        if afk_wait_supervisor_gone "$off_pid" "$off_hb"; then
          echo "/afk: off (supervisor exited; state cleared)"; return 0
        fi
        echo "/afk: off requested — state cleared, but the supervisor (pid ${off_pid:-unknown}) is still alive after ${AFK_OFF_WAIT_SECONDS}s; it will exit on its next tick" >&2
        return 1
      fi
      echo "/afk: off (state cleared; the supervisor + watchdog stop on their next tick)"; return 0 ;;
    --watchdog)  watchdog_loop; return $? ;;
    --reconcile) afk_reconcile "$MAIN_ROOT"; return $? ;;
    -h|--help)   sed -n '2,87p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
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
    # Mint + bind a fresh arm generation (#252): the old sleeper from a prior arm reads its bound
    # token as superseded on its next tick and steps down, so an off/re-arm recycle never runs two.
    _AFK_ARM_EPOCH="$(afk_new_arm_token)"
    afk_write_arm_epoch "$_AFK_ARM_EPOCH"
    _clear_dispatch_epochs   # fresh window ⇒ empty "dispatched by this run" set
    _clear_progress_state    # fresh window ⇒ no stale progress / answer-attempt epochs
    _clear_resume_markers    # fresh window ⇒ every spoke gets its one auto-resume again
    _clear_redispatch_markers # fresh window ⇒ every clean crash gets its one re-dispatch again (#202 C)
    _clear_nudge_counts      # fresh window ⇒ every spoke gets its AFK_NUDGE_MAX_ATTEMPTS nudges again (#255)
    _afk_clear_landed_count  # fresh window ⇒ the landed tally starts at zero (#150)
    _afk_clear_drain_complete # ...and drop any un-consumed completion signal from a prior drain
    _clear_blocked_records   # fresh window ⇒ --status shows only THIS run's durable blocks
    _afk_clear_dispatch_fail_counts # fresh window ⇒ every issue's dispatch ceiling resets (#170)
    _afk_clear_land_retry_counts # fresh window ⇒ every issue's land-retry budget resets (#202 D)
    _afk_clear_land_conflict_fps # fresh window ⇒ drop stale conflicted-land tip fingerprints (#285)
    _clear_conflict_resolve_markers # fresh window ⇒ every conflict gets its one resolution dispatch (#285)
    _afk_clear_last_action   # fresh window ⇒ no stale last-action label from a prior drain (#202 B)
    _afk_clear_status_labels_seed # fresh window ⇒ re-seed the afk:* label set once (#223)
    _afk_clear_selfupdate_pending # fresh window ⇒ drop any stale self-update flag (#250)
    _afk_clear_respawn_log   # fresh window ⇒ the crash-loop guard starts with an empty window (#250)
    log "/afk: armed ($([ "$end" = drain ] && echo 'drain — until the backlog is empty' || echo "until $(wt_date_ymd "$end") $(date -r "$end" +%H:%M 2>/dev/null || date -d "@$end" +%H:%M)"))"
    # Power-management caveats the sleep inhibitor cannot cover (#242): loud, once at arm.
    afk_warn_power          # on battery: the inhibitor holds only on AC, and a lid-close sleeps
    _afk_warn_no_inhibitor  # non-macOS: no caffeinate — arming proceeds, but sleep is not inhibited
  else
    # No window spec and not --once: a RESUME of the persisted window (a watchdog respawn or
    # a manual re-run). Refuse if a supervisor is ALREADY live — a second one clobbers the
    # per-run state (#202 B, the arm-precondition dedup extended to the resume path). The
    # arm path already refuses this via afk_arm_preconditions; AFK_ARM_PRECHECK=0 opts out.
    # SELF-EXEMPTION (#250): a self-update deploy re-execs THIS process in place, so $$ is
    # preserved and the heartbeat still holds our own pid — a supervisor is never its own
    # "second supervisor". Exempt the resume when the live heartbeat pid == $$.
    local resume_hb resume_pid; resume_hb="$(afk_read_heartbeat)"; resume_pid="${resume_hb%% *}"
    if [ "${AFK_ARM_PRECHECK:-1}" != "0" ] && [ "$(afk_supervisor_state)" = "live" ] \
       && [ "$resume_pid" != "$$" ]; then
      log "/afk: refusing to resume — a supervisor is already live (heartbeat pid running); run /afk --off first (a second supervisor clobbers per-run state)"
      return 2
    fi
    # Adopt the persisted arm generation (#252): a resume (watchdog respawn / reconcile) must NOT
    # mint a new token — that would read itself superseded on tick one. Empty when a pre-#252
    # window (or a torn-down epoch file) is resumed, which reads never-superseded (legacy-safe).
    _AFK_ARM_EPOCH="$(afk_read_arm_epoch)"
  fi

  while :; do
    # Step down the instant a newer arm (or --off) superseded this generation (#252): the old
    # sleeper from an off/re-arm recycle exits here instead of draining alongside the new lineage.
    # UNGATED by AFK_ARM_PRECHECK — a singleton-drain safety invariant, not an arm precondition.
    # Skipped for --once (a one-shot tick binds no generation).
    if [ "$once" -eq 0 ] && afk_arm_superseded; then
      log "/afk: superseded — a newer arm (or --off) took over this checkout; stepping down (no double-drain)"
      break
    fi
    afk_write_heartbeat   # stamp this tick before working, so a crash mid-tick is visible
    # Keep exactly one watchdog alive (idempotent: a no-op while one runs, respawns it if
    # it died). Doing this each tick — not just at arm — means the supervisor and watchdog
    # heal each other: neither is a single silent point of failure (#107). Skipped for
    # --once (a one-shot cron tick must not leave a background keeper behind).
    [ "$once" -eq 0 ] && _afk_spawn_watchdog
    # Keep the Mac awake for the whole armed window (#242): arm a `caffeinate -is -w $$`
    # tied to THIS supervisor's lifetime. Idempotent each tick (re-arms a killed caffeinate);
    # a watchdog respawn re-ties to the new pid. Skipped for --once (no background inhibitor
    # for a one-shot cron tick, mirroring the watchdog-spawn skip).
    [ "$once" -eq 0 ] && _afk_arm_inhibitor "$$"
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
    # A --once tick must NOT self-exec a redeploy; but its land may have flagged one, so clear
    # the flag before exiting the one-shot — else it leaks on disk and a later no-arg resume
    # (which also skips the arm-branch clear) would spuriously redeploy (#250 review finding 3).
    if [ "$once" -eq 1 ]; then _afk_clear_selfupdate_pending; break; fi
    # Self-update BEFORE the done-check (#250, review finding 2): a supervisor-scope land can
    # ALSO empty the backlog, and afk_done would then break first and leave the synced copy
    # stale — the operator would still have to sync by hand. Deploy at the tick boundary (never
    # mid-tick) first. On success _afk_self_deploy execs this process in place onto the new code
    # (never returns); on a fail-safe fallback it returns and the drain continues on old code.
    if _afk_selfupdate_pending; then
      _afk_self_deploy
    fi
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
