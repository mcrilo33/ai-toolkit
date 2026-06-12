#!/usr/bin/env bash
# hub-status.sh — live dashboard for the planning hub.
#
# Surveys what is in flight across the parallel-worktrees workflow:
#   - all tmux panes across every session (once, at startup)
#   - worktrees and, per task branch, ahead/behind vs main + dirty state,
#     linked GitHub issue state, and correlated tmux pane/jump command
#   - open GitHub issues, flagged by whether a worktree already exists for them
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

# --- Survey all tmux panes once ---------------------------------------------
# Format: session:window<TAB>path  (one entry per pane across all sessions)
all_panes="$(tmux list-panes -a -F '#{session_name}:#{window_index}	#{pane_current_path}' 2>/dev/null || true)"

# Current session name (empty when outside tmux)
current_session=""
if [ -n "${TMUX:-}" ]; then
  current_session="$(tmux display-message -p '#{session_name}' 2>/dev/null || true)"
fi

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

  # Extract leading digits from branch slug (e.g. feature/1-pushed → 1)
  slug="${branch##*/}"
  issue_num="$(printf '%s' "$slug" | sed 's/^\([0-9]*\).*/\1/')"

  # Issue state column
  issue_col=""
  if [ -n "$issue_num" ]; then
    issue_state="$(gh issue view "$issue_num" --json state -q .state 2>/dev/null || true)"
    if [ -n "$issue_state" ]; then
      issue_col="  #${issue_num} ${issue_state}"
    else
      issue_col="  #${issue_num} ?"
    fi
  fi

  # Pane correlation: find first pane whose path equals this worktree path
  pane_loc="no pane"
  pane_sess=""
  pane_idx=""
  if [ -n "$all_panes" ]; then
    while IFS='	' read -r sess_win pane_path; do
      if [ "$pane_path" = "$path" ]; then
        pane_loc="tmux ${sess_win}"
        pane_sess="${sess_win%%:*}"
        pane_idx="${sess_win#*:}"
        break
      fi
    done <<<"$all_panes"
  fi

  printf '  %-28s ↑%s ↓%s  %s%s  %s\n' \
    "$branch" "${ahead:-?}" "${behind:-?}" "$state" "$issue_col" "$pane_loc"

  # Jump line under rows that have a pane
  if [ "$pane_loc" != "no pane" ]; then
    sess_win_key="${pane_sess}:${pane_idx}"
    if [ -n "$current_session" ]; then
      if [ "$pane_sess" = "$current_session" ]; then
        printf "      ↳ jump: tmux select-window -t '%s'\n" "$sess_win_key"
      else
        printf "      ↳ jump: tmux switch-client -t '%s'\n" "$sess_win_key"
      fi
    else
      printf "      ↳ jump: tmux attach -t %s \\; select-window -t '%s'\n" \
        "$pane_sess" "$sess_win_key"
    fi
  fi
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

exit 0
