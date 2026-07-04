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
  else
    HINT="pass one of the paths above, or its issue number / slug / branch."
    [ -n "$LOCAL" ] && HINT="$HINT (--local also accepts a full local branch name)"
    wt_warn "no single worktree matches '$TARGET'. Existing task worktrees:"
    wt_print_worktrees "$REPO_ROOT"
    wt_die "$HINT"
  fi
fi

if [ -z "$LOCAL" ]; then
  git fetch origin --quiet 2>/dev/null \
    || wt_warn "fetch failed — ahead/behind checks use the last-known remote state"
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

# --- ship: push main; the pre-push hook is the single test gate (issue #19) -------
# --skip-tests / --test-cmd are threaded to the hook via TEST_SELECT_*, so the
# hook stays the single executor. A rejected push — the gate failing, or a remote
# refusal — rolls the merge back, so a failed land always leaves a clean hub.
if [ -n "$SKIP_TESTS" ]; then
  SUITE_RESULT="skipped (--skip-tests)"
elif [ -n "$AUTO_SKIP" ]; then
  SUITE_RESULT="skipped (clean fast-forward of an already-gated tree, issue #96)"
elif [ -n "$TEST_CMD" ]; then
  SUITE_RESULT="via pre-push hook (--test-cmd: $TEST_CMD)"
else
  SUITE_RESULT="via pre-push hook (tiered)"
fi
# The pre-push hook IS the test gate (issue #19). If it is not installed here,
# the push runs NOTHING — warn so a green land is never mistaken for a tested
# one, and report it honestly rather than claiming the gate ran.
if [ -z "$SKIP_TESTS" ] && [ -z "$AUTO_SKIP" ]; then
  PREPUSH_HOOK="$(git rev-parse --git-path hooks/pre-push 2>/dev/null || true)"
  if [ -z "$PREPUSH_HOOK" ] || [ ! -x "$PREPUSH_HOOK" ]; then
    wt_warn "no executable pre-push hook here — the test gate will NOT run on this push; install it with scripts/install-git-hooks.sh"
    SUITE_RESULT="NOT RUN — no pre-push hook installed"
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
  git reset --keep "$PRE_SHA" \
    || wt_die "rollback failed — hub is still on the merged commit; reset by hand: git reset --keep $PRE_SHA"
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
    wt_warn "gate ran green but the push transport died (SSH staleness, issue #119) — retrying ONCE with TEST_SELECT_SKIP=1"
    if ! land_push retry; then
      land_rollback "retry push failed too"
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
  bash "$SCRIPT_DIR/telemetry-ingest-spoke.sh" "$WT_DIR" \
    || wt_warn "post-run Langfuse ingestion errored — landing continues"
fi

if [ -n "$WT_DIR" ]; then
  bash "$SCRIPT_DIR/worktree-done.sh" "$WT_DIR" ${KEEP_BRANCH:+--keep-branch}
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

# --- report -------------------------------------------------------------------------
echo
echo "✓ landed $WT_BRANCH"
echo "  merged:  $MERGED_SHA"
echo "  suite:   $SUITE_RESULT"
echo "  pushed:  origin/$DEFAULT"
exit 0
