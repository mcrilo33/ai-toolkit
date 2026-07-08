#!/usr/bin/env bash
# anti-gutting-scan.sh — mechanical tripwire against an implementation that GUTS
# the tests to go green.
#
# A deterministic scan of the pushed diff for test-gutting signatures, with
# enforcement split by CHANNEL (#193):
#
#   * ATTENDED it is advisory everywhere — findings print to stderr, exit 0 — so a
#     human's ordinary test edit is never gated.
#   * UNATTENDED (/afk armed — the UNATTENDED env set truthy, or the supervisor's
#     `ai-toolkit-afk/unattended` marker (#74) under the git common dir every spoke
#     worktree shares) it fails CLOSED on the SHIP paths: a finding on a branch ref
#     or `refs/tags/ready/*` blocks the push, because no human is watching for a
#     test-gutting diff. Deliberately NOT read: hub-afk's `.afk-state` window file —
#     it conflates "a drain is armed" with "THIS push is unattended", so a stale or
#     live drain window would hard-block the hub operator's own attended pushes
#     with no bypass. Arming is the supervisor's explicit act (see #187's
#     enforcement audit for restoring the producer, removed with #143).
#   * Every OTHER tag is EXEMPT from blocking in every context (findings still
#     print for the log). The escalation markers `refs/tags/blocked/*` and
#     `refs/tags/gate/*` are the point: the exact spoke whose diff needs a human
#     decision ("my diff reduces assertions — is that legit?") must always be able
#     to announce it, or the tripwire deadlocks the escalation channel and the
#     drain sees a silent, stuck spoke instead of a blocked/ ping — the same
#     principle as spoke-ready's blocked/+gate/ exemption from the upstream guard
#     (#103). `refs/tags/accept/*` parks finished work for a human EYEBALL — the
#     human is the gate — and a foreign tag (a consumer repo's v1.2.3) carries no
#     hub semantics; neither ships code, because the code only moves on the gated
#     branch push. `refs/tags/ready/*` alone stays gated: it is auto_land's trust
#     basis, the trigger for an unattended land. The exemption is PER-REF: a mixed
#     push carrying a gated ref alongside a marker still blocks (git pre-push is
#     all-or-nothing), so escalation markers are pushed alone.
#
# It reads git's pre-push stdin (`<lref> <lsha> <rref> <rsha>` lines) exactly like
# test-select.sh, resolving the range `rsha..lsha` (a new ref with an all-zero
# remote sha falls back to the merge-base with the default branch). Classification
# keys on the REMOTE ref — the ref the push actually updates — so a refspec push
# (`git push origin <local>:<remote>`) cannot smuggle a gutting diff onto a branch
# under an escalation-tag local name.
#
# Signatures (a clean RED->GREEN diff only ADDS real assertions, so these stand out):
#   * any *.py: an added `sys.exit(0)` / `sys.exit()` / `os._exit(...)` — a hard
#     short-circuit that can exit a test process green;
#   * test files: an added `@pytest.mark.skip|xfail` or `pytest.skip(|xfail(` — a
#     silenced test;
#   * test files: an added tautological `assert True` / `assert 1`;
#   * test files: a NET DECREASE in `assert` statements (more removed than added) —
#     deleted / weakened assertions.
#
# Portable ERE only (BSD/macOS grep has no \s or \b): [[:space:]] and explicit
# character classes.
set -uo pipefail

STDIN="$(cat || true)"

is_zero_sha() {
  local sha="$1"
  [ -n "$sha" ] || return 1
  case "$sha" in
    *[!0]*) return 1 ;;
    *) return 0 ;;
  esac
}

# default_branch — origin/HEAD, else main/master, else main (the new-branch base).
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

DEFAULT="$(default_branch)"

# Resolve the range(s) to scan from the pushed refs. Each entry is "<mode> <range>":
# `gated` for the ship paths — branches, ready/<N>, any non-tag ref — which fail
# closed under unattended; `exempt` for every other tag (escalation markers
# blocked/<N> + gate/<N>, the accept/<N> eyeball park, foreign tags — never block,
# #193). The mode keys on the REMOTE ref (`rref`), the ref this push updates.
RANGES=()
while read -r _lref lsha rref rsha; do
  [ -n "${lsha:-}" ] || continue
  case "${rref:-}" in
    refs/tags/ready/*) mode=gated ;;
    refs/tags/*) mode=exempt ;;
    *) mode=gated ;;
  esac
  is_zero_sha "$lsha" && continue   # deleting a ref — nothing added
  if is_zero_sha "${rsha:-0}"; then
    base="$(git merge-base "$DEFAULT" "$lsha" 2>/dev/null || true)"
    [ -n "$base" ] || continue      # unresolved base — can't scan this ref
    RANGES+=("$mode $base..$lsha")
  else
    # A non-zero but unresolvable rsha (e.g. GC'd after a force-push) yields an
    # empty `git diff` and so a missed scan rather than a false block — acceptable
    # because this is defense-in-depth; test-select.sh remains the blocking gate.
    RANGES+=("$mode $rsha..$lsha")
  fi
done <<< "$STDIN"

[ "${#RANGES[@]}" -gt 0 ] || exit 0   # nothing to scan

# count_matches <pattern> <text> -> number of lines matching (0 when none; grep -c
# exits 1 on no match, so `|| true` keeps the count and the pipeline non-fatal).
count_matches() {
  printf '%s\n' "$2" | grep -Ec "$1" 2>/dev/null || true
}

FINDINGS=()
GATED_HIT=0   # set when a finding lands on a gated (ship-path) range
for entry in "${RANGES[@]}"; do
  mode="${entry%% *}"
  range="${entry#* }"
  before="${#FINDINGS[@]}"
  # Added lines (drop the +++ header) of any python file. Diff markers use bracket
  # classes ([+]/[-]) rather than ^\+ so the pattern is portable across BSD/GNU/ugrep
  # (BSD grep rejects a leading \+ as a repetition operator).
  py_added="$(git diff "$range" -- '*.py' 2>/dev/null | grep '^[+]' | grep -v '^[+][+][+]' || true)"
  if printf '%s\n' "$py_added" \
    | grep -Eq 'sys\.exit\([[:space:]]*0?[[:space:]]*\)|os\._exit\('; then
    FINDINGS+=("added sys.exit()/os._exit() short-circuit ($range)")
  fi

  # Test-file diff for the test-specific signatures. The pathspec targets pytest's
  # own discovery shapes (tests/ dir, test_*.py, *_test.py) rather than a bare
  # *test*.py, so files that merely contain "test" (latest.py, manifest_io.py) are
  # not scanned for test-only signatures.
  test_diff="$(git diff "$range" -- 'tests/' '*test_*.py' '*_test.py' 2>/dev/null || true)"
  test_added="$(printf '%s\n' "$test_diff" | grep '^[+]' | grep -v '^[+][+][+]' || true)"
  test_removed="$(printf '%s\n' "$test_diff" | grep '^[-]' | grep -v '^[-][-][-]' || true)"

  if printf '%s\n' "$test_added" \
    | grep -Eq '@pytest\.mark\.(skip|xfail)|pytest\.(skip|xfail)\('; then
    FINDINGS+=("added skip/xfail silencing a test ($range)")
  fi
  if printf '%s\n' "$test_added" \
    | grep -Eq 'assert[[:space:]]+(True|1)([^0-9A-Za-z_]|$)'; then
    FINDINGS+=("added tautological assert True/1 ($range)")
  fi
  added_asserts="$(count_matches 'assert[[:space:]]' "$test_added")"
  removed_asserts="$(count_matches 'assert[[:space:]]' "$test_removed")"
  if [ "${removed_asserts:-0}" -gt "${added_asserts:-0}" ]; then
    FINDINGS+=("net decrease in assertions ($removed_asserts removed, $added_asserts added) ($range)")
  fi

  if [ "$mode" = gated ] && [ "${#FINDINGS[@]}" -gt "$before" ]; then
    GATED_HIT=1
  fi
done

[ "${#FINDINGS[@]}" -gt 0 ] || exit 0

{
  echo "anti-gutting: the pushed diff weakens tests —"
  for f in "${FINDINGS[@]}"; do echo "  • $f"; done
} >&2

# is_unattended — an /afk drain armed the tripwire and no human is watching
# (#74/#193): a truthy UNATTENDED env (0/false/empty mean attended), or the
# supervisor's dedicated marker under the git common dir every spoke worktree
# shares. Arming is an explicit act — see the header for why `.afk-state` is not
# read here.
is_unattended() {
  case "${UNATTENDED:-}" in ""|0|false) ;; *) return 0 ;; esac
  local common
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -f "$common/ai-toolkit-afk/unattended" ]
}

# Fail CLOSED only on a SHIP-path finding under an unattended drain. Escalation-marker
# findings (blocked/<N>, gate/<N>) fall through to the advisory exit in every context:
# the spoke that trips this scan must always be able to ask for a human (#193).
if [ "$GATED_HIT" -eq 1 ] && is_unattended; then
  {
    echo "anti-gutting: UNATTENDED — blocking the push; no human is watching, so a ship ref (branch / ready/<N>) carrying a test-weakening diff must not land (#193)."
    echo "anti-gutting: the escalation channel stays open — push the blocked/<N> or gate/<N> marker ALONE to ask for a human."
  } >&2
  exit 1
fi

echo "anti-gutting: advisory — push allowed; review the above before landing." >&2
exit 0
