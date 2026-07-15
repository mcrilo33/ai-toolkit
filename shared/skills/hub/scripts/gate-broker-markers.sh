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

# --- queued-subtask channel (issue #278) --------------------------------------
# The drain's INBOUND channel to a LIVE spoke — the mirror of the outbound event spool
# above. When a ready issue's scope is packable into a spoke that is already running, the
# drain queues it here instead of spawning a second worktree and paying its whole lifecycle
# tax again (spawn + first-push suite seed 12-47 min, PLAN gate, review, land). The spoke
# consumes the queue at its ready boundary: while it is non-empty, spoke-ready.sh REFUSES
# the terminal ready/<primary>, which is what stops auto_land from landing a spoke — and
# tearing down its worktree — with subtasks still outstanding.
#
# ONE FILE PER QUEUED ISSUE — `queued-<spoke>/<issue>` — not one file of many lines. The
# hub and the spoke are separate processes sharing this dir, so a line-based queue would need
# a read-modify-write to remove an entry, and a concurrent append between its read and its
# rewrite is silently LOST (verified, not theoretical: the drain routing #270 while the spoke
# clears the #264 it just shipped drops #270). This host has no flock(1), and the repo's only
# lock (worktree-lib.sh) shells to python — far too heavy for a per-tick path. A file per
# issue sidesteps all of it: create and unlink are atomic, and each entry is independent, so
# no writer can clobber another's. Cheaper AND stronger than locking a shared file.
#
# ORDER is deliberately not preserved: the queue reads back ascending by issue number, not in
# the order entries were added. Insertion order buys nothing here — the members are same-scope
# peers, batch-plan has already picked the one that matters (the LEADER, which names the
# branch and is never queued), and the rest are independent issues whose completion order does
# not change the wall clock. Ascending is deterministic, which is what tests and logs need.
#
# Written with mkdir -p + a plain create, NOT _afk_atomic_write: that helper lives in
# hub-afk.sh, which gate-broker.sh does not source, so calling it here would be an unbound
# command whenever the broker runs standalone (e.g. hub-watchdog.sh).
#
# Three writers share only this path contract, because neither of the other two can source
# this module: worktree-new.sh seeds a dispatch-time pack's subtasks, hub-afk.sh routes a
# filing-time match, and spoke-ready.sh reads + clears from inside the spoke.
_queued_subtask_dir() { printf '%s\n' "$(_afk_state_dir)/queued-$1"; }

# stamp_queued_subtask <spoke> <issue> -> queue <issue> as a subtask for <spoke>.
# Idempotent for free: re-creating an existing entry is the same entry. The drain re-derives
# routing every tick and may re-route the same issue, and a duplicate would make the spoke
# re-anchor on work it already shipped.
stamp_queued_subtask() {
  local dir; dir="$(_queued_subtask_dir "$1")"
  mkdir -p "$dir" 2>/dev/null || true
  : > "$dir/$2" 2>/dev/null || true
  return 0
}
# read_queued_subtask <spoke> -> the queued issue numbers, one per line, ascending.
# Empty (rc 0) for a spoke with no queue — the common case on every tick.
read_queued_subtask() {
  local dir f; dir="$(_queued_subtask_dir "$1")"
  [ -d "$dir" ] || return 0
  for f in "$dir"/*; do
    [ -f "$f" ] || continue
    printf '%s\n' "${f##*/}"
  done | sort -n
  return 0
}
# clear_queued_subtask <spoke> [issue] -> drop ONE entry (the spoke shipped that subtask), or
# the WHOLE queue when no issue is given (the reclaim path: a spoke that reached its terminal
# ready before the entry was consumed drops it, and the issues fall back to a fresh dispatch).
# An exact filename, so clearing #4 cannot touch #264 or #40 — no substring hazard at all.
clear_queued_subtask() {
  local dir; dir="$(_queued_subtask_dir "$1")"
  if [ -z "${2:-}" ]; then
    rm -rf "$dir" 2>/dev/null || true
  else
    rm -f "$dir/$2" 2>/dev/null || true
  fi
  return 0
}
# NOTE: deliberately NOT cleared on a fresh arm, unlike the dispatch epochs / progress state
# above. Those are per-WINDOW; a queue is bound to a live SPOKE's lifetime, and worktrees
# outlive `/afk off` (the drain only tags them blocked/<N>, and re-arm reconciles that away).
# Wiping the channel at arm would silently drop subtasks already routed to a spoke that is
# still running them, forcing each back through a full fresh lifecycle — the exact waste this
# issue exists to remove. A queue whose spoke is gone is reclaimed by liveness instead
# (hub-afk.sh clears it once the target is terminal or no longer in flight).

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

# park-sig-<issue> — the park CONTEXT ("<tip>\t<sig>") the live park-onset epoch belongs to (#283).
# stamp-once + clear-when-not-parked (above) makes the onset track a park EPISODE only when some
# tick actually observes the spoke unparked. Under dense permission traffic (#276) none ever did:
# a dialog was pending on nearly every tick, so twenty minutes of distinct episodes fused into one
# onset still holding the original PLAN gate's timestamp — and the watchdog's park-unanswered
# ceiling measured against it. Pairing the onset with the CONTEXT of the park it was stamped for
# closes that: a changed context is a new episode however busy the spoke is. Keyed on (tip, sig)
# like the broker's own park record (_broker_reanswer_exhausted below), and for the same reason:
# a signature alone repeats across episodes, so an identical dialog re-raised after a commit would
# otherwise inherit the previous episode's age — the fused-onset bug one layer down.
_park_sig_file() { printf '%s\n' "$(_afk_state_dir)/park-sig-$1"; }
read_park_sig()  { local f; f="$(_park_sig_file "$1")"; [ -f "$f" ] && cat "$f" 2>/dev/null || true; }

# note_park_episode <wt> <issue> -> print the CURRENT park episode's onset epoch, re-stamping it
# first when this is a new episode. New means either the pending park's (tip, signature) context
# differs from the one the live onset was stamped for, or the onset is gone while the context
# record survives (slot_state's clear_park_onset_epoch drops the epoch on a not-parked tick and
# knows nothing about this record, so an identical dialog re-raised later must not inherit the
# dead episode's age). An empty signature — _broker_park_signature fail-opens to empty on an
# unextractable park, e.g. a gate tag whose plan artifact is unreadable (`gate:` -> empty) —
# records NOTHING and leaves the onset exactly as stamp-once left it: never claim an episode we
# cannot substantiate. Called by the watchdog (park-onset's only reader) on each waiting tick;
# best-effort throughout.
note_park_episode() {
  local wt="$1" issue="$2" sig key prev onset
  sig="$(_broker_park_signature "$wt" "$issue" 2>/dev/null)"
  onset="$(read_park_onset_epoch "$issue")"
  case "$onset" in '' | *[!0-9]*) onset="" ;; esac
  [ -n "$sig" ] || { printf '%s\n' "$onset"; return 0; }
  key="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)"$'\t'"$sig"
  prev="$(read_park_sig "$issue")"
  if [ "$prev" != "$key" ] || [ -z "$onset" ]; then
    stamp_park_onset_epoch "$issue"     # _stamp_issue_epoch mkdir -p's the state dir for both writes
    printf '%s\n' "$key" > "$(_park_sig_file "$issue")" 2>/dev/null || true
    onset="$(read_park_onset_epoch "$issue")"
  fi
  printf '%s\n' "$onset"
}

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
# the clean first-exhaustion re-service; a leftover served record (#294) would skip the first
# serve of a park whose approve was delivered in a window that is already over.
_clear_progress_state() {
  local dir; dir="$(_afk_state_dir)"
  rm -f "$dir"/progress-*.epoch "$dir"/answer-attempt-*.epoch "$dir"/done-*.epoch "$dir"/tip-* \
    "$dir"/park-onset-*.epoch "$dir"/park-sig-* \
    "$dir"/reanswer-* "$dir"/answer-drop-* "$dir"/served-* "$dir"/gate-voided-* \
    "$dir"/terminal-logged-* \
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

# read_reanswer_count <wt> <issue> -> the re-answer attempt COUNT recorded for the CURRENT
# (tip, sig), or empty when none/stale. The single reader of reanswer-<issue>'s record layout —
# so a caller outside this module (the watchdog's servicing-evidence probe, #288 AC2) never
# hand-parses the file and risks drifting from _broker_reanswer_exhausted's own writer if the
# layout ever changes.
read_reanswer_count() {
  local wt="$1" issue="$2" f tip sig rec_tip rec_sig rec_n
  f="$(_reanswer_state_file "$issue")"
  [ -f "$f" ] || return 0
  IFS=$'\t' read -r rec_tip rec_sig rec_n < "$f" 2>/dev/null || return 0
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)"
  sig="$(_broker_park_signature "$wt" "$issue" 2>/dev/null)"
  [ "$rec_tip" = "$tip" ] && [ "$rec_sig" = "$sig" ] || return 0
  case "$rec_n" in '' | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "$rec_n"
}

# --- computed-then-dropped answers (issue #288 AC3) ---------------------------
# answer-drop-<issue> — the SUBSET of reanswer-<issue>'s attempts that ended in a DROP (never
# injected): the #247 live-tree-changed drop and the "no longer parked on that prompt" drop both
# leave no trace the watchdog can read otherwise, since neither journals and the delivery epoch
# (answer-attempt-<issue>.epoch) is stamped only on a SUCCESSFUL inject. Keyed like
# _broker_reanswer_exhausted's own (tip, sig) counter and for the same reason: a changed tip or
# park content starts a fresh episode's count at 1, not a running total across resolved parks.
# Read back by the watchdog to tell "never touched" from "touched, never deliverable" and to name
# the drop count + last verdict in the park-undeliverable ledger line.
_answer_drop_state_file() { printf '%s\n' "$(_afk_state_dir)/answer-drop-$1"; }

# note_answer_drop <wt> <issue> <sig> <reason> -> record ONE computed-then-dropped answer:
# "<tip>\t<sig>\t<count>\t<reason>". <sig> is the signature of the park the DROPPED answer was
# actually computed for (the caller's own already-captured park_sig, e.g. broker_service_gate's
# re-answer-ceiling signature) — NOT re-derived here, exactly like _broker_reanswer_exhausted
# takes its sig as a parameter. Recomputing it internally would attribute the drop to whatever
# park happens to be live at CALL time, which can be a DIFFERENT park than the one the answerer
# actually reasoned about when the spoke moved on mid-reasoning (#288 review). <reason> is the
# drop's own log text (the LAST one wins), so the watchdog names the actual verdict without
# re-deriving it from the drain log. Best-effort; never aborts the caller.
note_answer_drop() {
  local wt="$1" issue="$2" sig="$3" reason="$4" tip f prev_tip="" prev_sig="" prev_n=0
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)"
  f="$(_answer_drop_state_file "$issue")"
  if [ -f "$f" ]; then
    IFS=$'\t' read -r prev_tip prev_sig prev_n _ < "$f" 2>/dev/null || true
    case "$prev_n" in '' | *[!0-9]*) prev_n=0 ;; esac
  fi
  if [ "$prev_tip" != "$tip" ] || [ "$prev_sig" != "$sig" ]; then prev_n=0; fi   # new context
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\t%s\t%s\n' "$tip" "$sig" "$(( prev_n + 1 ))" "$reason" > "$f" 2>/dev/null || true
}

# read_answer_drop <wt> <issue> -> "<count>\t<reason>" for the CURRENT (tip, sig), or empty when
# no drop is on record for this exact park episode (none ever happened, or the episode moved on).
read_answer_drop() {
  local wt="$1" issue="$2" f tip sig rec_tip rec_sig rec_n rec_reason
  f="$(_answer_drop_state_file "$issue")"
  [ -f "$f" ] || return 0
  IFS=$'\t' read -r rec_tip rec_sig rec_n rec_reason < "$f" 2>/dev/null || return 0
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)"
  sig="$(_broker_park_signature "$wt" "$issue" 2>/dev/null)"
  [ "$rec_tip" = "$tip" ] && [ "$rec_sig" = "$sig" ] || return 0
  case "$rec_n" in '' | *[!0-9]*) return 0 ;; esac
  printf '%s\t%s\n' "$rec_n" "$rec_reason"
}

clear_answer_drop() { rm -f "$(_answer_drop_state_file "$1")" 2>/dev/null || true; }

# --- served permission parks (issue #294) -------------------------------------
# served-<issue> — the permission lane's record that an APPROVE was already DELIVERED for one
# specific park: "<tip>\t<sig>\t<tool_use_id>\t<epoch>". _decide_permission's APPROVE branches
# called approve_permission and recorded nothing, so an UNCHANGED pending dialog — a pane that has
# not redrawn, or an approved `nohup ... &` whose gate keeps the gated tool_use unresolved — was
# re-dispatched on the next tick: _broker_reanswer_exhausted computed the SAME (tip, sig), found
# the counter still under the ceiling, and the identical command was approved a SECOND time
# (exactly one duplicate at the default ceiling of 2 — the #135/#188 concurrent-gate shape).
#
# Deliberately NOT folded into reanswer-<issue>: that counter's exhausted branch warns, journals,
# and calls _afk_warned_arm, so paying for a routine auto-approve with it would arm the
# warned-retry backoff on every healthy approve — pacing the whole service lane for that spoke
# (hub-afk.sh's _afk_warned_due gate) and mislabelling a success as a ceiling failure. The two
# records answer different questions: the ceiling asks "have we tried enough?", this asks "did we
# already succeed?".
#
# THE KEY IS (tip, sig, tool_use_id), one field wider than the family's usual (tip, sig). The
# extra field is load-bearing, not symmetry: a repeatable safe command re-issued VERBATIM at the
# same tip (a failed push retried) has an identical (tip, sig) but is a genuinely NEW dialog, and
# the tip cannot advance while the spoke is parked — so a (tip, sig)-only marker would refuse to
# serve it forever, stranding the spoke until the watchdog escalated it to a human. The gated
# tool_use's id is unique per call, so it separates the two cases exactly. A tip advance or a
# changed signature invalidates the record by key, the same convention reanswer-<issue> and
# answer-drop-<issue> use (they are not explicitly rm'd on progress either — only NON-keyed
# records like the warned state are). Per-window, so _clear_progress_state drops it on a fresh arm.
_permission_served_file() { printf '%s\n' "$(_afk_state_dir)/served-$1"; }

# note_permission_served <wt> <issue> <sig> <tool_id> -> record that <tool_id>'s dialog was
# approved at this (tip, sig). <sig> and <tool_id> are the caller's ALREADY-CAPTURED values from
# BEFORE the delivery — never re-derived here (the #288 note_answer_drop lesson: a re-derived sig
# attributes the record to whichever park is live at call time, which after a delivery can be a
# DIFFERENT one). Records NOTHING without both: an unsubstantiable park is never claimed
# (note_park_episode's posture), and the lane falls back to its pre-#294 behavior.
note_permission_served() {
  local wt="$1" issue="$2" sig="$3" tid="$4" tip f
  [ -n "$sig" ] && [ -n "$tid" ] || return 0
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)"
  f="$(_permission_served_file "$issue")"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\t%s\t%s\n' "$tip" "$sig" "$tid" "$(afk_now)" > "$f" 2>/dev/null || true
  return 0
}

# _broker_permission_served <wt> <issue> <sig> -> rc 0 when the LIVE park is the very one an
# approve was already delivered for: the record's tip, signature, AND pending tool_use id all match
# what is pending right now. Takes <sig> as a parameter for the same reason note_answer_drop does —
# the caller (broker_service_gate) has already captured it from the tick's single pane read (#269).
# An empty live id (the #269 unflushed-dialog window) never matches: fail OPEN, never suppress a
# serve on an unprovable match.
_broker_permission_served() {
  local wt="$1" issue="$2" sig="$3" f tip tid rec_tip rec_sig rec_tid
  [ -n "$sig" ] || return 1
  f="$(_permission_served_file "$issue")"
  [ -f "$f" ] || return 1
  IFS=$'\t' read -r rec_tip rec_sig rec_tid _ < "$f" 2>/dev/null || return 1
  [ -n "$rec_tid" ] || return 1
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)"
  [ "$rec_tip" = "$tip" ] && [ "$rec_sig" = "$sig" ] || return 1
  tid="$(extract_pending_tool_id "$wt")"
  [ -n "$tid" ] && [ "$tid" = "$rec_tid" ]
}

# _broker_served_skip_due <issue> [now] -> rc 0 when a served park is due for ONE supervised
# re-serve (never served, or the skip window has elapsed), rc 1 while the window still holds.
# Mirrors _afk_warned_due's shape and its never-armed-means-due default.
#
# The skip MUST NOT be terminal: approve_permission verifies only that the transcript mtime
# advanced, NOT that the dialog was consumed, so an approve whose keypress never landed leaves the
# identical (tip, sig, id) pending — and a permanent skip would never retry it, the same unbounded
# strand the tool_use id exists to avoid. The window is paced on the served record's own epoch
# rather than the shared warned-retry backoff precisely so a healthy auto-approve never arms that
# backoff. AFK_SERVED_SKIP_SECONDS (default 60) sits between the two cadences that matter: well
# above the event-driven wake burst (#176) that re-services a spoke seconds apart while the pane
# has not redrawn — the duplicate this closes — and well below the AFK_TICK_SECONDS backstop (300),
# so a dialog still pending on the next backstop tick reads as a genuinely undelivered approve and
# is re-served. From there the re-answer ceiling and the #241 curve bound a standing failure.
_broker_served_skip_due() {
  local issue="$1" now="${2:-$(afk_now)}" f window stamped
  f="$(_permission_served_file "$issue")"
  [ -f "$f" ] || return 0
  IFS=$'\t' read -r _ _ _ stamped < "$f" 2>/dev/null || return 0
  case "$stamped" in '' | *[!0-9]*) return 0 ;; esac
  case "$now" in '' | *[!0-9]*) return 0 ;; esac
  window="${AFK_SERVED_SKIP_SECONDS:-60}"
  case "$window" in '' | *[!0-9]*) window=60 ;; esac
  [ "$now" -ge "$(( stamped + window ))" ]
}

clear_permission_served() { rm -f "$(_permission_served_file "$1")" 2>/dev/null || true; }

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
