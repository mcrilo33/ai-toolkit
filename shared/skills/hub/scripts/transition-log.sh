#!/usr/bin/env bash
# transition-log.sh — the per-spoke lifecycle transition log (issue #300, Option C, phase 1).
#
# WHAT THIS IS. An append-only JSONL log, one file per issue, where the ACTOR that
# causes a lifecycle transition records it at the moment it happens — the same
# principle the marker tags already prove (spoke-ready.sh moves the tag AND is the
# process that knows why), extended to every transition and enriched with the
# lane/episode dimensions tags cannot carry. Detectors then READ recorded
# transitions instead of inferring state from side-effects (progress-epoch aging,
# pane text, transcript mtimes) — the root cause behind the #263/#265/#283/#288/
# #290/#292 watchdog false-fire family.
#
# PHASE 1 (this file): the library only — append + read API + tests. NOTHING
# sources it yet; call-site wiring (shadow writes), detector conversion, and the
# slot_state demotion land as the follow-up migration steps in #300's plan.
#
# LOCATION. <git-common-dir>/ai-toolkit-afk/transitions/<issue>.jsonl — the state
# dir every actor already shares (events spool precedent: spoke-ready.sh inlines
# the same resolver); survives worktree teardown, never dirties a tree, honors
# the AFK_STATE_DIR override for tests. Per-issue files avoid cross-issue lock
# contention and GC trivially.
#
# CRASH CONSISTENCY / ATOMICITY. Appends are serialized with a per-issue `mkdir`
# lock (the one atomic, portable filesystem primitive — macOS has no flock(1)),
# so a record is written whole regardless of its size. A crashed writer's stale
# lock is force-broken past a bound (see _tlog_append) so a dead process can't
# wedge the log. Readers additionally tolerate a torn TRAILING line (a crash
# between the printf and rmdir, or a pre-lock legacy write): a complete record is
# `^{...}$`, and an unterminated last fragment is dropped. Writes are best-effort
# and NEVER fail the calling operation (the hooks' always-exit-0 discipline): a
# missing expected record reads as "unknown", and per #300's contract "unknown"
# is never a firing basis by itself.
#
# RECORD SHAPE (v1). One JSON object per line, flat, fixed keys:
#   {"v":1,"ts":<epoch>,"issue":<n>,"kind":"transition","to":"<state>",
#    "actor":"<script>","cause":"<why>","run":"<spoke_run_id>","evidence":{...}}
#   {"v":1,"ts":<epoch>,"issue":<n>,"kind":"event","event":"<name>",
#    "lane":"<lane>","episode":"<sig>:<onset>","actor":"<script>","evidence":{...}}
# `run` is taken from $AFK_TLOG_RUN when set (worktree-new exports the
# spoke_run_id); `evidence` is a caller-supplied pre-encoded JSON object (or
# omitted). String fields are escaped here; callers pass raw text.
#
# BASH 3.2 COMPATIBLE. ASCII only, no assoc arrays, no ${var,,}.

# _tlog_state_dir -> the shared afk state dir (same resolver as the events spool
# and gate-broker-markers.sh; duplicated deliberately so spoke-side deployments
# can source this file alone).
_tlog_state_dir() {
  local common
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || common="/tmp"
  printf '%s\n' "${AFK_STATE_DIR:-$common/ai-toolkit-afk}"
}

# _tlog_file <issue> -> the per-issue log path. Empty (rc 1) on a non-numeric
# issue so a caller bug cannot scatter stray files across the state dir.
_tlog_file() {
  local issue="$1"
  case "$issue" in '' | *[!0-9]*) return 1 ;; esac
  printf '%s\n' "$(_tlog_state_dir)/transitions/$issue.jsonl"
}

# _tlog_json_escape <text> -> the text with backslashes, double quotes, and
# control characters (tab/newline) made JSON-safe. Values here are shell-plain
# tokens in practice; this guards the free-text cause/reason fields.
_tlog_json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n\t' '  '
}

# _tlog_append <issue> <line> -> append one complete record line under a
# per-issue mkdir lock. Best-effort: ALWAYS rc 0, never fails the calling
# operation.
#
# WHY A LOCK (not "small lines are atomic"). An earlier version relied on a
# single `printf >>` being atomic below a size threshold. Adversarial testing
# (2026-07-15) disproved it: bash's builtin printf tears interleaved appends at
# ~1KB on this hardware, probabilistically, and free-text fields (cause/actor/
# episode) can push a line arbitrarily large regardless. A torn record mid-file
# is worse than a torn TRAILING line — readers tolerate only the latter. So the
# write is serialized with `mkdir` (the one filesystem primitive that is atomic
# and portable — macOS has no flock(1)).
#
# WINNER-ONLY WRITES (the load-bearing invariant). A writer appends ONLY after
# its OWN `mkdir "$lock"` succeeds. A writer that never wins the lock DROPS its
# record rather than appending unlocked — because per #300's contract a MISSING
# record reads as "unknown" (safe, never a firing basis), whereas a TORN record
# corrupts a sibling's complete-looking line (unsafe). An earlier revision let
# losers fall through to a naked append at the spin ceiling; adversarial testing
# reproduced torn writes at just 2+ writers piled behind one stale lock. Never
# again: no path writes without holding.
#
# STALE-LOCK BREAK. A crashed writer's lock would wedge the log forever, so every
# _tlog_lock_wait spins a waiter rmdir's the lock (breaking a possibly-dead
# holder) and keeps racing — it does NOT then write on faith. Since a live hold
# is one printf (microseconds), the ~1s break cadence only ever fires on a truly
# dead holder; breaking a live holder would require it frozen >1s mid-printf, and
# even then that holder is not writing, so the winner's write stays clean.
_tlog_lock_wait="${AFK_TLOG_LOCK_WAIT:-50}"   # spins between stale-lock breaks (~1s at 20ms)
_tlog_lock_max="${AFK_TLOG_LOCK_MAX:-250}"    # hard spin cap (~5s) before dropping the record

_tlog_append() {
  local issue="$1" line="$2" f dir lock i="0" held="0"
  f="$(_tlog_file "$issue")" || return 0
  dir="${f%/*}"
  mkdir -p "$dir" 2>/dev/null || return 0
  lock="$f.lock"
  while [ "$i" -lt "$_tlog_lock_max" ]; do
    if mkdir "$lock" 2>/dev/null; then held="1"; break; fi
    i=$(( i + 1 ))
    # Periodically break a possibly-stale lock, then keep racing (winner-only).
    [ $(( i % _tlog_lock_wait )) -eq 0 ] && { rmdir "$lock" 2>/dev/null || true; }
    sleep 0.02 2>/dev/null || sleep 1
  done
  if [ "$held" = "1" ]; then
    printf '%s\n' "$line" >> "$f" 2>/dev/null || true
    rmdir "$lock" 2>/dev/null || true
  fi
  # held=0 (never acquired in ~5s) -> drop the record; unknown > corrupt.
  return 0
}

# afk_tlog_transition <issue> <to> <actor> <cause> [evidence-json] [episode]
# Record entering state <to>. `episode` is set by park transitions (the park
# signature + onset the broker already mints for the reanswer key).
afk_tlog_transition() {
  local issue="$1" to="$2" actor="$3" cause="$4" evidence="${5:-}" episode="${6:-}" line run
  line="{\"v\":1,\"ts\":$(date +%s),\"issue\":${issue:-0}"
  line="$line,\"kind\":\"transition\",\"to\":\"$(_tlog_json_escape "$to")\""
  line="$line,\"actor\":\"$(_tlog_json_escape "$actor")\""
  line="$line,\"cause\":\"$(_tlog_json_escape "$cause")\""
  run="${AFK_TLOG_RUN:-}"
  [ -n "$run" ] && line="$line,\"run\":\"$(_tlog_json_escape "$run")\""
  [ -n "$episode" ] && line="$line,\"episode\":\"$(_tlog_json_escape "$episode")\""
  [ -n "$evidence" ] && line="$line,\"evidence\":$evidence"
  line="$line}"
  _tlog_append "$issue" "$line"
}

# afk_tlog_event <issue> <event> <actor> [lane] [episode] [evidence-json]
# Record an action WITHIN a state (answer_delivered, nudge, revive, ...).
afk_tlog_event() {
  local issue="$1" event="$2" actor="$3" lane="${4:-}" episode="${5:-}" evidence="${6:-}" line run
  line="{\"v\":1,\"ts\":$(date +%s),\"issue\":${issue:-0}"
  line="$line,\"kind\":\"event\",\"event\":\"$(_tlog_json_escape "$event")\""
  line="$line,\"actor\":\"$(_tlog_json_escape "$actor")\""
  run="${AFK_TLOG_RUN:-}"
  [ -n "$run" ] && line="$line,\"run\":\"$(_tlog_json_escape "$run")\""
  [ -n "$lane" ] && line="$line,\"lane\":\"$(_tlog_json_escape "$lane")\""
  [ -n "$episode" ] && line="$line,\"episode\":\"$(_tlog_json_escape "$episode")\""
  [ -n "$evidence" ] && line="$line,\"evidence\":$evidence"
  line="$line}"
  _tlog_append "$issue" "$line"
}

# _tlog_complete_lines <issue> -> every COMPLETE record line (torn trailing line
# dropped: a complete line ends in '}' and, being printf'd with \n, is the only
# kind awk's line-splitting yields whole; the last unterminated fragment is
# excluded by requiring the closing brace).
_tlog_complete_lines() {
  local f
  f="$(_tlog_file "$1")" || return 0
  [ -f "$f" ] || return 0
  awk '/^\{.*\}$/ { print }' "$f" 2>/dev/null
  return 0
}

# _tlog_head <line> -> the record with the caller-supplied evidence suffix
# stripped. Both builders append `,"evidence":...` strictly LAST, so everything
# before it is lib-written, escaped, fixed-key content — the ONLY safe surface
# for field extraction. Matching the whole line lets an evidence key shadow a
# top-level field (greedy sed/index take the rightmost/any occurrence): a
# reconciler's evidence naming expected/actual states would hijack
# afk_current_state, an evidence ts would corrupt ages (validation findings,
# 2026-07-15). Every reader below extracts from this head only.
_tlog_head() {
  printf '%s\n' "${1%%,\"evidence\":*}"
}

# _tlog_field <line> <key> -> the string value of "key":"value" (first match),
# or the numeric value of "key":123, read from the pre-evidence head only.
# Empty when absent.
_tlog_field() {
  local head key="$2" v
  head="$(_tlog_head "$1")"
  v="$(printf '%s' "$head" | sed -n -E 's/.*"'"$key"'":"([^"]*)".*/\1/p')"
  [ -n "$v" ] || v="$(printf '%s' "$head" | sed -n -E 's/.*"'"$key"'":([0-9]+).*/\1/p')"
  printf '%s\n' "$v"
}

# _tlog_last_transition <issue> -> the last complete kind=transition line
# (kind matched on the pre-evidence head, so an event whose evidence quotes
# "kind":"transition" cannot masquerade).
_tlog_last_transition() {
  _tlog_complete_lines "$1" | awk '{
      h = $0; i = index(h, ",\"evidence\":"); if (i) h = substr(h, 1, i - 1)
      if (index(h, "\"kind\":\"transition\"")) l = $0
    }
    END { if (l != "") print l }'
}

# afk_current_state <issue> -> the state name of the last recorded transition,
# or "unknown" (rc 0 always; "unknown" is never a firing basis, #300).
afk_current_state() {
  local last
  last="$(_tlog_last_transition "$1")"
  if [ -z "$last" ]; then printf 'unknown\n'; return 0; fi
  _tlog_field "$last" to
}

# afk_state_onset <issue> -> the ts of the last transition, empty when unknown.
afk_state_onset() {
  local last
  last="$(_tlog_last_transition "$1")"
  [ -n "$last" ] || return 0
  _tlog_field "$last" ts
}

# afk_age_in_state <issue> [now] -> seconds since the last transition, or empty
# when the state is unknown (callers must treat empty as not-a-firing-basis).
afk_age_in_state() {
  local onset now="${2:-$(date +%s)}"
  onset="$(afk_state_onset "$1")"
  [ -n "$onset" ] || return 0
  printf '%s\n' "$(( now - onset ))"
}

# afk_current_episode <issue> -> the episode of the last transition that carried
# one (park transitions mint it), empty when none. All matching happens on the
# pre-evidence head (see _tlog_head).
afk_current_episode() {
  _tlog_complete_lines "$1" | awk '{
      h = $0; i = index(h, ",\"evidence\":"); if (i) h = substr(h, 1, i - 1)
      if (index(h, "\"kind\":\"transition\"") && index(h, "\"episode\":\"")) l = h
    }
    END { if (l != "") print l }' \
    | sed -n -E 's/.*"episode":"([^"]*)".*/\1/p'
}

# afk_last_service_event <issue> <episode> [lane] -> the last complete event
# line matching the episode (and lane when given), whole line on stdout.
afk_last_service_event() {
  local issue="$1" episode="$2" lane="${3:-}"
  _tlog_complete_lines "$issue" | awk -v ep="\"episode\":\"$episode\"" -v ln="$lane" '{
      h = $0; i = index(h, ",\"evidence\":"); if (i) h = substr(h, 1, i - 1)
      if (index(h, "\"kind\":\"event\"") == 0) next
      if (index(h, ep) == 0) next
      if (ln != "" && index(h, "\"lane\":\"" ln "\"") == 0) next
      l = $0
    }
    END { if (l != "") print l }'
}

# afk_lane_last_event <issue> <lane> [episode] -> the last complete event line on
# this lane (optionally within one episode), whole line on stdout; empty when the
# lane has no events (rc 0 always — an absent record reads "unknown", never an
# error). The symmetric partner of afk_lane_event_count: same lane + OPTIONAL
# episode filter, last matching line instead of a count.
#
# afk_last_service_event cannot serve a lane whose events carry no episode: it
# matches `"episode":"<ep>"` unconditionally, so an episode-LESS record never
# matches. A per-ISSUE lane — the drain's land lane, which has no park episode to
# key on (#318) — needs to read back its own most recent record, hence this.
# Matching is on the pre-evidence head only (see _tlog_head), so a caller's
# free-text evidence naming a lane cannot masquerade as one.
afk_lane_last_event() {
  local issue="$1" lane="$2" episode="${3:-}"
  _tlog_complete_lines "$issue" | awk -v ln="\"lane\":\"$lane\"" -v ep="$episode" '{
      h = $0; i = index(h, ",\"evidence\":"); if (i) h = substr(h, 1, i - 1)
      if (index(h, "\"kind\":\"event\"") == 0) next
      if (index(h, ln) == 0) next
      if (ep != "" && index(h, "\"episode\":\"" ep "\"") == 0) next
      l = $0
    }
    END { if (l != "") print l }'
  return 0
}

# afk_lane_event_count <issue> <lane> [episode] -> how many events this lane
# recorded (optionally within one episode). 0 when none.
afk_lane_event_count() {
  local issue="$1" lane="$2" episode="${3:-}" n
  n="$(_tlog_complete_lines "$issue" | awk -v ln="\"lane\":\"$lane\"" -v ep="$episode" '{
      h = $0; i = index(h, ",\"evidence\":"); if (i) h = substr(h, 1, i - 1)
      if (index(h, "\"kind\":\"event\"") == 0) next
      if (index(h, ln) == 0) next
      if (ep != "" && index(h, "\"episode\":\"" ep "\"") == 0) next
      c++
    }
    END { print c + 0 }')"
  printf '%s\n' "${n:-0}"
}
