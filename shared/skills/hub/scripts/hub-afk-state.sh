#!/usr/bin/env bash
# hub-afk-state.sh -- split out of hub-afk.sh (issue #351).
#
# The persisted-STATE lane of the /afk supervisor: the pure window/time layer
# (parse_duration / compute_end_epoch / window_expired / minutes_remaining) plus every
# on-disk supervisor record built on top of it -- the window state file, the #252
# arm-generation token + synchronous off, the #150 landed tally + drain-complete
# hand-off, the #107 heartbeat, and the #202 B last-action label -- and the liveness
# cross-checks derived from them (_afk_pid_alive, afk_supervisor_state,
# _afk_heartbeat_age_minutes). A pure function-definition module sourced by the entry lib
# hub-afk.sh AFTER worktree-lib / gate-broker / log / afk_now and the entry's own
# bounded-external-call primitives, and BEFORE any function is called, so every
# cross-module helper resolves at call time. Not run on its own.
set -uo pipefail

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
# armed its USR1 trap (_AFK_WAKE_TOKEN set by the entry lib), so afk-notify-wake never SIGUSR1s — and thus
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
