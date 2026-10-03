#!/usr/bin/env bash
# cutover.sh [--dry-run] [--push] [--v1 <ref>] [<checkout>]: the one-time v1 -> v2 restructuring of a checkout of branch v2 (06 section 8).
# Tags v1-final at --v1 (default origin/main: the last v1 state; local unless --push), deletes the v1 machinery, moves v2/* (dotfiles
# included) to the root, renames tests_v2 to tests, trims requirements-dev, and records it all as ONE commit. --dry-run only lists.
# Rollback = `git reset --hard v1-final` (plus re-sync of the hub). Exit: 0 done, 1 error, 2 refused (nothing touched).
set -euo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
dry=0; push=0; v1=origin/main; co=.
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1 ;; --push) push=1 ;; --v1) v1="${2:-}"; shift ;;
    -*) usage_exit "usage: cutover.sh [--dry-run] [--push] [--v1 <ref>] [<checkout>]" ;; *) co="$1" ;;
  esac; shift
done
cd "$co" || usage_exit "no such checkout: $co"
git rev-parse --git-dir > /dev/null 2>&1 || usage_exit "$co is not a git repository"
[ -d v2 ] && [ -d tests_v2 ] || usage_exit "no v2/ and tests_v2/ here: not a v2 checkout (already cut over?)"
[ -z "$(git status --porcelain)" ] || usage_exit "the working tree is not clean"
sha="$(git rev-parse -q --verify "$v1^{commit}")" || usage_exit "unknown ref $v1 (pass --v1 <ref>)"
! git rev-parse -q --verify refs/tags/v1-final > /dev/null || usage_exit "tag v1-final exists: already cut over? (rollback: git reset --hard v1-final)"

# What goes: the v1 machinery (06 section 8) plus the v1 files the v2 tree replaces or makes stale. docs/ keeps only the architecture doc.
DELETE="scripts mcp dashboard dist settings tests shared/hooks shared/skills/hub/scripts shared/pyproject.toml shared/ruff.toml CLAUDE.md README.md
  orca.yaml pyproject.toml ruff.toml .test-select-exempt .github/workflows/ci.yml"
for s in source-task next-batch verify-agents verify-rules verify-skills bootstrap-test-suite; do DELETE="$DELETE shared/skills/$s"; done
DELETE="$DELETE $(git ls-files 'shared/*/metadata.yml' | tr '\n' ' ') $(git ls-files docs | grep -vx 'docs/v2/architecture.md' | tr '\n' ' ')"

echo "tag v1-final $sha$([ "$push" = 1 ] && echo ' (pushed to origin)' || echo ' (local)')"
for p in $DELETE; do [ -z "$(git ls-files -- "$p")" ] || echo "delete $p"; done
for e in $(cd v2 && ls -A); do echo "move v2/$e -> $e"; done
echo "move tests_v2 -> tests"; echo "move docs/v2/architecture.md -> docs/architecture.md"
echo "trim requirements-dev.txt to pytest, pytest-xdist, pyyaml; ignore /CLAUDE.md (sync.sh generates it)"
[ "$dry" = 0 ] || exit 0

git tag v1-final "$sha"
for p in $DELETE; do [ -z "$(git ls-files -- "$p")" ] || { git rm -rq -- "$p"; rm -rf -- "$p"; }; done
mvtree() {   # file by file: a destination directory that already exists is merged into, never nested
  local f
  while IFS= read -r f; do mkdir -p "$(dirname "$2/${f#"$1"/}")"; git mv -f "$f" "$2/${f#"$1"/}"; done < <(git ls-files "$1")
  rm -rf "$1"
}
mvtree v2 .; mvtree tests_v2 tests; mvtree docs/v2 docs
grep -E '^(pytest|pytest-xdist|pyyaml)([<>=~!]|$)' requirements-dev.txt > requirements-dev.new; mv requirements-dev.new requirements-dev.txt
grep -qxF /CLAUDE.md .gitignore 2> /dev/null || echo /CLAUDE.md >> .gitignore
git add -A
# Mechanical commit: the v1 hooks of the host checkout (commit-quality...) are for hand-written changes, so they are skipped here only.
git -c core.hooksPath=/dev/null commit -q -m "chore!: v2 cutover: Orca owns execution, ai-toolkit keeps policy plus glue" -m "v1 is tagged v1-final (rollback: git reset --hard v1-final). See docs/architecture.md."
[ "$push" = 0 ] || git push origin refs/tags/v1-final || die "committed, but pushing tag v1-final failed: push it by hand"
echo "cut over: $(git rev-parse --short HEAD). Next: merge into the base branch, sync.sh into itself, install.sh (docs/architecture.md)."
