#!/usr/bin/env bash
# scope-guard.sh — shared machinery for the scope-guard hook family.
#
# rm-scope-guard and chmod-scope-guard share an identical safety posture:
# allow-or-silent, never deny, exit 0 always, and degrade to SILENT (the normal
# permission prompt fires) on anything dynamic, unparseable, jq-less, or
# ambiguous — never a false allow. The pieces each guard would otherwise copy
# verbatim live here; sourced AFTER lib/utils.sh.
#
#   • sg_resolve_path      — realpath -m via python3 (symlink-aware prefix)
#   • sg_is_protected      — protected-path test (.git/.claude/.review/.env*,
#                            / and $HOME), relative to a project root
#   • sg_is_benign_segment — the read-only command allowlist for compound chains
#   • sg_walk_segments     — quote-aware compound splitter (calls a per-segment
#                            validator callback)
#
# Each guard keeps its OWN scope decision (rm allows /tmp; chmod is root-only)
# and its OWN per-segment validator — only the mechanics are shared.

# ── Resolve a target to an absolute physical path ────────────────────
# realpath -m semantics: symlinks resolved for the existing prefix, `..`
# squashed lexically past it (targets may not exist yet). Relative targets
# need a known base — empty base + relative target fails (Cursor reports an
# empty cwd; a relative target there is unprovable). Requires python3
# (macOS /bin/realpath has no -m); callers verify python3 before relying on it.
sg_resolve_path() {
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

# ── Protected-path test (shared across scope guards) ─────────────────
# Returns 0 when the RESOLVED absolute path must NEVER be auto-allowed,
# regardless of the operation, and 1 otherwise. Protected: `/`, $HOME, any
# `.env*` basename, an existing directory that CONTAINS a `.env*` file (so the
# basename rule cannot be bypassed by touching the parent), or — relative to
# $root — `.git` (the dir and its content), `.claude` (the dir itself) and
# `.claude/settings*`, and `.review/`. Matching is CASE-INSENSITIVE: macOS APFS
# folds case, so `.GIT` IS `.git` on disk. Callers handle "resolved == root"
# and the in/out-of-scope decision themselves; this only flags the always-deny
# patterns.
sg_is_protected() {
  local resolved="$1" root="$2" rel lc
  [ "$resolved" = "/" ] && return 0
  [ "$resolved" = "${HOME:-/nonexistent}" ] && return 0
  case "$(basename "$resolved" | tr '[:upper:]' '[:lower:]')" in
    .env*) return 0 ;;
  esac
  if [ -d "$resolved" ] && [ -n "$(find "$resolved" -iname '.env*' 2>/dev/null | head -1)" ]; then
    return 0
  fi
  if [ -n "$root" ]; then
    case "$resolved" in
      "$root"/*)
        rel="${resolved#"$root"/}"
        lc=$(printf '%s' "$rel" | tr '[:upper:]' '[:lower:]')
        case "$lc" in
          .git | .git/* | .claude | .claude/settings* | .review | .review/*) return 0 ;;
        esac
        ;;
    esac
  fi
  return 1
}

# ── Is a non-primary segment read-only/benign? ──────────────────────
# The built-in list from issue #13: git status/log/diff/rev-parse, ls, head,
# tail, grep, cat, echo. None can write without redirection, and redirection is
# globally rejected by the callers before a segment ever reaches here. Pre-
# subcommand git global flags are restricted to a known-safe allowlist
# (-C <path>, --no-pager, -P) rather than a wildcard passthrough — a bare
# `-[^space]+` would vouch for `git --exec-path=…` and `-c <write-config>` forms
# whose read-only subcommand can be turned into a write or code-exec primitive.
# Post-subcommand, git's own --output flag is still a file-write primitive, so
# any --output spelling disqualifies it.
sg_is_benign_segment() {
  local seg="$1"
  if printf '%s' "$seg" | grep -qE '^(ls|head|tail|grep|cat|echo)([[:space:]]|$)'; then
    return 0
  fi
  if printf '%s' "$seg" \
    | grep -qE '^git([[:space:]]+(-C[[:space:]]+[^[:space:]]+|--no-pager|-P))*[[:space:]]+(status|log|diff|rev-parse)([[:space:]]|$)'; then
    printf '%s' "$seg" | grep -qE -- '--output' && return 1
    return 0
  fi
  return 1
}

# ── Quote-aware compound split ───────────────────────────────────────
# Walk the command once, tracking single/double-quote state ($ and backtick are
# already banned by the caller's global bail-list, so quoted content is inert).
# At top level, `&&`, `||`, `;`, and `|` end a segment, which is handed to the
# VALIDATOR callback immediately; a lone `&` (background) and unbalanced quotes
# fall through (return 1). The validator is a function name taking one segment
# and returning 0 (segment is provably safe) or non-zero (not safe). Returns 0
# only if every segment validated; the caller separately enforces "at least one
# primary segment was seen" via a flag its validator sets.
sg_walk_segments() {
  local cmd="$1" validator="$2" seg="" ch next i=0 len in_sq=0 in_dq=0
  len=${#cmd}
  while [ "$i" -lt "$len" ]; do
    ch=${cmd:$i:1}
    if [ "$in_sq" -eq 1 ]; then
      [ "$ch" = "'" ] && in_sq=0
      seg+=$ch
      i=$((i + 1))
      continue
    fi
    if [ "$in_dq" -eq 1 ]; then
      [ "$ch" = '"' ] && in_dq=0
      seg+=$ch
      i=$((i + 1))
      continue
    fi
    case "$ch" in
      "'") in_sq=1; seg+=$ch ;;
      '"') in_dq=1; seg+=$ch ;;
      ';')
        "$validator" "$seg" || return 1
        seg=""
        ;;
      '&')
        next=${cmd:$((i + 1)):1}
        [ "$next" = '&' ] || return 1
        i=$((i + 1))
        "$validator" "$seg" || return 1
        seg=""
        ;;
      '|')
        next=${cmd:$((i + 1)):1}
        [ "$next" = '|' ] && i=$((i + 1))
        "$validator" "$seg" || return 1
        seg=""
        ;;
      *) seg+=$ch ;;
    esac
    i=$((i + 1))
  done
  if [ "$in_sq" -eq 1 ] || [ "$in_dq" -eq 1 ]; then
    return 1
  fi
  "$validator" "$seg" || return 1
  return 0
}
