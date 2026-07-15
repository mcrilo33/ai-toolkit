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
# CRASH CONSISTENCY / ATOMICITY. macOS ships no flock(1), so appends rely on
# POSIX O_APPEND semantics: each record is ONE printf of ONE line (well under
# 4KB) opened in append mode — concurrent writers interleave whole lines on a
# local filesystem. Readers accept only COMPLETE lines (a torn trailing line —
# no closing brace + newline — is ignored), so a crash mid-write costs one
# record, never a wrong parse. Writes are best-effort and NEVER fail the calling
# operation (the hooks' always-exit-0 discipline): a missing expected record
# reads as "unknown", and per #300's contract "unknown" is never a firing basis
# by itself.
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

# _tlog_append <issue> <line> -> append one complete record line. Best-effort:
# ALWAYS rc 0, never fails the calling operation.
_tlog_append() {
  local issue="$1" line="$2" f dir
  f="$(_tlog_file "$issue")" || return 0
  dir="${f%/*}"
  mkdir -p "$dir" 2>/dev/null || return 0
  printf '%s\n' "$line" >> "$f" 2>/dev/null || true
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

# _tlog_field <line> <key> -> the string value of "key":"value" (first match),
# or the numeric value of "key":123. Empty when absent.
_tlog_field() {
  local line="$1" key="$2" v
  v="$(printf '%s' "$line" | sed -n -E 's/.*"'"$key"'":"([^"]*)".*/\1/p')"
  [ -n "$v" ] || v="$(printf '%s' "$line" | sed -n -E 's/.*"'"$key"'":([0-9]+).*/\1/p')"
  printf '%s\n' "$v"
}

# _tlog_last_transition <issue> -> the last complete kind=transition line.
_tlog_last_transition() {
  _tlog_complete_lines "$1" | awk '/"kind":"transition"/ { l = $0 } END { if (l != "") print l }'
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
# one (park transitions mint it), empty when none.
afk_current_episode() {
  _tlog_complete_lines "$1" \
    | awk '/"kind":"transition"/ && /"episode":"/ { l = $0 } END { if (l != "") print l }' \
    | sed -n -E 's/.*"episode":"([^"]*)".*/\1/p'
}

# afk_last_service_event <issue> <episode> [lane] -> the last complete event
# line matching the episode (and lane when given), whole line on stdout.
afk_last_service_event() {
  local issue="$1" episode="$2" lane="${3:-}"
  _tlog_complete_lines "$issue" | awk -v ep="\"episode\":\"$episode\"" -v ln="$lane" '
    /"kind":"event"/ {
      if (index($0, ep) == 0) next
      if (ln != "" && index($0, "\"lane\":\"" ln "\"") == 0) next
      l = $0
    }
    END { if (l != "") print l }'
}

# afk_lane_event_count <issue> <lane> [episode] -> how many events this lane
# recorded (optionally within one episode). 0 when none.
afk_lane_event_count() {
  local issue="$1" lane="$2" episode="${3:-}" n
  n="$(_tlog_complete_lines "$issue" | awk -v ln="\"lane\":\"$lane\"" -v ep="$episode" '
    /"kind":"event"/ {
      if (index($0, ln) == 0) next
      if (ep != "" && index($0, "\"episode\":\"" ep "\"") == 0) next
      c++
    }
    END { print c + 0 }')"
  printf '%s\n' "${n:-0}"
}
