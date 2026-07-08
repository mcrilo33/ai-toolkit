#!/usr/bin/env bash
# anti-gutting-scan.sh — mechanical tripwire against an implementation that GUTS
# the tests to go green.
#
# A deterministic scan of the pushed diff for test-gutting signatures, with
# enforcement split by CHANNEL (#193):
#
#   * ATTENDED it is advisory everywhere — findings print to stderr, exit 0 — so a
#     human's ordinary test edit is never gated.
#   * UNATTENDED (/afk armed — the UNATTENDED env, the supervisor's
#     `ai-toolkit-afk/unattended` marker (#74), or a non-empty `.afk-state`, all
#     under the git common dir every spoke worktree shares) it fails CLOSED on the
#     SHIP paths: a finding on a branch ref or `refs/tags/ready/*` blocks the push,
#     because no human is watching for a test-gutting diff.
#   * The ESCALATION markers `refs/tags/blocked/*` and `refs/tags/gate/*` are EXEMPT
#     from blocking in every context (findings still print for the log). This
#     ordering is the invariant the whole hub-and-spoke liveness model rests on: the
#     exact spoke whose diff needs a human decision ("my diff reduces assertions —
#     is that legit?") must always be able to announce it, or the tripwire deadlocks
#     the escalation channel and the drain sees a silent, stuck spoke instead of a
#     blocked/ ping — the same principle as spoke-ready's blocked/+gate/ exemption
#     from the upstream guard (#103). The exemption is PER-REF: a mixed push
#     carrying a gated ref alongside a marker still blocks (git pre-push is
#     all-or-nothing), so escalation markers are pushed alone.
#
# It reads git's pre-push stdin (`<lref> <lsha> <rref> <rsha>` lines) exactly like
# test-select.sh, resolving the range `rsha..lsha` (a new ref with an all-zero
# remote sha falls back to the merge-base with the default branch).
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
# `exempt` for the escalation markers (blocked/<N>, gate/<N> — never block, #193),
# `gated` for every ship path (branches, ready/<N>, any other ref — fail closed
# under unattended).
RANGES=()
while read -r _lref lsha _rref rsha; do
  [ -n "${lsha:-}" ] || continue
  case "$_lref" in
    refs/tags/blocked/*|refs/tags/gate/*) mode=exempt ;;
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

# is_unattended — an /afk drain is armed and no human is watching (#74/#193): the
# UNATTENDED env, the supervisor's dedicated marker, or hub-afk.sh's non-empty
# `.afk-state` window file, both under the git common dir every spoke worktree shares.
is_unattended() {
  [ -n "${UNATTENDED:-}" ] && return 0
  local common
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -f "$common/ai-toolkit-afk/unattended" ] && return 0
  [ -s "$common/.afk-state" ]
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
