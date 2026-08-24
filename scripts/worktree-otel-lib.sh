#!/usr/bin/env bash
#
# worktree-otel-lib.sh — native-OTel preflight machinery for the worktree scripts.
# Source this file; do not execute it. It is sourced by scripts/worktree-lib.sh
# (the thin entry), never directly by consumers — so every helper here can call the
# core helpers the entry defines (wt_warn, wt_pgrep, wt_ps_start_epoch,
# wt_sha256_stdin) at call time.
#
# Extracted from worktree-lib.sh in issue #353 (the spoke-side twin of the
# gate-broker #275 / hub-afk #307 / hub-watchdog #308 splits): a control-plane file
# every change must touch serializes the drain on its Scope: token (AFK Design
# Principle 7). This module owns the AI_TOOLKIT_OTEL=1 message-bridge (:4319) and
# otelcol collector (:4317) spawn-time preflights plus the #138 watchdog auto-arm.

# --- native-OTel message-bridge preflight (auto-populate) --------------------
# A spoke that opted into native OTel (AI_TOOLKIT_OTEL=1) needs the Langfuse
# message bridge (scripts/telemetry/langfuse_message_bridge.py, port :4319) up, or
# the audit events (#93) and LLM request/response I/O the otelcol forks to it never
# reach Langfuse. These helpers bring it up idempotently at spawn so the operator
# runs no manual step; they are best-effort and never fail the spawn.

# True when something is LISTENing on the given localhost TCP port. Split out so
# the preflight decision is unit-testable by overriding it (no live socket). Uses
# lsof when present, else nc; when neither exists, reports "down" so the caller
# attempts a start (a duplicate would simply fail to bind — never two servers).
wt_port_listening() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v nc >/dev/null 2>&1; then
    nc -z localhost "$port" >/dev/null 2>&1
  else
    return 1
  fi
}

# Start the message bridge in the background, detached, logging to a temp file.
# Split from the preflight so the decision logic can be tested without spawning a
# real server. PYTHONPATH=scripts lets the script import its sibling telemetry
# package; LANGFUSE_HOST defaults to the local Langfuse. The child reads its
# REQUIRED config from the environment — LANGFUSE_BASIC_AUTH (KeyErrors without it)
# and BRIDGE_PORT — so re-export them inside a subshell: a non-exported operator
# value still reaches the child (it passed the preflight as a shell-internal read),
# and the credential never lands on the python argv. Args: $1 = repo root.
wt_bridge_launch() {
  local repo_root="$1" log
  log="$(mktemp -t lf-bridge.XXXXXX 2>/dev/null)" || log="/tmp/lf-bridge.log"
  (
    export PYTHONPATH="$repo_root/scripts"
    # env -> config default (LANGFUSE_HOST_DEFAULT, set by wt_resolve_telemetry_config
    # in the launching shell) -> hardcoded local Langfuse (issue #228). So the live
    # bridge forwards to the SAME Langfuse the config names, not a hardcoded localhost.
    export LANGFUSE_HOST="${LANGFUSE_HOST:-${LANGFUSE_HOST_DEFAULT:-http://localhost:3000}}"
    export LANGFUSE_BASIC_AUTH BRIDGE_PORT
    nohup python3 "$repo_root/scripts/telemetry/langfuse_message_bridge.py" \
      >"$log" 2>&1 &
  )
  echo "→ started Langfuse message bridge on :${BRIDGE_PORT:-4319} (log: $log)"
}

# PID LISTENing on the bridge port (default :4319), via `lsof -t` — NOT `pgrep -f`,
# which false-negatives on non-ASCII argv under a non-UTF8 locale and would report a
# live bridge as down. '' when nothing listens or lsof is unavailable. Split out so
# the staleness decision is overridable in tests with no real socket probe.
wt_bridge_pid() {
  local port="${1:-4319}"
  command -v lsof >/dev/null 2>&1 || return 0
  lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n1
}

# Newest mtime (epoch seconds) among the bridge's source bundle: the bridge itself
# plus its only telemetry sibling import, langfuse_audit_events. Reading from the
# MAIN checkout (the preflight's repo_root) makes mtime a reliable change signal —
# a land rewrites the touched files, an untouched land leaves them old — so it does
# not over-fire the way a per-worktree checkout's fresh mtimes would. Portable stat,
# GNU-first (GNU -c %Y, then BSD -f %m): the order is load-bearing, because on GNU
# stat `-f` means "filesystem status" — `stat -f %m FILE` prints a multi-line fs
# block for FILE (taking %m as a missing operand) and exits nonzero, so a BSD-first
# fallback APPENDS the real epoch to that captured garbage and the helper silently
# yielded 0 on Linux (#132). GNU-first fails cleanly on BSD (usage error, empty
# stdout). 0 when none found. Overridable in tests.
# UPGRADE: extend this list if the bridge grows new telemetry.* imports.
wt_bridge_source_mtime() {
  local repo_root="$1" newest=0 f m
  for f in "$repo_root/scripts/telemetry/langfuse_message_bridge.py" \
           "$repo_root/scripts/telemetry/langfuse_audit_events.py"; do
    [ -f "$f" ] || continue
    m="$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null)" || continue
    [ "$m" -gt "$newest" ] && newest="$m"
  done
  printf '%s' "$newest"
}

# Stop the running bridge (best-effort). Split out so the staleness decision is
# overridable in tests with no real signal sent. Args: $1 = pid.
wt_bridge_kill() { kill "$1" 2>/dev/null || true; }

# Recycle the running bridge IFF its source bundle was modified after the process
# started (a land rewrote the bridge code). Best-effort and idempotent: no pid, no
# resolvable start time, or source not strictly newer leaves the process untouched,
# so the restart fires only on a PROVEN change and never loops. A stale process with
# LANGFUSE_BASIC_AUTH unset is also left running (warn instead of killing a working
# bridge for an un-authable one). The `-gt` is strict at second granularity, so a
# land landing in the very second the bridge (re)started is not seen as stale —
# erring toward no over-fire, an acceptable miss given lands are minutes apart.
# Args: $1 = repo root, $2 = bridge port.
wt_bridge_restart_if_stale() {
  local repo_root="$1" port="$2" pid start src
  pid="$(wt_bridge_pid "$port")"
  [ -n "$pid" ] || return 0
  # `|| true`: wt_ps_start_epoch now returns non-zero on a dead/unparseable pid, and
  # this runs under worktree-new.sh's `set -e`, where a failing command substitution
  # in an assignment aborts before the guard below. Swallow it so the preflight stays
  # best-effort (never fails the spawn); the empty-start guard covers both failures.
  start="$(wt_ps_start_epoch "$pid")" || true
  [ -n "$start" ] || return 0
  src="$(wt_bridge_source_mtime "$repo_root")"
  [ "$src" -gt "$start" ] 2>/dev/null || return 0
  if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
    wt_warn "Langfuse bridge source changed but LANGFUSE_BASIC_AUTH unset — leaving the running (stale) bridge; restart manually after exporting auth"
    return 0
  fi
  wt_bridge_kill "$pid"
  wt_bridge_launch "$repo_root"
}

# Idempotently ensure the message bridge is up AND current for an opted-in spoke. A
# no-op unless AI_TOOLKIT_OTEL=1. When :4319 already listens, delegate to
# wt_bridge_restart_if_stale — which recycles the process only when its source
# bundle proves it is running stale code, and otherwise leaves it untouched (no
# second bridge, no needless churn). When down: never starts a second bridge. When
# LANGFUSE_BASIC_AUTH is unset the bridge can't authenticate to Langfuse, so warn
# (audit events + LLM I/O won't land) but DO NOT fail the spawn. Args: $1 = repo root.
wt_otel_bridge_preflight() {
  local repo_root="$1" port="${BRIDGE_PORT:-4319}"
  [ "${AI_TOOLKIT_OTEL:-}" = "1" ] || return 0
  if wt_port_listening "$port"; then
    wt_bridge_restart_if_stale "$repo_root" "$port"
    return 0
  fi
  if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
    wt_warn "OTel bridge down on :$port and LANGFUSE_BASIC_AUTH unset — audit events (#93) + LLM I/O won't reach Langfuse; spoke still launches"
    return 0
  fi
  wt_bridge_launch "$repo_root"
}

# --- native-OTel collector preflight (auto-ensure) ---------------------------
# A spoke that opted into native OTel (AI_TOOLKIT_OTEL=1) exports its
# traces/logs/metrics to the otelcol collector on :4317; the collector in turn
# forks the LLM I/O + audit events to the message bridge (:4319). If the
# collector is down (fresh boot, crashed container, never started) every spoke
# exports into a dead port and nothing reaches Langfuse. This helper brings the
# collector up idempotently at spawn — mirroring wt_otel_bridge_preflight — so
# the operator runs no manual step. Best-effort: it never fails the spawn.

# The collector image and published-port set, as single sources of truth shared
# by the launch and by the staleness signature below — so a future bump to either
# flows into the config-version label and a container stamped with the old value
# is detected stale. The port flags are intentionally word-split at the `docker
# run` call site.
WT_COLLECTOR_IMAGE="otel/opentelemetry-collector-contrib:latest"
WT_COLLECTOR_PORT_FLAGS="-p 4317:4317 -p 4318:4318 -p 4418:4418 -p 8889:8889"

# Combined staleness signature for the collector: a hash over the otelcol.yaml
# CONTENT plus the expected port set + image. A content hash (not the file's
# mtime) is the right signal because the config is bind-mounted — the running
# container's mounted file already equals on-disk, and a per-worktree checkout
# rewrites mtimes without changing content. Any real change to config, ports, or
# image bumps the signature. Empty when the config is missing (caller then leaves
# the running instance untouched). Split out so it is overridable in tests.
# Args: $1 = repo root (holds dashboard/langfuse/otelcol.yaml).
wt_collector_config_version() {
  local cfg="$1/dashboard/langfuse/otelcol.yaml"
  [ -f "$cfg" ] || return 0
  { cat "$cfg"; printf '%s\n%s\n' "$WT_COLLECTOR_PORT_FLAGS" "$WT_COLLECTOR_IMAGE"; } \
    | wt_sha256_stdin
}

# Start the otelcol collector (lf-collector) in a detached Docker container.
# Split from the preflight so the decision logic stays unit-testable (override
# wt_port_listening, no real `docker run`). The non-secret connection endpoints
# default to the local stack when the operator left them unset; the `docker -e
# VAR` (valueless) form forwards them — and LANGFUSE_BASIC_AUTH — from this
# shell, so re-export them in a subshell. LANGFUSE_BASIC_AUTH is forwarded
# VERBATIM: wrapping it in extra quotes makes the collector's Authorization
# header 401 while metrics still flow (looks like a pipeline bug but is auth).
# Stamps the config-version label so a later spawn can detect a stale container.
# Args: $1 = repo root (holds dashboard/langfuse/otelcol.yaml).
wt_collector_launch() {
  local repo_root="$1" version
  version="$(wt_collector_config_version "$repo_root")"
  (
    export LANGFUSE_OTLP_ENDPOINT="${LANGFUSE_OTLP_ENDPOINT:-http://host.docker.internal:3000/api/public/otel}"
    export BRIDGE_OTLP_ENDPOINT="${BRIDGE_OTLP_ENDPOINT:-http://host.docker.internal:4319}"
    export LANGFUSE_BASIC_AUTH
    # shellcheck disable=SC2086  # WT_COLLECTOR_PORT_FLAGS is meant to word-split.
    docker run -d --name lf-collector --add-host=host.docker.internal:host-gateway \
      --label "ai-toolkit.config-version=$version" \
      $WT_COLLECTOR_PORT_FLAGS \
      -e LANGFUSE_OTLP_ENDPOINT -e LANGFUSE_BASIC_AUTH -e BRIDGE_OTLP_ENDPOINT \
      -v "$repo_root/dashboard/langfuse/otelcol.yaml:/etc/otelcol-contrib/config.yaml" \
      "$WT_COLLECTOR_IMAGE" >/dev/null 2>&1
  )
  echo "→ started lf-collector (otelcol) on :4317/:4318/:4418/:8889"
}

# The config-version label of the running lf-collector, or '' on any failure (no
# such container, unlabeled pre-feature container, docker unreachable). Split out
# so the staleness decision is overridable in tests with no real `docker inspect`.
wt_collector_running_version() {
  docker inspect -f '{{ index .Config.Labels "ai-toolkit.config-version" }}' \
    lf-collector 2>/dev/null || true
}

# Tear down the running collector (best-effort). Split out so the staleness
# decision is overridable in tests with no real `docker rm`.
wt_collector_remove() {
  docker rm -f lf-collector >/dev/null 2>&1 || true
}

# The lifecycle status of the lf-collector container — e.g. running/exited/created/
# dead (also restarting/paused/removing) — or '' when the container is absent or
# docker is unreachable. The list is illustrative, not exhaustive: the caller
# treats ANY non-running state as recoverable. Split out so the recover-when-dead
# decision is overridable in tests with no real `docker inspect`.
wt_collector_container_status() {
  docker inspect -f '{{ .State.Status }}' lf-collector 2>/dev/null || true
}

# Recover a stopped lf-collector so a subsequent launch's --name can't clash. The
# spawn preflight starts the collector only when :4317 is DOWN, but an
# Exited/Created/Dead container still owns the `lf-collector` name — a bare `docker
# run --name lf-collector` then fails the name check (swallowed best-effort) and
# never recovers: start-if-absent, not restart-if-dead (#115). So when a
# non-running container exists, tear it down here; the caller relaunches a fresh
# one. Absent (or docker unreachable → '') means nothing to recover. A container
# reporting `running` is left untouched ON PURPOSE — never tear down a possibly
# healthy or still-starting collector. The running guard is NOT dead code: the
# down path can be entered with a running container (a startup race before :4317
# binds, a bind to the wrong interface), and in that corner recovery is
# deliberately skipped. Split out so the decision is unit-testable with docker
# overridden.
# UPGRADE: a running-but-wedged collector (up, not serving :4317) is not
# auto-healed — out of #115's Exited/Created/Dead scope; add a liveness probe if it
# recurs.
wt_collector_recover_dead() {
  local status
  status="$(wt_collector_container_status)"
  [ -n "$status" ] || return 0
  [ "$status" = "running" ] && return 0
  wt_collector_remove
}

# Recycle the running collector IFF its stamped config-version differs from the
# current one (an otelcol.yaml / port / image change landed). Best-effort and
# idempotent: an unhashable config or a missing/unreadable label leaves the
# instance untouched, so the restart fires only on a PROVEN change and never
# loops. A stale instance with LANGFUSE_BASIC_AUTH unset is also left running
# (warn instead of tearing a working instance down for an un-authable one).
# Args: $1 = repo root.
wt_collector_restart_if_stale() {
  local repo_root="$1" cur run
  cur="$(wt_collector_config_version "$repo_root")"
  [ -n "$cur" ] || return 0
  run="$(wt_collector_running_version)"
  [ -n "$run" ] || return 0
  [ "$run" != "$cur" ] || return 0
  if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
    wt_warn "OTel collector config changed but LANGFUSE_BASIC_AUTH unset — leaving the running (stale) lf-collector; restart manually after exporting auth"
    return 0
  fi
  wt_collector_remove
  wt_collector_launch "$repo_root"
}

# Idempotently ensure the otelcol collector is up AND current for an opted-in
# spoke. A no-op unless AI_TOOLKIT_OTEL=1 (AI_TOOLKIT_OTEL=0 is a clean full
# opt-out). When :4317 already listens, delegate to wt_collector_restart_if_stale
# — which recycles the container only when its stamped config-version proves it is
# running stale code/config, and otherwise leaves it untouched (no second
# collector, no needless churn). When down: first recover a stopped lf-collector
# (wt_collector_recover_dead removes an Exited/Created/Dead container that would
# otherwise fail a fresh launch's --name check, swallowed best-effort, #115), then
# start exactly one collector. When LANGFUSE_BASIC_AUTH is unset the collector
# can't authenticate to Langfuse, so warn (telemetry won't land) and leave any
# stopped container in place — recovering without an authed relaunch only strands
# the port — but DO NOT fail the spawn (same posture as wt_otel_bridge_preflight).
# Run BEFORE the bridge preflight: the collector forks to the bridge, so both must
# be up before the spoke's first export. Args: $1 = repo root.
wt_otel_collector_preflight() {
  local repo_root="$1" port=4317
  [ "${AI_TOOLKIT_OTEL:-}" = "1" ] || return 0
  if wt_port_listening "$port"; then
    wt_collector_restart_if_stale "$repo_root"
    return 0
  fi
  if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
    wt_warn "OTel collector down on :$port and LANGFUSE_BASIC_AUTH unset — telemetry won't reach Langfuse; spoke still launches"
    return 0
  fi
  wt_collector_recover_dead
  wt_collector_launch "$repo_root"
}

# --- watchdog auto-arm (#138) -------------------------------------------------
# The preflights above cover the spawn INSTANT; nothing re-ensures a collector
# that dies mid-run (a machine sleep left lf-collector Exited for ~24 min while
# a live spoke dropped every span at source). Arm the hub-side watchdog at every
# spawn: hub-otel-watch.sh --daemon re-runs the same ensure paths each tick
# while ≥1 spoke pane is live and exits itself when the last pane closes, so
# capture self-heals across sleep/wake with no human in the loop.
#
# Best-effort and idempotent, same posture as the preflights: a no-op unless
# AI_TOOLKIT_OTEL=1, a no-op while a live daemon already holds the pidfile (the
# daemon's own singleton guard stays authoritative — this pre-check only avoids
# forking a doomed child per spawn), a warning (never a spawn failure) when the
# watch script is unresolvable. The subshell exports what the detached child
# needs: the OTel opt-in and Langfuse auth are plain (unexported) assignments in
# worktree-new.sh, and MAIN_ROOT pins the daemon's ensure target to this hub.
# Args: $1 = repo root.
wt_otel_watch_arm() {
  local repo_root="$1" bin="" pidfile pid cand common
  [ "${AI_TOOLKIT_OTEL:-}" = "1" ] || return 0
  for cand in \
    "${HUB_OTEL_WATCH_BIN:-}" \
    "$repo_root/shared/skills/hub/scripts/hub-otel-watch.sh" \
    "$repo_root/.ai-toolkit/scripts/hub-otel-watch.sh" \
    "$repo_root/.claude/skills/hub/scripts/hub-otel-watch.sh"; do
    if [ -n "$cand" ] && [ -f "$cand" ]; then bin="$cand"; break; fi
  done
  if [ -z "$bin" ]; then
    wt_warn "hub-otel-watch.sh not found under $repo_root — watchdog not armed; a mid-run collector death won't self-heal (run hub-otel-watch.sh --daemon manually)"
    return 0
  fi
  pidfile="${HUB_OTEL_WATCH_PIDFILE:-}"
  if [ -z "$pidfile" ]; then
    common="$(git -C "$repo_root" rev-parse --git-common-dir 2>/dev/null)" || common=""
    case "$common" in
      "" | /*) ;;
      *) common="$repo_root/$common" ;;
    esac
    [ -n "$common" ] && pidfile="$common/hub-otel-watch.pid"
  fi
  if [ -n "$pidfile" ] && [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then return 0; fi
  fi
  (
    export AI_TOOLKIT_OTEL LANGFUSE_BASIC_AUTH
    export MAIN_ROOT="$repo_root"
    export HUB_OTEL_WATCH_PIDFILE HUB_OTEL_WATCH_LOG HUB_OTEL_WATCH_INTERVAL HUB_OTEL_WATCH_IDLE_TICKS
    nohup bash "$bin" --daemon >/dev/null 2>&1 &
  )
  echo "→ armed hub-otel-watch daemon (collector/bridge self-heal across sleep/wake)"
}
