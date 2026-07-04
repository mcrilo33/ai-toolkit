#!/usr/bin/env bash
# test-reverse-index.sh — changed-file → referencing-test-files map (issue #123).
#
# The repo's convention is that a test names the script it covers as a literal
# token: either the full repo path in a docstring ("shared/skills/hub/scripts/
# hub-afk.sh") or the basename in a path build (HOOKS_DIR / "commit-gauntlet.sh").
# This lib scans tests/**/test_*.py for filename-shaped tokens and answers
# "which test files reference this changed file's basename as an EXACT token".
# Exactness matters: chmod-scope-guard.sh must never count as a reference to
# scope-guard.sh. Only collectible test_*.py files are map targets — a fixture
# or conftest mentioning a script is not a runnable selection.
#
# CACHE: <git-common-dir>/.test-reverse-index/<tree-hash-of-tests-at-HEAD> —
# structural invalidation exactly like the gate stamps (#122): any committed
# tests/ change yields a new key; shared by the hub and every spoke worktree,
# never in-tree. A dirty tests/ (tracked modifications or untracked files)
# bypasses the cache with a fresh scan of the WORKING TREE, so a just-written
# test is visible to the commit-time nudge before it is committed; the cache is
# neither consulted nor overwritten then.
#
# Functions operate on the repo at $PWD (test-select.sh runs at the repo root;
# other callers cd in a subshell first). Lookups never fail the caller: any
# git trouble degrades to a fresh scan, and a missing tests/ yields an empty
# map — which the selector treats as "nothing mapped", i.e. today's
# escalate-to-full behavior.

# Emit "token<TAB>test-file" lines for every filename-shaped token in every
# tests/**/test_*.py of the working tree. Tokens are maximal words carrying an
# extension (plus the well-known extensionless Dockerfile/Makefile), so a
# basename embedded in a longer name or path decomposes to itself and nothing
# shorter.
_reverse_index_scan() {
  [ -d tests ] || return 0
  local f
  while IFS= read -r f; do
    grep -ohE '[A-Za-z0-9_][A-Za-z0-9_.-]*\.[A-Za-z0-9]+|Dockerfile|Makefile' "$f" 2>/dev/null \
      | sort -u \
      | while IFS= read -r tok; do printf '%s\t%s\n' "$tok" "$f"; done
  done < <(find tests -type f -name 'test_*.py' 2>/dev/null | sort)
}

# Print the absolute cache directory <git-common-dir>/.test-reverse-index,
# absolutizing a relative --git-common-dir the way gate-stamp.sh does.
_reverse_index_dir() {
  local common
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -n "$common" ] || return 1
  case "$common" in
    /*) ;;
    *)  common="$PWD/$common" ;;
  esac
  printf '%s/.test-reverse-index' "$common"
}

# Print the cache file path for the current state — ONLY when tests/ is clean
# (no tracked modifications, no untracked files) so the cached map provably
# matches the working tree the suite would run against. Dirty or keyless
# (no tests/ at HEAD) states return 1: scan fresh instead.
_reverse_index_cache_path() {
  [ -z "$(git status --porcelain -- tests 2>/dev/null)" ] || return 1
  local key dir
  key="$(git rev-parse 'HEAD:tests' 2>/dev/null)" || return 1
  dir="$(_reverse_index_dir)" || return 1
  printf '%s/%s' "$dir" "$key"
}

# Build the map into <cache>. Temp-file + mv keeps concurrent builders atomic
# (two builders on the same tests/ tree write identical content). GC of stale
# keys rides along, like the gate-stamp mint.
_reverse_index_build_cache() {
  local cache="$1" dir tmp
  dir="${cache%/*}"
  mkdir -p "$dir"
  tmp="$(mktemp "$dir/.build.XXXXXX")" || return 1
  _reverse_index_scan > "$tmp"
  mv -f "$tmp" "$cache"
  find "$dir" -type f -mtime +14 -delete 2>/dev/null || true
}

# reverse_index_tests_for <changed-file> — print the sorted unique test files
# referencing the changed file's basename as an exact token; empty when none.
# Always returns 0: "no mapping" is an answer, not an error.
reverse_index_tests_for() {
  local base="${1##*/}" cache
  [ -n "$base" ] || return 0
  if cache="$(_reverse_index_cache_path)"; then
    [ -f "$cache" ] || _reverse_index_build_cache "$cache" || return 0
    awk -F'\t' -v b="$base" '$1 == b { print $2 }' "$cache" | sort -u
  else
    _reverse_index_scan | awk -F'\t' -v b="$base" '$1 == b { print $2 }' | sort -u
  fi
  return 0
}
