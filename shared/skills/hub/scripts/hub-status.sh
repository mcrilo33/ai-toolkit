#!/usr/bin/env bash
# hub-status.sh — live dashboard for the planning hub.
#
# Surveys what is in flight across the parallel-worktrees workflow:
#   - all tmux panes across every session (once, at startup)
#   - worktrees and, per task branch, ahead/behind vs main + dirty state,
#     linked GitHub issue state, correlated tmux pane/jump command, and
#     task ledger (Tasks system or TodoWrite; done/total from the spoke's
#     latest Claude session)
#   - open GitHub issues, flagged by whether a worktree already exists for them
#
# Read-only. Run from the main checkout. Safe to run anytime.
set -uo pipefail

main_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Not inside a git repository." >&2
  exit 1
}

# The integration base comes from the ONE canonical resolver (issue #117):
# config ai-toolkit.base-branch > AI_TOOLKIT_BASE_BRANCH > origin/HEAD > … .
# Two-layout candidates, mirroring hub-afk.sh: co-located sibling in a synced
# target (.ai-toolkit/scripts/), shared/hooks/lib/ in the ai-toolkit checkout.
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _cand in "$_script_dir/base-branch.sh" "$_script_dir/../../../hooks/lib/base-branch.sh"; do
  if [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand
if command -v wt_base_branch >/dev/null 2>&1; then
  default_branch="$(wt_base_branch "$main_root")"
else
  default_branch="$(git -C "$main_root" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')"
  default_branch="${default_branch:-main}"
fi

# Global on/off switch (#154): source the canonical resolver from the same two
# layouts, so hub-status can flag a disabled toolkit.
for _cand in "$_script_dir/enabled.sh" "$_script_dir/../../../hooks/lib/enabled.sh"; do
  if [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

bold() { printf '\033[1m%s\033[0m\n' "$1"; }

# Lead with a LOUD banner when the toolkit is disabled, so a forgotten `off`
# can't silently ship ungated/gutted code to the default branch (#154).
if command -v ai_toolkit_enabled >/dev/null 2>&1 && ! ai_toolkit_enabled "$main_root"; then
  printf '\033[1;31m⚠ AI-TOOLKIT OFF — gates/guards/telemetry bypassed\033[0m\n'
  printf '\033[1;31m  Commits/pushes on every worktree of this clone are UNGATED. Re-enable: ai-toolkit on\033[0m\n'
  echo
fi

# todos_for_path <worktree-path>
# Reads the newest .jsonl from the spoke's Claude project dir and returns
# "<done>/<total> [· step: <X>] · <activity> [· ⚠ WAITING ON INPUT]", or the
# same extras after "none", or "" if unavailable.
# Current runtimes keep the ledger via the Tasks system (TaskCreate/TaskUpdate);
# those records are reconstructed first, with the last TodoWrite snapshot as
# fallback for older runtimes. "none" only when NEITHER system has entries.
# step: cycle keyword (ANCHOR/RED/GREEN/REVIEW/PUSH) from the in_progress item,
# else its truncated text. Activity age comes from the transcript's mtime. The
# waiting flag fires on an open AskUserQuestion (no tool_result, no later
# meaningful event) or a trailing notification entry.
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
import json, os, re, time

tasks = {}        # task id -> {"content", "status"}, insertion-ordered
create_uses = set()   # TaskCreate tool_use ids awaiting their tool_result
update_uses = {}      # TaskUpdate tool_use id -> input (taskId fallback)
todos = None          # last TodoWrite snapshot (older-runtime fallback)
pending_ask = None    # open AskUserQuestion ids; any later meaningful event clears
last_type = None      # type of the newest parsed transcript entry
try:
    with open(os.environ["_TODOS_JSONL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            last_type = obj.get("type")
            content = (obj.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                if last_type == "user":
                    pending_ask = None  # plain text prompt — session moved on
                continue
            if last_type == "assistant":
                asks = set()
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    name = block.get("name")
                    if name == "TodoWrite":
                        todos = (block.get("input") or {}).get("todos") or []
                    elif name == "TaskCreate":
                        create_uses.add(block.get("id"))
                    elif name == "TaskUpdate":
                        update_uses[block.get("id")] = block.get("input") or {}
                    elif name == "AskUserQuestion":
                        asks.add(block.get("id"))
                # A meaningful assistant event supersedes an older open question;
                # one carrying an AskUserQuestion opens a new one.
                pending_ask = asks or None
            elif last_type == "user":
                pending_ask = None
                # The task id is NOT in the TaskCreate input — it only exists in
                # the tool_result line's toolUseResult; same for the authoritative
                # TaskUpdate status transition.
                tur = obj.get("toolUseResult")
                if not isinstance(tur, dict):
                    continue
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                        continue
                    uid = block.get("tool_use_id")
                    if uid in create_uses:
                        task = tur.get("task") or {}
                        tid = task.get("id")
                        if tid is not None:
                            tasks[str(tid)] = {
                                "content": str(task.get("subject") or ""),
                                "status": "pending",
                            }
                    elif uid in update_uses:
                        change = tur.get("statusChange") or {}
                        tid = str(tur.get("taskId") or update_uses[uid].get("taskId") or "")
                        new = change.get("to")
                        if tid in tasks and new:
                            if new == "deleted":
                                del tasks[tid]
                            else:
                                tasks[tid]["status"] = new
except Exception:
    pass

if tasks:
    entries = list(tasks.values())
elif todos is not None:
    entries = [t for t in todos if isinstance(t, dict)]
else:
    entries = None

parts = []
if entries is None:
    parts.append("none")
else:
    done = sum(1 for t in entries if t.get("status") == "completed")
    parts.append(f"{done}/{len(entries)}")
    ip = next((t for t in entries if t.get("status") == "in_progress"), None)
    if ip:
        text = (ip.get("content") or "").split("\n", 1)[0]
        m = re.search(r"\b(anchor|red|green|review|push)\b", text, re.I)
        parts.append(f"step: {m.group(1).upper() if m else text[:60]}")
try:
    age = max(0, time.time() - os.path.getmtime(os.environ["_TODOS_JSONL"]))
    if age < 60:
        parts.append(f"active {int(age)}s ago")
    elif age < 3600:
        parts.append(f"idle {int(age // 60)}m")
    else:
        parts.append(f"idle {int(age // 3600)}h")
except Exception:
    pass
if pending_ask or last_type == "notification":
    parts.append("⚠ WAITING ON INPUT")
print(" · ".join(parts))
PYEOF
}

# mergeable_state <worktree-path> <issue-num>
# A fully-pushed, ahead branch is mergeable only when completion is explicit:
# a ready/<issue> tag pointing at the branch tip (issue #16) — a per-subtask
# push looks identical to task completion otherwise. Branches without an
# issue number (ad-hoc/express) are exempt: their one push IS completion.
mergeable_state() {
  local wt_path="$1" issue="$2"
  if [ -z "$issue" ]; then
    printf 'pushed → mergeable'
    return
  fi
  local tip marker
  tip="$(git -C "$wt_path" rev-parse HEAD 2>/dev/null)"
  marker="$(git -C "$wt_path" rev-parse -q --verify "refs/tags/ready/${issue}^{commit}" 2>/dev/null)"
  if [ -n "$marker" ] && [ "$marker" = "$tip" ]; then
    printf 'pushed → mergeable'
  else
    printf 'pushed (in progress)'
  fi
}

# --- Survey all tmux panes once ---------------------------------------------
# Format: session:window<TAB>path  (one entry per pane across all sessions)
all_panes="$(tmux list-panes -a -F '#{session_name}:#{window_index}	#{pane_current_path}' 2>/dev/null || true)"

# Current session name (empty when outside tmux)
current_session=""
if [ -n "${TMUX:-}" ]; then
  current_session="$(tmux display-message -p '#{session_name}' 2>/dev/null || true)"
fi

# --- Collector-down warning (#138) -------------------------------------------
# While ≥1 spoke pane is live, the OTel collector ports (4317 gRPC traces /
# 4318 OTLP-HTTP cycle spans) MUST be listening or every span those spokes emit
# is dropped at source with no alert — a machine sleep does exactly this. Lead
# the dashboard with a loud warning in that state; stay silent when no spoke is
# live, when the collector is up, or on an explicit operator opt-out
# (AI_TOOLKIT_OTEL=0 — unset still warns: spokes are default-on). The port
# probe comes from worktree-lib.sh (same dual-layout ladder as hub-otel-watch);
# a partially-synced target without the lib skips the check silently.
for _cand in \
  "$_script_dir/worktree-lib.sh" \
  "$_script_dir/../../../../scripts/worktree-lib.sh" \
  "$main_root/scripts/worktree-lib.sh" \
  "$main_root/.ai-toolkit/scripts/worktree-lib.sh"; do
  if [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

spoke_pane_live=0
if [ -n "$all_panes" ]; then
  wt_paths="$(git -C "$main_root" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2}')"
  while IFS='	' read -r _sess_win pane_path; do
    [ -n "$pane_path" ] || continue
    while IFS= read -r wt_path; do
      [ -n "$wt_path" ] || continue
      [ "$wt_path" = "$main_root" ] && continue
      if [ "$pane_path" = "$wt_path" ]; then
        spoke_pane_live=1
        break 2
      fi
    done <<<"$wt_paths"
  done <<<"$all_panes"
fi

if [ "$spoke_pane_live" -eq 1 ] && [ "${AI_TOOLKIT_OTEL:-}" != "0" ] \
  && command -v wt_port_listening >/dev/null 2>&1; then
  dark_ports=""
  wt_port_listening 4317 || dark_ports="4317"
  wt_port_listening 4318 || dark_ports="${dark_ports:+$dark_ports }4318"
  if [ -n "$dark_ports" ]; then
    printf '\033[1;31m⚠ COLLECTOR DOWN\033[0m — spoke pane(s) live but OTel collector port(s) %s not listening.\n' "$dark_ports"
    echo "  Spans from running spokes are being dropped at source RIGHT NOW."
    echo "  Re-arm: bash shared/skills/hub/scripts/hub-otel-watch.sh --daemon  (with AI_TOOLKIT_OTEL=1 + LANGFUSE_BASIC_AUTH)"
    echo
  fi
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

  # Extract leading digits from branch slug (e.g. feature/1-pushed → 1)
  slug="${branch##*/}"
  issue_num="$(printf '%s' "$slug" | sed 's/^\([0-9]*\).*/\1/')"

  # Push state is measured against the branch's own UPSTREAM (not the default
  # branch): a branch can be fully pushed yet still carry commits ahead of the
  # default branch. Mergeability is the ahead-vs-default count, gated on the
  # explicit ready/<issue> completion marker (mergeable_state).
  state="$dirty"
  if [ -z "$dirty" ]; then
    if git -C "$path" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
      unpushed="$(git -C "$path" rev-list --count '@{u}..HEAD' 2>/dev/null)"
      if [ "${unpushed:-0}" -gt 0 ]; then
        state="unpushed"
      elif [ "${ahead:-0}" -gt 0 ]; then
        state="$(mergeable_state "$path" "$issue_num")"
      else
        state="pushed"
      fi
    else
      state="unpushed"
    fi
  fi

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

  # Task ledger sub-line (Tasks system or TodoWrite)
  todos_out="$(todos_for_path "$path")"
  [ -n "$todos_out" ] && printf "      ↳ todos: %s\n" "$todos_out"
done < <(git -C "$main_root" worktree list 2>/dev/null)
echo

# --- Hub agents (issue #245) -------------------------------------------------
# Hub-side agent work (pre-land reviews, bug-scopers, delta re-reviews) runs via
# hub-agent.sh, which journals a start record when it launches and an end record
# when it finishes. Surface the LIVE ones — a label whose latest journal event is
# `start` (no matching `end`) — so a running hub agent is as visible as a spoke
# pane. Strictly best-effort/read-only (mirrors the todos sub-line): a missing,
# empty, or malformed journal yields an empty view, never an error. The dir is
# overridable (AI_TOOLKIT_HUB_AGENTS_DIR) for tests; else the hub's gitignored
# .ai-toolkit/hub-agents.
bold "Hub agents"
hub_agents_dir="${AI_TOOLKIT_HUB_AGENTS_DIR:-$main_root/.ai-toolkit/hub-agents}"
hub_agents_journal="$hub_agents_dir/journal.jsonl"
hub_agents_out=""
if [ -f "$hub_agents_journal" ] && command -v python3 >/dev/null 2>&1; then
  hub_agents_out="$(_HA_JOURNAL="$hub_agents_journal" python3 2>/dev/null <<'PYEOF'
import json, os, time

# Keyed on run_id (unique per dispatch) so an `end` retires exactly its own run,
# never a still-running sibling launched under the same label. Falls back to the
# label when a record predates run_ids.
live = {}
try:
    with open(os.environ["_HA_JOURNAL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            label = obj.get("label")
            event = obj.get("event")
            if not label or event not in ("start", "end"):
                continue
            key = obj.get("run_id") or label
            if event == "start":
                live[key] = obj
            else:
                live.pop(key, None)
except Exception:
    pass


def _pid_alive(rec):
    """A journaled worker with a dead pid never wrote its `end` (killed window,
    crash, reboot). Retire it. A missing/unverifiable pid stays live."""
    pid = rec.get("pid")
    if not isinstance(pid, int):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


now = time.time()
for rec in live.values():
    if not _pid_alive(rec):
        continue
    label = rec.get("label")
    purpose = str(rec.get("purpose") or "").split("\n", 1)[0]
    parts = [f"hub:{label}"]
    if purpose:
        parts.append(purpose)
    try:
        age = max(0, now - float(rec.get("ts_epoch")))
        if age < 60:
            parts.append(f"{int(age)}s")
        elif age < 3600:
            parts.append(f"{int(age // 60)}m")
        else:
            parts.append(f"{int(age // 3600)}h")
    except (TypeError, ValueError):
        pass
    log = str(rec.get("log") or "")
    if log:
        parts.append(log)
    print("  " + " · ".join(parts))
PYEOF
)"
fi
if [ -n "$hub_agents_out" ]; then
  printf '%s\n' "$hub_agents_out"
else
  echo "  (none running)"
fi
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

# --- Scheduling (issue #223) -------------------------------------------------
# Surface the batch scheduler's own pre-dispatch decisions — why only one thing is
# running, what a `Scope: *` issue is holding back — right in the hub survey, so the
# answer is a glance rather than reading Scope: lines by hand. It reuses batch-plan's
# `--explain` renderer (the disposition logic lives in exactly one place); BATCH_PLAN
# overrides the resolved sibling for tests. Read-only, best-effort: a gh/graphql
# failure just yields an empty view — never an error that breaks the survey.
bold "Scheduling"
_bp="${BATCH_PLAN:-$_script_dir/batch-plan.sh}"
if command -v gh >/dev/null 2>&1 && [ -f "$_bp" ]; then
  sched="$(bash "$_bp" --explain 2>/dev/null || true)"
  if [ -n "$sched" ]; then
    printf '%s\n' "$sched" | sed 's/^/  /'
  else
    echo "  (nothing dispatchable)"
  fi
else
  echo "  (batch-plan unavailable — gh or batch-plan.sh missing)"
fi
echo

# --- Waived gates (issue #277) -----------------------------------------------
# Surface every PLAN gate the /afk fast-path WAIVED (auto-approved without the reasoner)
# from the decision journal, so an operator sees "this gate was fast-pathed, and why" at a
# glance rather than grepping the journal — the gh comment alone is not a hub view. The state
# dir resolves like gate-broker's _afk_state_dir: AFK_STATE_DIR override, else
# <git-common-dir>/ai-toolkit-afk. Read-only, best-effort: a missing/empty journal or absent
# python3 just yields the "none" marker.
bold "Waived gates"
_afk_common="$(git -C "$main_root" rev-parse --git-common-dir 2>/dev/null || echo .git)"
case "$_afk_common" in /*) ;; *) _afk_common="$main_root/$_afk_common" ;; esac
_journal="${AFK_STATE_DIR:-$_afk_common/ai-toolkit-afk}/decision-journal.jsonl"
if [ -f "$_journal" ] && command -v python3 >/dev/null 2>&1; then
  _waived="$(HUB_STATUS_JOURNAL="$_journal" python3 - <<'PYEOF'
import json, os

with open(os.environ["HUB_STATUS_JOURNAL"], encoding="utf-8", errors="replace") as fh:
    for line in fh:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        decision = rec.get("decision") or ""
        # A fast-path WAIVE is a park:gate journal line naming the fast path.
        if rec.get("park") == "gate" and "fast-path" in decision:
            print(f"#{rec.get('issue', '?')}  {decision}")
PYEOF
)"
  if [ -n "$_waived" ]; then
    printf '%s\n' "$_waived" | sed 's/^/  /'
  else
    echo "  (none this run)"
  fi
else
  echo "  (no decision journal yet)"
fi
echo

exit 0
