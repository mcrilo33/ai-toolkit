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
#     or exempt (repo-root .test-select-exempt)
#       → run NOTHING
#   • every non-doc/non-exempt changed file is *.py
#       → pytest --testmon (+ the control-plane coverage meta-test, #123)
#   • every non-python changed file maps to referencing tests (reverse index,
#     issue #123)
#       → run exactly the mapped test files (+ meta-test; + --testmon when
#         the diff also touches python)
#   • anything else (an unmapped, non-exempt file)
#       → the FULL suite (which contains the meta-test natively)
#
# SAFE FALLBACKS (all fail CLOSED — a demand we can't prove blocks, never waves
# through):
#   • testmon not installed  → full suite (never silently skip python tests)
#   • no pytest resolvable    → block the push (issue #213): a docs-only/empty
#     diff needs no runner and exits 0, but a diff that demands tests with no
#     runner can't be proven green, so it fails closed (nonzero) rather than
#     shipping untested — TEST_SELECT_SKIP is the explicit override
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
# GREEN-TREE STAMPS (issue #122): a gate pass stamps the tier that ran, keyed by
# `git rev-parse HEAD^{tree}` under <git-common-dir>/.gate-stamps/ (see
# lib/gate-stamp.sh). A later gate on the SAME clean tree with an equal-or-weaker
# demand skips the suite with a loud note — distinct from TEST_SELECT_SKIP; a
# stronger demand runs and upgrades the stamp. A dirty working tree, either
# escape hatch above, a failing run, or a runner-fingerprint mismatch never
# consumes or mints.
#
# EXIT: the selected suite's exit code IS this script's exit code, so a failing
# suite returns non-zero and aborts the push (the blocking ship gate).
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/utils.sh
source "$HOOK_DIR/lib/utils.sh"

# Green-tree stamps (issue #122): skip a suite already proven on this exact
# tree. The lib may be absent in an installed hook copy that predates it (the
# #45 stale-hook trap) — degrade to no stamps rather than breaking the gate.
STAMPS=0
if [ -f "$HOOK_DIR/lib/gate-stamp.sh" ]; then
  # shellcheck source=lib/gate-stamp.sh
  source "$HOOK_DIR/lib/gate-stamp.sh"
  STAMPS=1
fi

# Reverse index (issue #123): map changed non-python files to the test files
# that reference them, so a shell/config diff runs its mapped tests instead of
# the full suite. Absent in a stale installed hook copy (the same #45 trap as
# above) — degrade to no index, which the tier logic below treats as "nothing
# mapped", i.e. today's full-suite escalation.
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

# ── Env escape hatches (worktree-land's --skip-tests / --test-cmd) ──────────────
if [ -n "${TEST_SELECT_SKIP:-}" ]; then
  note "TEST_SELECT_SKIP set — skipping tests"
  exit 0
fi
if [ -n "${TEST_SELECT_CMD:-}" ]; then
  note "running custom suite (TEST_SELECT_CMD)"
  rc=0
  # The custom suite is a test command too (worktree-land --test-cmd) — run it
  # under the same git-hook env strip so it can't reach the real repo either, and
  # under the repo-integrity tripwire (issue #31) so an escape still aborts.
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
# definition of "exempt". Note the list's own basename (.test-select-exempt)
# can never be a filename-shaped token, so editing it is unmapped by
# construction and escalates to the full suite: high-stakes changes to what
# the gate ignores always pay the maximum price. Without the lib (a stale
# installed hook) there are no exemptions — conservative, like the index.
if [ "$RINDEX" = "1" ]; then
  is_exempt() { reverse_index_is_exempt "$1"; }
else
  is_exempt() { return 1; }
fi

# The control-plane coverage meta-test (issue #123): a milliseconds static
# scan asserting every control-plane script has a referencing test or an
# exemption. It rides every tier that runs pytest — SELECTED appends it to
# the selection, the testmon tier adds a separate invocation, and the full
# suite contains it natively — so an unmapped script can never land: its push
# escalates to FULL and the meta-test there is red until a test references
# it. Guarded on file existence: synced repos without the file are unaffected.
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
    CANNOT_PROVE=1   # range unresolved → can't prove safe
  fi
done <<< "$STDIN"

# ── Tag-only marker push: carries no code, skip the suite (issue #45) ────────────
# A push whose every ref is refs/tags/* (no branch refs) only moves a pointer —
# the shipped unit is the branch push, which runs the suite on its own. So
# emitting a marker (ready/N completion, gate/N PLAN-gate park) never re-runs the
# ~5-min suite for a tag that introduces nothing. Guards a hand-typed
# `git push origin <tag>` too, not just spoke-ready.sh.
if [ "$SAW_TAG_REF" = "1" ] && [ "$SAW_NONTAG_REF" = "0" ]; then
  note "tag-only push (no branch refs) — marker carries no code, skipping suite"
  exit 0
fi

# ── Decide the tier ─────────────────────────────────────────────────────────────
# A *.py file is code even when it lives under docs/ (e.g. docs/conf.py): classify
# it python so testmon can judge its impact, rather than skipping it as a doc.
has_py=0
has_other=0
UNMAPPED=0
UNMAPPED_FILE=""
UNMAPPED_SH=""
MAPPED_TESTS=""
EXEMPT_SKIPPED=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  if is_py "$f"; then has_py=1; continue; fi
  if is_doc "$f"; then continue; fi
  # Reverse-index lookup FIRST, exemption second (B-review hardening): an
  # exempt entry can only mute the escalation of a file the index cannot map —
  # it can never hide existing mapped coverage. Every mapped non-python file
  # adds its referencing tests to the selection; a single unmapped, non-exempt
  # file escalates the whole push to the full suite (default-to-full).
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
    UNMAPPED=1
    [ -n "$UNMAPPED_FILE" ] || UNMAPPED_FILE="$f"
    # Witness signal (issue #191): a changed *.sh with no referencing test is
    # the testmon blind spot this gate exists to close — testmon tracks python
    # imports only, so a bash-only edit re-exercised by subprocess/source
    # (test_hub_afk and friends) is invisible to it. The unmapped file already
    # escalates to the full suite below, but the full suite has no
    # subprocess-sourcing suite for THIS script either, so it stays untested.
    # Collect each such script for a distinct, greppable warning (fed to #187's
    # fail-open audit as a witness); non-shell unmapped files don't qualify.
    if is_shell "$f"; then
      UNMAPPED_SH="$UNMAPPED_SH$f"$'\n'
    fi
  fi
done <<< "$FILES"

# Emit the bash blind-spot witness (issue #191) before the tier decision so it
# surfaces even when a green-tree stamp later short-circuits the run: the
# coverage gap it names is a property of the diff, not of whether the suite ran.
# Dedup with sort -u (as MAPPED_TESTS is above) so a script carried by two
# pushed refs — the same path twice in FILES — is named once, not doubled into
# the #187 audit stream.
while IFS= read -r f; do
  [ -n "$f" ] || continue
  note "WARNING (witness: unmapped-shell) $f — shell change with no referencing test; testmon is blind to shell, nothing re-exercises it. Add a test referencing its basename or a .test-select-exempt entry."
done <<< "$(printf '%s' "$UNMAPPED_SH" | sort -u)"

# The selection: deduped, and every entry must still exist — a mapping to a
# vanished test proves nothing, so it escalates instead.
SELECTED_TESTS=""
if [ "$has_other" = "1" ] && [ "$UNMAPPED" = "0" ]; then
  SELECTED_TESTS="$(printf '%s' "$MAPPED_TESTS" | sort -u)"
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    if [ ! -f "$t" ]; then
      UNMAPPED=1
      UNMAPPED_FILE="$t (mapped test missing)"
    fi
  done <<< "$SELECTED_TESTS"
fi

if [ "$CANNOT_PROVE" = "1" ]; then
  DECISION=FULL
elif [ "$has_other" = "1" ] && [ "$UNMAPPED" = "1" ]; then
  DECISION=FULL
elif [ "$has_other" = "1" ]; then
  DECISION=SELECTED
elif [ "$has_py" = "1" ]; then
  DECISION=PYTHON
else
  DECISION=NOTHING
fi

if [ "$DECISION" = "NOTHING" ]; then
  if [ "$EXEMPT_SKIPPED" -gt 0 ]; then
    note "docs/exempt-only diff ($EXEMPT_SKIPPED exempt file(s), .test-select-exempt) — no tests to run"
  else
    note "docs-only (or empty) diff — no tests to run"
  fi
  exit 0
fi

# ── A runner is required for PYTHON/SELECTED/FULL; none → fail closed (issue #213) ─
# By here DECISION is PYTHON, SELECTED, or FULL — the NOTHING (docs-only/empty)
# tier already exited 0 above, needing no runner. This diff demands tests but no
# pytest resolves, so the tree CANNOT be proven green. Exiting 0 here would ship
# an untested python diff on a silent pass — and mint no green-tree stamp, so the
# post-land sweep never fires either. Fail closed instead, exactly as the bad-sha
# path escalates rather than waving a diff through. TEST_SELECT_SKIP (handled
# above) stays the explicit override for a runner-less checkout.
RUNNER="$(detect_pytest "." || true)"
if [ -z "$RUNNER" ]; then
  note "no pytest available but this $DECISION diff demands tests — cannot prove the tree green; blocking the push. Install pytest (pip install -r requirements-dev.txt) or set TEST_SELECT_SKIP=1 to override deliberately."
  exit 1
fi
read -r -a RUNNER_ARR <<< "$RUNNER"

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

# A mixed SELECTED diff needs testmon for its python part; without testmon the
# python part demands the full suite (the same fallback as the PYTHON tier).
if [ "$DECISION" = "SELECTED" ] && [ "$has_py" = "1" ] && ! runner_has_testmon; then
  note "mixed diff but testmon not installed — full suite"
  DECISION=FULL
fi

# ── Green-tree stamp consult (issue #122; set-aware selected tier #123-D) ───────
# TIER_TO_RUN names the suite this gate is about to execute — that is both the
# demand a stamp must cover and the tier a passing run proves. A python diff
# without testmon runs (and therefore proves) the FULL suite. A SELECTED run's
# proof is the exact set that ran (comma-joined), plus testmon for a mixed
# diff — both travel with the demand and the mint.
SET_CSV=""
if [ "$DECISION" = "SELECTED" ]; then
  TIER_TO_RUN=selected
  SET_CSV="$(printf '%s' "$SELECTED_TESTS" | tr '\n' ',')"
  SET_CSV="${SET_CSV%,}"
elif [ "$DECISION" = "PYTHON" ] && runner_has_testmon; then
  TIER_TO_RUN=testmon
else
  TIER_TO_RUN=full
fi

# The stamp env fingerprint is the resolved runner's own --version line (this
# repo has been bitten by `pytest`-on-PATH and `python3.12 -m pytest` being
# different interpreters). Probing INVOKES the runner outside the tripwire
# bracket below, so it is deferred to the two spots that truly need it: a
# candidate stamp to consume (pre-run) or a green run to mint (post-run).
runner_fingerprint() { "${RUNNER_ARR[@]}" --version 2>/dev/null | head -n 1 || true; }

# Consume: skip only when a stamp at least as strong as TIER_TO_RUN exists for
# the CURRENT clean tree under the SAME runner fingerprint. A dirty working
# tree yields no key (the suite would run against a tree other than HEAD's), so
# it neither consumes here nor mints below — logged distinctly, exactly like
# the stamp skip itself is distinct from the TEST_SELECT_SKIP hatch.
STAMP_TREE=""
STAMP_ENV=""
STAMP_ENV_PROBED=0
if [ "$STAMPS" = "1" ]; then
  if STAMP_TREE="$(gate_stamp_tree)"; then
    if gate_stamp_has "$STAMP_TREE"; then
      STAMP_ENV="$(runner_fingerprint)"
      STAMP_ENV_PROBED=1
      # The two trailing args matter only for a selected demand (the set and
      # whether the diff is mixed); full/testmon demands ignore them. An
      # installed stamp lib predating #123-D also just ignores them.
      if gate_stamp_check "$STAMP_TREE" "$TIER_TO_RUN" "$STAMP_ENV" "$SET_CSV" "$has_py"; then
        if [ "$TIER_TO_RUN" = "selected" ]; then
          note "green-tree stamp covers tree $STAMP_TREE (this selection already proven for this env) — skipping suite"
        else
          note "green-tree stamp covers tree $STAMP_TREE (proven ≥ $TIER_TO_RUN for this env) — skipping suite"
        fi
        exit 0
      fi
    fi
  else
    STAMP_TREE=""
    note "green-tree stamp: working tree dirty — no stamp consult or mint this run (suite runs as usual)"
  fi
fi

# Every tier runs pytest under the repo-integrity tripwire (issue #31), scoped
# to the refs this push updates (PUSH_SCOPE, issue #188): the run is bracketed
# by a snapshot/verify of the pushed ref tips + HEAD + core.bare/worktree, so a
# test that escapes isolation and mutates what this push ships aborts the push
# (and the snapshot is restored — without ever rewinding a ref that only gained
# commits; issue #135) instead of corrupting it silently. Refs outside the push
# are concurrent-spoke territory (sibling commits, marker tags, completing
# pushes) and are neither checked nor restored (#135, #188). Only
# TEST_SELECT_SKIP (handled above) bypasses the gate.
rc=0
case "$DECISION" in
  PYTHON)
    if [ "$TIER_TO_RUN" = "testmon" ]; then
      note "python-only diff — pytest --testmon"
      run_under_tripwire_scoped "$PUSH_SCOPE" "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" --testmon || rc=$?
      # The meta-test rides along as its own invocation: mixing an explicit
      # node id into --testmon would let testmon deselect it.
      if [ -f "$META_TEST_FILE" ]; then
        note "control-plane coverage meta-test"
        rc2=0
        run_under_tripwire_scoped "$PUSH_SCOPE" "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" "$META_TEST_NODE" || rc2=$?
        [ "$rc" -ne 0 ] || rc=$rc2
      fi
    else
      note "python-only diff but testmon not installed — full suite"
      run_under_tripwire_scoped "$PUSH_SCOPE" "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" || rc=$?
    fi
    ;;
  SELECTED)
    SEL_ARR=()
    while IFS= read -r t; do
      [ -n "$t" ] && SEL_ARR+=("$t")
    done <<< "$SELECTED_TESTS"
    if [ -f "$META_TEST_FILE" ]; then
      SEL_ARR+=("$META_TEST_NODE")
    fi
    note "mapped diff — selected test files: ${SEL_ARR[*]}"
    run_under_tripwire_scoped "$PUSH_SCOPE" "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" "${SEL_ARR[@]}" || rc=$?
    if [ "$has_py" = "1" ]; then
      note "mixed diff — pytest --testmon for the python part"
      rc2=0
      run_under_tripwire_scoped "$PUSH_SCOPE" "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" --testmon || rc2=$?
      [ "$rc" -ne 0 ] || rc=$rc2
    fi
    ;;
  FULL)
    if [ -n "$UNMAPPED_FILE" ]; then
      note "unmapped non-exempt change ($UNMAPPED_FILE) — full suite; add a test referencing it or a .test-select-exempt entry"
    else
      note "non-python or unrecognized changes — full suite"
    fi
    run_under_tripwire_scoped "$PUSH_SCOPE" "${GIT_HOOK_UNSET[@]}" "${RUNNER_ARR[@]}" || rc=$?
    ;;
esac

# Mint: record only what a green run just proved (only the gate mints — the
# scripted control plane). The stamp is a cache, never a gate: a failed write
# must not block a legitimately green push.
if [ "$rc" -eq 0 ] && [ "$STAMPS" = "1" ] && [ -n "$STAMP_TREE" ]; then
  if [ "$STAMP_ENV_PROBED" = "0" ]; then
    STAMP_ENV="$(runner_fingerprint)"
  fi
  # A selected mint records the set that ran; MINT_TESTMON=1 marks a mixed
  # green run whose --testmon leg also passed (both legs share rc).
  MINT_TESTMON=""
  if [ "$DECISION" = "SELECTED" ] && [ "$has_py" = "1" ]; then
    MINT_TESTMON=1
  fi
  gate_stamp_mint "$STAMP_TREE" "$TIER_TO_RUN" "$STAMP_ENV" "$SET_CSV" "$MINT_TESTMON" \
    || note "green-tree stamp: mint failed (non-fatal — next gate re-runs the suite)"
fi
exit "$rc"
