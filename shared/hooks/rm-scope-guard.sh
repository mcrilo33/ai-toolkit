#!/usr/bin/env bash
# rm-scope-guard — PreToolUse hook auto-allowing provably-scoped rm commands.
#
# User-global settings keep `Bash(rm *)` in the permissions ask list as a
# safety net, so EVERY rm interrupts the user — including obviously harmless
# ones (scratch files, fixtures, build artifacts inside the worktree). This
# guard moves that judgment into a deterministic hook: it ALLOWS when every
# rm target provably resolves inside the project root or /tmp//private/tmp,
# and otherwise stays SILENT so the normal permission prompt fires.
#
# It NEVER denies. The decision space is exactly two-valued:
#   • allow  — hookSpecificOutput.permissionDecision: "allow" on stdout
#   • silent — no output, exit 0: the user's ask rule stays the backstop
#
# ALLOW requires ALL of:
#   • the command is a single rm invocation (no `;`/`&`/`|` chaining, no
#     substitution, no redirection — anything compound falls through),
#   • every target is a static literal (no `$`, backticks, or glob chars),
#   • every target, resolved against the PAYLOAD cwd (realpath -m semantics
#     via python3 — symlink-aware for existing prefixes), lands strictly
#     inside the project root (git toplevel of the payload cwd) or under
#     /tmp//private/tmp,
#   • no target matches a protected pattern: the repo root itself, `.git`,
#     `.claude` (the dir — deleting it would take settings with it) and
#     `.claude/settings*`, `.review/`, any `.env*` basename, `/`, or $HOME.
#     Pattern matching is CASE-INSENSITIVE: macOS APFS is case-insensitive,
#     so `.GIT` IS `.git` on disk. A directory target that exists is also
#     scanned for contained `.env*` files — the basename rule must not be
#     bypassable by deleting the parent.
#
# Never auto-allowed regardless of targets: sudo rm, --no-preserve-root,
# `=`-prefixed relative targets (zsh equals-expansion would resolve them to
# PATH binaries).
# No python3 → fall through (degrade to the prompt, never a false allow).
#
# Exit 0 always — this hook cannot block anything.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")
[ -z "$COMMAND" ] && exit 0

# ── Bail on anything dynamic or compound (silent → normal prompt) ────
# `$`/backtick: substitution and variables are unknowable statically.
# `;`/`&`/`|`/newline: chained commands (compound handling is out of scope
# for a single-rm check). `<`/`>`: redirection. `(`/`)`: subshells.
# `*`/`?`/`[`: globs — rm may expand to anything the shell matched.
NL=$'\n'
case "$COMMAND" in
  *'$'* | *'`'* | *';'* | *'&'* | *'|'* | *'<'* | *'>'* | \
  *'('* | *')'* | *'*'* | *'?'* | *'['* | *"$NL"*) exit 0 ;;
esac

# Resolution requires python3 (macOS /bin/realpath has no -m).
command -v python3 >/dev/null 2>&1 || exit 0

# ── Resolve a target to an absolute physical path ────────────────────
# realpath -m semantics: symlinks resolved for the existing prefix, `..`
# squashed lexically past it (targets may not exist yet). Relative targets
# need a known base — empty base + relative target fails (Cursor reports an
# empty cwd; a relative rm there is unprovable).
resolve_path() {
  local base="$1" target="$2"
  python3 -c '
import os, sys

base, target = sys.argv[1], sys.argv[2]
if not os.path.isabs(target):
    if not base:
        sys.exit(1)
    target = os.path.join(base, target)
print(os.path.realpath(target))
' "$base" "$target" 2>/dev/null
}

# ── Anchor everything to the PAYLOAD cwd (never hub assumptions) ─────
# Claude delivers the live session cwd at the payload top level (it tracks
# `cd`). The project root is that cwd's git toplevel — in a worktree that IS
# the worktree root. Cursor's beforeShellExecution reports an empty cwd: only
# EXPLICIT anchors ($CURSOR_PROJECT_DIR, payload workspace_roots) may supply
# the ROOT scope then — never a walk up from the hook process's own pwd,
# which can sit in an unrelated repo (e.g. a $HOME dotfiles checkout) and
# would falsely scope absolute targets to it. Relative targets always need a
# real cwd (they fail in resolve_path without one). ROOT = $HOME is refused
# outright: the home directory is never a deletion scope.
CWD=$(json_field "$INPUT" "cwd")
ROOT=""
if [ -n "$CWD" ]; then
  CWD=$(resolve_path "" "$CWD") || CWD=""
fi
if [ -n "$CWD" ]; then
  ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || true)
else
  ROOT="${CURSOR_PROJECT_DIR:-}"
  if [ -z "$ROOT" ] && command -v jq &>/dev/null; then
    ROOT=$(echo "$INPUT" | jq -r '.workspace_roots[0] // empty' 2>/dev/null)
  fi
  if [ -n "$ROOT" ] && ! git -C "$ROOT" rev-parse --show-toplevel >/dev/null 2>&1; then
    ROOT=""
  fi
fi
if [ -n "$ROOT" ]; then
  ROOT=$(resolve_path "" "$ROOT") || ROOT=""
fi
if [ -n "$ROOT" ] && [ "$ROOT" = "${HOME:-}" ]; then
  ROOT=""
fi

# ── Is one resolved target deletable without asking? ─────────────────
# In scope: strictly inside ROOT (never ROOT itself) or under /tmp or
# /private/tmp. Protected even in scope: .git (the dir and its content),
# .claude (the dir itself) and .claude/settings*, .review/, and any .env*
# basename anywhere — all matched case-insensitively (APFS is
# case-insensitive: .GIT IS .git on disk). An existing directory target is
# scanned for contained .env* files so the basename rule cannot be bypassed
# by deleting the parent. `/` and $HOME are never deletable (they would fail
# the scope test anyway — the explicit checks document the contract).
# `=`-prefixed relative targets are refused: the hook tokenizes with bash
# semantics, but a zsh-executing platform would equals-expand them to PATH
# binaries far outside any scope this hook proved.
check_target() {
  local t="$1" resolved rel lc
  case "$t" in
    =*) return 1 ;;
  esac
  resolved=$(resolve_path "$CWD" "$t") || return 1
  [ -n "$resolved" ] || return 1
  [ "$resolved" = "/" ] && return 1
  [ "$resolved" = "${HOME:-/nonexistent}" ] && return 1
  case "$(basename "$resolved" | tr '[:upper:]' '[:lower:]')" in
    .env*) return 1 ;;
  esac
  if [ -d "$resolved" ] && [ -n "$(find "$resolved" -iname '.env*' 2>/dev/null | head -1)" ]; then
    return 1
  fi
  if [ -n "$ROOT" ]; then
    [ "$resolved" = "$ROOT" ] && return 1
    case "$resolved" in
      "$ROOT"/*)
        rel="${resolved#"$ROOT"/}"
        lc=$(printf '%s' "$rel" | tr '[:upper:]' '[:lower:]')
        case "$lc" in
          .git | .git/* | .claude | .claude/settings* | .review | .review/*) return 1 ;;
          *) return 0 ;;
        esac
        ;;
    esac
  fi
  case "$resolved" in
    /tmp/* | /private/tmp/*) return 0 ;;
  esac
  return 1
}

# ── Validate one rm invocation ───────────────────────────────────────
# Tokenize with the shell's own quoting rules: the bail-list above already
# rejected every metacharacter that could make `eval set --` execute or
# substitute anything, and `set -f` suppresses globbing, so the eval can
# only word-split, strip quotes, and expand `~` — exactly what rm itself
# would see. Unparseable (unbalanced quotes) → fail → silent.
# Flags are skipped up to `--`; --no-preserve-root fails the segment; at
# least one target is required (nothing to prove otherwise).
check_rm_segment() {
  local seg="$1" end_flags=0 found_target=0 tok
  set -f
  eval "set -- $seg" 2>/dev/null || { set +f; return 1; }
  set +f
  [ "${1:-}" = "rm" ] || return 1
  shift
  for tok in "$@"; do
    if [ "$end_flags" -eq 0 ]; then
      case "$tok" in
        --) end_flags=1; continue ;;
        --no-preserve-root) return 1 ;;
        -?*) continue ;;
      esac
    fi
    check_target "$tok" || return 1
    found_target=1
  done
  [ "$found_target" -eq 1 ]
}

# ── Allow output (cross-platform) ────────────────────────────────────
# hookSpecificOutput.permissionDecision for Claude; top-level permission
# for Cursor beforeShellExecution. Platforms that understand neither keep
# prompting — advisory degradation is the design.
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

check_rm_segment "$COMMAND" || exit 0
allow "rm-scope-guard: every rm target resolves inside the project root or /tmp — auto-allowed (out-of-scope or protected paths still prompt)"
