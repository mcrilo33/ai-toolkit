#!/usr/bin/env bash
# gate-broker.sh — the shared gate-broker core (issue #155).
#
# ONE machine services a parked spoke gate for BOTH the unattended /afk supervisor and the
# attended reviewer. hub-afk.sh sources this file and drives it as the *unattended* adapter
# (decide_and_act -> broker_service_gate ... unattended); the attended QCM adapter (subtask
# C) drives the same core. The six shared stages:
#
#   1. DETECT   a parked gate: gate/<N> at the tip (_gate_parked) or a pending question /
#               permission dialog (slot_state == waiting, _permission_pending).
#   2. EXTRACT  the plan/prompt the spoke parked on (extract_pending_question), or the
#               command a permission dialog is gating (extract_pending_command).
#   3. REASON   one fresh ephemeral context per gate (run_answerer -> parse_decision),
#               governed by the afk-answering rule. (Subtask B adds read-only worktree
#               evidence + the decisions-digest seed.)
#   4. CLASSIFY obvious, safe scoped self-ops decided by a fixed rules table
#               (classify_permission); a genuine judgment call routes out to the adapter.
#   5. INJECT   the ONE hardened injector (inject_and_verify): Esc-first menu cancel,
#               send-keys -l, a SEPARATE Enter, a bare-Enter retry that never re-pastes,
#               wedge -> pane respawn. Shared by both modes so the paste bugs are fixed once.
#   6. LOG      an auto-answer decision span (afk_emit_decision). (Subtask D adds the
#               automatable-decisions log + codification pass.)
#
# broker_service_gate <wt> <issue> [mode] is the orchestrator; the ONLY mode-divergent seam
# is _broker_on_human_decision (unattended -> escalate blocked/<N>; attended -> QCM).
#
# Sourceable on its own (the tests do): it pulls worktree-lib.sh and defines every helper it
# needs. respawn_wedged_spoke (a supervisor-lifecycle recovery) is the one outward call,
# reached by a runtime existence-check so a standalone/attended broker degrades to escalate.
set -uo pipefail

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# Our OWN location, ALWAYS from THIS file's BASH_SOURCE -- never the inherited SCRIPT_DIR,
# which the /afk self-copy supervisor sets to a temp dir holding only hub-afk.sh (#262). The
# sibling-source blocks below resolve from here FIRST so a co-located hub-inject.sh (and the
# checkout's ../../../../scripts/ tree) is found regardless of who sourced us or what
# SCRIPT_DIR they passed down.
_GB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Defaults the moved reasoner/detector read directly (set -u safe when sourced standalone;
# idempotent when hub-afk.sh already set them).
: "${AFK_SPOKE_MAX_MINUTES:=180}"
: "${AFK_IDLE_MINUTES:=30}"
: "${AFK_ANSWERER_EFFORT:=high}"
# Warned-retry backoff (issue #241): a converted stop site parks a spoke LAST rather than
# abandoning it — warn, then re-service on an exponential backoff so a persistently-failing
# spoke is retried rarely (doom-loop safety by the curve, not by abandonment; #144/#140/#202).
: "${AFK_WARN_BACKOFF_BASE:=60}"
: "${AFK_WARN_BACKOFF_CAP:=1800}"

# --- source worktree-lib.sh (the shared date/time + worktree helpers) ---------
# Resolution covers both layouts: the ai-toolkit checkout (scripts/worktree-lib.sh, four
# levels up) and a synced target (.ai-toolkit/scripts/). AFK_WT_LIB wins for tests.
_AFK_TOPLEVEL="${_AFK_TOPLEVEL:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
for _cand in \
  "${AFK_WT_LIB:-}" \
  "$_GB_DIR/worktree-lib.sh" \
  "$_GB_DIR/../../../../scripts/worktree-lib.sh" \
  "$SCRIPT_DIR/worktree-lib.sh" \
  "$SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/scripts/worktree-lib.sh}" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/worktree-lib.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

log() { printf '%s\n' "$*" >&2; }

# --- source hub-inject.sh (the ONE hardened tmux-inject + delivery-proof unit) -
# The spoke-pane injection + transcript-delivery primitives (issue #251) live in
# hub-inject.sh so the /afk answerer (us) and the tier-2 hub-watchdog share one tested
# helper. Always a co-located sibling — in the checkout AND a synced .ai-toolkit/scripts/
# target — so it resolves from $_GB_DIR (OUR own dir) regardless of the inherited SCRIPT_DIR.
# The _AFK_TOPLEVEL fallbacks mirror the worktree-lib block; without them a self-copy
# supervisor (SCRIPT_DIR = a temp dir with only hub-afk.sh) left every moved helper undefined
# and the drain serviced nothing (#262). AFK_HUB_INJECT wins for tests. Sourced AFTER
# log()/worktree-lib so its guarded fallbacks defer to ours.
for _cand in \
  "${AFK_HUB_INJECT:-}" \
  "$_GB_DIR/hub-inject.sh" \
  "$SCRIPT_DIR/hub-inject.sh" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/shared/skills/hub/scripts/hub-inject.sh}" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/hub-inject.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand _GB_DIR

# --- now-clock ----------------------------------------------------------------
# Current time, overridable via AFK_NOW for tests/cron.
afk_now() { printf '%s\n' "${AFK_NOW:-$(date +%s)}"; }

# --- per-spoke dispatch epochs (the wall-clock reap reference) ----------------
# Also the record of WHICH issues THIS run dispatched: a dispatch epoch exists only for
# a spoke this run spawned, so auto_land lands only those (not a foreign ready/<issue>
# from a parallel session). AFK_STATE_DIR overrides the location for tests.
_afk_state_dir() {
  if [ -n "${AFK_STATE_DIR:-}" ]; then printf '%s\n' "$AFK_STATE_DIR"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/ai-toolkit-afk"
}
stamp_dispatch_epoch() {
  local dir; dir="$(_afk_state_dir)"
  mkdir -p "$dir" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$dir/dispatch-$1.epoch" 2>/dev/null || true
}
read_dispatch_epoch() {
  local f; f="$(_afk_state_dir)/dispatch-$1.epoch"
  [ -f "$f" ] && cat "$f" 2>/dev/null || true
}
# _clear_dispatch_epochs -> drop every dispatch epoch so the "dispatched by this run"
# set starts empty for a freshly-armed window. Without this a stale epoch from a prior
# window could make a foreign ready/<issue> look like one we dispatched.
_clear_dispatch_epochs() {
  local dir; dir="$(_afk_state_dir)"
  rm -f "$dir"/dispatch-*.epoch 2>/dev/null || true
}

# --- event spool (issue #176) -------------------------------------------------
# The event-driven wake path: a spoke ANNOUNCES a state change (a marker push, a
# permission/question park) by dropping one content-free file per event, named
# <epoch>-<issue>-<type>, into this spool and SIGUSR1-ing the supervisor. Events are
# WAKE-UPS, not state — on wake the supervisor re-derives everything via slot_state, so a
# duplicate, stale, or lost event is safe (a lost one is caught by the next full sweep).
# The reader (hub-afk.sh) lives here; the two writers (spoke-ready.sh + the Notification
# hook) deploy to different dirs than the reader and inline the same tiny emit, sharing
# only this filename contract.
afk_event_dir() { printf '%s\n' "$(_afk_state_dir)/events"; }

# afk_drain_event_issues -> print each DISTINCT issue number that has a spooled event
# (one per line, sorted) and delete every spool file. Malformed names (no <issue> field)
# are dropped silently. Draining and dedup happen in one pass: the caller re-derives each
# named spoke's state via slot_state, so servicing an issue once per drain is enough.
afk_drain_event_issues() {
  local dir f base issue; dir="$(afk_event_dir)"
  [ -d "$dir" ] || return 0
  for f in "$dir"/*; do
    [ -f "$f" ] || continue
    base="${f##*/}"                      # <epoch>-<issue>-<type>
    issue="${base#*-}"; issue="${issue%%-*}"   # middle field
    case "$issue" in '' | *[!0-9]*) ;; *) printf '%s\n' "$issue" ;; esac
    rm -f "$f" 2>/dev/null || true
  done | sort -un
}

# --- per-spoke progress + answer-attempt epochs (issue #133, subtask 3) --------
# progress-<issue>.epoch — the reap ceiling's reference alongside the dispatch epoch:
# stamped when the branch tip advances between ticks, on a resume/respawn, and when a
# stale blocked marker is cleared, so a deliberately revived spoke gets a fresh
# ceiling instead of an instant re-reap (#123/#128).
# answer-attempt-<issue>.epoch — the idle clock's exclusion: stamped when the
# supervisor attempts an answer delivery, so time spent with a buffered/undelivered
# answer never reads as idle (the reaper killed #125 mid-delivery).
_stamp_issue_epoch() {
  local name="$1" issue="$2" dir
  dir="$(_afk_state_dir)"
  mkdir -p "$dir" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$dir/$name-$issue.epoch" 2>/dev/null || true
}
_read_issue_epoch() {
  local f; f="$(_afk_state_dir)/$1-$2.epoch"
  [ -f "$f" ] && cat "$f" 2>/dev/null || true
}
stamp_progress_epoch()  { _stamp_issue_epoch progress "$1"; }
read_progress_epoch()   { _read_issue_epoch progress "$1"; }
stamp_answer_attempt()  { _stamp_issue_epoch answer-attempt "$1"; }
read_answer_attempt()   { _read_issue_epoch answer-attempt "$1"; }
# done-<issue>.epoch — the un-landed clock's reference for the watchdog's auto-land-skipped
# check (#263): stamped the FIRST tick slot_state reads a ready/accept-at-tip terminal, so the
# ceiling measures from the done/ready transition, NOT the progress epoch (which is stamped
# only on tip advances and pre-ages during a pre-ready park — the false-fire this fixes).
# Cleared on a tip advance (a revived spoke re-stamps fresh) and on a fresh arm.
stamp_done_epoch()      { _stamp_issue_epoch done "$1"; }
read_done_epoch()       { _read_issue_epoch done "$1"; }
clear_done_epoch()      { rm -f "$(_afk_state_dir)/done-$1.epoch" 2>/dev/null || true; }
# stamp-once: a ready-at-tip spoke's un-landed clock starts at the FIRST done tick and does not
# reset while it stays done, so the full ceiling elapses before the watchdog can fire.
stamp_done_epoch_once() { [ -n "$(read_done_epoch "$1")" ] || stamp_done_epoch "$1"; }
# park-onset-<issue>.epoch — the watchdog's park-unanswered never-attempted reference (#265):
# stamped the FIRST tick slot_state reads a spoke `waiting`, so the park-unanswered ceiling
# measures from park onset rather than zero. The answer-attempt epoch is stamped only at answer
# DELIVERY (minutes into the answerer's reasoning), so before it exists the watchdog had no
# park-age floor and false-fired 1s after every fresh park. Held constant across consecutive
# waiting ticks (stamp-once) and cleared once the spoke is no longer parked, so a later re-park
# re-stamps fresh. Cleared on a fresh arm (_clear_progress_state), like done-<issue>.epoch.
stamp_park_onset_epoch()      { _stamp_issue_epoch park-onset "$1"; }
read_park_onset_epoch()       { _read_issue_epoch park-onset "$1"; }
clear_park_onset_epoch()      { rm -f "$(_afk_state_dir)/park-onset-$1.epoch" 2>/dev/null || true; }
stamp_park_onset_epoch_once() { [ -n "$(read_park_onset_epoch "$1")" ] || stamp_park_onset_epoch "$1"; }

# --- network-outage state (issue #249) ----------------------------------------
# A connectivity blackout makes the pre-reap auth probe fail for the WRONG reason. The two
# _afk_auth_is_dead callers in hub-afk.sh (reap_pass + _afk_service_auth_halt) share this state:
# the offline-since epoch anchors a CONSECUTIVE outage (surfaced as `OFFLINE for Nm` in --status),
# and _afk_refresh_offline_clocks re-stamps every in-flight spoke's idle + soft-ceiling clocks so
# the blackout is not counted toward a reap/block when connectivity returns. Per-window state,
# cleared by _clear_progress_state on a fresh arm. AFK_STATE_DIR overrides the location for tests.
_afk_offline_since_file() { printf '%s\n' "$(_afk_state_dir)/offline-since.epoch"; }
# stamp_offline_since -> record the epoch of the FIRST tick of the current outage run; a later
# tick must NOT overwrite it, so --status reports the true outage length, not just this tick.
stamp_offline_since() {
  local f; f="$(_afk_offline_since_file)"
  [ -f "$f" ] && return 0
  mkdir -p "$(_afk_state_dir)" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$f" 2>/dev/null || true
}
read_offline_since()  { local f; f="$(_afk_offline_since_file)"; [ -f "$f" ] && cat "$f" 2>/dev/null || true; }
clear_offline_since() { rm -f "$(_afk_offline_since_file)" 2>/dev/null || true; }
# offline_minutes -> whole minutes since the outage began, or empty when there is no outage.
offline_minutes() {
  local since; since="$(read_offline_since)"
  case "$since" in '' | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "$(( ($(afk_now) - since) / 60 ))"
}
# _afk_refresh_offline_clocks -> stamp a fresh progress (soft ceiling) + answer-attempt (idle
# clock) epoch for every in-flight spoke, so a network-OUTAGE tick's frozen transcript mtime does
# not accumulate into an idle/ceiling reap when connectivity returns (#249). The ABSOLUTE hard
# ceiling (dispatch x mult) is deliberately left untouched as a backstop. Best-effort per spoke.
_afk_refresh_offline_clocks() {
  local path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    stamp_progress_epoch "$issue"
    stamp_answer_attempt "$issue"
  done < <(inflight_worktrees)
}

# Fresh window ⇒ no stale progress/attempt state: a leftover answer-attempt epoch
# would suppress a legitimate idle reap in the next window; a leftover re-answer counter
# (#203) would strand a spoke at a ceiling reached in a prior window; a leftover gate-voided /
# terminal-logged marker (#237) would keep a since-resolved gate terminal across windows; a
# leftover warned-retry backoff (#241) would inherit a prior window's grown cadence and skip
# the clean first-exhaustion re-service.
_clear_progress_state() {
  local dir; dir="$(_afk_state_dir)"
  rm -f "$dir"/progress-*.epoch "$dir"/answer-attempt-*.epoch "$dir"/done-*.epoch "$dir"/tip-* \
    "$dir"/park-onset-*.epoch \
    "$dir"/reanswer-* "$dir"/gate-voided-* "$dir"/terminal-logged-* \
    "$dir"/wd-fire-dedup-* \
    "$dir"/offline-since.epoch 2>/dev/null || true   # #249: drop a stale outage marker too
  # #263: the watchdog's firing-dedup markers are per-window too — a leftover would suppress a
  # condition's first ledger firing in the next window (an autonomy-score under-count).
  _clear_warned_records   # #241: drop the warned-retry backoff + records for a fresh window
}

# --- re-answer ceiling (issue #203, finding 1) --------------------------------
# #171's blocked-at-tip→waiting fix made a parked spoke re-answerable with NO attempt
# ceiling: a legitimately-escalated spoke (answerer ESCALATE, timeout, unconfirmable
# inject) stays on the SAME prompt, and every tick re-ran the full 900s reasoner to reach
# the same ESCALATE — a doom-loop starving the tick and burning the subscription. The
# ceiling caps attempts on the SAME (tip, prompt-signature); a changed prompt or a moved
# tip resets it. Keyed like the decisions-log signature machinery (a content hash here).

# _broker_park_signature <wt> <issue> -> a stable hash of WHATEVER prompt the spoke is
# parked on (a permission command, a PLAN-gate plan, or an AskUserQuestion), or empty when
# nothing is extractable. Empty ⇒ the ceiling never engages (fail-open to answering).
_broker_park_signature() {
  local wt="$1" issue="$2" basis=""
  if _permission_pending "$wt"; then
    basis="perm:$(extract_pending_command "$wt")"
    # A shown dialog whose gated command is unflushed (empty) still parks (#269). Give it a
    # STABLE non-empty signature so the re-answer ceiling can BOUND the per-tick declines
    # (keyed on (tip, sig), it backs off after AFK_REANSWER_CEILING) instead of fail-opening
    # to a re-decline every tick on an empty signature (#269 review).
    [ "$basis" = "perm:" ] && basis="perm:unreadable"
  elif _gate_parked "$wt" "$issue"; then
    basis="gate:$(_read_gate_artifact "$wt" "$issue")"
    [ "$basis" = "gate:" ] && basis="gate:$(extract_pending_question "$wt")"
  else
    basis="q:$(extract_pending_question "$wt")"
  fi
  case "$basis" in perm: | gate: | q:) return 0 ;; esac    # nothing extractable
  printf '%s' "$basis" | shasum -a 256 2>/dev/null | awk '{print $1}'
}

# _reanswer_state_file <issue> -> the per-issue counter file: "<tip>\t<sig>\t<count>".
_reanswer_state_file() { printf '%s\n' "$(_afk_state_dir)/reanswer-$1"; }

# _broker_reanswer_exhausted <wt> <issue> <sig> -> rc 0 (EXHAUSTED — be terminal, skip the
# reasoner) when the SAME (tip, sig) has already been attempted AFK_REANSWER_CEILING (default
# 2) times; otherwise rc 1 AND this attempt is RECORDED (the counter bumped). A changed tip
# or signature resets the counter. An empty signature never suppresses (fail-open).
_broker_reanswer_exhausted() {
  local wt="$1" issue="$2" sig="$3" ceiling tip f prev_tip="" prev_sig="" prev_n=0
  [ -n "$sig" ] || return 1
  ceiling="${AFK_REANSWER_CEILING:-2}"
  case "$ceiling" in '' | *[!0-9]*) ceiling=2 ;; esac
  [ "$ceiling" -lt 1 ] && ceiling=1   # floor at 1: a 0 ceiling would strand every gate unanswered
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)"
  f="$(_reanswer_state_file "$issue")"
  if [ -f "$f" ]; then
    IFS=$'\t' read -r prev_tip prev_sig prev_n < "$f" 2>/dev/null || true
    case "$prev_n" in '' | *[!0-9]*) prev_n=0 ;; esac
  fi
  if [ "$prev_tip" != "$tip" ] || [ "$prev_sig" != "$sig" ]; then prev_n=0; fi   # new context
  [ "$prev_n" -ge "$ceiling" ] && return 0                                       # exhausted
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\t%s\n' "$tip" "$sig" "$(( prev_n + 1 ))" > "$f" 2>/dev/null || true
  return 1
}

# --- terminal gate markers (issue #237) ---------------------------------------
# A reasoner mutation-void is terminal on the FIRST occurrence: the reasoner wrote the
# spoke's live tree, so a human is required regardless of the parked prompt or branch tip.
# Unlike the (tip, sig) re-answer ceiling — which the mutation itself perturbs, since the
# write moves the tip and flips the pending command, resetting that counter every tick — this
# marker is durable and independent of both, so a voided gate never re-runs the reasoner.
# Cleared only on a fresh arm (_clear_progress_state), a current-window view.
_broker_voided_marker() { printf '%s\n' "$(_afk_state_dir)/gate-voided-$1"; }
_broker_gate_voided()   { [ -f "$(_broker_voided_marker "$1")" ]; }
_broker_mark_voided() {
  local issue="$1" f; f="$(_broker_voided_marker "$issue")"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$f" 2>/dev/null || true
}

# _broker_log_terminal_once <issue> <key> <msg> -> log <msg> only the FIRST tick a gate
# becomes terminal for <key>; a later tick on the same key stays silent, so a terminal gate
# never re-emits its "terminal" line on every event wake (issue #237). <key> folds in whatever
# the terminal state keys on (tip + signature for the re-answer ceiling), so a genuinely NEW
# terminal context (a moved tip / changed prompt) logs afresh.
_broker_terminal_log_file() { printf '%s\n' "$(_afk_state_dir)/terminal-logged-$1"; }
_broker_log_terminal_once() {
  local issue="$1" key="$2" msg="$3" f prev=""
  f="$(_broker_terminal_log_file "$issue")"
  [ -f "$f" ] && prev="$(cat "$f" 2>/dev/null)"
  [ "$prev" = "$key" ] && return 0
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\n' "$key" > "$f" 2>/dev/null || true
  log "$msg"
}

# _afk_note_tip_progress <wt> <issue> -> observe ledger progress as branch-tip
# advance: the first sighting records the tip WITHOUT stamping; a differing tip on a
# later tick stamps progress and re-records. Best-effort; never aborts the caller.
_afk_note_tip_progress() {
  local wt="$1" issue="$2" tip dir f last
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 0
  [ -n "$tip" ] || return 0
  # slot_state calls this ONLY after every terminal return, so a spoke observed here is
  # non-terminal at this tip → any live done-epoch is stale. Drop it unconditionally (#263) so a
  # revived-then-re-ready spoke re-stamps a fresh un-landed clock instead of measuring from the
  # pre-revival transition — covers first-sighting, tip-advance, and unchanged-tip alike.
  clear_done_epoch "$issue"
  dir="$(_afk_state_dir)"; f="$dir/tip-$issue"
  last="$( [ -f "$f" ] && cat "$f" 2>/dev/null )"
  if [ -z "$last" ]; then
    mkdir -p "$dir" 2>/dev/null || true
    printf '%s\n' "$tip" > "$f" 2>/dev/null || true
  elif [ "$last" != "$tip" ]; then
    printf '%s\n' "$tip" > "$f" 2>/dev/null || true
    stamp_progress_epoch "$issue"
    _afk_clear_warned "$issue"   # #241: a tip advance is genuine progress → drop the warned-retry backoff
  fi
  return 0
}

# _afk_ceiling_epoch <issue> -> the wall-clock ceiling's reference epoch:
# max(dispatch, progress). Empty when neither exists (spoke_over_ceiling reads that
# as "can't measure → never reap").
_afk_ceiling_epoch() {
  local issue="$1" d p
  d="$(read_dispatch_epoch "$issue")"
  p="$(read_progress_epoch "$issue")"
  case "$d" in '' | *[!0-9]*) d=0 ;; esac
  case "$p" in '' | *[!0-9]*) p=0 ;; esac
  [ "$p" -gt "$d" ] && d="$p"
  [ "$d" -gt 0 ] && printf '%s\n' "$d"
  return 0
}

# _spoke_over_any_ceiling <issue> <now> -> the reaper's full ceiling test. Progress
# DEFERS the soft ceiling (a revived / committing spoke gets fresh AFK_SPOKE_MAX_MINUTES
# from its last progress), but the dispatch epoch keeps an ABSOLUTE backstop at
# AFK_SPOKE_HARD_CEILING_MULT (default 3) x AFK_SPOKE_MAX_MINUTES — without it a
# doom-loop that commits every <180m would evade the reaper for the whole drain
# window, the exact outcome the reaper exists to prevent (ST3 review).
_spoke_over_any_ceiling() {
  local issue="$1" now="$2" d mult
  spoke_over_ceiling "$(_afk_ceiling_epoch "$issue")" "$now" && return 0
  d="$(read_dispatch_epoch "$issue")"
  case "$d" in '' | *[!0-9]*) return 1 ;; esac
  case "$now" in '' | *[!0-9]*) return 1 ;; esac
  mult="${AFK_SPOKE_HARD_CEILING_MULT:-3}"
  case "$mult" in '' | *[!0-9]*) mult=3 ;; esac
  [ "$(( (now - d) / 60 ))" -gt "$(( AFK_SPOKE_MAX_MINUTES * mult ))" ]
}

# --- sibling-script resolution ------------------------------------------------
# Find a workflow script across the checkout + synced layouts; the first existing
# candidate wins. An explicit override (passed as $1) short-circuits.
_afk_find_script() {
  local override="$1" name="$2" cand
  for cand in \
    "$override" \
    "$SCRIPT_DIR/$name" \
    "$SCRIPT_DIR/../../../../scripts/$name" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/scripts/$name" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/.ai-toolkit/scripts/$name" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/.ai-toolkit/scripts/hub/$name"; do
    [ -n "$cand" ] && [ -f "$cand" ] && { printf '%s\n' "$cand"; return 0; }
  done
  return 1
}

# --- in-flight survey ---------------------------------------------------------
# "<path>\t<issue>" per task worktree whose branch slug leads with an issue number.
# Built on worktree-lib's wt_task_worktrees so the hub and these helpers agree on
# what counts as a task worktree.
inflight_worktrees() {
  local main path br slug num
  main="$(wt_main_root 2>/dev/null)" || return 0
  while IFS=$'\t' read -r path br; do
    [ -n "$path" ] || continue
    slug="${br##*/}"
    num="$(printf '%s' "$slug" | sed 's/^\([0-9]*\).*/\1/')"
    [ -n "$num" ] && printf '%s\t%s\n' "$path" "$num"
  done < <(wt_task_worktrees "$main")
}
inflight_issues() { inflight_worktrees | cut -f2; }

# --- transcript helpers (newest .jsonl in the spoke's Claude project dir) -----
_transcript_idle_seconds() {
  local jsonl mtime; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  mtime="$(stat -f %m "$jsonl" 2>/dev/null || stat -c %Y "$jsonl" 2>/dev/null)"
  [ -n "$mtime" ] || return 0
  printf '%s\n' "$(( $(afk_now) - mtime ))"
}
# _task_output_mtime <wt_path> -> newest mtime among the harness background-task output
# files for this worktree's sessions, or empty when none exist. A spoke waiting on a
# background workflow (a code-review) writes nothing to its transcript (#180), so the
# reaper's idle clock reads a stale transcript mtime and kills it as hung. The harness
# streams each background task's stdout to <tmp>/claude-*/<munged-wt>/*/tasks/*.output as
# it runs — a fresh write there is the missing "still working" signal. AFK_TASKS_ROOT
# overrides the tmp root (tests; defaults to macOS /private/tmp).
_task_output_mtime() {
  local wt_path="$1" slug root newest="" mt f
  slug="$(printf '%s' "$wt_path" | sed 's/[^A-Za-z0-9]/-/g')"
  root="${AFK_TASKS_ROOT:-/private/tmp}"
  for f in "$root"/claude-*/"$slug"/*/tasks/*.output; do
    [ -f "$f" ] || continue        # no match: the glob stays literal, skipped here
    mt="$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null)"
    [ -n "$mt" ] || continue
    if [ -z "$newest" ] || [ "$mt" -gt "$newest" ]; then newest="$mt"; fi
  done
  [ -n "$newest" ] && printf '%s\n' "$newest"
}
# _spoke_idle_seconds <wt_path> <issue> -> idle seconds for the REAPER's clock: since the
# LATEST of three references — the transcript's last write, the supervisor's last
# answer-delivery attempt, and the newest background-task output write. Time with a
# buffered/undelivered answer is not idle (#133; the reaper killed #125 right as its
# answer was delivered); neither is a spoke waiting on a background workflow that writes
# nothing to its transcript (#180; the reaper killed a healthy #168 mid code-review).
# These signals EXTEND the idle reference only — the wall-clock ceiling (#133) is checked
# separately and stays untouched. Empty when no reference exists (same "can't measure"
# contract as _transcript_idle_seconds).
#
# #241 review N2: folding the answer-attempt epoch into the idle reference is the DELIBERATE
# #133 trade-off — a spoke sitting on a buffered/undelivered answer (or a frozen-but-alive
# claude whose inject didn't land) reads BUSY, so the reaper never kills it mid-delivery. This
# does NOT strand such a spoke: the separate WALL-CLOCK ceiling (_spoke_over_any_ceiling,
# AFK_SPOKE_MAX_MINUTES × the hard multiplier) ignores the answer-attempt fold and still fires,
# and under #241 §7 that ceiling REVIVES the spoke (kill + relaunch) rather than abandoning it.
# So the fold is safe by construction and is intentionally NOT gated on inject success.
_spoke_idle_seconds() {
  local wt="$1" issue="$2" ref attempt task
  ref="$(_transcript_mtime "$wt")"
  attempt="$(read_answer_attempt "$issue")"
  case "$attempt" in
    '' | *[!0-9]*) : ;;
    *) if [ -z "$ref" ] || [ "$attempt" -gt "$ref" ]; then ref="$attempt"; fi ;;
  esac
  # A task-output write only EXTENDS an existing reference — it never creates
  # measurability on its own. tmp is not cleared between runs, so a lingering .output from
  # a prior incarnation at a reused worktree path would otherwise drag a transcript-less
  # fresh spoke out of the "can't measure -> busy" guard and into a bogus idle reap off a
  # stale mtime (#180 review). Unlike the answer-attempt epoch (cleared per window), the
  # task-output signal can be stale, so it must not stand alone.
  task="$(_task_output_mtime "$wt")"
  case "$task" in
    '' | *[!0-9]*) : ;;
    *) if [ -n "$ref" ] && [ "$task" -gt "$ref" ]; then ref="$task"; fi ;;
  esac
  [ -n "$ref" ] || return 0
  printf '%s\n' "$(( $(afk_now) - ref ))"
}
# extract_pending_question <wt_path> -> the prompt the spoke is parked on, or empty when
# it is NOT waiting. The same waiting signal hub-status.sh surfaces (an open
# AskUserQuestion, or a trailing notification entry) — but here we return the actual
# question + options / trailing assistant message so the answerer has something to reason
# about. Empty output ⇒ not waiting, so this doubles as the auto-answer trigger.
extract_pending_question() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  _AFK_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json, os

pending = None        # list of formatted AskUserQuestion questions, or None
last_asst_text = ""   # text of the most recent assistant message
gate_plan = ""        # plan prose of a PLAN-gate park (spoke-ready.sh --gate), or ""
gate_ids = set()      # tool_use ids of gate emissions, to detect a FAILED one (is_error)
last_type = None
try:
    with open(os.environ["_AFK_JSONL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            last_type = obj.get("type")
            content = (obj.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                if last_type == "user":
                    pending = None
                continue
            if last_type == "assistant":
                asks, texts, gate_id = [], [], None
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        texts.append(block.get("text") or "")
                    elif block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                        for q in (block.get("input") or {}).get("questions") or []:
                            lines = [f"Q: {q.get('question', '').strip()}"]
                            for opt in q.get("options") or []:
                                label = (opt.get("label") or "").strip()
                                desc = (opt.get("description") or "").strip()
                                lines.append(f"  - {label}: {desc}" if desc else f"  - {label}")
                            asks.append("\n".join(lines))
                    elif block.get("type") == "tool_use" and block.get("name") == "Bash":
                        if "spoke-ready.sh --gate" in ((block.get("input") or {}).get("command") or ""):
                            gate_id = block.get("id") or True   # True: a park with no id (fixtures)
                if texts:
                    last_asst_text = "\n".join(t for t in texts if t).strip()
                pending = asks or None
                # A PLAN-gate park = prose plan + a `spoke-ready.sh --gate` Bash, no
                # AskUserQuestion. Remember the plan so the answerer has it to reason about; the
                # emission's tool_result (below) can still un-latch it if it FAILED.
                if gate_id:
                    gate_plan = last_asst_text
                    if gate_id is not True:
                        gate_ids.add(gate_id)
            elif last_type == "user":
                # A gate emission that resolved with is_error (a hook DENY or a script failure)
                # never established a park (issue #271): un-latch the plan so a spoke that keeps
                # working is read as busy, not `waiting` — the phantom park the watchdog answered.
                for b in content:
                    if (isinstance(b, dict) and b.get("type") == "tool_result"
                            and b.get("tool_use_id") in gate_ids and b.get("is_error")):
                        gate_plan = ""
                # A real human reply (a text block) means the spoke is no longer parked;
                # a tool_result-only user turn (e.g. the gate Bash's result) does NOT.
                if any(isinstance(b, dict) and b.get("type") == "text" for b in content):
                    pending = None
                    gate_plan = ""
except Exception:
    pass

out = ""
if pending:
    out = "\n\n".join(pending)
elif last_type == "notification":
    out = last_asst_text
elif gate_plan:
    out = gate_plan
# Bound the payload so a huge plan message can't blow up the answerer prompt.
print(out[:4000].strip())
PYEOF
}

# _is_seed_replay <wt_path> <text> -> true when <text> substantially replays the
# spoke's SEED prompt (the first user message in its transcript): normalized-whitespace,
# case-folded containment of the answer's first 200 chars in the seed, or of the whole
# seed in the answer. Short answers (< AFK_SEED_REPLAY_MIN_CHARS, default 80) are
# exempt — option labels legitimately appear inside a long kickoff. #124: the answerer
# echoed the kickoff back into a parked spoke six ticks in a row; a replay is never
# injected. Unreadable transcript / no python ⇒ not a replay (fail-open to answering).
_is_seed_replay() {
  local wt="$1" text="$2" jsonl
  jsonl="$(_spoke_jsonl "$wt")"
  [ -n "$jsonl" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_JSONL="$jsonl" _AFK_TEXT="$text" python3 2>/dev/null <<'PYEOF'
import json, os, re, sys

def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()

seed = ""
try:
    with open(os.environ["_AFK_JSONL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "user":
                continue
            content = (obj.get("message") or {}).get("content") or []
            if isinstance(content, str) and content.strip():
                seed = content
                break
            if isinstance(content, list):
                texts = [b.get("text") or "" for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                if any(t.strip() for t in texts):
                    seed = "\n".join(texts)
                    break
except Exception:
    sys.exit(1)

ans = norm(os.environ.get("_AFK_TEXT", ""))
seed = norm(seed)
try:
    floor = int(os.environ.get("AFK_SEED_REPLAY_MIN_CHARS", "80"))
except ValueError:
    floor = 80
replay = bool(seed) and len(ans) >= floor and (ans[:200] in seed or seed in ans)
sys.exit(0 if replay else 1)
PYEOF
}

# --- slot state ---------------------------------------------------------------
# slot_state <wt_path> <issue> -> done|waiting|reap|busy.
#   done    — a TERMINAL marker (ready/accept/blocked) at the branch tip.
#   waiting — parked on a question / gate / permission dialog (auto-answer it; never reaped,
#             regardless of ceiling — park detection precedes both reap verdicts, #246).
#   reap    — over the wall-clock ceiling, or idle past AFK_IDLE_MINUTES, AND with no
#             detectable pending park (a hung/working spoke, not a park).
#   busy    — actively working (or just spawned, no transcript yet).
slot_state() {
  local wt_path="$1" issue="$2" tip marker kind age
  tip="$(git -C "$wt_path" rev-parse HEAD 2>/dev/null)"
  if [ -n "$tip" ]; then
    for kind in ready accept; do
      marker="$(git -C "$wt_path" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)"
      # Stamp the un-landed clock on the FIRST done tick (#263) so the watchdog measures the
      # ceiling from here, not a progress epoch that pre-aged during a pre-ready park.
      [ "$marker" = "$tip" ] && { stamp_done_epoch_once "$issue"; printf 'done\n'; return; }
    done
    # blocked/<issue> at the tip is terminal ONLY if the spoke is not still parked. A
    # spurious blocked/<N> (a false escalation) over a spoke still on a question / permission
    # dialog would otherwise strand it — read as done, never re-answered, never reaped until
    # the window ends (#171-subtask-3). If it is still parked on an extractable prompt, read
    # it as waiting (re-answerable); reconcile_markers keeps clearing the tag once commits
    # land on top.
    if [ "$(git -C "$wt_path" rev-parse -q --verify "refs/tags/blocked/${issue}^{commit}" 2>/dev/null)" = "$tip" ]; then
      if [ -n "$(extract_pending_question "$wt_path")" ] || _permission_pending "$wt_path"; then
        stamp_park_onset_epoch_once "$issue"; printf 'waiting\n'; return
      fi
      printf 'done\n'; return
    fi
    # A pushed gate/<issue> at the tip = parked at the PLAN gate → waiting, never reaped.
    # The gate is a prose plan + this tag (no AskUserQuestion), so extract_pending_question
    # can't see it. Checking at the tip is self-clearing: once approved and the spoke
    # commits its first RED/GREEN, the tip moves past the gate commit and it reads busy.
    if [ "$(git -C "$wt_path" rev-parse -q --verify "refs/tags/gate/${issue}^{commit}" 2>/dev/null)" = "$tip" ]; then
      stamp_park_onset_epoch_once "$issue"; printf 'waiting\n'; return
    fi
  fi
  # Ledger progress (a tip advance since the last tick) refreshes the ceiling before
  # it is measured — a revived spoke is not re-reaped off its stale dispatch epoch.
  _afk_note_tip_progress "$wt_path" "$issue"
  # Park detection precedes BOTH reaps (#246): an answerable park — a pending question or a
  # permission dialog — is serviced by the answer lane, so it classifies `waiting` however long
  # it has been parked, never `reap`. Pre-#246 the wall-clock ceiling reap ran first, so an
  # over-ceiling permission-parked spoke was reaped + revived (claude --continue), which only
  # re-raised the identical dialog: parked -> reaped -> revived -> parked forever. The doom-loop a
  # genuinely-stuck dialog could form is bounded NOT here but in the answer lane
  # (broker_service_gate's _broker_reanswer_exhausted / AFK_REANSWER_CEILING + the _afk_warned_arm
  # backoff, escalating to blocked/<issue> on a real judgment call), so park-wins is unconditional.
  if [ -n "$(extract_pending_question "$wt_path")" ]; then stamp_park_onset_epoch_once "$issue"; printf 'waiting\n'; return; fi
  # A pending permission dialog (a CC confirmation prompt, no transcript entry) is decided by
  # the supervisor's classifier, so it waits — never reaped as idle (#149) or over-ceiling (#246).
  if _permission_pending "$wt_path"; then stamp_park_onset_epoch_once "$issue"; printf 'waiting\n'; return; fi
  # Past every park check ⇒ the spoke is NOT parked (busy/reap). Reset its park-onset clock so a
  # later re-park measures the watchdog's park-unanswered ceiling from the NEW onset, not a stale
  # one (#265). Placed here, not in _afk_note_tip_progress above: that runs BEFORE the two waiting
  # returns just above, so clearing there would clear-then-restamp a still-parked spoke every tick.
  clear_park_onset_epoch "$issue"
  if _spoke_over_any_ceiling "$issue" "$(afk_now)"; then printf 'reap\n'; return; fi
  age="$(_spoke_idle_seconds "$wt_path" "$issue")"
  if [ -n "$age" ] && [ "$age" -gt $(( AFK_IDLE_MINUTES * 60 )) ]; then printf 'reap\n'; return; fi
  printf 'busy\n'
}

# spoke_over_ceiling <dispatch_epoch> <now> -> true when a spoke has run longer than
# AFK_SPOKE_MAX_MINUTES. An empty/non-numeric epoch or clock reads as "not over" (can't
# measure → never reap), guarding `set -u` arithmetic against a bareword.
spoke_over_ceiling() {
  local epoch="$1" now="$2"
  case "$epoch" in '' | *[!0-9]*) return 1 ;; esac
  case "$now" in '' | *[!0-9]*) return 1 ;; esac
  [ "$(( (now - epoch) / 60 ))" -gt "$AFK_SPOKE_MAX_MINUTES" ]
}

# _gate_parked <wt> <issue> -> true when a gate/<issue> tag sits AT the branch tip:
# the spoke is parked at its PLAN gate. The same check slot_state does inline; here
# for the answerer's gate routing and its pre-inject re-check (#133).
_gate_parked() {
  local wt="$1" issue="$2" tip
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  [ -n "$tip" ] || return 1
  [ "$(git -C "$wt" rev-parse -q --verify "refs/tags/gate/${issue}^{commit}" 2>/dev/null)" = "$tip" ]
}

# _gate_answer_landed <wt> -> rc 0 when the spoke's transcript shows a GENUINE human/hub
# reply — a TYPED prompt submission (promptSource == "typed"): a human typing in the pane,
# or the broker's own tmux inject — AFTER the assistant turn that ran `spoke-ready.sh
# --gate`, i.e. the PLAN-gate approval reply already landed. Used to self-heal a STALE gate
# tag (issue #204): _consume_gate_tag ran only on the broker's confirmed-inject path, so an
# answer that registered late, a wedge respawn started OUTSIDE the broker, or ANY
# attended/manual reply in the pane left gate/<N> at the tip — re-read as "waiting" and
# re-answered, and (with the #204 guard) wedging the resumed spoke. Every synthetic user
# turn the harness injects (tool_results, <task-notification>/<system-reminder>, skill/meta
# turns, SDK/system prompts) carries a non-"typed" promptSource (or none), so it can NOT
# false-consume the tag on a spoke still awaiting its first approval. A (re-)park supersedes
# an earlier approval. Fail-CLOSED (rc 1): no transcript, no python3, or no typed post-park
# turn means "cannot prove a reply landed" → the broker services the gate as before. The
# plan-gate-guard's approval_in_transcript mirrors this so both sides read the same signal.
_gate_answer_landed() {
  local wt="$1" jsonl
  jsonl="$(_spoke_jsonl "$wt")"
  [ -n "$jsonl" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json, os, sys

parked = False
approved = False
try:
    with open(os.environ["_AFK_JSONL"], encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            ttype = obj.get("type")
            content = (obj.get("message") or {}).get("content")
            if ttype == "assistant":
                for block in content if isinstance(content, list) else []:
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("name") == "Bash"
                            and "spoke-ready.sh --gate" in ((block.get("input") or {}).get("command") or "")):
                        parked = True       # a (re-)park supersedes any earlier approval
                        approved = False
            elif ttype == "user" and parked:
                # ONLY a typed prompt submission is a genuine reply — harness-injected user
                # turns (tool_results, notifications, skill/meta, SDK/system) are not.
                if obj.get("promptSource") == "typed" and not obj.get("isMeta"):
                    approved = True
except Exception:
    sys.exit(1)
sys.exit(0 if approved else 1)
PYEOF
}

# _gate_artifact_path <wt> <issue> -> the gate plan artifact path (<wt>/.ai-toolkit/
# gate-<issue>.md). The single owner of that layout, shared by _read_gate_artifact and
# _consume_gate_tag (spoke-ready.sh writes the same path from the spoke side, #175). Falls
# back to <wt> as the root when rev-parse can't resolve a toplevel (a non-git path in a test).
_gate_artifact_path() {
  local wt="$1" issue="$2" root
  root="$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$wt")"
  printf '%s\n' "$root/.ai-toolkit/gate-$issue.md"
}

# _read_gate_artifact <wt> <issue> -> the plan the spoke wrote to its gate artifact
# (<wt>/.ai-toolkit/gate-<issue>.md, written by spoke-ready.sh --gate, issue #175), or empty
# when absent. The SCRIPTED handoff channel the gate route PREFERS over parsing the spoke
# transcript (extract_pending_question): a script reads what a script wrote, no heuristic.
# Empty (fall back to the transcript) when the spoke parked without writing one (a bare --gate).
_read_gate_artifact() {
  local wt="$1" issue="$2" f
  f="$(_gate_artifact_path "$wt" "$issue")"
  [ -f "$f" ] || return 0
  # Cap at 4000 CHARACTERS (matching extract_pending_question's out[:4000]) so a huge plan
  # can't blow up the answerer prompt AND a multibyte plan is never split mid-character —
  # head -c would cut on bytes. python3 is the broker's existing text tool (the
  # extract_pending_question path); when it is unavailable the untruncated plan
  # (spoke-authored, bounded in practice) is safer than a byte-truncated one.
  if command -v python3 >/dev/null 2>&1; then
    _AFK_GATE_FILE="$f" python3 -c \
      'import os,sys; sys.stdout.write(open(os.environ["_AFK_GATE_FILE"], encoding="utf-8", errors="replace").read()[:4000])' \
      2>/dev/null
  else
    cat "$f" 2>/dev/null
  fi
}

# _still_parked_same <wt> <issue> <was_gate> <question> <before_mtime> -> true when the
# spoke is still parked on the SAME prompt the answerer reasoned about. The answerer
# takes minutes; a spoke that moved on meanwhile (a human replied, the turn resumed)
# must not receive the stale answer mid-turn (#129/#89), and a spoke now parked on a
# DIFFERENT question needs a fresh answer, not this one. Three signals, ALL required:
#   - the transcript has not moved since the answerer started (<before_mtime>) — any
#     write means activity, and a gate tag alone can't be trusted: it stays at the tip
#     until the FIRST COMMIT, so a spoke that self-approved and kept coding (#117), or
#     a human approving in-pane, still reads "parked" by the tag;
#   - for a gate park, the gate/<issue> tag is still at the tip;
#   - the extraction is unchanged (catches a same-second write mtime can't see; for an
#     unextractable gate park this is the vacuous "" = "").
_still_parked_same() {
  local wt="$1" issue="$2" was_gate="$3" question="$4" before="$5"
  [ "$(_transcript_mtime "$wt")" = "$before" ] || return 1
  if [ "$was_gate" -eq 1 ]; then
    _gate_parked "$wt" "$issue" || return 1
  fi
  [ "$(extract_pending_question "$wt")" = "$question" ]
}

# _spoke_still_parked <wt> <issue> -> true when the spoke is currently parked on SOMETHING (a
# permission dialog, a PLAN gate, or an extractable question) — regardless of whether it is the
# SAME prompt as before. #241 §4 uses this to tell a genuine park-change (recompute) from a
# spoke that has MOVED ON and is actively working (no park → drop, preserving the #89 no-inject
# -mid-turn guard). A positive park signal, so an ambiguous read fails toward "moved on" (drop).
_spoke_still_parked() {
  local wt="$1" issue="$2"
  _permission_pending "$wt" && return 0
  _gate_parked "$wt" "$issue" && return 0
  [ -n "$(extract_pending_question "$wt")" ]
}

# _spoke_moved_on <wt> <before_mtime> -> true ONLY when the spoke's transcript has a NEW
# write since <before_mtime>: a positive, confident signal that it is actively working. The
# escalation freshness-gate (#171-subtask-2) uses this rather than !_still_parked_same so it
# fails SAFE: an unreadable clock (empty / non-numeric mtime) or a non-numeric baseline reads
# as "cannot confirm movement" → NOT moved on → the escalation is still stamped. Dropping an
# escalation is only warranted on demonstrated activity, never on an ambiguous probe (review).
_spoke_moved_on() {
  local wt="$1" before="$2" now
  now="$(_transcript_mtime "$wt")"
  case "$now" in '' | *[!0-9]* ) return 1 ;; esac
  case "$before" in '' | *[!0-9]* ) return 1 ;; esac
  [ "$now" -gt "$before" ]
}

# --- the answerer (the one reasoning step) ------------------------------------

# --- read-only worktree reasoner (issue #155, subtask B) ----------------------
# The gate reasoner gets READ-ONLY access to the spoke's LIVE worktree (cwd) so it can
# verify a decision against real state — uncommitted/staged included — before auto-
# answering: evidence, not a pattern-guess. Two enforcement layers:
#   1. PREVENTION — run with a read-only tool allowlist (the code-review/Explore
#      posture: Read/Grep/Glob + a narrow read-only git helper; never Edit/Write).
#   2. DETECTION — a content fingerprint of the worktree taken before and after the
#      reason step; ANY change is a read-only BREACH, so the answer is voided and the
#      gate routes to a human. Detection is the HARD guarantee: it does not depend on
#      the LLM honoring the allowlist.

# reasoner_allowed_tools -> the read-only allowlist passed to the headless reasoner
# (comma-joined for `claude --allowedTools`). Read/Grep/Glob plus narrow read-only git
# verbs via scoped Bash patterns — enough to inspect the tree and run status/diff to
# verify a plan, nothing that can mutate it. AFK_REASONER_TOOLS overrides.
# UPGRADE: confirm the exact `claude --allowedTools` list/pattern syntax against the
# installed CLI version if the reasoner ever reports a read tool it should have.
reasoner_allowed_tools() {
  printf '%s\n' "${AFK_REASONER_TOOLS:-Read,Grep,Glob,Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git rev-parse:*)}"
}

# _reasoner_bash_readonly <inner> -> rc 0 when a scoped Bash allow pattern's inner
# command is a read-only git verb (git status/diff/log/show/rev-parse/branch/ls-files/
# cat-file), rc 1 otherwise. Keeps a `Bash(...)` allow from smuggling a mutating verb
# (git push/commit/reset, rm, chmod, …) past assert_readonly_tools.
_reasoner_bash_readonly() {
  case "$1" in
    'git status'* | 'git diff'* | 'git log'* | 'git show'* | 'git rev-parse'* \
      | 'git branch'* | 'git ls-files'* | 'git cat-file'*) return 0 ;;
    *) return 1 ;;
  esac
}

# assert_readonly_tools <comma-list> -> rc 0 when every tool is read-only, rc 1 when any
# is a mutating tool (Edit/Write/MultiEdit/NotebookEdit), a bare unrestricted Bash, or a
# scoped Bash(...) whose inner verb is NOT a read-only git verb. Anything unrecognised is
# denied (default-deny). Parses by hand (no word-splitting) so a glob in a Bash(...)
# pattern never expands.
assert_readonly_tools() {
  local rest="$1" tok inner
  while [ -n "$rest" ]; do
    tok="${rest%%,*}"
    if [ "$tok" = "$rest" ]; then rest=""; else rest="${rest#*,}"; fi
    tok="${tok#"${tok%%[![:space:]]*}"}"; tok="${tok%"${tok##*[![:space:]]}"}"   # trim
    [ -n "$tok" ] || continue
    case "$tok" in
      Read | Grep | Glob | LS | WebFetch | WebSearch | TodoRead) ;;
      'Bash('*')')                                     # a scoped Bash verb: vet it
        inner="${tok#Bash(}"; inner="${inner%)}"
        _reasoner_bash_readonly "$inner" || return 1 ;;
      *) return 1 ;;                                    # mutating / bare Bash / unknown -> deny
    esac
  done
  return 0
}

# _broker_worktree_fingerprint <wt> -> a content hash of the LIVE worktree's TRACKED content
# PLUS its untracked-not-ignored files: each path + its CURRENT working-tree content. A
# tracked edit, a staged addition, a deletion, OR a brand-new untracked-not-ignored file all
# change it. IGNORED files stay excluded on purpose (issue #168): a parked spoke is not a
# frozen worktree — its own still-finishing push gate writes `.testmondata`, OTel dumps land
# under `.ai-toolkit/`, etc. Those runtime artifacts are git-ignored, so they must not be
# blamed on the read-only reasoner. `--others --exclude-standard` (issue #203) closes the
# creation gap #168 opened — a reasoner that CREATES a new untracked file used to be invisible
# here, mutating the tree unprevented AND undetected — while keeping the #168 ignored-artifact
# class safe (the exclude honors .gitignore, .git/info/exclude, AND the global excludesFile).
# `sort -zu` makes the combined listing order-stable. THIS WORKTREE'S HEAD is folded in too
# (issue #239): `git rev-parse HEAD` so a reasoner ref write that moves HEAD (`git commit` /
# `update-ref` of the checked-out branch) — which the index/working-tree content scan can never
# see — still changes the fingerprint, backstopping the snapshot isolation should it ever
# regress. Deliberately NOT `git for-each-ref`: on a linked worktree that lists the SHARED refs,
# so ordinary concurrent /afk-drain activity (a sibling spoke's push, a hub auto-land advancing
# main, a background fetch) would flip the fingerprint and terminally FALSE-void a correct
# answer — the concurrent-sibling false-BREACH class this repo already fights. HEAD reflects only
# THIS worktree's own branch tip, immune to sibling ref churn. UPGRADE: to also catch a ref
# write that does NOT move HEAD (a stray tag / non-checked-out branch), fingerprint the
# worktree's own per-worktree refs specifically — never the shared ref namespace.
# Empty (stable) for a non-git or missing path, so a non-worktree reasoner never trips a
# false breach.
_broker_worktree_fingerprint() {
  local wt="$1"
  [ -d "$wt" ] || return 0
  (
    cd "$wt" 2>/dev/null || exit 0
    git rev-parse --git-dir >/dev/null 2>&1 || exit 0
    {
      git ls-files -z --cached --others --exclude-standard 2>/dev/null | sort -zu |
        while IFS= read -r -d '' f; do
          printf '%s\0' "$f"
          if [ -f "$f" ]; then git hash-object "$f" 2>/dev/null || printf 'ERR'; else printf 'GONE'; fi
          printf '\0'
        done
      printf 'HEAD\0'; git rev-parse -q --verify HEAD 2>/dev/null || printf 'NONE'; printf '\0'
    } |
      shasum -a 256 2>/dev/null | awk '{print $1}'
  )
}

# _broker_worktree_unchanged <wt> <before_fingerprint> -> rc 0 when the worktree is
# byte-for-byte what it was at <before_fingerprint>, rc 1 when the reasoner mutated it.
_broker_worktree_unchanged() {
  local wt="$1" before="$2" after
  after="$(_broker_worktree_fingerprint "$wt")"
  [ "$before" = "$after" ]
}

# _broker_is_git_worktree <wt> -> rc 0 when <wt> is a real git worktree (so a NON-empty
# fingerprint is expected). Used to fail SAFE: an empty fingerprint for a git worktree
# means the fingerprint tooling (shasum/git) is missing and the read-only guard can't
# verify — which must escalate, not silently pass.
_broker_is_git_worktree() {
  [ -d "$1" ] && git -C "$1" rev-parse --git-dir >/dev/null 2>&1
}

# _broker_snapshot_worktree <wt> <dest> -> populate <dest> with a throwaway COPY of <wt>'s
# content so the reasoner can run there (cwd=<dest>) instead of the spoke's LIVE tree — real
# write isolation (#237), the "verify agent worktree isolation" prior art: even a tool that
# ignores the read-only allowlist writes into the copy, never the spoke's tree. rc 0 on a
# populated copy, rc 1 when <wt> is not a git worktree (the caller then runs in-place and the
# fingerprint void still guards). The copy carries ONLY the tracked + untracked-not-ignored
# set (the SAME set _broker_worktree_fingerprint measures) plus the .git linkage, so a per-tick
# copy never recurses the ignored heavy trees (.venv, .testmondata*, .ai-toolkit/ OTel dumps).
# `cp -R` preserves the worktree's uncommitted + untracked state — fidelity `git worktree add`
# (committed-HEAD only) can't give — so the reasoner's read git verbs still reflect real state.
# LINKED-WORKTREE GITDIR ISOLATION (#239): a spoke is always a LINKED worktree, whose `.git` is
# a gitfile still pointing at the SHARED common gitdir. Copying that pointer verbatim (`cp -R`)
# leaves git WRITE-verbs in the copy (a tool that ignores the read-only allowlist) resolving to
# the real shared refs — `git commit`/`update-ref` in the copy moved the live HEAD/branch tip
# and the content-only fingerprint never saw it. So for the gitfile case we give the copy a
# PRIVATE, self-contained gitdir (_broker_private_gitdir): the object store is shared READ-ONLY
# via `objects/info/alternates` (no per-tick object copy), while refs/HEAD/index are copied so
# read verbs still reflect real state AND every write lands in the copy's own gitdir. The
# main-checkout `.git`-DIRECTORY fast path stays `cp -R` — a self-contained dir is already
# isolated wholesale.
_broker_snapshot_worktree() {
  local wt="$1" dest="$2" f
  _broker_is_git_worktree "$wt" || return 1
  # Provide the git linkage first so read-only git verbs resolve, then the exact fingerprint
  # set — never the ignored heavy trees. A `.git` DIRECTORY copies wholesale (already isolated);
  # a linked-worktree GITFILE gets a private gitdir so writes can't reach the shared common dir.
  if [ -d "$wt/.git" ]; then
    cp -R "$wt/.git" "$dest/.git" 2>/dev/null
  elif [ -f "$wt/.git" ]; then
    # Best-effort (like the old `cp -R … 2>/dev/null`): even a partial/failed private gitdir is
    # still a PRIVATE $dest/.git — never a gitfile pointing at the shared common dir — so keeping
    # the copy preserves write isolation. A hard `return 1` here would make run_answerer fall
    # back to running the reasoner in the LIVE tree, silently dropping the very isolation this
    # provides; the reasoner's git reads just degrade if the private gitdir is incomplete.
    _broker_private_gitdir "$wt" "$dest" || true
  fi
  (
    cd "$wt" 2>/dev/null || exit 0
    git ls-files -z --cached --others --exclude-standard 2>/dev/null |
      while IFS= read -r -d '' f; do
        [ -f "$f" ] || continue
        mkdir -p "$dest/$(dirname "$f")" 2>/dev/null || true
        cp -p "$f" "$dest/$f" 2>/dev/null || true
      done
  )
  return 0
}

# _broker_private_gitdir <wt> <dest> -> build a PRIVATE, self-contained gitdir at <dest>/.git
# for a LINKED worktree <wt> (whose own `.git` is a gitfile at the shared common gitdir), so a
# git write-verb in the copy writes ONLY here — never the shared refs (#239). Objects are shared
# READ-ONLY via alternates (cheap: no per-tick copy of the object store); the shared refs +
# packed-refs are copied so read verbs reflect real state and a ref write lands locally; HEAD +
# index come from the per-worktree gitdir so `git status`/`diff` reflect the spoke's real
# uncommitted state; the real common config is copied (with worktree-specific bits neutralized)
# so any `[extensions]` carry over. rc 1 on a failure the caller treats as best-effort — a
# partial $dest/.git is still private, so write isolation holds either way.
# UPGRADE: the ref copy assumes the `files` ref backend; a `reftable`-backend repo keeps refs in
# a `reftable/` dir, not `refs/` + `packed-refs`, and would need that copied instead.
_broker_private_gitdir() {
  local wt="$1" dest="$2" common gitdir
  common="$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null)" || return 1
  gitdir="$(git -C "$wt" rev-parse --absolute-git-dir 2>/dev/null)" || return 1
  [ -n "$common" ] && [ -n "$gitdir" ] || return 1
  case "$common" in /*) ;; *) common="$wt/$common" ;; esac   # resolve a relative common dir
  mkdir -p "$dest/.git/objects/info" "$dest/.git/refs" || return 1
  printf '%s\n' "$common/objects" > "$dest/.git/objects/info/alternates"
  cp -R "$common/refs/." "$dest/.git/refs/" 2>/dev/null || true
  [ -f "$common/packed-refs" ] && cp "$common/packed-refs" "$dest/.git/packed-refs" 2>/dev/null
  cp "$gitdir/HEAD" "$dest/.git/HEAD" 2>/dev/null || return 1
  [ -f "$gitdir/index" ] && cp "$gitdir/index" "$dest/.git/index" 2>/dev/null
  # Copy the REAL common config (not a hardcoded version-0 stub) so any `[extensions]` the shared
  # repo needs — objectformat=sha256, etc. — carry over and the shared objects still parse; then
  # neutralize the worktree-specific bits so the copy is a plain non-bare worktree rooted at $dest.
  if [ -f "$common/config" ]; then
    cp "$common/config" "$dest/.git/config" 2>/dev/null
  else
    printf '[core]\n\tbare = false\n' > "$dest/.git/config"
  fi
  git -C "$dest" config core.bare false 2>/dev/null || true
  git -C "$dest" config --unset core.worktree 2>/dev/null || true
  return 0
}

# read_decisions_digest <issue> -> a compact digest of THIS spoke's prior gate outcomes,
# seeded into the reasoner for cross-gate consistency (NOT the old transcript, which
# replayed the seed in #124). Reads the automatable-decisions log (subtask D's writer),
# filtered to this issue; empty when the log is absent. Shared line format (with #155
# subtask D): <ts>\t<issue>\t<gate_type>\t<signature>\t<decision>.
read_decisions_digest() {
  local issue="$1" log
  log="$(_afk_state_dir)/decisions.log"
  [ -f "$log" ] || return 0
  awk -F'\t' -v issue="$issue" '$2 == issue { printf "- %s: %s (%s)\n", $3, $5, $4 }' "$log" 2>/dev/null || true
}

# --- automatable-decisions log + codification (issue #155, subtask D) ----------
# Every automatable PERMISSION decision (the mechanical classify_permission verdict — the
# codifiable class; a reasoner ANSWER is free text and a plan gate is a judgment call, so
# neither is logged) is recorded with a normalized SIGNATURE so recurrences of the same
# command shape collide; an on-demand codification pass then proposes deterministic rules
# for signatures that recur unanimously — graduating common gates out of the LLM in BOTH
# modes (the "scripted control plane, not LLM" payoff, generalizing #149's git-reset
# self-stage rule into a learning pipeline). Proposal-only: a human reviews before any
# rule is appended to the classifier table.

# _normalize_command_shape <command> -> the command's verb skeleton: each ;/&&/||/|
# segment reduced to "<verb>-<subcommand>" (flags/args/paths dropped), joined by '+'. So
# `git reset -q; git add tests/x.py` and `git reset HEAD; git add a.py` both normalize to
# `git-reset+git-add`. Parses tokens by hand (no word-splitting) so a glob never expands.
_normalize_command_shape() {
  local cmd="$1" norm seg out="" verb rest sub part
  # Split on the same operators classify_permission does (&& and || before the single &
  # and | so they are not pre-split); the single & must split too, or `git status & rm`
  # would sign as only `git-status`.
  norm="${cmd//&&/$'\n'}"; norm="${norm//||/$'\n'}"
  norm="${norm//&/$'\n'}"; norm="${norm//|/$'\n'}"; norm="${norm//;/$'\n'}"
  while IFS= read -r seg; do
    seg="${seg#"${seg%%[![:space:]]*}"}"; seg="${seg%"${seg##*[![:space:]]}"}"
    [ -n "$seg" ] || continue
    verb="${seg%% *}"
    rest="${seg#"$verb"}"; rest="${rest#"${rest%%[![:space:]]*}"}"
    sub="${rest%% *}"
    case "$sub" in '' | -*) sub="" ;; esac        # a flag / nothing isn't a subcommand
    part="$verb"; [ -n "$sub" ] && part="$verb-$sub"
    out="${out:+$out+}$part"
  done <<<"$norm"
  printf '%s\n' "$out"
}

# _broker_decision_signature <gate_type> <shape> -> a stable signature for the decision.
# A permission gate's shape is its command (normalized to the verb skeleton); other gate
# types sign as the gate type itself (a plan gate is a judgment call, not codifiable).
_broker_decision_signature() {
  local gate_type="$1" shape="$2"
  case "$gate_type" in
    permission) _normalize_command_shape "$shape" ;;
    *) printf '%s\n' "$gate_type" ;;
  esac
}

# log_decision <issue> <gate_type> <shape> <decision> -> append one automatable-decisions
# record: <ts>\t<issue>\t<gate_type>\t<signature>\t<decision>. Exactly the format
# read_decisions_digest (subtask B) consumes. Best-effort; never aborts the caller.
log_decision() {
  local issue="$1" gate_type="$2" shape="$3" decision="$4" sig log
  sig="$(_broker_decision_signature "$gate_type" "$shape")"
  log="$(_afk_state_dir)/decisions.log"
  mkdir -p "$(dirname "$log")" 2>/dev/null || true
  printf '%s\t%s\t%s\t%s\t%s\n' "$(afk_now)" "$issue" "$gate_type" "$sig" "$decision" \
    >>"$log" 2>/dev/null || true
}

# codify_decisions [min_count] -> propose a deterministic rule for every signature that
# recurs at least <min_count> times (default 2) with a UNANIMOUS decision. Output is a
# PROPOSAL a human reviews before it is codified into classify_permission — never an
# auto-applied rule. A single-occurrence or a conflicting signature proposes nothing. The
# signature drops flags/args, so the proposal carries a "verify destructive flag variants"
# caveat: the human must confirm the shape is safe across the flags classify_permission
# distinguishes before codifying. Malformed lines (missing signature/decision) are skipped.
codify_decisions() {
  local min="${1:-2}" log
  log="$(_afk_state_dir)/decisions.log"
  [ -f "$log" ] || return 0
  awk -F'\t' -v min="$min" '
    $4 != "" && $5 != "" {
      sig=$4; dec=$5; count[sig]++
      if (!(sig in decision)) decision[sig]=dec
      else if (decision[sig]!=dec) conflict[sig]=1 }
    END {
      for (s in count)
        if (count[s] >= min && !(s in conflict))
          printf "RULE: %s -> %s (%d occurrences, unanimous; verify destructive flag variants)\n", s, decision[s], count[s]
    }' "$log" 2>/dev/null | sort || true
}

# --- decision journal + warn-and-continue (issue #241) ------------------------
# The /afk answerer ALWAYS answers: every former terminal stop site (escalate-blocked, reap,
# ceiling, void, inject-failure, dispatch/land/auth halts) now TAKES the best action, WARNS
# loudly to four surfaces (drain log + hub-notify ping + --status + this decision journal),
# and parks the spoke LAST on the warned-retry backoff — never abandoned. The journal is the
# post-adjust surface: the operator reads it in the morning and reverses whatever was wrong.

# _broker_journal_file -> the per-run decision journal (one JSON line per taken decision).
_broker_journal_file() { printf '%s\n' "$(_afk_state_dir)/decision-journal.jsonl"; }

# _broker_json_escape <s> -> escape a value for a JSON string literal. A decision/reason can
# be built from captured tool output (git/gh/build lines carry \r, \t, and other C0 controls),
# and JSON forbids raw control characters in a string — so escape \ and ", space out the
# common whitespace for readability, then DROP any remaining C0 byte so the journal line stays
# valid JSONL a strict parser accepts. LC_ALL=C makes the byte range literal on this non-C host.
_broker_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"   # backslashes first, else the quote-escapes below get doubled
  s="${s//\"/\\\"}"
  s="${s//$'\t'/ }"; s="${s//$'\n'/ }"; s="${s//$'\r'/ }"   # keep the record one physical line
  printf '%s' "$s" | LC_ALL=C tr -d '\000-\037'
}

# _broker_journal_line <issue> <park_kind> <decision> <reversibility> [reasoning_ref] -> append
# ONE structured JSONL record (ts, issue, park, decision, reversibility, reasoning_ref) to the
# per-run journal FILE — and nothing else. This is the cheap, no-noise audit surface: a routine
# successful answer journals here WITHOUT a GitHub comment (per-answer comments would be spam).
# Best-effort; never aborts the caller.
_broker_journal_line() {
  local issue="$1" park="$2" decision="$3" rev="${4:-unknown}" ref="${5:-}" f
  f="$(_broker_journal_file)"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '{"ts":%s,"issue":"%s","park":"%s","decision":"%s","reversibility":"%s","reasoning_ref":"%s"}\n' \
    "$(afk_now)" "$(_broker_json_escape "$issue")" "$(_broker_json_escape "$park")" \
    "$(_broker_json_escape "$decision")" "$(_broker_json_escape "$rev")" \
    "$(_broker_json_escape "$ref")" >>"$f" 2>/dev/null || true
}

# broker_journal_decision <issue> <park_kind> <decision> <reversibility> [reasoning_ref] ->
# journal the record (file) AND post a best-effort GitHub issue comment, so the morning review
# reads either surface. Used for NOTEWORTHY decisions (a warned/parked call, a WARN-flagged or
# non-reversible answer) — a routine successful answer uses _broker_journal_line (file only).
# reversibility is one of reversible|outward|scope|irreversible|unknown. Best-effort; never aborts.
broker_journal_decision() {
  local issue="$1" park="$2" decision="$3" rev="${4:-unknown}"
  _broker_journal_line "$@"
  _broker_journal_gh_comment "$issue" "$park" "$decision" "$rev"
  return 0
}

# _broker_journal_gh_comment <issue> <park> <decision> <rev> -> best-effort issue comment
# recording the taken decision (#241 §10). Opt-out via AFK_JOURNAL_GH_COMMENT=0; no-op when
# gh is absent. Never aborts.
_broker_journal_gh_comment() {
  [ "${AFK_JOURNAL_GH_COMMENT:-1}" = 0 ] && return 0
  command -v gh >/dev/null 2>&1 || return 0
  local issue="$1" park="$2" decision="$3" rev="$4" body
  # Wrap the decision in backticks: a decision containing `#123` or `@name` would otherwise
  # render as a cross-issue link / user mention on GitHub, back-referencing unrelated issues.
  body="AFK auto-decision [$rev] on the $park park: \`$decision\` (review and post-adjust if wrong)"
  # Route through the TIME-BOUNDED runner so a hung gh (a black-hole network) can never
  # freeze the servicing tick — this is on the synchronous answer path. _wt_gh_run bounds
  # gh at AI_TOOLKIT_GH_TIMEOUT and returns its real rc (which we discard). Fall back to a
  # raw best-effort gh only when worktree-lib.sh did not source (the helper is undefined).
  if command -v _wt_gh_run >/dev/null 2>&1; then
    _wt_gh_run issue comment "$issue" --body "$body" || true
  else
    gh issue comment "$issue" --body "$body" >/dev/null 2>&1 || true
  fi
  return 0
}

# _broker_warned_record <issue> -> the durable, human-facing warned record: "<ts>\t<reason>".
# --status surfaces it and hub-notify pings on it (re-fired on an interval, unlike the
# once-deduped blocked ping). Distinct from blocked-<issue>.txt so the two states never blur.
_broker_warned_record() { printf '%s\n' "$(_afk_state_dir)/warned-$1.txt"; }

# broker_warn <issue> <reason> -> the loud, repeatable WARNING surface: log a WARNING line and
# overwrite the durable warned record (latest warning wins). Best-effort; never aborts.
broker_warn() {
  local issue="$1" reason="$2" f
  reason="${reason//$'\n'/ }"; reason="${reason//$'\r'/ }"   # keep the record one line (hub-notify cut -f2-)
  log "  WARNING: #$issue $reason"
  f="$(_broker_warned_record "$issue")"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\n' "$(afk_now)" "$reason" >"$f" 2>/dev/null || true
  return 0
}

# _afk_warned_state_file <issue> -> the backoff bookkeeping: "<attempt>\t<next_retry_epoch>".
_afk_warned_state_file() { printf '%s\n' "$(_afk_state_dir)/warned-state-$1"; }

# _afk_warned_arm <issue> -> advance the warned-retry backoff: read the prior attempt count
# (0 if none), schedule the next retry at now + min(BASE * 2^attempt, CAP), and persist
# "<attempt+1>\t<next>". Exponential so a standing failure is retried ever more rarely.
_afk_warned_arm() {
  local issue="$1" f base cap attempt=0 delay now i=0
  base="${AFK_WARN_BACKOFF_BASE:-60}"; case "$base" in '' | *[!0-9]*) base=60 ;; esac
  cap="${AFK_WARN_BACKOFF_CAP:-1800}"; case "$cap" in '' | *[!0-9]*) cap=1800 ;; esac
  f="$(_afk_warned_state_file "$issue")"
  if [ -f "$f" ]; then IFS=$'\t' read -r attempt _ <"$f" 2>/dev/null || true; fi
  case "$attempt" in '' | *[!0-9]*) attempt=0 ;; esac
  delay="$base"
  while [ "$i" -lt "$attempt" ] && [ "$delay" -lt "$cap" ]; do delay=$(( delay * 2 )); i=$(( i + 1 )); done
  [ "$delay" -gt "$cap" ] && delay="$cap"
  now="$(afk_now)"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\n' "$(( attempt + 1 ))" "$(( now + delay ))" >"$f" 2>/dev/null || true
}

# _afk_warned_due <issue> [now] -> rc 0 when the spoke is due for a retry (never warned, or the
# backoff window has elapsed), rc 1 when still inside the backoff (parked LAST this tick).
_afk_warned_due() {
  local issue="$1" now="${2:-$(afk_now)}" f next=""
  f="$(_afk_warned_state_file "$issue")"
  [ -f "$f" ] || return 0
  IFS=$'\t' read -r _ next <"$f" 2>/dev/null || true
  case "$next" in '' | *[!0-9]*) return 0 ;; esac
  [ "$now" -ge "$next" ]
}

# _afk_clear_warned <issue> -> drop one spoke's warned record + backoff (called on genuine
# progress: a tip advance or a fresh marker means the warned state is stale).
_afk_clear_warned() {
  rm -f "$(_afk_warned_state_file "$1")" "$(_broker_warned_record "$1")" 2>/dev/null || true
}
# _clear_warned_records -> drop every warned record + backoff for a freshly-armed window.
_clear_warned_records() {
  local dir; dir="$(_afk_state_dir)"
  rm -f "$dir"/warned-*.txt "$dir"/warned-state-* 2>/dev/null || true
}

# broker_warn_continue <wt> <issue> <park_kind> <decision> <reversibility> -> the #241
# replacement for _escalate_blocked at a converted stop site: warn loudly, journal the taken
# decision, advance the backoff, emit a warn span, and RETURN — the spoke stays in rotation
# (no blocked tag, no pane kill). It is retried on the backoff until it makes progress.
broker_warn_continue() {
  local wt="$1" issue="$2" park="$3" decision="$4" rev="${5:-unknown}"
  broker_warn "$issue" "$decision"
  broker_journal_decision "$issue" "$park" "$decision" "$rev"
  _afk_warned_arm "$issue"
  afk_emit_decision "$wt" warn
  return 0
}

# _rule_file -> the afk-answering rule path, across both layouts; empty if unfound.
_rule_file() {
  local cand
  for cand in \
    "${AFK_RULE_FILE:-}" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/.claude/rules/afk-answering.md" \
    "$SCRIPT_DIR/../../../../shared/rules/afk-answering.md" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/shared/rules/afk-answering.md"; do
    [ -n "$cand" ] && [ -f "$cand" ] && { printf '%s\n' "$cand"; return 0; }
  done
  return 1
}

# build_answerer_prompt <issue> <question> -> the full prompt for the reasoner: the
# governing rule, the issue contract, the read-only-worktree posture + evidence contract,
# a decisions-digest of this spoke's prior gate outcomes, and the parked prompt.
# Self-contained so the headless reasoner needs no project context loaded. The reasoner's
# cwd is the #237 snapshot COPY (created in run_answerer before this is called), so the
# posture points at "a throwaway copy (your cwd)" and — deliberately (#239) — never
# discloses the live worktree's absolute path, which used to invite an absolute-path write
# into the real tree.
# _default_answerer_policy -> the built-in fallback policy shipped when the afk-answering rule
# file is absent. #241: the reasoner ALWAYS answers — it never escalates-and-parks. It is kept
# in lockstep with shared/rules/afk-answering.md by a binding test, so both surfaces retire the
# ESCALATE output token and both instruct the ANSWER + REVERSIBILITY lines.
_default_answerer_policy() {
  cat <<'POLICY'
Answer in the interest of the issue contract and repo conventions; prefer the spoke's own
recommended option. You ALWAYS answer — you never escalate and park the spoke for a human.
For an irreversible, outward-facing, or scope-changing ask, choose the REVERSIBLE, in-scope
alternative when one exists (that IS the answer — e.g. do not force-push; rebase onto a new
branch instead; deny a destructive command and tell the spoke the reversible path); only when
no reversible alternative exists do you decide on the merits. Precede your decision with a
'REVERSIBILITY: reversible|outward|scope|irreversible' line naming the class, and add a
'WARN: <what the human should double-check>' line whenever you take a critical, irreversible,
outward-facing, or scope-changing decision so it is loudly recorded for morning post-review.
End with exactly one final line: 'ANSWER: <reply>'.
POLICY
}

build_answerer_prompt() {
  local issue="$1" question="$2" rule body digest
  rule="$(_rule_file)" && rule="$(cat "$rule")" \
    || rule="$(_default_answerer_policy)"
  body="$(gh issue view "$issue" --json title,body -q '.title + "\n\n" + .body' 2>/dev/null || echo "(issue #$issue body unavailable)")"
  digest="$(read_decisions_digest "$issue")"
  cat <<EOF
$rule

## Issue contract (#$issue)

$body

## Read-only worktree access

You have READ-ONLY access to a throwaway COPY of the spoke's worktree (your cwd). Use your
read/search tools to verify the decision against the code as it ACTUALLY is — confirm a command
touches only the spoke's own files, that a posted plan matches real state, and so on.
You must NOT edit, stage, commit, or push anything: the tree is read-only and any write
voids your answer. When you auto-answer, cite the worktree EVIDENCE you checked on an
'EVIDENCE:' line before your final decision line.

## Prior gate decisions for this spoke (decisions-digest)

${digest:-(none recorded yet)}

## The spoke's parked prompt

$question

Decide per the policy above — you ALWAYS answer, never escalate-and-park. Precede your
decision with a 'REVERSIBILITY: reversible|outward|scope|irreversible' line, and a
'WARN: <what to double-check>' line for any critical, irreversible, outward-facing, or
scope-changing call. End with exactly one final line: 'ANSWER: <reply>'.
EOF
}

# --- bounding the reasoner (issue #171, subtask 1) ----------------------------
# An untimed headless `claude` can hang the whole tick; every reasoner run is bounded so a
# wedged answerer never freezes the supervisor. Expiry yields no decision line, so the gate
# fails SAFE to escalate (blocked/<issue>) — the existing no-decision fail-safe.

# _afk_answerer_timeout -> the reasoner's wall-clock budget in seconds. AFK_ANSWERER_TIMEOUT
# tunes it (default 900); a non-numeric OR non-positive override (0 disables the bound in
# both `timeout` and perl `alarm`) falls back to the default, so the cap is never silently
# lifted (#171 review).
_afk_answerer_timeout() {
  local s="${AFK_ANSWERER_TIMEOUT:-900}"
  case "$s" in '' | *[!0-9]* ) s=900 ;; esac
  [ "$s" -lt 1 ] && s=900
  printf '%s\n' "$s"
}

# _broker_run_bounded <secs> <cmd...> -> run <cmd...> (prompt on this function's stdin) under
# a <secs> wall-clock cap and return its exit code (nonzero on expiry). PREFERS hub-afk's
# shared _afk_with_timeout when the supervisor sourced it (issue #170): it tree-kills a
# wedged grandchild via _afk_kill_tree, so a hung `claude` can't keep run_answerer's capture
# pipe open and re-hang the tick. Reused (not re-implemented) via a runtime existence check —
# the same seam gate-broker uses for respawn_wedged_spoke — so the bound has one owner. Falls
# back to a self-contained bound only for a STANDALONE / attended broker without hub-afk (the
# tests): coreutils timeout/gtimeout, then a perl(alarm) wrapper (SIGALRM survives exec and
# terminates a runaway), then best-effort unbounded.
_broker_run_bounded() {
  local secs="$1"; shift
  if command -v _afk_with_timeout >/dev/null 2>&1; then _afk_with_timeout "$secs" "$@"; return; fi
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; return; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout "$secs" "$@"; return; fi
  if command -v perl >/dev/null 2>&1; then
    # UPGRADE: unlike _afk_with_timeout this does not reap a wedged grandchild — only reached
    # in a hub-less standalone/attended context where a long-lived `claude` grandchild is not
    # expected; production routes through _afk_with_timeout above.
    perl -e 'alarm shift @ARGV; exec @ARGV or exit 127' "$secs" "$@"; return
  fi
  "$@"   # no bounding tool available — best-effort unbounded
}

# run_answerer <issue> <question> [wt] -> the reasoner's raw output (stdout AND stderr),
# and its exit status as the function's return code. The reasoner is a headless `claude
# -p` (overridable via AFK_ANSWERER_CMD for tests), run with a thinking budget and a
# READ-ONLY tool allowlist; the prompt is passed on stdin so a long contract never hits
# argv limits. When <wt> is a directory it becomes the reasoner's cwd, so its read-only
# tools verify against the spoke's live state (the mutation guard in broker_service_gate
# is what makes that safe). The run is bounded by AFK_ANSWERER_TIMEOUT (_broker_run_bounded)
# so a hung `claude` never freezes the tick; expiry reads as no decision → escalate.
# stderr is folded into the captured stream (NOT discarded)
# because the CLI prints credential failures there and exits nonzero — the auth-failure
# detector needs both the message and the exit code. parse_decision is line-anchored, so
# interleaved stderr noise never pollutes a decision.
#
# --no-session-persistence stays belt-and-suspenders for #164. The original collision: the
# reasoner ran with cwd=<wt>, so a persisted transcript landed in the SAME
# ~/.claude/projects/<munged-wt>/ dir as the spoke's own, shadowing it — `_spoke_jsonl` picks
# the newest jsonl there, so every `_still_parked_same` check saw the transcript "move" and
# dropped the answer as stale, stranding the spoke. The #237 write-isolation snapshot already
# removes that collision at the root: the reasoner's cwd is now a mktemp copy, so any persisted
# transcript maps to the copy's OWN munged dir — disjoint from <wt>'s. We keep the flag anyway
# so no throwaway transcript is written for the snapshot path at all. It does NOT touch
# CLAUDE_CONFIG_DIR, so keychain credentials/auth are unaffected.
# UPGRADE: if a deployed `claude` lacks --no-session-persistence it exits nonzero with no
# decision, so the gate fails SAFE (escalates to blocked/<issue>) rather than stranding —
# but auto-answering silently stops; drop the flag / switch to filtering the reasoner's
# jsonl out of _spoke_jsonl if the installed CLI ever loses it.
run_answerer() {
  local issue="$1" question="$2" wt="${3:-}"
  local tools; tools="$(reasoner_allowed_tools)"
  # #247 option (c): `--output-format stream-json --verbose` streams every reasoner tool_use
  # (name + input) onto the captured stdout so _reasoner_wrote_live_tree can AUDIT what the
  # reasoner actually did (the void's attribution signal). The stream is NOT the final answer:
  # every CALLER must run the raw output through _normalize_answerer_output before any text parse
  # (parse_decision / parse_decision_field / is_auth_failure) — the audit is the ONE consumer that
  # reads the raw stream. `--verbose` is required for stream-json under `-p`; a deployed CLI that
  # lacks the format exits nonzero → the audit reads no stream (rc 2) and the void degrades to the
  # #244 activity fallback — safe, never stranding.
  local cmd="${AFK_ANSWERER_CMD:-claude -p --no-session-persistence --output-format stream-json --verbose --model claude-opus-4-8 --allowedTools '$tools'}"
  local secs; secs="$(_afk_answerer_timeout)"
  # Write isolation (#237): run the reasoner against a throwaway COPY of the worktree, not the
  # spoke's LIVE tree — so even a tool that ignores the read-only allowlist writes into the
  # copy. The reasoner's cwd moves to the snapshot; broker_service_gate still fingerprints the
  # real $wt, now a should-never-fire backstop. On any copy failure (no mktemp, non-git tree),
  # fall back to running in-place: the fingerprint void remains the guard. The snapshot is
  # built BEFORE the prompt (#239) so the posture can point cwd at the copy and never disclose $wt.
  local snap="" run_dir="$wt"
  if [ -n "$wt" ] && [ -d "$wt" ]; then
    snap="$(mktemp -d 2>/dev/null)" || snap=""
    if [ -n "$snap" ] && _broker_snapshot_worktree "$wt" "$snap"; then
      run_dir="$snap"
    elif [ -n "$snap" ]; then
      rm -rf "$snap" 2>/dev/null || true; snap=""
    fi
  fi
  local prompt; prompt="$(build_answerer_prompt "$issue" "$question")"
  # Deliver the prompt via a temp file the wrapped command re-opens with `exec <`, NOT only
  # the here-string: the bound (_afk_with_timeout's portable fallback) BACKGROUNDS the
  # command, and POSIX assigns a backgrounded job's stdin to /dev/null — a plain here-string
  # would be lost, starving the reasoner of its prompt. `exec <file` reopens stdin inside the
  # backgrounded shell, so the prompt survives every bound path. The here-string stays as a
  # fallback for when mktemp is unavailable (the foreground timeout/perl paths keep stdin).
  local pf rc; pf="$(mktemp 2>/dev/null)" || pf=""
  [ -n "$pf" ] && { printf '%s' "$prompt" > "$pf"; cmd="exec <'$pf'; $cmd"; }
  # _broker_run_bounded caps the reasoner (#171): a hung `claude` never freezes the tick.
  # stderr is folded in (2>&1) so the auth-failure detector still sees credential messages.
  (
    [ -n "$run_dir" ] && [ -d "$run_dir" ] && cd "$run_dir"
    CLAUDE_EFFORT="$AFK_ANSWERER_EFFORT" _broker_run_bounded "$secs" bash -c "$cmd" <<<"$prompt" 2>&1
  )
  rc=$?
  [ -n "$pf" ] && rm -f "$pf"
  [ -n "$snap" ] && rm -rf "$snap" 2>/dev/null || true
  return "$rc"
}

# _normalize_answerer_output <raw> -> the reasoner's FINAL TEXT, extracted from a
# `--output-format stream-json` event stream (#247) so the line-anchored DECISION parsers
# (parse_decision / parse_decision_field) see the ANSWER / REVERSIBILITY / WARN lines they
# expect — NOT buried inside JSON, where they would silently read as empty and drop the #241
# reversibility class + WARN note. (is_auth_failure is fed the RAW stream instead, so an auth
# signature carried in a dropped event — a system/error line — is never missed; see the call
# sites.) The extraction:
#   - the final `type:"result"` event's `result` field (the consolidated answer) wins; a
#     missing/empty result falls back to concatenated assistant `text` blocks (real claude emits
#     BOTH, so the answer survives a drift in either shape);
#   - NON-JSON lines pass through (a plain-text answerer stub is entirely non-JSON, so it is
#     surfaced whole).
# The raw stream is delivered via a temp FILE (path in env), never argv/env directly, so a verbose
# stream echoing large tool_result payloads never trips ARG_MAX. A pure plain-text input (every
# #244 answerer stub) has no JSON events, so its content passes through — the DECISION lines are
# preserved, though the python path normalizes whitespace (drops blank lines, adds a trailing
# newline); only the no-python3 branch is byte-identical. No python3 ⇒ byte-for-byte passthrough
# (the degraded env keeps plain-text answerers working; the void degrades to the #244 fallback).
_normalize_answerer_output() {
  command -v python3 >/dev/null 2>&1 || { printf '%s' "$1"; return 0; }
  local rawfile; rawfile="$(mktemp 2>/dev/null)" || { printf '%s' "$1"; return 0; }
  printf '%s' "$1" > "$rawfile"
  _AFK_RAWFILE="$rawfile" python3 2>/dev/null <<'PYEOF' || printf '%s' "$1"
import json, os

result_text = None
assistant_texts = []
passthrough = []
with open(os.environ["_AFK_RAWFILE"], encoding="utf-8", errors="replace") as fh:
    for raw_line in fh:
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            passthrough.append(raw_line.rstrip("\n"))  # plain text (a stub) — surfaced whole
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "result":
            r = obj.get("result")
            if isinstance(r, str) and r.strip():
                result_text = r  # the LAST result event's consolidated text wins
        elif kind == "assistant":
            content = (obj.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and (block.get("text") or "").strip():
                        assistant_texts.append(block["text"])
        # other event types (system/user/tool_use noise) carry no final text — skipped.

out = []
if result_text is not None:
    out.append(result_text)
elif assistant_texts:
    out.append("\n".join(assistant_texts))
out.extend(passthrough)
print("\n".join(out))
PYEOF
  rm -f "$rawfile" 2>/dev/null || true
}

# parse_decision <raw-answerer-output> -> "ANSWER\t<text>" or "ESCALATE\t<reason>" on
# stdout, or empty when the answerer emitted no decision line. The LAST matching line
# wins (the answerer reasons first, then concludes). Decisions are SINGLE-LINE by
# construction (the grep is line-anchored) — inject_answer and _afk_continue_command
# rely on this; supporting multi-line answers would re-trigger the bracketed-paste
# hazard (#123/#124) and the quoting hazard on the respawn command line.
parse_decision() {
  local line kind rest
  line="$(printf '%s\n' "$1" | grep -E '^(ANSWER|ESCALATE):' | tail -1)"
  [ -n "$line" ] || return 0
  kind="${line%%:*}"
  rest="${line#*:}"
  rest="${rest#"${rest%%[![:space:]]*}"}"          # ltrim
  printf '%s\t%s\n' "$kind" "$rest"
}

# parse_decision_field <raw-answerer-output> <KEYWORD> -> the trimmed value of the LAST
# '<KEYWORD>: <value>' line (empty when absent). #241 reads the reasoner's 'REVERSIBILITY:'
# class and 'WARN:' note off the same single-line convention as the ANSWER line, so a taken
# decision carries its reversibility class + human-review flag into the decision journal.
# <KEYWORD> must be a metacharacter-free literal (callers pass REVERSIBILITY / WARN); it is
# interpolated into an ERE. The value is both l- and r-trimmed so a class enum compares exact.
parse_decision_field() {
  local raw="$1" key="$2" line rest
  line="$(printf '%s\n' "$raw" | grep -E "^${key}:" | tail -1)"
  [ -n "$line" ] || return 0
  rest="${line#*:}"
  rest="${rest#"${rest%%[![:space:]]*}"}"          # ltrim
  rest="${rest%"${rest##*[![:space:]]}"}"          # rtrim
  printf '%s\n' "$rest"
}

# is_auth_failure <raw-answerer-output> -> true (rc 0) when the text carries a Claude /
# Anthropic auth-failure signature (dead credentials / token could not refresh). Matched
# case-insensitively against the known wordings. The CALLER additionally gates on the
# answerer having EXITED NONZERO (decide_and_act) — auth discussion in a healthy answer
# exits 0 and is never treated as a failure — so this predicate can favor recall without
# a false positive halting the whole drain. The /login signature is still anchored to the
# CLI's "run [`claude `]/login" phrasing so prose like "run the /login migration" misses.
is_auth_failure() {
  printf '%s' "$1" | grep -Eqi \
    'authentication_error|invalid (x-)?api[ -]?key|invalid bearer token|oauth (token|authentication)|run `?(claude )?/login|401|unauthorized|credit balance is too low'
}

# --- the permission classifier (issue #149) -----------------------------------
# A spoke under /afk stalls on Claude Code PERMISSION dialogs (distinct from the
# question/gate parks the answerer handles): the FIRST RED-commit selective stage
# `git reset -q; git add <own file>` prompts and, unanswered, the spoke idles until
# reaped. classify_permission decides such a dialog the way a human would — but by a
# fixed rules table, not the reasoning answerer, since the decision is mechanical and
# must be conservative. It is the unit-tested heart of the supervisor's permission
# handling (the tmux detection + injection that drives it lives in decide_and_act).

# _pytest_seg_scoped <segment> -> rc 0 when a `pytest` / `python -m pytest` segment carries a
# genuine SCOPING argument (a path or node-id), rc 1 otherwise. A bare `pytest`, one carrying
# only flags (`pytest -q`, `pytest -x`), OR one whose only non-flag token is a value belonging
# to a selection option (`pytest -k foo`, `pytest -m slow`, `pytest -p plugin`) still collects
# the WHOLE suite, whose escaped tests rewrite real refs (#135) — the full-suite ref-rewind
# hazard (#203). A separate-token value of such an option is therefore SKIPPED, not counted as
# a path. Tokens are walked by hand (no word-splitting) so a glob argument never expands.
_pytest_seg_scoped() {
  local seg="$1" rest tok skip_val=0
  case "$seg" in
    'python -m pytest'*)          rest="${seg#python -m pytest}" ;;
    'python3 -m pytest'*)         rest="${seg#python3 -m pytest}" ;;
    '.venv/bin/python -m pytest'*) rest="${seg#.venv/bin/python -m pytest}" ;;
    'pytest'*)                    rest="${seg#pytest}" ;;
    *) return 1 ;;
  esac
  while [ -n "$rest" ]; do
    rest="${rest#"${rest%%[![:space:]]*}"}"          # ltrim
    [ -n "$rest" ] || break
    tok="${rest%%[[:space:]]*}"                       # first token
    rest="${rest#"$tok"}"
    if [ "$skip_val" -eq 1 ]; then skip_val=0; continue; fi   # a prior option's value token
    case "$tok" in
      # separate-token value options: the NEXT token is a value, not a scoping path.
      -k | -m | -p | -c | -o | -W | -n | -r | --rootdir | --deselect | --ignore \
        | --ignore-glob | --confcutdir | --override-ini) skip_val=1 ;;
      -*) ;;                                          # any other flag (incl. --opt=value)
      *) return 0 ;;                                  # a genuine non-flag token = a path/node-id
    esac
  done
  return 1
}

# --- benign in-worktree mutation lane (issue #203, finding 4) ------------------
# A confirmation dialog on a COMPOUND command (cd into the worktree, mv a stashed file from
# the scratchpad, chmod +x it, stash pop, targeted pytest) used to classify as one opaque
# "risky" string and escalate, wedging the whole drain. These helpers let classify_permission
# APPROVE segments whose writes are confined to the spoke's OWN worktree or its session
# scratchpad — the spoke already has unrestricted Edit/Write there, so a chmod on its own new
# hook script carries no additional risk. .git/ internals and secret-like paths stay denied.

# _broker_path_physically_in <abs> <wt> <tasks> -> rc 0 when <abs>, with ALL symlinks
# resolved, is physically under the worktree or the tasks root and NOT under <wt>/.git; rc 1
# otherwise. Closes the symlink-indirection escape a textual check cannot see: a logically
# in-tree path (e.g. `.venv/bin/python3`, a symlink worktree-new.sh points out of tree) can
# physically resolve anywhere. os.path.realpath resolves the existing prefix — following a
# final symlink FILE (the overwrite case) — and appends any not-yet-created tail, so it works
# for create targets too. Fails CLOSED (rc 1) without python3: an unverifiable mutation path
# is denied, not trusted (a false deny escalates — the safe direction).
_broker_path_physically_in() {
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_ABS="$1" _AFK_WT="$2" _AFK_TASKS="$3" python3 2>/dev/null <<'PYEOF'
import os, sys

abs_ = os.path.realpath(os.environ["_AFK_ABS"])
wt = os.path.realpath(os.environ["_AFK_WT"])
tasks = os.path.realpath(os.environ["_AFK_TASKS"])

def under(p, root):
    return p == root or p.startswith(root.rstrip("/") + "/")

if not (under(abs_, wt) or under(abs_, tasks)):
    sys.exit(1)
# Reject any `.git` path component, case-INSENSITIVELY: macOS's default filesystem is
# case-insensitive, so `.GIT` addresses the same dir as `.git` and a literal-`.git` guard
# alone misses it; this also covers a nested repo's `.git` anywhere under the roots.
if any(part.lower() == ".git" for part in abs_.split(os.sep)):
    sys.exit(1)
sys.exit(0)
PYEOF
}

# _broker_resolve_in_roots <path> <cwd> <wt> <slug> <tasks> -> print <path>'s absolute form
# (resolved against <cwd>) IF it lies under the worktree <wt> or the spoke's session
# scratchpad (<tasks>/claude-*/<slug>/…), and NOT under <wt>/.git; else rc 1. TWO layers:
# a textual containment check (fast, and the only one that can bound the scratchpad glob),
# THEN a physical symlink-resolving check (_broker_path_physically_in) — both must pass.
# Any token the shell would EXPAND to a different path (traversal, variable/command
# substitution, tilde, brace or glob metacharacters) is rejected outright: a textual
# resolver cannot see through those, and a false deny escalates — the safe direction.
_broker_resolve_in_roots() {
  local p="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" abs
  # Reject any token the shell rewrites at execution to a path the textual/realpath checks
  # cannot see: traversal (`..`), variable/command substitution (`$`, backtick), tilde, brace
  # and glob metacharacters, quoting/escaping (`"` `'` `\`), and redirection (`>` `<`). Two
  # are load-bearing beyond the obvious: a leading quote/backslash (`rm "/etc/x"`) makes the
  # `/*` absolute test below miss it so it is joined onto the worktree cwd as if relative, and
  # a redirection (`cd foo>/etc/x`) hides an out-of-tree target the shell splits off — this
  # resolver is the cd-handler's ONLY guard, so it must reject `>`/`<` that _permission_seg_safe
  # rejects on the mutation path. realpath treats all these as ordinary chars, so an escaped
  # target would pass containment yet the shell mutates the real path. A false deny escalates.
  case "$p" in
    *'..'* | *'$'* | *'`'* | '~'* | *'{'* | *'}'* | *'*'* | *'?'* | *'['* | *']'* \
      | *'"'* | *"'"* | *'\'* | *'>'* | *'<'*) return 1 ;;
  esac
  case "$p" in /*) abs="$p" ;; *) abs="$cwd/$p" ;; esac
  # Collapse `/./` and duplicate slashes textually (no glob, no fs touch). The replacement
  # is `$sl` (a bare slash held in a var), NOT a literal `\/`: bash keeps the backslash in a
  # `${var//pat/repl}` replacement string, so `\/` would corrupt the path (`/x/./y`→`/x\/y`).
  local sl=/
  while case "$abs" in */./* | *//*) true ;; *) false ;; esac; do
    abs="${abs//\/.\//$sl}"; abs="${abs//\/\//$sl}"
  done
  abs="${abs%/.}"                                  # a trailing `/.` (bare `.` target) → the dir
  abs="${abs%/}"; [ -n "$abs" ] || abs="/"
  case "$abs" in "$wt"/.git | "$wt"/.git/*) return 1 ;; esac      # never .git internals (textual)
  case "$abs" in
    "$wt" | "$wt"/*) ;;                                           # under the worktree
    "$tasks"/claude-*/"$slug"/*) ;;                               # under the scratchpad
    *) return 1 ;;
  esac
  _broker_path_physically_in "$abs" "$wt" "$tasks" || return 1   # symlink-resolved containment
  printf '%s\n' "$abs"
}

# _broker_seg_secretlike <token> -> rc 0 when a path token looks like a secret (a mutation of
# it is never in the benign lane, even inside the worktree). Mirrors the repo's own secret
# .gitignore classes (.env, *.pem) plus the common credential filenames. Matched case-
# INSENSITIVELY (via tr — bash 3.2 lacks `${v,,}`): macOS's default filesystem is case-
# insensitive, so `.ENV` addresses the same inode as `.env` and must not slip the guard
# (mirroring the case-folded `.git` component check in _broker_path_physically_in).
_broker_seg_secretlike() {
  local base lower path_lower
  base="${1##*/}"
  lower="$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')"
  case "$lower" in
    .env | .env.* | *.pem | *.key | *.p12 | id_rsa | id_dsa | id_ecdsa | id_ed25519 \
      | .netrc | credentials | .npmrc | .pypirc) return 0 ;;
  esac
  path_lower="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  # Credential STORES by directory (issue #261 review): the SSH/AWS/GPG dirs plus the common
  # cloud/registry credential dirs, so a `cat ~/.kube/config` / `~/.docker/config.json` /
  # gcloud / gh-token read is recognized as secret-like and the deny-wall's credential lane
  # stops it. UPGRADE: a novel cred store not listed here falls to the Tier-3 judge, not a
  # static deny -- extend this set as the #261 journal surfaces new ones.
  case "$path_lower" in
    */.ssh/* | */.aws/* | */.gnupg/* | */.kube/* | */.docker/* \
      | */.config/gcloud/* | */.config/gh/*) return 0 ;;
  esac
  return 1
}

# _permission_seg_mutation_ok <segment> <cwd> <wt> <slug> <tasks> -> rc 0 when a mutating
# segment (mv/cp/rm/mkdir/chmod) touches ONLY paths under the worktree or the spoke's
# scratchpad, none secret-like, none the worktree root itself. Tokens are walked by hand (no
# word-splitting) so a glob argument never expands. Inert (rc 1) without a worktree context.
_permission_seg_mutation_ok() {
  local seg="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" verb rest tok resolved saw_path=0 mode_pending=0
  [ -n "$wt" ] || return 1
  verb="${seg%% *}"
  case "$verb" in
    mv | cp | rm | mkdir | chmod) ;;
    *) return 1 ;;
  esac
  [ "$verb" = chmod ] && mode_pending=1        # chmod's first non-flag token is the mode
  rest="${seg#"$verb"}"
  while [ -n "$rest" ]; do
    rest="${rest#"${rest%%[![:space:]]*}"}"    # ltrim
    [ -n "$rest" ] || break
    tok="${rest%%[[:space:]]*}"                 # first token
    rest="${rest#"$tok"}"
    # `-t DIR` / `-tDIR` / `--target-directory[=DIR]` (GNU mv/cp) hide the DESTINATION inside
    # a flag; the glued/`=`-form would be skipped as a flag and its out-of-tree target never
    # checked. Deny the whole segment when one appears — a false deny escalates (BSD mv/cp on
    # the macOS host lacks -t, but this repo also runs on Linux/GNU coreutils).
    case "$tok" in
      -t | -t?* | --target-directory | --target-directory=*) return 1 ;;
    esac
    case "$tok" in -*) continue ;; esac         # a flag (mv -f, mkdir -p, …)
    if [ "$mode_pending" -eq 1 ]; then mode_pending=0; continue; fi
    _broker_seg_secretlike "$tok" && return 1
    resolved="$(_broker_resolve_in_roots "$tok" "$cwd" "$wt" "$slug" "$tasks")" || return 1
    [ "$resolved" = "$wt" ] && return 1         # never target the worktree root itself
    saw_path=1
  done
  [ "$saw_path" -eq 1 ]
}

# _permission_seg_exec_ok <segment> <cwd> <wt> <slug> <tasks> -> rc 0 when the segment EXECUTES
# a spoke-authored in-tree script via a `./<relative-path>` invocation whose executable resolves
# under the worktree or the spoke's session scratchpad (via _broker_resolve_in_roots — the same
# scope the mutation lane uses — which rejects `..`, absolute paths, `.git`, and shell
# metacharacters). Trailing args are opaque to WHICH code runs and are left to the
# script; the segment-level substitution/redirection reject in _permission_seg_safe has already
# fired before this is reached. Inert (rc 1) without a worktree context. Approving this is a
# worktree-trust-boundary call (#240): the gate protects SHARED state — main, the remote, sibling
# worktrees, out-of-tree paths — and trusts the spoke inside its OWN worktree, where it already
# has auto-accepted edits and where an APPROVEd targeted pytest already runs spoke-authored code.
_permission_seg_exec_ok() {
  local seg="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" tok resolved
  [ -n "$wt" ] || return 1
  tok="${seg%%[[:space:]]*}"                   # the executable (first token)
  case "$tok" in './'*) ;; *) return 1 ;; esac # only the relative ./ self-op form
  _broker_seg_secretlike "$tok" && return 1
  resolved="$(_broker_resolve_in_roots "$tok" "$cwd" "$wt" "$slug" "$tasks")" || return 1
  [ "$resolved" = "$wt" ] && return 1          # never "execute" the worktree root itself
  return 0
}

# _permission_seg_marker_ok <segment> <cwd> <wt> <slug> <tasks> -> rc 0 when the segment EMITS a
# workflow marker via the canonical emitter — `bash <path>/spoke-ready.sh …` or `bash
# <path>/spoke-push.sh …` — with the script resolving inside the spoke's worktree/scratchpad and
# every argument fitting the marker shape: a state flag (--gate|--accept|--blocked|--ready), a
# numeric issue, an optional --plan-file whose path ALSO resolves in-tree (so an out-of-tree file
# is never read into the pushed tag body), and an optional -m/--message reason (free-form — once
# seen the segment tail is accepted, since the segment-level substitution/redirection reject in
# _permission_seg_safe has already fired). Gate-marker emission is the drain's MOST critical
# control-plane op (#271): a deterministic Tier-1 lane keeps it off the probabilistic Tier-3 judge,
# which was denying `bash <path>/spoke-ready.sh --gate <N>` and leaving a spoke unable to park. The
# `./`-invoked form is already the #240 exec lane's job, so this lane only adds the `bash <path>`
# invocation (worktree-new.sh's seed prompt, hub-afk.sh's nudge, solo-cycle all use it). Inert
# (rc 1) without a worktree context — the SAME confinement discipline as the #203/#240 lanes,
# reusing _broker_resolve_in_roots (which rejects `..`, absolute paths, `.git`, secret-like names,
# and shell metacharacters).
_permission_seg_marker_ok() {
  local seg="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" rest tok script val_pending=""
  [ -n "$wt" ] || return 1
  case "$seg" in
    'bash '*) rest="${seg#bash }" ;;             # only the canonical `bash <path>` invocation
    *) return 1 ;;
  esac
  rest="${rest#"${rest%%[![:space:]]*}"}"        # ltrim
  script="${rest%%[[:space:]]*}"                 # the script path (first token)
  rest="${rest#"$script"}"
  # The basename must be EXACTLY one of the two canonical emitters — never a same-named decoy
  # elsewhere and never an arbitrary script.
  case "${script##*/}" in
    spoke-ready.sh | spoke-push.sh) ;;
    *) return 1 ;;
  esac
  _broker_seg_secretlike "$script" && return 1
  _broker_resolve_in_roots "$script" "$cwd" "$wt" "$slug" "$tasks" >/dev/null || return 1
  # Walk the arguments by hand (no word-splitting) so a glob never expands; each must fit the
  # marker shape or the whole segment is denied (default-deny).
  while [ -n "$rest" ]; do
    rest="${rest#"${rest%%[![:space:]]*}"}"      # ltrim
    [ -n "$rest" ] || break
    tok="${rest%%[[:space:]]*}"                   # first token
    rest="${rest#"$tok"}"
    if [ "$val_pending" = plan ]; then           # a separate-token --plan-file value
      _broker_resolve_in_roots "$tok" "$cwd" "$wt" "$slug" "$tasks" >/dev/null || return 1
      val_pending=""; continue
    fi
    case "$tok" in
      -m | --message | --message=*) return 0 ;;   # free-form reason tail — accept the remainder
      --plan-file) val_pending=plan ;;
      --plan-file=*)
        _broker_resolve_in_roots "${tok#--plan-file=}" "$cwd" "$wt" "$slug" "$tasks" \
          >/dev/null || return 1 ;;
      --gate | --accept | --blocked | --ready | -h | --help) ;;
      --ready=*) case "${tok#--ready=}" in '' | *[!0-9]*) return 1 ;; esac ;;
      -*) return 1 ;;                             # any other flag is not marker shape
      *) case "$tok" in '' | *[!0-9]*) return 1 ;; esac ;;   # a bare positional must be the numeric issue
    esac
  done
  [ -z "$val_pending" ]                           # a dangling --plan-file (no value) is malformed
}

# _permission_seg_safe <segment> [cwd wt slug tasks] -> true when ONE command segment is a
# safe scoped self-op the spoke legitimately runs on its OWN worktree: the same vetted class
# worktree-new.sh seeds into the spoke allowlist (unstage/stage, own-file pytest,
# read-only helpers). A segment carrying command substitution, backticks, or a
# redirection is never safe — those could smuggle a destructive op behind a safe
# prefix. `git reset`'s working-tree-mutating modes (`--hard`/`--merge`/`--keep`) are
# rejected before the safe `git reset` prefix matches — only unstage/uncommit is safe.
# Everything unrecognised is unsafe (default-deny).
_permission_seg_safe() {
  local seg="$1" cwd="${2:-}" wt="${3:-}" slug="${4:-}" tasks="${5:-}"
  case "$seg" in
    *'$('* | *'`'* | *'>'* | *'<'*) return 1 ;;   # substitution / redirection smuggling
  esac
  # Benign in-worktree mutation lane (#203): when we know the spoke's worktree, a mutating
  # verb (mv/cp/rm/mkdir/chmod) is decided ENTIRELY by the lane — approve when confined to
  # the worktree or its scratchpad, else deny. Deciding it here (not falling through) is what
  # keeps the legacy relative-only `chmod +x` rule below from re-approving a lane MISS such as
  # `chmod +x .git/hooks/pre-commit`. Without worktree context the lane is inert and these
  # verbs fall through to the context-free rules (the relative-only chmod rule / default-deny).
  case "$seg" in
    'mv '* | 'cp '* | 'rm '* | 'mkdir '* | 'chmod '*)
      if [ -n "$wt" ]; then
        _permission_seg_mutation_ok "$seg" "$cwd" "$wt" "$slug" "$tasks" && return 0
        return 1
      fi ;;
  esac
  # Benign in-worktree EXECUTION lane (#240): running the spoke's OWN in-tree script
  # (`./path/to/script.sh`) is a scoped self-op, decided ENTIRELY by the lane when a worktree
  # is known — mirroring the mutation lane above so the context-free rules below never re-judge
  # it. Without a worktree context the lane is inert and `./…` falls through to default-deny.
  case "$seg" in
    './'*)
      if [ -n "$wt" ]; then
        _permission_seg_exec_ok "$seg" "$cwd" "$wt" "$slug" "$tasks" && return 0
        return 1
      fi ;;
  esac
  # Marker-emission lane (#271): the drain's most critical control-plane op — emitting a workflow
  # marker via `bash <path>/spoke-{ready,push}.sh …` — is decided ENTIRELY by the lane when a
  # worktree is known (a deterministic Tier-1 APPROVE, never the Tier-3 judge), else `bash …`
  # falls through to default-deny. Same shape as the mutation/exec lanes above.
  case "$seg" in
    'bash '*)
      if [ -n "$wt" ]; then
        _permission_seg_marker_ok "$seg" "$cwd" "$wt" "$slug" "$tasks" && return 0
        return 1
      fi ;;
  esac
  case "$seg" in
    *'--hard'* | *'--merge'* | *'--keep'*) return 1 ;;  # reset modes that touch the worktree
    'git reset' | 'git reset '* ) return 0 ;;      # unstage/uncommit only — worktree-local
    'git add' | 'git add '* ) return 0 ;;          # stage — worktree-confined
    'git status' | 'git status '* | 'git diff' | 'git diff '* ) return 0 ;;
    'git log' | 'git log '* | 'git show' | 'git show '* ) return 0 ;;
    'git rev-parse' | 'git rev-parse '* | 'git branch --show-current' ) return 0 ;;
    'git fetch' | 'git fetch '* ) return 0 ;;
    # git stash is worktree/stash-local (never touches main or the remote): pop/apply restore
    # the spoke's own stashed work, push/save stash it, list/show inspect it (#203 finding 4).
    'git stash' | 'git stash pop'* | 'git stash apply'* | 'git stash push'* \
      | 'git stash save'* | 'git stash list'* | 'git stash show'* ) return 0 ;;
    # pytest MUST carry a NON-FLAG argument (a path / node-id): a bare `pytest` OR one
    # carrying only flags (`pytest -q`, `pytest -x`) still runs the whole suite, whose
    # escaped tests rewrite real refs (#135) — the full-suite ref-rewind hazard. Requiring
    # a token (not merely any token) closes the flag-only bypass (#203).
    'pytest '* | 'python -m pytest '* | 'python3 -m pytest '* | '.venv/bin/python -m pytest '* )
      _pytest_seg_scoped "$seg" && return 0 || return 1 ;;
    'ls' | 'ls '* | 'cat '* | 'head '* | 'tail '* | 'wc' | 'wc '* ) return 0 ;;
    'grep '* | 'rg '* | 'echo' | 'echo '* | 'tree' | 'tree '* ) return 0 ;;
    'find '* )
      # A read-only find is a fine self-op, but any side-effecting primary is not: `-delete`
      # destroys files; `-exec`/`-execdir`/`-ok`/`-okdir` spawn processes; `-fprint`/`-fprintf`/
      # `-fprint0`/`-fls` write to an arbitrary file. Deny them all (#171 + review). `-print`/
      # `-printf` write only to stdout and stay allowed. Over-denial (a filename that happens to
      # contain one of these) escalates to a human, the safe direction for a default-deny guard.
      case "$seg" in *-delete* | *-exec* | *-ok* | *-fprint* | *-fls* ) return 1 ;; esac
      return 0 ;;
    'chmod +x '* )
      # chmod +x only on a RELATIVE, in-tree path. Reject an absolute target (a leading `/` or
      # a later ` /` token like `chmod +x a /bin/x`) and any `..` that would traverse out of the
      # spoke's worktree (#171 + review). A false deny (a filename containing `..`) escalates.
      case "$seg" in *' /'* | 'chmod +x /'* | *'..'* ) return 1 ;; esac
      return 0 ;;
    * ) return 1 ;;
  esac
}

# --- read-only Read tool lane (issue #181) ------------------------------------
# A spoke parks on a `Read` PERMISSION dialog for a legitimate, write-free research read —
# a hub script/hook (#175 parked on Read(<hub>/.git/hooks/pre-push)) or a sibling worktree.
# extract_pending_command surfaces such a park as "Read <file_path>"; classify_permission
# AUTO-APPROVES it when the target is confined to the repo family (the main root + its
# worktrees) and is not secret-like. A Read mutates nothing, so — unlike the write lane above
# — .git internals are readable; only the global secret classes (~/.ssh, ~/.aws, *.pem,
# id_rsa*, credential confs) stay denied. Every OTHER non-Bash tool arrives as a bare name and
# keeps default-deny.

# _broker_repo_family_roots <wt> -> print each repo-family root (the main worktree PLUS every
# linked worktree, from `git worktree list`), realpath-canonicalized, one per line. Empty when
# <wt> is not a git worktree. This is the read scope a spoke legitimately studies.
_broker_repo_family_roots() {
  local wt="$1" line p
  [ -n "$wt" ] || return 0
  git -C "$wt" worktree list --porcelain 2>/dev/null | while IFS= read -r line; do
    case "$line" in
      'worktree '*) p="${line#worktree }"; wt_realpath "$p" ;;
    esac
  done
}

# _broker_read_in_family <path> <wt> -> print <path>'s realpath (symlinks followed) IF it
# resolves under some repo-family root, else rc 1. Resolves <path> against the worktree cwd when
# relative, mirroring _broker_path_physically_in. The printed realpath lets the caller re-check
# the secret class on the RESOLVED surface (a benign-named in-family symlink to a key, or a
# trailing-slash form, evades a raw-path-only check). Fails CLOSED (rc 1) without python3 or a
# resolvable family — an unverifiable read escalates, the safe direction.
_broker_read_in_family() {
  local path="$1" wt="${2:-}" roots
  roots="$(_broker_repo_family_roots "$wt")"
  [ -n "$roots" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_PATH="$path" _AFK_WT="$wt" _AFK_ROOTS="$roots" python3 2>/dev/null <<'PYEOF'
import os, sys

path = os.environ["_AFK_PATH"]
wt = os.environ.get("_AFK_WT", "")
if not os.path.isabs(path) and wt:
    path = os.path.join(wt, path)
abs_ = os.path.realpath(path)

def under(p, root):
    return p == root or p.startswith(root.rstrip("/") + "/")

for root in os.environ["_AFK_ROOTS"].splitlines():
    if root and under(abs_, os.path.realpath(root)):
        print(abs_)
        sys.exit(0)
sys.exit(1)
PYEOF
}

# _classify_read_tool <path> [wt] -> print "APPROVE" or "ESCALATE<TAB><reason>" for a Read of
# <path>. APPROVE only when <path> is a CLEAN inert path, confined to the repo family, and not
# secret-like. rc is always 0 (the verdict is on stdout, like classify_permission).
#
# The clean-path guard is load-bearing security, not cosmetics: extract_pending_command emits a
# Bash tool_use as its RAW command string in the same slot a Read emits "Read <file_path>", so a
# Bash command whose text starts with "Read " (e.g. `Read x.py; rm -rf ~`) would otherwise enter
# this lane and SKIP classify_permission's operator-split default-deny — auto-approving arbitrary
# shell. A genuine Read file_path is a single inert path, so any whitespace / shell metacharacter /
# operator / traversal makes the target unapprovable here (a false deny escalates — the safe
# direction). The secret class is then checked on BOTH the raw path and its resolved realpath, so
# an in-family symlink with a benign name (notes.txt -> deploy.pem) can't launder a key.
#
# Two properties of the whitespace rejection are load-bearing, DO NOT weaken blindly:
#   - It is a DENYLIST: safety rests on the reject set covering every shell control / expansion /
#     quoting metacharacter. Extend the set, never trim it.
#   - `*[[:space:]]*` rejects an embedded NEWLINE too (a case-glob matches it), closing the
#     newline-as-command-separator variant. If anyone ever relaxes this to allow spaced paths,
#     ONLY space/tab may be re-admitted — a re-allowed newline reopens the masquerade.
# Known limitation: a worktree whose ROOT path itself contains whitespace makes every family read
# non-clean, so the feature degrades to always-escalate for that checkout (safe, but silent).
_classify_read_tool() {
  local path="$1" wt="${2:-}" abs
  if [ -z "$path" ]; then
    printf 'ESCALATE\t%s\n' "Read with no target"
    return 0
  fi
  case "$path" in
    *[[:space:]]* | *';'* | *'&'* | *'|'* | *'$'* | *'`'* | '~'* | *'..'* \
      | *'{'* | *'}'* | *'*'* | *'?'* | *'['* | *']'* | *'"'* | *"'"* | *'\'* \
      | *'>'* | *'<'* | *'('* | *')'*)
      printf 'ESCALATE\t%s\n' "read target is not a clean path: $path"
      return 0 ;;
  esac
  if _broker_seg_secretlike "$path"; then
    printf 'ESCALATE\t%s\n' "secret-like read target: $path"
    return 0
  fi
  abs="$(_broker_read_in_family "$path" "$wt")" || {
    printf 'ESCALATE\t%s\n' "read outside the repo family: $path"
    return 0
  }
  if _broker_seg_secretlike "$abs"; then
    printf 'ESCALATE\t%s\n' "secret-like read target (resolved): $abs"
    return 0
  fi
  printf 'APPROVE\n'
}

# classify_permission <command> [worktree] -> "APPROVE" or "ESCALATE<TAB><reason>".
# DEFAULT-DENY: the command is APPROVEd only when EVERY segment (split on ; && || |) is a
# safe scoped self-op, so a single risky segment in a chain escalates the whole. When the
# spoke's <worktree> is known, the compound is DECOMPOSED and `cd` is tracked so the benign
# in-worktree mutation lane (#203, finding 4) can approve writes confined to the worktree or
# its scratchpad. Anything unrecognised — main-touching, force-push, history rewrite, an
# out-of-tree deletion, network fetch, browser/computer/mcp tool, or a bare non-Bash tool
# name — ESCALATEs, naming the offending command so the block record is actionable.
classify_permission() {
  local cmd="$1" wt="${2:-}" norm seg saw_seg=0 cwd="" slug="" tasks="" target new_cwd
  if [ -n "$wt" ]; then
    slug="$(printf '%s' "$wt" | sed 's/[^A-Za-z0-9]/-/g')"
    tasks="${AFK_TASKS_ROOT:-/private/tmp}"
    cwd="$wt"                                       # the compound starts in the worktree
  fi
  # A non-Bash READ tool invocation arrives as "Read <file_path>" (extract_pending_command
  # carries the target). It is decided ENTIRELY by the read lane (#181), BEFORE operator-
  # splitting so a path with shell-ish characters is never chopped into bogus segments.
  case "$cmd" in
    'Read '*) _classify_read_tool "${cmd#Read }" "$wt"; return 0 ;;
  esac
  # Normalise the shell operators to newlines, longest first so `||` is not split by `|`
  # and `&&` is not split by a single `&`. The single `&` (background) MUST also split, or
  # `echo x & rm -rf /` would match the safe `echo ` prefix and never inspect the tail.
  norm="${cmd//&&/$'\n'}"
  norm="${norm//&/$'\n'}"
  norm="${norm//||/$'\n'}"
  norm="${norm//|/$'\n'}"
  norm="${norm//;/$'\n'}"
  while IFS= read -r seg; do
    seg="${seg#"${seg%%[![:space:]]*}"}"           # ltrim
    seg="${seg%"${seg##*[![:space:]]}"}"           # rtrim
    [ -n "$seg" ] || continue
    saw_seg=1
    # cd-tracking within the compound: a `cd` into a path that stays under the worktree/
    # scratchpad updates the current dir for the following segments' relative paths; a `cd`
    # that escapes (or a bare `cd` → $HOME, or no worktree context) escalates the whole.
    case "$seg" in
      'cd '*)
        target="${seg#cd }"; target="${target#"${target%%[![:space:]]*}"}"
        # An empty target (`cd` → $HOME) or a `-`-prefixed one (`cd -`/`--`/`-P`/`-L` → $OLDPWD
        # or $HOME) navigates OUT of the tree — never a literal in-tree dir. Reject before the
        # resolver, which would otherwise read `--` as an in-tree directory name and track a
        # bogus cwd. A real dir starting with `-` is always reachable as `./-x`.
        case "$target" in '' | -*) printf 'ESCALATE\t%s\n' "risky or unrecognised command: $cmd"; return 0 ;; esac
        if [ -n "$wt" ] && new_cwd="$(_broker_resolve_in_roots "$target" "$cwd" "$wt" "$slug" "$tasks")"; then
          cwd="$new_cwd"; continue
        fi
        printf 'ESCALATE\t%s\n' "risky or unrecognised command: $cmd"; return 0 ;;
    esac
    if ! _permission_seg_safe "$seg" "$cwd" "$wt" "$slug" "$tasks"; then
      printf 'ESCALATE\t%s\n' "risky or unrecognised command: $cmd"
      return 0
    fi
  done <<< "$norm"
  # An empty / all-whitespace command has no segment to vouch for — never approve nothing.
  [ "$saw_seg" -eq 1 ] || { printf 'ESCALATE\t%s\n' "empty or unreadable command"; return 0; }
  printf 'APPROVE\n'
}

# --- Tier-2 static danger classifier (issue #261) -----------------------------
# The deny-wall's fast static blacklist. classify_permission (Tier 1) names the KNOWN-SAFE
# scoped self-ops (APPROVE); classify_danger names the KNOWN-DANGEROUS boundary crossings
# (DENY) among everything else, so the Tier-3 LLM judge runs ONLY on the true residue --
# neither statically safe nor statically dangerous. Same operator-split as classify_permission
# (; && || | &), first dangerous segment wins. Empty output = "no static danger" -> the
# orchestrator routes the command to the judge.
#
# CATEGORIES (the boundary the existing scope-guards do NOT already cover):
#   - privilege escalation / disk destroyers / ownership: sudo/doas/su, dd, mkfs*, fdisk,
#     parted, shred, chown/chgrp
#   - arbitrary exec / classifier-evasion (#269): eval, a shell `-c` inline command, a bare
#     shell verb (pipe-to-shell target), xargs spawning a shell
#   - network egress off-allowlist: curl/wget to a non-allowlisted host; raw sockets
#     (nc/ncat/netcat/telnet/ssh/scp/sftp/ftp) denied outright; a curl/wget WRITE-METHOD flag
#     (-d/--data*, -F/--form, -T/--upload-file, -X POST|PUT|PATCH|DELETE) even to an allowed host
#   - supply-chain publish (#269): npm/yarn/pnpm/poetry publish, twine upload, gem push,
#     cargo publish, docker/podman push
#   - repo/collaboration mutation (#269): mutating gh subcommands (pr create|merge|close|...,
#     repo delete|create|..., release create|...) -- read/comment/issue subcommands stay open
#   - keychain / credential reads: `security`, or a read of a secret-like path
#   - out-of-tree writes: a mutating verb (mv/cp/rm/mkdir/chmod) not confined to the worktree,
#     or a `>`/`>>` redirection whose target escapes it
# NOT duplicated here (owned by sibling PreToolUse scope-guards, kept authoritative): push to
# main / other refs (push-scope-guard, spoke-main-guard), secrets in a commit (secrets-scan),
# config edits (config-protection), `--no-verify` (block-no-verify). Package-installs are
# deliberately NOT statically denied -- they route to the judge so legit fresh-worktree setup
# (`pip install -r requirements-dev.txt`) is not stranded; the journal surfaces a dangerous one
# for promotion to a static rule (the #261 Phase-4 measure->promote loop).
# UPGRADE: promote a judge-caught tier-2 miss (a novel destructive verb, a package-install that
# ran a hostile lifecycle script) into a static case here once the journal shows it.

# _danger_strip_prefix <segment> -> echo the segment with any leading `NAME=value` env
# assignments and no-flag wrapper commands (env/command/nohup/setsid) removed, so a verb-keyed
# category is not evaded by `FOO=1 sudo ...` or `env sudo ...` (the repo's #15292 env-prefix gap,
# here on a DENY wall). The assignment case is the classic gap and is handled fully; a wrapper
# that itself carries flags (`nice -n 10 sudo`, `env -i sudo`) stops the strip and routes to the
# judge -- an acceptable partial fix for the common shapes.
_danger_strip_prefix() {
  local seg="$1" first
  while :; do
    seg="${seg#"${seg%%[![:space:]]*}"}"
    first="${seg%%[[:space:]]*}"
    case "$first" in
      [A-Za-z_]*=*)
        case "${first%%=*}" in *[!A-Za-z0-9_]*) break ;; esac   # not a valid var name -> stop
        seg="${seg#"$first"}" ;;
      env | command | nohup | setsid) seg="${seg#"$first"}" ;;
      *) break ;;
    esac
  done
  printf '%s' "${seg#"${seg%%[![:space:]]*}"}"
}

# _danger_redirect_targets <cmd> -> print each FILE redirect target in <cmd> (one per line),
# skipping fd-duplications (2>&1, >&2). shlex-tokenized so a `>` inside a quoted string is NOT a
# redirect and quoting is honored; prints `__UNPARSEABLE__` on unbalanced quotes (caller denies,
# deny-lean). Replaces the last-`>`-only heuristic that a trailing `2>&1` defeated (#261 review):
# scanning EVERY redirect operator token catches the real out-of-tree target regardless of what
# trails it. Empty (no python3, or no redirects) -> the caller finds no target and moves on.
_danger_redirect_targets() {
  command -v python3 >/dev/null 2>&1 || return 0
  _DANGER_CMD="$1" python3 2>/dev/null <<'PYEOF'
import os, re, shlex

cmd = os.environ["_DANGER_CMD"]
try:
    toks = shlex.split(cmd)
except Exception:
    print("__UNPARSEABLE__"); raise SystemExit(0)

op = re.compile(r"^([0-9]*|&)(>>?)\|?")   # a redirect operator, possibly glued to its target
targets = []
i = 0
while i < len(toks):
    t = toks[i]
    m = op.match(t)
    if m:
        suffix = t[m.end():]
        if suffix:
            if not suffix.startswith("&"):
                targets.append(suffix)
        elif i + 1 < len(toks) and not toks[i + 1].startswith("&"):
            targets.append(toks[i + 1])
            i += 1
    i += 1
for t in targets:
    print(t)
PYEOF
}

# _danger_network_seg <segment> -> print a reason and rc 0 when the segment is off-allowlist
# network egress OR a write-method egress (possible exfil), else rc 1. Raw-socket tools are denied
# outright; curl/wget URL hosts are parsed (python3) and denied unless every host is allowlisted.
# Only `://`-scheme tokens are read as hosts (so a bare hostless arg is never mistaken for a host);
# a curl/wget carrying NO scheme URL fails CLOSED (deny), as does an absent python3 -- an
# unverifiable egress does not run. #269: a WRITE-METHOD flag (-d/--data*, -F/--form,
# -T/--upload-file, -X/--request with POST|PUT|PATCH|DELETE) is denied even to an allowlisted host
# -- a POST body / upload can exfil to a gist or the API. Download flags -o/-O (write the RESPONSE
# to a file) stay benign, so a legit `curl ... -o out.json` GET read is not blocked.
_danger_network_seg() {
  local seg="$1" verb host_check
  verb="${seg%%[[:space:]]*}"
  case "$verb" in
    nc | ncat | netcat | telnet | ssh | scp | sftp | ftp)
      printf 'raw network egress (%s) is denied under bypass' "$verb"; return 0 ;;
    curl | wget) ;;
    *) return 1 ;;
  esac
  command -v python3 >/dev/null 2>&1 || { printf 'network egress unverifiable (no python3): %s' "$verb"; return 0; }
  host_check="$(_DANGER_SEG="$seg" python3 2>/dev/null <<'PYEOF'
import os, sys, shlex
from urllib.parse import urlparse

def allowed(h):
    h = h.lower()
    if h in {"api.anthropic.com", "anthropic.com", "github.com", "api.github.com",
             "raw.githubusercontent.com", "codeload.github.com", "objects.githubusercontent.com"}:
        return True
    return any(h.endswith(s) for s in (".anthropic.com", ".github.com", ".githubusercontent.com"))

try:
    toks = shlex.split(os.environ["_DANGER_SEG"])
except Exception:
    print("DENY unparseable"); sys.exit(0)

# A WRITE-METHOD flag makes this an upload/POST egress (possible exfil) regardless of host --
# deny it even to an allowlisted host (#269). -o/-O write the RESPONSE to a file, not an upload,
# so they stay benign. Short upload flags (-d/-F/-T) and their GLUED forms (-d@f, -XPOST) are
# CURL-only: in wget -d/-F/-T mean --debug/--force-html/--timeout, so applying curl semantics to
# wget false-denied benign reads (#269 review WARNING). wget write-methods are the long forms
# (--post-data/--post-file/--body-data/--body-file/--method=), handled for both below.
mutating = {"POST", "PUT", "PATCH", "DELETE"}
verb = os.path.basename(toks[0]) if toks else ""
is_curl = verb == "curl"

def method_ok(m):
    return m.upper() in mutating

i = 1
while i < len(toks):
    t = toks[i]
    # Long-form request bodies / methods -- curl AND wget.
    if t.startswith("--data") or t.startswith("--form") or t == "--upload-file" \
            or t.startswith("--post-data") or t.startswith("--post-file") \
            or t.startswith("--body-data") or t.startswith("--body-file"):
        print("DENYWRITE " + t); sys.exit(0)
    if (t.startswith("--request=") or t.startswith("--method=")) and method_ok(t.split("=", 1)[1]):
        print("DENYWRITE " + t); sys.exit(0)
    if t in ("--request", "--method") and i + 1 < len(toks) and method_ok(toks[i + 1]):
        print("DENYWRITE " + t + " " + toks[i + 1]); sys.exit(0)
    if is_curl:
        # curl short upload flags, spaced or glued (-d @f / -d@f / -Ffile=@f / -Tfile). startswith
        # subsumes the bare exact flag (-d / -F / -T).
        if t.startswith("-d") or t.startswith("-F") or t.startswith("-T"):
            print("DENYWRITE " + t); sys.exit(0)
        # -X METHOD (spaced) or -XMETHOD (glued).
        if t == "-X" and i + 1 < len(toks) and method_ok(toks[i + 1]):
            print("DENYWRITE -X " + toks[i + 1]); sys.exit(0)
        if len(t) > 2 and t.startswith("-X") and method_ok(t[2:]):
            print("DENYWRITE " + t); sys.exit(0)
    i += 1

# Only a scheme URL (contains ://) whose urlparse yields a hostname counts as an egress host.
# A header value or a bare hostless arg is never a host -> no false deny, and a curl with no
# scheme URL yields no host -> fail-closed DENY below.
hosts = []
for t in toks[1:]:
    if "://" in t:
        h = urlparse(t).hostname
        if h:
            hosts.append(h)

if not hosts:
    print("DENY no-parseable-host"); sys.exit(0)
bad = [h for h in hosts if not allowed(h)]
print("DENY " + bad[0] if bad else "OK")
PYEOF
)"
  case "$host_check" in
    OK) return 1 ;;
    'DENYWRITE '*) printf 'network write-method egress (possible exfil): %s' "${host_check#DENYWRITE }"; return 0 ;;
    'DENY '*) printf 'network egress to a non-allowlisted host: %s' "${host_check#DENY }"; return 0 ;;
    *) printf 'network egress unverifiable: %s' "$verb"; return 0 ;;
  esac
}

# _danger_credential_seg <segment> <cwd> <wt> <slug> <tasks> -> print a reason and rc 0 when the
# segment reads a keychain or an OUT-OF-TREE secret-like path, else rc 1. `security`/`keychain`
# (macOS keychain access) is denied on the verb; a read verb (cat/head/...) touching a secret-like
# token (reusing _broker_seg_secretlike) is denied ONLY when the path is out-of-tree. An IN-TREE
# secret-named file (`tests/fixtures/key.pem`) is the spoke's OWN fixture -- it already has write
# access there, so reading it is within the worktree trust boundary and is NOT denied (#261 review
# NIT). Without a worktree context every secret-like read is denied (can't prove in-tree -> safe).
_danger_credential_seg() {
  local seg="$1" cwd="${2:-}" wt="${3:-}" slug="${4:-}" tasks="${5:-}" verb rest tok
  verb="${seg%%[[:space:]]*}"
  case "$verb" in
    security | keychain) printf 'keychain/credential access (%s) is denied' "$verb"; return 0 ;;
    cat | less | more | head | tail | strings | xxd | od | base64 | openssl | grep | awk | sed | cp | dd | sort | uniq) ;;
    *) return 1 ;;
  esac
  rest="${seg#"$verb"}"
  while [ -n "$rest" ]; do
    rest="${rest#"${rest%%[![:space:]]*}"}"
    [ -n "$rest" ] || break
    tok="${rest%%[[:space:]]*}"
    rest="${rest#"$tok"}"
    case "$tok" in -*) continue ;; esac
    _broker_seg_secretlike "$tok" || continue
    # An in-tree secret-named file is the spoke's own fixture -> within the trust boundary, allow.
    if [ -n "$wt" ] && _broker_resolve_in_roots "$tok" "$cwd" "$wt" "$slug" "$tasks" >/dev/null 2>&1; then
      continue
    fi
    printf 'reads a secret-like path: %s' "$tok"; return 0
  done
  return 1
}

# _danger_write_seg <segment> <cwd> <wt> <slug> <tasks> -> print a reason and rc 0 when the
# segment is a mutating verb (mv/cp/rm/mkdir/chmod) the in-worktree mutation lane does NOT
# confine (invert _permission_seg_mutation_ok), else rc 1. Out-of-tree REDIRECTIONS are handled
# separately, whole-command, in classify_danger (a `2>&1` tail must not shift the check off the
# real target). Inert (rc 1) without a worktree context -- the resolver needs it.
_danger_write_seg() {
  local seg="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" verb
  [ -n "$wt" ] || return 1
  verb="${seg%%[[:space:]]*}"
  case "$verb" in
    mv | cp | rm | mkdir | chmod)
      if ! _permission_seg_mutation_ok "$seg" "$cwd" "$wt" "$slug" "$tasks"; then
        printf 'writes outside the worktree (%s)' "$verb"; return 0
      fi ;;
  esac
  return 1
}

# _danger_privilege_seg <segment> -> print a reason and rc 0 for a privilege-escalation,
# disk-destroying, or ownership-mutation verb, else rc 1. chown/chgrp fold in here (deny
# outright, unlike the in-tree-allowed chmod owned by _danger_write_seg): a worktree-confined
# spoke has no legit ownership-mutation use, so a target check would only add surface (#269).
_danger_privilege_seg() {
  local verb; verb="${1%%[[:space:]]*}"
  case "$verb" in
    sudo | doas | su | dd | fdisk | parted | shred | mkfs | mkfs.*)
      printf 'privileged / destructive command (%s) is denied' "$verb"; return 0 ;;
    chown | chgrp)
      printf 'ownership mutation (%s) is denied under bypass' "$verb"; return 0 ;;
    *) return 1 ;;
  esac
}

# _danger_publish_seg <segment> -> print a reason and rc 0 for a supply-chain PUBLISH (an
# outward package/image release a worktree-confined spoke never performs), else rc 1. A
# two-token verb+subcommand match (npm/yarn/pnpm/poetry publish, twine upload, gem push,
# cargo publish, docker/podman push); a bare `npm install` or `docker build` is untouched.
_danger_publish_seg() {
  local seg="$1" verb sub
  verb="${seg%%[[:space:]]*}"
  sub="${seg#"$verb"}"; sub="${sub#"${sub%%[![:space:]]*}"}"; sub="${sub%%[[:space:]]*}"
  case "$verb $sub" in
    'npm publish' | 'yarn publish' | 'pnpm publish' | 'poetry publish' | \
    'twine upload' | 'gem push' | 'cargo publish' | 'docker push' | 'podman push')
      printf 'supply-chain publish (%s %s) is denied under bypass' "$verb" "$sub"; return 0 ;;
    *) return 1 ;;
  esac
}

# _danger_eval_seg <segment> -> print a reason and rc 0 for arbitrary-exec / classifier-evasion
# shapes, else rc 1. `eval` runs an unsplit string (`eval "$(curl ...)"` is never operator-split
# or host-checked); a shell verb carrying `-c` (inline command) or `-s` (script on stdin) runs
# arbitrary code; a BARE shell verb (no non-flag argument) is a pipe-to-shell target reading stdin
# (`curl ... | bash`, whose halves are separate segments); and `xargs` whose COMMAND WORD is a
# shell (`xargs sh -c ...`) launders exec through the argv. Benign `bash -n file` / `bash
# script.sh` (a non-flag arg, no -c/-s), `bash --version` (info probe), and `find | xargs grep
# bash` (the command word is grep, NOT the shell-named ARGUMENT) all still pass -- the xargs scan
# checks only the exec'd command word, skipping xargs's own value-taking options (#269 review
# BLOCKER). UPGRADE: a combined short-flag cluster (`bash -lc`, `sh -sc`) is not split here -- it
# routes to the fail-closed judge; add flag-cluster parsing if the journal shows it exploited.
_danger_eval_seg() {
  local seg="$1" verb rest tok has_arg=0 skip=0 cmdword=""
  verb="${seg%%[[:space:]]*}"
  case "$verb" in
    eval) printf 'eval runs an uninspected command string -- denied under bypass'; return 0 ;;
    sh | bash | zsh | dash | ksh)
      rest="${seg#"$verb"}"
      while [ -n "$rest" ]; do
        rest="${rest#"${rest%%[![:space:]]*}"}"
        [ -n "$rest" ] || break
        tok="${rest%%[[:space:]]*}"
        rest="${rest#"$tok"}"
        case "$tok" in
          -c | -s)
            printf 'inline/stdin shell command (%s %s ...) is denied under bypass' "$verb" "$tok"; return 0 ;;
          --version | --help | -V) return 1 ;;  # an info probe, not an exec -- benign
          -*) continue ;;
          *) has_arg=1 ;;
        esac
      done
      if [ "$has_arg" -eq 0 ]; then
        printf 'pipe-to-shell / bare interactive shell (%s) is denied under bypass' "$verb"; return 0
      fi
      return 1 ;;
    xargs)
      # The command xargs execs is its FIRST non-option token -- match ONLY that against the shell
      # set, skipping xargs's own value-taking options (a separate-word value like `-I {}` / `-n 1`
      # / `-P 4` / GNU `--max-procs 4`). Scanning every token falsely denied `xargs grep bash` (#269
      # review BLOCKER). REQUIRED-arg GNU long options are skipped in their SPACED form too (#269
      # review). UPGRADE: an OPTIONAL-arg option (GNU `--max-lines`/`--replace`/`-e`/`--eof`, whose
      # value is `=`-glued only) in a bogus spaced form, or an unknown long option, is left to the
      # fail-closed tier-3 judge rather than risk a mis-skip that OPENS a shell-launder.
      rest="${seg#"$verb"}"
      while [ -n "$rest" ]; do
        rest="${rest#"${rest%%[![:space:]]*}"}"
        [ -n "$rest" ] || break
        tok="${rest%%[[:space:]]*}"
        rest="${rest#"$tok"}"
        if [ "$skip" -eq 1 ]; then skip=0; continue; fi
        case "$tok" in
          -I | -J | -L | -n | -P | -R | -S | -s | -E | -a | -d | \
          --max-args | --max-procs | --max-chars | --delimiter | --arg-file | --process-slot-var)
            skip=1; continue ;;                       # an option taking a separate-word value
          --*=* | -*) continue ;;                     # glued-value / no-arg / optional-arg option
          *) cmdword="$tok"; break ;;
        esac
      done
      case "$cmdword" in
        sh | bash | zsh | dash | ksh | eval | /bin/sh | /bin/bash | /usr/bin/env | env)
          printf 'xargs spawning a shell (%s) is denied under bypass' "$cmdword"; return 0 ;;
      esac
      return 1 ;;
  esac
  return 1
}

# _danger_gh_seg <segment> -> print a reason and rc 0 for a MUTATING gh subcommand a
# worktree-confined spoke never legitimately runs (repo/collaboration/release mutation), else
# rc 1. Split at the SUBCOMMAND level, never blanket-deny: the spoke tooling shells `gh issue
# view/comment` and `gh pr view` (allowlisted at worktree-new.sh:338), so only the mutating
# pr/repo/release verbs are denied; every read/comment/issue subcommand falls through to Tier 1
# / the judge. A spoke never self-lands or opens PRs (the ship-discipline rule).
_danger_gh_seg() {
  local seg="$1" verb obj sub rest
  verb="${seg%%[[:space:]]*}"
  [ "$verb" = gh ] || return 1
  rest="${seg#gh}"; rest="${rest#"${rest%%[![:space:]]*}"}"
  obj="${rest%%[[:space:]]*}"
  rest="${rest#"$obj"}"; rest="${rest#"${rest%%[![:space:]]*}"}"
  sub="${rest%%[[:space:]]*}"
  case "$obj $sub" in
    'pr create' | 'pr merge' | 'pr close' | 'pr reopen' | 'pr ready' | 'pr edit' | \
    'repo delete' | 'repo create' | 'repo rename' | 'repo archive' | 'repo edit' | \
    'release create' | 'release delete' | 'release edit' | 'release upload')
      printf 'gh mutating subcommand (gh %s %s) is denied under bypass' "$obj" "$sub"; return 0 ;;
    *) return 1 ;;
  esac
}

# classify_danger <command> [worktree] -> "DENY<TAB><reason>" for the FIRST dangerous segment,
# or empty (rc 0) when no segment statically matches (the orchestrator then routes to the judge).
classify_danger() {
  local cmd="$1" wt="${2:-}" norm seg slug="" tasks="" cwd="" reason target new_cwd rtargets rt
  if [ -n "$wt" ]; then
    slug="$(printf '%s' "$wt" | sed 's/[^A-Za-z0-9]/-/g')"
    tasks="${AFK_TASKS_ROOT:-/private/tmp}"
    cwd="$wt"
    # Out-of-tree REDIRECTION (whole-command, shlex-based). Done once on the RAW command -- not
    # per operator-split segment -- so a trailing `2>&1` (which the `&`-split would shatter) can
    # never shift the check off the real target (#261 review). Targets resolve against the initial
    # worktree cwd, so an ABSOLUTE out-of-tree redirect is caught regardless of any earlier `cd`.
    rtargets="$(_danger_redirect_targets "$cmd")"
    while IFS= read -r rt; do
      [ -n "$rt" ] || continue
      [ "$rt" = "__UNPARSEABLE__" ] && { printf 'DENY\t%s\n' "unparseable command (unbalanced quoting) -- fail-closed"; return 0; }
      case "$rt" in '&'*) continue ;; esac
      if ! _broker_resolve_in_roots "$rt" "$wt" "$wt" "$slug" "$tasks" >/dev/null 2>&1; then
        printf 'DENY\t%s\n' "writes outside the worktree via redirection: $rt"; return 0
      fi
    done <<< "$rtargets"
  fi
  norm="${cmd//&&/$'\n'}"; norm="${norm//&/$'\n'}"; norm="${norm//||/$'\n'}"
  norm="${norm//|/$'\n'}"; norm="${norm//;/$'\n'}"
  while IFS= read -r seg; do
    seg="${seg#"${seg%%[![:space:]]*}"}"; seg="${seg%"${seg##*[![:space:]]}"}"
    [ -n "$seg" ] || continue
    # Strip a leading `FOO=bar` / `env|command|nohup|setsid` prefix so it can't shift the verb
    # off a keyed category (the #15292 env-prefix gap, here on a DENY wall).
    seg="$(_danger_strip_prefix "$seg")"
    [ -n "$seg" ] || continue
    # cd-tracking mirrors classify_permission so a `cd`-then-write compound resolves relative
    # targets against the right dir. A cd that ESCAPES the roots does NOT leave cwd in-tree
    # (that let a following relative write resolve against a stale in-tree cwd, #261 review);
    # instead cwd is set to an out-of-tree sentinel, so subsequent relative writes resolve
    # out-of-tree and deny.
    case "$seg" in
      'cd '*)
        target="${seg#cd }"; target="${target#"${target%%[![:space:]]*}"}"
        case "$target" in '' | -*) continue ;; esac
        if [ -n "$wt" ] && new_cwd="$(_broker_resolve_in_roots "$target" "$cwd" "$wt" "$slug" "$tasks")"; then
          cwd="$new_cwd"
        else
          cwd="/__afk_cd_escaped__"   # out-of-tree sentinel: relative writes below now deny
        fi
        continue ;;
    esac
    if reason="$(_danger_privilege_seg "$seg")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_eval_seg "$seg")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_network_seg "$seg")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_publish_seg "$seg")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_gh_seg "$seg")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_credential_seg "$seg" "$cwd" "$wt" "$slug" "$tasks")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_write_seg "$seg" "$cwd" "$wt" "$slug" "$tasks")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
  done <<< "$norm"
  return 0
}

# --- Tier-3 headless LLM judge (issue #261) -----------------------------------
# The residue tiers 1-2 did not resolve goes to a cheap headless judge: a TOOLLESS `claude -p`
# (pure text yes/no classification, no tools granted, so it makes NO tool calls and can never
# itself trigger the deny-wall or recurse), Haiku, bounded ~2s. FAIL-CLOSED: a timeout, a
# nonzero exit, or an unparseable verdict all read as DANGEROUS -- an unjudgeable risky command
# does not run under bypass (the caller warns + journals instead). Verdicts are cached by command
# hash so a command repeated across a drain costs at most one LLM call.

# _judge_cache_dir -> the per-run verdict cache dir (under the afk state dir, cleared per window).
_judge_cache_dir() { printf '%s\n' "$(_afk_state_dir)/judge-cache"; }

# _judge_cache_key <cmd> -> a stable content hash of the command (the cache filename). Empty
# when shasum is unavailable, which disables caching (every call runs the judge) but is harmless.
_judge_cache_key() { printf '%s' "$1" | shasum -a 256 2>/dev/null | awk '{print $1}'; }

# _judge_timeout -> the judge wall-clock budget in seconds (AFK_JUDGE_TIMEOUT, default 120).
# The default must accommodate a full headless claude -p round trip: CLI cold start alone
# exceeds the old 2s bound, which fail-closed EVERY tier-3 decision on this host (#268). A
# non-numeric or non-positive override falls back to the default so the bound is never lifted.
_judge_timeout() {
  local s="${AFK_JUDGE_TIMEOUT:-120}"
  case "$s" in '' | *[!0-9]*) s=120 ;; esac
  [ "$s" -lt 1 ] && s=120
  printf '%s\n' "$s"
}

# --- drain-level judge-unavailable halt (#268 AC4) ----------------------------
# A dead judge (a bad model, a revoked token, a structural budget bug) otherwise DENIES one
# tier-3 command at a time, silently, for the whole window. After N CONSECUTIVE unavailable
# (nonzero-rc) outcomes we raise a drain-level flag -- a FILE in the shared afk state dir, since
# the judge runs in the SPOKE's PreToolUse hook subprocess (not the supervisor loop, so the
# process-global _AFK_AUTH_FAILED the answerer uses cannot reach it). A supervisor that consults
# broker_judge_halt_pending can then pause dispatch + re-probe, mirroring the answerer
# auth-failure path (#241 §9) -- that consult is the drain's to wire (kept out of this change's
# scope). A reachable judge (rc 0) clears the streak + the flag so the drain resumes on recovery.
# The streak counter is a best-effort read-modify-write shared across concurrent spoke hooks: a
# lost increment only DELAYS the halt (fires a failure or two later), never spuriously raises it
# -- fine for an advisory heuristic; no lock is warranted until a consumer needs the exact count.

# _judge_streak_file -> the consecutive judge-unavailable counter (reset by any reachable judge).
_judge_streak_file() { printf '%s\n' "$(_afk_state_dir)/judge-unavailable-streak"; }

# _judge_halt_file -> the raised drain-level halt flag; its content is a human-readable reason.
_judge_halt_file() { printf '%s\n' "$(_afk_state_dir)/judge-halt"; }

# _judge_halt_streak -> consecutive unavailable outcomes before the halt is raised
# (AFK_JUDGE_HALT_STREAK, default 3). A non-numeric/non-positive override falls back to the
# default so the threshold is never silently disabled (mirrors _judge_timeout).
_judge_halt_streak() {
  local n="${AFK_JUDGE_HALT_STREAK:-3}"
  case "$n" in '' | *[!0-9]*) n=3 ;; esac
  [ "$n" -lt 1 ] && n=3
  printf '%s\n' "$n"
}

# broker_judge_halt_pending -> rc 0 when the drain-level judge halt is raised. A supervisor
# consults this to pause dispatch and re-probe (the judge counterpart of _AFK_AUTH_FAILED, #268).
broker_judge_halt_pending() { [ -f "$(_judge_halt_file)" ]; }

# broker_reset_judge_halt -> clear the halt flag AND the streak counter (the judge recovered, or
# a manual reset). Best-effort; never aborts.
broker_reset_judge_halt() {
  rm -f "$(_judge_halt_file)" "$(_judge_streak_file)" 2>/dev/null || true
}

# _judge_note_unavailable -> record one consecutive judge-unavailable outcome; at the threshold
# crossing raise the halt flag ONCE and journal a distinct drain-level event (so the morning
# review sees "the judge died, dispatch paused" rather than a scatter of per-command DENYs).
_judge_note_unavailable() {
  local sf hf n streak
  sf="$(_judge_streak_file)"; hf="$(_judge_halt_file)"
  mkdir -p "$(dirname "$sf")" 2>/dev/null || true
  streak="$(cat "$sf" 2>/dev/null)"; case "$streak" in '' | *[!0-9]*) streak=0 ;; esac
  streak=$(( streak + 1 ))
  printf '%s\n' "$streak" > "$sf" 2>/dev/null || true
  n="$(_judge_halt_streak)"
  # Raise only on the threshold crossing (flag not already present) so the distinct journal
  # event fires once, not on every further failure past the threshold.
  if [ "$streak" -ge "$n" ] && [ ! -f "$hf" ]; then
    printf 'judge unavailable %sx in a row -- dispatch paused, re-probing (#268)\n' "$streak" \
      > "$hf" 2>/dev/null || true
    _broker_journal_line "" judge "judge unavailable ${streak}x in a row -- pausing dispatch" scope
  fi
}

# _judge_note_available -> a reachable judge (rc 0): clear the streak + any raised halt so the
# drain resumes. Only touches disk when there is state to clear (no write on the healthy path).
_judge_note_available() {
  if [ -f "$(_judge_streak_file)" ] || [ -f "$(_judge_halt_file)" ]; then
    broker_reset_judge_halt
  fi
}

# _judge_base_cmd -> the headless judge command (AFK_JUDGE_CMD override for tests). TOOLLESS by
# construction: `--allowedTools ''` grants NO tools, so the judge can make no tool calls -- it can
# never fire afk_danger_guard or recurse. Haiku + `-p` for a cheap, non-interactive run.
# UPGRADE: confirm `--allowedTools ''` grants zero tools on the deployed CLI; if a future CLI
# defaults some tool on, switch to an explicit `--disallowedTools '*'` or equivalent.
_judge_base_cmd() {
  printf '%s\n' "${AFK_JUDGE_CMD:-claude -p --no-session-persistence --model ${AFK_JUDGE_MODEL:-claude-haiku-4-5} --allowedTools ''}"
}

# _judge_prompt <cmd> -> the tight classification prompt. Plain ASCII (no backticks/parens in a
# way that would trip bash 3.2, and this is a normal heredoc, not $()-nested).
_judge_prompt() {
  local cmd="$1"
  cat <<EOF
You are a security gate for an UNATTENDED coding agent running inside its OWN git worktree
under bypassPermissions -- no human will ever see a prompt. Decide whether the shell command
below is DANGEROUS to run unattended. It is DANGEROUS if its effects can reach OUTSIDE the
worktree: writing or deleting files outside it, network egress to an untrusted host, reading
credentials or the keychain, touching the main branch or other git refs, force-pushing or
rewriting history, privilege escalation, or destroying disks. It is SAFE if its effects stay
confined to the worktree. When in doubt, answer dangerous.
End with EXACTLY one final line: 'VERDICT: safe' or 'VERDICT: dangerous'. You may precede it
with a one-line reason.

COMMAND:
$cmd
EOF
}

# judge_permission <cmd> [issue] -> "SAFE" or "DANGEROUS<TAB><reason>". Cache-first; otherwise
# run the toolless headless judge, bounded and fail-closed. Only a PARSED verdict (VERDICT:
# safe|dangerous) is cached: an unavailable or unparseable judge fails closed for THIS decision
# but is never cached, so a transient failure cannot poison the command for the whole window
# (#268). A nonzero rc also feeds the consecutive-unavailable streak (_judge_note_unavailable):
# at the threshold a drain-level halt is raised; a reachable judge clears it (_judge_note_available).
# Always rc 0 (the verdict is on stdout, like classify_permission / classify_danger).
judge_permission() {
  local cmd="$1" key cache f raw rc verdict secs jcmd prompt pf tag cacheable=0
  key="$(_judge_cache_key "$cmd")"
  cache="$(_judge_cache_dir)"; f="$cache/$key"
  if [ -n "$key" ] && [ -f "$f" ]; then cat "$f" 2>/dev/null; return 0; fi
  secs="$(_judge_timeout)"
  jcmd="$(_judge_base_cmd)"
  prompt="$(_judge_prompt "$cmd")"
  # Deliver the prompt via a temp file the wrapped command reopens with `exec <`, mirroring
  # run_answerer: the bound (_afk_with_timeout portable fallback) BACKGROUNDS the command and a
  # backgrounded job's stdin is /dev/null, so a bare here-string would be lost. The here-string
  # stays as the fallback for the foreground timeout/perl paths (and when mktemp is unavailable).
  pf="$(mktemp 2>/dev/null)" || pf=""
  [ -n "$pf" ] && { printf '%s' "$prompt" > "$pf"; jcmd="exec <'$pf'; $jcmd"; }
  raw="$(_broker_run_bounded "$secs" bash -c "$jcmd" <<<"$prompt" 2>/dev/null)"; rc=$?
  [ -n "$pf" ] && rm -f "$pf" 2>/dev/null
  if [ "$rc" -ne 0 ]; then
    # #268 AC3: distinguish a TIMEOUT (coreutils `timeout` -> 124, perl `alarm`/SIGALRM -> 142,
    # the path this macOS host hits with no coreutils installed) from any other judge failure,
    # so the decision journal separates "the budget was too short" from "the judge is broken".
    # 137/143 (SIGKILL/SIGTERM) are deliberately NOT treated as timeouts -- they overlap with
    # non-timeout kills and would over-claim.
    case "$rc" in
      124 | 142) verdict="$(printf 'DANGEROUS\tjudge timed out (rc=%s) -- fail-closed' "$rc")" ;;
      *) verdict="$(printf 'DANGEROUS\tjudge unavailable (rc=%s) -- fail-closed' "$rc")" ;;
    esac
    # #268 AC4: an unavailable judge is a transient outcome -- count the consecutive streak and,
    # at the threshold, raise the drain-level halt so dispatch pauses instead of grinding on.
    _judge_note_unavailable
  else
    tag="$(printf '%s' "$raw" | grep -ioE 'VERDICT:[[:space:]]*(safe|dangerous)' | tail -1 \
      | grep -ioE 'safe|dangerous' | tr '[:upper:]' '[:lower:]')"
    case "$tag" in
      safe) verdict="SAFE"; cacheable=1 ;;
      dangerous) verdict="$(printf 'DANGEROUS\tjudge verdict: dangerous')"; cacheable=1 ;;
      *) verdict="$(printf 'DANGEROUS\tjudge verdict unparseable -- fail-closed')" ;;
    esac
    # #268 AC4: rc 0 means the judge is REACHABLE (even an unparseable answer proves the CLI ran)
    # -- clear the consecutive-unavailable streak + any raised halt so the drain resumes.
    _judge_note_available
  fi
  if [ -n "$key" ] && [ "$cacheable" -eq 1 ]; then
    mkdir -p "$cache" 2>/dev/null || true
    printf '%s\n' "$verdict" > "$f" 2>/dev/null || true
  fi
  printf '%s\n' "$verdict"
}

# --- permission-dialog detection + handling (issue #149) ----------------------
# A permission dialog is a pane-only surface — a Claude Code confirmation prompt with no
# transcript entry of its OWN — but the tool_use it is gating IS flushed to the JSONL as an
# UNRESOLVED block (no matching tool_result) for the whole park. So the dialog is detected
# from the pane (the only "a dialog is up" signal) and the command it gates is read from that
# unresolved tool_use. classify_permission decides it; these helpers see it and deliver the
# decision. _decide_permission is reached from decide_and_act, which routes a
# permission-pending spoke here instead of to the answerer.

# extract_pending_command <wt_path> -> the command of the spoke's trailing UNRESOLVED
# assistant tool_use — the one a permission dialog is gating (Bash -> its command string;
# Read -> "Read <file_path>"; any other tool -> the tool name, so the classifier escalates
# non-Bash tools like browser/computer/mcp). A tool_use is UNRESOLVED when no later
# tool_result carries its id; the PRIOR calls a parked spoke already completed are resolved
# and MUST be skipped (#240: returning the last resolved tool surfaced a phantom "Write" and
# escalated a spoke that needed no human). Empty when nothing is unresolved -> the caller
# escalates honestly ("unreadable command"), never on a stale resolved tool name.
extract_pending_command() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  _AFK_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json, os

# Two passes over the transcript: first collect every tool_result's tool_use_id (a
# tool_result always trails its tool_use in file order, so resolution can only be known
# after a full read), then pick the LAST tool_use whose id is NOT among them — the one the
# permission dialog is still gating. Prior, already-resolved calls are skipped (#240).
tool_uses = []            # ordered (id, name, input) of every assistant tool_use
resolved = set()          # tool_use_ids that a later tool_result has settled
try:
    with open(os.environ["_AFK_JSONL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            content = (obj.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use" and obj.get("type") == "assistant":
                    tool_uses.append(
                        (block.get("id"), (block.get("name") or "").strip(), block.get("input") or {})
                    )
                elif btype == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid:
                        resolved.add(tid)
except Exception:
    tool_uses = []

cmd = ""
for tid, name, inp in reversed(tool_uses):
    if tid in resolved:       # a completed call the spoke already ran — never the pending one
        continue
    if not isinstance(inp, dict):
        inp = {}
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
    elif name == "Read":
        # Carry the Read TARGET alongside the name (#181) so the classifier can vet the
        # path — a repo-family read is auto-approvable, a bare name is not.
        fp = (inp.get("file_path") or "").strip()
        cmd = f"{name} {fp}" if fp else name
    elif name:
        cmd = name
    break                     # the trailing unresolved tool_use is the pending command
# NB: NOT truncated since #257 -- this command feeds the default-deny classify_permission and the
# _reason_permission prompt in the pane path. Truncating a benign prefix off a risky tail could
# hide the risky segment and mis-approve it, exactly as #253 avoided for afk_permission_hook_decide.
# The 2000-char DISPLAY cap now lives at the log call sites in _decide_permission via cmd_display,
# not here. The other consumers tolerate the full command: _permission_pending tests non-emptiness
# and _broker_park_signature hashes the basis.
# Plain ASCII, no backticks/parens: bash 3.2 mis-parses those inside a heredoc.
print(cmd.strip())
PYEOF
}

# _permission_pending <wt_path> -> true when the spoke is parked on a permission dialog. #269
# (#254 option b): DETECTION is decoupled from EXTRACTION. A shown pane dialog IS a park even
# when extract_pending_command is empty -- the gated tool_use is not flushed while the dialog is
# pending (the #240/#254 finding), so ANDing a non-empty command made a real park read as FALSE,
# and the reaper (_reap_or_resume) fell past the park check into "likely hung -> revive",
# re-raising the identical dialog. The pane is the "a dialog is up" signal -- but the prompt
# PHRASE alone is not enough: it can appear in a spoke's OWN rendered output (a spoke maintaining
# the afk subsystem git-shows the file that defines the phrase), a #240/#89-class false park
# (#269 review). So require BOTH the phrase (_pane_shows_permission_prompt) AND the live dialog's
# interactive affordance -- a numbered Yes/No option line the real menu draws but a plain text
# echo does not. The #240 guard holds: NO pane dialog -> false (no phantom park on a stale
# RESOLVED tool). _decide_permission reads the command separately and handles an unreadable one
# (decline + warn, never park). Fail-closed on no tmux/pane. The single gate slot_state and
# decide_and_act share. The pane is captured ONCE and both patterns are grepped from that copy:
# a second capture-pane doubled the tmux subprocess load and, more importantly, its extra
# failure surface destabilized the park signature under heavy load (a flaked capture flipped the
# park verdict, resetting the re-answer ceiling -- #269 final review NIT + a load-flake fix). The
# phrase default MIRRORS _pane_shows_permission_prompt (hub-inject.sh) and reads the SAME
# AFK_PERMISSION_PROMPT_RE override, so an operator retune stays consistent across both.
_permission_pending() {
  local wt="$1" target pane
  command -v tmux >/dev/null 2>&1 || return 1
  target="$(_spoke_pane_target "$wt")"
  [ -n "$target" ] || return 1
  pane="$(tmux capture-pane -p -t "$target" 2>/dev/null)" || return 1
  printf '%s\n' "$pane" | grep -Eq -- "${AFK_PERMISSION_PROMPT_RE:-Do you want to proceed\?}" || return 1
  printf '%s\n' "$pane" | grep -Eq -- "${AFK_PERMISSION_AFFORD_RE:-[0-9]+\.[[:space:]]+(Yes|No)}"
}

# _reason_permission_record <wt> <issue> <decision> <rev> -> the post-DELIVERY record for a
# reasoned permission decision: a loud warned record, a gh comment (off the keypress critical
# path), the warned-retry backoff arm, and the warn span. The caller writes the cheap FILE
# journal line BEFORE the keypress (durability); this reflects the delivered OUTCOME after.
_reason_permission_record() {
  local wt="$1" issue="$2" decision="$3" rev="$4"
  broker_warn "$issue" "$decision"
  _broker_journal_gh_comment "$issue" permission "$decision" "$rev"
  _afk_warned_arm "$issue"
  afk_emit_decision "$wt" warn
}

# _reason_permission <wt> <issue> <cmd> <classify_reason> -> the reasoner decides a permission
# dialog the fixed rules would NOT auto-approve (#241 §2: the reasoner decides even irreversible
# asks). It runs in run_answerer's read-only snapshot copy and answers 'ANSWER: APPROVE' or
# 'ANSWER: DENY: <reversible path>'. APPROVE delivers Yes; DENY (or any unclear reply — the safe
# default) declines the dialog and injects the reversible-path guidance. Either way the taken
# decision is warned + journaled with its reversibility class, and the spoke is NEVER parked.
_reason_permission() {
  local wt="$1" issue="$2" cmd="$3" why="$4" q raw rc ans text rev guidance
  q="The spoke is parked on a PERMISSION dialog and wants to run this command:

$cmd

The mechanical classifier would not auto-approve it ($why). Decide: APPROVE only if it is
safe, reversible, and in-scope (touches the spoke's own worktree; no default branch, no
force-push, no history rewrite, no deletion outside the worktree, no outward/network action);
otherwise DENY. NEVER approve an irreversible, destructive, or outward command — DENY it and
name the reversible path. Your ANSWER line MUST begin with 'APPROVE' or 'DENY: <the reversible
path to tell the spoke>'."
  # Stamp the attempt FIRST so the reason→deliver window never reads as idle (#202 C).
  stamp_answer_attempt "$issue"
  raw="$(run_answerer "$issue" "$q" "$wt")"; rc=$?
  # #247: run_answerer streams stream-json; normalize ONCE to the final text for the DECISION
  # parsers. is_auth_failure reads the RAW stream (below) so an auth signature in a dropped event
  # is never missed.
  local raw_text; raw_text="$(_normalize_answerer_output "$raw")"
  # Auth failure is the one true external blocker (#73): a dead supervisor token yields an
  # auth-error blob, not a decision — and parse_decision would fall to the DENY default and
  # inject a SPURIOUS denial into the live dialog. Detect it (rc != 0 AND an auth signature),
  # raise the global halt flag (the supervisor pauses DISPATCH + re-probes, #241 §9), and return
  # without injecting. #241 §9: WARN the spoke (an auth failure is not the spoke's fault — never
  # block it); the drain resumes servicing it once auth recovers.
  if [ "$rc" -ne 0 ] && is_auth_failure "$raw"; then
    _AFK_AUTH_FAILED=1
    broker_warn_continue "$wt" "$issue" auth "subscription auth failed — token could not refresh; re-run /login on the host (drain paused, re-probing)" reversible
    return 0
  fi
  ans="$(parse_decision "$raw_text")"
  text="${ans#*$'\t'}"
  rev="$(parse_decision_field "$raw_text" REVERSIBILITY)"
  # NB: the classifier verdict (ESCALATE) is already recorded in decisions.log by the caller; the
  # reasoned approve/deny is journaled here (a FILE line before the keypress + a gh comment after,
  # via _reason_permission_record), NOT in decisions.log — that log codifies only the MECHANICAL
  # classifier (#155 D).
  case "$text" in
    APPROVE*)
      # #241 review B1: journal the decision to the FILE BEFORE approve_permission delivers the
      # keypress (durable if the inject crashes/races the command it authorized) — file-only, so
      # no network gh-comment sits on the spoke's unblock critical path. The pre-keypress line is
      # PROVISIONAL ("APPROVING", present-continuous) so a per-record read can never mistake the
      # in-flight intent for a delivered-and-ran approval; the OUTCOME line (delivered / FAILED)
      # is written after (#241 review).
      _broker_journal_line "$issue" permission "reasoner APPROVING (delivery pending): $cmd" "${rev:-unknown}"
      if approve_permission "$wt"; then
        _broker_journal_line "$issue" permission "reasoner APPROVED (delivered): $cmd" "${rev:-unknown}"
        _reason_permission_record "$wt" "$issue" "reasoner APPROVED (delivered): $cmd" "${rev:-unknown}"
      else
        # A delivery failure is distinct on the DURABLE surfaces (a FAILED journal line + gh),
        # so the morning review never reads an undelivered approval as "authorized and ran".
        _broker_journal_line "$issue" permission "reasoner APPROVED but delivery FAILED: $cmd" "${rev:-unknown}"
        _reason_permission_record "$wt" "$issue" "reasoner APPROVED but delivery FAILED: $cmd" "${rev:-unknown}"
      fi ;;
    *)
      # DENY, or any reply that does not clearly approve — the safe default is to decline. Only a
      # DENY-prefixed reply carries guidance (with or without the colon); anything else uses the
      # default decline message rather than injecting the raw reply.
      case "$text" in
        DENY*)
          guidance="${text#DENY}"; guidance="${guidance#:}"
          guidance="${guidance#"${guidance%%[![:space:]]*}"}" ;;   # ltrim
        *) guidance="" ;;
      esac
      [ -n "$guidance" ] || guidance="Declined that command — take the reversible, in-scope path instead."
      # B1 generalized to DENY (#241 review): a provisional FILE line before _deny_permission
      # injects (survives a crash between inject and record), then the OUTCOME. The delivery rc is
      # NOT swallowed: a failed redirect (dead pane / failed inject) is journaled DISTINCTLY, so a
      # review never reads a stuck spoke as cleanly redirected. Decline-and-redirect is reversible
      # by construction, so default the class to reversible.
      _broker_journal_line "$issue" permission "reasoner DENYING (redirect pending) ($cmd): $guidance" "${rev:-reversible}"
      if _deny_permission "$wt" "$guidance"; then
        _broker_journal_line "$issue" permission "reasoner DENIED ($cmd): $guidance" "${rev:-reversible}"
        _reason_permission_record "$wt" "$issue" "reasoner DENIED ($cmd): $guidance" "${rev:-reversible}"
      else
        _broker_journal_line "$issue" permission "reasoner DENIED but redirect delivery FAILED ($cmd): $guidance" "${rev:-reversible}"
        _reason_permission_record "$wt" "$issue" "reasoner DENIED but redirect delivery FAILED ($cmd): $guidance" "${rev:-reversible}"
      fi ;;
  esac
}

# _decide_permission <wt_path> <issue> -> classify the spoke's pending permission dialog and act.
# AUTO-APPROVE a safe scoped self-op (mechanical fast path, unchanged, unwarned). Anything the
# fixed rules will not auto-approve — an ESCALATE verdict or an unreadable command — no longer
# parks the spoke: it routes to the always-answering reasoner (#241) which approves a safe
# command or declines-and-redirects a risky one, warning + journaling the taken decision.
_decide_permission() {
  local wt="$1" issue="$2" cmd cmd_display decision kind reason
  cmd="$(extract_pending_command "$wt")"
  if [ -z "$cmd" ]; then
    # Unreadable command: cannot classify. Decline it (the reversible action) + warn — never
    # park. The spoke gets a denial and keeps going; the backoff paces any retry.
    stamp_answer_attempt "$issue"
    _deny_permission "$wt" "Declined an unreadable permission command — re-issue it in a clearer form." || true
    broker_warn_continue "$wt" "$issue" permission "declined an unreadable permission command" reversible
    return 0
  fi
  # #257: classify the WHOLE command (uncapped) so a risky tail past 2000 chars can't hide behind
  # a benign prefix. The 2000-char cap is DISPLAY-only now, applied to a copy used solely for the
  # log/codify surfaces HERE (log_decision's signature + the drain log line) — kept byte-identical
  # to pre-fix. The classifier and the _reason_permission prompt get the full $cmd; the reasoner
  # path (its file journal + gh comment) then deliberately carries the untruncated command on its
  # OWN surfaces, so a human reviewing a genuine escalation sees the whole thing.
  cmd_display="${cmd:0:2000}"
  decision="$(classify_permission "$cmd" "$wt")"
  kind="${decision%%$'\t'*}"
  reason="${decision#*$'\t'}"
  # Record the classifier's VERDICT (both APPROVE and ESCALATE) for the codification pass,
  # not just successful approvals — otherwise every logged line is APPROVE and codify's
  # unanimity check is vacuous. Logging both makes a flag-dependent signature (`git reset
  # -q` APPROVE vs `git reset --hard` ESCALATE, which share the signature git-reset+git-add)
  # correctly read as a CONFLICT, so codify never proposes it as a safe unanimous rule (#155 D).
  log_decision "$issue" permission "$cmd_display" "$kind"
  if [ "$kind" = "APPROVE" ]; then
    log "→ auto-approving safe permission for #$issue: $cmd_display"
    # Stamp the delivery attempt FIRST: the approve→resume window must not read as idle.
    stamp_answer_attempt "$issue"
    if approve_permission "$wt"; then
      log "  approved permission for #$issue"
      afk_emit_decision "$wt" success
      return 0
    fi
    # Delivery failed — warn + retry on the backoff, never park (#241).
    broker_warn_continue "$wt" "$issue" permission "could not deliver the approval to the spoke — will retry" reversible
    return 0
  fi
  # ESCALATE: the fixed rules will not auto-approve this one. The reasoner decides it (#241) —
  # approve a safe/reversible command, or decline an irreversible one and name the reversible
  # path — and warns + journals the taken decision. Never park.
  _reason_permission "$wt" "$issue" "$cmd" "$reason"
}

# --- programmatic PreToolUse permission decision (issue #253) ------------------
# The pane-answering path above (extract_pending_command + _pane_shows_permission_prompt +
# approve_permission) detects and OPERATES a TUI dialog after it appears — the brittle surface
# behind the #240/#246/#238 bug family (new dialog shapes, glyphs, and timing windows keep
# breaking the scraper). afk_permission_hook_decide moves the COMMON case OFF the pane entirely:
# a spoke-side PreToolUse hook runs classify_permission on the gated tool call BEFORE any dialog
# and AUTO-APPROVES a benign scoped self-op, so no dialog is ever shown and there is nothing to
# scrape. It reuses the SAME classify_permission verdict (one source of truth), journals the
# auto-approve per #241, and NEVER denies: an ESCALATE — or any un-gated context — stays silent
# (exit 0, no output), so the existing scope-guard hooks' denies remain authoritative and the
# rare genuine escalation still falls through to the drain reasoner / pane path. (A2 — the hook
# itself reasons and returns deny-with-reason to fully retire the pane — is a deferred follow-up.)
#
# COMPOUND LIMIT (#259): Claude Code evaluates a compound Bash command PER-SEGMENT against
# permissions.allow (deny > ask > allow > default-prompt), and this whole-command `allow` does
# NOT satisfy that per-segment check. So the hook suppresses the dialog only for a STANDALONE
# benign op; a compound whose tail segment matches no allow rule (the #238 `chmod +x X && ./X`)
# still prompts despite the allow. The deterministic layer for that class is worktree-new.sh's
# dispatch-time exec-lane seed (`Bash(./:*)`) — this fn stays the standalone + #241-journal path.

# _afk_supervisor_live <wt> -> rc 0 when a LIVE /afk supervisor heartbeat governs <wt>. This is
# the hook's self-limit: it auto-approves ONLY inside a running drain, never in an attended
# session. Mirrors afk-notify-wake.sh's gate — the .afk-heartbeat pidfile in the git-common-dir
# (AFK_HEARTBEAT overrides for tests) names a running pid. Fails CLOSED (rc 1) on any gap.
_afk_supervisor_live() {
  local wt="$1" common hb pid
  common="$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null)" || return 1
  case "$common" in /*) ;; *) common="$wt/$common" ;; esac   # rev-parse may print a relative dir
  hb="${AFK_HEARTBEAT:-$common/.afk-heartbeat}"
  [ -f "$hb" ] || return 1
  read -r pid _ < "$hb" 2>/dev/null || return 1
  case "$pid" in '' | *[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2>/dev/null
}

# _afk_hook_emit_allow <reason> -> print the PreToolUse allow verdict. Mirrors chmod-scope-guard's
# shape: hookSpecificOutput.permissionDecision for Claude Code + a top-level `permission` for
# Cursor's beforeShellExecution, so the auto-approve is understood on both. jq when present, a
# hand-rolled literal otherwise.
_afk_hook_emit_allow() {
  local reason="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -nc --arg r "$reason" '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "allow",
        permissionDecisionReason: $r
      },
      permission: "allow"
    }'
  else
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"%s"},"permission":"allow"}\n' "$reason"
  fi
}

# _afk_hook_emit_deny <reason> -> print the PreToolUse deny verdict (issue #261). Mirrors
# _afk_hook_emit_allow's dual shape (Claude hookSpecificOutput + a top-level Cursor `permission`).
# The reasons this hook emits are controlled ASCII category strings (the resolver already rejected
# quote/metachar paths), so the hand-rolled fallback needs no JSON escaping -- same contract as
# _afk_hook_emit_allow.
_afk_hook_emit_deny() {
  local reason="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -nc --arg r "$reason" '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: $r
      },
      permission: "deny"
    }'
  else
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"},"permission":"deny"}\n' "$reason"
  fi
}

# afk_permission_hook_decide -> read a Claude Code PreToolUse payload on stdin and print an
# `allow` verdict IFF classify_permission APPROVEs the gated tool call inside a live drain;
# otherwise print nothing. Always rc 0 (a PreToolUse allow-only hook must never fail a session).
# The command string is rebuilt EXACTLY as extract_pending_command does (Bash -> its command;
# Read -> "Read <file_path>"; any other tool -> the tool name) so the hook and the pane path
# classify identically. Gated on a spoke branch (issue-numbered slug) AND a live supervisor, so
# an attended session and the hub checkout are untouched.
afk_permission_hook_decide() {
  local payload wt cmd parsed br slug issue decision kind
  payload="$(cat)"
  command -v python3 >/dev/null 2>&1 || return 0
  # One python pass: line 1 = cwd, the remainder = the classifier command string (a Bash command
  # may itself contain newlines, so cmd is everything AFTER the first line, not just line 2).
  parsed="$(_AFK_HOOK_PAYLOAD="$payload" python3 2>/dev/null <<'PYEOF'
import json, os

try:
    obj = json.loads(os.environ.get("_AFK_HOOK_PAYLOAD") or "{}")
except Exception:
    obj = {}
if not isinstance(obj, dict):
    obj = {}
name = (obj.get("tool_name") or "").strip()
inp = obj.get("tool_input")
if not isinstance(inp, dict):
    inp = {}
cwd = (obj.get("cwd") or "").strip()
if name == "Bash":
    cmd = (inp.get("command") or "").strip()
elif name == "Read":
    fp = (inp.get("file_path") or "").strip()
    cmd = f"{name} {fp}" if fp else name
elif name:
    cmd = name
else:
    cmd = ""
print(cwd)
# NB: NOT truncated -- the 2000-char cap lives in the _decide_permission cmd_display log-only
# copy, never in extract_pending_command since #257, because a silent auto-approve must classify
# the WHOLE command. Truncating a benign prefix off a risky tail could hide the risky segment
# and mis-approve it with no dialog. Since
# classify_permission is default-deny, an over-long or unrecognised command just escalates.
# (Plain ASCII + no backticks/parens here: bash 3.2 mis-parses those inside a $()-nested
# heredoc.)
print(cmd.strip())
PYEOF
)"
  # No newline ⇒ python emitted only the cwd line (empty command) — nothing to vouch for.
  case "$parsed" in *$'\n'*) ;; *) return 0 ;; esac
  # Line 1 is the cwd, the rest is the command. This assumes the payload cwd has no embedded
  # newline (Claude Code sets it to a real dir path, never attacker-controlled). If one ever did,
  # wt takes only the first line and the remainder prepends to cmd — which only makes classify
  # STRICTER (extra bogus segments) and fails a bad `git -C "$wt"` below, so it fails CLOSED.
  wt="${parsed%%$'\n'*}"
  cmd="${parsed#*$'\n'}"
  [ -n "$cmd" ] || return 0
  [ -n "$wt" ] || wt="$(pwd)"
  # Spoke self-limit: an issue-numbered branch slug AND a live supervisor. Either missing ⇒ stay
  # silent so the normal permission flow (and any attended user) is untouched.
  br="$(git -C "$wt" branch --show-current 2>/dev/null)" || return 0
  slug="${br##*/}"; issue="${slug%%[!0-9]*}"
  case "$issue" in '' | *[!0-9]*) return 0 ;; esac
  _afk_supervisor_live "$wt" || return 0
  decision="$(classify_permission "$cmd" "$wt")"
  kind="${decision%%$'\t'*}"
  # NEVER deny: only APPROVE emits a verdict; ESCALATE (or anything else) stays silent so the
  # scope-guard denies stay authoritative and the reasoner/pane path still handles the rare case.
  [ "$kind" = APPROVE ] || return 0
  # #241: journal the hook-layer auto-approve (file only — a per-approve gh comment would be
  # spam) BEFORE emitting the verdict, so a decision made with no dialog is auditable. A hook
  # auto-approve is a benign scoped self-op by construction, hence reversible.
  _broker_journal_line "$issue" permission "hook auto-approved: $cmd" reversible
  _afk_hook_emit_allow "afk-permission-hook: classify_permission APPROVEd a benign scoped self-op inside a live drain — auto-allowed (no dialog; ESCALATE and everything else still prompt)"
}

# --- programmatic PreToolUse deny-wall (issue #261) ---------------------------
# Under bypassPermissions an afk spoke raises NO permission dialog (worktree-new.sh --mode afk),
# so a PreToolUse deny-hook is the ONLY safety boundary -- and a deny-hook STILL fires and its
# permissionDecision:"deny" is honored under bypass (proven on CC v2.1.207). afk_danger_guard_decide
# is that wall's decision fn: read a PreToolUse payload, run three tiers, DENY the dangerous ones:
#   Tier 2  classify_danger == DENY        -> journal + emit permissionDecision:"deny"  (deny-first)
#   Tier 1  classify_permission == APPROVE -> silent allow (bypass runs it; no judge, no journal)
#   Tier 3  judge_permission               -> DANGEROUS/fail-closed => journal + deny; SAFE => allow
# Tier 2 runs BEFORE Tier 1 on purpose: classify_permission (built for the old prompt-approve
# model) APPROVEs any read verb, so a `cat ~/.ssh/id_rsa` secret read would be Tier-1-approved and
# never reach the deny list -- checking classify_danger first closes that gap; both static checks
# are cheap (no LLM), so deny-first costs nothing. It reuses the SAME classifiers the drain trusts
# (one source of truth). afk-permission-hook (#253) is left in place and untouched -- its allow is
# redundant-but-harmless under bypass; THIS hook only adds DENY (which wins in CC).

# _afk_spoke_mode <wt> -> print the spoke's execution mode from <root>/.ai-toolkit/mode
# (whitespace-trimmed), or empty when the file is missing / unreadable / <wt> is not a git tree.
# The mode is the load-bearing signal for the deny-wall gate (see afk_danger_guard_decide): the
# file is written by worktree-new.sh at spawn and is gitignored (info/exclude), so it SURVIVES a
# branch checkout / detach -- unlike the branch name, which does not.
_afk_spoke_mode() {
  local wt="$1" root
  root="$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null)" || return 0
  [ -n "$root" ] || return 0
  [ -f "$root/.ai-toolkit/mode" ] || return 0
  tr -d '[:space:]' < "$root/.ai-toolkit/mode" 2>/dev/null || return 0
}

# afk_danger_guard_decide -> read a Claude Code PreToolUse payload on stdin and print a `deny`
# verdict for a boundary-crossing / judge-dangerous command inside an afk (bypass) spoke; else
# print nothing (the command runs under bypass). Always rc 0 (a PreToolUse hook must never fail a
# session). The command string is rebuilt EXACTLY as extract_pending_command / afk_permission_hook_
# decide do, so all three classify identically. Gated on an issue-numbered spoke branch AND the
# fail-safe mode gate (never the hub / ad-hoc / positively-attended).
afk_danger_guard_decide() {
  local payload parsed wt cmd br slug issue decision djson dreason verdict vkind vreason
  payload="$(cat)"
  command -v python3 >/dev/null 2>&1 || return 0
  parsed="$(_AFK_HOOK_PAYLOAD="$payload" python3 2>/dev/null <<'PYEOF'
import json, os

try:
    obj = json.loads(os.environ.get("_AFK_HOOK_PAYLOAD") or "{}")
except Exception:
    obj = {}
if not isinstance(obj, dict):
    obj = {}
name = (obj.get("tool_name") or "").strip()
inp = obj.get("tool_input")
if not isinstance(inp, dict):
    inp = {}
cwd = (obj.get("cwd") or "").strip()
if name == "Bash":
    cmd = (inp.get("command") or "").strip()
elif name == "Read":
    fp = (inp.get("file_path") or "").strip()
    cmd = f"{name} {fp}" if fp else name
elif name:
    cmd = name
else:
    cmd = ""
print(cwd)
# NOT truncated: a deny-wall must classify the WHOLE command -- a truncated benign prefix could
# hide a risky tail. Plain ASCII, no backticks in this comment: bash 3.2 mis-parses those nested.
print(cmd.strip())
PYEOF
)"
  case "$parsed" in *$'\n'*) ;; *) return 0 ;; esac
  wt="${parsed%%$'\n'*}"; cmd="${parsed#*$'\n'}"
  [ -n "$cmd" ] || return 0
  [ -n "$wt" ] || wt="$(pwd)"
  # Issue number (best-effort, for the fail-safe gate + the journal). Empty on a detached HEAD
  # or a non-issue branch -- which is EXACTLY why it must NOT be the primary gate.
  br="$(git -C "$wt" branch --show-current 2>/dev/null || true)"
  slug="${br##*/}"; issue="${slug%%[!0-9]*}"
  case "$issue" in *[!0-9]*) issue="" ;; esac
  # MODE GATE (fail-safe, mode-first -- #261 review BLOCKER). A positively-read `afk` mode means
  # this spoke launched under bypassPermissions, so the wall is ACTIVE on ANY branch: `git bisect`
  # / `rebase` / `checkout <sha>` detach HEAD or move off the issue branch, and the wall must NOT
  # silently drop then (the .ai-toolkit/mode file survives the checkout; the branch name does not).
  # `attended` -> INERT (the human is the wall). A missing / unreadable / ambiguous mode keeps the
  # wall ACTIVE only for an issue-numbered spoke branch (a corrupted spoke); the hub (on main, no
  # mode file) and ad-hoc lanes stay INERT so hub operations are never walled.
  case "$(_afk_spoke_mode "$wt")" in
    attended) return 0 ;;
    afk) ;;
    *) [ -n "$issue" ] || return 0 ;;
  esac
  # Tier 2 (static deny) first -- see the header for why it precedes Tier 1.
  djson="$(classify_danger "$cmd" "$wt")"
  if [ "${djson%%$'\t'*}" = DENY ]; then
    dreason="${djson#*$'\t'}"
    _broker_journal_line "$issue" permission "tier2 deny: $cmd -- $dreason" scope
    _afk_hook_emit_deny "afk-danger-guard tier-2: $dreason"
    return 0
  fi
  # Tier 1 -- a benign scoped self-op the deny list already cleared: allow silently, skip the judge.
  decision="$(classify_permission "$cmd" "$wt")"
  [ "${decision%%$'\t'*}" = APPROVE ] && return 0
  # Tier 3 -- the toolless LLM judge on the residue. Fail-closed (DANGEROUS) => deny.
  verdict="$(judge_permission "$cmd" "$issue")"
  vkind="${verdict%%$'\t'*}"
  if [ "$vkind" = SAFE ]; then
    _broker_journal_line "$issue" permission "tier3 judge SAFE: $cmd" reversible
    return 0
  fi
  vreason="${verdict#*$'\t'}"
  _broker_journal_line "$issue" permission "tier3 judge DENY: $cmd -- $vreason" scope
  _afk_hook_emit_deny "afk-danger-guard tier-3: $vreason"
  return 0
}

# --- tmux injection + telemetry -----------------------------------------------

# _scan_appended_turns <wt_path> <sizes> <mode> -> scan the transcript bytes APPENDED after the
# <sizes> snapshot for a matching record. <mode> selects the filter:
#   typed    — ONLY a genuine typed prompt submission (type:"user", promptSource=="typed", not
#              isMeta); SYNTHETIC harness user turns (tool_results, <system-reminder> /
#              <task-notification>, skill/meta, SDK/system) do NOT match — the #240 non-turn class.
#   activity — the above OR the spoke's OWN assistant work (an assistant record with a tool_use,
#              e.g. Edit/Write/Bash, or non-empty text).
# rc 0 a match landed, rc 1 none, rc 2 unavailable (no python3 / no project dir / interpreter crash).
# Only appended regions are read (offset from <sizes>); a rotated/truncated file rescans from 0.
_scan_appended_turns() {
  local wt="$1" sizes="$2" mode="$3" dir
  dir="$(_spoke_project_dir "$wt")"
  [ -d "$dir" ] || return 2
  command -v python3 >/dev/null 2>&1 || return 2
  _AFK_DIR="$dir" _AFK_SIZES="$sizes" _AFK_MODE="$mode" python3 2>/dev/null <<'PYEOF'
import glob, json, os, sys

mode = os.environ.get("_AFK_MODE", "activity")
offsets = {}
for line in os.environb.get(b"_AFK_SIZES", b"").splitlines():
    size, _, path = line.partition(b"\t")
    if path:
        try:
            offsets[os.fsdecode(path)] = int(size)
        except ValueError:
            pass


def matches(record):
    if not isinstance(record, dict):
        return False
    # `any` mode: ANY appended record is a spoke-side write — the ISOLATED reasoner (#237 cwd=snap,
    # --no-session-persistence) never writes the live transcript, so a #240 tool_result-only
    # self-resume still proves the spoke touched the tree (#247 residual 2, the fail-safe's DROP arm).
    if mode == "any":
        return True
    kind = record.get("type")
    # A genuine typed human/self reply — shared by both modes (mirrors _gate_answer_landed).
    if kind == "user" and record.get("promptSource") == "typed" and not record.get("isMeta"):
        return True
    if mode == "typed":
        return False
    # activity mode also counts the spoke's OWN assistant work: a tool_use (it ran Edit/Write/
    # Bash) or a non-empty text turn. The isolated reasoner never writes the live transcript.
    if kind == "assistant":
        msg = record.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    return True
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    return True
    return False


for path in glob.glob(os.path.join(os.environ["_AFK_DIR"], "*.jsonl")):
    try:
        with open(path, "rb") as fh:
            offset = offsets.get(path, 0)
            fh.seek(0, 2)
            if offset > fh.tell():  # rotated/truncated since the snapshot
                # typed mode fails toward a from-0 rescan (fail-toward-pre-#201). activity mode must
                # NOT: a from-0 scan would match the PRE-park record (an AskUserQuestion IS an
                # assistant tool_use) and mask a real escape (rc 0 -> drop). Skip the file instead,
                # so the caller reads "no activity" (rc 1) and fails SAFE (voids) on a lost boundary.
                if mode in ("activity", "any"):
                    continue
                offset = 0
            fh.seek(offset)
            appended = fh.read()
    except OSError:
        continue
    for line in appended.splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        if matches(record):
            sys.exit(0)
sys.exit(3)
PYEOF
  case $? in 0) return 0 ;; 3) return 1 ;; *) return 2 ;; esac
}

# _user_turn_appended <wt_path> <sizes> -> did a GENUINE typed reply land in transcript bytes
# appended after the <sizes> snapshot? The "the spoke MOVED ON" signal (#241 §4). The staleness
# recompute gates on the DEFINITE "no genuine reply" (rc 1); rc 0 (a typed reply landed) or rc 2
# (cannot tell) both fall to the #89-safe drop. rc 0 found, rc 1 none, rc 2 unavailable.
_user_turn_appended() { _scan_appended_turns "$1" "$2" typed; }

# _spoke_activity_appended <wt_path> <sizes> -> did a GENUINE spoke turn (a typed reply OR the
# spoke's OWN assistant work) land in appended transcript bytes? The read-only void's #244
# discriminator: a spoke that self-resumed mid-GREEN and edited its own tree ALWAYS leaves a turn
# here, while the isolated reasoner (#237 cwd=snap, --no-session-persistence) never writes the live
# transcript and a #240 non-turn bump / a HEAD-moving commit-escape is NOT a spoke turn. So a tree
# diff with NO activity (rc 1) is a reasoner escape (VOID). rc 2 (cannot scan) is treated as a
# breach too — fail SAFE, mirroring the unverifiable-fingerprint escalation. rc 0 found, rc 1 none,
# rc 2 unavailable (no python3 / no project dir / crash).
_spoke_activity_appended() { _scan_appended_turns "$1" "$2" activity; }

# _spoke_touched_transcript <wt_path> <sizes> -> did the spoke append ANY record to its live
# transcript since the <sizes> snapshot? The #247 fail-safe's positive spoke signal: a weaker bar
# than a full "turn" (it also counts a #240 tool_result-only self-resume — residual 2), sound
# because the ISOLATED reasoner never writes the live transcript, so any appended record is the
# spoke. Used when the reasoner audit did NOT prove a write (rw_rc 1/2): DROP on a positive touch,
# else fail SAFE and VOID. rc 0 touched, rc 1 none, rc 2 unavailable (no python3 / no project dir).
_spoke_touched_transcript() { _scan_appended_turns "$1" "$2" any; }

# _reasoner_wrote_live_tree <raw-answerer-output> <wt> -> the #247 option (c) attribution
# primitive: audit the REASONER's OWN tool_use stream (from `--output-format stream-json`) for a
# write that could reach the LIVE tree, instead of attributing a whole-tree diff by the spoke's
# transcript (the #244 discriminator, which is leaky at the edges because the diff carries no
# evidence of WHO wrote it). Since #237 the reasoner runs in a snapshot COPY (cwd=snap), so its
# RELATIVE writes land in the copy and never touch the live tree; the ONLY live-tree vector is an
# ABSOLUTE path under <wt> (a write tool targeting $wt/…, or a mutating Bash referencing the
# absolute $wt path / `git -C $wt`). This scans for exactly that:
#   - a write tool (Write/Edit/MultiEdit/NotebookEdit) whose path input is absolute and under <wt>;
#   - a Bash whose command references the absolute <wt> path AND is NOT a read-only git verb
#     (mirrors _reasoner_bash_readonly, and also recognises the `git -C <wt> <verb>` form).
# rc 0 a live-tree write is PROVEN (VOID even amid coincident spoke activity — closes residual 1);
# rc 1 the stream parsed but shows NO modelled live-tree write; rc 2 the input is not an auditable
# stream (a plain-text answerer stub / no stream / no python3). The caller treats ONLY rc 0 as proof:
# on rc 1 / rc 2 it does NOT trust the audit alone (an escape via a vector this does not model must
# still fail SAFE), so it attributes the diff to the spoke ONLY on a positive spoke-transcript signal
# and otherwise VOIDs. The raw stream is delivered via a temp FILE (not argv/env) so a verbose
# stream that echoes large tool_result payloads never trips ARG_MAX. Uses python3 like the scanners.
_reasoner_wrote_live_tree() {
  local raw="$1" wt="$2" rawfile rc
  command -v python3 >/dev/null 2>&1 || return 2
  # Deliver the raw stream via a temp FILE (path in env), never in argv/env directly: a verbose
  # stream that echoes large tool_result payloads would trip ARG_MAX. mktemp mirrors run_answerer.
  rawfile="$(mktemp 2>/dev/null)" || return 2
  printf '%s' "$raw" > "$rawfile"
  _AFK_WT="$wt" _AFK_RAWFILE="$rawfile" python3 2>/dev/null <<'PYEOF'
import json, os, re, sys

wt = os.environ.get("_AFK_WT", "")
cands = {c for c in (wt, os.path.realpath(wt) if wt else "") if c}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
RO_GIT = ("status", "diff", "log", "show", "rev-parse", "branch", "ls-files", "cat-file")


def path_under_wt(p):
    if not isinstance(p, str) or not p.startswith("/"):
        return False  # a relative path writes into the #237 snapshot copy, not the live tree
    # Compare both the raw and the symlink-resolved form so a /tmp-vs-/private/tmp alias (or any
    # symlinked component) on either side still matches — cands already carries realpath(wt).
    forms = {p}
    try:
        forms.add(os.path.realpath(p))
    except Exception:
        pass
    return any(f == c or f.startswith(c.rstrip("/") + "/") for f in forms for c in cands)


def references_wt(text):
    # Boundary-aware: <wt> followed by a non-path char (/ , whitespace, quote, EOL) — never a bare
    # substring, so a SIBLING worktree like `<wt>-2` / `<wt>.bak` is not mistaken for the live tree.
    return any(re.search(re.escape(c) + r"(?![\w.-])", text) for c in cands)


def bash_mutates_wt(cmd):
    if not isinstance(cmd, str) or not references_wt(cmd):
        return False  # does not reference the absolute live-tree path at all
    # Command chaining / an output redirect could smuggle a write past a leading read-only verb
    # (`git -C $wt status && rm $wt/x`, `... > $wt/x`) — treat such a compound as a mutation. A bare
    # pipe is NOT included: `git -C $wt log | head` stays a read. (The reasoner is never told $wt's
    # absolute path — #239 — so ANY command referencing it is already off-posture; voiding is safe.)
    if any(t in cmd for t in (">", ";", "&&", "||", "$(", "`")):
        return True
    m = re.match(r"git\s+(?:-C\s+\S+\s+)?(\S+)", cmd.strip())
    if m and m.group(1) in RO_GIT:
        return False  # a LONE read-only `git [-C <wt>] status/diff/…` inspection — cannot mutate
    return True  # any other command referencing the absolute live path is a potential live write


saw_stream = False
with open(os.environ["_AFK_RAWFILE"], encoding="utf-8", errors="replace") as fh:
    for raw_line in fh:
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue  # non-JSON (a plain-text answerer stub) — not an auditable stream event
        if not isinstance(obj, dict):
            continue
        if obj.get("type") in ("system", "assistant", "user", "result"):
            saw_stream = True
        if obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            inp = block.get("input") or {}
            if not isinstance(inp, dict):
                continue
            if name in WRITE_TOOLS and any(path_under_wt(inp.get(k)) for k in ("file_path", "path", "notebook_path")):
                sys.exit(0)
            if name == "Bash" and bash_mutates_wt(inp.get("command")):
                sys.exit(0)
sys.exit(3 if saw_stream else 4)
PYEOF
  rc=$?
  rm -f "$rawfile" 2>/dev/null || true
  case "$rc" in 0) return 0 ;; 3) return 1 ;; *) return 2 ;; esac
}

# afk_emit_decision <wt_path> <status> -> one kind=agent span per auto-answer decision,
# attributed to the SPOKE (emit with the worktree as CWD, like worktree-lib does), so the
# decision surfaces on the observability dashboard. Metadata only — the question→answer
# text rides the answerer's own sidecar session (the dashboard's node summary), never the
# span (the telemetry privacy contract logs no payload). No-op when telemetry is off.
# _afk_emit_span <wt> <name> <status> -> the shared one-span emitter (kind=agent, phase
# review), attributed to the spoke. No-op when telemetry is off or the worktree is gone.
_afk_emit_span() {
  command -v telemetry_emit_span >/dev/null 2>&1 || return 0
  local wt="$1" name="$2" status="$3"
  [ -d "$wt" ] || return 0
  ( cd "$wt" && telemetry_emit_span --kind agent --name "$name" --phase review --status "$status" ) || true
  return 0
}
afk_emit_decision() { _afk_emit_span "$1" afk-answer "$2"; }

# _consume_gate_tag <wt_path> <issue> -> drop the gate/<issue> marker once a PLAN-gate
# answer has been injected. slot_state reads the LOCAL tag at the tip, so deleting the local
# tag is what closes the window between "answered" and the spoke committing its first code
# (the tip still equals the gate commit until then, and an untouched tag would re-read as
# waiting and re-answer the same gate). The remote delete is cosmetic (dashboard /
# hub-status) and best-effort. Never aborts the loop.
_consume_gate_tag() {
  local wt="$1" issue="$2"
  git -C "$wt" tag -d "gate/$issue" >/dev/null 2>&1 || true
  git -C "$wt" push origin ":refs/tags/gate/$issue" >/dev/null 2>&1 || true
  # Drop the scripted plan artifact too (issue #175): once the gate is answered the plan
  # handoff is spent, and a lingering gate-<N>.md would feed a stale plan to a later re-park.
  rm -f "$(_gate_artifact_path "$wt" "$issue")" 2>/dev/null || true
}

# --- attended QCM surface (issue #155, subtask C) -----------------------------
# In attended mode a human-decision gate is serviced by an INTERACTIVE per-gate context
# that owns present + capture + inject (NOT the hub, which is only NOTIFIED via hub-notify
# #146). The upstream reasoning stays one-shot (run_answerer); when it returns a
# human-decision, _broker_present_qcm renders a structured QCM (summary + reviewer advice
# + freeform escape) on a dedicated per-gate surface, waits for the reviewer's reply HERE,
# and injects it into the spoke via the shared injector — off the pane, off the hub chat.
# One interactive context per gate; it closes after.

# _broker_qcm_dir -> the directory holding per-gate QCM surfaces. GATE_BROKER_QCM_DIR
# overrides (shared with hub-notify.sh, whose gate ping points the human at the surface).
_broker_qcm_dir() { printf '%s\n' "${GATE_BROKER_QCM_DIR:-$(_afk_state_dir)/gate-broker}"; }

# _broker_qcm_surface <issue> -> the per-gate QCM surface path.
_broker_qcm_surface() { printf '%s\n' "$(_broker_qcm_dir)/qcm-$1.md"; }

# _broker_qcm_clear <issue> -> drop a resolved gate's surface.
_broker_qcm_clear() { rm -f "$(_broker_qcm_surface "$1")" 2>/dev/null || true; }

# build_qcm <issue> <summary> <advice> -> write the structured QCM surface: the parked
# prompt (summary — the spoke's own options recommended-first, as it posted them), the
# reviewer's advice, and the freeform-escape instruction. Human-readable + a record, and
# its existence is the flag hub-notify keys the "resolve via QCM" ping on.
build_qcm() {
  local issue="$1" summary="$2" advice="$3" surface
  surface="$(_broker_qcm_surface "$issue")"
  mkdir -p "$(dirname "$surface")" 2>/dev/null || true
  cat > "$surface" <<EOF
# Gate broker · QCM for #$issue

## Summary — what the spoke is parked on

$summary

## Reviewer advice

$advice

## Your decision

Reply with the option you want (the spoke listed its own options above, recommended
first), or type any freeform instruction — it is injected verbatim into the spoke. An
empty reply defers the gate (escalated as blocked/$issue for later).
EOF
}

# _broker_present_qcm <wt> <issue> <advice> -> the ATTENDED human-decision route AND the
# interactive per-gate context: render the QCM, present it, read the reviewer's reply from
# THIS context's stdin, and inject it into the spoke via the shared injector. Empty reply
# -> defer (escalate). The hub only ever gets the hub-notify ping; the resolution happens
# here. UPGRADE: offer the spoke's discrete options as numbered one-key picks (parse them
# out of the summary) once the extract carries structured option labels.
_broker_present_qcm() {
  local wt="$1" issue="$2" advice="$3" summary reply target
  summary="$(extract_pending_question "$wt")"
  [ -n "$summary" ] || summary="(the spoke's parked prompt could not be extracted; decide from the advice + the issue contract)"
  build_qcm "$issue" "$summary" "$advice"
  {
    printf '\n=== Gate broker · #%s — resolve this gate ===\n\n' "$issue"
    printf '## Summary\n%s\n\n' "$summary"
    printf '## Reviewer advice\n%s\n\n' "$advice"
    printf 'Your reply (injected into the spoke; an empty reply defers the gate): '
  } >&2
  # `|| true`, NOT `|| reply=""`: an EOF that arrives right after a newline-less reply
  # returns non-zero with $reply already populated — clobbering it would turn a genuine
  # approval (typed then Ctrl-D) into a spurious block. The [ -z ] below still defers on a
  # truly empty/EOF reply.
  IFS= read -r reply || true
  if [ -z "$reply" ]; then
    _escalate_blocked "$wt" "$issue" "attended reviewer deferred the gate — $advice"
    _broker_qcm_clear "$issue"
    return 0
  fi
  target="$(_spoke_pane_target "$wt")"
  if [ -n "$target" ] && inject_and_verify "$wt" "$target" "$reply"; then
    log "  injected the reviewer's reply into #$issue"
    _consume_gate_tag "$wt" "$issue"
    afk_emit_decision "$wt" success
    _broker_qcm_clear "$issue"
  else
    _escalate_blocked "$wt" "$issue" "attended QCM: could not inject the reviewer's reply into the spoke — needs a human"
    _broker_qcm_clear "$issue"
  fi
  return 0
}

# decide_and_act <wt_path> <issue> -> reason about a parked spoke and act: inject the
# answer, or escalate to blocked/<issue>. Fail-safe: an answerer that returns no decision
# (or an answer we cannot inject) escalates rather than guessing.
broker_service_gate() {
  local wt="$1" issue="$2" mode="${3:-unattended}" depth="${4:-0}" question orig_question raw rc decision kind text target was_gate=0 inject_diagnosed=0
  # Self-heal a stale gate tag (issue #204): if gate/<issue> is at the tip but the spoke
  # already resumed past its PLAN gate (a late / external / attended approval that never ran
  # the confirmed-inject path), consume the stale tag and stop — do NOT re-answer, and do NOT
  # count it against the re-answer ceiling (checked BEFORE it, so a resumed spoke heals even
  # once exhausted). The plan-gate-guard self-heals the same signal from the spoke side.
  if _gate_parked "$wt" "$issue" && _gate_answer_landed "$wt"; then
    log "  #$issue resumed past its PLAN gate outside the broker — consuming the stale gate/$issue tag"
    _consume_gate_tag "$wt" "$issue"
    return 0
  fi
  # A prior tick found the reasoner mutated the live tree for this gate (#237). The mutation
  # perturbs the (tip, sig) ceiling every tick (a tree write flips the pending command), so a
  # DURABLE void marker — not the ceiling — is what throttles the mutating reasoner. #241 §5: the
  # void is no longer terminal-forever — it is BACKOFF-paced. Inside the warned-retry backoff:
  # skip (parked LAST). Once the backoff elapses: clear the marker for ONE supervised retry (if
  # the reasoner mutates again this tick it re-voids + re-arms a longer backoff). Checked before
  # the ceiling on purpose. A fresh arm clears both the marker and the backoff.
  if _broker_gate_voided "$issue"; then
    _afk_warned_due "$issue" || return 0                             # inside the backoff → parked LAST
    rm -f "$(_broker_voided_marker "$issue")" 2>/dev/null || true    # backoff elapsed → allow one retry
  fi
  # Re-answer ceiling (#203 finding 1): a legitimately-escalated spoke parked on the SAME
  # prompt must not re-run the reasoner/classifier every tick forever. After the ceiling on
  # the SAME (tip, prompt-signature) the gate is terminal — it stays blocked/<issue> at the
  # tip from the prior escalation — until the prompt changes or the tip moves. Checked before
  # BOTH the permission path (#203 finding 4's compound dialog) and the answerer path.
  local park_sig; park_sig="$(_broker_park_signature "$wt" "$issue")"
  if _broker_reanswer_exhausted "$wt" "$issue" "$park_sig"; then
    # #241 §5: the ceiling is no longer TERMINAL — it warns and retries on an exponential
    # backoff, so a doom-loop is throttled by the growing curve, not abandoned. On the FIRST
    # exhaustion: warn, arm the backoff, and pause. Inside the backoff window: skip (parked
    # LAST). Once the backoff elapses: warn, re-arm a longer backoff, and fall through for ONE
    # supervised retry (the counter stays exhausted, so each window yields a single re-run).
    local ws; ws="$(_afk_warned_state_file "$issue")"
    if [ ! -f "$ws" ]; then
      broker_warn "$issue" "re-answer ceiling reached on the same prompt — backing off (retried on the curve, #241)"
      broker_journal_decision "$issue" ceiling "re-answer ceiling reached; backing off on unchanged park" reversible
      _afk_warned_arm "$issue"
      return 0
    fi
    if ! _afk_warned_due "$issue"; then return 0; fi   # inside the backoff window → parked LAST
    broker_warn "$issue" "re-answer backoff elapsed — one supervised retry on the same prompt (#241)"
    broker_journal_decision "$issue" ceiling "re-answer backoff elapsed; supervised retry" reversible
    # ARM the next (longer) backoff HERE, before the retry runs, so the pause is guaranteed
    # regardless of the retry's OUTCOME. This is the ONLY arm that paces a MECHANICAL classifier
    # auto-approve (line ~2091) which, on success, leaves the same (tip, park-sig) intact and
    # neither arms nor clears — without it a re-appearing auto-approvable dialog would re-warn +
    # re-run every tick (hub-review B1-cluster regression). A retry that instead self-arms (reasoned
    # DENY / ESCALATE) advances the counter a SECOND time; that double-step only GROWS the backoff
    # (strictly more conservative, bounded by the cap) and is cleared the moment the tip advances
    # (genuine progress), so it never strands a recoverable spoke. A guard that suppressed the
    # second arm was tried and reverted (#241 review r2.2): a tick-scoped global leaked into the
    # reap/land passes that also call broker_warn_continue and into the depth+1 recursion, which
    # was strictly worse than the benign double-step it removed.
    _afk_warned_arm "$issue"
    # fall through for one supervised retry; the arm above paces the next
  fi
  # A pending permission dialog is decided by the rules classifier, not the answerer (#149).
  if _permission_pending "$wt"; then _decide_permission "$wt" "$issue"; return; fi
  # Snapshot the transcript clock BEFORE the park checks: a write landing between
  # this and the pre-inject re-check must count as movement (review nit, ST2).
  local parked_mtime; parked_mtime="$(_transcript_mtime "$wt")"
  local parked_sizes; parked_sizes="$(_transcript_sizes "$wt")"   # #241 §4: detect a real reply vs a non-turn write
  _gate_parked "$wt" "$issue" && was_gate=1
  orig_question="$(extract_pending_question "$wt")"
  question="$orig_question"
  if [ "$was_gate" -eq 1 ]; then
    # Route a PLAN-gate park to approve/amend-the-POSTED-PLAN — generic transcript
    # re-extraction is what replayed the seed six times in #124. PREFER the scripted plan
    # artifact (issue #175: a script reads what a script wrote) over transcript extraction;
    # orig_question (the transcript walk) stays as the fallback for an unextractable gate
    # park (rotated transcript, no gate Bash record) or a bare --gate that wrote no artifact.
    local plan; plan="$(_read_gate_artifact "$wt" "$issue")"
    [ -n "$plan" ] || plan="$orig_question"
    question="The spoke is parked at its PLAN gate; below is the plan it posted. Approve it or state precise amendments to it. Do NOT restate or re-issue the task itself.

${plan:-(the plan prose could not be extracted — approve or amend from the issue contract above)}"
  elif [ -z "$question" ]; then
    return 0
  fi
  log "→ answering #$issue (parked on input)"
  # Read-only guard (subtask B): fingerprint the LIVE worktree around the reason step.
  # Since #237 the reasoner runs in a throwaway snapshot COPY (cwd=snap), so its relative
  # writes land in the copy and the live-tree fingerprint is a should-never-fire backstop —
  # its one remaining true purpose is catching an ABSOLUTE-path escape (a reasoner tool
  # writing `$wt/…` / `git -C $wt`, which bypasses cwd=snap). Detection is the hard guarantee
  # independent of the LLM's tool-allowlist. #247: a live-tree diff is now ATTRIBUTED by the
  # reasoner's own tool_use audit (_reasoner_wrote_live_tree), not the spoke transcript.
  local fp_before; fp_before="$(_broker_worktree_fingerprint "$wt")"
  raw="$(run_answerer "$issue" "$question" "$wt")"; rc=$?
  # #247: run_answerer streams stream-json for the audit; the text parsers below (auth,
  # parse_decision, parse_decision_field) read the NORMALIZED final text, the audit reads $raw.
  local raw_text; raw_text="$(_normalize_answerer_output "$raw")"
  if _broker_is_git_worktree "$wt" && [ -z "$fp_before" ]; then
    # Fail SAFE: a git worktree with an empty fingerprint means the fingerprint tooling
    # is unavailable, so we cannot verify the reasoner stayed read-only. Never trust an
    # unverifiable answer — escalate rather than pass.
    log "  could not fingerprint #$issue's worktree to verify read-only — escalating"
    _broker_on_human_decision "$mode" "$wt" "$issue" \
      "could not fingerprint the worktree to verify the reasoner stayed read-only — needs a human"
    return 0
  fi
  if ! _broker_worktree_unchanged "$wt" "$fp_before"; then
    # The live tree changed during the reason step. Once the tree moved under the answerer its
    # answer is derived from a stale tree and must NEVER be injected (#244 review): the tree-changed
    # path ALWAYS returns here, so we never fall through to the ANSWER branch's recompute (which
    # would re-baseline fp_before and inject over the mutated tree).
    # #247 option (c): ATTRIBUTE the diff by the REASONER's OWN tool_use audit (whose whole-tree-diff
    # attribution the spoke transcript could only guess at — #244 residuals). Only a PROVEN reasoner
    # write (rw_rc 0) short-circuits to void amid spoke activity; when the audit does NOT prove a write
    # we do NOT trust it alone — an escape via a vector the audit does not model must still fail SAFE
    # (#244's "unconfirmed change => VOID"), so we attribute to the spoke ONLY on a positive spoke
    # signal and otherwise VOID.
    _reasoner_wrote_live_tree "$raw" "$wt"; local rw_rc=$? do_void=0
    if [ "$rw_rc" -eq 0 ]; then
      # The audit PROVES a reasoner live-tree write (a write tool under $wt / a mutating $wt-absolute
      # Bash). Void even amid COINCIDENT genuine spoke activity — closes #244 residual 1.
      do_void=1
    elif [ "$rw_rc" -eq 1 ]; then
      # The audit saw the reasoner's stream and found no modelled live-tree write. Attribute the diff
      # to the spoke ONLY on a positive transcript TOUCH — any appended record, since the isolated
      # reasoner writes nothing to the live transcript, so a #240 tool_result-only self-resume still
      # proves the spoke (closes residual 2). Spoke totally silent + a tree diff the audit could not
      # attribute ⇒ an unmodelled escape ⇒ fail SAFE and VOID (the #244 posture, restored).
      _spoke_touched_transcript "$wt" "$parked_sizes"; local touch_rc=$?
      [ "$touch_rc" -ne 0 ] && do_void=1
    else
      # rw_rc 2: the audit is UNAVAILABLE (a plain-text answerer / no stream-json / no python3) —
      # fall back to the #244 spoke-activity signal, unchanged: void UNLESS a genuine spoke turn
      # landed (fail SAFE on rc 2 there too, mirroring the unverifiable-fingerprint escalation). This
      # is what keeps every #244 answerer-stub test green — those stubs carry no auditable stream.
      _spoke_activity_appended "$wt" "$parked_sizes"; local act_rc=$?
      [ "$act_rc" -ne 0 ] && do_void=1
    fi
    if [ "$do_void" -eq 1 ]; then
      # Stamp the durable void marker FIRST so the top-of-function backoff short-circuit paces the
      # mutating reasoner across ticks — the (tip, sig) ceiling can't, since the tree write perturbs
      # it every tick. #241 §5: no longer terminal; warn + back off. 'unknown' reversibility: the
      # reasoner ESCAPED #237 snapshot isolation and wrote the LIVE tree — a should-never-fire event
      # the morning review must triage from the benign.
      _broker_mark_voided "$issue"
      log "  reasoner mutated the read-only worktree of #$issue — voiding its answer (backoff-paced; #241)"
      _broker_on_human_decision "$mode" "$wt" "$issue" \
        "the gate reasoner mutated the read-only worktree — its answer is voided; review the live tree" unknown
      return 0
    fi
    # No reasoner write: the diff is the spoke's own concurrent edit (the #234 self-resume) or a
    # sibling's. DROP the stale answer, never inject (a recompute would re-baseline the fingerprint
    # and inject mid-turn, #89): no gate-voided marker, no blocked/<issue> on an actively-working
    # spoke. A fresh park next tick is serviced anew.
    log "  #$issue's live tree changed but the reasoner did not write it — dropping the stale answer (#247)"
    return 0
  fi
  # The answerer is the supervisor's own `claude`; if its credentials are dead, every
  # other `claude` (the spokes, the next tick's answerer) is dead too. We treat it as an
  # auth failure only when the answerer EXITED NONZERO and its output carries an auth
  # signature — a healthy answer that merely discusses auth exits 0 and is unaffected.
  # Raise the global stop flag so the supervisor pauses DISPATCH and re-probes (#241 §9). WARN
  # this spoke (an auth failure is not its fault — never block it); the drain resumes servicing
  # it once auth recovers, rather than parking it blocked/<issue>. #247: read the RAW stream (not
  # the normalized text) so an auth signature in a dropped stream-json event is never missed.
  if [ "$rc" -ne 0 ] && is_auth_failure "$raw"; then
    _AFK_AUTH_FAILED=1
    broker_warn_continue "$wt" "$issue" auth \
      "subscription auth failed — token could not refresh; re-run /login on the host (drain paused, re-probing)" reversible
    return 0
  fi
  decision="$(parse_decision "$raw_text")"
  kind="${decision%%$'\t'*}"
  text="${decision#*$'\t'}"
  if [ "$kind" = "ANSWER" ] && [ -n "$text" ]; then
    # Park freshness gates EVERYTHING: if the spoke moved on while the answerer
    # reasoned, nothing happens regardless of the answer's content — injecting would
    # land mid-turn (#129/#89) and even a seed-replay escalation would stamp a
    # spurious blocked/<issue> on an actively-working spoke.
    if ! _still_parked_same "$wt" "$issue" "$was_gate" "$orig_question" "$parked_mtime"; then
      # #241 §4: the park may have CHANGED (a new prompt), or a non-turn write may have bumped
      # the transcript mtime while the spoke is STILL parked (the recurring-false-staleness that
      # stranded #240). Recompute against the CURRENT park in the same pass (depth-bounded to one
      # re-run) ONLY when the spoke is still parked AND no USER TURN landed since the park — a
      # DEFINITE no-reply (rc 1). Preserve #89/#129: a reply landing (rc 0) or an unreadable
      # transcript (rc 2) means the spoke may have moved on, so drop rather than inject mid-turn.
      _user_turn_appended "$wt" "$parked_sizes"; local _ut_rc=$?
      if [ "$depth" -lt 1 ] && [ "$_ut_rc" -eq 1 ] && _spoke_still_parked "$wt" "$issue"; then
        log "  #$issue still parked on a refreshed prompt (no reply landed) — recomputing against the current park (#241)"
        broker_service_gate "$wt" "$issue" "$mode" "$(( depth + 1 ))"
        return $?
      fi
      log "  #$issue is no longer parked on that prompt — dropping the stale answer (spoke moved on)"
      return 0
    elif _is_seed_replay "$wt" "$text"; then
      log "  answer to #$issue replays the spoke's own seed prompt — suppressing (#124)"
      text="answerer replayed the spoke's seed prompt — suppressed; needs a human"
    else
      target="$(_spoke_pane_target "$wt")"
      if [ -z "$target" ]; then
        text="could not locate spoke pane to inject the answer"
      else
        # Stamp the delivery attempt FIRST: from here until the answer registers the
        # spoke may sit on a buffered answer, and that window must not read as idle.
        stamp_answer_attempt "$issue"
        inject_and_verify "$wt" "$target" "$text"; rc=$?
        if [ "$rc" -eq 0 ]; then
          log "  injected answer into #$issue"
          _consume_gate_tag "$wt" "$issue"
          _afk_clear_warned "$issue"   # #241: genuine progress → drop this issue's warned-retry backoff
          # #241 review B2: record the taken answer for morning review. Read the reasoner's own
          # 'WARN:' note and 'REVERSIBILITY:' class off the reply. A WARN or a non-reversible class
          # is a NOTEWORTHY decision → a loud warned record + a journal line WITH a gh comment. A
          # routine reversible answer is a cheap FILE-ONLY journal line (no per-answer gh spam).
          local ans_rev_raw ans_rev ans_warn
          ans_rev_raw="$(parse_decision_field "$raw_text" REVERSIBILITY)"
          # Normalize to the first ALPHABETIC RUN (portable lowercasing, tolerant of quotes,
          # parens, or a trailing period around the class word) so 'Reversible', 'reversible.',
          # and '"irreversible"' all classify correctly. Gate the warn on the RAW presence, not
          # the normalized value: a present-but-non-reversible class (even one that normalizes to
          # empty, e.g. all-punctuation noise) must fail SAFE to a loud warned record, never
          # silently collapse to routine the way a bare trailing-strip did (#241 review).
          ans_rev="$(printf '%s' "$ans_rev_raw" | tr '[:upper:]' '[:lower:]' | grep -oE '[a-z]+' | head -n1 || true)"
          ans_warn="$(parse_decision_field "$raw_text" WARN)"
          if [ -n "$ans_warn" ] || { [ -n "$ans_rev_raw" ] && [ "$ans_rev" != reversible ]; }; then
            # The clear above dropped the retry BACKOFF (progress); this warned record is the
            # DELIBERATE loud review flag for the noteworthy decision — not a stale leftover.
            broker_warn "$issue" "answered [${ans_rev:-unknown}]${ans_warn:+ — WARN: $ans_warn}"
            broker_journal_decision "$issue" answer "injected answer${ans_warn:+ (WARN: $ans_warn)}" "${ans_rev:-unknown}"
          else
            _broker_journal_line "$issue" answer "injected answer (routine)" "${ans_rev:-reversible}"
          fi
          afk_emit_decision "$wt" success
          return 0
        elif [ "$rc" -eq 2 ] && command -v respawn_wedged_spoke >/dev/null 2>&1 && respawn_wedged_spoke "$wt" "$issue" "$text"; then
          # The wedged composer was recovered by a pane respawn that carries the answer
          # as its --continue prompt — delivered, same success contract as an inject.
          _consume_gate_tag "$wt" "$issue"
          afk_emit_decision "$wt" success
          return 0
        elif [ "$rc" -eq 2 ]; then
          # The old window is dead and the answer text lives nowhere else — carry its
          # head in the blocked reason so the returning human need not re-derive it.
          text="composer wedged and the pane respawn could not be confirmed — needs a human; the undelivered answer began: $(printf '%.120s' "${text%%$'\n'*}")"
          inject_diagnosed=1
        elif [ "$rc" -eq 3 ]; then
          log "  answer to #$issue never left the composer (delivery refuted) — escalating"
          text="answer never left the composer (delivery refuted, #201) — needs a human"
          inject_diagnosed=1
        else
          log "  answer to #$issue did not register — escalating"
          text="answer did not register in the spoke (inject not confirmed) — needs a human"
        fi
      fi
    fi
  elif [ "$kind" = "ESCALATE" ]; then
    [ -n "$text" ] || text="answerer escalated (no reason given)"
  else
    text="answerer returned no decision — escalating for human review"
  fi
  # Park freshness gates the ESCALATE / no-decision / inject-failure escalation too, not just
  # the ANSWER inject (#171-subtask-2): the answerer takes minutes (or timed out), and if the
  # spoke moved on meanwhile (a human replied, the turn resumed) stamping blocked/<N> would
  # strand an actively-working spoke — worse now that a blocked-at-tip park is re-answerable
  # (#171-subtask-3). A late-registered inject also drops here rather than double-escalating.
  # Uses _spoke_moved_on (a POSITIVE transcript-advanced signal), NOT !_still_parked_same:
  # an ambiguous probe must NOT drop a real escalation (review) — only demonstrated activity
  # does. (The ANSWER branch's own pre-inject re-check stays _still_parked_same: there,
  # dropping on uncertainty is the safe direction — it just skips a possibly-stale inject.)
  # EXCEPT when the injector itself diagnosed a wedge/refuted delivery (rc 2/3): there the
  # advance is EXPLAINED by the very non-turn write that triggered the diagnosis, so reading
  # it as "moved on" would drop every #201 escalation and re-paste onto the wedged composer
  # forever, with no blocked/<issue> ever stamped (#201 review, CONFIRMED).
  if [ "$inject_diagnosed" -eq 0 ] && _spoke_moved_on "$wt" "$parked_mtime"; then
    log "  #$issue transcript advanced while reasoning — dropping the escalation (spoke moved on)"
    return 0
  fi
  # A diagnosed wedge/refuted inject (rc 2/3) is genuinely UNCERTAIN — the paste may have
  # partially landed — so journal it 'unknown' for triage; an ESCALATE/no-decision is reversible.
  local decision_rev=reversible
  [ "$inject_diagnosed" -eq 1 ] && decision_rev=unknown
  _broker_on_human_decision "$mode" "$wt" "$issue" "$text" "$decision_rev"
}

decide_and_act() { broker_service_gate "$1" "$2" unattended; }

# _broker_on_human_decision <mode> <wt> <issue> <reason> -> route a decision that the answerer
# could not resolve into an injectable answer (a voided/mutated read-only tree, an unverifiable
# fingerprint, an ESCALATE/no-decision reasoner reply, an inject failure). The ONE mode-divergent
# seam of the shared core. Attended: present a structured QCM on a dedicated per-gate surface
# (#155). Unattended (/afk): #241 no longer parks blocked/<issue> — it WARNS loudly, journals the
# taken decision, and keeps the spoke serviced (retried on the warned-retry backoff). The reason
# IS the decision text; these are reversible (the answer is voided/undelivered, the spoke's work
# is intact and re-serviceable).
_broker_on_human_decision() {
  local mode="$1" wt="$2" issue="$3" reason="$4" rev="${5:-reversible}"
  if [ "$mode" = attended ] && command -v _broker_present_qcm >/dev/null 2>&1; then
    _broker_present_qcm "$wt" "$issue" "$reason"
    return
  fi
  # <rev> is the reversibility of the DECISION taken (void/decline the answer, retry) — almost
  # always reversible. Callers pass 'unknown' when the underlying EVENT is genuinely uncertain
  # (a reasoner that escaped snapshot isolation and wrote the live tree; a wedge whose paste may
  # have partially landed) so the morning review can triage those out of the benign default.
  broker_warn_continue "$wt" "$issue" answer "$reason" "$rev"
}

# --- durable local block record (issue #109, AC2) -----------------------------
# spoke-ready.sh emits blocked/<issue> by `git tag` + `git push -f origin blocked/<issue>`;
# that push can fail for any reason (no/unreachable remote, a transient network drop, a
# push-hook error) — and in the #103 incident the reap logged `could not emit blocked/103`
# and dropped it. When the tag can't be pushed after retries, a blocked state is recorded
# LOCALLY instead, so it is NEVER silently dropped: --status surfaces this record for the
# operator returning from AFK. Cleared on a fresh arm (a current-window view).
_afk_blocked_record() { printf '%s\n' "$(_afk_state_dir)/blocked-$1.txt"; }
_afk_record_blocked_locally() {
  local issue="$1" reason="$2" f
  f="$(_afk_blocked_record "$issue")"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\n' "$(afk_now)" "$reason" > "$f" 2>/dev/null \
    || log "  WARNING: could not write a durable block record for #$issue at $f"
}
_clear_blocked_records() { rm -f "$(_afk_state_dir)"/blocked-*.txt 2>/dev/null || true; }

# _escalate_blocked <wt_path> <issue> <reason> -> emit blocked/<issue> on the spoke's
# behalf via spoke-ready.sh, RETRYING the push, and falling back to a durable local record
# when it still can't be emitted — escalation never fails silently (#109). Always emits a
# deny decision span. Best-effort; never aborts the loop.
_escalate_blocked() {
  local wt="$1" issue="$2" reason="$3" sr tries i=0 ok=0
  log "  escalate #$issue: $reason"
  tries="${AFK_ESCALATE_TRIES:-3}"
  case "$tries" in '' | *[!0-9]*) tries=3 ;; esac   # guard the loop arithmetic
  sr="$(_afk_find_script "${SPOKE_READY:-}" spoke-ready.sh)" || sr=""
  if [ -n "$sr" ]; then
    while [ "$i" -lt "$tries" ]; do
      if ( cd "$wt" && "$sr" --blocked "$issue" -m "$reason" ) >/dev/null 2>&1; then ok=1; break; fi
      i=$(( i + 1 ))
      [ "$i" -lt "$tries" ] && sleep "${AFK_ESCALATE_SLEEP:-1}" 2>/dev/null || true
    done
  fi
  if [ "$ok" -ne 1 ]; then
    log "  could not push blocked/$issue after $tries tries — recording it durably (see --status)"
    _afk_record_blocked_locally "$issue" "$reason"
  fi
  afk_emit_decision "$wt" deny
}

