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
for _mod in land dispatch arm supervise; do
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

# --- crash ≠ hang: auto-resume-once a pane-dead spoke (issue #109) -------------
# A reaped spoke is not always hung. The reaper abandoned #103 as "idle, likely hung"
# when its tmux PANE had crashed but its committed work was intact. So before declaring
# blocked we distinguish a DEAD pane (session crashed → re-adopt the worktree ONCE,
# reusing the spoke_run_id) from a LIVE-but-idle pane (truly hung → block).

# _spoke_pane_alive <wt> -> true when the spoke's AGENT is running in a tmux pane mapped to
# the worktree. Two ways to be dead: no pane maps at all (the window crashed / is gone), OR a
# pane maps but runs a bare shell with no agent beneath it (#301: the spoke is launched as
# `sh -c "<cmd>; exec zsh"`, so a killed claude — reboot, OOM, a human quitting it — leaves the
# pane alive running zsh in the worktree). Before #301 only the first was checked, so the second
# read as a healthy spoke and stranded #296/#299: never revived, and answers typed into the shell.
#
# The agent probe fails OPEN here — the OPPOSITE direction from the inject primitives' write-side
# _pane_agent_ready. A write refuses on an unprovable probe (rc 2) because the cost of guessing
# wrong is prose executed as a shell command; liveness instead keeps an unobservable pane ALIVE,
# because the cost of guessing wrong is killing + relaunching a HEALTHY spoke. So only a PROVEN
# dead agent (rc 1) flips a mapped pane to dead; rc 0 and rc 2 both read alive.
# The pane target is resolved ONCE per call and reused for the probe: a second _spoke_pane_target
# would be a second `tmux list-panes` against a loaded server, the shape that flaked #269.
# UPGRADE: memoize the verdict per (wt, tick) if reap-tick cost becomes a problem — this went from
# one `tmux list-panes` to list-panes + display-message + a `ps -eo` scan, and a single tick calls
# it several times per spoke across _reap_or_resume / _afk_finish_up_or_revive / recover_dead_panes
# (plus slot_state's own _detect_agent_dead). Not cached yet on purpose: a per-tick cache risks a
# stale ALIVE masking a pane that crashed mid-tick, and one ps scan is cheap next to the hours of
# stranding a miss costs — revisit only if profiling shows the probe dominating a tick.
_spoke_pane_alive() {
  local target rc
  target="$(_spoke_pane_target "$1")"
  [ -n "$target" ] || return 1     # no pane maps ⇒ the window crashed / is gone
  _pane_agent_alive "$target"; rc=$?
  [ "$rc" -ne 1 ]                  # rc 1 (proven dead) ⇒ dead; rc 0 alive, rc 2 unprovable ⇒ alive
}

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

# _afk_route_subtask_prompt <spoke> <issue> -> the #278 message telling a LIVE spoke that a
# newly-filed issue sharing its scope has been queued onto its branch. Deliberately does NOT
# say "stop what you are doing": the spoke finishes its current subtask first, and the queue
# is consumed at the ready boundary — the only point where its tree is provably clean and
# pushed, so a fresh RED can never land in a tree with an in-flight push gate running.
_afk_route_subtask_prompt() {
  local spoke="$1" issue="$2" marker_dir
  marker_dir="$(wt_marker_script_dir "${_AFK_TOPLEVEL:-.}")"
  cat <<EOF
Issue #$issue was just filed and shares this spoke's scope, so it has been QUEUED onto THIS
branch as a subtask rather than spawning a second worktree (which would pay another full
spawn + first-push suite seed + review + land for the same files).

Finish what you are doing first -- do NOT abandon the current subtask. Then, at your next
clean-and-pushed boundary: run '/source-task $issue' to re-anchor on it, work its full
solo-cycle (RED -> GREEN -> REVIEW -> PUSH), and emit
'bash ${marker_dir}/spoke-push.sh --ready $issue' -- that clears it from your queue.

Check what you still owe with 'bash ${marker_dir}/spoke-ready.sh --queued $spoke'. Your
terminal 'bash ${marker_dir}/spoke-push.sh --ready $spoke' is REFUSED until that prints
nothing, so emit it only once the queue is empty. Do NOT self-land -- the hub lands these.
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

# _afk_pushed_unmarked_prompt <issue> -> the #305 nudge for a clean-pushed tip that carries NO
# ready marker and is NOT over the ceiling (the #200 shape #299 stranded on): the tree is pushed
# and clean but the spoke stopped without emitting ready. Distinct from _afk_finish_up_prompt (an
# over-ceiling finish-up) so the message is accurate -- it says nothing about a time ceiling. Names
# the marker-emitter path that EXISTS in the spoke's worktree (the #271 probe).
_afk_pushed_unmarked_prompt() {
  local issue="$1" marker_dir
  marker_dir="$(wt_marker_script_dir "${_AFK_TOPLEVEL:-.}")"
  cat <<EOF
Your branch is pushed and your working tree is clean, but you never emitted the ready marker for
#$issue -- and nothing is blocking you (no question or permission dialog is pending). If the
issue's acceptance criteria are ALL met, emit the ready marker now
(bash ${marker_dir}/spoke-push.sh --ready $issue). If you still owe subtasks, re-read your task
ledger and the working tree, then continue the solo flow (RED -> GREEN -> REVIEW -> PUSH) from
where you left off, pushing each subtask. Do NOT self-land -- the hub lands #$issue.
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
  # #300 step 3: re-adopting a crashed-but-intact pane is a revive transition (cause distinct
  # from _revive_spoke's kill-and-relaunch — this one never kills, the pane was already dead).
  _afk_tlog_transition "$wt" "$issue" revived \
    "pane crashed with work intact — re-adopted in place once" '{"path":"resume"}'
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
    # GNU `-c %Y` FIRST, BSD `-f %m` second (#289/#132). Reversed, this breaks on GNU: there
    # `-f` selects filesystem-status mode and takes no inline format, so `%m` is read as a file
    # operand -- GNU errors on it yet still PRINTS a multi-line fs block for the real file and
    # exits nonzero, so the `||` fallback ALSO runs and the capture holds the garbage AND the
    # epoch. That fails _afk_write_fingerprint's all-digits guard, stranding the silence delta
    # (the hang's tell) as `unknown`. BSD rejects `-c` cleanly, so GNU-first is safe on both.
    mtime="$(stat -c %Y "$jsonl" 2>/dev/null || stat -f %m "$jsonl" 2>/dev/null)"
  fi
  _afk_write_fingerprint "$issue" "$now" "${mtime:-}" "$jsonl" > "$dir/fingerprint.txt" 2>/dev/null || true
  printf '%s\n' "$dir"
}

# --- #241 §7/§8: revive-first, warned-parked-LAST, never abandon -----------------
# The reaper no longer kills a stuck spoke into blocked/<issue>. Every former reap TAKES a
# revival first (kill any hung/crashed pane + relaunch `claude --continue`); only a spoke whose
# revival was ALREADY tried this window downgrades to warned-and-parked-LAST (warn + journal +
# arm the warned-retry backoff, retried at low frequency), NEVER killed or abandoned.

# AFK_WARN_ESCALATE_ATTEMPTS: #305 — the warn count after which a mode=afk warn-park that is
# STALLING scope-blocked dependents stops warn-parking silently and escalates blocked/<issue>. The
# count is the warned-retry `attempt` (exponential backoff), so the default 3 is ~7 min of standing
# failure (60 + 120 + 240s), not 3 ticks — long enough that a transient blip clears first, short
# enough that a night is never lost. A non-numeric override falls back so a typo can't disable the
# escalation (mirroring AFK_NUDGE_MAX_ATTEMPTS' guard).
: "${AFK_WARN_ESCALATE_ATTEMPTS:=3}"
case "$AFK_WARN_ESCALATE_ATTEMPTS" in '' | *[!0-9]*) AFK_WARN_ESCALATE_ATTEMPTS=3 ;; esac

# _afk_warn_attempt <issue> [lane] -> the warned-retry attempt count already tracked in
# _afk_warned_state_file ("<attempt>\t<next>"), or 0 when never warned on that lane. Reads the
# gate-broker record directly (the file has no field-1 reader — _afk_warned_next reads field 2).
_afk_warn_attempt() {
  local f a=0
  f="$(_afk_warned_state_file "$1" "${2:-}")"
  [ -f "$f" ] && IFS=$'\t' read -r a _ <"$f" 2>/dev/null || true
  case "$a" in '' | *[!0-9]*) a=0 ;; esac
  printf '%s\n' "$a"
}

# _warn_parked_last <wt> <issue> <reason> [park_kind=reap] -> the never-abandon replacement for
# reap_spoke: keep the spoke in rotation on the warned-retry backoff. NO window kill, NO
# blocked/<issue>. It HONORS the backoff — it warns + journals only when the spoke is DUE, and
# parks LAST SILENTLY inside the backoff window — so a permanently-stuck spoke is retried (and
# re-warned) at LOW frequency, not warned + gh-commented every 5-minute tick. reversible: the
# spoke's committed work is intact.
#
# #305 exception — the ONE place a warn-park is NOT cheap: an unattended (mode=afk) park that has
# persisted past AFK_WARN_ESCALATE_ATTEMPTS WHILE other issues are scope-blocked behind it. Silence
# there costs the whole window + everything queued (the #299 shape). So a DUE such park escalates a
# loud, reversible blocked/<issue> (the one marker hub-notify pings under a live drain) naming the
# stalled dependents, instead of warn-parking again. Gated on all three — a positive afk read, the
# attempt bound, AND real dependents — so it is inert for: attended parks (the human is the wall,
# AC2), worktree-less parks (dispatch failures, wt=""), and any park with nothing waiting behind it
# (warn-parking is genuinely harmless then). The irreversible/outward carve-out is untouched.
_warn_parked_last() {
  local wt="$1" issue="$2" reason="$3" park="${4:-reap}" lane
  lane="$(_afk_warned_lane "$park")"
  # Gate on the SAME lane broker_warn_continue arms for this park kind (#274): a land/review park
  # reads/arms auto_land's LAND lane, every other kind the default lane — so the due-check and the
  # arm stay on one clock. Inside the backoff → parked LAST silently this tick.
  _afk_warned_due "$issue" "" "$lane" || return 0
  # #305: past-bound afk park stalling dependents → escalate loudly instead of re-warning.
  if [ -n "$wt" ] && [ "$(_afk_spoke_mode "$wt")" = afk ] \
     && [ "$(_afk_warn_attempt "$issue" "$lane")" -ge "$AFK_WARN_ESCALATE_ATTEMPTS" ]; then
    local behind; behind="$(_afk_scope_blocked_behind "$issue")"
    if [ -n "$behind" ]; then
      local ereason="$reason — persisted past its warn bound (${AFK_WARN_ESCALATE_ATTEMPTS} warns) while STALLING scope-blocked dependents: $behind. Escalated blocked/$issue for a human (#305)."
      log "→ warn-escalate #$issue: $ereason"
      _afk_set_last_action "warn-escalate #$issue"
      broker_journal_decision "$issue" "$park" "$ereason" reversible
      _afk_park_terminal "$wt"
      _afk_escalate_blocked "$wt" "$issue" "$ereason"
      return 0
    fi
  fi
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
  # #300 step 3: the drain reviving this spoke is a lifecycle transition — record it.
  _afk_tlog_transition "$wt" "$issue" revived \
    "killed a hung/crashed pane and relaunched claude --continue" \
    "{\"path\":\"revive\"${bundle:+,\"forensics\":\"$bundle\"}}"
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
  # #300 step 3: the #255 nudge lane records its event (delivered vs retry via the rc), so a
  # reader can tell "the drain nudged this spoke" apart from "the spoke is silently idle".
  _afk_tlog_event "$wt" "$issue" nudge nudge \
    "{\"delivered\":$([ "$rc" -eq 0 ] && printf true || printf false)}"
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

# _afk_pushed_unmarked_nudge <wt> <issue> -> #305: the first rung of the pushed-but-unmarked ACT
# ladder. Injects the emit-ready / continue nudge (_afk_pushed_unmarked_prompt) into the LIVE
# session via the shared hardened injector, then journals a `markready` decision. Mirrors
# _afk_finish_up_nudge: stamps only the answer-attempt epoch (never the progress epoch — a nudge
# must not buy a fresh full ceiling), and rc mirrors inject_and_verify (the caller counts the
# attempt against the shared #255 budget). Caller wraps this in _afk_run_with_heartbeat_fg.
_afk_pushed_unmarked_nudge() {
  local wt="$1" issue="$2" target rc
  log "→ pushed-unmarked-nudge #$issue: clean pushed tip, no ready marker — injecting the emit-ready / continue nudge (no relaunch)"
  _afk_set_last_action "pushed-unmarked-nudge #$issue"
  target="$(_spoke_pane_target "$wt")"
  if [ -z "$target" ]; then
    log "  no live pane for #$issue — cannot nudge"
    return 1
  fi
  stamp_answer_attempt "$issue"
  inject_and_verify "$wt" "$target" "$(_afk_pushed_unmarked_prompt "$issue")"; rc=$?
  broker_journal_decision "$issue" markready \
    "pushed-but-unmarked: nudged the live session to emit ready / continue the cycle (no relaunch, #305)" reversible
  if [ "$rc" -eq 0 ]; then _afk_emit_span "$wt" afk-pushed-unmarked-nudge success; else _afk_emit_span "$wt" afk-pushed-unmarked-nudge retry; fi
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

# _afk_warn_pushed_but_unmarked <wt> <issue> -> #200/#241/#305: the pushed-but-unmarked handler,
# dispatched on the spoke's execution mode. The shape is AMBIGUOUS — genuinely finished (the marker
# just failed, as #299) vs idle BETWEEN subtasks — and today's warn-and-wait is correct ONLY when a
# human is the wall. So:
#   * mode=attended -> keep the warn-and-parked-LAST warn (the human re-runs --ready or lands by
#     hand); NOT auto-marked, since auto-emitting ready could auto-LAND incomplete work onto main.
#   * mode=afk -> the human is NOT there, so warn-parking forever wastes the window (the #299
#     incident: a 10h stall that jammed everything scope-blocked behind it). ACT via the
#     nudge -> relaunch -> decide ladder (_afk_act_pushed_but_unmarked) instead.
# The mode is read from _afk_spoke_mode (gate-broker-permission.sh's empty-default helper, the
# deny-wall's fail-safe signal); the attended DEFAULT is applied HERE (afk only on a positive read),
# so a missing/unknown pointer keeps today's warn-and-wait — the conservative, regression-safe side.
_afk_warn_pushed_but_unmarked() {
  local wt="$1" issue="$2"
  if [ "$(_afk_spoke_mode "$wt")" = afk ]; then
    _afk_act_pushed_but_unmarked "$wt" "$issue"
    return
  fi
  _warn_parked_last "$wt" "$issue" \
    "pushed-but-unmarked (#200): clean tip, no ready/$issue marker — if finished, re-run 'spoke-push.sh --ready $issue' or land by hand" \
    markready
}

# _afk_act_pushed_but_unmarked <wt> <issue> -> #305: the ACT ladder for a mode=afk clean-pushed tip
# with no marker. nudge -> relaunch -> decide, each rung bounded by an existing per-window counter so
# it CANNOT loop forever (the AC1 "never warn-parks indefinitely" guarantee):
#   1. nudge   — a finished-turn-idle pane under the shared #255 nudge budget gets the emit-ready /
#                continue nudge injected into its LIVE session (no relaunch). This is exactly the
#                lane the pushed-but-unmarked warn short-circuited PAST before #305.
#   2. relaunch— nudge budget spent (or the pane is hung/dead) and not yet revived this window ->
#                _revive_spoke (kill + claude --continue; committed work survives, as #299's did).
#   3. decide  — nudge budget spent AND already revived -> _afk_decide_pushed_but_unmarked: a LOUD
#                terminal blocked/<issue> escalation (never an auto-land of ambiguous work).
# Because blocked/<issue> at the tip reads terminal (`done`) in slot_state, the decide rung takes the
# spoke OUT of the reap rotation — the ladder always terminates.
_afk_act_pushed_but_unmarked() {
  local wt="$1" issue="$2"
  if _spoke_pane_alive "$wt" \
     && _transcript_finished_turn_idle "$wt" \
     && [ "$(_afk_read_nudge_count "$issue")" -lt "$AFK_NUDGE_MAX_ATTEMPTS" ]; then
    _afk_incr_nudge_count "$issue" >/dev/null
    _afk_run_with_heartbeat_fg _afk_pushed_unmarked_nudge "$wt" "$issue"
    return 0
  fi
  if ! _afk_already_resumed "$issue"; then
    _revive_spoke "$wt" "$issue" && return 0
    # revival launch could not start — fall through to the terminal decision.
  fi
  _afk_decide_pushed_but_unmarked "$wt" "$issue"
}

# _afk_decide_pushed_but_unmarked <wt> <issue> -> #305: the ladder's terminal rung. Nudge + relaunch
# both failed to produce a marker, so the spoke is genuinely stuck at a clean-pushed tip. The
# acceptance evidence is on disk (clean tree, pushed==upstream), but the shape is ambiguous with
# idle-between-subtasks, so we do NOT auto-land — we escalate a LOUD, reversible blocked/<issue> (the
# one marker hub-notify pings under a live drain) naming any scope-blocked dependents, and journal
# the decision (#241) so the morning review can land it or re-run --ready.
_afk_decide_pushed_but_unmarked() {
  local wt="$1" issue="$2" behind reason
  behind="$(_afk_scope_blocked_behind "$issue")"
  reason="pushed-but-unmarked (#200/#305): clean pushed tip, no ready/$issue marker; nudge + relaunch both failed to produce it${behind:+ — STALLING scope-blocked dependents: $behind}. Landing evidence is on disk (clean tree, pushed==upstream); escalated blocked/$issue for a human to land or re-run --ready."
  broker_journal_decision "$issue" markready "$reason" reversible
  _afk_park_terminal "$wt"
  _afk_escalate_blocked "$wt" "$issue" "$reason"
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
  local wt="$1" issue="$2" wt_done run
  # #300 step 3: read the run id BEFORE the teardown — worktree-done.sh removes the worktree,
  # so .ai-toolkit/spoke-run-id is gone by the time we'd record the redispatched transition.
  run="$(_afk_spoke_run_id "$wt")"
  log "→ redispatch #$issue: pane crashed with no work to preserve — tearing down the empty worktree so it re-dispatches"
  _kill_spoke_window "$issue"
  if [ -n "${AFK_REDISPATCH_CMD:-}" ]; then
    bash -c "$AFK_REDISPATCH_CMD"; _afk_mark_redispatched "$issue"
    AFK_TLOG_RUN="$run" wt_tlog_transition "$issue" redispatched hub-afk.sh \
      "pane crashed with no work to preserve — tore down the empty worktree to re-dispatch" \
      '{"path":"redispatch-cmd"}'
    return 0
  fi
  wt_done="$(_afk_find_script "${WT_DONE:-}" worktree-done.sh)" \
    || { log "  worktree-done.sh not found — cannot re-dispatch #$issue"; return 1; }
  if bash "$wt_done" "$issue" --force --no-code >/dev/null 2>&1; then
    _afk_mark_redispatched "$issue"
    AFK_TLOG_RUN="$run" wt_tlog_transition "$issue" redispatched hub-afk.sh \
      "pane crashed with no work to preserve — tore down the empty worktree to re-dispatch" \
      '{"path":"worktree-done"}'
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
    # `done` is terminal regardless of liveness (a ready/accept/blocked marker is a human/gate
    # decision — never revive over it).
    case "$state" in done) continue ;; esac
    # `waiting` means "parked, the answer lane owns it" — but a park is only real if the AGENT is
    # there to be answered. #301: a dead agent whose pane still renders a stale dialog, or carries
    # a gate/<issue> tag at the tip (the #296/#299 shape), classifies `waiting` off scrollback / a
    # git tag that outlived the agent; skipping such a pane here would strand the very crash this
    # function exists to recover. So honor `waiting` (and hand the live pane to reap_pass) only
    # when the agent is alive — ST3 also stops slot_state emitting it, this is the belt to that
    # braces. Probed ONCE (a second _spoke_pane_alive is a second `tmux list-panes` — the #269 flake).
    if _spoke_pane_alive "$path"; then continue; fi        # live pane — reap_pass / answer lane own it
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
