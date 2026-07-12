#!/usr/bin/env bash
#
# worktree-land.sh — land a finished task branch from the hub (main checkout).
# The deterministic half of the land skill (`/land <id>`): the hub starts
# and ends tasks; spokes only execute. Run it FROM the hub, on the default
# branch, after the spoke has pushed — never from inside a worktree.
#
# Usage:
#   scripts/worktree-land.sh <issue|slug|branch|path> [--skip-tests] [--keep-branch] [--local] [--force-land] [--test-cmd <cmd>]
#
#   <issue|slug|branch|path>  anything that identifies the task worktree
#   --skip-tests              skip the pre-push test gate (threads TEST_SELECT_SKIP=1)
#   --keep-branch             keep the branch after landing (passed to worktree-done.sh)
#   --local                   micro-spoke path: skip upstream guards and accept a bare
#                             local branch with no registered worktree (the hub's diff
#                             review is the gate; merge+push is what ships the work)
#   --force-land              land a numbered branch that carries no ready/<issue>
#                             completion marker (express/ad-hoc branches that never
#                             emit one); the marker guard is otherwise mandatory
#   --test-cmd <cmd>          run <cmd> as the gate instead of the tiered selection
#                             (threads TEST_SELECT_CMD to the pre-push hook)
#
# The pre-push hook is the SINGLE owner of test execution (issue #19): landing
# merges locally and pushes main, and that push's pre-push hook runs the tiered,
# diff-aware suite — "one push = one run". Landing no longer runs pytest itself;
# --skip-tests/--test-cmd are threaded to the hook via TEST_SELECT_*.
#
# A clean fast-forward land of an already-gated branch (ready/<issue> marker at
# the tip) re-tests an identical tree, so the gate is auto-skipped there (issue
# #96); diverged/merge-commit lands still run it. Set LAND_FORCE_GATE=1 to force
# the gate back on even for a clean-FF land.
#
# Sequence, each step aborting safely on failure:
#   guards  hub on default branch + clean; worktree resolved, clean, fully pushed;
#           numbered branches carry a ready/<issue> marker at their tip (issue #16)
#   merge   --ff-only when possible, else a merge commit (plain `git merge`)
#   ship    push origin <default> — the pre-push hook is the test gate; a rejected
#           push (gate failed or remote refused) rolls back `git reset --keep`.
#           Then worktree-done.sh → `gh issue close` (numeric ids)
#   tmux    kill the task's window in the project session when its pane path is gone
#
set -euo pipefail

WT_PROG="worktree-land"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

# --- guard: role, not directory (issue #26) -------------------------------------
# A spoke's claude has full filesystem access, so it can cd into the main checkout
# and slip past a "must run from the hub" directory check. worktree-new.sh stamps
# the spoke session with WT_SPOKE; that marker rides every command it runs, so
# refuse here before any work. No override flag — an escape hatch is exactly how a
# spoke would self-land again. The hub is user-started and never carries WT_SPOKE.
[ -z "${WT_SPOKE:-}" ] \
  || wt_die "this is the spoke session for '$WT_SPOKE' — lands run on the hub. Emit your ready/<issue> marker (your push is your ship gate); the hub will land it."

# Span start clock for the lifecycle/land span emitted after a successful merge.
WT_T0="$(wt_now_ms)"

TARGET=""
SKIP_TESTS=""
KEEP_BRANCH=""
LOCAL=""
FORCE_LAND=""
TEST_CMD=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-tests)  SKIP_TESTS=1; shift ;;
    --keep-branch) KEEP_BRANCH=1; shift ;;
    --local)       LOCAL=1; shift ;;
    --force-land)  FORCE_LAND=1; shift ;;
    --test-cmd)    [ "$#" -ge 2 ] || wt_die "--test-cmd needs a value"; TEST_CMD="$2"; shift 2 ;;
    --test-cmd=*)  TEST_CMD="${1#--test-cmd=}"; shift ;;
    -*)            wt_die "unknown option: $1 (supported: --skip-tests, --keep-branch, --local, --force-land, --test-cmd)" ;;
    *)
      [ -z "$TARGET" ] || wt_die "unexpected extra argument: $1"
      TARGET="$1"; shift
      ;;
  esac
done
[ -n "$TARGET" ] || wt_die "usage: worktree-land.sh <issue|slug|branch|path> [--skip-tests] [--keep-branch] [--local] [--force-land] [--test-cmd <cmd>]"

# --- guards: the hub ----------------------------------------------------------
git rev-parse --git-dir >/dev/null 2>&1 || wt_die "run this from inside your checkout (cd into the repo first)"
REPO_ROOT="$(wt_main_root)" || wt_die "could not locate the main worktree"
[ "$(wt_realpath "$(git rev-parse --show-toplevel)")" = "$REPO_ROOT" ] \
  || wt_die "landing is hub-side — run from the main checkout ($REPO_ROOT), not a worktree"
cd "$REPO_ROOT"

# Base (integration) branch: the canonical resolver (issue #117) —
# config ai-toolkit.base-branch > AI_TOOLKIT_BASE_BRANCH > origin/HEAD > main/master.
DEFAULT="$(wt_base_branch "$REPO_ROOT")"
HUB_BRANCH="$(git symbolic-ref --short -q HEAD || true)"
[ "$HUB_BRANCH" = "$DEFAULT" ] \
  || wt_die "hub is on '${HUB_BRANCH:-detached HEAD}' — land from the base branch '$DEFAULT'"
[ -z "$(git status --porcelain -uno)" ] \
  || wt_die "hub checkout is dirty — commit or stash before landing"

# land_resume_finalize <target> -> clean up the residue of a spoke whose land was
# killed AFTER the worktree was removed but BEFORE its branch/tag/issue were cleaned up
# (issue #151): a caller-timeout mid-teardown. Engages ONLY when <target> is a bare
# issue number carrying a ready/<issue> marker whose commit is already merged into
# $DEFAULT — positive proof the work shipped — so it can never touch an unshipped issue.
# Each destructive step is guarded to fire only when its residue actually remains:
#   * branch prune — ONLY the exact branch whose tip IS the merged marker (never a
#     same-numbered sibling), and its remote is deleted only when the remote ref is
#     itself merged into the base (the reduce-and-prune data-loss guard the normal
#     land treats as fatal, issues #10/#16).
#   * issue close — ONLY when the issue is still OPEN, so a done issue whose merged tag
#     merely lingered is never re-closed / re-commented.
# It NEVER re-merges or re-pushes. Returns 1 (no-op) when no resume signal exists, so
# the caller aborts exactly as before.
land_resume_finalize() {
  local target="$1" issue marker_sha fetch_ok br br_tip state sess win name path
  [[ "$target" =~ ^[0-9]+$ ]] || return 1
  issue="$target"
  marker_sha="$(git rev-parse -q --verify "refs/tags/ready/${issue}^{commit}" 2>/dev/null || true)"
  [ -n "$marker_sha" ] || return 1
  git merge-base --is-ancestor "$marker_sha" "$DEFAULT" 2>/dev/null || return 1

  wt_warn "issue #$issue shipped (ready/$issue at ${marker_sha:0:9} is merged into $DEFAULT) but its teardown left residue — cleaning it up (issue #151)"

  # Refresh remote state so the merged-remote guard below is honest. The land
  # itself stays best-effort, but a FAILED fetch disarms the remote delete below
  # (issue #195): the merged-remote check would otherwise pass on a stale
  # tracking ref while the real remote holds commits the hub never fetched.
  fetch_ok=1
  git fetch origin --quiet 2>/dev/null || fetch_ok=""

  # Prune ONLY the exact branch that shipped: its tip IS the merged marker commit, so a
  # same-numbered but unrelated branch is never touched. `git branch -d` is merged-only;
  # the remote delete then fires only when the remote ref is merged into the base too, so
  # a branch whose remote is strictly ahead can never lose its remote-only commits.
  while IFS= read -r br; do
    [ -n "$br" ] || continue
    br_tip="$(git rev-parse -q --verify "refs/heads/$br" 2>/dev/null || true)"
    [ "$br_tip" = "$marker_sha" ] || continue
    if git branch -d "$br" >/dev/null 2>&1; then
      echo "✓ pruned merged branch $br"
      if [ -z "$fetch_ok" ]; then
        wt_warn "fetch failed — kept remote origin/$br (its merged-ness can't be verified against stale remote state, issue #195); once origin is reachable, delete it by hand if it still exists: git push origin --delete $br"
      elif git merge-base --is-ancestor "refs/remotes/origin/$br" "$DEFAULT" 2>/dev/null; then
        wt_git_push origin --delete "$br" >/dev/null 2>&1 \
          || wt_warn "couldn't delete remote origin/$br — delete it by hand: git push origin --delete $br"
      else
        wt_warn "kept remote origin/$br — it has commits not in $DEFAULT; reconcile it by hand"
      fi
    fi
  done < <(git for-each-ref --format='%(refname:short)' refs/heads/ 2>/dev/null || true)

  # Consume the lingering completion marker (local + remote), best-effort.
  git tag -d "ready/${issue}" >/dev/null 2>&1 || true
  wt_git_push origin ":refs/tags/ready/${issue}" >/dev/null 2>&1 || true

  # Close the issue ONLY when it is still OPEN — never re-close / re-comment a done
  # issue whose merged tag merely lingered.
  if command -v gh >/dev/null 2>&1; then
    state="$(gh issue view "$issue" --json state -q .state 2>/dev/null || true)"
    if [ "$state" = "OPEN" ]; then
      if gh issue close "$issue" --comment "Landed on $DEFAULT; teardown finalized by a resumed land (issue #151)."; then
        echo "✓ closed issue #$issue"
      else
        wt_warn "couldn't close issue #$issue — close it by hand: gh issue close $issue"
      fi
    fi
  fi

  # Kill the stranded tmux window (its pane cwd vanished with the worktree).
  if command -v tmux >/dev/null 2>&1; then
    sess="$(wt_tmux_session "$REPO_ROOT")"
    while IFS=$'\t' read -r win name path; do
      [ -n "$win" ] || continue
      case "$name" in "$issue" | "$issue"-*) ;; *) continue ;; esac
      [ -d "$path" ] || tmux kill-window -t "$win" 2>/dev/null || true
    done < <(tmux list-windows -t "=$sess" -F $'#{window_id}\t#{window_name}\t#{pane_current_path}' 2>/dev/null || true)
  fi

  echo "✓ finalized partially-landed issue #$issue (resumed teardown)"
  return 0
}

# --- guards: the spoke ----------------------------------------------------------
WT_DIR=""
WT_BRANCH=""
if WT_DIR="$(wt_resolve "$TARGET" "$REPO_ROOT")"; then
  # Normal worktree path: resolve branch from the registered worktree list.
  while IFS=$'\t' read -r wt br; do
    if [ "$wt" = "$WT_DIR" ]; then
      WT_BRANCH="$br"
      break
    fi
  done < <(wt_task_worktrees "$REPO_ROOT")
  [ -n "$WT_BRANCH" ] || wt_die "worktree $WT_DIR is on a detached HEAD — nothing to land"

  # Untracked files count as dirty: `git worktree remove` would refuse them later,
  # and a stray WIP file is exactly what landing must not destroy.
  [ -z "$(git -C "$WT_DIR" status --porcelain)" ] \
    || wt_die "worktree $WT_DIR has uncommitted or untracked changes — finish or stash them on the spoke"
else
  # wt_resolve failed — no registered worktree matched TARGET.
  if [ -n "$LOCAL" ] && git show-ref --verify --quiet "refs/heads/$TARGET"; then
    # --local + bare local branch: the temp worktree is already gone; land directly.
    # Never the default branch itself — a typo'd target would otherwise "land" as
    # a no-op self-merge that exits 0 and then advises deleting it by hand.
    [ "$TARGET" != "$DEFAULT" ] || wt_die "refusing to land the default branch '$DEFAULT' into itself"
    WT_BRANCH="$TARGET"
    WT_DIR=""
  elif land_resume_finalize "$TARGET"; then
    # A land whose teardown was killed after the worktree was removed — finished
    # from the surviving merged ready/<issue> marker (issue #151). Terminal path.
    exit 0
  else
    HINT="pass one of the paths above, or its issue number / slug / branch."
    [ -n "$LOCAL" ] && HINT="$HINT (--local also accepts a full local branch name)"
    wt_warn "no single worktree matches '$TARGET'. Existing task worktrees:"
    wt_print_worktrees "$REPO_ROOT"
    wt_die "$HINT"
  fi
fi

if [ -z "$LOCAL" ]; then
  # A failed fetch is FATAL, not a warning (issue #195): the ahead/behind guards
  # below would otherwise run against the LAST-KNOWN origin/<branch> — a spoke
  # push the hub never fetched reads behind=0, the land proceeds, and teardown
  # deletes origin/<branch> with the remote-only commits. Destructive decisions
  # never run on stale remote state. One immediate retry absorbs a transient
  # blip (the #119 SSH-staleness class — a fresh short connection usually
  # clears it) so an unattended /afk land isn't escalated blocked/<N> over a
  # moment of network noise; a real outage still dies.
  git fetch origin --quiet 2>/dev/null \
    || git fetch origin --quiet 2>/dev/null \
    || wt_die "fetch from origin failed — refusing to land on last-known remote state (a spoke push this checkout never fetched would be silently pruned). Restore connectivity and re-run."
  # Upstream guards: the spoke's push is its ship gate.
  UPSTREAM="$(git rev-parse --symbolic-full-name "${WT_BRANCH}@{upstream}" 2>/dev/null || true)"
  [ -n "$UPSTREAM" ] || wt_die "branch $WT_BRANCH has never been pushed — the spoke's push is its ship gate"
  AHEAD="$(git rev-list --count "${UPSTREAM}..${WT_BRANCH}")"
  [ "$AHEAD" -eq 0 ] || wt_die "branch $WT_BRANCH is $AHEAD commit(s) ahead of $UPSTREAM — push from the spoke first"
  # Behind is just as fatal as ahead: landing a reduced local branch would later
  # prune the remote ref and silently lose the commits only the remote still has.
  BEHIND="$(git rev-list --count "${WT_BRANCH}..${UPSTREAM}")"
  [ "$BEHIND" -eq 0 ] || wt_die "branch $WT_BRANCH is $BEHIND commit(s) behind $UPSTREAM — the remote has work this checkout lacks; reconcile on the spoke first"
else
  # --local is for micro-spokes, which never push. A branch WITH an upstream is
  # not a micro-spoke: skipping the behind guard could merge a reduced local tip
  # and later prune the remote ref, losing the commits only the remote still has.
  ! git rev-parse --symbolic-full-name "${WT_BRANCH}@{upstream}" >/dev/null 2>&1 \
    || wt_die "branch $WT_BRANCH has an upstream — not a micro-spoke; land it without --local"
fi

# Issue number = leading number of the branch slug (feature/42-foo → 42);
# ad-hoc branches have none and skip the issue close.
BSLUG="${WT_BRANCH##*/}"
ISSUE="${BSLUG%%-*}"
[[ "$ISSUE" =~ ^[0-9]+$ ]] || ISSUE=""

# --- guard: the ready-to-land marker (issue #16) --------------------------------
# A per-subtask push is indistinguishable from task completion. For a numbered,
# pushed branch, require an explicit ready/<issue> tag at the branch tip before
# landing — otherwise a spoke caught between subtasks (clean + pushed) would be
# landed as a finished issue and have its worktree torn down. The tag is shared
# between hub and spoke worktrees, so a marker the spoke set is visible here.
# Exempt: --local micro-spokes (never push, no marker), ad-hoc/non-numbered
# branches (their one push IS completion), and --force-land (explicit override).
#
# GATED_TREE records that we can PROVE the branch tip was already test-gated: the
# marker sits at the tip (==this verified completion) and the upstream guards above
# confirmed tip == pushed upstream (ahead==0 && behind==0), so the spoke's push ran
# the gate on exactly this tree. It licenses the clean-FF gate skip below (#96);
# without a marker (--local/--force-land/ad-hoc) we make no such claim.
GATED_TREE=""
if [ -z "$LOCAL" ] && [ -z "$FORCE_LAND" ] && [ -n "$ISSUE" ]; then
  MARKER="ready/${ISSUE}"
  MARKER_SHA="$(git rev-parse -q --verify "refs/tags/${MARKER}^{commit}" 2>/dev/null || true)"
  TIP_SHA="$(git rev-parse -q --verify "refs/heads/${WT_BRANCH}" 2>/dev/null || true)"
  if [ -z "$MARKER_SHA" ]; then
    wt_die "branch $WT_BRANCH carries no ${MARKER} marker — it looks mid-task (pushed but not signalled complete). Emit it on the spoke after the FINAL subtask's push (git tag ${MARKER} && git push origin ${MARKER}), or pass --force-land for a branch that never carries one."
  elif [ "$MARKER_SHA" != "$TIP_SHA" ]; then
    wt_die "${MARKER} marker is stale (points at ${MARKER_SHA:0:9}, branch tip is ${TIP_SHA:0:9}) — the spoke pushed more work after signalling complete. Re-tag at the tip on the spoke (git tag -f ${MARKER} && git push -f origin ${MARKER}), or pass --force-land."
  fi
  GATED_TREE=1
fi

# --- merge ----------------------------------------------------------------------
PRE_SHA="$(git rev-parse HEAD)"
echo "→ merging $WT_BRANCH into $DEFAULT"
if ! git merge --no-edit "$WT_BRANCH"; then
  git merge --abort 2>/dev/null || true
  wt_die "merge of $WT_BRANCH conflicts with $DEFAULT — rebase the branch on $DEFAULT (on the spoke, then push, when it has one) and re-run"
fi
MERGED_SHA="$(git rev-parse HEAD)"

# Roll the hub back to its pre-merge tip. A failed reset leaves the hub on the merge
# commit — unrecoverable here, so die with by-hand instructions. Shared by the three
# land abort paths (merge-sanity, missing-hook, push-rejection) so the recovery
# command and its guidance never drift across copies (issue #196).
land_reset_keep_or_die() {
  git reset --keep "$PRE_SHA" \
    || wt_die "rollback failed — hub is still on the merge commit; reset by hand: git reset --keep $PRE_SHA"
}

# --- skip the redundant gate on a clean fast-forward land (issue #96) -------------
# A clean fast-forward leaves HEAD identical to the branch tip the spoke already
# gated on its push (GATED_TREE: marker == tip == upstream), so re-running the
# pre-push suite re-tests an identical tree — the dominant cost of a land whenever
# the diff escalates test-select to the full suite. A diverged merge instead builds
# a NEW merge commit (HEAD != branch tip) whose combined tree was never tested as a
# unit, so its gate must still run. Explicit --skip-tests / --test-cmd already own
# the gate decision; LAND_FORCE_GATE=1 is the escape hatch to force it back on.
AUTO_SKIP=""
if [ -z "$SKIP_TESTS" ] && [ -z "$TEST_CMD" ] && [ -z "${LAND_FORCE_GATE:-}" ] \
   && [ -n "$GATED_TREE" ] && [ "$MERGED_SHA" = "$(git rev-parse "refs/heads/$WT_BRANCH")" ]; then
  AUTO_SKIP=1
fi

# --- merge-sanity on a diverged --skip-tests land (issue #174) --------------------
# auto_land trusts the ready-marker green and lands with --skip-tests — correct
# per-branch, but a DIVERGED land builds a merge commit whose combined tree (the
# hub's post-marker commits + the branch) nobody ever tested: the one untested
# state that reaches $DEFAULT unattended. Before pushing it, run a BOUNDED sanity
# check on the merged tree: `pytest --collect-only -q` (import/collection health)
# plus the tests the reverse index maps to the files the merge changed — NEVER the
# full suite (the #140 ref-collision class). Target wall-clock < 60s: collection is
# a couple of seconds, and the mapped bodies are capped (MERGE_SANITY_MAX_MAPPED) so
# a change to a widely-referenced script can't drag the land into a multi-minute
# suite. A pytest failure rolls the merge back and aborts (the /afk caller then
# escalates blocked/<N>). Fires ONLY for --skip-tests + a real merge commit;
# fast-forward --skip-tests lands and manual (no --skip-tests) lands are untouched.
merge_sanity_rollback() {
  wt_warn "merge-sanity check FAILED on the diverged merge (issue #174) — rolling back: git reset --keep $PRE_SHA"
  land_reset_keep_or_die
  wt_die "landing aborted: the diverged --skip-tests merge failed its merge-sanity check; nothing was pushed. Fix on the branch, push from the spoke, and re-run."
}

# run_merge_sanity — exit code is a THREE-way signal so the report can stay honest:
#   0  the check RAN and passed
#   1  the check RAN and a pytest run failed (or the tripwire tripped) → abort
#   2  DEGRADED: no libs / no runner — nothing was actually verified (don't claim
#      "passed"). Only code 1 blocks the land; a degrade proceeds like the rest of
#      this script's best-effort tail.
# Runs in a SUBSHELL: sourcing the hook libs may `exit 0` when the toolkit is
# globally off (enabled.sh) or arm a telemetry EXIT trap — both must stay contained
# so the land in progress is never aborted or instrumented by the check's own libs.
run_merge_sanity() (
  lib_dir=""
  for cand in "$SCRIPT_DIR/../shared/hooks/lib" \
              "$(git rev-parse --git-path hooks/ai-toolkit-scripts/lib 2>/dev/null || true)"; do
    if [ -n "$cand" ] && [ -f "$cand/utils.sh" ] && [ -f "$cand/test-reverse-index.sh" ]; then
      lib_dir="$cand"; break
    fi
  done
  if [ -z "$lib_dir" ]; then
    wt_warn "merge-sanity: hook libs not found — SKIPPING the diverged-land check (install with scripts/install-git-hooks.sh); the merged tree is UNVERIFIED"
    return 2
  fi
  # shellcheck source=../shared/hooks/lib/utils.sh
  source "$lib_dir/utils.sh" 2>/dev/null || true
  # shellcheck source=../shared/hooks/lib/test-reverse-index.sh
  source "$lib_dir/test-reverse-index.sh" 2>/dev/null || true
  # Guard every function used below — a partial source (edited/truncated lib) can
  # define some and not others, and calling an undefined one under set -e would
  # abort the land instead of degrading.
  if ! command -v detect_pytest >/dev/null 2>&1 \
     || ! command -v run_under_tripwire_scoped >/dev/null 2>&1 \
     || ! command -v reverse_index_tests_for >/dev/null 2>&1; then
    wt_warn "merge-sanity: hook libs incomplete — SKIPPING the diverged-land check; the merged tree is UNVERIFIED"
    return 2
  fi

  runner="$(detect_pytest "." || true)"
  if [ -z "$runner" ]; then
    wt_warn "merge-sanity: no pytest available — SKIPPING the diverged-land check; the merged tree is UNVERIFIED"
    return 2
  fi
  read -r -a runner_arr <<< "$runner"

  # Strip git's ambient repo-targeting env before every pytest child (the same
  # defense test-select.sh applies): a test that shells out to git must hit its own
  # tmpdir, never the REAL hub repo, even if worktree-land was invoked with GIT_*
  # exported. The tripwire is the backstop; this keeps it from firing in the first
  # place.
  git_unset=(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_OBJECT_DIRECTORY \
    -u GIT_COMMON_DIR -u GIT_NAMESPACE -u GIT_PREFIX)

  # The check runs in the SHARED hub ref store, where sibling spokes legitimately move
  # their own refs mid-run (a committed head, a pushed branch's remote-tracking ref, an
  # /afk drain's ready/<N> tag). Scope the tripwire to the one ref the land itself owns —
  # refs/heads/$DEFAULT, the merge commit we are about to push — so sibling churn is not
  # mistaken for an escape and the restore can never roll a sibling ref back (issue #205).
  # A test that escapes onto $DEFAULT is still caught and aborts the land.
  sanity_scope="refs/heads/$DEFAULT"

  # Collection/import health of the whole suite against the merged tree — bounded
  # (no test bodies execute), catches a cross-import break the combined tree adds.
  echo "→ merge-sanity: pytest --collect-only -q on the merged tree (import/collection health)"
  run_under_tripwire_scoped "$sanity_scope" "${git_unset[@]}" "${runner_arr[@]}" --collect-only -q || return 1

  # The tests test-select maps to the files the merge changed (PRE_SHA..MERGED_SHA),
  # deduped and existing only. No mapping → collection health was the whole check.
  mapped=""
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    hits="$(reverse_index_tests_for "$f")"
    [ -n "$hits" ] && mapped="$mapped$hits"$'\n'
  done < <(git diff --name-only "$PRE_SHA" "$MERGED_SHA" 2>/dev/null || true)

  sel=()
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    [ -f "$t" ] && sel+=("$t")
  done < <(printf '%s' "$mapped" | sort -u)

  if [ "${#sel[@]}" -eq 0 ]; then
    echo "→ merge-sanity: no mapped tests for the merged diff — collection health only"
    return 0
  fi
  # Boundedness cap: a change to a widely-referenced control-plane script (e.g. the
  # land script itself) maps a dozen heavy subprocess-driven suites — running them
  # would blow the < 60s target and stall an /afk drain holding the land lock. When
  # the mapped set is large, keep the fast collection-health signal and skip the
  # mapped bodies with a loud note rather than the full suite (MERGE_SANITY_MAX_MAPPED
  # tunes the cap).
  if [ "${#sel[@]}" -gt "${MERGE_SANITY_MAX_MAPPED:-4}" ]; then
    wt_warn "merge-sanity: the merged diff maps ${#sel[@]} test files (> ${MERGE_SANITY_MAX_MAPPED:-4}) — too many to stay bounded; running collection health only (raise MERGE_SANITY_MAX_MAPPED to force them)"
    return 0
  fi
  echo "→ merge-sanity: mapped tests for the merged diff — ${sel[*]}"
  run_under_tripwire_scoped "$sanity_scope" "${git_unset[@]}" "${runner_arr[@]}" "${sel[@]}" || return 1
  return 0
)

MERGE_SANITY_RAN=""
if [ -n "$SKIP_TESTS" ] && [ "$MERGED_SHA" != "$(git rev-parse "refs/heads/$WT_BRANCH")" ]; then
  echo "→ diverged --skip-tests land — running the bounded merge-sanity check (issue #174)"
  MS_RC=0
  run_merge_sanity || MS_RC=$?
  case "$MS_RC" in
    0) MERGE_SANITY_RAN=1 ;;      # ran and passed — the report may say so
    2) : ;;                       # degraded (warned inside) — report stays honest
    *) merge_sanity_rollback ;;   # ran and failed / tripwire → roll back and abort
  esac
fi

# --- ship: push main; the pre-push hook is the single test gate (issue #19) -------
# --skip-tests / --test-cmd are threaded to the hook via TEST_SELECT_*, so the
# hook stays the single executor. A rejected push — the gate failing, or a remote
# refusal — rolls the merge back, so a failed land always leaves a clean hub.
if [ -n "$SKIP_TESTS" ]; then
  if [ -n "$MERGE_SANITY_RAN" ]; then
    SUITE_RESULT="skipped (--skip-tests); merge-sanity check passed on the diverged tree (issue #174)"
  else
    SUITE_RESULT="skipped (--skip-tests)"
  fi
elif [ -n "$AUTO_SKIP" ]; then
  SUITE_RESULT="skipped (clean fast-forward of an already-gated tree, issue #96)"
elif [ -n "$TEST_CMD" ]; then
  SUITE_RESULT="via pre-push hook (--test-cmd: $TEST_CMD)"
else
  SUITE_RESULT="via pre-push hook (tiered)"
fi
# The pre-push hook IS the test gate (issue #19). If a gate is REQUIRED here (not a
# --skip-tests / auto-skip land) and no executable hook is installed, the push would
# run NOTHING — a missing enforcement precondition silently shipping untested code to
# $DEFAULT (the #187 fail-open shape, issue #196). ABORT: roll the merge back and die
# with the install command, so landing ungated is only ever a visible flag, never an
# environmental accident.
if [ -z "$SKIP_TESTS" ] && [ -z "$AUTO_SKIP" ]; then
  PREPUSH_HOOK="$(git rev-parse --git-path hooks/pre-push 2>/dev/null || true)"
  if [ -z "$PREPUSH_HOOK" ] || [ ! -x "$PREPUSH_HOOK" ]; then
    wt_warn "no executable pre-push hook here — the test gate cannot run; rolling back: git reset --keep $PRE_SHA"
    land_reset_keep_or_die
    wt_die "landing aborted: the pre-push test gate is REQUIRED but no executable hook is installed here, so the push would ship untested code to $DEFAULT. Install it with scripts/install-git-hooks.sh, or land ungated on purpose with --skip-tests. Nothing was pushed."
  fi
fi
echo "→ pushing $DEFAULT to origin (the pre-push hook runs the test gate)"

# One ship attempt; a non-empty $1 forces TEST_SELECT_SKIP=1 (the retry lane).
# The subshell scopes the exports; the push routes through wt_git_push so the
# SSH connection is kept alive across the multi-minute in-push gate (issue #119).
land_push() {
  (
    if [ -n "$SKIP_TESTS" ] || [ -n "$AUTO_SKIP" ] || [ -n "${1:-}" ]; then
      export TEST_SELECT_SKIP=1
    fi
    if [ -n "$TEST_CMD" ]; then export TEST_SELECT_CMD="$TEST_CMD"; fi
    wt_git_push origin "$DEFAULT"
  )
}

land_rollback() {
  rm -f "$PUSH_LOG"
  wt_warn "$1 — rolling back: git reset --keep $PRE_SHA"
  land_reset_keep_or_die
  wt_die "landing aborted; nothing was pushed. Fix on the branch (push from the spoke when it has one) and re-run."
}

# Capture the push's combined output while streaming it live: tee exits 0 and
# pipefail is on, so PUSH_RC is git's own exit code (a 141 SIGPIPE survives) and
# the capture file is complete when the pipeline returns.
PUSH_LOG="$(mktemp "${TMPDIR:-/tmp}/wt-land-push.XXXXXX")"
PUSH_RC=0
land_push "" 2>&1 | tee "$PUSH_LOG" || PUSH_RC=$?
if [ "$PUSH_RC" -ne 0 ]; then
  # Second line of defense (issue #119): retry EXACTLY once with the suite
  # skipped when the failure is demonstrably POST-green — (a) a transport-death
  # signature (git only enters the transfer phase after the pre-push hook exited
  # 0) AND (b) no pytest failure shape in the capture: a FAILING gate whose
  # output merely quotes a transport phrase (this repo's own tests embed those
  # literals) must still read as a failed gate. The filter covers all of
  # pytest's red summaries — "N failed", "N error(s)" (collection/internal),
  # and the "Interrupted:" banner — none of which a green run prints ("N
  # xfailed" has no digit before "failed" and never matches). Anything else —
  # failed gate, policy rejection — rolls back exactly as before, and a failed
  # retry does too.
  if wt_push_transport_died "$PUSH_RC" "$PUSH_LOG" \
     && ! grep -qE '[0-9]+ (failed|error)|Interrupted' "$PUSH_LOG"; then
    # A transport-death SHAPE (exit 141 or an SSH-disconnect signature) with no
    # pytest summary is necessary but NOT sufficient (issue #214): a gate KILLED
    # mid-run (SIGPIPE/OOM) shares exit 141 and prints no summary either. Honor
    # the skipped-suite retry ONLY when a green-tree stamp exists for this pushed
    # HEAD^{tree} (issue #122) — proof the gate ran this tree green and stamped it
    # before the transfer died. A killed gate never reaches its mint, so it leaves
    # no stamp and rolls back rather than shipping a tree whose suite never
    # completed. (wt_gate_green_stamped checks EXISTENCE, not tier/runner parity —
    # see its header for the bounded weakening.)
    if wt_gate_green_stamped; then
      wt_warn "gate ran green (this tree is green-stamped) but the push transport died (SSH staleness, issue #119) — retrying ONCE with TEST_SELECT_SKIP=1"
      if ! land_push retry; then
        land_rollback "retry push failed too"
      fi
      # Witness the skip: record it in the suite result so the issue-close
      # comment does not read as a normal, fully-gated land (issue #214).
      SUITE_RESULT="$SUITE_RESULT; post-green transport death — re-pushed with TEST_SELECT_SKIP=1 (this tree was green-stamped, issue #214)"
    else
      land_rollback "push exited $PUSH_RC with no test summary and no green-tree stamp for this tree — the gate was killed mid-run (not a post-green transport death), or no stamp was minted; refusing to retry with the suite skipped. Re-run the land to re-run the gate (issue #214)"
    fi
  else
    land_rollback "push rejected (pre-push test gate or remote)"
  fi
fi
rm -f "$PUSH_LOG"

# --- telemetry: hub-side Langfuse auth resolution (issue #127) --------------------
# Hub sessions don't hand-export LANGFUSE_BASIC_AUTH, which silently skipped the
# post-run ingest below and left the span sink dark. Resolve it here — env first,
# then the shared ~/.afk-telemetry conf — exporting auth + host for the ingesters
# and the OTLP span endpoint for the emits below. Best-effort by contract: an
# unresolvable auth returns 1 and exports nothing, keeping the ingest's existing
# skip-WARN; the land NEVER fails on telemetry.
wt_resolve_langfuse_auth || true

# --- telemetry: land lifecycle marker + script run-node --------------------------
# Emit AFTER the merge+push succeeds but BEFORE teardown, while the worktree (and
# its spoke_run_id) still exists. The script span is this control script as a trace
# node, sharing the marker's name (emission-link basis); it is also the node a later
# subtask uses to anchor the script→script chain to the worktree-done span this
# script shells out to next. No-op unless AI_TOOLKIT_TELEMETRY=1.
if [ -n "$WT_DIR" ]; then
  wt_emit_lifecycle "worktree-land" "land" "success" "$WT_T0" "$WT_DIR"
  wt_emit_script "worktree-land" "success" "$WT_T0" "$WT_DIR"
fi

# --- telemetry: automated post-run Langfuse ingestion ----------------------------
# An OTel spoke (AI_TOOLKIT_OTEL=1) only streams native traces live; the loaded-
# context itemization (#87) and transcript backfill (#92) are post-run steps that
# must read a SETTLED state, so run them now — after the push lands but BEFORE the
# tmux/worktree teardown SIGKILLs the spoke (dropping in-flight spans) and removes
# the worktree (taking its spoke-run-id + raw request bodies with it). The helper
# self-gates (not-an-OTel spoke, no LANGFUSE_BASIC_AUTH) and is best-effort: it
# never fails the land, so it never blocks shipping. No worktree (--local) → no-op.
if [ -n "$WT_DIR" ]; then
  # Terminal-outcome stamp (#231): record that this spoke LANDED before the view build reads it,
  # so the assembled trace carries an outcome:landed tag. Any blocked/relaunch count pointers the
  # supervisor left in .ai-toolkit persist here — the "disaster that eventually landed" economics.
  # If the supervisor already stamped a non-landed outcome, a block-time view was posted, so pass
  # --rebuild to refresh that partial snapshot rather than first-write-wins onto it. Only an
  # existing .ai-toolkit dir is written (worktree-new.sh mints + git-excludes it for a real OTel
  # spoke); a worktree without one is not an OTel spoke, so writing there would only dirty the tree
  # the teardown then refuses to remove. Best-effort — a write failure never blocks a completed land.
  REBUILD_VIEW=""
  if [ -d "$WT_DIR/.ai-toolkit" ]; then
    OUTCOME_FILE="$WT_DIR/.ai-toolkit/outcome"
    if [ -f "$OUTCOME_FILE" ] && [ "$(head -n1 "$OUTCOME_FILE" 2>/dev/null)" != "landed" ]; then
      REBUILD_VIEW="--rebuild"
    fi
    printf 'landed\n' > "$OUTCOME_FILE" 2>/dev/null \
      || wt_warn "couldn't stamp outcome=landed for $WT_DIR — trace may keep a stale/absent outcome tag"
  fi
  bash "$SCRIPT_DIR/telemetry-ingest-spoke.sh" "$WT_DIR" ${REBUILD_VIEW:+"$REBUILD_VIEW"} \
    || wt_warn "post-run Langfuse ingestion errored — landing continues"
fi

# From here on main has ADVANCED (origin/$DEFAULT moved). A teardown step that fails now must
# NOT die with the generic exit 1 (which auto_land reads as "land failed" and stamps blocked
# over already-merged code, issue #198): track it and exit the CLEANUP-INCOMPLETE sentinel (3)
# at the end so the caller can tell "nothing shipped" (1) from "shipped, cleanup incomplete" (3).
CLEANUP_INCOMPLETE=""
if [ -n "$WT_DIR" ]; then
  # WT_DONE seams the teardown for tests (default: the sibling worktree-done.sh). A failure is
  # post-ship residue, not a land failure — warn and flag the sentinel, never abort under set -e.
  bash "${WT_DONE:-$SCRIPT_DIR/worktree-done.sh}" "$WT_DIR" ${KEEP_BRANCH:+--keep-branch} \
    || { wt_warn "worktree-done teardown failed for $WT_DIR — main already advanced ($MERGED_SHA is on $DEFAULT); finish the teardown by hand"; CLEANUP_INCOMPLETE=1; }
elif [ -z "$KEEP_BRANCH" ]; then
  # Bare-branch mode: the worktree is already gone; just delete the merged local branch.
  # Safe — just merged; warn rather than abort if deletion fails.
  git branch -d "$WT_BRANCH" \
    || wt_warn "couldn't delete local branch $WT_BRANCH — delete it by hand: git branch -d $WT_BRANCH"
fi

# Consume the ready/<issue> completion marker (issue #16): the work is landed,
# so the tag has done its job. Leaving it behind would let a stale marker
# re-flag a future branch reusing the issue number as mergeable. Local then
# remote, warn-only — a missing tag (--force-land, ad-hoc) is a no-op.
if [ -n "$ISSUE" ] && [ -z "$LOCAL" ]; then
  MARKER="ready/${ISSUE}"
  if git rev-parse -q --verify "refs/tags/${MARKER}" >/dev/null 2>&1; then
    git tag -d "$MARKER" >/dev/null 2>&1 \
      || wt_warn "couldn't delete local tag $MARKER — delete it by hand: git tag -d $MARKER"
    wt_git_push origin ":refs/tags/${MARKER}" >/dev/null 2>&1 \
      || wt_warn "couldn't delete remote tag $MARKER — delete it by hand: git push origin :refs/tags/$MARKER"
  fi
fi

if [ -n "$ISSUE" ]; then
  if command -v gh >/dev/null 2>&1; then
    if gh issue close "$ISSUE" --comment "Landed on $DEFAULT in $MERGED_SHA (suite: $SUITE_RESULT)."; then
      echo "✓ closed issue #$ISSUE"
    else
      wt_warn "couldn't close issue #$ISSUE — close it by hand: gh issue close $ISSUE"
    fi
    # Mirror teardown (issue #236): the spoke no longer has live local state, so
    # strip the status:*/mode:*/lane:* labels dispatch stamped. Best-effort and
    # separate from the close comment above (which is unchanged): a failed gh here
    # never fails the land.
    wt_gh_clear_lifecycle_labels "$ISSUE"
  else
    wt_warn "gh not found — close issue #$ISSUE by hand"
  fi
fi

# --- tmux: kill the task's stranded window in the project session -----------------
# Spokes live as windows of the project's tmux session (issue #39: derived from
# the repo root, '<parent>-<base>'), named "<id>" or "<id>-<slug>". A window is
# stranded when its pane's cwd vanished with the worktree; live windows are kept.
# The session name MUST match worktree-new.sh's spawn target, so derive it the
# same way ('=' pins the exact match so a sibling session can't be enumerated).
TAG="${ISSUE:-$BSLUG}"
cleanup_tmux() {
  command -v tmux >/dev/null 2>&1 || return 0
  local sess win name path
  sess="$(wt_tmux_session "$REPO_ROOT")"
  while IFS=$'\t' read -r win name path; do
    [ -n "$win" ] || continue
    case "$name" in
      "$TAG"|"$TAG"-*) ;;
      *) continue ;;
    esac
    if [ ! -d "$path" ]; then
      if tmux kill-window -t "$win" 2>/dev/null; then
        echo "✓ killed stranded tmux window '$name' ($win)"
      else
        wt_warn "couldn't kill tmux window '$name' ($win) — close it by hand"
      fi
    fi
  done < <(tmux list-windows -t "=$sess" -F $'#{window_id}\t#{window_name}\t#{pane_current_path}' 2>/dev/null || true)
}
cleanup_tmux

# --- conditional post-land background sweep (issue #124) --------------------------
# If the gate that certified the landed tree ran a PRUNED set (a testmon/selected
# green-tree stamp, issue #122), launch the full suite in the background as the
# selection-miss safety net — detached, so the land's exit code and duration stay
# untouched; gate-sweep.sh owns dedupe (a full-stamped tree is never swept), the
# one-sweep-at-a-time lock, the stamp upgrade on green, and the issue filing on
# red. A `full` stamp or no stamp (docs-only skip, --skip-tests) launches
# nothing. Best-effort like the rest of the tail: a sweep that fails to launch
# warns and never fails the land. GATE_SWEEP_BIN is the test seam.
bash "${GATE_SWEEP_BIN:-$SCRIPT_DIR/gate-sweep.sh}" --spawn "$MERGED_SHA" \
  --branch "$WT_BRANCH" ${ISSUE:+--issue} ${ISSUE:+"$ISSUE"} \
  || wt_warn "post-land sweep failed to launch — landing is unaffected"

# --- report -------------------------------------------------------------------------
echo
echo "✓ landed $WT_BRANCH"
echo "  merged:  $MERGED_SHA"
echo "  suite:   $SUITE_RESULT"
echo "  pushed:  origin/$DEFAULT"
# Exit 3 (CLEANUP INCOMPLETE) when main advanced but a teardown step failed — the work IS
# shipped, so the caller must not treat this like a pre-merge failure (#202 I / #198).
if [ -n "$CLEANUP_INCOMPLETE" ]; then
  echo "  cleanup: INCOMPLETE — main advanced but a teardown step failed (see warnings above); finish it by hand"
  exit 3
fi
exit 0
