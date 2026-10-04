#!/usr/bin/env bash
# shellcheck disable=SC2086  # `check $seg` splits a segment into words on purpose (globbing is off: set -f)
# danger-guard: PreToolUse(Bash|Write|Edit|MultiEdit|NotebookEdit|AskUserQuestion). Yolo mode (D8) has no prompts of its own, so this is the brake.
# The rule: everything is authorized except sensitive operations, which ASK the user when someone can answer and are DENIED when no one can.
# Asks (exit 0 + a permissionDecision "ask" on stdout: the prompt shows even in yolo mode), one per call whose reason names every sensitive segment: writes
# to orca.yaml, ~/.claude/settings.json, .github/workflows/ and, in a worker, the project .claude/{settings*.json,hooks/} and .ai-toolkit/spoke-run-id;
# rm -r outside the worktree, of its root or home, of another git checkout, or with an unexpanded variable; git reset --hard in the main checkout (or
# with --git-dir/--work-tree); git clean -x and git stash -a. Someone can answer when there is no worker marker (the human's own prompt) or when a worker
# has the permission-relay hook installed and registered (it puts the question to the Run: the user answers an attended one, the auto loop denies it);
# else the ask is denied like a hard deny, since a local ask nobody sees would hang. Before either, a Bash hit goes to judge.sh (a ~4 s model call, so the regex stays the fast
# first stage): it clears a command that only MENTIONS the operation (a message, a pattern), logging it to ~/.ai-toolkit/judge-cleared.log; any doubt or error is today's ask. The reason is recorded for the relay to join
# (.ai-toolkit/ask-reasons/<sha256 of tool+input>, the one thing a PermissionRequest payload lacks).
# Always denied (exit 2 + stderr), never asked: AskUserQuestion in a worker (not a permission: it has the Run's ask), a write to the human's reply spool
# ~/.ai-toolkit/coordinator/ in a worker (an approved write would forge the human's reply), and an unparsable payload (bad JSON, no jq: fail-closed).
# A pattern list, not a sandbox.
set -Eeuo pipefail
deny() { echo "danger-guard: blocked: $*" >&2; exit 2; }
trap 'deny "cannot parse the tool payload (fail-closed)"' ERR
set -f; shopt -s nocasematch # macOS is case-insensitive: RM, ORCA.YAML and .GitHub/ are the same files
in="$(cat)"
j() { jq -r "$1 // empty" <<<"$in"; }
phys() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
relay_ready() { [ -f "$root/.claude/hooks/permission-relay.sh" ] && grep -q 'hooks/permission-relay.sh' "$root/.claude/settings.json" 2> /dev/null; }
record_reason() { # $1 = why, for the relay: one single-use file per tool+input, first line the epoch
  local d="$root/.ai-toolkit/ask-reasons" k; k="$(jq -cS '[.tool_name,.tool_input]' <<<"$in" | shasum -a 256 | cut -d' ' -f1)"
  (umask 077; mkdir -p "$d"; printf '%s\n%s\n' "$(date +%s)" "$1" > "$d/$k")
}
m="" # the sensitive findings of this call, settled by finish()
sens() { local t="${*//$'\n'/ }"; case "; $m; " in *"; $t; "*) return 0;; esac; m="${m:+$m; }$t"; } # once each: the guard scans a command twice
clip() { printf '%s' "$1" | LC_ALL=C tr -d '\000-\037\177' | cut -c1-200; } # one capped line, no control bytes
audit() { (umask 077; mkdir -p "$home/.ai-toolkit" && [ ! -L "$home/.ai-toolkit/judge-cleared.log" ] && printf '%s\t%s\t%s\t%s\n' "$(date +%s)" Bash "$(clip "$m")" "$(clip "$1")" >> "$home/.ai-toolkit/judge-cleared.log") 2> /dev/null; } # no trace, no clear
finish() {
  [ -n "$m" ] || exit 0
  if [ "$(j .tool_name)" = Bash ] && [ -n "${cmd:-}" ] && why="$(printf '%s' "$cmd" | bash "$(dirname "$0")/judge.sh" "$cwd" "$root" "$m" "$(j .session_id)" 2> /dev/null || :)" && [ -n "$why" ] && audit "$why"; then exit 0; fi # the judge cleared a command that only mentions it
  if [ -z "$spoke" ] || relay_ready; then local r="danger-guard: needs your approval: $m"
    [ -z "$spoke" ] || record_reason "$r"
    jq -nc --arg r "$r" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "ask", permissionDecisionReason: $r}}'; exit 0; fi
  deny "$m"
}
cwd="$(j .cwd)"; cwd="${cwd:-$PWD}" # root = the project dir, not the cwd: a `cd` out of the repo must not move it
root="$(phys "${CLAUDE_PROJECT_DIR:-$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || echo "$cwd")}")"
home="$(phys "$HOME")"; spoke=""; if [ -e "$root/.ai-toolkit/spoke-run-id" ]; then spoke=1; fi
if [ "$(j .tool_name)" = AskUserQuestion ]; then
  [ -z "$spoke" ] || deny "AskUserQuestion is off in a spoke: ask the coordinator with your preamble's orchestration ask"; exit 0
fi
canon() { local p="$1"; case "$p" in \~ | \~/*) p="$HOME${p#\~}";; esac; case "$p" in /*) ;; *) p="$cwd/$p";; esac
  while [ "$p" != / ] && [ "${p%/}" != "$p" ]; do p="${p%/}"; done
  local rest="" d="$p"; while [ ! -d "$d" ]; do rest="/${d##*/}$rest"; d="$(dirname "$d")"; done
  rest="$(printf '%s' "$rest" | sed -E -e ':a' -e 's#/[^/]+/\.\./#/#' -e 'ta')"; printf '%s%s' "$(phys "$d")" "$rest"; }
prot() { case "$1" in "$root/orca.yaml" | "$home/.claude/settings.json") return 0;; esac
  [ -n "$spoke" ] && case "$1" in "$root/.claude/settings.json" | "$root/.claude/settings.local.json" | "$root/.claude/hooks" | "$root/.claude/hooks/"* | "$root/.ai-toolkit/spoke-run-id") return 0;; esac
  return 1; }
fp="$(j '.tool_input.file_path // .tool_input.notebook_path')"
spool="(~|HOME\}?|$home)/\.ai-toolkit/coordinator" # the human's reply spool: a file there is sent as the human's answer, so a worker never writes it
if [ -n "$fp" ]; then fc="$(canon "$fp")"; if prot "$fc"; then sens "write to a protected path ($fp)"; fi
  [ -z "$spoke" ] || case "$fc" in "$home/.ai-toolkit/coordinator" | "$home/.ai-toolkit/coordinator/"*) deny "write to the human's reply spool ($fp)";; esac
  case "$fc" in "$root/.github/workflows" | "$root/.github/workflows/"*) sens "write to .github/workflows ($fp)";; esac; fi
cmd="$(j .tool_input.command)"; [ -n "$cmd" ] || finish

c=" $(printf '%s' "$cmd" | sed -E "s/[\"'\\\\]//g; s/[0-9&]*>+ *(\/dev\/null|&[0-9])//g; s/[[:space:]>]/& /g")" # writes to a protected path
pre="((^|[^[:alnum:]_./-])(\./)?|$root/|\\\$\{?(PWD|CLAUDE_PROJECT_DIR)\}?/|\\\$\(pwd\)/)"; e="([^[:alnum:]_./-]|$)" # project-root paths only: v2/orca.yaml is fine
p="$pre(orca\.yaml([^[:alnum:]_.-]|$))|(~|HOME\}?|$home)/\.claude/settings\.json"
if [ -n "$spoke" ]; then p="$p|$pre(\.claude/?$e|\.claude/(settings(\.local)?\.json|hooks)([^[:alnum:]_.-]|$)|\.ai-toolkit/?$e|\.ai-toolkit/spoke-run-id)"; fi
w="(>|[[:space:]](tee|cp|mv|rm|touch|ln|dd|install|rsync|truncate|patch|chmod|python[0-9.]*|perl|ruby|node)[[:space:]]|[[:space:]]sed[[:space:]][^;|&]*(-[a-z]*i|--in-place))"
va="${w}[^;|&]*|[[:space:]](cd|pushd)[[:space:]][^;|&]*" # a verb before the path, or a cd into it
if [ -n "$spoke" ] && [[ $c =~ ($va)($spool) ]]; then deny "write to the human's reply spool: $(printf '%.200s' "$cmd" | tr '\n' ' ')"; fi
bw=""; if [[ $c =~ ($va)($p) ]]; then bw="a protected path (orca.yaml, claude settings/hooks)"; fi
if [[ $c =~ ($va)($pre\.github/workflows) ]]; then bw="${bw:+$bw and }.github/workflows"; fi
[ -z "$bw" ] || sens "write to $bw: ${cmd:0:200}"

target() { # one `rm -r` target: inside the worktree, or strictly below a temp root
  local p="$1" v; for v in HOME TMPDIR; do p="${p/#\$$v/${!v:-}}"; p="${p/#\$\{$v\}/${!v:-}}"; done
  case "$p" in *\$* | \~[!/]*) sens "rm -r target $1 has an unexpanded variable: use a literal path"; return 0;; esac
  p="$(canon "$p")"
  case "$p" in "$root"/?*) return 0;; "$root" | "$home" | "$home"/*) sens "rm -r of $1 (worktree root or home)"; return 0;; esac
  if [ -e "$p/.git" ]; then sens "rm -r of another git checkout or worktree: $1"; return 0; fi
  case "$p" in /tmp/?* | /private/tmp/?* | "$(phys "${TMPDIR:-/nonexistent}")"/?*) return 0;; esac
  sens "rm -r outside the worktree: $1"
}
check() { # the words of one command segment
  while [ $# -gt 0 ] && [[ ${1##*/} != rm && ${1##*/} != git ]]; do shift; done
  [ $# -gt 0 ] || return 0
  local k="${1##*/}" d t="" rec="" a; shift
  if [[ $k == git ]]; then
    if [[ " $* " == *" clean "* || " $* " == *" stash "* ]]; then for a in "$@"; do [[ $a =~ ^(--all|-[a-zA-Z]*[xa][a-zA-Z]*)$ ]] && { sens "git clean -x / stash -a remove the ignored .claude/ and .ai-toolkit/: git $*"; break; }; done; fi
    case " $* " in *" reset "*"--hard "*) ;; *) return 0;; esac
    case " $* " in *" --git-dir"* | *" --work-tree"*) sens "git reset --hard with --git-dir/--work-tree: git $*"; return 0;; esac
    d="$(printf '%s' " $* " | sed -n 's/.* -C *\([^ ]*\).*/\1/p')"; d="${d:-.}"; case "$d" in /*) ;; *) d="$cwd/$d";; esac
    a="$(git -C "$d" rev-parse --path-format=absolute --git-dir 2>/dev/null || echo x)"
    [ "$a" != "$(git -C "$d" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo y)" ] || sens "git reset --hard in the main checkout ($(phys "$d")): git $*"
    return 0
  fi
  for a in "$@"; do case "$a" in --recursive | -[rR]* | -[!-]*[rR]*) rec=1;; -*) ;; *) t="$t $a";; esac; done
  [ -z "$rec" ] || for a in $t; do target "$a"; done
  return 0
}
# quotes dropped (pu""sh, "$TMPDIR"/x), then quotes glued between a word and a letter turned into a space (bash -c'rm ..')
for sc in "s/[\"'\\\\]//g" "s/([^[:space:]])[\"'\\\\]+([[:alnum:]])/\\1 \\2/g; s/[\"'\\\\]//g"; do
  while IFS= read -r seg; do check $seg; done < <(printf '%s\n' "$cmd" | sed -E "$sc" | tr ';|&()`<>' '\n')
done
finish
