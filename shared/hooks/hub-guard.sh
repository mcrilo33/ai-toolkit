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
# outside a git repo, while a merge/rebase is in progress (sanctioned hub work),
# or for any other command — so execution spokes are never touched. Modelled on
# config-protection.sh: one script, every platform. On Cursor the metadata
# override wires this hook to beforeShellExecution only, so there it gates
# commit/branch-create (not pre-write file edits); Claude/Copilot gate all four.
#
# Exit 2 = block, Exit 0 = allow.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

# Fail closed on an unparseable payload (issue #208). The only extraction that can
# crash (get_edit_file_path, below) runs ONLY after the hub checks confirm this is
# the main checkout on the default branch — a spoke exits 0 earlier and never
# reaches it — so a crash here is hub task work we could not classify. Without
# this trap jq's non-zero exit under `set -euo pipefail` would propagate out and,
# being neither 0 nor 2, let the edit/commit proceed on the hub (Claude Code
# treats a non-2 exit as non-blocking). Convert any uncaught crash into a deny.
# The spoke-no-op guarantee rests on bash's default: a jq failure inside an
# earlier extraction's command substitution does NOT abort (errexit is off in
# subshells), so only get_edit_file_path's top-level assignment can trip this
# trap. Do not enable `shopt -s inherit_errexit` here without re-checking that
# the pre-worktree extractions still recover rather than deny a spoke.
trap 'deny "hub-guard could not parse the tool payload on the planning hub; blocking (fail-closed, issue #208). Dispatch task work into its own worktree via the start-task skill."' ERR

# Resolve the base branch the hub stays on — the ONE canonical resolver
# (issue #117): config ai-toolkit.base-branch > AI_TOOLKIT_BASE_BRANCH >
# origin/HEAD > init.defaultBranch (existing ref) > main/master > "main".
# The inline chain below is the degraded fallback for a stale synced layout
# whose lib/ predates base-branch.sh — the same chain the guard always had.
# Its existence checks matter because a bare `git init` lands on master on
# some platforms and main on others, with init.defaultBranch frequently unset
# — a fixed "main" fallback would silently disable the guard on a
# master-default hub.
[ -f "$HOOK_DIR/lib/base-branch.sh" ] && source "$HOOK_DIR/lib/base-branch.sh"
hub_default_branch() {
  local root="$1" def
  if command -v wt_base_branch >/dev/null 2>&1; then
    wt_base_branch "$root"
    return 0
  fi
  def=$(git -C "$root" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
  [ -n "$def" ] && { printf '%s' "$def"; return 0; }
  def=$(git -C "$root" config --get init.defaultBranch 2>/dev/null || true)
  if [ -n "$def" ] && git -C "$root" show-ref --verify --quiet "refs/heads/$def"; then
    printf '%s' "$def"
    return 0
  fi
  for def in main master; do
    git -C "$root" show-ref --verify --quiet "refs/heads/$def" && { printf '%s' "$def"; return 0; }
  done
  printf 'main'
}

INPUT=$(read_stdin)
ROOT=$(project_root_from_payload "$INPUT")

# Not in a git repo → nothing to guard.
GIT_DIR=$(git -C "$ROOT" rev-parse --absolute-git-dir 2>/dev/null || true)
[ -z "$GIT_DIR" ] && exit 0

# Inside a linked worktree → this is an execution spoke, never the hub. A
# worktree's git-dir lives under <main>/.git/worktrees/<name>; matching the full
# "/.git/worktrees/" segment (not a bare "/worktrees/") avoids misreading a main
# checkout that merely sits under a directory named "worktrees".
case "$GIT_DIR" in
  */.git/worktrees/*) exit 0 ;;
esac

# The hub lives only on the default branch; any other branch is a spoke.
DEFAULT_BRANCH=$(hub_default_branch "$ROOT")
CURRENT_BRANCH=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
[ -z "$CURRENT_BRANCH" ] && exit 0
[ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" ] && exit 0

# A merge / rebase / cherry-pick / revert in progress is sanctioned hub work
# (the hub lands spoke branches): resolving conflicts needs edits + a commit, so
# allow them until the operation finishes rather than dead-ending it.
for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
  [ -e "$GIT_DIR/$marker" ] && exit 0
done

# A `hub-guard-allow` file in the git-dir is the explicit, user-granted escape
# hatch (issue #89): while present it bypasses EVERY guard check, so the hub
# session can drive commits into a /quick worktree. Granting creates the file
# (worktree-quick.sh drops it); revoking removes it (worktree-done.sh on
# teardown). With no marker the default deny below is unchanged.
[ -e "$GIT_DIR/hub-guard-allow" ] && exit 0

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

# No command → a tool call. Skip the Cursor scratch path first.
FILE_PATH=$(get_edit_file_path "$INPUT")
is_agent_tools_path "$FILE_PATH" && exit 0

# A file path OUTSIDE this repo is not hub task work (e.g. writing a /tmp scratch
# file to feed `gh issue create --body-file`) — let it through.
case "$FILE_PATH" in
  "" | "$ROOT" | "$ROOT"/*) ;; # no path, or inside the repo → keep guarding
  /*) exit 0 ;;                # an absolute path outside the repo → allow
esac

# Deny only file-MUTATING tools, identified by tool name so read-only tools
# (read_file, grep, …) still work on the hub — important under Copilot, where
# this hook carries no matcher and fires for EVERY tool. Write/Edit/NotebookEdit
# always mutate (NotebookEdit carries notebook_path, not file_path, so the name
# match is what catches it); a "create"-named tool is only treated as a file
# write when it carries a file_path, so the hub's own create_issue /
# create_pull_request style tools are not collateral-blocked.
case "$(get_tool_name "$INPUT")" in
  *[Ww]rite* | *[Ee]dit* | *[Nn]otebook*) deny "$DENY_MSG" ;;
  *[Cc]reate*) [ -n "$FILE_PATH" ] && deny "$DENY_MSG" ;;
esac
exit 0
