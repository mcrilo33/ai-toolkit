#!/usr/bin/env bash
# hub-status.sh — live dashboard for the planning hub.
#
# Surveys what is in flight across the parallel-worktrees workflow:
#   - worktrees and, per task branch, ahead/behind vs main + dirty state
#   - open GitHub issues, flagged by whether a worktree already exists for them
#   - tmux windows (when run inside tmux)
#
# Read-only. Run from the main checkout. Safe to run anytime.
set -uo pipefail

main_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Not inside a git repository." >&2
  exit 1
}
default_branch="$(git -C "$main_root" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')"
default_branch="${default_branch:-main}"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }

# --- Worktrees -------------------------------------------------------------
bold "Worktrees"
# Collect branches that have a worktree so we can cross-reference issues later.
worktree_branches=""
while IFS= read -r line; do
  path="$(awk '{print $1}' <<<"$line")"
  branch="$(sed -n 's/.*\[\(.*\)\].*/\1/p' <<<"$line")"
  [ -z "$branch" ] && branch="(detached)"
  worktree_branches+="$branch"$'\n'

  if [ "$branch" = "$default_branch" ]; then
    printf '  %-28s %s  (hub)\n' "$branch" "$path"
    continue
  fi

  # ahead/behind vs default branch
  counts="$(git -C "$path" rev-list --left-right --count "$default_branch...HEAD" 2>/dev/null)"
  behind="$(awk '{print $1}' <<<"$counts")"; ahead="$(awk '{print $2}' <<<"$counts")"
  dirty=""
  [ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ] && dirty="dirty"
  # Push state is measured against the branch's own UPSTREAM (not the default
  # branch): a branch can be fully pushed yet still carry commits ahead of the
  # default branch. Mergeability is the ahead-vs-default count.
  state="$dirty"
  if [ -z "$dirty" ]; then
    if git -C "$path" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
      unpushed="$(git -C "$path" rev-list --count '@{u}..HEAD' 2>/dev/null)"
      if [ "${unpushed:-0}" -gt 0 ]; then
        state="unpushed"
      elif [ "${ahead:-0}" -gt 0 ]; then
        state="pushed → mergeable"
      else
        state="pushed"
      fi
    else
      state="unpushed"
    fi
  fi
  printf '  %-28s ↑%s ↓%s  %s\n' "$branch" "${ahead:-?}" "${behind:-?}" "$state"
done < <(git -C "$main_root" worktree list 2>/dev/null)
echo

# --- Open issues -----------------------------------------------------------
bold "Open issues"
if command -v gh >/dev/null 2>&1; then
  gh issue list --state open --limit 30 \
    --json number,title,labels \
    --template '{{range .}}{{printf "  #%v  %s\n" .number .title}}{{end}}' 2>/dev/null \
  | while IFS= read -r row; do
      num="$(sed -n 's/^  #\([0-9]*\).*/\1/p' <<<"$row")"
      if [ -n "$num" ] && grep -q "/${num}-" <<<"$worktree_branches"; then
        printf '%s  ⟶ worktree active\n' "$row"
      else
        printf '%s  ⟶ no worktree\n' "$row"
      fi
    done
  [ -z "$(gh issue list --state open --limit 1 2>/dev/null)" ] && echo "  (none open)"
else
  echo "  gh not installed — skipping issue survey"
fi
echo

# --- tmux windows ----------------------------------------------------------
if [ -n "${TMUX:-}" ]; then
  bold "tmux windows"
  tmux list-windows -F '  #{window_index}: #{window_name}#{?window_active, (active),}' 2>/dev/null
fi
