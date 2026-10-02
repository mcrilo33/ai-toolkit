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

# orca_worktree_by_name <name> -> rc 0 and ORCA_OUT shaped like a `worktree create` reply when a
# worktree with that display name is listed. Read-only: the settle probe for `worktree create`.
orca_worktree_by_name() {
  orca_json worktree list || return 1
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
