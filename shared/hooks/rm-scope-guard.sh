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
#   • the command splits cleanly on `&&`/`||`/`;`/`|` (quote-aware; lone `&`,
#     newlines, unbalanced quotes, substitution, and redirection all fall
#     through), with at least one rm segment,
#   • every rm segment passes the target scope test below, and every non-rm
#     segment is on the built-in read-only list: git status/log/diff/
#     rev-parse (without write-capable flags like --output), ls, head, tail,
#     grep, cat, echo — pipes between them are fine,
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
source "$HOOK_DIR/lib/scope-guard.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")
[ -z "$COMMAND" ] && exit 0

# ── Bail on anything dynamic (silent → normal prompt) ───────────────
# `$`/backtick: substitution and variables are unknowable statically.
# `<`/`>`: redirection (a write primitive even in "read-only" segments).
# `(`/`)`: subshells. `*`/`?`/`[`: globs — rm may expand to anything the
# shell matched. Newlines: not a sanctioned separator. Chaining via
# `&&`/`||`/`;`/`|` is handled by the quote-aware splitter below.
# The length cap bounds the char-scan splitter; an over-long command just
# prompts like any other unproven one.
NL=$'\n'
case "$COMMAND" in
  *'$'* | *'`'* | *'<'* | *'>'* | \
  *'('* | *')'* | *'*'* | *'?'* | *'['* | *"$NL"*) exit 0 ;;
esac
[ "${#COMMAND}" -gt 4096 ] && exit 0

# ── Bail on backslash escaping (silent → normal prompt) ─────────────
# A backslash desyncs this hook's tokenizer from bash. The splitter has no
# escape model, so `rm /tmp/ok\; head /outside` splits at the `;` — but bash
# treats `\;` as a literal char joining ONE rm whose operand then includes
# `/outside`, which never gets scope-checked. Separately,
# normalize_escaped_quotes has already folded any `\"`/`\'` artifact into a
# real quote, which can disagree with bash's word boundaries. Rather than
# model every escape rule, refuse any backslash: scoped targets containing
# spaces can be single-quoted instead. RAW is the PRE-normalization command
# pulled from EVERY source get_shell_command reads — top-level .command
# (Cursor), .tool_input.command (Claude), and the JSON-encoded .toolArgs
# string (Copilot) — so a folded `\"`/`\'` artifact is caught regardless of
# which shape delivered it (COMMAND alone would miss it; checking only
# .command/.tool_input.command would miss the toolArgs shape). jq-less
# platforms cannot use the allow decision anyway, so an empty RAW costs
# nothing there.
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

# Target resolution (sg_resolve_path), the protected-path test
# (sg_is_protected), the read-only benign-segment list (sg_is_benign_segment),
# and the quote-aware compound splitter (sg_walk_segments) are shared with
# chmod-scope-guard via lib/scope-guard.sh.

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
  CWD=$(sg_resolve_path "" "$CWD") || CWD=""
fi
if [ -n "$CWD" ]; then
  ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || true)
else
  ROOT="${CURSOR_PROJECT_DIR:-}"
  if [ -z "$ROOT" ] && command -v jq &>/dev/null; then
    # `|| ROOT=""` guards a malformed (non-array) workspace_roots: jq exits
    # non-zero at runtime there, and unguarded that would errexit the hook
    # with rc=5 — breaking the exit-0-always contract.
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
  local t="$1" resolved
  case "$t" in
    =*) return 1 ;;
  esac
  resolved=$(sg_resolve_path "$CWD" "$t") || return 1
  [ -n "$resolved" ] || return 1
  sg_is_protected "$resolved" "$ROOT" && return 1
  if [ -n "$ROOT" ]; then
    [ "$resolved" = "$ROOT" ] && return 1
    case "$resolved" in
      "$ROOT"/*) return 0 ;;
    esac
  fi
  case "$resolved" in
    /tmp/* | /private/tmp/*) return 0 ;;
  esac
  return 1
}

# ── Validate one rm invocation ───────────────────────────────────────
# Tokenize with the shell's own quoting rules: the global bail-list rejected
# substitution, redirection, subshells, globs, AND every backslash, so the
# only quoting left is `'`/`"` — a model the splitter shares with bash, which
# guarantees no UNQUOTED `;`/`&`/`|` reaches a segment (quoted ones are inert
# words). With `set -f` suppressing globbing, the eval can only word-split,
# strip quotes, and expand `~` — exactly what rm itself would see.
# Unparseable (unbalanced quotes) → fail → silent.
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

# ── Validate one split-out segment ───────────────────────────────────
# Empty segments (a trailing `;`) are skipped. An rm segment must pass the
# scope test and flips RM_SEEN — the whole command needs at least one, or
# this hook has nothing to vouch for. Everything else must be on the shared
# read-only benign list (sg_is_benign_segment).
RM_SEEN=0
check_segment() {
  local seg="$1"
  seg="${seg#"${seg%%[![:space:]]*}"}"
  seg="${seg%"${seg##*[![:space:]]}"}"
  [ -z "$seg" ] && return 0
  case "$seg" in
    rm | rm[[:space:]]*)
      RM_SEEN=1
      check_rm_segment "$seg"
      ;;
    *)
      sg_is_benign_segment "$seg"
      ;;
  esac
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

# Split the command on shell separators (quote-aware) and validate every
# segment; then require at least one rm segment, or this hook has nothing to
# vouch for. Either failing → silent (the ask rule still prompts).
sg_walk_segments "$COMMAND" check_segment || exit 0
[ "$RM_SEEN" -eq 1 ] || exit 0
allow "rm-scope-guard: every rm target resolves inside the project root or /tmp and every chained segment is read-only — auto-allowed (out-of-scope or protected paths still prompt)"
