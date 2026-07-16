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
#                                default: the supervisor's own scripts + the afk-answering rule
#                                + ai-toolkit.yml (#291 — a config-only land, e.g. model routing,
#                                also redeploys the synced spoke-model.env snapshot).
#                                A supervisor-scope land re-syncs + re-execs the drain in place
#                                onto the new code at the next tick boundary (see the self-update
#                                block below); AFK_SYNC_CMD / AFK_SELFUPDATE_SMOKE_CMD are seams.
#   AFK_ARM_PRECHECK=1           arm-precondition gate (=0 skips live/dirty/branch/gh-auth checks)
#   AFK_ARM_SELFCHECK=1          arm-time LIVENESS self-check (#279): real round trips against
#                                the judge, claude, the gh API and testmon, reported with the
#                                telemetry preflight as ONE verdict before any state is
#                                written. =0 waives those four LIVE PROBES (independent of
#                                AFK_ARM_PRECHECK); the telemetry gate still runs — its own
#                                opt-out is AI_TOOLKIT_OTEL=0. Arm-only: NOT run on --reconcile
#                                or a watchdog respawn, which are recovery paths
#   AFK_ARM_AUTH_TIMEOUT=120     seconds bounding the arm-time claude round trip (#279) — a COLD
#                                start, so between the reap probe's 30s and the answerer's 900s
#   AFK_GH_PROBE_ENDPOINT        the arm-time gh api round-trip endpoint [default: rate_limit];
#                                proves the API answers, not just that a token exists (#279)
#   AFK_TESTMON_PROBE_CMD        override the whole pytest-testmon probe (tests); rc 0 ⇒ present.
#                                Missing testmon WARNS (every first push degrades to the full
#                                suite) — it never blocks the arm (#279)
#   AFK_TESTMON_PROBE_TIMEOUT=60 seconds bounding the testmon probe's runner --help call (#279)
#   AFK_JUDGE_SENTINEL           the benign command the arm-time judge probe classifies (#279);
#                                see gate-broker-danger.sh (AFK_JUDGE_TIMEOUT bounds it)
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

# --- source the hub-afk functional modules (fail-CLOSED per #211) --------------
# hub-afk.sh is split into hub-afk-<lane>.sh modules (issue #307) so disjoint afk subtasks
# stop colliding on one multi-thousand-line Scope: token (AFK Design Principle 7). These are
# pure function-definition files (no top-level work beyond `: "${VAR:=default}"` guards),
# sourced AFTER worktree-lib/gate-broker/log/afk_now and BEFORE any function is called — bash
# resolves calls at call time, so cross-module definition order does not matter.
#
# Resolution is from _AFK_DIR (THIS file's own BASH_SOURCE dir, NOT the inherited SCRIPT_DIR a
# self-copy supervisor may point at a temp dir, #262) FIRST, then the source-tree and synced
# .ai-toolkit/scripts layouts. FAIL-CLOSED (constraint 4 / #211): a `source` of a missing file
# can exit the shell as a special builtin, so each candidate is [ -r ]-guarded; a required
# module that resolves nowhere sets _AFK_MODULES_OK=0 and main() REFUSES TO ARM rather than run
# a partial drain (AFK Design Principle 2 — fail loud, never silently degrade).
_AFK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The entry's OWN path, captured here where BASH_SOURCE[0] is hub-afk.sh. Moved supervisor
# functions (_afk_self, _afk_exec_self_copy) that need "this script" must read $_AFK_ENTRY, NOT
# their own BASH_SOURCE[0] — once they live in hub-afk-supervise.sh, BASH_SOURCE[0] there is the
# MODULE, and a resume/self-copy would relaunch the wrong file (#307).
_AFK_ENTRY="${BASH_SOURCE[0]}"
_AFK_MODULES_OK=1
for _mod in land dispatch arm supervise recover; do
  _afkm=""
  for _cand in \
    "$_AFK_DIR/hub-afk-$_mod.sh" \
    "$SCRIPT_DIR/hub-afk-$_mod.sh" \
    "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/shared/skills/hub/scripts/hub-afk-$_mod.sh}" \
    "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/hub-afk-$_mod.sh}"; do
    if [ -n "$_cand" ] && [ -r "$_cand" ]; then _afkm="$_cand"; break; fi
  done
  if [ -n "$_afkm" ]; then
    . "$_afkm"
  else
    log "hub-afk: FATAL required module hub-afk-$_mod.sh missing/unreadable -- refusing to arm"
    _AFK_MODULES_OK=0
  fi
done
unset _mod _afkm _cand

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

# --- #300 step 3 lifecycle transition log (shadow writers) --------------------
# The drain-side actors record the transition/event they CAUSE at the moment they act, the
# same principle step 2 wired into the spoke/land actors (9d7acbe). These front the guarded
# worktree-lib wrappers (no-op on a non-numeric issue or an absent transition-log lib, so a
# write never fails the drain) and stamp the spoke's run id onto every record, so a whole
# lifecycle is greppable by run even across a revive/redispatch. Shadow-only: no detector
# reads the log for decisions in this step. <wt> supplies the run id; pass it BEFORE a
# redispatch tears the worktree down.
_afk_tlog_transition() {
  local wt="$1" issue="$2" to="$3" cause="$4" evidence="${5:-}"
  AFK_TLOG_RUN="$(_afk_spoke_run_id "$wt")" \
    wt_tlog_transition "$issue" "$to" hub-afk.sh "$cause" "$evidence"
}
_afk_tlog_event() {
  local wt="$1" issue="$2" event="$3" lane="${4:-}" evidence="${5:-}"
  AFK_TLOG_RUN="$(_afk_spoke_run_id "$wt")" \
    wt_tlog_event "$issue" "$event" hub-afk.sh "$lane" "" "$evidence"
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
# _afk_pending_queued_subtasks -> every issue still queued for some spoke, one per line.
# Empty when nothing is owed. Walks the channel's per-spoke dirs (#278) directly rather than
# via read_queued_subtask, because the spoke numbers are exactly what we don't know here.
_afk_pending_queued_subtasks() {
  local dir d f
  dir="$(_afk_state_dir)"
  [ -d "$dir" ] || return 0
  for d in "$dir"/queued-*; do
    [ -d "$d" ] || continue
    for f in "$d"/*; do
      [ -f "$f" ] || continue
      printf '%s\n' "${f##*/}"
    done
  done
  return 0
}

afk_done() {
  local state="$1" now="$2" bp inflight_count batch tok sub remaining=""
  [ -n "$state" ] || return 0
  window_expired "$state" "$now" && return 0
  # The backlog-drained stop below is drain-mode-only: a non-expired clock-bound (numeric)
  # state keeps ticking regardless of the backlog, so it never self-completes on tick one
  # when the whole backlog is empty / held / poisoned (#222).
  [ "$state" = drain ] || return 1
  inflight_count="$(inflight_issues | grep -c '^[0-9]' || true)"
  [ "$inflight_count" -eq 0 ] || return 1
  # #278: a routed-but-unconsumed subtask is real work this window still owes. Declaring the
  # drain complete over it would end the window with an issue queued and never done — and the
  # queue outlives the spoke's worktree, so "nothing in flight" is no longer proof of an idle
  # drain on its own. An empty queue DIR is not pending work (the spoke drained it and the dir
  # simply remains), so test the entries, not the directory.
  if [ -n "$(_afk_pending_queued_subtasks)" ]; then
    log "/afk: subtask(s) still queued for a spoke — not declaring the drain done"
    return 1
  fi
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
  # A token is a comma-joined GROUP since #278 ("263,265"), so split it: left whole it matches
  # the non-numeric arm below and is KEPT as an unrecognized token, holding a fully-poisoned
  # backlog open forever — the exact never-completing drain #202 F fixed for single issues.
  for tok in $batch; do
    case "$tok" in
      route:*) continue ;;   # the #278 route channel is not backlog work
    esac
    for sub in ${tok//,/ }; do
      case "$sub" in *[!0-9]*) remaining="$remaining $sub"; continue ;; esac   # non-issue token — keep
      _afk_issue_poisoned "$sub" && continue
      remaining="$remaining $sub"
    done
  done
  [ -z "$(printf '%s' "$remaining" | tr -d '[:space:]')" ]
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
    # Print the header comment block: every line from 2 up to (not including) the first
    # non-comment line. Derived rather than a hard-coded upper bound (was '2,87p'): the #279
    # env knobs pushed the block past 87, which silently truncated the whole Usage section out
    # of --help. A line range that must be hand-maintained on every header edit will drift
    # again; `/^[^#]/q` cannot.
    -h|--help)   sed -n '2,${/^[^#]/q;p;}' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
  esac

  # A missing/unreadable functional module (issue #307) leaves the drain unable to
  # dispatch/land/reap — refuse to ARM loudly rather than run a partial drain (AFK Design
  # Principle 2). Placed after the read-only subcommands (--status/--off/--help) return, so
  # inspecting a half-synced tree still works; only the loop-entering paths are gated.
  if [ "${_AFK_MODULES_OK:-1}" != "1" ]; then
    log "hub-afk: refusing to arm — a required functional module failed to load (FATAL above)"
    return 1
  fi

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
    # Liveness self-check BEFORE arming (#279): the static preconditions above all passed on
    # the #268 host while the tier-3 judge was structurally dead, so the drain armed clean and
    # ground every permission to DENY for an hour. This runs the REAL round trips — judge,
    # claude, gh API, testmon — and folds in the #108 telemetry preflight, so the operator
    # gets ONE arm-time verdict. Same refuse-to-arm posture: write no state, never reach the
    # loop, dispatch nothing into a dependency that cannot answer.
    afk_arm_selfcheck "$MAIN_ROOT" || return 2
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
