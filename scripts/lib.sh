#!/usr/bin/env bash
# ai-toolkit v2 shared helpers: source me. No `set` here, the caller owns the shell options.
AI_TOOLKIT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf '%s: %s\n' "${0##*/}" "$*" >&2; exit 1; }
warn() { printf '%s: warning: %s\n' "${0##*/}" "$*" >&2; }

# KEY=VALUE lines; a variable that is already set is never overwritten. Assigned, NOT exported:
# the local file may hold secrets, and a caller that needs a key in a child env exports it itself.
_load_env_file() {
  local line k v
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | \#*) continue ;; esac
    k="${line%%=*}"; v="${line#*=}"
    case "$k" in '' | *[!A-Z0-9_]*) continue ;; esac
    v="${v#\"}"; v="${v%\"}"
    if ! eval "[ -n \"\${$k+set}\" ]"; then printf -v "$k" %s "$v"; fi
  done < "$1"
}

# Precedence: caller env > local override (main checkout, gitignored) > shipped defaults, which
# sit in settings/ in the ai-toolkit repo and next to scripts/ (.ai-toolkit/) in a synced target.
load_env() {
  local d="$AI_TOOLKIT_LIB_DIR" root
  root="${ORCA_ROOT_PATH:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo .)")}"
  _load_env_file "${AI_TOOLKIT_LOCAL_ENV:-$root/.ai-toolkit/ai-toolkit.local.env}"
  if [ -n "${AI_TOOLKIT_ENV:-}" ]; then _load_env_file "$AI_TOOLKIT_ENV"
  elif [ -f "$d/../settings/ai-toolkit.env" ]; then _load_env_file "$d/../settings/ai-toolkit.env"
  else _load_env_file "$d/../ai-toolkit.env"; fi
}

# orca_json <args...>: a read; prints the --json reply.
orca_json() { orca "$@" --json; }

# orca_mutate <args...>: one mutation. The CLI drops a long call at ~30 s while Orca carries on
# (03 #12), so an unsettled result is replayed with the SAME --retry-request id and can never
# double-apply. A definite failure is returned at once. Only for verbs that take --retry-request.
orca_mutate() {
  local rid out err rc=0
  rid="$(uuidgen 2>/dev/null || printf '%s-%s' "$(date +%s)" "$RANDOM")"
  err="$(mktemp)"
  for _ in 1 2 3; do
    out="$(orca "$@" --retry-request "$rid" --json 2>"$err")" && rc=0 || rc=$?
    [ "$rc" -eq 0 ] && break
    case "$out $(cat "$err")" in
      *runtime_unavailable* | *outcome_unknown* | *"closed the connection"*) sleep "${ORCA_RETRY_SLEEP:-2}" ;;
      *) break ;;
    esac
  done
  cat "$err" >&2; rm -f "$err"
  printf '%s\n' "$out"
  return "$rc"
}

# gh: AI_TOOLKIT_GH (caller env or the local env file) swaps the binary. The e2e stubs GitHub this way, because
# Orca's setup terminal does not inherit the caller's PATH.
gh() { command "${AI_TOOLKIT_GH:-gh}" "$@"; }

# gh_issue <n>: {number,title,body} of one issue.
gh_issue() { gh issue view "$1" --json number,title,body; }

# stale_note <why>: warn on stderr and print the note a gate adds to its prompt when it cannot read the live issue (the intent may be stale).
stale_note() {
  warn "$1; keeping the existing .ai-toolkit/task.md"
  printf ' NOTE: %s, so .ai-toolkit/task.md may be stale: the issue may have changed since dispatch.' "$1"
}

# The one definition of .ai-toolkit/task.md. task_md renders {number,title,body} (stdin) to the file text; task_md_number reads the number
# back from the header. Keep them together: a header change in one breaks the other.
task_md() { jq -r '"# #\(.number) \(.title)\n\n\(.body)"'; }
task_md_number() { sed -n '1s/^# #\([0-9][0-9]*\)[[:space:]].*/\1/p' "$1" 2> /dev/null || true; }

# issue_of <worktree> [tries]: the issue a worktree belongs to, from Orca's link ONLY. Which issue a worktree is for is set by the coordinator and read
# back from Orca; the branch name and task.md are the worker's to rewrite, so a gate never reads them. An Orca that cannot answer (error, non-JSON, no
# worktree object) is retried every AI_TOOLKIT_POLL seconds, ORCA_LINK_TRIES times (default 40, about 2 minutes), never guessed around; an answer of "no
# link" is final. rc 0 prints the number; 1 Orca has no usable link; 2 Orca never answered. Each failure names the worktree and the cause on stderr.
_orca_link_once() { orca_json worktree show --worktree "path:$1" 2> /dev/null | jq -er '.result.worktree | if type == "object" then .linkedIssue // "" else error("no worktree") end' 2> /dev/null; }
issue_of() {
  local n
  n="$(wait_until "${2:-${ORCA_LINK_TRIES:-40}}" "${AI_TOOLKIT_POLL:-3}" _orca_link_once "$1")" || { printf '%s: Orca could not answer for %s: no issue number is guessed\n' "${0##*/}" "$1" >&2; return 2; }
  case "$n" in '' | *[!0-9]*) printf '%s: no issue linked in Orca for %s\n' "${0##*/}" "$1" >&2; return 1 ;; esac
  printf '%s' "$n"
}

# refresh_task <worktree> <issue>: rewrite the worktree's .ai-toolkit/task.md from the live issue, so a gate judges the issue as it is now.
# An issue that cannot be read or written keeps the old copy and prints the stale note for the gate's prompt; never a failure.
refresh_task() {
  local t f="$1/.ai-toolkit/task.md"
  if t="$(gh_issue "$2" | task_md)" && [ -n "$t" ] && mkdir -p "$1/.ai-toolkit" \
    && printf '%s\n' "$t" > "$f.new" && mv "$f.new" "$f"; then :
  else stale_note "issue $2 could not be refreshed"; fi
}

# status_label <add|remove> <issue>: the one `status:in-progress` marker follows the worker. A failure only warns: the work it marks is already done.
status_label() {
  local l="status:in-progress"
  if [ "$1" = add ]; then
    gh issue edit "$2" --add-label "$l" > /dev/null 2>&1 \
      || { gh label create "$l" --color FBCA04 > /dev/null 2>&1 || true; gh issue edit "$2" --add-label "$l" > /dev/null || warn "cannot label #$2 $l"; }
  else gh issue edit "$2" --remove-label "$l" > /dev/null || warn "cannot remove $l from #$2"; fi
}

# issue_footer <body> <Key>: value of the last `Key: value` line (Scope:, Gate:, Model:).
issue_footer() { printf '%s\n' "$1" | sed -n "s/^$2:[[:space:]]*//p" | tail -n 1; }

valid_run() { [[ "$1" =~ ^run_[a-z0-9]+$ ]]; }   # an Orca Run id: it names a directory, so nothing else gets near the spool path
# spool_dir <run-id>: the coordinator's only state, outside every worktree (AITK_STATE_DIR relocates it): replies/ queued human answers,
# holder.<pid> = "<handle> <mode>" of a live coordinator.sh (the --stop wait signal, and --status's mode).
spool_dir() { valid_run "$1" || die "bad run id '$1'"; echo "${AITK_STATE_DIR:-$HOME/.ai-toolkit/coordinator}/$1"; }

# usage_exit <msg>: caller misuse, exit 2 (die is exit 1: a failed operation).
usage_exit() { printf '%s: %s\n' "${0##*/}" "$*" >&2; exit 2; }

# wait_until <tries> <sleep-seconds> <cmd...>: poll until cmd succeeds; false when the tries run out.
wait_until() {
  local t="$1" s="$2" i; shift 2
  for ((i = 0; i < t; i++)); do "$@" && return 0; sleep "$s"; done
  return 1
}

# spawn_claude <worktree> <title> <model> <effort>: the two-step launch (docs/architecture.md): a terminal running bin/claude-spoke
# (OTel env), waited on until claude is up; prints its handle. The first run in a repo root meets Claude's trust dialog
# (default "No, exit"): Down+Enter. dispatch.sh uses it for a new worker and for a retry, so a retried worker keeps the shim's env.
_agent_up() {
  [ "$(orca_json terminal show --terminal "$1" | jq -r '.result.terminal.agentIdentity // empty')" = claude ] && return 0
  if orca_json terminal read --terminal "$1" | jq -e '.result.terminal.tail | join(" ") | test("No, exit")' > /dev/null; then
    orca terminal send --terminal "$1" --text $'\e[B' --json > /dev/null; sleep "${AI_TOOLKIT_POLL:-3}"
    orca terminal send --terminal "$1" --enter --json > /dev/null
  fi
  return 1
}
spawn_claude() {
  local h bin
  bin="$(cd "$AI_TOOLKIT_LIB_DIR/../bin" && pwd -P)/claude-spoke"
  h="$(orca_json terminal create --worktree "path:$1" --title "$2" \
    --command "$(printf %q "$bin") --model $3 --effort $4 --dangerously-skip-permissions" | jq -r '.result.terminal.handle // empty')"
  [ -n "$h" ] || die "terminal create returned no handle"
  wait_until "${DISPATCH_TRIES:-60}" "${AI_TOOLKIT_POLL:-3}" _agent_up "$h" || die "claude did not start in terminal $h"
  printf '%s' "$h"
}
