#!/usr/bin/env bash
# test-select.sh — the FAST tier of the native pre-push gate.
#
# THE MODEL (issue #378, option B): CI is the gate; the local machine only runs
# the fast tier. This hook classifies the diff a push carries and runs the cheap,
# diff-aware tests — mapped/selected files, the control-plane coverage meta-test
# and `pytest --testmon` — and NEVER the whole suite. The full suite runs in CI on
# every branch push; spoke-ready.sh and the land script wait for it to go green.
# Where a diff used to escalate to the full suite (an unmapped change, testmon
# absent, an unresolvable range) the hook now runs the mapped tests that exist and
# prints that CI is the full gate.
#
# WHAT RUNS (over the set of changed files):
#   • every changed file is docs-only (*.md, docs/, LICENSE, *.rst, images)
#     or exempt (repo-root .test-select-exempt)
#       → run NOTHING
#   • a non-python changed file that maps to referencing tests (reverse index,
#     issue #123)
#       → exactly the mapped test files, under `-n auto`
#   • a python change with testmon installed AND a testmon database already present
#       → `pytest --testmon` (never under xdist: testmon is single-writer). A first run
#         without a database would execute the whole suite to seed it, so none starts.
#   • the control-plane coverage meta-test (#123) rides along whenever anything
#     non-doc changed: an unmapped script stays red there until a test names it
#
# FAIL-CLOSED:
#   • no pytest resolvable → block the push (issue #213): a diff that demands
#     tests with no runner can't be proven green — TEST_SELECT_SKIP is the
#     explicit override
#
# INPUT: git feeds a pre-push hook the pushed refs on stdin, one per line:
#   <local ref> <local sha> <remote ref> <remote sha>
# The range tested per ref is `remote_sha..local_sha`; a new branch (all-zero
# remote sha) falls back to merge-base(default-branch, local_sha); a deletion
# (all-zero local sha) contributes nothing.
#
# ENV ESCAPE HATCHES (threaded from the land and ready scripts so this hook stays
# the single executor of local tests):
#   • TEST_SELECT_SKIP non-empty → run nothing
#   • TEST_SELECT_CMD  non-empty → run that command verbatim (the land script's
#     --test-cmd / --local-gate, spoke-ready's --local-gate)
#
# EXIT: the selected suite's exit code IS this script's exit code, so a failing
# suite returns non-zero and aborts the push (the blocking ship gate).
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/utils.sh
source "$HOOK_DIR/lib/utils.sh"

# Reverse index (issue #123): map changed non-python files to the test files
# that reference them, so a shell/config diff runs its mapped tests. Absent in a
# stale installed hook copy (the #45 stale-hook trap) — degrade to no index, which
# the logic below treats as "nothing mapped".
RINDEX=0
if [ -f "$HOOK_DIR/lib/test-reverse-index.sh" ]; then
  # shellcheck source=lib/test-reverse-index.sh
  source "$HOOK_DIR/lib/test-reverse-index.sh"
  RINDEX=1
fi

note() { echo "test-select: $*" >&2; }

is_zero_sha() {
  local sha="$1"
  [ -n "$sha" ] || return 1
  case "$sha" in
    *[!0]*) return 1 ;;  # contains a non-zero char
    *) return 0 ;;       # all zeros
  esac
}

# Defense-in-depth for issue #30: git exports GIT_DIR/GIT_WORK_TREE/etc. into this
# hook's environment. tests/conftest.py strips them before fixtures load, but we
# also drop them for every pytest CHILD here so a test that spawns git before the
# conftest loads (or a repo without that conftest) can't have its throwaway-repo
# operations retargeted at the REAL repo. Scoped to the pytest invocations only —
# this script's own git classification calls below still need GIT_DIR.
# The repo-targeting vars are stripped here; the GIT_CONFIG_* family (KEY/VALUE_n
# pairs that `env -u` can't glob) is the conftest layer's job.
GIT_HOOK_UNSET=(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE \
  -u GIT_OBJECT_DIRECTORY -u GIT_COMMON_DIR -u GIT_NAMESPACE -u GIT_PREFIX \
  -u GIT_CONFIG -u GIT_CONFIG_GLOBAL -u GIT_CONFIG_SYSTEM -u GIT_CONFIG_COUNT)

# Drain git's pre-push stdin up front: the env escape hatches exit early, and an
# unread pipe would hand the caller a SIGPIPE under pipefail.
STDIN="$(cat || true)"

# Tripwire scope (issue #188): the integrity snapshot guards ONLY the refs this
# push updates — the local refs git names on the pre-push stdin — plus HEAD and
# the config markers. Any other ref moving mid-gate is concurrent-spoke behavior
# in the shared ref store (a sibling's commit, rewind, marker tag, or completing
# push), not a breach — the whole-namespace snapshot both false-aborted those
# pushes and rolled sibling refs back on restore (#135, #188). A ref deletion
# carries no local ref and a raw-sha local side is not a refname, so neither
# contributes; an empty scope degrades to the whole-repo tripwire.
PUSH_SCOPE=""
while read -r _lref lsha _rref _rsha; do
  [ -n "${lsha:-}" ] || continue
  if is_zero_sha "$lsha"; then continue; fi
  case "$_lref" in
    refs/*) PUSH_SCOPE+="${_lref}"$'\n' ;;
  esac
done <<< "$STDIN"

# ── Persistent per-project config (issue #334) ──────────────────────────────────
# The durable equivalent of the one-shot TEST_SELECT_SKIP / TEST_SELECT_CMD env vars,
# read from git config (ai-toolkit.hook.test-select.*) so a host is not limited to a
# per-invocation env var no re-sync preserves. A LIVE env var still WINS (only an
# UNSET one is filled); an unconfigured repo keeps today's tiered behavior. Guarded on
# the resolver's presence so a stale install predating #334 is unaffected.
if command -v ai_toolkit_hook_enabled >/dev/null 2>&1; then
  # Per-hook kill switch: a disabled test-select gate runs nothing (like the skip
  # hatch) rather than blocking the push.
  if ! ai_toolkit_hook_enabled test-select .; then
    note "test-select disabled by per-project config (ai-toolkit.hook.test-select.enabled) — skipping tests"
    exit 0
  fi
  if [ -z "${TEST_SELECT_SKIP:-}" ] && ai_toolkit_hook_test_select_skip .; then
    TEST_SELECT_SKIP=1
  fi
  if [ -z "${TEST_SELECT_CMD:-}" ]; then
    _persist_cmd="$(ai_toolkit_hook_test_select_command .)"
    [ -n "$_persist_cmd" ] && TEST_SELECT_CMD="$_persist_cmd"
  fi
fi

# ── Env escape hatches (the land script's --skip-tests / --test-cmd / --local-gate) ─
if [ -n "${TEST_SELECT_SKIP:-}" ]; then
  note "TEST_SELECT_SKIP set — skipping tests"
  exit 0
fi
if [ -n "${TEST_SELECT_CMD:-}" ]; then
  note "running custom suite (TEST_SELECT_CMD)"
  rc=0
  # The custom suite is a test command too — run it under the same git-hook env
  # strip so it can't reach the real repo either, and under the repo-integrity
  # tripwire (issue #31) so an escape still aborts.
  run_under_tripwire_scoped "$PUSH_SCOPE" "${GIT_HOOK_UNSET[@]}" bash -c "$TEST_SELECT_CMD" || rc=$?
  exit "$rc"
fi

# ── Classification helpers ──────────────────────────────────────────────────────
is_doc() {
  case "$1" in
    # A script/config suffix is never docs, wherever it lives: */docs/* must
    # not swallow a control-plane script the coverage meta-test claims (#123).
    *.sh|*.yml|*.yaml) return 1 ;;
    *.md|*.rst) return 0 ;;
    docs/*|*/docs/*) return 0 ;;
    LICENSE|*/LICENSE) return 0 ;;
    *.png|*.jpg|*.jpeg|*.gif|*.svg|*.webp|*.ico) return 0 ;;
    *) return 1 ;;
  esac
}

is_py() { case "$1" in *.py) return 0 ;; *) return 1 ;; esac; }

is_shell() { case "$1" in *.sh) return 0 ;; *) return 1 ;; esac; }

# Exempt handling (issue #123): the parser lives in lib/test-reverse-index.sh
# (reverse_index_is_exempt) so this gate and the commit-time nudge share one
# definition of "exempt". Without the lib (a stale installed hook) there are no
# exemptions — conservative, like the index.
if [ "$RINDEX" = "1" ]; then
  is_exempt() { reverse_index_is_exempt "$1"; }
else
  is_exempt() { return 1; }
fi

# The control-plane coverage meta-test (issue #123): a milliseconds static
# scan asserting every control-plane script has a referencing test or an
# exemption. It rides every push that changes non-doc files, so an unmapped
# script can never ship green: the meta-test is red until a test references it.
# Guarded on file existence: synced repos without the file are unaffected.
META_TEST_FILE="tests/unit/test_test_reverse_index.py"
META_TEST_NODE="$META_TEST_FILE::TestControlPlaneCoverage"

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
SAW_TAG_REF=0
SAW_NONTAG_REF=0
DEFAULT="$(default_branch)"
while read -r _lref lsha _rref rsha; do
  [ -n "${lsha:-}" ] || continue
  if is_zero_sha "$lsha"; then continue; fi   # deleting a ref — nothing added
  # Track ref kind for the tag-only short-circuit below: a marker (ready/N,
  # gate/N) is pushed as a refs/tags/* ref and carries no new code (#45).
  case "$_lref" in
    refs/tags/*) SAW_TAG_REF=1 ;;
    *)           SAW_NONTAG_REF=1 ;;
  esac
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
    CANNOT_PROVE=1   # range unresolved → nothing provable locally; CI is the gate
  fi
done <<< "$STDIN"

# ── Tag-only marker push: carries no code, skip the suite (issue #45) ────────────
# A push whose every ref is refs/tags/* (no branch refs) only moves a pointer —
# the shipped unit is the branch push, which runs the fast tier on its own. So
# emitting a marker (ready/N completion, gate/N PLAN-gate park) never re-runs the
# tests for a tag that introduces nothing. Guards a hand-typed
# `git push origin <tag>` too, not just spoke-ready.sh.
if [ "$SAW_TAG_REF" = "1" ] && [ "$SAW_NONTAG_REF" = "0" ]; then
  note "tag-only push (no branch refs) — marker carries no code, skipping suite"
  exit 0
fi

# ── Classify the diff ───────────────────────────────────────────────────────────
# A *.py file is code even when it lives under docs/ (e.g. docs/conf.py): classify
# it python so testmon can judge its impact, rather than skipping it as a doc.
has_py=0
has_other=0
UNMAPPED_FILE=""
UNMAPPED_SH=""
MAPPED_TESTS=""
EXEMPT_SKIPPED=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  if is_py "$f"; then has_py=1; continue; fi
  if is_doc "$f"; then continue; fi
  # Reverse-index lookup FIRST, exemption second (B-review hardening): an
  # exempt entry can only mute the warning for a file the index cannot map —
  # it can never hide existing mapped coverage.
  mapped=""
  if [ "$RINDEX" = "1" ]; then
    mapped="$(reverse_index_tests_for "$f")"
  fi
  if [ -n "$mapped" ]; then
    has_other=1
    MAPPED_TESTS="$MAPPED_TESTS$mapped"$'\n'
  elif is_exempt "$f"; then
    EXEMPT_SKIPPED=$((EXEMPT_SKIPPED + 1))
  else
    has_other=1
    [ -n "$UNMAPPED_FILE" ] || UNMAPPED_FILE="$f"
    # Witness signal (issue #191): a changed *.sh with no referencing test is
    # the testmon blind spot — testmon tracks python imports only, so a bash-only
    # edit re-exercised by subprocess/source is invisible to it. Collect each such
    # script for a distinct, greppable warning (fed to #187's fail-open audit as a
    # witness); non-shell unmapped files don't qualify.
    if is_shell "$f"; then
      UNMAPPED_SH="$UNMAPPED_SH$f"$'\n'
    fi
  fi
done <<< "$FILES"

# Emit the bash blind-spot witness (issue #191). Dedup with sort -u so a script
# carried by two pushed refs — the same path twice in FILES — is named once, not
# doubled into the #187 audit stream.
while IFS= read -r f; do
  [ -n "$f" ] || continue
  note "WARNING (witness: unmapped-shell) $f — shell change with no referencing test; testmon is blind to shell, nothing re-exercises it. Add a test referencing its basename or a .test-select-exempt entry."
done <<< "$(printf '%s' "$UNMAPPED_SH" | sort -u)"

if [ "$has_py" = "0" ] && [ "$has_other" = "0" ] && [ "$CANNOT_PROVE" = "0" ]; then
  if [ "$EXEMPT_SKIPPED" -gt 0 ]; then
    note "docs/exempt-only diff ($EXEMPT_SKIPPED exempt file(s), .test-select-exempt) — no tests to run"
  else
    note "docs-only (or empty) diff — no tests to run"
  fi
  exit 0
fi

# ── The fast selection: mapped test files that still exist + the meta-test ──────
# A mapping to a vanished test proves nothing, so it is dropped rather than run.
SEL_ARR=()
MAPPED_FILES=()
while IFS= read -r t; do
  [ -n "$t" ] || continue
  if [ -f "$t" ]; then
    SEL_ARR+=("$t"); MAPPED_FILES+=("$t")
  else
    note "mapped test $t is missing — skipped"
  fi
done <<< "$(printf '%s' "$MAPPED_TESTS" | sort -u)"
if [ -f "$META_TEST_FILE" ]; then
  SEL_ARR+=("$META_TEST_NODE")
fi

# CI is the full gate: say so wherever the old gate would have escalated.
[ "$CANNOT_PROVE" = "0" ] || note "diff range unresolved — running the mapped tests that exist; CI is the full gate"
[ -z "$UNMAPPED_FILE" ] || note "unmapped non-exempt change ($UNMAPPED_FILE) — no mapped tests to run; CI is the full gate. Add a test referencing it or a .test-select-exempt entry."

# ── A runner is required for anything that runs; none → fail closed (issue #213) ─
# This diff demands tests but no pytest resolves, so the tree CANNOT be proven
# green locally. Fail closed rather than shipping an untested diff on a silent
# pass. TEST_SELECT_SKIP (handled above) stays the explicit override.
RUNNER="$(detect_pytest "." || true)"
if [ -z "$RUNNER" ]; then
  note "no pytest available but this diff demands tests — cannot prove the tree green; blocking the push. Install pytest (pip install -r requirements-dev.txt) or set TEST_SELECT_SKIP=1 to override deliberately."
  exit 1
fi
read -r -a RUNNER_ARR <<< "$RUNNER"

# Probe the runner's `--help` for a plugin flag. Capture the help text rather than
# piping into grep: under pipefail an early -q match would SIGPIPE the (longer, real)
# pytest --help into a non-zero exit and falsely report the plugin absent.
runner_has() {
  local help=""
  help="$("${RUNNER_ARR[@]}" --help 2>/dev/null || true)"
  case "$help" in
    *"$1"*) return 0 ;;
    *) return 1 ;;
  esac
}

# pytest-xdist on the mapped leg (issue #276): embarrassingly parallel, so run it under
# `-n auto`. NEVER spliced into the `--testmon` leg: testmon serializes a single-writer DB.
# Guarded on the plugin being present — a venv without pytest-xdist degrades to
# single-process rather than erroring the push ("unrecognized -n").
XDIST=()
if runner_has "-n numprocesses" || runner_has "--numprocesses"; then
  XDIST=(-n auto)
fi

# Every leg runs pytest under the repo-integrity tripwire (issue #31), scoped to the
# refs this push updates (PUSH_SCOPE, issue #188): a test that escapes isolation and
# mutates what this push ships aborts the push (the snapshot is restored without ever
# rewinding a ref that only gained commits; issue #135) instead of corrupting it.
rc=0
if [ "${#SEL_ARR[@]}" -gt 0 ]; then
  note "selected tests: ${SEL_ARR[*]} (parallel: ${XDIST[*]:-off})"
  run_under_tripwire_scoped "$PUSH_SCOPE" "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" ${XDIST[@]+"${XDIST[@]}"} "${SEL_ARR[@]}" || rc=$?
fi

if [ "$has_py" = "1" ]; then
  if ! runner_has "--testmon"; then
    note "python change but testmon not installed — only the selected tests ran; CI is the full gate"
  elif [ ! -f "${TESTMON_DATAFILE:-.testmondata}" ]; then
    # A first testmon run in a tree without a database executes the WHOLE suite to build
    # it — exactly what this tier must never do. The seeded baseline worktree-new copies
    # in is what makes testmon incremental here; without it, leave the seeding to CI.
    note "python change but no testmon database (${TESTMON_DATAFILE:-.testmondata}) — a first run would execute the whole suite, so none is started; CI is the full gate"
  else
    # Dedup (issue #270): the selected files ALREADY ran, in full, above. --ignore each
    # (and the meta file) so testmon collects everything EXCEPT them: any impacted test
    # inside a selected file already ran, so nothing is missed and nothing double-runs.
    # (--ignore, not an explicit node list, so testmon can never deselect a mapped node.)
    IGNORE_ARR=()
    for t in ${MAPPED_FILES[@]+"${MAPPED_FILES[@]}"}; do IGNORE_ARR+=("--ignore=$t"); done
    [ ! -f "$META_TEST_FILE" ] || IGNORE_ARR+=("--ignore=$META_TEST_FILE")
    note "python change — pytest --testmon (selected files ran above; --ignore'd so testmon can't double-run them)"
    rc2=0
    run_under_tripwire_scoped "$PUSH_SCOPE" "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" --testmon ${IGNORE_ARR[@]+"${IGNORE_ARR[@]}"} || rc2=$?
    # Exit 5 = "no tests collected": when the selected files ARE testmon's whole impact
    # set, --ignore leaves it nothing to run — a GREEN outcome, never a block.
    [ "$rc2" != "5" ] || rc2=0
    [ "$rc" -ne 0 ] || rc=$rc2
  fi
fi
exit "$rc"
