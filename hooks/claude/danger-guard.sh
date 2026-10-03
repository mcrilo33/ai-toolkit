#!/usr/bin/env bash
# shellcheck disable=SC2086  # `check $seg` splits a segment into words on purpose (globbing is off: set -f)
# danger-guard: PreToolUse(Bash|Write|Edit|MultiEdit|NotebookEdit|AskUserQuestion). Yolo mode (D8) has no prompts of its own, so
# this deny-list is the brake: rm -r outside the worktree, git reset --hard in the main checkout, writes to orca.yaml,
# ~/.claude/settings.json and, in a spoke, the project .claude/{settings*.json,hooks/}; AskUserQuestion in a spoke.
# Exit 2 + stderr = deny; a crash (bad JSON, no jq) is exit 2 too (fail-closed). A pattern list, not a sandbox.
# Writes to .github/workflows/ ASK instead (exit 0 + a permissionDecision "ask" on stdout: the prompt shows even in yolo mode)
# when a human attends, and are denied like the rest when nobody does. Attended = outside a spoke (no Run), or in a spoke when the
# terminal holding its Run is a Claude session. Read from Orca: worker-list (this terminal's dispatched row -> Run), run-show
# (-> holder terminal), terminal show (-> agentIdentity claude, title). The loop terminal reports claude too (its headless `claude -p`
# children), so coordinator.sh titles it "coordinator.sh <mode>" and that title is unattended. Anything else (a missing handle, orca
# failing or timing out, an unreadable answer) is unattended: deny. The ask is emitted last, so a deny in a compound still wins.
set -Eeuo pipefail
deny() { echo "danger-guard: blocked: $*" >&2; exit 2; }
trap 'deny "cannot parse the tool payload (fail-closed)"' ERR
set -f; shopt -s nocasematch # macOS is case-insensitive: RM, ORCA.YAML and .GitHub/ are the same files
in="$(cat)"
j() { jq -r "$1 // empty" <<<"$in"; }
phys() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
orc() { # `orca ...` stdout (empty on any failure), killed after 5s: a hung CLI must deny, not time the hook out into an allow
  local o p w; o="$(mktemp)"; orca "$@" > "$o" 2> /dev/null & p=$!; { sleep 5; kill -9 $p; } > /dev/null 2>&1 & w=$!
  if wait $p 2> /dev/null; then cat "$o"; fi; kill $w 2> /dev/null; wait $w 2> /dev/null; rm -f "$o"; return 0
}
attended() { # 0 = a human attends (outside a spoke, or a Claude session holds the spoke's Run); every unreadable answer is empty
  [ -n "$spoke" ] || return 0
  local h="${ORCA_TERMINAL_HANDLE:-}" run holder
  [ -n "$h" ] || return 1
  run="$(orc orchestration worker-list --json --limit 100 | jq -r --arg h "$h" '[.result.workers[] | select(.agentTerminalHandle == $h and .dispatchStatus == "dispatched")][0].runId // empty' 2> /dev/null || :)"
  [ -n "$run" ] || return 1
  holder="$(orc orchestration run-show --id "$run" --json | jq -r '.result.run.coordinator_handle // empty' 2> /dev/null || :)"
  [ -n "$holder" ] || return 1
  [ "$(orc terminal show --terminal "$holder" --json | jq -r '.result.terminal | select(.agentIdentity == "claude" and ((.title // "") | contains("coordinator.sh") | not)) | "session"' 2> /dev/null || :)" = session ]
}
wf=""; wfd="write to a protected path (.github/workflows, orca.yaml, claude settings/hooks)" # a .github/workflows/ write (what, deny text), settled by finish()
finish() {
  [ -n "$wf" ] || exit 0
  if attended; then jq -nc --arg r "danger-guard: write to .github/workflows needs your approval: $wf" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "ask", permissionDecisionReason: $r}}'; exit 0; fi
  deny "$wfd"
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
if [ -n "$fp" ]; then fc="$(canon "$fp")"; if prot "$fc"; then deny "write to a protected path ($fp)"; fi
  case "$fc" in "$root/.github/workflows" | "$root/.github/workflows/"*) wf="$fp"; wfd="write to a protected path ($fp)";; esac; fi
cmd="$(j .tool_input.command)"; [ -n "$cmd" ] || finish

c=" $(printf '%s' "$cmd" | sed -E "s/[\"'\\\\]//g; s/[0-9&]*>+ *(\/dev\/null|&[0-9])//g; s/[[:space:]>]/& /g")" # writes to a protected path
pre="((^|[^[:alnum:]_./-])(\./)?|$root/|\\\$\{?(PWD|CLAUDE_PROJECT_DIR)\}?/|\\\$\(pwd\)/)"; e="([^[:alnum:]_./-]|$)" # project-root paths only: v2/orca.yaml is fine
p="$pre(orca\.yaml([^[:alnum:]_.-]|$))|(~|HOME\}?|$home)/\.claude/settings\.json"
if [ -n "$spoke" ]; then p="$p|$pre(\.claude/?$e|\.claude/(settings(\.local)?\.json|hooks)([^[:alnum:]_.-]|$)|\.ai-toolkit/?$e|\.ai-toolkit/spoke-run-id)"; fi
w="(>|[[:space:]](tee|cp|mv|rm|touch|ln|dd|install|rsync|truncate|patch|chmod|python[0-9.]*|perl|ruby|node)[[:space:]]|[[:space:]]sed[[:space:]][^;|&]*(-[a-z]*i|--in-place))"
va="${w}[^;|&]*|[[:space:]](cd|pushd)[[:space:]][^;|&]*" # a verb before the path, or a cd into it
if [[ $c =~ ($va)($p) ]]; then deny "write to a protected path (orca.yaml, claude settings/hooks)"; fi
if [[ $c =~ ($va)($pre\.github/workflows) ]]; then wf="${cmd:0:200}"; fi

target() { # one `rm -r` target: inside the worktree, or strictly below a temp root
  local p="$1" v; for v in HOME TMPDIR; do p="${p/#\$$v/${!v:-}}"; p="${p/#\$\{$v\}/${!v:-}}"; done
  case "$p" in *\$* | \~[!/]*) deny "rm -r target $1 has an unexpanded variable: use a literal path";; esac
  p="$(canon "$p")"
  case "$p" in "$root"/?*) return 0;; "$root" | "$home" | "$home"/*) deny "rm -r of $1 (worktree root or home)";; esac
  if [ -e "$p/.git" ]; then deny "rm -r of another git checkout or worktree: $1"; fi
  case "$p" in /tmp/?* | /private/tmp/?* | "$(phys "${TMPDIR:-/nonexistent}")"/?*) return 0;; esac
  deny "rm -r outside the worktree: $1"
}
check() { # the words of one command segment
  while [ $# -gt 0 ] && [[ ${1##*/} != rm && ${1##*/} != git ]]; do shift; done
  [ $# -gt 0 ] || return 0
  local k="${1##*/}" d t="" rec="" a; shift
  if [[ $k == git ]]; then
    if [[ " $* " == *" clean "* || " $* " == *" stash "* ]]; then for a in "$@"; do [[ $a =~ ^(--all|-[a-zA-Z]*[xa][a-zA-Z]*)$ ]] && deny "git clean -x / stash -a remove the ignored .claude/ and .ai-toolkit/"; done; fi
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
# quotes dropped (pu""sh, "$TMPDIR"/x), then quotes glued between a word and a letter turned into a space (bash -c'rm ..')
for sc in "s/[\"'\\\\]//g" "s/([^[:space:]])[\"'\\\\]+([[:alnum:]])/\\1 \\2/g; s/[\"'\\\\]//g"; do
  while IFS= read -r seg; do check $seg; done < <(printf '%s\n' "$cmd" | sed -E "$sc" | tr ';|&()`<>' '\n')
done
finish
