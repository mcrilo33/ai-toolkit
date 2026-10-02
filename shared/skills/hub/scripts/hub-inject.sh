#!/usr/bin/env bash
# hub-inject.sh -- the ONE Orca delivery module of the /afk control plane (issue #251, rewritten
# for Orca in #365). Every answer, approval and nudge reaches a spoke through two verbs:
#   * deliver_reply <wt> <msg_id> <text>   answer a worker's `ask` by message id. The ack IS the
#                                           proof of delivery.
#   * deliver_text  <wt> <text> [proof] [esc]  type into the agent's terminal with
#                                           `terminal send --enter --wait-submit`. The proof is
#                                           the stage Orca observed (turn_started by default).
# rc 0 = delivered, rc 1 = not. Silence is never resent within the tick: the caller retries on its
# next tick. Plus the transcript locators the idle clocks still read. Sourceable on its own.
set -uo pipefail

HUB_INJECT_SCRIPT_DIR="${HUB_INJECT_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# worktree-lib.sh brings orca-lib.sh (every Orca verb) and wt_tlog_event. Source it only when a
# parent has not already (gate-broker sources it before us); same dual-layout ladder as our
# siblings. HUB_INJECT_WT_LIB / AFK_WT_LIB win for tests.
if ! declare -F orca_reply >/dev/null 2>&1; then
  _hi_top="${_AFK_TOPLEVEL:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
  for _cand in \
    "${HUB_INJECT_WT_LIB:-}" "${AFK_WT_LIB:-}" \
    "$HUB_INJECT_SCRIPT_DIR/worktree-lib.sh" "$HUB_INJECT_SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
    "${_hi_top:+$_hi_top/scripts/worktree-lib.sh}" "${_hi_top:+$_hi_top/.ai-toolkit/scripts/worktree-lib.sh}"; do
    if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
  done
  unset _cand _hi_top
fi

# identity.sh (#361): the delivery events take the issue from a worktree's recorded identity before
# the branch slug. This is the one loader the gate-broker modules rely on. Same dual layout as the
# siblings plus the _AFK_TOPLEVEL fallbacks a self-copy drain needs; absent => the reader is simply
# undefined and every site keeps its branch-slug inference.
if ! declare -F ai_toolkit_identity_issue_at >/dev/null 2>&1; then
  _hi_top="${_AFK_TOPLEVEL:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
  for _cand in \
    "$HUB_INJECT_SCRIPT_DIR/identity.sh" "$HUB_INJECT_SCRIPT_DIR/../../../hooks/lib/identity.sh" \
    "$HUB_INJECT_SCRIPT_DIR/../../../hooks/scripts/lib/identity.sh" \
    "${_hi_top:+$_hi_top/shared/hooks/lib/identity.sh}" "${_hi_top:+$_hi_top/.ai-toolkit/scripts/identity.sh}"; do
    if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
  done
  unset _cand _hi_top
fi

# Guarded log fallback: gate-broker.sh defines its own log() first; this fires for a standalone source.
declare -F log >/dev/null 2>&1 || log() { printf '%s\n' "$*" >&2; }

# --- #300 step 3b delivery-event shadow writers -------------------------------
# The delivery verbs are the ONE place that KNOWS a delivery verdict, so they record it as a
# transition-log EVENT at the moment they compute it. Shadow-only + best-effort: a no-op when the
# log lib is unavailable or the issue is not derivable, and it NEVER changes a return value. The
# issue comes from AFK_TLOG_ISSUE, else the worktree's recorded identity (#361), else its branch
# slug; lane/episode ride AFK_TLOG_LANE / AFK_TLOG_EPISODE (the permission lane threads them in).

# _hi_issue_for_wt <wt> -> the numeric issue this worktree belongs to (AFK_TLOG_ISSUE wins, then
# the identity record, else the branch slug's leading digits), or empty (rc 1) when none is
# derivable.
_hi_issue_for_wt() {
  local wt="$1" issue="${AFK_TLOG_ISSUE:-}" br slug
  if [ -z "$issue" ] && declare -F ai_toolkit_identity_issue_at >/dev/null 2>&1; then
    issue="$(ai_toolkit_identity_issue_at "$wt" 2>/dev/null)" || issue=""
  fi
  if [ -z "$issue" ]; then
    br="$(git -C "$wt" branch --show-current 2>/dev/null)" || true
    slug="${br##*/}"; issue="${slug%%[!0-9]*}"
  fi
  case "$issue" in '' | *[!0-9]*) return 1 ;; esac
  printf '%s\n' "$issue"
}

# _hi_tlog_delivery <wt> <event> <default_lane> [evidence-json] -> record one delivery event.
# Best-effort: no-ops when the log lib is absent or the issue is not derivable; never fails the
# injector. lane defaults to <default_lane> unless the caller set AFK_TLOG_LANE.
_hi_tlog_delivery() {
  local wt="$1" event="$2" default_lane="$3" evidence="${4:-}" issue
  command -v wt_tlog_event >/dev/null 2>&1 || return 0
  issue="$(_hi_issue_for_wt "$wt")" || return 0
  wt_tlog_event "$issue" "$event" hub-inject.sh \
    "${AFK_TLOG_LANE:-$default_lane}" "${AFK_TLOG_EPISODE:-}" "$evidence"
}


# _hi_span <wt> <telemetry_emit_span args...> -> an S7 span under the spoke's spoke_run_id, emitted
# with the worktree as cwd (like afk_emit_decision). Existing span kinds only; best-effort: a missing
# emitter, a gone worktree or an emit failure never fails the caller.
_hi_span() {
  local wt="$1"; shift
  command -v telemetry_emit_span >/dev/null 2>&1 && [ -d "$wt" ] || return 0
  ( cd "$wt" && telemetry_emit_span "$@" ) >/dev/null 2>&1 || true
}

# _hi_wait_ms <since> -> ms elapsed since an Orca timestamp: epoch milliseconds (an agent's
# stateStartedAt) or ISO-8601 UTC (a message's created_at). Empty when unusable.
_hi_wait_ms() {
  local t="${1:-}" d
  case "$t" in
    '' | *[!0-9]*) t="$(LC_ALL=C date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$t" +%s 2>/dev/null)" || return 0
                   [ -n "$t" ] && t=$(( t * 1000 )) || return 0 ;;
  esac
  d=$(( ${AFK_NOW:-$(date +%s)} * 1000 - t ))
  [ "$d" -ge 0 ] && printf '%s\n' "$d"
  return 0
}

# _hi_delivery_verdict <rc> -> the delivery-verdict event name (#281) for a deliver_* rc.
_hi_delivery_verdict() { [ "$1" -eq 0 ] && printf 'answer_delivered\n' || printf 'answer_not_registered\n'; }

# === transcript locators (the reaper's idle clock) =====================================

_spoke_project_dir() {
  local wt_path="$1" projects_root slug
  projects_root="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
  slug="$(printf '%s' "$wt_path" | sed 's/[^A-Za-z0-9]/-/g')"
  printf '%s\n' "$projects_root/$slug"
}
_spoke_jsonl() {
  local dir; dir="$(_spoke_project_dir "$1")"
  [ -d "$dir" ] || return 0
  ls -t "$dir"/*.jsonl 2>/dev/null | head -1
}
# _transcript_mtime <wt_path> -> epoch mtime of the spoke's newest transcript, or empty.
# Probes GNU `-c %Y` FIRST, BSD `-f %m` second (#289, the ordering fix #132 made in
# worktree-lib.sh). The reverse breaks on GNU coreutils: there `-f` selects
# filesystem-status mode and takes no inline format, so `%m` is read as a file operand --
# GNU errors on it yet still PRINTS a multi-line fs block for the real file and exits
# nonzero, so the `||` fallback ALSO runs and the capture holds both the garbage and the
# epoch. BSD rejects `-c` cleanly (usage error, empty stdout), so GNU-first is safe on both.
_transcript_mtime() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  stat -c %Y "$jsonl" 2>/dev/null || stat -f %m "$jsonl" 2>/dev/null
}
# _transcript_sizes <wt_path> -> one "size<TAB>path" line per jsonl in the spoke's project dir
# (empty when none): the pre-reason snapshot the broker's activity scans read past.
_transcript_sizes() {
  local dir f
  dir="$(_spoke_project_dir "$1")"
  [ -d "$dir" ] || return 0
  for f in "$dir"/*.jsonl; do
    [ -e "$f" ] || continue
    # GNU `-c %s` first, BSD `-f %z` second -- same ordering contract as _transcript_mtime (#289).
    printf '%s\t%s\n' "$(stat -c %s "$f" 2>/dev/null || stat -f %z "$f" 2>/dev/null)" "$f"
  done
}

# === delivery ===========================================================================

# _hi_terminal <wt> -> the handle of the spoke's agent terminal (rc 1 and a log line when Orca
# does not know one: a missing record is never a reason to type anywhere else).
_hi_terminal() {
  local h
  h="$(orca_worker_field "$1" '.agentTerminalHandle' 2>/dev/null)" || h=""
  [ -n "$h" ] && { printf '%s\n' "$h"; return 0; }
  log "  no Orca terminal is recorded for $1 -- not delivering"
  return 1
}

# deliver_reply <wt> <msg_id> <text> [asked-at-ms] -> answer the worker's recorded `ask` message. rc 0
# on the ack, which also closes a human `gate` span (ask -> reply) when the ask time is known.
deliver_reply() {
  local wt="$1" id="$2" text="$3" asked="${4:-}" rc=1 ms
  if orca_reply "$id" "$text" "$(_orca_identity "$wt" run_id)"; then
    rc=0; _hi_tlog_delivery "$wt" answer_injected answer
    ms="$(_hi_wait_ms "$asked")"
    [ -z "$ms" ] || _hi_span "$wt" --kind human --name orca-gate-wait --human-type gate --human-wait-ms "$ms"
  fi
  _hi_tlog_delivery "$wt" "$(_hi_delivery_verdict "$rc")" answer "{\"rc\":$rc}"
  return "$rc"
}

# deliver_text <wt> <text> [proof=turn_started] [esc=0] -> type <text> + Enter into the agent's
# terminal. esc=1 sends Escape first: it cancels a stray AskUserQuestion menu that ignores typed
# text (#74, D1). rc 0 only when Orca observed <proof> (a permission answer resumes a turn, so it
# asks for another stage); the text is sent ONCE, whatever the outcome.
deliver_text() {
  local wt="$1" text="$2" proof="${3:-turn_started}" esc="${4:-0}" h stages rc=1
  # Never type into a worker Orca reports exited: its terminal may have fallen back to a shell (#301).
  [ "$(orca_worker_liveness "$wt" 2>/dev/null)" != exited ] || { log "  worker of $wt has exited -- not typing into its terminal"; return 1; }
  h="$(_hi_terminal "$wt")" || return 1
  [ "$esc" = 1 ] && { orca_send_text "$h" $'\e' 0 0 >/dev/null || true; sleep "${AFK_INJECT_MENU_PAUSE:-0.3}"; }
  if stages="$(orca_send_text "$h" "$text" "${AFK_SEND_WAIT:-15}" 1)"; then
    _hi_tlog_delivery "$wt" answer_injected answer
    case ",$stages," in *",$proof,"*) rc=0 ;; esac
  fi
  _hi_tlog_delivery "$wt" "$(_hi_delivery_verdict "$rc")" answer "{\"rc\":$rc}"
  return "$rc"
}

# deliver_answer <wt> <issue> <qid> <text> [asked-at-ms] -> the answer lane's one entry: reply to the
# recorded question id when there is one, else (a stray AskUserQuestion dialog) type it, Escape first.
deliver_answer() {
  local wt="$1" issue="$2" qid="$3" text="$4" asked="${5:-}"
  if [ -n "$qid" ]; then AFK_TLOG_ISSUE="$issue" deliver_reply "$wt" "$qid" "$text" "$asked"
  else AFK_TLOG_ISSUE="$issue" deliver_text "$wt" "$text" turn_started 1; fi
}

# approve_permission <wt> -> select "Yes" (option 1, this once, NEVER "don't ask again") on the
# waiting permission dialog: a bare `1` keypress (no Enter, which would land on whatever dialog
# follows). Orca's prompt stages never fire for a menu key, so delivery is proven by Orca's own
# agent state: the agent LEFT `waiting` (or its stateStartedAt moved, a new state) within
# AFK_APPROVE_SETTLE_SECONDS (default 10). Silence is rc 1, retried next tick and NEVER resent here.
# Records approval_injected with the delivered verdict (the permission broker threads
# AFK_TLOG_LANE / AFK_TLOG_EPISODE).
approve_permission() {
  local wt="$1" rc=1 h since now st waited=0 budget="${AFK_APPROVE_SETTLE_SECONDS:-10}" ms
  since="$(orca_agent_field "$wt" stateStartedAt 2>/dev/null)"
  if [ "$(orca_worker_liveness "$wt" 2>/dev/null)" != exited ] && h="$(_hi_terminal "$wt")" \
     && orca_send_text "$h" 1 0 0 >/dev/null; then
    while :; do
      orca_tick_reset
      st="$(orca_agent_state "$wt" 2>/dev/null)"; now="$(orca_agent_field "$wt" stateStartedAt 2>/dev/null)"
      if { [ "$st" != waiting ] && [ "$st" != unknown ]; } || { [ -n "$since" ] && [ -n "$now" ] && [ "$now" != "$since" ]; }; then rc=0; break; fi
      [ "$waited" -ge "$budget" ] && break
      sleep 1; waited=$(( waited + 1 ))
    done
  fi
  if [ "$rc" -eq 0 ]; then   # a human `permission` span: waiting -> the consumed approval
    ms="$(_hi_wait_ms "$since")"
    [ -z "$ms" ] || _hi_span "$wt" --kind human --name orca-permission-wait --human-type permission --human-wait-ms "$ms"
  fi
  _hi_tlog_delivery "$wt" approval_injected permission \
    "{\"delivered\":$([ "$rc" -eq 0 ] && printf true || printf false)}"
  return "$rc"
}

# _deny_permission <wt> <guidance> -> decline the dialog and tell the spoke the reversible path:
# Escape cancels the dialog, then the guidance goes in as a new message. Best-effort (rc from
# deliver_text): a failed delivery still lets the caller warn + retry on the backoff.
_deny_permission() { deliver_text "$1" "$2" turn_started 1; }
