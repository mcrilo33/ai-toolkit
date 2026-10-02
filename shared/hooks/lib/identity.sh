#!/usr/bin/env bash
# identity.sh — the ONE reader of spoke identity (issue #360, Orca migration S2a).
# Identity is RECORDED data: provision-worktree.sh writes <wt>/.ai-toolkit/identity
# (key=value lines, LF-terminated, no quoting) and the guards read it here. The three
# historical inference strategies — the WT_SPOKE env marker, the linked-worktree git-dir
# pattern and the issue number in the branch name — live ONLY in this file, as per-key
# fallbacks for a worktree with no record (or an empty value), so such a worktree behaves
# exactly as before the record existed.
#
# Self-contained pure bash 3.2 (no hook-lib dependencies), sourced by the guard hooks from
# their own lib/ dir. The record is parsed line by line and NEVER sourced or eval'd; blank
# lines, `#` comments and lines without `=` are ignored, and the first occurrence of a key
# wins.
#
# API (root defaults to the current directory):
#   ai_toolkit_identity_get <key> [root]        value + rc 0; empty + rc 1 when unknown
#   ai_toolkit_identity_issue [root]            record issue, else branch-leaf leading digits
#   ai_toolkit_identity_is_spoke [root] [fb]    rc 0 for a spoke; fb = any (default) | env | gitdir
#   ai_toolkit_identity_has_issue_anchor [root] rc 0 when the record or branch names an issue

# Print the recorded value of <key> under <root>; rc 1 and nothing when the record, the key
# or its value is absent.
ai_toolkit_identity_get() {
  local key="${1:-}" root="${2:-.}" file line value=""
  file="$root/.ai-toolkit/identity"
  [ -n "$key" ] && [ -f "$file" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$key="*) value="${line#*=}"; break ;;
    esac
  done < "$file"
  [ -n "$value" ] || return 1
  printf '%s' "$value"
}

# The issue number: the record, else the leading digits of the branch's last path segment.
ai_toolkit_identity_issue() {
  local root="${1:-.}" v branch leaf
  if v="$(ai_toolkit_identity_get issue "$root")"; then
    printf '%s' "$v"
    return 0
  fi
  branch="$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  leaf="${branch##*/}"
  v="${leaf%%[!0-9]*}"
  [ -n "$v" ] || return 1
  printf '%s' "$v"
}

# Is <root> a spoke? A non-empty recorded issue always wins; otherwise the fallback selected
# by $2: `env` = WT_SPOKE only, `gitdir` = linked-worktree git-dir only, `any` = either.
ai_toolkit_identity_is_spoke() {
  local root="${1:-.}" fb="${2:-any}" gd
  ai_toolkit_identity_get issue "$root" >/dev/null && return 0
  case "$fb" in
    env | any) [ -n "${WT_SPOKE:-}" ] && return 0 ;;
  esac
  case "$fb" in
    gitdir | any)
      gd="$(git -C "$root" rev-parse --absolute-git-dir 2>/dev/null || true)"
      case "$gd" in
        */.git/worktrees/*) return 0 ;;
      esac
      ;;
  esac
  return 1
}

# Does <root> carry an issue anchor? A recorded issue does; else the branch must hold a
# tracker key (PROJ-12) anywhere, or a bare number as the first segment right after the type
# prefix (feature/142-x, fix/142). Anchoring the bare number to the post-"/" segment avoids
# incidental numbers inside a slug (oauth-2-factor, utf-8) or version/year tokens.
ai_toolkit_identity_has_issue_anchor() {
  local root="${1:-.}" branch
  ai_toolkit_identity_get issue "$root" >/dev/null && return 0
  branch="$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if echo "$branch" | grep -qE '(^|[/_-])[A-Z][A-Z0-9]+-[0-9]+([/_-]|$)' \
     || echo "$branch" | grep -qE '/[0-9]+([/_-]|$)' \
     || echo "$branch" | grep -qE '^[0-9]+([/_-]|$)'; then
    return 0
  fi
  return 1
}
