#!/usr/bin/env bash
# Orca setup hook (orca.yaml scripts.setup). Runs in the NEW worktree before the agent starts
# (wait-for-setup), so a non-zero exit keeps the agent off a half-provisioned tree. Idempotent.
# A worker gets main's .claude/ and .ai-toolkit/rules/ (the on-demand rules its seed and agents name; .ai-toolkit/ is untracked).
# Orca env: ORCA_ROOT_PATH (main checkout), ORCA_WORKTREE_PATH. Claude Code only (D4).
# --refresh: the same provisioning for a KEPT worktree (dispatch.sh --address / --retry-of): the main checkout's current
# .claude/ synced files and .ai-toolkit/rules/ (and the removal of those main dropped) and setup.local.sh output land again, spoke-run-id stays, and task.md is
# rewritten from the issue as it reads now (an issue edited since the first dispatch must reach the worker and its reviewer); an issue that cannot be fetched keeps the old task.md and warns.
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
# The synced set is the .claude/ files sync installed in main (its manifest is the source of truth); everything else under .claude/ is local.
# The worktree records the set it was given, so the next refresh removes what main's sync has dropped since.
manifest="$root/.ai-toolkit/sync-manifest"
installed=.ai-toolkit/synced-claude
synced="$(grep '^\.claude/' "$manifest" 2> /dev/null | grep -v '\.\.' || true)"
if [ "$refresh" = 0 ]; then
  cp -R "$root/.claude/." .claude/
  if [ -d "$root/.ai-toolkit/rules" ]; then mkdir -p .ai-toolkit/rules; cp -R "$root/.ai-toolkit/rules/." .ai-toolkit/rules/; fi
else   # a kept worktree: tracked .claude files reach it through git (the branch's own, and main's via land's merge); copying them would
  # dirty the tree, clobber the worker's edits, or leave an untracked file that makes that merge fail. Only the untracked synced copies are refreshed.
  [ -n "$synced" ] || die "$manifest lists no .claude/ files (missing, empty or cut short): sync ai-toolkit into the main checkout first"   # never delete against an empty set
  tracked="$(git -c core.quotePath=false ls-files .claude; git -C "$root" -c core.quotePath=false ls-files .claude)"
  # One pass over the lists (grep -vxFf: the lines of the first not in the second), then one rm and one tar for the whole set, so the
  # number of processes stays flat as the tree grows.
  dropped="$(grep '^\.claude/' "$installed" 2> /dev/null | grep -v '\.\.' | grep -vxFf <(printf '%s\n' "$synced" "$tracked") || true)"   # the record is data in the worktree: never leave .claude/
  if [ -n "$dropped" ]; then
    tr '\n' '\0' <<< "$dropped" | xargs -0 rm -f --
    # shellcheck disable=SC2001  # ${var//} anchors on the whole multi-line string, not each line's last component; one sed covers the list
    sed 's|/[^/]*$||' <<< "$dropped" | sort -u | tr '\n' '\0' | xargs -0 rmdir -p 2> /dev/null || true
  fi
  copy="$(grep -vxFf <(printf '%s\n' "$tracked") <<< "$synced" || true)"
  [ -z "$copy" ] || printf '%s\n' "$copy" | tar -C "$root" -cf - -T - | tar -xf -   # symlinks stay symlinks
  # .ai-toolkit/rules/ is untracked and wholly sync-owned: mirror the manifest's entries (a rule main dropped goes; the empty-manifest die above ran first).
  rules="$(grep '^\.ai-toolkit/rules/' "$manifest" | grep -v '\.\.' || true)"
  rm -rf .ai-toolkit/rules
  [ -z "$rules" ] || printf '%s\n' "$rules" | tar -C "$root" -cf - -T - | tar -xf -
fi
printf '%s\n' "$synced" > "$installed"

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
n="$(issue_of "$wt")"
if [ -n "$n" ]; then
  fetched="$(mktemp .ai-toolkit/task.md.XXXXXX)"   # a failed fetch must not truncate the task.md already there
  if gh_issue "$n" | task_md > "$fetched"; then mv "$fetched" .ai-toolkit/task.md
  else
    rm -f "$fetched"
    if [ "$refresh" = 1 ]; then warn "cannot fetch issue $n: keeping the existing task.md"; else die "cannot fetch issue $n for task.md"; fi
  fi
fi

# Host-specific extras (venv, caches...): untracked, lives in the main checkout, cwd = the worktree.
if [ -f "$root/.ai-toolkit/setup.local.sh" ]; then bash "$root/.ai-toolkit/setup.local.sh"; fi

# Readiness marker for launchers that start the agent themselves (two-step launch, 03-notes Q1).
touch .ai-toolkit/setup-done
