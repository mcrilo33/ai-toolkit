#!/usr/bin/env bash
#
# worktree-land.sh — land a finished task branch from the hub (main checkout).
# The deterministic half of the land skill (`/land <id>`): the hub starts
# and ends tasks; spokes only execute. Run it FROM the hub, on the default
# branch, after the spoke has pushed — never from inside a worktree.
#
# Usage:
#   scripts/worktree-land.sh <issue|slug|branch|path> [--skip-tests] [--local] [--force-land] [--local-gate] [--test-cmd <cmd>]
#
#   <issue|slug|branch|path>  anything that identifies the task worktree
#   --skip-tests              skip the pre-push hook's fast tier on the main push (CI is
#                             still required: it is the gate, issue #378)
#   --local                   micro-spoke path: skip upstream guards and accept a bare
#                             local branch with no registered worktree (the hub's diff
#                             review is the gate; merge+push is what ships the work)
#   --force-land              land a numbered branch that carries no ready/<issue>
#                             completion marker (express/ad-hoc branches that never
#                             emit one); the marker guard is otherwise mandatory
#   --local-gate              offline escape hatch: run the former local full suite once
#                             (-n auto) on the merged tree instead of waiting for CI, and
#                             record that in the land log
#   --test-cmd <cmd>          run <cmd> once on the merged tree instead of waiting for CI
#                             (threads TEST_SELECT_CMD to the pre-push hook)
#
# CI IS THE GATE (issue #378): landing never runs tests itself. The full suite runs in
# CI on every branch push, so a pushed spoke lands only on a green CI run for the exact
# SHA being shipped:
#   * the ready SHA must be CI-green (bounded wait: LAND_CI_WAIT_MAX, default 1200s);
#   * when the default branch is an ancestor of it, the land is a pure fast-forward of
#     that CI-green SHA and the main push skips the local hook;
#   * when the default branch moved, it is merged INTO the spoke branch ON THE SPOKE,
#     pushed, and CI must go green on the new tip before main fast-forwards to it.
#
# Sequence, each step aborting safely on failure:
#   guards  hub on default branch + clean; worktree resolved, clean, fully pushed;
#           numbered branches carry a ready/<issue> marker at their tip (issue #16)
#   gate    CI green for the tip (merging the default branch into the spoke if it moved)
#   merge   --ff-only of the CI-green tip (--local / --local-gate / --test-cmd: a plain
#           `git merge`, gated locally instead)
#   ship    push origin <default>; a rejected push rolls back `git reset --keep`.
#           Then ingest → worker-release → worktree-done.sh (`orca worktree rm`) → `gh issue close`
#   release the spoke's Orca worker (`worker-release`), then tear down via worktree-done.sh --no-hooks
#
set -euo pipefail

WT_PROG="worktree-land"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

# --- land mutex (issue #315) ----------------------------------------------------
# A manual/quick operator land and the drain's auto_land BOTH merge+push $DEFAULT
# through THIS script (auto_land shells out to it), with nothing coordinating them.
# They raced on 2026-07-16: a rejected push's `git reset --keep` left the hub BEHIND
# origin. So every land takes a shared mutex before the merge+push critical section;
# whoever holds it lands, the other WAITS (bounded) and re-checks state. Because ALL
# land paths funnel through here, one lock here serializes them all — manual, /quick,
# /land, and auto_land (transitively).
#
# The lock is a `mkdir` dir (the one atomic, portable FS primitive — macOS has no
# flock(1), the same #300 rationale) at ${AFK_STATE_DIR:-<git-common-dir>/ai-toolkit-afk}
# /land.lock — the shared state dir every actor already uses, resolved inline (this
# script is synced to spoke targets that cannot source hub-skill modules). It holds an
# `owner` file "<pid> <ts> <host>" (ts BEFORE host so ts parses by fixed position even
# if a host ever carried spaces). A crashed holder is broken (dead pid, or a hard age
# bound as a wedged-alive/pid-reuse backstop); a live holder is waited on and the wait
# is LOGGED, never silent (fail-loud, Principle 2). Bounds are env-tunable for tests.
# The land now holds the lock across CI waits (LAND_CI_WAIT_MAX each, up to LAND_SYNC_ROUNDS
# of them, #378), so both bounds must outlast a slow CI run or a live holder is aged out.
: "${LAND_LOCK_WAIT_MAX:=5400}"        # hard cap (s) to wait before failing loud
: "${LAND_LOCK_STALE_SECONDS:=5400}"   # break even a live-looking holder past this age (0 disables)
: "${LAND_LOCK_POLL:=2}"               # poll interval (s) between acquire attempts
: "${LAND_LOCK_PUSH_RETRIES:=1}"       # non-ff push-recovery re-merge+retry attempts (issue #315)
case "$LAND_LOCK_PUSH_RETRIES" in '' | *[!0-9]*) LAND_LOCK_PUSH_RETRIES=1 ;; esac
# Sanitize a garbage value to the SAFE default, never to 0 (Principle 2): a STALE_SECONDS
# silently zeroed would read EVERY live lock as stale and disable the mutex. An EXPLICIT
# numeric 0 is honored as "no age backstop" (dead-pid break only), handled in _land_lock_stale.
case "$LAND_LOCK_WAIT_MAX"      in '' | *[!0-9]*) LAND_LOCK_WAIT_MAX=5400 ;; esac
case "$LAND_LOCK_STALE_SECONDS" in '' | *[!0-9]*) LAND_LOCK_STALE_SECONDS=5400 ;; esac
case "$LAND_LOCK_POLL"          in '' | *[!0-9]* | 0) LAND_LOCK_POLL=2 ;; esac

_LAND_LOCK=""   # the lock dir once WE own it — the release guard's ownership witness

_land_state_dir() {
  if [ -n "${AFK_STATE_DIR:-}" ]; then printf '%s\n' "$AFK_STATE_DIR"; return; fi
  local common
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  case "$common" in /*) ;; *) common="${REPO_ROOT:-.}/$common" ;; esac
  printf '%s\n' "$common/ai-toolkit-afk"
}

_land_lock_owner_pid() {
  local f="$1/owner"
  [ -f "$f" ] || return 0
  awk '{ print $1; exit }' "$f" 2>/dev/null || true
}

# _land_lock_age <lock> -> seconds since the owner ts (field 2), empty when unknown.
_land_lock_age() {
  local f="$1/owner" ts now
  [ -f "$f" ] || return 0
  ts="$(awk '{ print $2; exit }' "$f" 2>/dev/null || true)"
  case "$ts" in '' | *[!0-9]*) return 0 ;; esac
  now="$(date +%s)"
  printf '%s\n' "$(( now - ts ))"
}

# _land_lock_stale <lock> -> rc 0 (stale, break it) ONLY when the owner pid is DEAD
# (probe the real process, Principle 4) or the lock is older than the hard age bound;
# rc 1 (live, wait) otherwise. An ABSENT/torn owner reads NOT-stale (wait): a lock whose
# mkdir just won but whose owner file is a microsecond from being written must never be
# broken by a concurrent waiter (the #315 review's premature-break race). A genuinely
# owner-less orphan (a crash in that microsecond window) is caught by the loud WAIT_MAX
# timeout, not raced. STALE_SECONDS=0 disables the age backstop (dead-pid break only).
_land_lock_stale() {
  local lock="$1" pid age
  pid="$(_land_lock_owner_pid "$lock")"
  case "$pid" in '' | *[!0-9]*) return 1 ;; esac    # no/torn owner -> wait, never break
  kill -0 "$pid" 2>/dev/null || return 0            # holder dead -> stale
  [ "$LAND_LOCK_STALE_SECONDS" -gt 0 ] || return 1  # age backstop disabled -> live holder waits
  age="$(_land_lock_age "$lock")"
  case "$age" in '' | *[!0-9]*) return 1 ;; esac    # unknown age on a live holder -> wait
  [ "$age" -ge "$LAND_LOCK_STALE_SECONDS" ]
}

# acquire_land_lock -> block until this process owns the land lock, then arm the EXIT
# release. Loud + bounded: warns once on first wait, breaks a stale holder and retries,
# and wt_die's if it never wins within LAND_LOCK_WAIT_MAX (a stuck land is surfaced, not
# raced). Call AFTER the cheap hub guards so an arg error never takes or leaks the lock.
acquire_land_lock() {
  local lock waited=0 warned="" host stale_pid aside now
  lock="$(_land_state_dir)/land.lock"
  mkdir -p "$(dirname "$lock")" 2>/dev/null || true
  host="$(hostname 2>/dev/null || printf 'unknown')"
  while :; do
    # Stamp the ts BEFORE mkdir so no `$(date)` fork sits in the mkdir->owner-write gap,
    # shrinking that window (which a concurrent waiter must never mistake for an orphan)
    # to a single builtin printf.
    now="$(date +%s)"
    if mkdir "$lock" 2>/dev/null; then
      printf '%s %s %s\n' "$$" "$now" "$host" > "$lock/owner" 2>/dev/null || true
      _LAND_LOCK="$lock"
      trap '_release_land_lock' EXIT
      return 0
    fi
    if _land_lock_stale "$lock"; then
      # Break ATOMICALLY: `mv` the stale dir aside so that when two waiters break the SAME
      # dead lock only ONE mv wins (the loser's source is already gone). NEITHER reclaims
      # here — both fall through to re-race the mkdir above, the single source of ownership
      # truth, so simultaneous breakers can never both own it (the #315 review's double-break).
      # UPGRADE: a residual micro-TOCTOU remains (a waiter that passed the staleness check
      # could mv a lock a third lander reclaimed in the same instant); it needs a live owner
      # to reclaim within a few instructions and is backstopped by the non-ff push recovery.
      stale_pid="$(_land_lock_owner_pid "$lock")"
      aside="$lock.stale.$$"
      if mv "$lock" "$aside" 2>/dev/null; then
        wt_warn "broke a stale land lock (holder pid ${stale_pid:-unknown} is dead or older than ${LAND_LOCK_STALE_SECONDS}s) — issue #315"
        rm -rf "$aside" 2>/dev/null || true
      fi
      continue
    fi
    if [ -z "$warned" ]; then
      wt_warn "waiting for the land lock (held by pid $(_land_lock_owner_pid "$lock"), $(_land_lock_age "$lock")s) — another land is in progress; serializing to avoid the #315 race"
      warned=1
    fi
    if [ "$waited" -ge "$LAND_LOCK_WAIT_MAX" ]; then
      wt_die "timed out after ${LAND_LOCK_WAIT_MAX}s waiting for the land lock (held by pid $(_land_lock_owner_pid "$lock")). Another land looks stuck — investigate, then re-run."
    fi
    sleep "$LAND_LOCK_POLL" 2>/dev/null || sleep 1
    waited=$(( waited + LAND_LOCK_POLL ))
  done
}

# _release_land_lock -> drop the lock, but ONLY when we still own it: a lock broken as
# stale and reacquired by another lander now belongs to them, so an ownership check
# (owner pid == ours, or a torn/empty owner) prevents us from removing their lock.
_release_land_lock() {
  [ -n "$_LAND_LOCK" ] || return 0
  local pid
  pid="$(_land_lock_owner_pid "$_LAND_LOCK")"
  if [ "$pid" = "$$" ] || [ -z "$pid" ]; then
    rm -rf "$_LAND_LOCK" 2>/dev/null || true
  fi
  _LAND_LOCK=""
}

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
LOCAL=""
FORCE_LAND=""
TEST_CMD=""
LOCAL_GATE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-tests)  SKIP_TESTS=1; shift ;;
    --local)       LOCAL=1; shift ;;
    --force-land)  FORCE_LAND=1; shift ;;
    --local-gate)  LOCAL_GATE=1; shift ;;
    --test-cmd)    [ "$#" -ge 2 ] || wt_die "--test-cmd needs a value"; TEST_CMD="$2"; shift 2 ;;
    --test-cmd=*)  TEST_CMD="${1#--test-cmd=}"; shift ;;
    -*)            wt_die "unknown option: $1 (supported: --skip-tests, --local, --force-land, --local-gate, --test-cmd)" ;;
    *)
      [ -z "$TARGET" ] || wt_die "unexpected extra argument: $1"
      TARGET="$1"; shift
      ;;
  esac
done
[ -n "$TARGET" ] || wt_die "usage: worktree-land.sh <issue|slug|branch|path> [--skip-tests] [--local] [--force-land] [--local-gate] [--test-cmd <cmd>]"
[ -z "$LOCAL_GATE" ] || [ -z "$TEST_CMD" ] || wt_die "--local-gate and --test-cmd conflict (pick one)"
# A local gate that is also told to skip tests would report "ran" over a hook that ran nothing.
[ -z "$SKIP_TESTS" ] || { [ -z "$LOCAL_GATE" ] && [ -z "$TEST_CMD" ]; } \
  || wt_die "--skip-tests conflicts with --local-gate/--test-cmd (a gate that is skipped proves nothing)"
# --local-gate is --test-cmd with the canonical full-suite command (issue #378).
[ -z "$LOCAL_GATE" ] || TEST_CMD="$(wt_local_gate_cmd)"

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

# Take the land mutex (issue #315) now that the cheap hub guards passed: serialize the
# fetch -> sync -> merge -> push critical section against a concurrent land (manual or
# auto_land) so a rejected push can never leave the hub behind origin. Released on EXIT,
# so the lock intentionally spans the post-push teardown/telemetry tail too — a concurrent
# lander waits out the full land, which is correct (never interleave) and bounded by
# LAND_LOCK_WAIT_MAX.
acquire_land_lock

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
  local target="$1" issue marker_sha fetch_ok br br_tip state
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

  # UPGRADE (#278): closes only the PRIMARY, so a packed branch resumed here leaves its
  # subtask issues open. This path CANNOT reuse the main path's ISSUES derivation: that
  # bounds markers to `$DEFAULT..$WT_BRANCH`, and this function's own precondition is that
  # the marker is already merged INTO $DEFAULT — so the range is empty by construction, and
  # the branch may already be pruned. Ancestors-of-marker_sha would find them, but would
  # also sweep in any sibling's marker merged along from $DEFAULT, i.e. exactly the foreign
  # close the main path guards against. Narrow in practice (it needs a land INTERRUPTED
  # mid-teardown, after the merge but before the close, on a PACKED branch), and the
  # leftover issues stay open and visible rather than silently wrong. Worth closing properly
  # once packed branches are common — most likely by recording the shipped set at merge time
  # rather than re-deriving it from refs here.
  #
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

  echo "✓ finalized partially-landed issue #$issue (resumed teardown)"
  return 0
}

# --- guards: the spoke ----------------------------------------------------------
WT_DIR=""
WT_BRANCH=""
if WT_DIR="$(wt_resolve "$TARGET" "$REPO_ROOT")"; then
  # Normal worktree path: the branch is the one Orca lists for it.
  WT_BRANCH="$(wt_branch_of "$WT_DIR" "$REPO_ROOT")"
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

# --- the invariant upstream-precondition exit code (issue #354) ------------------
# The upstream guards below refuse a branch whose push state only the SPOKE can fix
# (never pushed / ahead / behind / --local-but-tracked). This is INVARIANT from the
# hub side: nothing here pushes a spoke branch, so an unattended auto_land that got a
# generic exit-1 die routed it onto the transient retry ladder and re-failed
# identically until the 900s watchdog ceiling (#352). Signal the class with a
# dedicated exit code — distinct from 1 (generic die), 3 (teardown-incomplete), and
# 4 (merge conflict) — so auto_land can escalate the real blocker immediately instead
# of retrying. The stderr text is unchanged (it still names the specific remediation);
# the exit code IS the machine contract, so a non-numeric override falls back to 5.
: "${WT_LAND_PRECONDITION_EXIT:=5}"
case "$WT_LAND_PRECONDITION_EXIT" in '' | *[!0-9]*) WT_LAND_PRECONDITION_EXIT=5 ;; esac
wt_land_precondition_die() {
  printf '%s: %s\n' "${WT_PROG:-worktree}" "$*" >&2
  exit "$WT_LAND_PRECONDITION_EXIT"
}

if [ -z "$LOCAL" ]; then
  # A failed fetch is FATAL, not a warning (issue #195): the ahead/behind guards
  # below would otherwise run against the LAST-KNOWN origin/<branch> — a spoke
  # push the hub never fetched reads behind=0, the land proceeds, and teardown
  # deletes origin/<branch> with the remote-only commits. Destructive decisions
  # never run on stale remote state. One immediate retry absorbs a transient
  # blip (the #119 SSH-staleness class — a fresh short connection usually
  # clears it) so an unattended /afk land isn't escalated blocked/<N> over a
  # moment of network noise; a real outage still dies. A fetch failure is
  # TRANSIENT (network), so it keeps the generic exit-1 die — not the invariant
  # precondition code below.
  git fetch origin --quiet 2>/dev/null \
    || git fetch origin --quiet 2>/dev/null \
    || wt_die "fetch from origin failed — refusing to land on last-known remote state (a spoke push this checkout never fetched would be silently pruned). Restore connectivity and re-run."
  # Upstream guards: the spoke's push is its ship gate. Each is an INVARIANT the hub
  # cannot clear, so it exits WT_LAND_PRECONDITION_EXIT (issue #354), not 1.
  UPSTREAM="$(git rev-parse --symbolic-full-name "${WT_BRANCH}@{upstream}" 2>/dev/null || true)"
  [ -n "$UPSTREAM" ] || wt_land_precondition_die "branch $WT_BRANCH has never been pushed — the spoke's push is its ship gate"
  AHEAD="$(git rev-list --count "${UPSTREAM}..${WT_BRANCH}")"
  [ "$AHEAD" -eq 0 ] || wt_land_precondition_die "branch $WT_BRANCH is $AHEAD commit(s) ahead of $UPSTREAM — push from the spoke first"
  # Behind is just as fatal as ahead: landing a reduced local branch would later
  # prune the remote ref and silently lose the commits only the remote still has.
  BEHIND="$(git rev-list --count "${WT_BRANCH}..${UPSTREAM}")"
  [ "$BEHIND" -eq 0 ] || wt_land_precondition_die "branch $WT_BRANCH is $BEHIND commit(s) behind $UPSTREAM — the remote has work this checkout lacks; reconcile on the spoke first"
else
  # --local is for micro-spokes, which never push. A branch WITH an upstream is
  # not a micro-spoke: skipping the behind guard could merge a reduced local tip
  # and later prune the remote ref, losing the commits only the remote still has.
  # This is the OTHER half of the #354 --local-vs-upstream deadlock: a drain branch
  # tracks origin/main (created with branch.<b>.merge=refs/heads/main), so the ahead
  # guard above refuses `worktree-land <issue>` AND this guard refuses
  # `worktree-land <issue> --local` — no worktree-land invocation can land it. That
  # is intentional and now surfaced STRUCTURALLY (the same precondition exit code):
  # it is resolvable only by the spoke pushing, never by the hub.
  ! git rev-parse --symbolic-full-name "${WT_BRANCH}@{upstream}" >/dev/null 2>&1 \
    || wt_land_precondition_die "branch $WT_BRANCH has an upstream — not a micro-spoke; land it without --local"
fi

# --- heal a hub left behind origin (issue #315) ---------------------------------
# Under the land lock, and with origin freshly fetched, fast-forward local $DEFAULT up to
# origin/$DEFAULT when it is strictly BEHIND — the exact state a prior racy land's
# `git reset --keep` produced (#315). Doing it here, before the ISSUES range derivation and
# the merge, makes PRE_SHA the current remote tip so the push is a clean fast-forward and the
# hub is never left behind. A hub carrying its OWN local commits (ahead of / diverged from
# origin, e.g. a not-yet-pushed hub change) is NOT an ancestor of origin/$DEFAULT, so this
# skips it and the normal merge+push handles it (a genuine non-ff at push is recovered below).
ORIGIN_DEFAULT="refs/remotes/origin/$DEFAULT"
land_heal_behind() {
  [ -z "$LOCAL" ] || return 0
  git rev-parse -q --verify "$ORIGIN_DEFAULT" >/dev/null 2>&1 || return 0
  local head origin
  head="$(git rev-parse HEAD)"
  origin="$(git rev-parse "$ORIGIN_DEFAULT")"
  [ "$head" != "$origin" ] || return 0
  git merge-base --is-ancestor "$head" "$origin" 2>/dev/null || return 0   # ahead/diverged -> leave it
  echo "→ fast-forwarding local $DEFAULT up to origin/$DEFAULT (behind by an origin advance, issue #315)"
  git merge --ff-only "$ORIGIN_DEFAULT" >/dev/null \
    || wt_die "could not fast-forward local $DEFAULT to origin/$DEFAULT — reconcile by hand, then re-run"
}
land_heal_behind

# Issue number = leading number of the branch slug (feature/42-foo → 42);
# ad-hoc branches have none and skip the issue close.
BSLUG="${WT_BRANCH##*/}"
ISSUE="${BSLUG%%-*}"
[[ "$ISSUE" =~ ^[0-9]+$ ]] || ISSUE=""

# --- every issue this branch ships (issue #278) -------------------------------
# A packed spoke carries several same-scope issues on ONE branch, so the branch slug names
# only the PRIMARY. Closing that scalar alone would leave the shipped subtasks open forever
# with nothing recording that they were done.
#
# The per-subtask ready/<N> markers ARE that record — but they are NOT all at the tip. The
# spoke emits ready/<subtask> as each one lands and defers ready/<primary> to LAST (the
# at-tip marker guard below requires it there), so every earlier subtask's marker sits on an
# ANCESTOR commit. A `--points-at HEAD` scan would therefore find only the final subtask and
# the primary, silently dropping the rest of a 3+ chain.
#
# So: markers REACHABLE FROM the tip, bounded to this branch's OWN commits via
# `$DEFAULT..HEAD`. The bound is load-bearing — an un-landed ready/<X> from a sibling spoke
# that already reached $DEFAULT is reachable from our tip once main is merged in, and closing
# it would mark someone else's in-flight issue done. `git tag --merged` cannot express that
# (it has no exclusion side), hence the explicit rev-list membership test.
#
# UPGRADE: the range reads the hub's LOCAL $DEFAULT. The hub lands serially and this runs
# after the merge guards, so local $DEFAULT is current in practice — but nothing here proves
# it. A local ref lagging origin/$DEFAULT would widen the range to commits already on the
# remote, and a sibling's still-open ready/<X> among them would be closed as ours. Pin it
# with `git fetch && git merge --ff-only origin/$DEFAULT` before this block if the hub ever
# lands concurrently, or if a lagging local default is ever observed in the wild.
#
# ISSUES always leads with the primary, so the close comment order matches the branch's own
# story, and stays a single element for the overwhelmingly common unpacked spoke.
ISSUES=()
if [ -n "$ISSUE" ]; then
  ISSUES=("$ISSUE")
  # The commits this branch adds on top of the default branch — empty for an already-merged
  # or empty branch, which just leaves ISSUES at the primary.
  BRANCH_COMMITS="$(git rev-list "$DEFAULT..$WT_BRANCH" 2>/dev/null || true)"
  if [ -n "$BRANCH_COMMITS" ]; then
    while IFS= read -r _tag; do
      [ -n "$_tag" ] || continue
      _n="${_tag#ready/}"
      case "$_n" in '' | *[!0-9]*) continue ;; esac
      [ "$_n" = "$ISSUE" ] && continue                  # the primary is already first
      _tag_sha="$(git rev-parse -q --verify "refs/tags/$_tag^{commit}" 2>/dev/null || true)"
      [ -n "$_tag_sha" ] || continue
      printf '%s\n' "$BRANCH_COMMITS" | grep -qxF "$_tag_sha" || continue   # not ours
      ISSUES+=("$_n")
    done < <(git for-each-ref --format='%(refname:short)' 'refs/tags/ready/*' 2>/dev/null || true)
  fi
  [ "${#ISSUES[@]}" -gt 1 ] && echo "→ branch ships ${#ISSUES[@]} issues: ${ISSUES[*]}"
fi

# --- guard: the ready-to-land marker (issue #16) --------------------------------
# A per-subtask push is indistinguishable from task completion. For a numbered,
# pushed branch, require an explicit ready/<issue> tag at the branch tip before
# landing — otherwise a spoke caught between subtasks (clean + pushed) would be
# landed as a finished issue and have its worktree torn down. The tag is shared
# between hub and spoke worktrees, so a marker the spoke set is visible here.
# Exempt: --local micro-spokes (never push, no marker), ad-hoc/non-numbered
# branches (their one push IS completion), and --force-land (explicit override).
if [ -z "$LOCAL" ] && [ -z "$FORCE_LAND" ] && [ -n "$ISSUE" ]; then
  MARKER="ready/${ISSUE}"
  MARKER_SHA="$(git rev-parse -q --verify "refs/tags/${MARKER}^{commit}" 2>/dev/null || true)"
  TIP_SHA="$(git rev-parse -q --verify "refs/heads/${WT_BRANCH}" 2>/dev/null || true)"
  if [ -z "$MARKER_SHA" ]; then
    wt_die "branch $WT_BRANCH carries no ${MARKER} marker — it looks mid-task (pushed but not signalled complete). Emit it on the spoke after the FINAL subtask's push (git tag ${MARKER} && git push origin ${MARKER}), or pass --force-land for a branch that never carries one."
  elif [ "$MARKER_SHA" != "$TIP_SHA" ]; then
    wt_die "${MARKER} marker is stale (points at ${MARKER_SHA:0:9}, branch tip is ${TIP_SHA:0:9}) — the spoke pushed more work after signalling complete. Re-tag at the tip on the spoke (git tag -f ${MARKER} && git push -f origin ${MARKER}), or pass --force-land."
  fi
fi

# --- gate + merge (issue #378: CI is the gate) -----------------------------------
# CI mode (a pushed spoke, no --local-gate/--test-cmd): the ready tip must be CI-green,
# the default branch is merged into the spoke on the spoke when it moved (re-waiting for
# CI on the new tip), and the hub then only FAST-FORWARDS to a CI-green SHA. The other
# modes (--local micro-spokes, --local-gate, --test-cmd) merge on the hub and are gated
# locally by the pre-push hook instead.
#
# A DETERMINISTIC merge conflict is a distinct failure from a transient push rejection
# (issue #285): a conflict is a pure function of the two tips, so an unattended auto_land
# must route it to a resolution lane rather than blind-retry the identical land. Signal it
# with a dedicated exit code (WT_LAND_CONFLICT_EXIT, default 4 — 1 is the generic die and 3
# is cleanup-incomplete) plus a machine-readable CONFLICT marker naming the conflicting
# file(s), captured BEFORE `git merge --abort` wipes the unmerged index entries. The exit
# code IS the machine contract, so a non-numeric override falls back to 4.
: "${WT_LAND_CONFLICT_EXIT:=4}"
case "$WT_LAND_CONFLICT_EXIT" in '' | *[!0-9]*) WT_LAND_CONFLICT_EXIT=4 ;; esac
CI_MODE=""
[ -n "$LOCAL" ] || [ -n "$TEST_CMD" ] || CI_MODE=1
AUTO_SKIP=""        # the main push skips the hook: CI already proved this exact SHA
LOCAL_GATE_UNPROVEN=""
CI_URL=""
PRE_SHA="$(git rev-parse HEAD)"

land_merge_conflict() {
  local dir="$1" files
  # `|| true`: guard the capture under set -e so a nonzero pipeline can never abort the
  # script BEFORE the abort + exit below (which guarantee a clean tree + the conflict code).
  files="$(git -C "$dir" diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ' ' || true)"
  files="${files% }"
  git -C "$dir" merge --abort 2>/dev/null || true
  printf '%s: CONFLICT %s\n' "$WT_PROG" "$files" >&2
  printf '%s: merge of %s conflicts with %s on: %s — rebase the branch on %s (on the spoke, then push, when it has one) and re-run\n' \
    "$WT_PROG" "$WT_BRANCH" "$DEFAULT" "${files:-(unknown)}" "$DEFAULT" >&2
  # #300 writer: the landing attempt ended in a deterministic conflict. Recorded so a
  # reader can tell "land tried and needs resolution" from "land never ran" (#285).
  wt_tlog_transition "$ISSUE" land_failed worktree-land.sh "merge conflict" \
    "{\"conflicts\":\"${files:-unknown}\"}"
  exit "$WT_LAND_CONFLICT_EXIT"
}

# land_require_ci_green <sha> -> return once CI is green for <sha> (CI_URL set), else die
# with the reason from wt_ci_refusal. A RED run is an invariant only the spoke can fix, so
# it exits the precondition code (#354) and auto_land escalates it instead of retrying;
# pending at the bound / no run / no gh are transient or environmental (exit 1).
land_require_ci_green() {
  local sha="$1" rc=0 why
  wt_ci_check "$sha" "${LAND_CI_WAIT_MAX:-1200}" || rc=$?
  if [ "$rc" -eq 0 ]; then
    CI_URL="$WT_CI_URL"
    echo "→ CI is green for ${sha:0:9} ($CI_URL)"
    return 0
  fi
  why="$(wt_ci_refusal "$rc")"
  wt_tlog_transition "$ISSUE" land_failed worktree-land.sh "$why" "{\"sha\":\"${sha:0:9}\"}"
  [ "$rc" -ne 1 ] || wt_land_precondition_die "$why — fix it on the spoke, push, and re-emit ready"
  wt_die "$why — nothing landed. Re-run once CI settles, or pass --local-gate to run the suite locally."
}

# land_move_ready_markers <old> <new> -> re-point every ready/<N> tag at <old> to <new>,
# keeping its annotation (the force/local-gate audit notes live in the body), and re-push it.
land_move_ready_markers() {
  local t msg
  while IFS= read -r t; do
    [ "$(git rev-parse -q --verify "refs/tags/$t^{commit}" 2>/dev/null)" = "$1" ] || continue
    msg="$(git tag -l --format='%(contents)' "$t")"
    git tag -f -a "$t" -m "$msg" "$2" >/dev/null \
      && ( export TEST_SELECT_SKIP=1; wt_git_push -f origin "refs/tags/$t" >/dev/null 2>&1 ) \
      || wt_warn "couldn't move $t to the merged tip ${2:0:9} — re-tag it on the spoke before re-landing"
  done < <(git for-each-ref --format='%(refname:short)' 'refs/tags/ready/*' 2>/dev/null || true)
}

# land_sync_with_default -> make the spoke tip contain the default branch. While it does
# not: merge the default branch INTO the spoke ON THE SPOKE (conflicts are the spoke's to
# resolve), push the spoke, and wait for CI on the new tip — so the hub's merge is a pure
# fast-forward of a CI-green SHA (land-resolve-on-spoke-then-FF). Bounded rounds guard a
# default branch that keeps moving under the land.
land_sync_with_default() {
  local round=0 tip old
  while ! git merge-base --is-ancestor "$DEFAULT" "refs/heads/$WT_BRANCH"; do
    round=$(( round + 1 ))
    [ "$round" -le "${LAND_SYNC_ROUNDS:-3}" ] \
      || wt_die "$DEFAULT kept moving through ${LAND_SYNC_ROUNDS:-3} spoke merges — nothing landed; re-run"
    echo "→ $DEFAULT moved past $WT_BRANCH — merging it into the branch on the spoke, then waiting for CI (round $round)"
    old="$(git rev-parse "refs/heads/$WT_BRANCH")"
    git -C "$WT_DIR" merge --no-edit "$DEFAULT" || land_merge_conflict "$WT_DIR"
    tip="$(git rev-parse "refs/heads/$WT_BRANCH")"
    # CI is this SHA's gate, so the spoke push skips the hook's fast tier (and its minutes).
    if ! ( export TEST_SELECT_SKIP=1; cd "$WT_DIR" && wt_git_push origin "$WT_BRANCH" ); then
      # Undo the unpushed merge so the spoke is exactly what it was (pushed, tip == upstream):
      # a re-land must not trip the "ahead of upstream" precondition on our own commit.
      git -C "$WT_DIR" reset --keep "$old" >/dev/null 2>&1 || true
      wt_die "pushing the merged $WT_BRANCH failed — nothing landed; re-run"
    fi
    # The land moved the tip, so the land records it (Principle 1): ready/<N> markers sitting
    # at the old tip follow it, else a re-land after a CI timeout reads a "stale" marker.
    land_move_ready_markers "$old" "$tip"
    land_require_ci_green "$tip"
  done
}

# land_merge_into_default -> put the branch on the default branch (sets MERGED_SHA).
land_merge_into_default() {
  if [ -n "$CI_MODE" ]; then
    land_sync_with_default
    git merge --ff-only "$WT_BRANCH" >/dev/null \
      || wt_die "could not fast-forward $DEFAULT to $WT_BRANCH — nothing landed; re-run"
  elif ! git merge --no-edit "$WT_BRANCH"; then
    land_merge_conflict "$REPO_ROOT"
  fi
  MERGED_SHA="$(git rev-parse HEAD)"
}

# #300 writer, INTENT-FIRST: record `landing` BEFORE the gate and merge start. This is the
# state the design table calls literally unlearnable today, and its absence caused
# #290: a land consumes the ready tag and kills the spoke's window BEFORE removing
# the worktree, so for that window the watchdog sees "no marker, dead pane" and
# fires dead-pane on a spoke that is being landed successfully. With `landing`
# recorded up front, that read becomes "landing, 40s in" — the false-fire has no
# room to exist (and a CI wait can now be minutes long). Recorded even if the gate
# or merge then fails: an explicit in-flight state with an onset is exactly the point.
echo "→ landing $WT_BRANCH into $DEFAULT"
wt_tlog_transition "$ISSUE" landing worktree-land.sh "merge $WT_BRANCH into $DEFAULT" \
  "{\"branch\":\"$WT_BRANCH\",\"base\":\"$PRE_SHA\"}"
if [ -n "$CI_MODE" ]; then
  land_require_ci_green "$(git rev-parse "refs/heads/$WT_BRANCH")"
  AUTO_SKIP=1
fi
land_merge_into_default

# Roll the hub back to its pre-merge tip. A failed reset leaves the hub on the merged
# tip — unrecoverable here, so die with by-hand instructions. Shared by the land abort
# paths (missing-hook, push-rejection) so the recovery command and its guidance never
# drift across copies (issue #196).
land_reset_keep_or_die() {
  git reset --keep "$PRE_SHA" \
    || wt_die "rollback failed — hub is still on the merged tip; reset by hand: git reset --keep $PRE_SHA"
}

# --- ship: push main (issue #378) ------------------------------------------------------
# A CI-green push skips the hook (CI already proved this exact SHA); --skip-tests skips its
# fast tier too; --test-cmd/--local-gate thread the command to the hook, the single executor
# of local tests. A rejected push rolls the merge back, so a failed land leaves a clean hub.
#
# The pre-push hook IS the local test gate. If a gate is REQUIRED here (not a skipped /
# CI-proven land) and no executable hook is installed, the push would run NOTHING — a
# missing enforcement precondition silently shipping untested code to $DEFAULT (the #187
# fail-open shape, issue #196). ABORT: roll the merge back and die with the install
# command, so landing ungated is only ever a visible flag, never an environmental accident.
if [ -z "$SKIP_TESTS" ] && [ -z "$AUTO_SKIP" ]; then
  PREPUSH_HOOK="$(git rev-parse --git-path hooks/pre-push 2>/dev/null || true)"
  if [ -z "$PREPUSH_HOOK" ] || [ ! -x "$PREPUSH_HOOK" ]; then
    wt_warn "no executable pre-push hook here — the test gate cannot run; rolling back: git reset --keep $PRE_SHA"
    land_reset_keep_or_die
    wt_die "landing aborted: the pre-push test gate is REQUIRED but no executable hook is installed here, so the push would ship untested code to $DEFAULT. Install it with scripts/install-git-hooks.sh, or land ungated on purpose with --skip-tests. Nothing was pushed."
  fi
fi
echo "→ pushing $DEFAULT to origin"

# One ship attempt. The subshell scopes the exports; the push routes through wt_git_push
# so the SSH connection is kept alive across a long local gate (issue #119).
land_push() {
  (
    if [ -n "$SKIP_TESTS" ] || [ -n "$AUTO_SKIP" ]; then
      export TEST_SELECT_SKIP=1
    fi
    # An inherited skip would make the hook exit 0 without running the local gate.
    if [ -n "$TEST_CMD" ]; then unset TEST_SELECT_SKIP; export TEST_SELECT_CMD="$TEST_CMD"; fi
    wt_git_push origin "$DEFAULT"
  )
}

land_rollback() {
  rm -f "$PUSH_LOG"
  wt_warn "$1 — rolling back: git reset --keep $PRE_SHA"
  land_reset_keep_or_die
  # Never leave the hub BEHIND origin (issue #315): if origin advanced under us, re-fetch and
  # ff local $DEFAULT back up to it (best-effort) so even a FAILED land is at origin, not behind.
  if [ -z "$LOCAL" ]; then
    git fetch origin --quiet 2>/dev/null || true
    land_heal_behind || true
  fi
  wt_die "landing aborted; nothing was pushed. Fix on the branch (push from the spoke when it has one) and re-run."
}

# land_push_remote_advanced -> rc 0 when PUSH_LOG carries a signature that origin/$DEFAULT moved
# UNDER us specifically: a non-fast-forward / fetch-first rejection, or the compare-and-swap
# "cannot lock ref ... is at X but expected Y" a concurrent push to the same ref produces. Kept
# NARROW on purpose: a bare "[remote rejected]" / "failed to update ref" also accompanies a
# server-side POLICY decline (a pre-receive hook), which a re-fetch+retry can never fix and must
# still roll back (issue #315). A pre-push GATE failure exits before transfer and matches none.
land_push_remote_advanced() {
  grep -qiE 'non-fast-forward|fetch first|cannot lock ref' "$PUSH_LOG"
}

# land_nonff_recover -> a push the remote refused because origin advanced under us, reconciled
# and retried under the still-held land lock — up to LAND_LOCK_PUSH_RETRIES times (issue #315).
# Each attempt: roll our merge back, re-fetch, ff local $DEFAULT to the NEW origin tip, redo the
# gate + merge (CI mode merges the new tip into the spoke and waits for CI on it again; a
# conflict exits with the #285 contract), and re-push. Updates PRE_SHA/MERGED_SHA and returns 0
# when a retry lands; returns 1 (caller rolls back) on a fetch/ff failure, a non-advance push
# failure, or exhausted retries.
land_nonff_recover() {
  local attempt=0
  while [ "$attempt" -lt "$LAND_LOCK_PUSH_RETRIES" ]; do
    attempt=$(( attempt + 1 ))
    wt_warn "push refused: origin/$DEFAULT advanced under the land — reconciling and retrying under the lock (attempt $attempt/$LAND_LOCK_PUSH_RETRIES, issue #315)"
    git reset --keep "$PRE_SHA" || { wt_warn "recovery: reset --keep failed"; return 1; }
    git fetch origin --quiet 2>/dev/null || git fetch origin --quiet 2>/dev/null \
      || { wt_warn "recovery: re-fetch from origin failed"; return 1; }
    git merge --ff-only "$ORIGIN_DEFAULT" >/dev/null 2>&1 \
      || { wt_warn "recovery: could not fast-forward to origin/$DEFAULT"; return 1; }
    PRE_SHA="$(git rev-parse HEAD)"
    land_merge_into_default
    PUSH_RC=0
    land_push 2>&1 | tee "$PUSH_LOG" || PUSH_RC=$?
    [ "$PUSH_RC" -ne 0 ] || return 0
    land_push_remote_advanced || { wt_warn "recovery: retry push failed for a non-advance reason"; return 1; }
    # origin advanced AGAIN — loop for another bounded attempt
  done
  wt_warn "recovery: exhausted $LAND_LOCK_PUSH_RETRIES retry attempt(s) — origin keeps advancing under the land (issue #315)"
  return 1
}

# Capture the push's combined output while streaming it live: tee exits 0 and
# pipefail is on, so PUSH_RC is git's own exit code and the capture file is complete
# when the pipeline returns.
PUSH_LOG="$(mktemp "${TMPDIR:-/tmp}/wt-land-push.XXXXXX")"
PUSH_RC=0
land_push 2>&1 | tee "$PUSH_LOG" || PUSH_RC=$?
if [ "$PUSH_RC" -ne 0 ]; then
  if land_push_remote_advanced; then
    # origin/$DEFAULT advanced under us (a push race despite the lock — a non-honoring pusher):
    # reconcile + retry under the lock rather than rolling back behind origin (issue #315). If
    # recovery can't complete, land_rollback re-syncs to origin so the hub is never left behind.
    land_nonff_recover \
      || land_rollback "push rejected: origin advanced under the land and automatic recovery could not complete (issue #315)"
  else
    land_rollback "push rejected (pre-push test gate or remote)"
  fi
fi
# A local gate is only a gate if the hook really ran it: the installed hook also exits 0 when
# the toolkit or the test-select hook is disabled or a persistent skip is configured. The
# land already pushed, so this can only be loud, not roll back — but it must never write
# "full suite ran" over a hook that ran nothing (Principle 2).
if [ -n "$TEST_CMD" ] && ! grep -q 'running custom suite (TEST_SELECT_CMD)' "$PUSH_LOG"; then
  wt_warn "the pre-push hook did not report running the local gate (disabled or skipped by config?) — the suite may NOT have run for this land"
  LOCAL_GATE_UNPROVEN=1
fi
rm -f "$PUSH_LOG"

# What gated this land, for the issue-close comment and the report (computed from the FINAL
# merged SHA: a recovery re-merge may have moved it).
if [ -n "$AUTO_SKIP" ]; then
  SUITE_RESULT="no local tests — CI green for ${MERGED_SHA:0:9} ($CI_URL), issue #378"
elif [ -n "$SKIP_TESTS" ]; then
  SUITE_RESULT="skipped (--skip-tests)"
elif [ -n "$LOCAL_GATE" ]; then
  SUITE_RESULT="local full suite via the pre-push hook (--local-gate); CI not consulted${LOCAL_GATE_UNPROVEN:+ — UNVERIFIED: the hook did not report running it}"
elif [ -n "$TEST_CMD" ]; then
  SUITE_RESULT="via pre-push hook (--test-cmd: $TEST_CMD)"
else
  SUITE_RESULT="via pre-push hook (fast tier; --local land, issue #378)"
fi

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
# worker release / worktree teardown stops the spoke (dropping in-flight spans) and removes
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
  # Thread the PRE-MERGE default tip (issue #344): the ingest dumps the spoke's commit range
  # for the #162/#280/#231 enrichments, but it runs here AFTER the merge pushed and advanced
  # origin/main, so its own origin/main..HEAD range is empty (worktrees share remote-tracking
  # refs). PRE_SHA is the default tip captured before the merge (line ~565), so PRE_SHA..HEAD
  # captures the spoke's commits. The ingest resolve-or-skips it and stays best-effort.
  AI_TOOLKIT_COMMIT_BASE="$PRE_SHA" \
    bash "$SCRIPT_DIR/telemetry-ingest-spoke.sh" "$WT_DIR" ${REBUILD_VIEW:+"$REBUILD_VIEW"} \
    || wt_warn "post-run Langfuse ingestion errored — landing continues"
fi

# From here on main has ADVANCED (origin/$DEFAULT moved). A teardown step that fails now must
# NOT die with the generic exit 1 (which auto_land reads as "land failed" and stamps blocked
# over already-merged code, issue #198): track it and exit the CLEANUP-INCOMPLETE sentinel (3)
# at the end so the caller can tell "nothing shipped" (1) from "shipped, cleanup incomplete" (3).
CLEANUP_INCOMPLETE=""
if [ -n "$WT_DIR" ]; then
  # Release the spoke's Orca worker after the ingest, before the removal: its terminal closes and the
  # OTel exporter stops (the old tmux reap's job, #273). No recorded dispatch => nothing to release.
  DISPATCH_ID="$(ai_toolkit_identity_get orca_dispatch_id "$WT_DIR" 2>/dev/null || true)"
  if [ -n "$DISPATCH_ID" ]; then
    orca_json orchestration worker-release --dispatch "$DISPATCH_ID" \
      || wt_warn "orca worker-release failed for dispatch $DISPATCH_ID -- continuing: ${ORCA_ERR:-$ORCA_OUT}"
  fi
  # WT_DONE seams the teardown for tests (default: the sibling worktree-done.sh). --no-hooks: this land
  # already ingested, so Orca's archive hook would ingest the spoke a second time. A failure is
  # post-ship residue, not a land failure -- warn and flag the sentinel, never abort under set -e.
  bash "${WT_DONE:-$SCRIPT_DIR/worktree-done.sh}" "$WT_DIR" --no-hooks \
    || { wt_warn "worktree-done teardown failed for $WT_DIR — main already advanced ($MERGED_SHA is on $DEFAULT); finish the teardown by hand"; CLEANUP_INCOMPLETE=1; }
else
  # Bare-branch mode: the worktree is already gone; just delete the merged local branch.
  # Safe — just merged; warn rather than abort if deletion fails.
  git branch -d "$WT_BRANCH" \
    || wt_warn "couldn't delete local branch $WT_BRANCH — delete it by hand: git branch -d $WT_BRANCH"
fi

# Consume the ready/<issue> completion marker (issue #16): the work is landed,
# so the tag has done its job. Leaving it behind would let a stale marker
# re-flag a future branch reusing the issue number as mergeable. Local then
# remote, warn-only — a missing tag (--force-land, ad-hoc) is a no-op.
# Looped over ISSUES (#278): a packed branch's SUBTASK markers must be consumed too — this
# block's own reason applies to each of them. A lingering ready/265 would re-flag a future
# branch reusing that number as mergeable, which is exactly the stale-marker hazard here.
if [ -n "$ISSUE" ] && [ -z "$LOCAL" ]; then
  for LAND_ISSUE in "${ISSUES[@]}"; do
    MARKER="ready/${LAND_ISSUE}"
    if git rev-parse -q --verify "refs/tags/${MARKER}" >/dev/null 2>&1; then
      git tag -d "$MARKER" >/dev/null 2>&1 \
        || wt_warn "couldn't delete local tag $MARKER — delete it by hand: git tag -d $MARKER"
      wt_git_push origin ":refs/tags/${MARKER}" >/dev/null 2>&1 \
        || wt_warn "couldn't delete remote tag $MARKER — delete it by hand: git push origin :refs/tags/$MARKER"
    fi
  done
fi

# Looped over ISSUES (#278): every issue the branch shipped is closed and has its lifecycle
# labels cleared, not just the primary the slug happens to name.
if [ -n "$ISSUE" ]; then
  if command -v gh >/dev/null 2>&1; then
    for LAND_ISSUE in "${ISSUES[@]}"; do
      if gh issue close "$LAND_ISSUE" --comment "Landed on $DEFAULT in $MERGED_SHA (suite: $SUITE_RESULT)."; then
        echo "✓ closed issue #$LAND_ISSUE"
      else
        wt_warn "couldn't close issue #$LAND_ISSUE — close it by hand: gh issue close $LAND_ISSUE"
      fi
      # Mirror teardown (issue #236): the spoke no longer has live local state, so
      # strip the status:*/mode:*/lane:* labels dispatch stamped. Best-effort and
      # separate from the close comment above (which is unchanged): a failed gh here
      # never fails the land.
      wt_gh_clear_lifecycle_labels "$LAND_ISSUE"
    done
  else
    wt_warn "gh not found — close issue(s) #${ISSUES[*]} by hand"
  fi
fi

# The spoke is landed and its worktree gone, so any queued-subtask channel it still carries
# (#278) is dead state. Drop it: a leftover queue keyed on this primary would refuse the
# terminal ready of a LATER, unrelated spoke that reused the issue number. The path contract
# is inlined (gate-broker-markers.sh is a hub-skill module this script cannot source from a
# synced target), matching how worktree-new.sh seeds it. Best-effort — never fails the land.
if [ -n "$ISSUE" ]; then
  _q_common="$(git rev-parse --git-common-dir 2>/dev/null || printf '.git')"
  case "$_q_common" in /*) ;; *) _q_common="$REPO_ROOT/$_q_common" ;; esac
  rm -rf "${AFK_STATE_DIR:-$_q_common/ai-toolkit-afk}/queued-$ISSUE" 2>/dev/null || true
  unset _q_common
fi


# #300 writer: terminal success. Today `landed` is recorded only as ABSENCES (tag
# consumed, worktree gone, issue closed); an explicit record closes the lifecycle.
wt_tlog_transition "$ISSUE" landed worktree-land.sh "merged into $DEFAULT" \
  "{\"merged\":\"$MERGED_SHA\"}"

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
