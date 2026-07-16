#!/usr/bin/env bash
# hub-watchdog-detect.sh -- split out of hub-watchdog.sh (issue #308).
#
# The DETECTION half of the tier-2 watchdog: the five condition detectors (_wd_detect_*)
# and every reader they use to decide whether the /afk drain fell short on a spoke
# (park-unanswered, park-undeliverable, dead/idle, mergeable-skipped, supervisor-dead). A
# pure function-definition module sourced by the entry lib hub-watchdog.sh AFTER
# worktree-lib / gate-broker / hub-inject and the entry's own path/time/state primitives,
# and BEFORE any function is called, so every cross-module helper resolves at call time.
# Carries only the detector ceiling knob-guards (:= defaults) as top-level work. Not run on
# its own.
set -uo pipefail

# --- detection conditions + scripted interventions (issue #251) ---------------
# Each detector fires ONLY when the drain already had its chance and the condition persists
# past a grace margin beyond the drain's OWN threshold — so a CORRECT drain never trips the
# watchdog (every firing is an afk defect). On a firing the dispatcher records it (_wd_fire →
# the intervention-ledger; subtask 4 adds classify + bug-scoper) AND takes the safe scripted
# intervention behind a HUB_WATCHDOG_*_CMD seam (so each is unit-testable without a live
# tmux/gh/claude). All best-effort: a detector/intervention error never aborts a tick.
: "${HUB_WATCHDOG_PARK_CEILING:=600}"   # a waiting spoke unanswered this long ⇒ drain fell short
: "${HUB_WATCHDOG_IDLE_CEILING:=3600}"  # a dead/idle spoke unrevived this long ⇒ reaper missed it
: "${HUB_WATCHDOG_LAND_CEILING:=900}"   # a ready-at-tip branch un-landed this long ⇒ auto-land skipped
: "${HUB_WATCHDOG_LAND_ACTIVE:=900}"    # a land-<issue>.log quiet this long ⇒ that land is NOT in flight

# _wd_epoch_stale <epoch> <now> <ceiling> -> true when now-epoch > ceiling. Empty/non-numeric
# reads as NOT stale (can't measure → never fire), guarding set -u arithmetic against a bareword.
_wd_epoch_stale() {
  local epoch="$1" now="$2" ceiling="$3"
  case "$epoch" in '' | *[!0-9]*) return 1 ;; esac
  case "$now" in '' | *[!0-9]*) return 1 ;; esac
  [ "$(( now - epoch ))" -gt "$ceiling" ]
}

# _wd_tag_at_tip <wt> <kind> <issue> -> true when <kind>/<issue> points exactly at HEAD.
_wd_tag_at_tip() {
  local wt="$1" kind="$2" issue="$3" tip tag
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  tag="$(git -C "$wt" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)" || return 1
  [ -n "$tag" ] && [ "$tag" = "$tip" ]
}

# _wd_blocked_stale <wt> <issue> -> true when blocked/<issue> is a STRICT ancestor of the tip:
# the spoke committed on top, so the drain's reconcile_markers should have cleared it already.
_wd_blocked_stale() {
  local wt="$1" issue="$2" tag tip
  tag="$(git -C "$wt" rev-parse -q --verify "refs/tags/blocked/${issue}^{commit}" 2>/dev/null)" || return 1
  [ -n "$tag" ] || return 1
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  [ "$tag" != "$tip" ] || return 1
  git -C "$wt" merge-base --is-ancestor "$tag" "$tip" 2>/dev/null
}

# _wd_issue_open <issue> -> best-effort "is this GitHub issue still open?" A closed issue was
# landed (not skipped), so condition 4 must not fire on it. gh unavailable / a failed query
# reads as OPEN (fire) — the drain-skip signal already gated it; HUB_WATCHDOG_ISSUE_STATE_CMD
# overrides for tests (echo the state: open|closed).
_wd_issue_open() {
  local issue="$1" state
  if [ -n "${HUB_WATCHDOG_ISSUE_STATE_CMD:-}" ]; then
    state="$(bash -c "$HUB_WATCHDOG_ISSUE_STATE_CMD" hub-watchdog "$issue" 2>/dev/null)"
  else
    command -v gh >/dev/null 2>&1 || return 0
    state="$(gh issue view "$issue" --json state -q .state 2>/dev/null)"
  fi
  case "$state" in [Cc]losed | CLOSED) return 1 ;; *) return 0 ;; esac
}

# --- the 5 detectors (predicates over the drain's own state readers) -----------
# Read-only EXCEPT condition 1, which notes the current park episode as it measures (#283) — the
# same way slot_state stamps the epochs it reads. So a detector is safe to run on a tick, but NOT
# speculatively (a dry-run / status probe would re-stamp the park-onset clock it reads).
# _wd_park_lane <wt> <issue> -> permission | gate | question | unknown: WHOSE lane the pending park
# belongs to (#283). Probes STRUCTURALLY, in _broker_park_signature's own precedence order, rather
# than parsing that signature: a gate-tagged park whose plan artifact is unreadable hashes to EMPTY
# (`gate:` -> ''), which would read as `unknown` here and — under the "unknown never fires" rule
# below — silence #265's never-attempted strand. _gate_parked is a tag-at-tip check, immune to
# artifact readability, so the strand keeps firing. Permission wins over a gate tag still at the
# tip, exactly as the broker orders it.
_wd_park_lane() {
  local wt="$1" issue="$2"
  if command -v _permission_pending >/dev/null 2>&1 && _permission_pending "$wt"; then
    printf 'permission\n'; return
  fi
  if command -v _gate_parked >/dev/null 2>&1 && _gate_parked "$wt" "$issue"; then
    printf 'gate\n'; return
  fi
  if command -v extract_pending_question >/dev/null 2>&1 &&
     [ -n "$(extract_pending_question "$wt" 2>/dev/null)" ]; then
    printf 'question\n'; return
  fi
  printf 'unknown\n'
}

# _wd_park_is_answer_lane <wt> <issue> -> true for a park the ANSWER lane owns (a plan gate or a
# question). A permission dialog is the BROKER's lane with its own timers and its own re-answer
# ceiling; the watchdog must not answer it (#271) and must not measure it against the answer
# ceiling. `unknown` is NOT the answer lane: slot_state's `waiting` is derived from these same
# three probes, so waiting-with-no-lane means the park resolved between the two calls — a race,
# not a strand. The deliberate trade-off (journaled per AC3): a churning permission dialog is now
# unbounded BY THE WATCHDOG. That is on purpose — it is the broker's lane, and its re-answer
# ceiling escalating to blocked/<issue> is what bounds it. A second ceiling here would re-create
# the lane confusion this fix exists to remove.
_wd_park_is_answer_lane() {
  case "$(_wd_park_lane "$1" "$2")" in gate | question) return 0 ;; esac
  return 1
}

# _wd_park_base <wt> <issue> -> the epoch the park-unanswered ceiling measures FROM:
# max(current episode onset, answer delivery). An answer delivered BEFORE the current park began
# cannot count against it (#283) — that was the #276 false-fire: one answered plan gate, then ten
# productive minutes, and the ceiling still measured from that one delivery. note_park_episode
# re-stamps the onset when the pending park's context changes, so the onset names the episode
# actually pending; a delivery INSIDE it is the more recent word and wins. Empty when neither is
# measurable (the detector then cannot fire — same contract as _afk_ceiling_epoch).
#
# #304 note: the onset is the reconciler-stamped park-onset epoch (read_park_onset_epoch), a
# side-channel the #304 reconciler now writes on the FIRST park observation in slot_state's place.
# The park EPISODE the answer-lane events key on comes off this same onset (via _gb_episode_key), so
# note_park_episode's re-stamp on a changed park signature is load-bearing: dropping it would strand
# a stale onset and re-open the #276/#283 mis-attribution. It stays unconditional here.
_wd_park_base() {
  local wt="$1" issue="$2" onset attempt
  if command -v note_park_episode >/dev/null 2>&1; then
    onset="$(note_park_episode "$wt" "$issue" 2>/dev/null)"
  else
    onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  fi
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  case "$onset" in '' | *[!0-9]*) onset=0 ;; esac
  case "$attempt" in '' | *[!0-9]*) attempt=0 ;; esac
  [ "$attempt" -gt "$onset" ] && onset="$attempt"
  [ "$onset" -gt 0 ] && printf '%s\n' "$onset"
  return 0
}

# _wd_last_decision_ts <issue> -> the ts of the drain's most recent decision-journal record for
# this issue, or empty. The journal is the drain's own "I acted on this spoke" evidence (#241),
# keyed by issue and already written per broker decision — no new plumbing needed. Append-only and
# chronological, so the LAST match is the newest.
# CONTRACT: this parses the record's field ORDER (ts first) written by _broker_journal_line
# (gate-broker-answerer.sh) — the journal's sole writer. A reorder there would make this return
# empty for every issue and silently disable the servicing suppression, so the pairing is pinned
# end-to-end by test_last_decision_ts_reads_a_record_written_by_the_real_journal_writer.
_wd_last_decision_ts() {
  local issue="$1" f
  command -v _broker_journal_file >/dev/null 2>&1 || return 0
  f="$(_broker_journal_file)"
  [ -f "$f" ] || return 0
  grep -F "\"issue\":\"$issue\"" "$f" 2>/dev/null | sed -n 's/^{"ts":\([0-9][0-9]*\).*/\1/p' | tail -n1
}

# _wd_drain_touched_recently <issue> <now> -> true when the drain acted on this issue within the
# ceiling window: a broker decision for it, or a progress-epoch advance. This is what separates
# "being handled" from "abandoned" (#283) — on #276 the broker approved a tier-3 command 12s
# BEFORE the firing and three more within 8 minutes after it.
# Measured as RECENCY, not "after the base", on purpose: the answerer journals its own delivery a
# beat AFTER stamp_answer_attempt, so an "after the base" test would read the delivery's own record
# as servicing and a genuinely stranded park could never fire (AC2).
_wd_drain_touched_recently() {
  local issue="$1" now="$2" cand newest=""
  for cand in "$(_wd_last_decision_ts "$issue")" "$(read_progress_epoch "$issue" 2>/dev/null)"; do
    case "$cand" in '' | *[!0-9]*) continue ;; esac
    if [ -z "$newest" ] || [ "$cand" -gt "$newest" ]; then newest="$cand"; fi
  done
  [ -n "$newest" ] || return 1
  ! _wd_epoch_stale "$newest" "$now" "$HUB_WATCHDOG_PARK_CEILING"
}

# _wd_episode_service <issue> -> delivered | dropped | none | unknown: the CURRENT park episode's
# service state, read from the transition log's episode-keyed answer-lane events (#304). This is the
# log-native replacement for the two side-channels the pre-#304 branch split read — the
# answer-attempt epoch (a delivery) and the answer-drop file (a drop) — unified into ONE
# episode-scoped read, so a delivery/drop from a RESOLVED park (a different episode key) can never be
# misattributed to the pending one (the canonical #241/#283/#288 bug). `unknown` (no read API, or the
# log records no park episode) means fall back to the epoch side-channels — NEVER a firing NOR a
# suppression basis on its own (#300). Gated on the current log state being a park so a stale episode
# left by a resolved park cannot be read as live.
#   delivered — the episode's last answer-lane event is a delivery/compute (#283 stale-attempt turf)
#   dropped   — the episode's last answer-lane event is a computed-then-dropped answer (#288 turf)
#   none      — the episode is recorded but carries no answer-lane event yet (never-attempted)
_wd_episode_service() {
  local issue="$1" ep ev
  command -v afk_current_state >/dev/null 2>&1 || { printf 'unknown\n'; return 0; }
  case "$(afk_current_state "$issue" 2>/dev/null)" in
    parked | parked-gate) ;;
    *) printf 'unknown\n'; return 0 ;;
  esac
  command -v afk_current_episode >/dev/null 2>&1 || { printf 'unknown\n'; return 0; }
  command -v afk_last_service_event >/dev/null 2>&1 || { printf 'unknown\n'; return 0; }
  ep="$(afk_current_episode "$issue" 2>/dev/null)"
  [ -n "$ep" ] || { printf 'unknown\n'; return 0; }
  ev="$(afk_last_service_event "$issue" "$ep" answer 2>/dev/null)"
  [ -n "$ev" ] || { printf 'none\n'; return 0; }
  # Classify on the pre-evidence HEAD only (transition-log.sh's _tlog_head discipline): the event
  # line carries a caller-supplied evidence object whose free-text `reason` is built from captured
  # tool output, so an answer_delivered whose evidence text happens to contain the substring
  # "event":"answer_dropped" would otherwise misclassify as a drop. The builders append
  # ,"evidence":... strictly LAST, so everything before it is lib-written fixed-key content.
  ev="${ev%%,\"evidence\":*}"
  case "$ev" in
    *'"event":"answer_dropped"'*) printf 'dropped\n' ;;
    *) printf 'delivered\n' ;;
  esac
}

# _wd_delivered_in_episode <wt> <issue> <attempt> <onset> -> rc 0 when a delivery landed INSIDE the
# current park episode (the #283 stale-attempt branch), rc 1 for never-attempted. Log-first: a
# `delivered` episode-service reading decides it, and `none`/`dropped` decide never-attempted; only an
# `unknown` log falls back to the epoch compare (_wd_park_attempt_in_episode), preserving the pre-#304
# behavior when the log is absent. The single branch oracle both the detector and the reason consult,
# so the reported branch can never disagree with the one that fired.
_wd_delivered_in_episode() {
  local wt="$1" issue="$2" attempt="$3" onset="$4"
  case "$(_wd_episode_service "$issue")" in
    delivered) return 0 ;;
    none | dropped) return 1 ;;
  esac
  _wd_park_attempt_in_episode "$attempt" "$onset"
}

# _wd_park_attempt_in_episode <attempt> <onset> -> true when a delivery landed INSIDE the current
# park episode (attempt >= onset, or onset unmeasurable): the same rule _wd_park_base measures by,
# and the branch split _wd_park_unanswered_reason reports on (#283/#288). Takes the ALREADY-READ
# values rather than re-reading them, so a caller that captured attempt/onset once (for a printed
# reason, or after refreshing the episode via _wd_park_base) can never have the branch decision
# disagree with a second, independently-timed read of the same epochs (#288 review). The epoch
# FALLBACK for _wd_delivered_in_episode when the transition log is unknown (#304).
_wd_park_attempt_in_episode() {
  local attempt="$1" onset="$2"
  case "$attempt" in '' | *[!0-9]*) return 1 ;; esac
  case "$onset" in '' | *[!0-9]*) return 0 ;; esac
  [ "$attempt" -ge "$onset" ]
}

# _wd_park_answer_attempted <wt> <issue> -> true when reanswer-<issue> records at least one
# attempt for the CURRENT (tip, sig) — proof the reasoner ran on THIS exact park, so labeling it
# "never-attempted" would be a lie (#288 AC2). Delegates to read_reanswer_count (gate-broker-
# markers.sh's own reader of the record it writes) rather than hand-parsing the file here.
_wd_park_answer_attempted() {
  local wt="$1" issue="$2" n
  command -v read_reanswer_count >/dev/null 2>&1 || return 1
  n="$(read_reanswer_count "$wt" "$issue" 2>/dev/null)"
  case "$n" in '' | *[!0-9]*) return 1 ;; esac
  [ "$n" -gt 0 ]
}

# _wd_park_warned_backoff_pending <issue> -> true when the default (answer) lane's warned-retry
# backoff is armed and NOT YET due — the drain is paced to retry, not abandoned (#288 AC2). This
# reads a signal _wd_drain_touched_recently structurally cannot: the backoff's own next-due epoch
# can be scheduled further out than the fixed recency window (AFK_WARN_BACKOFF_CAP defaults to
# 1800s, 3x the 600s park ceiling — the #274 land-lane inversion, unfixed here).
_wd_park_warned_backoff_pending() {
  local issue="$1" next
  command -v _afk_warned_next >/dev/null 2>&1 || return 1
  next="$(_afk_warned_next "$issue" 2>/dev/null)"
  case "$next" in '' | *[!0-9]*) return 1 ;; esac
  [ "$next" -gt "$(_wd_now)" ]
}

# _wd_park_drop_info <wt> <issue> -> "<count>\t<reason>" for the CURRENT (tip, sig)'s recorded
# answer-drop episode (issue #288 AC3), or empty when none/stale/unavailable.
_wd_park_drop_info() {
  local wt="$1" issue="$2"
  command -v read_answer_drop >/dev/null 2>&1 || return 0
  read_answer_drop "$wt" "$issue" 2>/dev/null
}

# Condition 1: a parked spoke answer_pass left unanswered past the grace margin. Three gates, in
# order: the park must be the ANSWER lane's (#283/#271), the CURRENT episode's base must be older
# than the ceiling (#283/#265 — never zero, and never a delivery from a long-resolved park), and
# the drain must not be visibly servicing the spoke. Only then did the drain genuinely fall short.
_wd_detect_park_unanswered() {
  local wt="$1" issue="$2" now="$3" base attempt onset
  command -v slot_state >/dev/null 2>&1 || return 1
  [ "$(slot_state "$wt" "$issue")" = "waiting" ] || return 1
  _wd_park_is_answer_lane "$wt" "$issue" || return 1
  base="$(_wd_park_base "$wt" "$issue")"   # refreshes the episode onset (note_park_episode) first
  _wd_epoch_stale "$base" "$now" "$HUB_WATCHDOG_PARK_CEILING" || return 1
  _wd_drain_touched_recently "$issue" "$now" && return 1
  # #288 AC2: the never-attempted branch specifically must not fire "no answer delivered" when
  # the drain plainly HAS attempted one on this exact park and is paced to retry — neither drop
  # path journals (so _wd_drain_touched_recently's recency window can't see it), and a warned
  # backoff can legitimately be scheduled past that window. A genuinely-untouched park (no
  # reanswer record at all) still fires unchanged. The drop-evidence case gets its own honest
  # condition (park-undeliverable) below, so this suppression must not engage there either.
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  if ! _wd_delivered_in_episode "$wt" "$issue" "$attempt" "$onset" \
     && _wd_park_answer_attempted "$wt" "$issue" \
     && _wd_park_warned_backoff_pending "$issue"; then
    return 1
  fi
  return 0
}

# Condition 1b (issue #288 AC3): a never-attempted park the drain DID service (>=1 reasoner run)
# but every delivery was dropped before injection — the #277 shape. Fires INSTEAD of
# park-unanswered whenever a drop is on record for the current episode, regardless of backoff
# phase: a park that has exhausted its backoff and still has nothing to show must still surface,
# just under the honest reason (a serviced-but-undeliverable park must never read as silence).
_wd_detect_park_undeliverable() {
  local wt="$1" issue="$2" now="$3" base attempt onset svc
  command -v slot_state >/dev/null 2>&1 || return 1
  # Cheap pre-check BEFORE the slot_state/lane/base probe chain (each a tmux capture-pane call,
  # #269's load-flake class): the vast majority of parked spokes never had a drop recorded at all.
  # #304: a drop is on record either as an episode-keyed `answer_dropped` lane event (log-native) OR
  # as the pre-#304 answer-drop file; bail only when NEITHER exists rather than repeating
  # _wd_detect_park_unanswered's entire probe chain for nothing (#288 review).
  svc="$(_wd_episode_service "$issue")"
  if [ "$svc" != dropped ]; then
    command -v _answer_drop_state_file >/dev/null 2>&1 || return 1
    [ -f "$(_answer_drop_state_file "$issue")" ] || return 1
  fi
  [ "$(slot_state "$wt" "$issue")" = "waiting" ] || return 1
  _wd_park_is_answer_lane "$wt" "$issue" || return 1
  base="$(_wd_park_base "$wt" "$issue")"   # refreshes the episode onset BEFORE it is read below
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  _wd_delivered_in_episode "$wt" "$issue" "$attempt" "$onset" && return 1   # a delivery landed -> stale-attempt's turf
  _wd_epoch_stale "$base" "$now" "$HUB_WATCHDOG_PARK_CEILING" || return 1
  # A drop is proven by the log episode (dropped) or the pre-#304 drop file.
  [ "$svc" = dropped ] || [ -n "$(_wd_park_drop_info "$wt" "$issue")" ]
}

# _wd_park_undeliverable_reason <wt> <issue> <now> -> names the drop count + last drop's own
# verdict, so the ledger line is diagnosable alone (#288 AC3, mirroring #283 AC5's measured-base
# reason).
_wd_park_undeliverable_reason() {
  local wt="$1" issue="$2" now="$3" info count reason onset
  info="$(_wd_park_drop_info "$wt" "$issue")"
  count="${info%%$'\t'*}"
  reason="${info#*$'\t'}"
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  printf 'park-undeliverable: %s computed-then-dropped answer(s), last drop: %s (parked %s; ceiling %ss)' \
    "${count:-?}" "${reason:-unknown}" "$(_wd_age_seconds "$onset" "$now")" "$HUB_WATCHDOG_PARK_CEILING"
}

# _wd_park_unanswered_reason <wt> <issue> <now> -> the MEASURED firing reason (#265/#283): which
# branch fired (never-attempted vs stale-attempt), the actual age, AND the base it was measured
# from — which epoch, the current park episode, the lane — so a future false positive is
# diagnosable from the ledger line alone (#283 AC5) instead of re-deriving the timeline from four
# state files. Reads the epochs on demand (no cross-pass global — the #241 leak trap); the episode
# onset was already noted by the detector on this tick, so this only reads it back.
_wd_park_unanswered_reason() {
  local wt="$1" issue="$2" now="$3" attempt onset base sig=""   # sig="": the guard below may skip it (set -u)
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  command -v read_park_sig >/dev/null 2>&1 && sig="$(read_park_sig "$issue" 2>/dev/null)"
  sig="${sig#*$'\t'}"                          # drop the tip half of the "<tip>\t<sig>" record
  case "$attempt" in '' | *[!0-9]*) attempt="" ;; esac
  case "$onset" in '' | *[!0-9]*) onset="" ;; esac
  # The branch decision is shared with the detector via _wd_delivered_in_episode (log-first, epoch
  # fallback), fed the SAME attempt/onset locals used for the printed text below, so the reported
  # branch can never disagree with the one that actually fired.
  if _wd_delivered_in_episode "$wt" "$issue" "$attempt" "$onset"; then
    base="answer-attempt@$attempt"
    printf 'park-unanswered (stale-attempt): last answer delivery %s ago' \
      "$(_wd_age_seconds "$attempt" "$now")"
  else
    base="park-onset@${onset:-?}"
    printf 'park-unanswered (never-attempted): parked %s with no answer delivered' \
      "$(_wd_age_seconds "$onset" "$now")"
  fi
  printf ' (ceiling %ss; base=%s, episode=%s@%s, lane=%s)' \
    "$HUB_WATCHDOG_PARK_CEILING" "$base" "${sig:0:8}" "${onset:-?}" "$(_wd_park_lane "$wt" "$issue")"
}

# _wd_age_seconds <epoch> <now> -> "<n>s" elapsed, or "an unknown time" when unmeasurable
# (empty/non-numeric epoch or now) — guards set -u arithmetic against a bareword.
_wd_age_seconds() {
  local epoch="$1" now="$2"
  case "$epoch" in '' | *[!0-9]*) printf 'an unknown time'; return ;; esac
  case "$now" in '' | *[!0-9]*) printf 'an unknown time'; return ;; esac
  printf '%ss' "$(( now - epoch ))"
}

# _wd_done_epoch <issue> -> the stamped done epoch, or empty when unstamped / the reader is absent
# (a standalone watchdog with no gate-broker). Wrapped rather than calling read_done_epoch inline so
# the detector and _wd_intervene_revive's second lock read the same guarded way.
_wd_done_epoch() {
  command -v read_done_epoch >/dev/null 2>&1 || return 0
  read_done_epoch "$1" 2>/dev/null
}

# _wd_land_log <issue> -> the per-issue land log auto_land writes (<state-dir>/land-<issue>.log,
# hub-afk.sh's #198 record), or empty when the state-dir reader is absent (standalone watchdog).
_wd_land_log() {
  command -v _afk_state_dir >/dev/null 2>&1 || return 0
  printf '%s\n' "$(_afk_state_dir)/land-$1.log"
}

# _wd_file_mtime <path> -> the file's mtime epoch, or empty when unreadable. GNU stat first, BSD
# second — the house idiom (gate-broker-detect.sh's _transcript_mtime): this hub is macOS, CI is
# GNU, and a one-flavor probe reads empty on the other (#289).
_wd_file_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}

# _wd_land_in_flight <issue> <now> -> true when a land for this issue is running RIGHT NOW, so the
# watchdog must not read its teardown as an abandoned spoke (#290). Two signals: a LIVE drain whose
# last action is `land #<issue>` (auto_land stamps it before the synchronous land, so it names the
# issue for the land's whole duration — the primary signal), or a land-<issue>.log written within
# HUB_WATCHDOG_LAND_ACTIVE (the fallback for a clobbered/stale last-action). Both fail toward NOT
# in flight — a crashed drain's stale last-action, or an unreadable mtime, must never silence
# condition 2 forever.
# NOT the same as _wd_land_lane_servicing (#285), and the two must not be collapsed: that one reads
# the land lane's ARMED RETRY backoff — a land that already FAILED and is scheduled to be
# re-attempted; this one reads a land executing at this instant.
_wd_land_in_flight() {
  local issue="$1" now="$2" log mtime
  if [ "$(_wd_drain_state)" = "live" ]; then
    case "$(_wd_last_action)" in "land #$issue") return 0 ;; esac
  fi
  log="$(_wd_land_log "$issue")"
  [ -n "$log" ] && [ -f "$log" ] || return 1
  mtime="$(_wd_file_mtime "$log")"
  # _wd_epoch_stale reads an unmeasurable epoch as NOT stale, which negates to "in flight" — the
  # wrong direction for a defer. Screen both operands first so only a real, fresh mtime defers.
  case "$mtime" in '' | *[!0-9]*) return 1 ;; esac
  case "$now" in '' | *[!0-9]*) return 1 ;; esac
  # A FUTURE-dated mtime (clock skew, a corrupted timestamp) is unmeasurable, not fresh: negating
  # _wd_epoch_stale on it would defer until wall-clock caught up to the bogus stamp — an unbounded
  # silence rather than the documented HUB_WATCHDOG_LAND_ACTIVE window. Fail toward firing.
  [ "$mtime" -le "$now" ] || return 1
  ! _wd_epoch_stale "$mtime" "$now" "$HUB_WATCHDOG_LAND_ACTIVE"
}

# _wd_current_state <issue> -> the spoke's last RECORDED lifecycle state from the #300 transition
# log (afk_current_state), or "unknown" when the read API is absent (a standalone watchdog with no
# gate-broker/worktree-lib in scope) or nothing has been recorded. Wrapped rather than calling
# afk_current_state inline so every detector reads the log the same guarded way and a missing API
# degrades to "unknown" — which per #300's contract is NEVER a firing basis NOR a suppression basis
# on its own (each caller falls back to today's side-effect inference on unknown).
_wd_current_state() {
  command -v afk_current_state >/dev/null 2>&1 || { printf 'unknown\n'; return 0; }
  afk_current_state "$1" 2>/dev/null
}

# _wd_state_phase_fresh <issue> <now> -> true when the RECORDED phase's onset (afk_state_onset) is
# still within HUB_WATCHDOG_IDLE_CEILING — i.e. the landing/pushing phase is plausibly in progress,
# not stuck. This BOUNDS the dead-pane defer so it can never silence the reaper-miss backstop forever
# (the sibling _wd_land_in_flight bounds its own defer for exactly this reason — "must never silence
# condition 2 forever"). spoke-push/worktree-land record landing/pushing INTENT-FIRST and leave them
# stuck on a mid-phase crash (#299's silent-stall class), so a phase older than the ceiling has run
# impossibly long: re-arm the backstop. An unmeasurable onset fails toward re-arming (return 1),
# never toward an unbounded silence — the same fail-toward-firing direction _wd_land_in_flight takes
# on an unmeasurable mtime. The worst-case legitimate gate (a first-push seed, tens of minutes) sits
# well under the ceiling, so a healthy phase never re-arms early; only a genuinely stuck one does.
_wd_state_phase_fresh() {
  local onset now="$2"
  onset="$(afk_state_onset "$1" 2>/dev/null)"
  case "$onset" in '' | *[!0-9]*) return 1 ;; esac
  ! _wd_epoch_stale "$onset" "$now" "$HUB_WATCHDOG_IDLE_CEILING"
}

# Condition 2: a dead/crashed pane recover_dead_panes/reap_pass never revived, past the ceiling.
# A DONE-stamped spoke is never a reaper miss (#290): the epoch records that the spoke reached
# terminal state, and a land consuming the ready/<issue> tag flips LIVE slot_state off `done` while
# the worktree still exists — the teardown gap that false-fired on #284.
#
# <done-epoch> MUST be read BEFORE anything on this tick calls slot_state, which is why the
# dispatcher pre-reads it and passes it in rather than letting this read it live: a non-terminal
# slot_state read DELETES the epoch (_afk_note_tip_progress -> clear_done_epoch, deliberate per
# #263 — "non-terminal at this tip ⇒ any live done-epoch is stale"), and the park detectors call
# slot_state ahead of this one every tick. Read live here, the epoch is always already gone and the
# guard is dead code. Omit the argument and it falls back to a live read for a direct caller.
# The guard is therefore self-limiting rather than permanent: it holds only while the last
# slot_state read was terminal, and expires one tick after the spoke is observed non-terminal —
# after which _wd_land_in_flight carries the defer for the rest of the land.
# That defer lives in the DISPATCHER, not here, for the reason documented at condition 4: a
# detector returning 1 falls into the else-branch, which CLEARS the firing-dedup marker — mid-land
# that would let a subsequently-failed land re-fire and double-count (#263).
# _wd_dead_idle_base <issue> -> "<kind>\t<epoch>" — the epoch condition 2's ceiling measures FROM
# and WHICH clock it is, or empty when neither is measurable (#297). max(dispatch, progress), the
# same base the drain's reaper uses (_afk_ceiling_epoch): progress alone is stamped only on a
# branch-tip ADVANCE, so a pane that crashes BEFORE its first commit leaves it empty forever and
# _wd_epoch_stale reads empty as never-fire — condition 2 was structurally blind to the very reaper
# miss it exists to catch. Dispatch is the floor: a spoke has been running since it was dispatched
# whether or not it ever committed.
# Duplicates _afk_ceiling_epoch's max() rather than calling it because the reason string must name
# the WINNER (base=dispatch@N vs base=progress@N tells a triaging human whether the spoke ever
# committed at all). The two are pinned to agree by
# test_dead_idle_base_epoch_agrees_with_the_real_reaper_ceiling — they must never diverge on WHEN a
# spoke is over its ceiling, or the watchdog and the reaper disagree about the same spoke.
# Neither measurable ⇒ empty ⇒ the detector cannot fire, preserving _afk_ceiling_epoch's contract:
# never invent a ceiling with no clock behind it. Both reads are case-screened, so an absent reader
# (a standalone watchdog with no gate-broker) degrades to the other rather than erroring.
_wd_dead_idle_base() {
  local issue="$1" now="${2:-$(_wd_now)}" d p
  d="$(read_dispatch_epoch "$issue" 2>/dev/null)"
  p="$(read_progress_epoch "$issue" 2>/dev/null)"
  case "$d" in '' | *[!0-9]*) d=0 ;; esac
  case "$p" in '' | *[!0-9]*) p=0 ;; esac
  case "$now" in '' | *[!0-9]*) now=0 ;; esac
  # A FUTURE-dated epoch is unmeasurable, not fresh — and taking max() would let it OUTRANK a
  # genuinely stale clock and silence this condition for the whole window (now-epoch goes negative,
  # which is never > the ceiling). That is the unbounded silence #284 was: worse than the blindness
  # this fix removes. Drop each skewed epoch so the other still measures — the same fail-toward-
  # firing rule _wd_land_in_flight applies to its log mtime 40 lines above. Reachable via a clock
  # that steps forward (VM resume, a bad NTP source) while spokes stamp dispatch, then corrects.
  # This screen is ALSO why this cannot simply call _afk_ceiling_epoch: the reaper's base has no
  # such bound, and tier-2 exists to not go blind in the same breath as tier-1.
  [ "$now" -gt 0 ] && [ "$d" -gt "$now" ] && d=0
  [ "$now" -gt 0 ] && [ "$p" -gt "$now" ] && p=0
  # Ties go to progress: at equal epochs it is the more specific statement (the spoke actually
  # committed), and it keeps the label stable for a spoke whose first commit lands on its
  # dispatch second.
  if [ "$p" -gt 0 ] && [ "$p" -ge "$d" ]; then printf 'progress\t%s\n' "$p"; return 0; fi
  [ "$d" -gt 0 ] && printf 'dispatch\t%s\n' "$d"
  return 0
}

_wd_detect_dead_idle() {
  local wt="$1" issue="$2" now="$3" done_epoch="${4-$(_wd_done_epoch "$2")}" \
        base="${5-$(_wd_dead_idle_base "$2" "$3")}"
  command -v slot_state >/dev/null 2>&1 || return 1
  [ -z "$(_spoke_pane_target "$wt" 2>/dev/null)" ] || return 1   # a live pane is the reaper's job
  [ -n "$done_epoch" ] && return 1                               # done-stamped ⇒ not a reaper miss
  # done ⇒ terminal, waiting ⇒ PARKED — neither is a hang. `waiting` must be excluded explicitly
  # now that the base falls back to dispatch (#297): a spoke parked at a gate before its first
  # commit has no progress epoch, so the old progress-only base made this structurally silent for
  # it; measuring from dispatch arms it, and the revive seam checks slot_state nowhere — a spoke
  # parked at an UNAPPROVED plan gate would be resumed headless and implement unreviewed work.
  # A parked spoke is conditions 1/3's (park-unanswered / stale-marker), exactly as the drain's own
  # recover_dead_panes skips it: `case "$state" in done | waiting) continue`.
  case "$(slot_state "$wt" "$issue")" in done | waiting) return 1 ;; esac
  _wd_epoch_stale "${base#*$'\t'}" "$now" "$HUB_WATCHDOG_IDLE_CEILING"
}

# _wd_dead_idle_reason <wt> <issue> <now> [done-epoch] -> the MEASURED firing reason (#290 AC4):
# the age, plus every input the decision turned on — which epoch it measured from, the live
# slot_state, whether a done epoch was present, and what the drain last did — so a future false
# positive is diagnosable from the ledger line alone instead of re-deriving the timeline from four
# state files. Mirrors _wd_park_unanswered_reason's measured-base contract (#283 AC5); the reason
# field is where this file records a base, so _wd_fire's JSON shape needs no new key.
# Takes the SAME pre-read done epoch the detector was given, so the reported classification cannot
# disagree with the one that actually fired (a live re-read here would come back empty every time —
# the park detectors' slot_state call has already cleared it, per #263).
# UPGRADE: this re-reads slot_state for the diagnostic, and as a command-substitution argument to
# _wd_fire it re-runs every tick the condition holds, not just the deduped first firing — thread
# the detector's own read through if a persistent dead pane ever makes the repeat cost matter. It
# is safe today: every slot_state mutation is stamp-once/clear-idempotent, and the dead-idle path
# has no pane, so _permission_pending short-circuits before any tmux capture (not the #269 class).
# The base is read from the SAME _wd_dead_idle_base the detector measured, and the label names
# which clock won (#297): `base=dispatch@N` says the spoke never committed at all — a materially
# different story from a stalled-after-progress `base=progress@N`, and the first thing a human needs
# when triaging. The activity noun follows the base for the same reason: reporting "last progress"
# for a spoke that never made any is how a reader concludes the epoch is broken rather than absent.
# Takes the detector's ALREADY-MEASURED base as $5 for the same reason it takes the pre-read done
# epoch as $4: a live re-read here can disagree with the epoch that actually fired (a concurrent
# revive stamping progress, or a fresh arm clearing both), emitting a self-contradictory ledger line
# — "ceiling 3600s breached" beside a 5s age. Falls back to a live read for a direct caller.
_wd_dead_idle_reason() {
  local wt="$1" issue="$2" now="$3" done_epoch="${4-$(_wd_done_epoch "$2")}" \
        base="${5-$(_wd_dead_idle_base "$2" "$3")}" kind epoch
  kind="${base%%$'\t'*}"; epoch="${base#*$'\t'}"
  # The activity noun follows the base: reporting "last progress" for a spoke that never made any
  # is how a reader concludes the epoch is BROKEN rather than absent — the opposite of the #290 AC4
  # diagnosability this line exists for.
  case "$kind" in
    dispatch)
      printf 'reaper missed a dead/idle pane: no pane, dispatched (no commit yet) %s ago' \
        "$(_wd_age_seconds "$epoch" "$now")" ;;
    progress)
      printf 'reaper missed a dead/idle pane: no pane, last progress %s ago' \
        "$(_wd_age_seconds "$epoch" "$now")" ;;
    *)
      kind="none"; epoch=""
      printf 'reaper missed a dead/idle pane: no pane, no dispatch or progress epoch to measure from' ;;
  esac
  printf ' (ceiling %ss; base=%s@%s, slot_state=%s, done-epoch=%s, last-action=%s)' \
    "$HUB_WATCHDOG_IDLE_CEILING" "$kind" "${epoch:-?}" "$(slot_state "$wt" "$issue" 2>/dev/null)" \
    "${done_epoch:-none}" "$(_wd_last_action)"
}

# Condition 3: a stale blocked/ marker reconcile_markers should have cleared.
_wd_detect_stale_marker() { _wd_blocked_stale "$1" "$2"; }

# _wd_land_lane_servicing <issue> -> true when the drain's LAND lane has a FRESH armed retry
# (warned-state-<issue>-land next-due in the FUTURE): the drain is actively re-attempting the land
# on its backoff, so condition 4 must defer — the analogue of the answer-lane servicing defer
# (_wd_supervisor_servicing) for the LAND lane (#285 AC5). Reads the same backoff record auto_land
# arms (_afk_warned_next, in scope via gate-broker); absent/elapsed ⇒ not servicing (fall through).
_wd_land_lane_servicing() {
  local issue="$1" now next
  command -v _afk_warned_next >/dev/null 2>&1 || return 1
  next="$(_afk_warned_next "$issue" land 2>/dev/null)"
  case "$next" in '' | *[!0-9]*) return 1 ;; esac
  now="$(_wd_now)"
  [ "$next" -gt "$now" ]
}

# _wd_land_base_ref <wt> -> the ref a land would merge the branch INTO: the local base branch
# (refs/heads/<base>) when present — the same tip worktree-land merges onto — else origin/<base>.
# Resolves <base> via the canonical wt_base_branch (in scope via worktree-lib); defaults to main.
_wd_land_base_ref() {
  local wt="$1" base=""
  command -v wt_base_branch >/dev/null 2>&1 && base="$(wt_base_branch "$wt" 2>/dev/null || true)"
  [ -n "$base" ] || base=main
  if git -C "$wt" show-ref --verify --quiet "refs/heads/$base" 2>/dev/null; then printf '%s\n' "$base"; return; fi
  if git -C "$wt" show-ref --verify --quiet "refs/remotes/origin/$base" 2>/dev/null; then printf 'origin/%s\n' "$base"; return; fi
  printf '%s\n' "$base"
}

# _wd_terminal_marker_kind <wt> <issue> -> `ready`, `accept`, or empty — WHICH terminal marker sits
# at the branch tip (#292). slot_state reads BOTH as `done`, which is why condition 4 could not tell
# them apart: ready/ means the drain SHOULD have landed and did not (a genuine shortfall, #274's
# class), while accept/ is the deliberate human-eyeball terminal the drain MUST NOT land (auto_land
# lands only _ready_at_tip: "accept/ awaits a human sign-off").
# READY IS CHECKED FIRST, mirroring slot_state's own `for kind in ready accept` precedence: when
# both tags sit at the tip, the watchdog must classify off the SAME marker that made the spoke read
# `done`, or the two disagree about what the spoke even is.
# Empty when neither is at the tip (or the probe fails) — callers must then keep the historical
# afk-defect path, never the quieter accept one: an unreadable marker must not silence a defect.
_wd_terminal_marker_kind() {
  local wt="$1" issue="$2" kind
  for kind in ready accept; do
    _wd_tag_at_tip "$wt" "$kind" "$issue" && { printf '%s\n' "$kind"; return 0; }
  done
  return 0
}

# _wd_log_terminal_kind <issue> -> `ready`, `accept`, or empty — the terminal marker kind read from
# the #300 transition log's RECORDED state instead of the tip tag (#303/#292). spoke-ready records
# `accepted` as a state DISTINCT from `ready` (its _tlog_state_for_kind), so this is the log-native,
# structurally-unambiguous classifier: a human-sign-off close is `accepted`, which the caller routes
# to the non-escalating accept-unsigned path — a tag that is stale or mid-move can no longer make it
# read as a ready drain shortfall. Empty when the log records no terminal state (unknown, or a
# non-terminal state like landing/pushing), so the caller falls back to the tip-tag probe
# (_wd_terminal_marker_kind) — never firing NOR reclassifying on unknown alone (#300 contract). Maps
# the recorded `accepted` to the classifier's `accept` vocabulary so both readers speak one language.
_wd_log_terminal_kind() {
  case "$(_wd_current_state "$1")" in
    ready)    printf 'ready\n' ;;
    accepted) printf 'accept\n' ;;
  esac
}

# _wd_terminal_onset <issue> -> the onset ts of the last transition ONLY when it is a terminal marker
# (ready|accepted); empty otherwise (#303). This is the log-native replacement for condition 4's
# done-epoch staleness base: the transition is stamped when the spoke actually reached ready/accepted
# (spoke-ready), whereas the done epoch is a slot_state side-effect that pre-ages during a pre-ready
# park (#263). Empty for a non-terminal/unknown state so the caller falls back to the done epoch —
# the log adds precision on the healthy path without ever becoming a firing basis on its own.
_wd_terminal_onset() {
  case "$(_wd_current_state "$1")" in
    ready | accepted) afk_state_onset "$1" 2>/dev/null ;;
  esac
}

# _wd_base_name <wt> -> the bare base-branch name (no origin/ prefix); defaults to main.
_wd_base_name() {
  local wt="$1" base=""
  command -v wt_base_branch >/dev/null 2>&1 && base="$(wt_base_branch "$wt" 2>/dev/null || true)"
  [ -n "$base" ] || base=main
  printf '%s\n' "$base"
}

# _wd_own_commits <wt> -> how many commits the branch has that the base's history does not — i.e.
# what the spoke itself authored. Zero is the #286 close-without-code shape: the spoke judged the
# work already shipped and minted accept/ at the branch point, so a land would merge nothing and
# close the issue as SHIPPED — not the decision actually pending.
# Excludes BOTH refs/heads/<base> AND refs/remotes/origin/<base>, whichever exist, rather than
# measuring `<land-base>..HEAD`. Two ways that single-base measure lies, in opposite directions:
#   * spokes branch from origin/<base> (wt_base_start_point, "the hub's local base may lag or carry
#     unpushed work"), while _wd_land_base_ref PREFERS the local ref — so a lagging local base makes
#     a genuinely empty branch report the base's own missing commits as "own work", silently
#     dropping the zero-diff clause on exactly #292's headline scenario;
#   * and a base that has already absorbed the branch reports 0 for real authored work, which would
#     tell a human not to land it.
# Subtracting every base ref that exists answers the question actually asked — did this spoke write
# anything the base does not already have — under both skews.
# Empty when nothing is measurable, so the reason OMITS the claim rather than asserting a count it
# did not measure: silence is the safe direction here, a wrong "no own commits" is not.
_wd_own_commits() {
  local wt="$1" base ref n
  local -a nots=()
  base="$(_wd_base_name "$wt")"
  for ref in "refs/heads/$base" "refs/remotes/origin/$base"; do
    git -C "$wt" show-ref --verify --quiet "$ref" 2>/dev/null && nots+=("$ref")
  done
  [ "${#nots[@]}" -gt 0 ] || return 0
  n="$(git -C "$wt" rev-list --count HEAD --not "${nots[@]}" 2>/dev/null)" || return 0
  case "$n" in '' | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "$n"
}

# _wd_accept_unsigned_reason <wt> <issue> [conflicts] -> the honest remediation for an accept/ spoke.
# NOT "human land": on #286 that told a human to land a zero-diff branch, which would have closed
# the issue as shipped when the pending decision was "confirm the duplicate finding, or reject it
# and re-kick". Same reason-honesty contract #285 established for the conflicted case, one
# marker-kind over — and for the same reason it must ALSO carry #285's conflict list when there is
# one: accept/ is normally eyeball-THEN-land, and condition 4 is the only thing that probes
# mergeability for it (auto_land never touches an accept/ branch, so it never routes a conflict
# resolution either). Dropping the files would send a human who approves the code straight into an
# unannounced conflict — exactly the misdirection #285 exists to prevent.
# <conflicts> and <base> are passed in already-measured rather than re-probed: the base that is
# PRINTED must be the one the count was measured against (the measured-base contract this file
# documents at _wd_dead_idle_reason), and the reason re-runs every tick as a _wd_fire argument.
_wd_accept_unsigned_reason() {
  local wt="$1" issue="$2" conflicts="${3:-}" own base
  base="$(_wd_land_base_ref "$wt")"
  printf 'accept/%s awaits human sign-off (escalate-only: the drain must NOT land accept/ — it is the human-eyeball terminal)' "$issue"
  own="$(_wd_own_commits "$wt")"
  [ "$own" = "0" ] && printf ' — no own commits vs %s: confirm close-without-code, do not land' "$base"
  [ -n "$conflicts" ] && printf ' — NOTE: the branch also conflicts with %s on: %s — resolve on the spoke before any land' \
    "$base" "$conflicts"
  return 0
}

# _wd_land_conflicts <wt> -> the space-separated conflicting file(s) when the checked-out branch
# does NOT merge cleanly into the base, else empty. Uses `git merge-tree --write-tree --name-only`
# (git >= 2.38): rc 0 = mergeable (empty), rc 1 = conflict (line 1 is the tree OID; the conflicted
# paths follow up to the first blank line), any other rc = probe unavailable/errored ⇒ empty so a
# tooling gap never mislabels a branch as conflicted (it stays on the mergeable path).
_wd_land_conflicts() {
  local wt="$1" branch base out rc
  branch="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  base="$(_wd_land_base_ref "$wt")"
  [ -n "$branch" ] && [ -n "$base" ] || return 0
  out="$(git -C "$wt" merge-tree --write-tree --name-only "$base" "$branch" 2>/dev/null)"; rc=$?
  case "$rc" in
    0) return 0 ;;   # mergeable
    1) ;;            # conflict — extract the file list below
    *) return 0 ;;   # probe error (old git / bad ref) — never claim a conflict
  esac
  printf '%s\n' "$out" | awk 'NR==1{next} /^$/{exit} {printf "%s ", $0}'
}

# Condition 4: a mergeable (ready-at-tip) branch auto_land terminal-skipped. NOT blocked-at-tip
# (a deliberate skip is never a false-skip), issue still open, un-landed past the ceiling.
# #303 (#300 step 4): staleness is measured from the RECORDED terminal transition (ready/accepted
# onset, _wd_terminal_onset) — stamped by spoke-ready when the spoke actually reached the state — in
# preference to the DONE epoch. The done epoch is a slot_state side-effect and remains the FALLBACK
# when the log is unknown/absent (never fire on unknown alone, #300): a standalone watchdog, or a
# spoke that pre-dates the writer layer, still measures from the epoch exactly as before. Both bases
# already dodge the #263 pre-ready-park pre-aging the progress epoch caused — the done epoch is
# stamped on the first `done` tick, the transition when the state is entered, so neither reads stale
# during the park. The LAND-lane servicing defer (#285 AC5) and the mergeability probe both live in
# the DISPATCHER, NOT here: a servicing tick must neither fire NOR clear the fire-dedup marker
# (clearing mid-service would let one persistent conflict re-fire and double-count, #263), so it is
# gated BEFORE this detector — mirroring how the answer lane defers the INTERVENTION, not the detector.
_wd_detect_mergeable_skipped() {
  local wt="$1" issue="$2" now="$3" base
  command -v slot_state >/dev/null 2>&1 || return 1
  [ "$(slot_state "$wt" "$issue")" = "done" ] || return 1
  _wd_tag_at_tip "$wt" blocked "$issue" && return 1              # deliberately blocked → not a skip
  base="$(_wd_terminal_onset "$issue")"                          # #303: the recorded ready/accepted onset
  [ -n "$base" ] || base="$(read_done_epoch "$issue" 2>/dev/null)"   # unknown log → today's done-epoch base
  _wd_epoch_stale "$base" "$now" "$HUB_WATCHDOG_LAND_CEILING" || return 1
  _wd_issue_open "$issue"                                        # a closed issue was landed, not skipped
}

# Condition 5: the drain supervisor crashed (armed state, dead heartbeat).
_wd_detect_supervisor_dead() { [ "$(_wd_drain_state)" = "stale" ]; }

# --- the 5 scripted interventions (each behind a HUB_WATCHDOG_*_CMD seam) ------
# The seam receives the worktree + issue as positional args ($1 $2 after the argv0 sentinel).
# _wd_last_action -> the drain supervisor's most-recent-action label (hub-afk.sh's #202 record,
# e.g. "answer #5"), read directly from the shared state dir so the watchdog needs no hub-afk.sh
# source. Honors the AFK_LAST_ACTION override exactly as hub-afk.sh does (tests point it at a
# scratch file). Empty when unreadable / the state dir reader is absent (standalone watchdog).
_wd_last_action() {
  local f="${AFK_LAST_ACTION:-}"
  if [ -z "$f" ]; then
    command -v _afk_state_dir >/dev/null 2>&1 || return 0
    f="$(_afk_state_dir)/last-action"
  fi
  [ -f "$f" ] && head -n1 "$f" 2>/dev/null || true
}

# _wd_supervisor_servicing <issue> -> true when the tier-1 drain is CURRENTLY working this
# park, so the watchdog must NOT run a second decide_and_act and race the in-flight answerer
# (#265 AC4; the #89 stale-answer/strand hazard). Two positive signals: a FRESH answer-delivery
# stamp (the supervisor delivered within the ceiling), or — for the never-attempted window where
# no stamp exists yet — a LIVE drain whose LAST ACTION names this issue (any drain pass touching
# it: an `answer` mid-reasoning pre-delivery, or a revive/nudge/land in flight). Deferring on any
# such action is deliberately conservative — never race the drain while it is on this issue. The
# defer can't wedge: the firing is still ledgered, and the answer lane's re-answer ceiling
# (-> blocked/<issue>) plus the supervisor-dead detector bound a genuinely stuck drain. Either ⇒ defer.
_wd_supervisor_servicing() {
  local issue="$1" attempt
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  case "$attempt" in
    '' | *[!0-9]*) ;;   # no delivery yet — fall through to the heartbeat + last-action check
    *) _wd_epoch_stale "$attempt" "$(_wd_now)" "$HUB_WATCHDOG_PARK_CEILING" || return 0 ;;
  esac
  [ "$(_wd_drain_state)" = "live" ] || return 1
  case "$(_wd_last_action)" in *"#$issue") return 0 ;; esac
  return 1
}

