#!/usr/bin/env bash
# hub-guard — PreToolUse hook enforcing the planning-hub invariant.
#
# The MAIN CHECKOUT is the planning hub: it stays on the default branch and
# never holds task work. Every task lives in its own worktree, on its own
# branch, driven by its own session (see the start-task skill and
# docs/parallel-worktrees.md). This guard makes that invariant enforced rather
# than merely advisory.
#
# It DENIES — but ONLY when BOTH conditions hold:
#   • the working tree is the MAIN checkout (not a linked worktree), AND
#   • HEAD is on the repository's DEFAULT branch,
# and the attempted action is one of:
#   • an Edit / Write / NotebookEdit of a file, OR
#   • git commit, OR
#   • git checkout -b / git switch -c  (creating a task branch).
#
# It is a NO-OP (exit 0) inside any linked worktree, on any non-default branch,
# outside a git repo, or for any other command — so execution spokes are never
# touched. Modelled on config-protection.sh: one script, every platform.
#
# Exit 2 = block, Exit 0 = allow.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

# Resolve the repository's default branch — the branch the hub stays on:
# origin/HEAD if set, else the configured init.defaultBranch, else "main".
hub_default_branch() {
  local root="$1" def
  def=$(git -C "$root" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
  [ -n "$def" ] && { printf '%s' "$def"; return 0; }
  def=$(git -C "$root" config --get init.defaultBranch 2>/dev/null || true)
  [ -n "$def" ] && { printf '%s' "$def"; return 0; }
  printf 'main'
}

INPUT=$(read_stdin)
ROOT=$(project_root_from_payload "$INPUT")

# Not in a git repo → nothing to guard.
GIT_DIR=$(git -C "$ROOT" rev-parse --git-dir 2>/dev/null || true)
[ -z "$GIT_DIR" ] && exit 0

# Inside a linked worktree → this is an execution spoke, never the hub. A
# worktree's git-dir lives under <main>/.git/worktrees/<name>.
case "$GIT_DIR" in
  *"/worktrees/"*) exit 0 ;;
esac

# The hub lives only on the default branch; any other branch is a spoke.
DEFAULT_BRANCH=$(hub_default_branch "$ROOT")
CURRENT_BRANCH=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
[ -z "$CURRENT_BRANCH" ] && exit 0
[ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" ] && exit 0

# ── We are on the main checkout, on the default branch: the planning hub. ──
DENY_MSG="The main checkout on '$DEFAULT_BRANCH' is the planning hub — it never holds task work.
Dispatch this task into its own worktree instead of editing or committing here:
run the start-task skill (it drafts the issue and spawns the worktree via
scripts/worktree-new.sh), then do the work in that worktree's session.
See the planning-hub rule and docs/parallel-worktrees.md."

# A shell command present → only git commit / branch-creation are task work;
# everything else (status, log, reading, switching to an existing branch) is
# fine on the hub.
COMMAND=$(get_shell_command "$INPUT")
if [ -n "$COMMAND" ]; then
  if is_git_commit "$COMMAND" || is_git_branch_create "$COMMAND"; then
    deny "$DENY_MSG"
  fi
  exit 0
fi

# Otherwise this is a file-mutating tool (Edit / Write / NotebookEdit). Skip
# only the Cursor internal scratch path, which is never a real edit.
FILE_PATH=$(get_edit_file_path "$INPUT")
is_agent_tools_path "$FILE_PATH" && exit 0
deny "$DENY_MSG"
