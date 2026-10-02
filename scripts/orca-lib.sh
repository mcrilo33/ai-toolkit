#!/usr/bin/env bash
#
# orca-lib.sh -- the only file that shells out to the `orca` CLI (#363). Mechanism, no policy:
# callers decide what a failure means. Sourced (never run) by worktree-lib.sh, so every
# consumer gets it; needs jq. Each call leaves its result in ORCA_OUT / ORCA_ERR / ORCA_RC.

ORCA_MIN_VERSION="${ORCA_MIN_VERSION:-1.4.218}"

_orca_err() { printf 'orca: %s\n' "$*" >&2; }

# orca_require_version -> rc 0 when `orca` is on PATH and >= ORCA_MIN_VERSION, else 1 + why.
orca_require_version() {
  local have
  command -v orca >/dev/null 2>&1 \
    || { _orca_err "the orca CLI is not on PATH; Orca >= $ORCA_MIN_VERSION is a prerequisite"; return 1; }
  have="$(orca --version 2>/dev/null | grep -Eo '[0-9]+(\.[0-9]+)+' | head -n1)"
  [ -n "$have" ] && [ "$(printf '%s\n%s\n' "$ORCA_MIN_VERSION" "$have" | sort -V | head -n1)" = "$ORCA_MIN_VERSION" ] \
    || { _orca_err "Orca ${have:-of unknown version} is older than the required $ORCA_MIN_VERSION; update Orca"; return 1; }
}

# orca_json <args...> -> run `orca <args> --json`. ORCA_OUT=stdout, ORCA_ERR=stderr, ORCA_RC=rc;
# an `ok:false` reply counts as a failure even when the CLI exited 0.
orca_json() {
  local errf; errf="$(mktemp)"; ORCA_RC=0
  ORCA_OUT="$(orca "$@" --json 2>"$errf")" || ORCA_RC=$?
  ORCA_ERR="$(cat "$errf")"; rm -f "$errf"
  if [ "$ORCA_RC" -eq 0 ] && printf '%s' "$ORCA_OUT" | jq -e '.ok == false' >/dev/null 2>&1; then ORCA_RC=1; fi
  return "$ORCA_RC"
}

# orca_field <jq-filter> -> a field of ORCA_OUT (empty when absent or null).
orca_field() { printf '%s' "$ORCA_OUT" | jq -r "$1 // empty" 2>/dev/null || true; }
orca_wt_path() { orca_field '.result.worktree.path'; }
orca_wt_id() { orca_field '.result.worktree.id'; }
orca_dispatch_id() { orca_field '.result.dispatchId'; }
orca_terminal_handle() { orca_field '.result.terminal.handle'; }
orca_blocked_reason() { orca_field '[.. | objects | select(has("blockedReason")) | .blockedReason] | .[0]'; }

# orca_run_id -> the Run bound to this terminal; rc 1 (and no output) when there is none.
orca_run_id() {
  local id
  orca_json orchestration run-current || return 1
  id="$(orca_field '.result.run.id')"
  [ -n "$id" ] && printf '%s\n' "$id"
}

# orca_worktree_by_name <name> [repo-selector] -> rc 0 and ORCA_OUT shaped like a `worktree create`
# reply when a worktree with that display name is listed (the listing spans every repo, so pass the
# selector). Read-only: the settle probe for `worktree create`.
orca_worktree_by_name() {
  orca_json worktree list ${2:+--repo "$2"} || return 1
  ORCA_OUT="$(printf '%s' "$ORCA_OUT" | jq -c --arg n "$1" \
    '[.result.worktrees[]? | select(.displayName == $n)] | .[0] // empty | {ok: true, result: {worktree: .}}')"
  [ -n "$ORCA_OUT" ]
}

# _orca_unsettled -> rc 0 when the last call died in a way that does not say whether the
# mutation took effect (the CLI drops a long call at ~30 s while the runtime carries on).
_orca_unsettled() {
  case "$(orca_field '.error.code // .result.state') $ORCA_ERR" in
    *runtime_unavailable* | *outcome_unknown* | *"closed the connection"*) return 0 ;;
  esac
  return 1
}

# orca_call_settled <probe-fn|""> <orca args...> -> run ONE mutation. A definite failure returns
# at once. An unsettled one is re-checked read-only for ~ORCA_SETTLE_TRIES x ORCA_SETTLE_SLEEP
# (15 x 2 s): `orchestration request-show` when the error carried a request id, else <probe-fn>.
# On settle ORCA_OUT holds the success reply. The mutation is NEVER re-issued; rc 1 means unknown.
orca_call_settled() {
  local probe="$1" i id first_out first_err; shift
  orca_json "$@" && return 0
  _orca_unsettled || return "$ORCA_RC"
  first_out="$ORCA_OUT"; first_err="$ORCA_ERR"
  id="$(orca_field '.error.data.orchestrationRequestId')"
  [ -n "$id" ] || [ -n "$probe" ] || return 1   # nothing read-only to check: unknown
  for ((i = 0; i < ${ORCA_SETTLE_TRIES:-15}; i++)); do
    sleep "${ORCA_SETTLE_SLEEP:-2}"
    if [ -n "$id" ]; then
      if orca_json orchestration request-show --request "$id" \
         && [ "$(orca_field '.result.state')" = completed ]; then
        ORCA_OUT="$(printf '%s' "$ORCA_OUT" | jq -c '{ok: true, result: .result.receipt}')"; ORCA_RC=0
        return 0
      fi
    elif [ -n "$probe" ] && "$probe"; then
      ORCA_RC=0; return 0
    fi
  done
  ORCA_OUT="$first_out"; ORCA_ERR="$first_err"; ORCA_RC=1
  return 1
}

# orca_wait_agent <terminal-handle> -> rc 0 once Orca reports claude running in the terminal
# (~ORCA_AGENT_TRIES x ORCA_AGENT_SLEEP, 60 x 0.5 s). Gate worker-start on it: before that the
# seed prompt would be typed into a bare shell.
orca_wait_agent() {
  local i
  for ((i = 0; i < ${ORCA_AGENT_TRIES:-60}; i++)); do
    orca_json terminal show --terminal "$1" && [ "$(orca_field '.result.terminal.agentIdentity')" = claude ] && return 0
    sleep "${ORCA_AGENT_SLEEP:-0.5}"
  done
  return 1
}

# --- the drain's reads and writes (#365) -------------------------------------------------------
# Reads answer from Orca, never from a terminal pane or a transcript. A record that is missing (the call
# failed, the worktree or worker is not listed) reads UNKNOWN (rc 2), never "dead" or "nothing":
# unknown alone is never a basis for recovery or a `blocked` (AFK principle #6).

# orca_tick_reset -> start a fresh per-tick read cache. The cache is a dir (not variables) so it
# survives the $(...) subshells the callers read through; with no reset, reads are uncached.
orca_tick_reset() {
  _ORCA_TICK="${TMPDIR:-/tmp}/orca-tick-$$"
  rm -rf "$_ORCA_TICK"   # always: a leftover dir from a reused pid must never serve stale replies
  mkdir -p "$_ORCA_TICK" 2>/dev/null || _ORCA_TICK=""
}

# _orca_cached <key> <orca args...> -> the reply on stdout and the call's rc, one real call per tick.
_orca_cached() {
  local key="$1" f="" rc; shift
  [ -n "${_ORCA_TICK:-}" ] && f="$_ORCA_TICK/$key"
  if [ -n "$f" ] && [ -f "$f.rc" ]; then cat "$f.out"; return "$(cat "$f.rc")"; fi
  orca_json "$@"; rc=$?
  if [ -n "$f" ]; then printf '%s' "$ORCA_OUT" >"$f.out"; printf '%s' "$rc" >"$f.rc"; fi
  printf '%s' "$ORCA_OUT"; return "$rc"
}

_orca_rp() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
_orca_identity() { { sed -n "s/^$2=//p" "$1/.ai-toolkit/identity" 2>/dev/null | head -n1; } || true; }

# orca_agent_state <wt> -> working|waiting|done|none (rc 0), or unknown (rc 2) when ps failed or
# does not list the worktree. Several agents: waiting beats working beats done.
orca_agent_state() {
  local out s
  out="$(_orca_cached ps worktree ps)" || { echo unknown; return 2; }
  s="$(printf '%s' "$out" | jq -r --arg a "$1" --arg b "$(_orca_rp "$1")" '
    [.result.worktrees[]? | select(.path == $a or .path == $b)][0] as $w
    | if $w == null then "unknown" else ([$w.agents[]?.state] | if index("waiting") then "waiting"
      elif index("working") then "working" elif length > 0 then "done" else "none" end) end' 2>/dev/null)"
  [ -n "$s" ] || s=unknown
  echo "$s"; [ "$s" != unknown ] || return 2
}

# orca_agent_field <wt> <name> -> a field (toolName, toolInput, stateStartedAt, ...) of the
# worktree's most-attention agent; toolInput is flattened to a string. Empty when absent.
orca_agent_field() {
  local out
  out="$(_orca_cached ps worktree ps)" || return 2
  printf '%s' "$out" | jq -r --arg a "$1" --arg b "$(_orca_rp "$1")" --arg f "$2" '
    [.result.worktrees[]? | select(.path == $a or .path == $b)][0].agents // []
    | (map(select(.state == "waiting"))[0] // map(select(.state == "working"))[0] // .[0] // {})[$f]
    | if . == null then empty elif type == "string" then . else tojson end' 2>/dev/null
}

# _orca_worker_row <wt> -> the worker-list row for the worktree as one JSON line: the identity's
# dispatch id when recorded, else the newest row on that worktree (worker-list is newest first).
# rc 1 no row, rc 2 unknown.
_orca_worker_row() {
  local run did out row
  run="$(_orca_identity "$1" run_id)"; did="$(_orca_identity "$1" orca_dispatch_id)"
  out="$(_orca_cached "wl-${run:-bound}" orchestration worker-list ${run:+--run "$run"})" || return 2
  row="$(printf '%s' "$out" | jq -c --arg d "$did" --arg p "::$(_orca_rp "$1")" '
    [.result.workers[]?] | (map(select($d != "" and .dispatchId == $d))[0]
      // map(select((.resource.worktreeId // "") | endswith($p)))[0]) // empty' 2>/dev/null)" || return 2
  [ -n "$row" ] && printf '%s\n' "$row"
}

# orca_worker_field <wt> <jq-path> -> a field of the worktree's worker row; rc 1 no row, rc 2 unknown.
orca_worker_field() {
  local row; row="$(_orca_worker_row "$1")" || return $?
  printf '%s' "$row" | jq -r "$2 // empty" 2>/dev/null
}

# orca_worker_live_count <wt> [exclude-dispatch] -> how many `live` workers Orca lists on the worktree
# besides <exclude-dispatch> (the one just fenced); rc 2 when the list is unreadable. The guard that
# keeps a restart from ever making a second live dispatch on one worktree.
orca_worker_live_count() {
  local run out
  run="$(_orca_identity "$1" run_id)"
  out="$(_orca_cached "wl-${run:-bound}" orchestration worker-list ${run:+--run "$run"})" || return 2
  printf '%s' "$out" | jq -r --arg p "::$(_orca_rp "$1")" --arg x "${2:-}" '[.result.workers[]?
    | select(((.resource.worktreeId // "") | endswith($p)) and .dispatchId != $x
        and .projection.liveness.verdict == "live")] | length' 2>/dev/null
}

# orca_worker_liveness <wt> -> live|unverifiable|exited (rc 0); unknown with rc 1 (no row) or 2.
orca_worker_liveness() {
  local v rc=0
  v="$(orca_worker_field "$1" '.projection.liveness.verdict')" || rc=$?
  [ "$rc" -eq 0 ] && [ -n "$v" ] && { echo "$v"; return 0; }
  echo unknown; return $(( rc == 0 ? 2 : rc ))
}

# orca_inbox_question <wt> <id|created|body> -> a field of the oldest unread `question` the
# worktree's worker sent; rc 1 when none, rc 2 unknown. A gate park shows only here: the agent
# reads `working` while it blocks in `ask` (spike #17).
orca_inbox_question() {
  local h run out rc=0
  h="$(orca_worker_field "$1" '.agentTerminalHandle')" || rc=$?
  [ "$rc" -eq 0 ] && [ -n "$h" ] || return 2   # no worker record: whose inbox? unknown, never "none"
  run="$(_orca_identity "$1" run_id)"
  out="$(_orca_cached "in-${run:-bound}" orchestration check --peek --types question ${run:+--run "$run"})" || return 2
  out="$(printf '%s' "$out" | jq -r --arg h "$h" --arg f "$2" '
    [.result.messages[]? | select(.type == "question" and .from_handle == $h)]
    | sort_by(.created_at) | .[0] | {id: .id, created: .created_at, body: .body}[$f] // empty' 2>/dev/null)"
  [ -n "$out" ] && printf '%s\n' "$out"
}

# _orca_mutate <orca args...> -> ONE mutation through orca_call_settled with a fresh
# --retry-request, so a replay can never double-apply (03 #12).
_orca_mutate() {
  orca_call_settled "" "$@" --retry-request "$(uuidgen 2>/dev/null || printf '%s-%s' "$(date +%s)" "$RANDOM")"
}

# orca_reply <msg_id> <body> [run] -> rc 0 when Orca acked the reply (the ack is the proof).
orca_reply() { _orca_mutate orchestration reply --id "$1" --body "$2" ${3:+--run "$3"}; }

# orca_send_text <terminal> <text> [wait-seconds=15] [enter=1] -> type into the agent's terminal;
# prints the stages Orca observed (input_accepted, turn_started). rc 0 when the call was accepted
# (NOT proof of submission: the caller checks the stage it needs), 1 otherwise. Never resends.
# --retry-request and --wait-submit are only valid on a prompt (text + --enter), so a bare key
# (Escape) goes without them.
orca_send_text() {
  local wait="${3:-15}" enter="${4:-1}" args=(terminal send --terminal "$1" --text "$2")
  if [ "$enter" = 1 ]; then
    args+=(--enter)
    [ "$wait" -gt 0 ] && args+=(--wait-submit "$wait")
    _orca_mutate "${args[@]}" || return 1
  else
    orca_call_settled "" "${args[@]}" || return 1
  fi
  printf '%s' "$ORCA_OUT" | jq -r '[.. | objects | .stages? | arrays | .[]] | unique | join(",")' 2>/dev/null
  return 0
}

# orca_worker_stop|abandon|release <dispatch> -> rc 0 when Orca settled it (`release` is only
# refused as release_unknown).
orca_worker_stop() { _orca_mutate orchestration worker-stop --dispatch "$1"; }
orca_worker_abandon() { _orca_mutate orchestration worker-abandon --dispatch "$1"; }
orca_worker_release() { _orca_mutate orchestration worker-release --dispatch "$1"; }

# orca_capability <wt> -> the dispatch capability token the spoke was handed in its preamble:
# $ORCA_DISPATCH_CAPABILITY, else <wt>/.ai-toolkit/dispatch-capability (written by spoke-ready.sh the
# first time the agent passes it). Orca offers no other way to read it; empty when unknown.
orca_capability() {
  if [ -n "${ORCA_DISPATCH_CAPABILITY:-}" ]; then printf '%s\n' "$ORCA_DISPATCH_CAPABILITY"; return 0; fi
  printf '%s\n' "$(head -n1 "$1/.ai-toolkit/dispatch-capability" 2>/dev/null | tr -d '[:space:]')"
}

# orca_worker_done <wt> <succeeded|failed> <subject> -> tell the coordinator this worker finished
# (a `worker_done` message; there is no verb). Best-effort: the caller ignores the rc.
orca_worker_done() {
  local did task cap
  did="$(_orca_identity "$1" orca_dispatch_id)"
  [ -n "$did" ] || return 1
  task="$(orca_worker_field "$1" '.taskId' 2>/dev/null)"; cap="$(orca_capability "$1")"
  _orca_mutate orchestration send --type worker_done --outcome "$2" --subject "$3" \
    ${ORCA_TERMINAL_HANDLE:+--from "$ORCA_TERMINAL_HANDLE"} --dispatch-id "$did" ${task:+--task-id "$task"} \
    ${cap:+--dispatch-capability "$cap"}
}

# orca_ask <wt> <question> <options-csv> <timeout-ms> | orca_ask_resume <wt> <msg_id> <timeout-ms>
# -> block in `orchestration ask` for the coordinator's reply. Prints the answer (rc 0). rc 3: the
# timeout elapsed and the question is STILL PENDING (its id is in ORCA_ASK_ID: resume it, never ask
# again). rc 1: failed or cancelled. Not settled through orca_call_settled: the call blocks by design.
_orca_ask_run() {
  local wt="$1"; shift
  local cap; cap="$(orca_capability "$wt")"
  orca_json orchestration ask "$@" ${cap:+--dispatch-capability "$cap"} || true
  ORCA_ASK_ID="$(orca_field '.result.messageId')"
  [ "$(orca_field '.result.timedOut')" = true ] && return 3
  [ -n "$(orca_field '.result')" ] && [ "$(orca_field '.result.cancelled')" != true ] || return 1
  orca_field '.result.answer'
}
orca_ask() { _orca_ask_run "$1" --question "$2" --options "$3" --timeout-ms "$4"; }
orca_ask_resume() { _orca_ask_run "$1" --resume "$2" --timeout-ms "$3"; }
