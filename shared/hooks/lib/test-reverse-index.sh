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
# SOURCE GRAPH (issue #326): a changed *.sh is often a sourced *library* that no
# test names directly, only its callers do. testmon is blind to shell, so such a
# lib would otherwise escalate the whole push to the full suite. So the map is
# augmented with a shell source-dependency graph: for every shipped *.sh we scan
# its `source`/`.` includes to learn "script -> libs it sources", invert to
# "lib -> scripts that (transitively) source it", and map a changed lib to the
# UNION of the tests naming it directly and the mirror tests of all its
# transitive dependents. A leaf script still maps to its own test; a bare-$var
# source with no resolvable .sh basename simply drops that edge — under-mapping
# (backstopped by the #124 post-land sweep), never a wrong mapping.
#
# CACHE: <git-common-dir>/.test-reverse-index/<key>, where <key> is a content
# hash over the tree objects the map depends on — tests/ (the token map) and the
# shipped shell dirs (the source graph). Structural invalidation like the gate
# stamps (#122): any committed change under tests/ or those dirs yields a new key
# — a superset of .sh changes (a doc commit under shared/ also rebuilds), which
# is safe over-invalidation, never a stale map. Shared by the hub and every spoke
# worktree, never in-tree. A dirty tests/ or shell dir (tracked modifications or
# untracked files) bypasses the cache with a fresh scan of the WORKING TREE, so a
# just-edited lib is visible to the commit-time nudge before it is committed; the
# cache is neither consulted nor overwritten then.
#
# Known assumption: the scan reads the WORKING TREE while the key names the
# HEAD:tests tree, and a gitignored test_*.py is invisible to the clean check —
# such a file could leak into a cache shared across worktrees. The direction is
# over-mapping (extra tests selected), never skipping, so it is accepted rather
# than paying `git check-ignore` on every scan.
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
    # `|| true`: a token-less test file makes grep exit 1, which under a
    # caller's `set -euo pipefail` would abort the scan at that file and
    # silently truncate the map — the one direction this lib must never fail.
    { grep -ohE '[A-Za-z0-9_][A-Za-z0-9_.-]*\.[A-Za-z0-9]+|Dockerfile|Makefile' "$f" 2>/dev/null \
      || true; } \
      | sort -u \
      | while IFS= read -r tok; do printf '%s\t%s\n' "$tok" "$f"; done
  done < <(find tests -type f -name 'test_*.py' 2>/dev/null | sort)
}

# The shipped shell surface scanned for source-dependency edges (issue #326).
REVERSE_INDEX_SHELL_DIRS=("scripts" "shared/hooks" "shared/skills" "dashboard/langfuse")

# Emit "sourced-lib-basename<TAB>sourcing-script-basename" for every `source X` /
# `. X` include in every shipped *.sh whose argument resolves to a .sh basename.
# A bare-$var argument (no .sh token) drops its edge — under-mapping, backstopped
# by the #124 sweep, never a wrong mapping. `|| true` keeps a source-less file
# from aborting the scan under a caller's `set -euo pipefail`, exactly as the
# token scan above does. A self-source (a file sourcing its own basename) is
# skipped so it can't seed a spurious cycle.
_reverse_index_source_edges() {
  local dirs=() d f base
  for d in "${REVERSE_INDEX_SHELL_DIRS[@]}"; do
    [ -d "$d" ] && dirs+=("$d")
  done
  [ "${#dirs[@]}" -gt 0 ] || return 0
  while IFS= read -r f; do
    base="${f##*/}"
    { grep -hE '^[[:space:]]*(source|\.)[[:space:]]' "$f" 2>/dev/null || true; } \
      | { grep -oE '[A-Za-z0-9_.-]+\.sh' || true; } \
      | while IFS= read -r lib; do
          [ -n "$lib" ] && [ "$lib" != "$base" ] && printf '%s\t%s\n' "$lib" "$base"
        done
  done < <(find "${dirs[@]}" -type f -name '*.sh' 2>/dev/null | sort)
}

# The full map: the direct token map (_reverse_index_scan) PLUS graph edges — for
# each sourced lib, one "lib<TAB>test" line per mirror test of every script that
# transitively sources it. One awk pass: load the source edges (from a process-
# substitution fd so an empty edge set is handled cleanly) into an adjacency list
# "lib -> scripts that source it", pass the direct lines through while indexing
# "token -> tests", then BFS each lib's transitive dependents and emit their
# tests. Duplicates are harmless — callers sort -u at lookup. `_reverse_index_scan`
# stays a PURE direct scan (the coverage meta-test reads its tokens verbatim).
_reverse_index_scan_expanded() {
  _reverse_index_scan \
    | awk -F'\t' -v edgesfile=<(_reverse_index_source_edges) '
      function collect(start,   wl, seen, head, tail, cur, i, k, arr, m, tarr, n) {
        head = 0; tail = 0; wl[tail++] = start
        while (head < tail) {
          cur = wl[head++]
          m = split(adj[cur], arr, SUBSEP)
          for (i = 1; i <= m; i++) {
            k = arr[i]
            if (k != "" && !(k in seen)) { seen[k] = 1; wl[tail++] = k }
          }
        }
        for (k in seen) {
          if (k in tests) {
            n = split(tests[k], tarr, SUBSEP)
            for (i = 1; i <= n; i++) if (tarr[i] != "") print start "\t" tarr[i]
          }
        }
      }
      BEGIN {
        while ((getline line < edgesfile) > 0) {
          p = index(line, "\t")
          if (p == 0) continue
          lib = substr(line, 1, p - 1)
          scr = substr(line, p + 1)
          adj[lib] = adj[lib] SUBSEP scr
        }
        close(edgesfile)
      }
      { print; tests[$1] = tests[$1] SUBSEP $2 }
      END { for (lib in adj) collect(lib) }
    '
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

# The cache key (issue #123 + #326): a content hash over the tree objects the map
# depends on — tests/ (the token map) and the shipped shell dirs (the source
# graph). Coarse by dir, not by .sh blob: any committed change under these mints a
# new key (safe over-invalidation, ~sub-second rebuild); commits elsewhere keep
# it. Dirs absent at HEAD are skipped; an all-absent set returns 1 (scan fresh).
# `-q --verify` keeps a missing dir quiet (no stray "HEAD:dir" echoed to stdout).
_reverse_index_key() {
  local d sha parts=""
  for d in tests scripts shared dashboard; do
    sha="$(git rev-parse -q --verify "HEAD:$d" 2>/dev/null)" || sha=""
    [ -n "$sha" ] || continue
    parts="${parts}${d}:${sha};"
  done
  [ -n "$parts" ] || return 1
  printf '%s' "$parts" | git hash-object --stdin 2>/dev/null
}

# Print the cache file path for the current state — ONLY when the map's inputs
# are clean (no tracked modifications, no untracked files under tests/ or the
# shipped shell dirs) so the cached map provably matches the working tree the
# suite would run against. `git status` tolerates a pathspec that is absent at
# HEAD, so all four are passed unconditionally. Dirty or keyless states return 1:
# scan fresh instead.
_reverse_index_cache_path() {
  [ -z "$(git status --porcelain -- tests scripts shared dashboard 2>/dev/null)" ] || return 1
  local key dir
  key="$(_reverse_index_key)" || return 1
  [ -n "$key" ] || return 1
  dir="$(_reverse_index_dir)" || return 1
  printf '%s/%s' "$dir" "$key"
}

# Build the map into <cache>. Temp-file + mv keeps concurrent builders atomic
# (two builders on the same inputs write identical content). GC of stale keys
# rides along, like the gate-stamp mint.
_reverse_index_build_cache() {
  local cache="$1" dir tmp
  dir="${cache%/*}"
  mkdir -p "$dir"
  tmp="$(mktemp "$dir/.build.XXXXXX")" || return 1
  _reverse_index_scan_expanded > "$tmp"
  mv -f "$tmp" "$cache"
  find "$dir" -type f -mtime +14 -delete 2>/dev/null || true
}

# Exempt list (issue #123): repo-root .test-select-exempt names paths with
# legitimately no test surface (LICENSE, .gitignore, editor config). One path
# per line; `#` starts a comment; an entry matches exactly or as a directory
# prefix ("settings/" covers its children); surrounding whitespace/CR is
# stripped. An absent file means no exemptions. Lives here so the selector
# (test-select.sh) and the commit-time nudge (commit-gauntlet.sh) share ONE
# parser and can never drift on what "exempt" means.
REVERSE_INDEX_EXEMPT_LIST=".test-select-exempt"
reverse_index_is_exempt() {
  local f="$1" entry
  [ -f "$REVERSE_INDEX_EXEMPT_LIST" ] || return 1
  while IFS= read -r entry || [ -n "$entry" ]; do
    entry="${entry%%#*}"
    entry="${entry%"${entry##*[![:space:]]}"}"   # strip trailing whitespace/CR
    entry="${entry#"${entry%%[![:space:]]*}"}"   # strip leading whitespace
    [ -n "$entry" ] || continue
    entry="${entry%/}"
    [ "$f" = "$entry" ] && return 0
    case "$f" in "$entry"/*) return 0 ;; esac
  done < "$REVERSE_INDEX_EXEMPT_LIST"
  return 1
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
    _reverse_index_scan_expanded | awk -F'\t' -v b="$base" '$1 == b { print $2 }' | sort -u
  fi
  return 0
}
