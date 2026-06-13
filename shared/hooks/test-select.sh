#!/usr/bin/env bash
# test-select.sh — tiered, diff-aware test selector for the native pre-push hook.
#
# THE MODEL (issue #19): the pre-push hook is the SINGLE owner of test execution.
# "One push = one run." This script classifies the diff a push carries and runs
# the cheapest sufficient suite, with DEFAULT-TO-FULL safety — anything not
# provably docs-only or python-only runs the full suite, and a missing testmon
# falls back to the full suite rather than silently skipping python tests.
#
# TIERS (over the set of changed files):
#   • every changed file is docs-only (*.md, docs/, LICENSE, *.rst, images)
#       → run NOTHING
#   • every non-doc changed file is *.py
#       → pytest --testmon   (coverage-based test-impact analysis)
#   • anything else (.sh, Dockerfile, *.yml, Makefile, unrecognized)
#       → the FULL suite
#
# SAFE FALLBACKS:
#   • testmon not installed  → full suite (never silently skip python tests)
#   • no pytest resolvable   → nothing to run (degrade, don't error the push)
#   • a diff range that can't be resolved → full suite (can't prove safe)
#
# INPUT: git feeds a pre-push hook the pushed refs on stdin, one per line:
#   <local ref> <local sha> <remote ref> <remote sha>
# The range tested per ref is `remote_sha..local_sha`; a new branch (all-zero
# remote sha) falls back to merge-base(default-branch, local_sha); a deletion
# (all-zero local sha) contributes nothing.
#
# ENV ESCAPE HATCHES (threaded from worktree-land.sh so the hook stays the single
# executor — keeps land's --skip-tests / --test-cmd working without a land-side
# run):
#   • TEST_SELECT_SKIP non-empty → run nothing (the --skip-tests path)
#   • TEST_SELECT_CMD  non-empty → run that command verbatim (the --test-cmd path)
#
# EXIT: the selected suite's exit code IS this script's exit code, so a failing
# suite returns non-zero and aborts the push (the blocking ship gate).
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/utils.sh
source "$HOOK_DIR/lib/utils.sh"

note() { echo "test-select: $*" >&2; }

# Drain git's pre-push stdin up front: the env escape hatches exit early, and an
# unread pipe would hand the caller a SIGPIPE under pipefail.
STDIN="$(cat || true)"

# ── Env escape hatches (worktree-land's --skip-tests / --test-cmd) ──────────────
if [ -n "${TEST_SELECT_SKIP:-}" ]; then
  note "TEST_SELECT_SKIP set — skipping tests"
  exit 0
fi
if [ -n "${TEST_SELECT_CMD:-}" ]; then
  note "running custom suite (TEST_SELECT_CMD)"
  rc=0
  bash -c "$TEST_SELECT_CMD" || rc=$?
  exit "$rc"
fi

# ── Classification helpers ──────────────────────────────────────────────────────
is_doc() {
  case "$1" in
    *.md|*.rst) return 0 ;;
    docs/*|*/docs/*) return 0 ;;
    LICENSE|*/LICENSE) return 0 ;;
    *.png|*.jpg|*.jpeg|*.gif|*.svg|*.webp|*.ico) return 0 ;;
    *) return 1 ;;
  esac
}

is_py() { case "$1" in *.py) return 0 ;; *) return 1 ;; esac; }

is_zero_sha() {
  local sha="$1"
  [ -n "$sha" ] || return 1
  case "$sha" in
    *[!0]*) return 1 ;;  # contains a non-zero char
    *) return 0 ;;       # all zeros
  esac
}

# Default branch for the new-branch merge-base fallback: origin/HEAD, else the
# conventional main/master, else main.
default_branch() {
  local def=""
  def=$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||') \
    || def=""
  if [ -n "$def" ]; then printf '%s' "$def"; return 0; fi
  for def in main master; do
    if git show-ref --verify --quiet "refs/heads/$def" 2>/dev/null; then
      printf '%s' "$def"
      return 0
    fi
  done
  printf 'main'
}

# ── Resolve the changed files across every pushed ref ───────────────────────────
CANNOT_PROVE=0
FILES=""
DEFAULT="$(default_branch)"
while read -r _lref lsha _rref rsha; do
  [ -n "${lsha:-}" ] || continue
  if is_zero_sha "$lsha"; then continue; fi   # deleting a ref — nothing added
  if is_zero_sha "${rsha:-0}"; then
    base="$(git merge-base "$DEFAULT" "$lsha" 2>/dev/null || true)"
    [ -n "$base" ] || { CANNOT_PROVE=1; continue; }
    range="$base..$lsha"
  else
    range="$rsha..$lsha"
  fi
  if changed="$(git diff --name-only "$range" 2>/dev/null)"; then
    FILES="$FILES$changed"$'\n'
  else
    CANNOT_PROVE=1   # range unresolved → can't prove safe
  fi
done <<< "$STDIN"

# ── Decide the tier ─────────────────────────────────────────────────────────────
# A *.py file is code even when it lives under docs/ (e.g. docs/conf.py): classify
# it python so testmon can judge its impact, rather than skipping it as a doc.
has_py=0
has_other=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  if is_py "$f"; then has_py=1; continue; fi
  if is_doc "$f"; then continue; fi
  has_other=1
done <<< "$FILES"

if [ "$CANNOT_PROVE" = "1" ]; then
  DECISION=FULL
elif [ "$has_other" = "1" ]; then
  DECISION=FULL
elif [ "$has_py" = "1" ]; then
  DECISION=PYTHON
else
  DECISION=NOTHING
fi

if [ "$DECISION" = "NOTHING" ]; then
  note "docs-only (or empty) diff — no tests to run"
  exit 0
fi

# ── A runner is required for PYTHON/FULL; none → nothing to run (safe fallback) ──
RUNNER="$(detect_pytest "." || true)"
if [ -z "$RUNNER" ]; then
  note "no pytest available — nothing to run"
  exit 0
fi
read -r -a RUNNER_ARR <<< "$RUNNER"

# Defense-in-depth for issue #30: git exports GIT_DIR/GIT_WORK_TREE/etc. into this
# hook's environment. tests/conftest.py strips them before fixtures load, but we
# also drop them for the pytest CHILD here so a test that spawns git before the
# conftest loads (or a repo without that conftest) can't have its throwaway-repo
# operations retargeted at the REAL repo. Scoped to the pytest invocation only —
# this script's own git classification calls above still need GIT_DIR.
GIT_HOOK_UNSET=(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE \
  -u GIT_OBJECT_DIRECTORY -u GIT_COMMON_DIR -u GIT_NAMESPACE -u GIT_PREFIX)

runner_has_testmon() {
  # testmon advertises --testmon in `pytest --help` when its plugin is installed.
  # Capture the help text rather than piping into grep: under pipefail an early
  # -q match would SIGPIPE the (longer, real) pytest --help into a non-zero exit
  # and falsely report testmon absent.
  local help=""
  help="$("${RUNNER_ARR[@]}" --help 2>/dev/null || true)"
  case "$help" in
    *--testmon*) return 0 ;;
    *) return 1 ;;
  esac
}

rc=0
case "$DECISION" in
  PYTHON)
    if runner_has_testmon; then
      note "python-only diff — pytest --testmon"
      "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" --testmon || rc=$?
    else
      note "python-only diff but testmon not installed — full suite"
      "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" || rc=$?
    fi
    ;;
  FULL)
    note "non-python or unrecognized changes — full suite"
    "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" || rc=$?
    ;;
esac
exit "$rc"
