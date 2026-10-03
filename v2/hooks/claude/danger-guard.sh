#!/usr/bin/env bash
# shellcheck disable=SC2086  # `check $seg` splits a segment into words on purpose (globbing is off: set -f)
# danger-guard: PreToolUse(Bash|Write|Edit|MultiEdit|NotebookEdit|AskUserQuestion). Yolo mode (D8) has no prompts, so this
# deny-list is the brake: rm -r outside the worktree, git reset --hard in the main checkout, writes to .github/workflows/,
# orca.yaml, ~/.claude/settings.json and, in a spoke, the project .claude/{settings*.json,hooks/}; AskUserQuestion in a spoke.
# Exit 2 + stderr = deny; a crash (bad JSON, no jq) is exit 2 too (fail-closed). A pattern list, not a sandbox.
set -euo pipefail
deny() { echo "danger-guard: blocked: $*" >&2; exit 2; }
trap 'deny "cannot parse the tool payload (fail-closed)"' ERR
set -f; shopt -s nocasematch # macOS is case-insensitive: RM, ORCA.YAML and .GitHub/ are the same files
in="$(cat)"
j() { jq -r "$1 // empty" <<<"$in"; }
phys() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
cwd="$(j .cwd)"; cwd="${cwd:-$PWD}"; root="$(phys "$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || echo "$cwd")")"
home="$(phys "$HOME")"; spoke=""; if [ -e "${CLAUDE_PROJECT_DIR:-$root}/.ai-toolkit/spoke-run-id" ]; then spoke=1; fi
if [ "$(j .tool_name)" = AskUserQuestion ]; then
  [ -z "$spoke" ] || deny "AskUserQuestion is off in a spoke: ask the coordinator with your preamble's orchestration ask"; exit 0
fi
canon() { local p="$1"; case "$p" in \~ | \~/*) p="$HOME${p#\~}";; esac; case "$p" in /*) ;; *) p="$cwd/$p";; esac
  if [ -d "$p" ]; then phys "$p"; else printf '%s/%s' "$(phys "$(dirname "$p")")" "${p##*/}"; fi; }
prot() { case "$1" in */.github/workflows/* | */orca.yaml | "$home/.claude/settings.json") return 0;; esac
  [ -n "$spoke" ] && case "$1" in */.claude/settings.json | */.claude/settings.local.json | */.claude/hooks/*) return 0;; esac
  return 1; }
fp="$(j '.tool_input.file_path // .tool_input.notebook_path')"
if [ -n "$fp" ] && prot "$(canon "$fp")"; then deny "write to a protected path ($fp)"; fi
cmd="$(j .tool_input.command)"; [ -n "$cmd" ] || exit 0

c=" $(printf '%s' "$cmd" | sed -E "s/[\"'\\\\]//g; s/[0-9&]*>+ *(\/dev\/null|&[0-9])//g")" # writes to a protected path
p="\.github/workflows|orca\.yaml([^[:alnum:]_.-]|$)|(~|HOME\}?|$home)/\.claude/settings\.json"
if [ -n "$spoke" ]; then p="$p|\.claude/(settings(\.local)?\.json|hooks/)"; fi
w="(>|[[:space:]](tee|cp|mv|rm|touch|ln|dd|install|rsync|truncate|patch|chmod|python[0-9.]*|perl|ruby|node)[[:space:]]|[[:space:]]sed[[:space:]][^;|&]*(-[a-z]*i|--in-place))"
re="(${w}[^;|&]*|[[:space:]](cd|pushd)[[:space:]][^;|&]*)($p)" # a verb before the path, or a cd into it
if [[ $c =~ $re ]]; then deny "write to a protected path (.github/workflows, orca.yaml, claude settings/hooks)"; fi

target() { # one `rm -r` target: inside the worktree, or strictly below a temp root
  local p="$1" v; for v in HOME TMPDIR; do p="${p/#\$$v/${!v:-}}"; p="${p/#\$\{$v\}/${!v:-}}"; done
  case "$p" in *\$* | \~[!/]*) deny "rm -r target $1 has an unexpanded variable: use a literal path";; esac
  p="$(canon "$p")"
  case "$p" in "$root"/?*) return 0;; "$root" | "$home" | "$home"/*) deny "rm -r of $1 (worktree root or home)";; esac
  case "$p" in /tmp/?* | /private/tmp/?* | "$(phys "${TMPDIR:-/nonexistent}")"/?*) return 0;; esac
  deny "rm -r outside the worktree: $1"
}
check() { # the words of one command segment
  while [ $# -gt 0 ] && [[ ${1##*/} != rm && ${1##*/} != git ]]; do shift; done
  [ $# -gt 0 ] || return 0
  local k="${1##*/}" d t="" rec="" a; shift
  if [[ $k == git ]]; then
    case " $* " in *" reset "*"--hard "*) ;; *) return 0;; esac
    case " $* " in *" --git-dir"* | *" --work-tree"*) deny "git reset --hard with --git-dir/--work-tree";; esac
    d="$(printf '%s' " $* " | sed -n 's/.* -C *\([^ ]*\).*/\1/p')"; d="${d:-.}"; case "$d" in /*) ;; *) d="$cwd/$d";; esac
    a="$(git -C "$d" rev-parse --path-format=absolute --git-dir 2>/dev/null || echo x)"
    [ "$a" != "$(git -C "$d" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo y)" ] || deny "git reset --hard in the main checkout"
    return 0
  fi
  for a in "$@"; do case "$a" in --recursive | -[rR]* | -[!-]*[rR]*) rec=1;; -*) ;; *) t="$t $a";; esac; done
  [ -z "$rec" ] || for a in $t; do target "$a"; done
  return 0
}
for q in '' ' '; do
  while IFS= read -r seg; do check $seg; done < <(printf '%s\n' "$cmd" | sed "s/[\"'\\\\]/$q/g" | tr ';|&()`<>' '\n')
done
