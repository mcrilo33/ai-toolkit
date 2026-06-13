#!/usr/bin/env bash
# chmod-scope-guard — PreToolUse hook auto-allowing provably-safe in-repo chmod.
#
# User-global settings keep `Bash(chmod *)` in the permissions ask list as a
# safety net, so EVERY chmod interrupts the user — including the benign 99%
# case, `chmod +x` on a repo script, and the compound spokes commonly run
# (`chmod +x script.sh; pytest …`). A settings allowlist can't fix the compound:
# permission rules match the whole command string, so `Bash(chmod +x *)` never
# matches `chmod …; pytest …`. Only a hook can parse the compound — the same
# situation that motivated rm-scope-guard (#13). This is the third scope-guard
# (rm → push → chmod) and reuses rm's machinery via lib/scope-guard.sh.
#
# It NEVER denies. The decision space is exactly two-valued:
#   • allow  — hookSpecificOutput.permissionDecision: "allow" on stdout
#   • silent — no output, exit 0: the user's ask rule stays the backstop
#
# ALLOW requires ALL of:
#   • the command splits cleanly on `&&`/`||`/`;`/`|` (quote-aware; lone `&`,
#     newlines, unbalanced quotes, substitution, and redirection all fall
#     through), with at least one chmod segment,
#   • every chmod segment uses a SAFE MODE: `+x`, `u+x`, `0755`/`755`,
#     `0644`/`644`, `0700`, `0600` and the like — explicitly NOT setuid/setgid
#     (`+s`, `4xxx`/`2xxx`), NOT sticky (`+t`, `1xxx`), NOT world/group-writable
#     (`o+w`, `g+w`, `a+w`, `777`, `666`); owner-write IS safe,
#   • not recursive (`-R`/`--recursive`) and not `--reference=FILE`,
#   • every target is a static literal (no `$`, backticks, or glob chars),
#     resolving (realpath -m against the PAYLOAD cwd) strictly inside the
#     project root — NOT /tmp (unlike rm), never the root itself,
#   • no target is a protected path: `.git`, `.claude`/`.claude/settings*`,
#     `.review`, any `.env*` basename, `.ssh`, `/`, or $HOME — matched
#     case-insensitively (APFS folds case),
#   • every non-chmod segment is read-only/benign (the shared list: git
#     status/log/diff/rev-parse, ls, head, tail, grep, cat, echo) OR a pytest
#     invocation (`pytest …`, `python[3] -m pytest …`) — pytest is not in the
#     user's ask list, so auto-allowing a compound that contains it grants
#     nothing the permission system wasn't already letting run unprompted.
#
# Stays silent (→ normal prompt): dangerous modes, `-R`, targets outside the
# repo or matching protected patterns, dynamic/glob targets, `sudo chmod`,
# anything unparseable. No python3 / jq-less / ambiguous input → fall through
# (never a false allow).
#
# Exit 0 always — this hook cannot block anything.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"
source "$HOOK_DIR/lib/scope-guard.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")
[ -z "$COMMAND" ] && exit 0

# ── Bail on anything dynamic (silent → normal prompt) ───────────────
# Identical posture to rm-scope-guard: `$`/backtick (substitution/vars),
# `<`/`>` (redirection), `(`/`)` (subshells), `*`/`?`/`[` (globs), and newlines
# are all unprovable or not sanctioned separators. Chaining via `&&`/`||`/`;`/
# `|` is handled by the quote-aware splitter (sg_walk_segments). The length cap
# bounds the char-scan splitter.
NL=$'\n'
case "$COMMAND" in
  *'$'* | *'`'* | *'<'* | *'>'* | \
  *'('* | *')'* | *'*'* | *'?'* | *'['* | *"$NL"*) exit 0 ;;
esac
[ "${#COMMAND}" -gt 4096 ] && exit 0

# ── Bail on backslash escaping (silent → normal prompt) ─────────────
# A backslash desyncs this hook's tokenizer from bash (see rm-scope-guard for
# the full rationale). RAW is the PRE-normalization command pulled from every
# source get_shell_command reads, so a folded `\"`/`\'` artifact is caught
# regardless of which payload shape delivered it.
RAW=""
if command -v jq &>/dev/null; then
  RAW=$(printf '%s' "$INPUT" \
    | jq -r '.command // .tool_input.command // (.toolArgs | fromjson? | .command) // empty' 2>/dev/null) || RAW=""
fi
case "$COMMAND$RAW" in
  *'\'*) exit 0 ;;
esac

# Resolution requires python3 (macOS /bin/realpath has no -m).
command -v python3 >/dev/null 2>&1 || exit 0

# ── Anchor everything to the PAYLOAD cwd (never hub assumptions) ─────
# Identical to rm-scope-guard: ROOT is the git toplevel of the payload cwd; for
# Cursor's empty-cwd events only explicit anchors ($CURSOR_PROJECT_DIR,
# workspace_roots) supply it. ROOT = $HOME is refused.
CWD=$(json_field "$INPUT" "cwd")
ROOT=""
if [ -n "$CWD" ]; then
  CWD=$(sg_resolve_path "" "$CWD") || CWD=""
fi
if [ -n "$CWD" ]; then
  ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || true)
else
  ROOT="${CURSOR_PROJECT_DIR:-}"
  if [ -z "$ROOT" ] && command -v jq &>/dev/null; then
    ROOT=$(echo "$INPUT" | jq -r '.workspace_roots[0] // empty' 2>/dev/null) || ROOT=""
  fi
  if [ -n "$ROOT" ] && ! git -C "$ROOT" rev-parse --show-toplevel >/dev/null 2>&1; then
    ROOT=""
  fi
fi
if [ -n "$ROOT" ]; then
  ROOT=$(sg_resolve_path "" "$ROOT") || ROOT=""
fi
if [ -n "$ROOT" ] && [ "$ROOT" = "${HOME:-}" ]; then
  ROOT=""
fi

# ── Is one resolved target chmod-able without asking? ────────────────
# Strictly inside ROOT (never ROOT itself), NOT /tmp (chmod is root-only,
# unlike rm), and not a protected path. sg_is_protected covers .git/.claude/
# .review/.env*//$HOME; chmod additionally protects `.ssh` (issue #27),
# matched case-insensitively under the root. `=`-prefixed relative targets are
# refused: a zsh-executing platform would equals-expand them to PATH binaries.
check_target() {
  local t="$1" resolved rel lc
  case "$t" in
    =*) return 1 ;;
  esac
  resolved=$(sg_resolve_path "$CWD" "$t") || return 1
  [ -n "$resolved" ] || return 1
  sg_is_protected "$resolved" "$ROOT" && return 1
  [ -n "$ROOT" ] || return 1
  [ "$resolved" = "$ROOT" ] && return 1
  case "$resolved" in
    "$ROOT"/*)
      rel="${resolved#"$ROOT"/}"
      lc=$(printf '%s' "$rel" | tr '[:upper:]' '[:lower:]')
      case "$lc" in
        .ssh | .ssh/*) return 1 ;;
      esac
      return 0
      ;;
  esac
  return 1
}

# ── Is an octal mode safe? ───────────────────────────────────────────
# 3 or 4 octal digits. A 4th (high) digit must be 0 — any setuid/setgid/sticky
# bit is unsafe. The group and other digits must not carry the write bit (2):
# digits 2/3/6/7 are group/world-writable. Owner-write is fine.
is_safe_octal() {
  local m="$1" g o
  case "$m" in
    *[!0-7]*) return 1 ;;
  esac
  case "${#m}" in
    3) ;;
    4) [ "${m:0:1}" = "0" ] || return 1; m="${m:1}" ;;
    *) return 1 ;;
  esac
  g="${m:1:1}"
  o="${m:2:1}"
  case "$g" in 2 | 3 | 6 | 7) return 1 ;; esac
  case "$o" in 2 | 3 | 6 | 7) return 1 ;; esac
  return 0
}

# ── Is one symbolic clause safe? ─────────────────────────────────────
# A clause is `[ugoa]*[+-=][perms]`. Removing bits (`-`) never escalates, so any
# valid removable perms are safe. Adding/setting (`+`/`=`) may only use
# `r`/`x`/`X` for any who, and `w` only when who is exactly `u` (owner-write).
# `s` (setuid/setgid), `t` (sticky), and the copy-from refs `u`/`g`/`o` in the
# perms position are all unsafe. (The bare `=perms` who-less form starts with
# `=` and is refused upstream as a mode token — zsh equals-expansion hazard —
# so `who` is non-empty here whenever the op is `=`.)
is_safe_symbolic_clause() {
  local c="$1" rest who="" op perms ch
  rest="$c"
  while [ -n "$rest" ]; do
    ch="${rest:0:1}"
    case "$ch" in
      [ugoa]) who+="$ch"; rest="${rest:1}" ;;
      *) break ;;
    esac
  done
  op="${rest:0:1}"
  perms="${rest:1}"
  case "$op" in
    -)
      case "$perms" in *[!rwxXst]*) return 1 ;; esac
      return 0
      ;;
    + | =) ;;
    *) return 1 ;;
  esac
  case "$perms" in *[!rwxXw]*) return 1 ;; esac
  case "$perms" in
    *w*) [ "$who" = "u" ] || return 1 ;;
  esac
  return 0
}

# ── Is a whole mode token safe? ──────────────────────────────────────
# All-octal-digit tokens take the octal path; anything else is symbolic and is
# checked clause-by-clause (comma-separated). A `=`-prefixed token is refused
# by check_chmod_segment before reaching here.
is_safe_mode() {
  local m="$1" clause
  [ -z "$m" ] && return 1
  case "$m" in
    *[!0-7]*)
      local IFS=','
      for clause in $m; do
        is_safe_symbolic_clause "$clause" || return 1
      done
      return 0
      ;;
    *)
      is_safe_octal "$m"
      ;;
  esac
}

# ── Validate one chmod invocation ────────────────────────────────────
# Tokenize with the shell's own quoting rules (the global bail-list rejected
# substitution, redirection, subshells, globs, and every backslash). `-R`/
# `--recursive` and `--reference=` fail the segment; other flags are skipped up
# to `--`. The FIRST non-flag token is the mode (refused if `=`-prefixed, then
# safety-checked); the rest are targets, each of which must pass the scope test.
# A mode and at least one target are required.
check_chmod_segment() {
  local seg="$1" end_flags=0 mode_set=0 found_target=0 tok
  set -f
  eval "set -- $seg" 2>/dev/null || { set +f; return 1; }
  set +f
  [ "${1:-}" = "chmod" ] || return 1
  shift
  for tok in "$@"; do
    if [ "$end_flags" -eq 0 ]; then
      case "$tok" in
        --) end_flags=1; continue ;;
        -R | --recursive) return 1 ;;
        --reference=*) return 1 ;;
        -?*) continue ;;
      esac
    fi
    if [ "$mode_set" -eq 0 ]; then
      case "$tok" in
        =*) return 1 ;;
      esac
      is_safe_mode "$tok" || return 1
      mode_set=1
      continue
    fi
    check_target "$tok" || return 1
    found_target=1
  done
  [ "$mode_set" -eq 1 ] && [ "$found_target" -eq 1 ]
}

# ── Is a pytest invocation? ──────────────────────────────────────────
# pytest is NOT in the user's ask list, so it already runs unprompted; allowing
# a compound that contains it grants nothing new. Accepts `pytest …`, any
# path-suffixed `pytest` (e.g. `./.venv/bin/pytest …` — the leading path is
# intentionally unconstrained), and `python[3] -m pytest …` (optional leading
# path). Plain `python foo.py` / `python -c …` are NOT matched — only a
# `-m pytest` runner — so arbitrary code execution still prompts.
is_pytest_segment() {
  local seg="$1"
  printf '%s' "$seg" | grep -qE \
    '^([^[:space:]]*/)?(pytest|python3?[[:space:]]+-m[[:space:]]+pytest)([[:space:]]|$)'
}

# ── Validate one split-out segment ───────────────────────────────────
# Empty segments (a trailing `;`) are skipped. A chmod segment must pass the
# mode + scope test and flips CHMOD_SEEN — the whole command needs at least one.
# Every other segment must be on the shared read-only benign list or a pytest
# invocation.
CHMOD_SEEN=0
check_segment() {
  local seg="$1"
  seg="${seg#"${seg%%[![:space:]]*}"}"
  seg="${seg%"${seg##*[![:space:]]}"}"
  [ -z "$seg" ] && return 0
  case "$seg" in
    chmod | chmod[[:space:]]*)
      CHMOD_SEEN=1
      check_chmod_segment "$seg"
      ;;
    *)
      sg_is_benign_segment "$seg" || is_pytest_segment "$seg"
      ;;
  esac
}

# ── Allow output (cross-platform) ────────────────────────────────────
# hookSpecificOutput.permissionDecision for Claude; top-level permission for
# Cursor beforeShellExecution. Platforms that understand neither keep prompting.
allow() {
  local reason="$1"
  telemetry_event "allow"
  if command -v jq &>/dev/null; then
    jq -nc --arg r "$reason" '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "allow",
        permissionDecisionReason: $r
      },
      permission: "allow"
    }'
  else
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"%s"},"permission":"allow"}\n' "$reason"
  fi
  exit 0
}

# Split the command on shell separators (quote-aware) and validate every
# segment; then require at least one chmod segment, or this hook has nothing to
# vouch for. Either failing → silent (the ask rule still prompts).
sg_walk_segments "$COMMAND" check_segment || exit 0
[ "$CHMOD_SEEN" -eq 1 ] || exit 0
allow "chmod-scope-guard: every chmod uses a safe mode (no setuid/setgid, no world/group-write, not recursive) on static targets inside the project root, and every chained segment is read-only or pytest — auto-allowed (dangerous modes, out-of-scope, or protected paths still prompt)"
