#!/usr/bin/env bash
# hub-status.sh — live dashboard for the planning hub.
#
# Surveys what is in flight across the parallel-worktrees workflow:
#   - all tmux panes across every session (once, at startup)
#   - worktrees and, per task branch, ahead/behind vs main + dirty state,
#     linked GitHub issue state, correlated tmux pane/jump command, and
#     TodoWrite ledger (done/total from the spoke's latest Claude session)
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

# todos_for_path <worktree-path>
# Reads the newest .jsonl from the spoke's Claude project dir and returns
# "<done>/<total> [· in_progress: <content>]", or "none", or "" if unavailable.
todos_for_path() {
  local wt_path="$1"
  local projects_root="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
  local slug
  slug="$(printf '%s' "$wt_path" | sed 's/[^A-Za-z0-9]/-/g')"
  local project_dir="${projects_root}/${slug}"
  [ -d "$project_dir" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  local jsonl
  jsonl="$(ls -t "${project_dir}"/*.jsonl 2>/dev/null | head -1)"
  [ -n "$jsonl" ] || return 0
  _TODOS_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json, os
result = None
try:
    with open(os.environ["_TODOS_JSONL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("type") != "assistant":
                continue
            content = (obj.get("message") or {}).get("content") or []
            for block in content:
                if (isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "TodoWrite"):
                    result = (block.get("input") or {}).get("todos") or []
except Exception:
    pass
if result is None:
    print("none")
else:
    done = sum(1 for t in result if t.get("status") == "completed")
    total = len(result)
    ip = next((t for t in result if t.get("status") == "in_progress"), None)
    line = f"{done}/{total}"
    if ip:
        content = (ip.get("content") or "")[:60]
        line += f" · in_progress: {content}"
    print(line)
PYEOF
}

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

  # Issue state column. </dev/null guards the outer loop's stdin: a
  # stdin-draining gh wrapper would otherwise swallow the worktree list.
  issue_col=""
  if [ -n "$issue_num" ]; then
    issue_state="$(gh issue view "$issue_num" --json state -q .state 2>/dev/null </dev/null || true)"
    if [ -n "$issue_state" ]; then
      issue_col="  #${issue_num} ${issue_state}"
    else
      issue_col="  #${issue_num} ?"
    fi
  fi

  # Pane correlation: find first pane whose path equals this worktree path
  pane_loc="no pane"
  pane_target=""
  pane_sess=""
  if [ -n "$all_panes" ]; then
    while IFS='	' read -r sess_win pane_path; do
      if [ "$pane_path" = "$path" ]; then
        pane_loc="tmux ${sess_win}"
        pane_target="$sess_win"
        pane_sess="${sess_win%%:*}"
        break
      fi
    done <<<"$all_panes"
  fi

  printf '  %-28s ↑%s ↓%s  %s%s  %s\n' \
    "$branch" "${ahead:-?}" "${behind:-?}" "$state" "$issue_col" "$pane_loc"

  # Jump line under rows that have a pane
  if [ "$pane_loc" != "no pane" ]; then
    if [ -n "$current_session" ]; then
      if [ "$pane_sess" = "$current_session" ]; then
        printf "      ↳ jump: tmux select-window -t '%s'\n" "$pane_target"
      else
        printf "      ↳ jump: tmux switch-client -t '%s'\n" "$pane_target"
      fi
    else
      printf "      ↳ jump: tmux attach -t %s \\; select-window -t '%s'\n" \
        "$pane_sess" "$pane_target"
    fi
  fi

  # TodoWrite ledger sub-line
  todos_out="$(todos_for_path "$path")"
  [ -n "$todos_out" ] && printf "      ↳ todos: %s\n" "$todos_out"
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
