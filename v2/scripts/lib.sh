#!/usr/bin/env bash
# ai-toolkit v2 shared helpers: source me. No `set` here, the caller owns the shell options.
AI_TOOLKIT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf '%s: %s\n' "${0##*/}" "$*" >&2; exit 1; }
warn() { printf '%s: warning: %s\n' "${0##*/}" "$*" >&2; }

# KEY=VALUE lines; a variable that is already set is never overwritten.
_load_env_file() {
  local line k v
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | \#*) continue ;; esac
    k="${line%%=*}"; v="${line#*=}"
    case "$k" in '' | *[!A-Z0-9_]*) continue ;; esac
    v="${v#\"}"; v="${v%\"}"
    if ! eval "[ -n \"\${$k+set}\" ]"; then export "$k=$v"; fi
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

# gh_issue <n>: {number,title,body} of one issue.
gh_issue() { gh issue view "$1" --json number,title,body; }

# issue_footer <body> <Key>: value of the last `Key: value` line (Scope:, Gate:, Model:).
issue_footer() { printf '%s\n' "$1" | sed -n "s/^$2:[[:space:]]*//p" | tail -n 1; }
