#!/usr/bin/env bash
# gate-broker-markers.sh -- split out of gate-broker.sh (issue #275).
#
# A pure function-definition module of the gate-broker core. Sourced by the entry lib
# gate-broker.sh AFTER worktree-lib/hub-inject/log/afk_now and BEFORE any function is
# called, so every cross-module helper resolves at call time. Not run on its own.
set -uo pipefail

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
