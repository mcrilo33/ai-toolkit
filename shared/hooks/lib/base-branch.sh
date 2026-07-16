#!/usr/bin/env bash
# base-branch.sh — the ONE canonical resolver for the integration ("base")
# branch (issue #117). Sourced by BOTH sides of the toolkit so they can never
# disagree on where spokes branch from and land into:
#   - the worktree/hub scripts, via worktree-lib.sh (which re-exports it), and
#   - the guard hooks (hub-guard, spoke-main-guard, push-scope-guard), directly
#     from their lib/ dir.
# It is therefore intentionally self-contained: no wt_* / hook-lib dependencies.
#
# Precedence (first non-empty wins):
#   1. git config ai-toolkit.base-branch — per-clone, survives sync, shared by
#      every linked worktree of the clone; honored WITHOUT an existence check
#      (explicit operator intent — existence is the call site's concern).
#   2. AI_TOOLKIT_BASE_BRANCH env — one-shot override (hub-afk.sh keeps its
#      historical AFK_DEFAULT_BRANCH as a deprecated alias on its own side).
#   3. origin/HEAD symbolic ref — what a clone gets for free.
#   4. init.defaultBranch, only when that local ref exists — the tier the guard
#      hooks always had, kept so adopting this resolver changes nothing for them.
#   5. local main, then local master.
#   6. literal "main" — the guards' historical last resort; never fails, so a
#      DENY hook can always name the branch it protects.
#
# Usage: wt_base_branch [root]   (root defaults to the current directory)

# Resolve the base branch name for the repo at $1 (default "."). Always
# succeeds and prints a branch name (see precedence above).
wt_base_branch() {
  local root="${1:-.}" b
  b="$(git -C "$root" config --get ai-toolkit.base-branch 2>/dev/null)" || true
  if [ -z "$b" ] && git -C "$root" config --get ai-toolkit.basebranch >/dev/null 2>&1; then
    # camelCase footgun (issue #309): git flattens a hand-set
    # `ai-toolkit.baseBranch` to the distinct key `basebranch`, which this
    # resolver never reads — so it would silently fall through to origin/HEAD.
    # Warn LOUDLY to stderr (stdout stays the clean branch name) rather than
    # resolve the wrong branch in silence.
    printf 'ai-toolkit: WARNING: git config ai-toolkit.baseBranch is set but the resolver reads ai-toolkit.base-branch (hyphenated); the camelCase key is IGNORED. Run: git config ai-toolkit.base-branch "<branch>"\n' >&2
  fi
  [ -n "$b" ] && { printf '%s' "$b"; return 0; }
  if [ -n "${AI_TOOLKIT_BASE_BRANCH:-}" ]; then
    printf '%s' "$AI_TOOLKIT_BASE_BRANCH"
    return 0
  fi
  b="$(git -C "$root" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null)" || true
  [ -n "$b" ] && { printf '%s' "${b#origin/}"; return 0; }
  b="$(git -C "$root" config --get init.defaultBranch 2>/dev/null)" || true
  if [ -n "$b" ] && git -C "$root" show-ref --verify --quiet "refs/heads/$b" 2>/dev/null; then
    printf '%s' "$b"
    return 0
  fi
  for b in main master; do
    if git -C "$root" show-ref --verify --quiet "refs/heads/$b" 2>/dev/null; then
      printf '%s' "$b"
      return 0
    fi
  done
  printf 'main'
}
