#!/usr/bin/env bash
# hub-afk-supervise.sh -- split out of hub-afk.sh (issue #307).
#
# The runtime-SUPERVISION lane of the /afk supervisor: the sleep inhibitor (caffeinate), the
# watchdog + respawn, the self-update / self-deploy protocol (deploy the supervisor's own
# landed code), the respawn crash-loop guard + kill-wedged, and the restart-survival re-arm.
# A pure function-definition module sourced by the entry lib hub-afk.sh AFTER worktree-lib /
# gate-broker / log / afk_now and the entry's own state/time primitives, and BEFORE any
# function is called, so every cross-module helper resolves at call time. Not run on its own.
set -uo pipefail

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
_afk_self() { printf '%s\n' "${AFK_ORIG_SCRIPT:-${_AFK_ENTRY:-${BASH_SOURCE[0]}}}"; }

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
  local src src_dir dir copy cfg
  # The ENTRY's path, not THIS module's BASH_SOURCE[0] (#307): the self-copy must re-exec
  # hub-afk.sh and stamp AFK_ORIG_SCRIPT with the entry, or the resumed process would resolve
  # _afk_self to hub-afk-supervise.sh. src_dir is the same scripts dir either way (co-located).
  src="${_AFK_ENTRY:-${BASH_SOURCE[0]}}"
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
  # Seed the model-config manifest too (#306): spoke-model.env / ai_toolkit_config.py are
  # NOT *.sh, so the glob above skips them. Without spoke-model.env, worktree-new.sh -- which
  # the drain resolves to THIS self-copy dir -- finds no config in wt_resolve_agent_model and
  # falls to the literal fallback, dispatching EVERY freshly-armed spoke on the wrong tier
  # until the first _afk_resync reseeds. Parity with _afk_resync (which re-renders both).
  # Best-effort / fail-open: a source-run hub has neither, and the copy proceeds regardless.
  for cfg in spoke-model.env ai_toolkit_config.py; do
    [ -f "$src_dir/$cfg" ] && cp "$src_dir/$cfg" "$dir"/ 2>/dev/null
  done
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
  printf '%s\n' "${AFK_SELFUPDATE_SCOPE:-hub-afk.sh hub-afk-land.sh hub-afk-dispatch.sh hub-afk-arm.sh hub-afk-supervise.sh hub-afk-recover.sh hub-watchdog.sh gate-broker.sh hub-notify.sh worktree-lib.sh worktree-land.sh batch-plan.sh afk-answering.md ai-toolkit.yml}"
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
# add its source path here too, or a broken version of it would deploy unvalidated. EXCEPTION
# (#291): ai-toolkit.yml is data, not a supervisor script, so it has no source path here —
# `bash -n` has nothing to parse-check. It's still safe to redeploy on since sync-to-repo.sh
# re-renders spoke-model.env from it on every _afk_resync.
_afk_selfupdate_source_scripts() {
  local root="${1:-${MAIN_ROOT:-.}}"
  printf '%s\n' \
    "$root/shared/skills/hub/scripts/hub-afk.sh" \
    "$root/shared/skills/hub/scripts/hub-afk-land.sh" \
    "$root/shared/skills/hub/scripts/hub-afk-dispatch.sh" \
    "$root/shared/skills/hub/scripts/hub-afk-arm.sh" \
    "$root/shared/skills/hub/scripts/hub-afk-supervise.sh" \
    "$root/shared/skills/hub/scripts/hub-afk-recover.sh" \
    "$root/shared/skills/hub/scripts/hub-watchdog.sh" \
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
# We normally run from OUR OWN frozen self-copy (#133), so _afk_find_script resolves $wd to
# hub-watchdog.sh's copy in that SAME tmp dir — a bundle no land ever rewrites. Hand over the
# ORIGIN sibling as HUB_WATCHDOG_ORIG_SCRIPT (mirroring AFK_ORIG_SCRIPT, the same contract we
# carry for ourselves), derived from AFK_ORIG_SCRIPT's directory since it and hub-watchdog.sh
# live side by side in the real checkout; without this the watchdog's #296 self-recycle hashes
# the frozen copy forever, structurally dead exactly like before that fix.
_afk_arm_hub_watchdog() {
  [ "${HUB_WATCHDOG_COARM:-1}" = "1" ] || return 0
  if [ -n "${HUB_WATCHDOG_ARM_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_ARM_CMD" >/dev/null 2>&1 || true; return 0; fi
  local wd orig
  wd="$(_afk_find_script "${HUB_WATCHDOG_BIN:-}" hub-watchdog.sh)" || return 0
  orig="$wd"
  if [ -n "${AFK_ORIG_SCRIPT:-}" ] && [ -f "$(dirname "$AFK_ORIG_SCRIPT")/hub-watchdog.sh" ]; then
    orig="$(dirname "$AFK_ORIG_SCRIPT")/hub-watchdog.sh"
  fi
  HUB_WATCHDOG_ORIG_SCRIPT="$orig" bash "$wd" --arm >/dev/null 2>&1 || true
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
  # NO #279 liveness self-check here, deliberately. Reconcile looks like a re-arm, but it is
  # the RECOVERY path: hub-watchdog.sh's _wd_intervene_rearm recovers a crashed drain with
  # `bash hub-afk.sh --reconcile >/dev/null 2>&1 || true`, discarding both the output and the
  # exit code. Gating that on live judge/claude/gh round trips would mean a transient outage
  # (the very thing most likely to be happening around a crash) SILENTLY blocks recovery: the
  # watchdog records the intervention, the refusal log goes to /dev/null, .afk-state stays
  # armed, and every in-flight spoke strands with no answerer or lander — the ~10h overnight
  # strand this function exists to prevent (#202 A). It would also block each watchdog tick for
  # up to ~4 minutes of probes before the per-spoke detectors run.
  #
  # Refusing to resume is strictly worse than resuming degraded: a dead judge still leaves the
  # lander and reaper working, and #268's judge halt plus #241 §9's auth halt already catch a
  # dependency that dies mid-window. The self-check is therefore ARM-ONLY (#279) — the moment
  # a fresh window is about to dispatch its first spoke into those dependencies.
  if ! afk_telemetry_preflight "$repo_root"; then
    log "/afk reconcile: telemetry preflight failed — not re-arming (see above)"
    return 1
  fi
  _afk_watchdog_respawn   # detached no-arg resume — re-adopts the in-flight spokes
  _afk_spawn_watchdog     # keep exactly one watchdog alive (idempotent)
  log "/afk reconcile: re-armed — supervisor resumed, watchdog ensured"
  return 0
}

