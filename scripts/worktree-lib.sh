#!/usr/bin/env bash
#
# worktree-lib.sh — shared helpers for worktree-new.sh and worktree-done.sh.
# Source this file; do not execute it. Callers set WT_PROG to their program name
# so diagnostics are attributed correctly.
#
# The two scripts MUST agree on slugify rules and on how a user-supplied target
# resolves to a worktree, so that anything you can create you can also tear down.
# Keeping that logic here is what guarantees it.

# --- diagnostics --------------------------------------------------------------

wt_die()  { printf '%s: %s\n' "${WT_PROG:-worktree}" "$*" >&2; exit 1; }
wt_warn() { printf '%s: %s\n' "${WT_PROG:-worktree}" "$*" >&2; }

# --- paths --------------------------------------------------------------------

# Canonical absolute path (resolves symlinks, e.g. /tmp -> /private/tmp on macOS).
# Empty output if the path does not exist.
wt_realpath() { (cd "$1" 2>/dev/null && pwd -P) || true; }

# Absolute, canonical path of the MAIN worktree — the first entry of
# `git worktree list`. Correct even when called from inside a linked worktree,
# which is why both scripts use this instead of `git rev-parse --show-toplevel`.
wt_main_root() {
  local p
  p="$(git worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2; exit}')"
  [ -n "$p" ] || return 1
  wt_realpath "$p"
}

# --- slug ---------------------------------------------------------------------

# Lowercase, collapse non-alphanumeric runs to '-', strip edges, keep <=4 segments.
# Both creation and teardown run identical input through this, so a raw arg
# normalizes the same way on both sides.
wt_slugify() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' \
    | cut -d- -f1-4
}

# --- worktree enumeration / resolution ---------------------------------------

# Emit "path<TAB>branch" (branch without refs/heads/) for every worktree EXCEPT
# the main one. Detached worktrees emit an empty branch field. Handles the
# porcelain stream's lack of a trailing blank line by flushing at EOF.
# Args: $1 = canonical main root.
wt_task_worktrees() {
  local main="$1" wt="" br=""
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) wt="${line#worktree }"; br="" ;;
      "branch "*)   br="${line#branch }"; br="${br#refs/heads/}" ;;
      "")
        if [ -n "$wt" ] && [ "$(wt_realpath "$wt")" != "$main" ]; then
          printf '%s\t%s\n' "$wt" "$br"
        fi
        wt=""; br=""
        ;;
    esac
  done < <(git worktree list --porcelain 2>/dev/null)
  if [ -n "$wt" ] && [ "$(wt_realpath "$wt")" != "$main" ]; then
    printf '%s\t%s\n' "$wt" "$br"
  fi
}

# Pretty-print the task worktrees to stderr (path + branch), for error recovery.
# Args: $1 = canonical main root.
wt_print_worktrees() {
  local main="$1" any="" wt br
  while IFS=$'\t' read -r wt br; do
    [ -n "$wt" ] || continue
    any=1
    printf '    %-50s %s\n' "$wt" "${br:-(detached)}" >&2
  done < <(wt_task_worktrees "$main")
  [ -n "$any" ] || printf '    (none)\n' >&2
}

# Resolve a user-supplied target to exactly one task-worktree path.
# Matches a target against each worktree by, in order of intent:
#   - canonical path equality (target is/locates a worktree dir)
#   - directory basename, or its tag (basename with the "<repo>-" prefix stripped)
#   - the slugified target vs that tag (so raw "Refactor_Sync" finds "refactor-sync")
#   - the full branch name, or the branch's trailing slug
#   - the leading issue number of the branch slug (so "42" finds feature/42-foo)
# Prints the single match on stdout and returns 0. On zero or multiple matches it
# returns 1 — the caller is expected to list candidates and exit.
# Args: $1 = target, $2 = canonical main root.
wt_resolve() {
  local target="$1" main="$2"
  local tslug repo trp wt br base tag bslug bnum
  tslug="$(wt_slugify "$target")"
  repo="$(basename "$main")"
  trp="$(wt_realpath "$target")"

  local matches=() seen=""
  while IFS=$'\t' read -r wt br; do
    [ -n "$wt" ] || continue
    base="$(basename "$wt")"
    tag="${base#"${repo}-"}"
    bslug="${br##*/}"
    bnum="${bslug%%-*}"
    if { [ -n "$trp" ] && [ "$trp" = "$(wt_realpath "$wt")" ]; } \
       || [ "$target" = "$base" ] \
       || [ "$target" = "$tag" ] || [ "$tslug" = "$tag" ] \
       || { [ -n "$br" ] && [ "$target" = "$br" ]; } \
       || { [ -n "$bslug" ] && { [ "$target" = "$bslug" ] || [ "$tslug" = "$bslug" ]; }; } \
       || { [ -n "$bnum" ] && [ "$bnum" != "$bslug" ] && [ "$target" = "$bnum" ]; }; then
      case "$seen" in
        *"|$wt|"*) ;;            # already collected
        *) matches+=("$wt"); seen="${seen}|$wt|" ;;
      esac
    fi
  done < <(wt_task_worktrees "$main")

  if [ "${#matches[@]}" -eq 1 ]; then
    printf '%s\n' "${matches[0]}"
    return 0
  fi
  return 1
}
