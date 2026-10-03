#!/usr/bin/env bash
# shellcheck disable=SC2086  # `check $seg` splits a segment into words on purpose (globbing is off: set -f)
# push-guard: PreToolUse(Bash). Denies pushes to the base branch, force pushes, --no-verify, core.hooksPath
# overrides and `git checkout <base>` from a linked worktree. Exit 2 + stderr = deny; any crash (bad JSON, no jq)
# is exit 2 too, since every other code is non-blocking. A pattern deny-list, not a sandbox (D8).
set -euo pipefail
deny() { echo "push-guard: blocked: $*" >&2; exit 2; }
trap 'deny "cannot parse the tool payload (fail-closed)"' ERR
set -f; shopt -s nocasematch # macOS is case-insensitive: GIT, ORCA.YAML and Main are the same files
in="$(cat)"
cmd="$(jq -r '.tool_input.command // empty' <<<"$in")"
cwd="$(jq -r '.cwd // empty' <<<"$in")"; cwd="${cwd:-$PWD}"
[ -n "$cmd" ] || exit 0
base="${BASE_BRANCH:-$(git -C "$cwd" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || true)}"; base="${base#origin/}"; base="${base:-main}"
g() { git -C "$1" rev-parse --path-format=absolute "$2" 2>/dev/null || true; }

check() { # the words of one command segment
  while [ $# -gt 0 ] && [[ ${1##*/} != git ]]; do shift; done
  [ $# -gt 0 ] || return 0
  shift; local d="$cwd" sub="" a r dst cur refs="" n=0
  while [ $# -gt 0 ]; do
    case "$1" in
      -c | -C) [[ ${2:-} =~ hookspath ]] && deny "core.hooksPath override"; [ "$1" != -C ] || d="${2:-.}"; shift;; # case-sensitive [
      --git-dir | --work-tree | --namespace | --exec-path) shift;;
      -*) ;;
      *) sub="$1"; shift; break;;
    esac
    shift
  done
  case "$d" in /*) ;; *) d="$cwd/$d";; esac
  for a in "$@"; do case "$a" in --no-verify) deny "--no-verify";; esac; done
  case "$sub" in
    config) [[ $* =~ hookspath ]] && deny "core.hooksPath override";;
    commit) for a in "$@"; do [[ $a =~ ^-[^-mFCct][a-zA-Z]*$ && $a == *n* ]] && deny "commit -n skips the hooks"; done;;
    push)
      for a in "$@"; do
        case "$a" in
          --force* | --all | --mirror | +* | *\** ) deny "force/all/mirror push ($a)";;
          -*) [[ $a =~ ^-[a-zA-Z]*f[a-zA-Z]*$ ]] && deny "force push ($a)";;
          *) n=$((n + 1)); refs="$refs $a";; # every positional: --repo=origin shifts the remote
        esac
      done
      [ $n -gt 1 ] || refs="$refs HEAD" # a bare push sends the current branch
      cur="$(git -C "$d" symbolic-ref -q --short HEAD 2>/dev/null || true)"
      for r in $refs; do
        dst="${r#*:}"; dst="${dst#refs/heads/}"
        case "$dst" in HEAD | @) dst="$cur";; esac
        [[ $dst != "$base" ]] || deny "push to the base branch ($base)"
      done;;
    checkout | switch)
      [ "$(g "$d" --git-dir)" != "$(g "$d" --git-common-dir)" ] || return 0 # main checkout
      for a in "$@"; do [ "$a" != -- ] || return 0; done # path restore
      for a in "$@"; do [[ $a != "$base" ]] || deny "checkout of $base from a linked worktree"; done;;
  esac
  return 0
}

for q in '' ' '; do # quotes deleted (pu""sh) and quotes as spaces (bash -c'git ..')
  while IFS= read -r seg; do check $seg; done < <(printf '%s\n' "$cmd" | sed "s/[\"'\\\\]/$q/g" | tr ';|&()`<>' '\n')
done
