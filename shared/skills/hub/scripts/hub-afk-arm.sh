#!/usr/bin/env bash
# hub-afk-arm.sh -- split out of hub-afk.sh (issue #307).
#
# The ARM-time lane of the /afk supervisor: the telemetry preflight,
# the sleep-inhibitor / power status warnings, and the arm-time liveness probes +
# preconditions + the ONE arm verdict + the self-check -- everything that runs ONCE before
# the supervisor loop starts. A pure function-definition module sourced by the entry lib
# hub-afk.sh AFTER worktree-lib / gate-broker / log / afk_now and hub-afk-state.sh's state/time
# primitives, and BEFORE any function is called, so every cross-module helper resolves at
# call time. Not run on its own.
set -uo pipefail

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
# conf file — env wins) and EXPORT it + LANGFUSE_HOST so
# every dispatched spoke inherits working credentials. Also exports AI_TOOLKIT_OTEL=1 so
# spokes opt in to native OTel. rc 1 when no auth can be resolved (caller refuses to arm).
afk_resolve_telemetry_auth() {
  local conf="${AFK_TELEMETRY_CONF:-$HOME/.afk-telemetry}"
  # Source the conf for BOTH fields (auth and host) with env winning each independently
  # (save-source-restore), so an operator can set auth in the env and still pick host up
  # from the file (and vice versa).
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

# --- arm-time liveness probes (issue #279) ------------------------------------
# afk_arm_preconditions below is STATIC: a live supervisor, a clean tree, HEAD on base, a
# valid gh token. Every one of those passed on the #268 host while the tier-3 judge was
# structurally dead (a 2s budget could not cover a `claude -p` cold start), so /afk armed
# clean, dispatched, and fail-closed every uncached tier-3 verdict to DENY for ~an hour --
# diagnosed only by autopsying a stranded spoke's judge-cache.
#
# The whole failure CLASS is that shape: a dependency the drain cannot run without is dead,
# every static check passes anyway, and the drain silently grinds. These are the REAL round
# trips that catch it before a single spoke dispatches. Each is bounded (a dead dependency
# must never hang the arm itself) and each has a stub seam so no test needs a live `claude`,
# a network, or a real `gh`.

# _afk_arm_judge_check -> rc 0 when the tier-3 judge answered with a parsed verdict, else rc 1.
# Prints the probe's reason either way (the caller logs it). Delegates to gate-broker-danger's
# broker_judge_probe, which runs the REAL prompt/command/budget on a benign sentinel and writes
# no verdict cache, streak, or halt (#268). hub-afk.sh already sources gate-broker.sh, so this
# is a direct call; a broker without the probe (a stale sync) reads as unavailable rather than
# silently passing -- an unprovable judge is exactly what this check exists to refuse.
_afk_arm_judge_check() {
  if ! command -v broker_judge_probe >/dev/null 2>&1; then
    printf 'judge probe unavailable (stale gate-broker.sh -- re-sync)\n'
    return 1
  fi
  local report
  report="$(broker_judge_probe)"; local rc=$?
  printf '%s\n' "${report#*$'\t'}"   # drop the AVAILABLE/UNAVAILABLE tag, keep the reason
  return "$rc"
}

# _afk_arm_claude_check -> print offline | auth-dead | unresponsive | alive; rc 0 ONLY for
# alive. Reuses #249's machinery -- _afk_network_is_down first (so a connectivity blackout is
# never reported as "your token is dead"), the same AFK_AUTH_PROBE_CMD seam, and the same
# is_auth_failure signature detector -- but it deliberately does NOT call _afk_probe_state.
#
# WHY NOT: _afk_probe_state fails OPEN by design. A nonzero exit WITHOUT an auth signature --
# `claude` absent from PATH, a wedged CLI, a probe killed at its budget -- reads as "alive"
# there, and that is CORRECT at reap time: a transient hiccup must never halt a drain that is
# already running and doing useful work. An ARM-time gate needs the opposite polarity: it must
# refuse unless liveness is PROVEN, because arming wrong dispatches every spoke into a
# dependency that cannot answer. Reusing it verbatim made this check pass on exactly the
# conditions it exists to catch (verified: a nonexistent probe binary reported alive/rc 0),
# which is the #268 failure class the arm-time self-check was added to close. The sibling
# probes (gh, judge) already fail closed; this one was the lone hole.
#
# So: rc 0 is POSITIVE PROOF the CLI answered -> alive. A nonzero exit carrying an auth
# signature -> auth-dead (the recoverable, operator-actionable state). Any other nonzero ->
# unresponsive, a FOURTH state the reap path has no need for and does not see.
#
# The budget is the other arm-time difference. The reap probe's 30s bounds a warm, repeated
# tick check; an arm-time round trip is COLD (CLI start + model round trip) -- the same cold
# start whose 2s bound caused #268. AFK_ARM_AUTH_TIMEOUT (120s) sits between that 30s and the
# answerer's 900s, and an operator who already raised AFK_AUTH_PROBE_TIMEOUT globally is
# honored rather than silently narrowed back down.
_afk_arm_claude_check() {
  local secs cmd raw rc
  secs="${AFK_ARM_AUTH_TIMEOUT:-${AFK_AUTH_PROBE_TIMEOUT:-120}}"
  case "$secs" in '' | *[!0-9]*) secs=120 ;; esac   # never let a typo lift the bound
  [ "$secs" -lt 1 ] && secs=120
  if _afk_network_is_down; then printf 'offline\n'; return 1; fi
  cmd="${AFK_AUTH_PROBE_CMD:-claude -p --no-session-persistence --model claude-haiku-4-5 ok}"
  raw="$(_afk_with_timeout "$secs" bash -c "$cmd" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then printf 'alive\n'; return 0; fi
  if is_auth_failure "$raw"; then printf 'auth-dead\n'; return 1; fi
  printf 'unresponsive\n'; return 1
}

# _afk_arm_gh_check -> rc 0 when a bounded REAL gh API round trip succeeds. afk_arm_preconditions
# already runs `gh auth status`, which proves only that a token EXISTS -- a host whose token is
# valid but whose API is unreachable (DNS, a proxy, an outage, a revoked scope) passes it and
# then fails every dispatch, land, and answer. `rate_limit` is the cheapest authenticated
# endpoint and mutates nothing, so the probe is free of side effects.
_afk_arm_gh_check() {
  _afk_with_timeout "$AFK_GH_TIMEOUT" gh api "${AFK_GH_PROBE_ENDPOINT:-rate_limit}" \
    >/dev/null 2>&1
}

# _afk_arm_testmon_check [repo_root] -> print ok | missing | unknown; rc 0 only for ok.
# Without pytest-testmon the pre-push gate cannot select affected tests, so EVERY first push
# per worktree degrades to the full multi-thousand-test suite -- slow-but-correct, which is why
# the caller warns rather than blocks. DETECTION ONLY: never install anything.
#
# The runner ladder deliberately MIRRORS shared/hooks/lib/utils.sh's detect_pytest instead of
# sourcing it. utils.sh is a HOOK lib with source-time side effects that are wrong in a
# supervisor: `set -euo pipefail`, an `ai_toolkit_enabled || exit 0` off-switch (#154), and
# telemetry_arm_hook_span's EXIT trap. Sourcing it made this probe report a FALSE "ok" -- the
# off-marker's `exit 0` is not catchable by a `|| exit 3` guard, so a probe that ran NOTHING
# read as a clean pass -- and a false "missing" when the lib's own source-time work failed. It
# is also unreachable in a synced target (sync-to-repo.sh co-locates only telemetry/base-branch/
# enabled next to hub-afk.sh; utils.sh ships to the platform hooks dir), so the probe would be
# permanently `unknown` on every deployed hub. Four lines of duplication beat all of that.
# UPGRADE: if detect_pytest ever grows a rung, mirror it here -- a drift only mis-WARNS.
#
# Each rung invokes its own argv rather than word-splitting a resolved string, so a repo path
# containing a space cannot shred the command. The runner is asked whether it advertises
# --testmon -- the exact check test-select.sh and gate-sweep.sh use to decide the degrade, so
# this probe can never disagree with the gate it is warning about. `--help` is captured then
# case-matched, never piped to `grep -q`, which would SIGPIPE the runner under pipefail and
# falsely report testmon absent (the trap test-select.sh documents).
#
# `unknown` (a runner that will not answer) is deliberately NOT `missing`: a probe with no
# evidence must not manufacture a warning. AFK_TESTMON_PROBE_CMD overrides the whole probe
# (rc 0 => ok) for tests.
_afk_arm_testmon_check() {
  local repo_root="${1:-.}" venv secs help=""
  secs="${AFK_TESTMON_PROBE_TIMEOUT:-60}"
  case "$secs" in '' | *[!0-9]*) secs=60 ;; esac
  if [ -n "${AFK_TESTMON_PROBE_CMD:-}" ]; then
    if _afk_with_timeout "$secs" bash -c "$AFK_TESTMON_PROBE_CMD" >/dev/null 2>&1; then
      printf 'ok\n'; return 0
    fi
    printf 'missing\n'; return 1
  fi
  venv="$repo_root/.venv/bin/pytest"
  if [ -x "$venv" ]; then
    help="$(_afk_with_timeout "$secs" "$venv" --help 2>/dev/null)"
  elif command -v pytest >/dev/null 2>&1; then
    help="$(_afk_with_timeout "$secs" pytest --help 2>/dev/null)"
  elif command -v python3 >/dev/null 2>&1 && python3 -c 'import pytest' >/dev/null 2>&1; then
    help="$(_afk_with_timeout "$secs" python3 -m pytest --help 2>/dev/null)"
  else
    printf 'missing\n'; return 1        # no pytest resolves at all -> certainly no testmon
  fi
  [ -n "$help" ] || { printf 'unknown\n'; return 1; }   # the runner would not answer
  case "$help" in *--testmon*) printf 'ok\n'; return 0 ;; esac
  printf 'missing\n'; return 1
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
  afk_arm_orca_guard
}

# afk_arm_orca_guard -> the last arm precondition (#363). Dispatch is Orca-only since S4, so arming
# needs a usable Orca (version floor + a read-only worktree listing). It then REFUSES regardless:
# INTERIM guard until S6 (#365) moves the supervisor onto Orca. Until then recover_dead_panes would
# read a pane-less Orca spoke as crashed and tear it down or relaunch it in tmux. S6 deletes it.
# AFK_ARM_PRECHECK=0 opts out like the rest of the gate.
afk_arm_orca_guard() {
  [ "${AFK_ARM_PRECHECK:-1}" = "0" ] && return 0
  if ! orca_require_version || ! orca_json worktree list; then
    log "/afk: refusing to arm — Orca is not usable (need >= ${ORCA_MIN_VERSION} and a reachable runtime, see above); dispatch is Orca-only since #363"
    return 1
  fi
  log "/afk: refusing to arm — dispatch now runs on Orca (#363) but the drain still supervises tmux panes, so recover_dead_panes would tear down or relaunch a pane-less Orca spoke; blocked until #365 (S6) moves the supervisor to Orca"
  return 1
}

# --- the ONE arm-time verdict (issue #279) ------------------------------------

# _afk_arm_telemetry_gate <repo_root> -> the #108 preflight with a self-check-shaped refusal
# line. Runs on BOTH the probed and the opted-out path: AI_TOOLKIT_OTEL=0 is the telemetry
# opt-out, AFK_ARM_SELFCHECK=0 is the liveness-probe opt-out, and neither may stand in for the
# other. Called LAST on the probed path because it is the only check with side effects (it
# launches the collector/bridge and exports auth) -- nothing gets started on a host that was
# going to be refused anyway.
_afk_arm_telemetry_gate() {
  afk_telemetry_preflight "$1" && return 0
  log "/afk: refusing to arm — the telemetry pipeline could not be wired (see above); the dashboard is the single source of truth for an unattended run (AI_TOOLKIT_OTEL=0 to drain without it)"
  return 1
}

# _afk_telemetry_desc -> the verdict line's telemetry clause: never claim "wired" when the
# operator opted out.
_afk_telemetry_desc() {
  if afk_telemetry_enabled; then
    printf 'telemetry wired (collector :4317, bridge :4319)\n'
  else
    printf 'telemetry off (AI_TOOLKIT_OTEL=0)\n'
  fi
}
# afk_arm_selfcheck <repo_root> -> rc 0 when the drain may arm (possibly DEGRADED), rc 1 to
# refuse. Chained in main() AFTER afk_arm_preconditions (so the instant static refusals — a
# dirty tree, an off-base HEAD, a live supervisor — never wait ~2 minutes on real round trips)
# and BEFORE afk_write_state, so a refusal writes no state, reaches no loop, and dispatches
# no spoke. AFK_ARM_SELFCHECK=0 opts the whole gate out, independently of AFK_ARM_PRECHECK.
#
# Per-check policy, and why each falls where it does:
#   judge     BLOCK — a dead judge grinds EVERY tier-3 permission to DENY for the whole
#                     window. This is #268 itself, the incident that motivated the issue.
#   claude    BLOCK — no answerer means every parked spoke escalates to blocked/<issue>.
#   gh api    BLOCK — dispatch, land, and answer all need the API, not merely a valid token.
#   testmon   WARN  — the ONLY degradation that is still CORRECT, just slow (every first push
#                     runs the full suite). Refusing the whole drain over it would be worse
#                     than the problem.
#   telemetry BLOCK — unchanged #108 posture (the dashboard is the SSOT for an unattended
#                     run); AI_TOOLKIT_OTEL=0 remains its opt-out.
#
# Order: gh first (bounded 30s, the cheapest real proof), then the two ~2-minute LLM round
# trips, then the free local testmon read, and telemetry LAST because it is the only check
# with side effects — it launches the collector/bridge containers and exports auth. Nothing
# gets started on a host that was going to be refused anyway.
afk_arm_selfcheck() {
  local repo_root="$1" judge_reason claude_state testmon_state telemetry_desc
  # The opt-out waives the LIVE PROBES only -- never the telemetry preflight below. Folding
  # telemetry inside this early return would have made AFK_ARM_SELFCHECK=0 a second, silent
  # opt-out for a gate that already has its own explicit one (AI_TOOLKIT_OTEL=0), so an
  # operator skipping the slow round trips would also have lost the #108 hard-fail without
  # asking to.
  if [ "${AFK_ARM_SELFCHECK:-1}" = "0" ]; then
    _afk_arm_telemetry_gate "$repo_root" || return 1
    log "/afk: arm self-check SKIPPED (AFK_ARM_SELFCHECK=0) — judge/claude/gh/testmon liveness NOT probed; $(_afk_telemetry_desc)"
    return 0
  fi

  if ! _afk_arm_gh_check; then
    log "/afk: refusing to arm — the GitHub API did not answer a bounded '${AFK_GH_PROBE_ENDPOINT:-rate_limit}' round trip (the token is present — 'gh auth status' passed — but the API is unreachable: check the network, a proxy, or an outage). Dispatch, land, and answer all need it (#279)"
    return 1
  fi

  judge_reason="$(_afk_arm_judge_check)" || {
    log "/afk: refusing to arm — the tier-3 permission judge is not usable: $judge_reason. An unusable judge fails EVERY uncached tier-3 verdict closed, so every spoke's permissions grind to DENY for the whole window (#268). Fix the judge, or raise AFK_JUDGE_TIMEOUT if it timed out (#279)"
    return 1
  }

  claude_state="$(_afk_arm_claude_check)"
  case "$claude_state" in
    alive) ;;
    offline)
      log "/afk: refusing to arm — this host cannot reach the network (${AFK_NET_PROBE_URL:-https://api.anthropic.com} did not answer). Nothing is wrong with your credentials; restore connectivity and re-arm (#249/#279)"
      return 1 ;;
    auth-dead)
      log "/afk: refusing to arm — the network is up but 'claude' reports an AUTH failure: the subscription token is dead and every spoke would stall on it. Run 'claude' → /login (see docs/afk-arm-selfcheck.md) and re-arm (#279)"
      return 1 ;;
    *)
      # Name the budget the probe ACTUALLY used -- the same ladder _afk_arm_claude_check
      # resolves -- or an operator who raised AFK_AUTH_PROBE_TIMEOUT is told to debug against
      # a number that was never applied.
      log "/afk: refusing to arm — 'claude' did not answer a bounded ${AFK_ARM_AUTH_TIMEOUT:-${AFK_AUTH_PROBE_TIMEOUT:-120}}s probe (no auth error, just no answer): the CLI may be absent from PATH, wedged, or slower than the budget. An arm-time gate must PROVE the answerer works before dispatching into it (#279)"
      return 1 ;;
  esac

  # WARN-only: name the consequence the operator will actually feel, not the missing package.
  testmon_state="$(_afk_arm_testmon_check "$repo_root")"
  case "$testmon_state" in
    ok) testmon_state="testmon present" ;;
    missing)
      log "/afk: DEGRADED — pytest-testmon is not importable by the resolved pytest runner, so EVERY first push per worktree runs the full multi-thousand-test suite instead of the affected set. Arming anyway (slow but correct); 'pip install -r requirements-dev.txt' to fix (#279)"
      testmon_state="testmon MISSING (every first push runs the full suite)" ;;
    *)
      log "/afk: DEGRADED — could not determine whether pytest-testmon is installed (no pytest runner answered). Arming anyway; if it is absent, every first push runs the full suite (#279)"
      testmon_state="testmon UNKNOWN" ;;
  esac

  # LAST: the only check that starts things. Its own refusal lines are already loud (#108).
  _afk_arm_telemetry_gate "$repo_root" || return 1
  telemetry_desc="$(_afk_telemetry_desc)"

  # ONE line, all five dependencies, honest about a degradation: an operator scanning an
  # unattended run's log gets a single arm-time verdict rather than five scattered notes.
  log "/afk: arm self-check OK — judge alive ($judge_reason), claude alive, gh api reachable, $testmon_state, $telemetry_desc"
  return 0
}

