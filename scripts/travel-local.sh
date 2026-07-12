#!/usr/bin/env bash
# travel-local.sh — keep an /afk drain running lid-closed on the iPhone hotspot (issue #248).
#
# For metro-scale trips (< ~1 hour): instead of migrating the hub+spokes to a second Mac,
# keep THIS Mac draining while it rides lid-closed in a bag, tethered to the iPhone's
# Personal Hotspot. The afk machinery already tolerates connectivity gaps (5-min supervisor
# tick, 30-min idle ceiling with revival, pre-reap auth probe); the only missing pieces are
# OS-level — stay awake on a closed lid, and switch to the phone's Wi-Fi.
#
#   travel-local.sh on       # join hotspot, verify reachability, disablesleep, caffeinate
#   travel-local.sh off      # restore power + Wi-Fi, refresh in-flight spoke clocks
#   travel-local.sh status   # disablesleep / caffeinate / SSID / connectivity / /afk status
#
# Configure via a sourced ~/.afk-travel (same pattern as ~/.afk-remote):
#
#   TRAVEL_HOTSPOT_SSID='Mathieu iPhone'   # required
#   TRAVEL_HOME_SSID='HomeNet'             # optional; `off` rejoins it explicitly
#
# `on` needs passwordless `pmset` so it runs non-interactively (disablesleep is the ONLY
# switch that survives lid-close on battery; caffeinate alone does not). Add ONE sudoers
# line (via `sudo visudo`), replacing <user> with your login name:
#
#   <user> ALL=(root) NOPASSWD: /usr/bin/pmset
#
# See docs/travel-local.md for the sudoers line, hotspot limits, battery/thermal notes, and
# when to prefer the remote-afk second-Mac path (docs/remote-afk.md) instead.
#
# Test seams (env overrides): AFK_TRAVEL_CONF, AFK_TRAVEL_WIFI_DEV, AFK_TRAVEL_PIDFILE,
# AFK_TRAVEL_API_URL, AFK_TRAVEL_JOIN_RETRIES, AFK_TRAVEL_JOIN_DELAY, AFK_TRAVEL_CAFFEINATE,
# AFK_TRAVEL_SETTLE, AFK_GATE_BROKER, AFK_HUB_AFK, AFK_STATE_DIR.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Anchor the hub root to the SCRIPT's checkout, not the invocation cwd — on/off/status
# must resolve the same repo (pidfile, afk state, worktrees) no matter where they run from.
HUB_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"

info() { printf '%s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf '%s\n' "$*" >&2; exit 1; }

# _conf_path -> the config file path (~/.afk-travel unless overridden).
_conf_path() { printf '%s\n' "${AFK_TRAVEL_CONF:-$HOME/.afk-travel}"; }

# --- config -------------------------------------------------------------------

# require_conf -> source ~/.afk-travel and guarantee TRAVEL_HOTSPOT_SSID; refuse (with the
# exact fix) when the file is missing or the SSID is unset. Used by `on`.
require_conf() {
  local conf; conf="$(_conf_path)"
  if [ ! -f "$conf" ]; then
    die "travel-local on: ~/.afk-travel missing — create it with TRAVEL_HOTSPOT_SSID='<your iPhone hotspot SSID>' (see docs/travel-local.md)"
  fi
  # shellcheck disable=SC1090
  . "$conf"
  if [ -z "${TRAVEL_HOTSPOT_SSID:-}" ]; then
    die "travel-local on: TRAVEL_HOTSPOT_SSID unset in ~/.afk-travel — set it to your iPhone hotspot SSID"
  fi
}

# load_conf_optional -> source ~/.afk-travel when present (for TRAVEL_HOME_SSID); `off` and
# `status` still work without it, so a missing file is not an error here.
load_conf_optional() {
  local conf; conf="$(_conf_path)"
  # shellcheck disable=SC1090
  [ -f "$conf" ] && . "$conf" 2>/dev/null || true
}

# --- Wi-Fi --------------------------------------------------------------------

# wifi_dev -> the Wi-Fi hardware port's BSD device (e.g. en0), or empty.
wifi_dev() {
  if [ -n "${AFK_TRAVEL_WIFI_DEV:-}" ]; then printf '%s\n' "$AFK_TRAVEL_WIFI_DEV"; return; fi
  # LC_ALL=C: parse the system tool's output by its English keyword regardless of host locale.
  LC_ALL=C networksetup -listallhardwareports 2>/dev/null | awk '/Wi-Fi/{getline; print $2; exit}'
}

# current_ssid <dev> -> the joined SSID, "not connected" when unassociated, or "unknown".
current_ssid() {
  local dev="$1" out
  [ -n "$dev" ] || { printf 'unknown\n'; return; }
  out="$(LC_ALL=C networksetup -getairportnetwork "$dev" 2>/dev/null)"
  # macOS prints "Current Wi-Fi Network: <ssid>" when joined, else a sentence like
  # "You are not associated with an AirPort network." — don't pass that through as an SSID.
  case "$out" in
    "Current Wi-Fi Network: "*) printf '%s\n' "${out#Current Wi-Fi Network: }" ;;
    *)                          printf 'not connected\n' ;;
  esac
}

# join_hotspot <dev> <ssid> -> retry the join (the hotspot only broadcasts while Personal
# Hotspot is enabled on the phone). rc 0 on join, rc 1 after exhausting the retries.
join_hotspot() {
  local dev="$1" ssid="$2" i
  local retries="${AFK_TRAVEL_JOIN_RETRIES:-8}" delay="${AFK_TRAVEL_JOIN_DELAY:-3}"
  for (( i = 1; i <= retries; i++ )); do
    if networksetup -setairportnetwork "$dev" "$ssid"; then return 0; fi
    if [ "$i" -lt "$retries" ]; then sleep "$delay"; fi
  done
  return 1
}

# restore_wifi -> rejoin TRAVEL_HOME_SSID when set, else power-cycle Wi-Fi so macOS
# auto-joins its preferred network. Best-effort (never aborts `off`).
restore_wifi() {
  local dev; dev="$(wifi_dev)"
  [ -n "$dev" ] || return 0
  if [ -n "${TRAVEL_HOME_SSID:-}" ]; then
    networksetup -setairportnetwork "$dev" "$TRAVEL_HOME_SSID" || true
  else
    networksetup -setairportpower "$dev" off || true
    networksetup -setairportpower "$dev" on || true
  fi
}

# verify_connectivity -> a bounded HEAD to the Anthropic API through the live interface.
verify_connectivity() {
  local url="${AFK_TRAVEL_API_URL:-https://api.anthropic.com}"
  curl -sS -I --max-time "${AFK_TRAVEL_CURL_TIMEOUT:-10}" "$url" >/dev/null 2>&1
}

# --- power (disablesleep + caffeinate) ----------------------------------------

set_disablesleep() { sudo pmset -a disablesleep "$1"; }

read_disablesleep() {
  LC_ALL=C pmset -g 2>/dev/null | awk '/SleepDisabled/{print $NF; f=1} END{if (!f) print "unknown"}'
}

caffeinate_pidfile() {
  if [ -n "${AFK_TRAVEL_PIDFILE:-}" ]; then printf '%s\n' "$AFK_TRAVEL_PIDFILE"; return; fi
  # An ABSOLUTE common dir so on/off/status agree from any cwd — `--git-common-dir` is
  # cwd-relative (`.git`) in a main checkout, which would resolve a different pidfile per cwd.
  local common; common="$(git -C "${HUB_ROOT:-$SCRIPT_DIR}" rev-parse --absolute-git-dir 2>/dev/null)" \
    || common="${HUB_ROOT:-$SCRIPT_DIR}/.git"
  printf '%s\n' "$common/.afk-travel-caffeinate.pid"
}

# caffeinate_live -> rc 0 when the pidfile names a live process.
caffeinate_live() {
  local pf pid; pf="$(caffeinate_pidfile)"
  [ -f "$pf" ] || return 1
  pid="$(cat "$pf" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# start_caffeinate -> launch a detached `caffeinate -s`, record its pid, and confirm it
# actually stayed up. Idempotent: a live caffeinate is reused. rc 1 (pidfile cleared) when
# the binary is unavailable OR the launch died immediately — so the caller rolls back
# disablesleep rather than leave the Mac half-configured and falsely report "holding".
# AFK_TRAVEL_CAFFEINATE overrides the binary name (tests inject an absent name to exercise
# the rollback); AFK_TRAVEL_SETTLE tunes the post-launch settle before the liveness re-check.
start_caffeinate() {
  local pf pid bin; pf="$(caffeinate_pidfile)"; bin="${AFK_TRAVEL_CAFFEINATE:-caffeinate}"
  if caffeinate_live; then return 0; fi
  command -v "$bin" >/dev/null 2>&1 || return 1
  # Detach stdio so the daemon never holds the launcher's pipes open (a caller that
  # captures our output would otherwise block until caffeinate exits).
  "$bin" -s </dev/null >/dev/null 2>&1 &
  pid=$!
  printf '%s\n' "$pid" > "$pf"
  sleep "${AFK_TRAVEL_SETTLE:-0.3}"
  if kill -0 "$pid" 2>/dev/null; then return 0; fi
  rm -f "$pf"
  return 1
}

# stop_caffeinate -> kill the recorded caffeinate and drop the pidfile. Idempotent.
stop_caffeinate() {
  local pf pid; pf="$(caffeinate_pidfile)"
  [ -f "$pf" ] || return 0
  pid="$(cat "$pf" 2>/dev/null)"
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  rm -f "$pf"
}

# --- /afk surface + spoke clocks ----------------------------------------------

# resolve_script <name> -> first existing candidate across the checkout / synced layouts.
resolve_script() {
  local name="$1" override="$2" c
  for c in "$override" \
           "$SCRIPT_DIR/$name" \
           "$SCRIPT_DIR/../shared/skills/hub/scripts/$name" \
           "${TOPLEVEL:+$TOPLEVEL/shared/skills/hub/scripts/$name}"; do
    [ -n "$c" ] && [ -f "$c" ] && { printf '%s\n' "$c"; return; }
  done
}

print_afk_status() {
  local hubafk; hubafk="$(resolve_script hub-afk.sh "${AFK_HUB_AFK:-}")"
  [ -n "$hubafk" ] || { warn "hub-afk.sh not found — skipping /afk status"; return 0; }
  bash "$hubafk" --status 2>&1 || true
}

# refresh_spoke_epochs -> stamp BOTH the progress and answer-attempt epoch for every in-flight
# spoke, mirroring hub-afk's resume_spoke: a blackout freezes the spoke's transcript mtime, so
# progress-epoch alone (the reap-ceiling reference) is not enough — the answer-attempt epoch is
# the idle clock's exclusion, and without it the reaper kills a merely-offline spoke on the
# first post-blackout tick. Sourced in a subshell so gate-broker's helpers never leak here.
refresh_spoke_epochs() {
  local broker; broker="$(resolve_script gate-broker.sh "${AFK_GATE_BROKER:-}")"
  if [ -z "$broker" ]; then
    warn "gate-broker.sh not found — skipping spoke clock refresh"
    return 0
  fi
  (
    # Anchor cwd to the hub checkout so the broker's `git rev-parse --git-common-dir` and
    # inflight_worktrees resolve the hub's .git / worktrees, not the invocation cwd's.
    [ -n "${HUB_ROOT:-}" ] && cd "$HUB_ROOT" 2>/dev/null || true
    # shellcheck disable=SC1090
    . "$broker" 2>/dev/null || exit 0
    while read -r issue; do
      [ -n "$issue" ] || continue
      stamp_progress_epoch "$issue"
      stamp_answer_attempt "$issue"
    done < <(inflight_issues)
  )
}

# --- verbs --------------------------------------------------------------------

cmd_on() {
  require_conf
  # `|| true`: wifi_dev ends in a pipeline, so under pipefail a networksetup failure would
  # abort the assignment before the explicit empty-check below could give its clear message.
  local dev; dev="$(wifi_dev)" || true
  [ -n "$dev" ] || die "travel-local on: could not resolve the Wi-Fi device"

  info "joining hotspot '$TRAVEL_HOTSPOT_SSID'..."
  if ! join_hotspot "$dev" "$TRAVEL_HOTSPOT_SSID"; then
    die "travel-local on: hotspot '$TRAVEL_HOTSPOT_SSID' never appeared — enable Personal Hotspot on the iPhone and retry"
  fi
  info "verifying connectivity..."
  if ! verify_connectivity; then
    die "travel-local on: joined '$TRAVEL_HOTSPOT_SSID' but ${AFK_TRAVEL_API_URL:-https://api.anthropic.com} is unreachable"
  fi

  set_disablesleep 1
  if ! start_caffeinate; then
    warn "caffeinate failed to launch — rolling back disablesleep"
    set_disablesleep 0
    die "travel-local on: caffeinate did not start; rolled back disablesleep"
  fi

  echo "OK lid-close safe: disablesleep on, caffeinate holding, joined '$TRAVEL_HOTSPOT_SSID'"
  print_afk_status
}

cmd_off() {
  load_conf_optional
  # Every step is best-effort: the spoke-clock refresh is the whole point of `off`, so a
  # failing pmset/kill/Wi-Fi step must never `set -e`-abort the run before it stamps them.
  set_disablesleep 0 || warn "could not clear disablesleep (is passwordless pmset configured?)"
  stop_caffeinate
  restore_wifi
  refresh_spoke_epochs
  echo "OK travel-local off: disablesleep cleared, caffeinate released, Wi-Fi restored, spoke clocks refreshed"
}

cmd_status() {
  load_conf_optional
  local dev; dev="$(wifi_dev)" || true
  echo "disablesleep: $(read_disablesleep)"
  if caffeinate_live; then
    echo "caffeinate: running (pid $(cat "$(caffeinate_pidfile)"))"
  else
    echo "caffeinate: not running"
  fi
  echo "Wi-Fi SSID: $(current_ssid "$dev")"
  if verify_connectivity; then
    echo "connectivity: reachable (${AFK_TRAVEL_API_URL:-https://api.anthropic.com})"
  else
    echo "connectivity: UNREACHABLE"
  fi
  echo "--- /afk status ---"
  print_afk_status
}

main() {
  if [ "$(uname 2>/dev/null || true)" != "Darwin" ]; then
    die "travel-local: macOS only (this host is $(uname 2>/dev/null || echo unknown))"
  fi
  case "${1:-}" in
    on)     cmd_on ;;
    off)    cmd_off ;;
    status) cmd_status ;;
    *)      printf 'usage: travel-local.sh {on|off|status}\n' >&2; exit 2 ;;
  esac
}

main "$@"
