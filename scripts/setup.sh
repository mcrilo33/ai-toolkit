#!/usr/bin/env bash
# Orca setup hook (orca.yaml scripts.setup). Runs in the NEW worktree before the agent starts
# (wait-for-setup), so a non-zero exit keeps the agent off a half-provisioned tree. Idempotent.
# Orca env: ORCA_ROOT_PATH (main checkout), ORCA_WORKTREE_PATH. Claude Code only (D4).
# --refresh: the same provisioning for a KEPT worktree (dispatch.sh --address / --retry-of), minus task.md: the main checkout's current
# .claude/ and setup.local.sh output land again, spoke-run-id stays, and the task text is left as the worker found it.
set -euo pipefail
refresh=0
case "${1:-}" in '') ;; --refresh) refresh=1 ;; *) echo "usage: setup.sh [--refresh]" >&2; exit 2 ;; esac
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
load_env   # the main checkout's local env (AI_TOOLKIT_GH)

root="${ORCA_ROOT_PATH:?ORCA_ROOT_PATH unset: run me as the Orca setup hook}"
wt="${ORCA_WORKTREE_PATH:-$PWD}"
cd "$wt"
rm -f .ai-toolkit/setup-done   # first, so a failed re-run never leaves a stale ready marker
[ -d "$root/.claude" ] || die "$root/.claude is missing: sync ai-toolkit into the main checkout first"

mkdir -p .claude .ai-toolkit
if [ "$refresh" = 0 ]; then cp -R "$root/.claude/." .claude/
else   # a kept worktree: tracked .claude files reach it through git (the branch's own, and main's via land's merge); copying them would
  # dirty the tree, clobber the worker's edits, or leave an untracked file that makes that merge fail. Only the untracked copies are refreshed.
  tracked="$(git -c core.quotePath=false ls-files .claude; git -C "$root" -c core.quotePath=false ls-files .claude)"
  while IFS= read -r f; do
    ! grep -qxF -- ".claude/$f" <<< "$tracked" || continue
    mkdir -p ".claude/$(dirname "$f")"; cp -P "$root/.claude/$f" ".claude/$f"
  done < <(cd "$root/.claude" && find . \( -type f -o -type l \) | sed 's|^\./||')
fi

# Keep the provisioning out of `git status` (info/exclude is shared by all worktrees).
excl="$(git rev-parse --path-format=absolute --git-path info/exclude)"
mkdir -p "$(dirname "$excl")"
patterns=".ai-toolkit/"
[ -n "$(git ls-files .claude)" ] || patterns="$patterns .claude/"
for p in $patterns; do grep -qxF "$p" "$excl" 2>/dev/null || echo "$p" >> "$excl"; done

# One id per spoke, kept across re-runs: the Langfuse session key (bin/claude-spoke).
[ -s .ai-toolkit/spoke-run-id ] || uuidgen | tr '[:upper:]' '[:lower:]' > .ai-toolkit/spoke-run-id

# task.md from the linked issue: Orca's link, else the `<n>-<slug>` branch name (worker-start
# links the issue only after setup).
n=""
[ "$refresh" = 1 ] || n="$(orca_json worktree show --worktree "path:$wt" 2>/dev/null | jq -r '.result.worktree.linkedIssue // empty' 2>/dev/null || true)"
if [ -z "$n" ] && [ "$refresh" = 0 ]; then
  b="$(git branch --show-current)"
  case "${b%%-*}" in '' | *[!0-9]*) ;; *) n="${b%%-*}" ;; esac
fi
if [ -n "$n" ]; then
  gh_issue "$n" | jq -r '"# #\(.number) \(.title)\n\n\(.body)"' > .ai-toolkit/task.md || die "cannot fetch issue $n for task.md"
fi

# Host-specific extras (venv, caches...): untracked, lives in the main checkout, cwd = the worktree.
if [ -f "$root/.ai-toolkit/setup.local.sh" ]; then bash "$root/.ai-toolkit/setup.local.sh"; fi

# Readiness marker for launchers that start the agent themselves (two-step launch, 03-notes Q1).
touch .ai-toolkit/setup-done
