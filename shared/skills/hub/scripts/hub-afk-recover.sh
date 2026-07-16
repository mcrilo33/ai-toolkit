#!/usr/bin/env bash
# hub-afk-recover.sh -- split out of hub-afk.sh (issue #307).
#
# The RECOVER lane of the /afk supervisor: reap / revive / nudge / dead-pane / finish-up --
# the crash-resume + liveness probes, the ledger completion signal, the #255 nudge counter,
# the resume / nudge / finish-up / pushed-but-unmarked prompts + resume/respawn commands, the
# #241 revive-first / warned-parked-last lane, _reap_or_resume,
# the auth/network reap-prep probes, reap_pass, and recover_dead_panes. A pure function-
# definition module sourced by the entry lib hub-afk.sh AFTER worktree-lib / gate-broker /
# log / afk_now and the entry's own state/time primitives, and BEFORE any function is called,
# so every cross-module helper resolves at call time. Not run on its own.
set -uo pipefail

# --- crash ≠ hang: auto-resume-once a pane-dead spoke (issue #109) -------------
# A reaped spoke is not always hung. The reaper abandoned #103 as "idle, likely hung"
# when its tmux PANE had crashed but its committed work was intact. So before declaring
# blocked we distinguish a DEAD pane (session crashed → re-adopt the worktree ONCE,
# reusing the spoke_run_id) from a LIVE-but-idle pane (truly hung → block).

# _spoke_pane_alive <wt> -> true when the spoke's AGENT is running in a tmux pane mapped to
# the worktree. Two ways to be dead: no pane maps at all (the window crashed / is gone), OR a
# pane maps but runs a bare shell with no agent beneath it (#301: the spoke is launched as
# `sh -c "<cmd>; exec zsh"`, so a killed claude — reboot, OOM, a human quitting it — leaves the
# pane alive running zsh in the worktree). Before #301 only the first was checked, so the second
# read as a healthy spoke and stranded #296/#299: never revived, and answers typed into the shell.
#
# The agent probe fails OPEN here — the OPPOSITE direction from the inject primitives' write-side
# _pane_agent_ready. A write refuses on an unprovable probe (rc 2) because the cost of guessing
# wrong is prose executed as a shell command; liveness instead keeps an unobservable pane ALIVE,
# because the cost of guessing wrong is killing + relaunching a HEALTHY spoke. So only a PROVEN
# dead agent (rc 1) flips a mapped pane to dead; rc 0 and rc 2 both read alive.
# The pane target is resolved ONCE per call and reused for the probe: a second _spoke_pane_target
# would be a second `tmux list-panes` against a loaded server, the shape that flaked #269.
# UPGRADE: memoize the verdict per (wt, tick) if reap-tick cost becomes a problem — this went from
# one `tmux list-panes` to list-panes + display-message + a `ps -eo` scan, and a single tick calls
# it several times per spoke across _reap_or_resume / _afk_finish_up_or_revive / recover_dead_panes
# (plus slot_state's own _detect_agent_dead). Not cached yet on purpose: a per-tick cache risks a
# stale ALIVE masking a pane that crashed mid-tick, and one ps scan is cheap next to the hours of
# stranding a miss costs — revisit only if profiling shows the probe dominating a tick.
_spoke_pane_alive() {
  local target rc
  target="$(_spoke_pane_target "$1")"
  [ -n "$target" ] || return 1     # no pane maps ⇒ the window crashed / is gone
  _pane_agent_alive "$target"; rc=$?
  [ "$rc" -ne 1 ]                  # rc 1 (proven dead) ⇒ dead; rc 0 alive, rc 2 unprovable ⇒ alive
}

# _afk_default_ref <wt> -> the ref the spoke branched from, so "has commits" measures work
# ABOVE the branch point. AFK_DEFAULT_BRANCH wins (historical top precedence, kept for
# back-compat — it predates and now aliases AI_TOOLKIT_BASE_BRANCH, which the canonical
# resolver honors); else wt_base_branch (issue #117: config ai-toolkit.base-branch >
# AI_TOOLKIT_BASE_BRANCH > origin/HEAD > … > `main`), sourced via worktree-lib.sh above.
_afk_default_ref() {
  local wt="$1" ref
  [ -n "${AFK_DEFAULT_BRANCH:-}" ] && { printf '%s\n' "$AFK_DEFAULT_BRANCH"; return; }
  if command -v wt_base_branch >/dev/null 2>&1; then
    wt_base_branch "$wt"
    printf '\n'
    return
  fi
  ref="$(git -C "$wt" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)"
  printf '%s\n' "${ref:-main}"
}

# _spoke_has_commits <wt> -> true when HEAD carries work to preserve: a commit ABOVE the
# branch point (merge-base HEAD <default> != HEAD). A worktree is cut from the default
# branch, so a bare "HEAD exists" is always true and would be meaningless; this is the AC1
# "with commits" test. If the base can't be resolved we can't measure it, so we favor
# preserving work (true) — the resume is bounded to once regardless.
_spoke_has_commits() {
  local wt="$1" ref base tip
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  ref="$(_afk_default_ref "$wt")"
  base="$(git -C "$wt" merge-base HEAD "$ref" 2>/dev/null)" || return 0
  [ -n "$base" ] || return 0
  [ "$base" != "$tip" ]
}

# _afk_pushed_but_unmarked <wt> <issue> -> true when the spoke did its work, PUSHED it, and is
# clean — but carries NO completion/park marker at the tip (issue #200's two-phase gap: the
# branch push landed but the ready/<issue> emission failed, leaving origin ahead with no
# signal). Requires HEAD == @{upstream} (fully pushed), a clean tree, a commit above the base,
# and no ready/accept/blocked/gate tag at the tip. Used to give the reaper an ACCURATE,
# actionable reason (re-run the marker / land by hand) instead of the misleading "likely hung".
# It deliberately does NOT auto-emit ready — a clean-pushed-no-marker tip is also the shape of
# a spoke idle BETWEEN subtasks, so auto-completing it could land incomplete work; a crashed
# such spoke is safely revived by recover_dead_panes (resume re-emits the mark after verifying).
_afk_pushed_but_unmarked() {
  local wt="$1" issue="$2" head up kind
  head="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  up="$(git -C "$wt" rev-parse -q --verify '@{upstream}' 2>/dev/null)" || return 1
  [ "$head" = "$up" ] || return 1
  [ -z "$(git -C "$wt" status --porcelain 2>/dev/null)" ] || return 1
  _spoke_has_commits "$wt" || return 1
  for kind in ready accept blocked gate; do
    [ "$(git -C "$wt" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)" = "$head" ] && return 1
  done
  return 0
}

# --- #256: the ledger completion signal before a time-ceiling reap ------------
# The wall-clock ceiling used to revive (kill + relaunch) EVERY over-ceiling spoke with no
# check for whether it was essentially DONE — so #241 was reaped at 33/33 todos, all committed
# and pushed, one step from ready. A near-complete task ledger is the "am I done?" signal the
# clean-pushed check (_afk_pushed_but_unmarked) misses when the tree is not a pristine
# HEAD==@{upstream} (a .testmondata-wal artifact, a final unpushed commit). It routes the spoke
# to a finish-up nudge (emit ready / final push) instead of a blind kill.

# AFK_LEDGER_DONE_PCT: a task ledger is "near-complete" when at least this % of its todos are
# completed (default 90 — all-but-a-few of a long ledger, or a fully-complete short one, while a
# low-progress runaway like 5/33=15% stays below). Only a plain 1..100 integer is honored: a
# bareword would break the integer test in _afk_ledger_near_complete, and an out-of-range value
# (>100) would overflow `total * PCT` negative and read EVERY spoke as near-complete — inverting
# AC2. The length bound (<=3 digits) keeps the range compare itself from erroring on a huge
# override; anything outside falls back to the default.
: "${AFK_LEDGER_DONE_PCT:=90}"
case "$AFK_LEDGER_DONE_PCT" in
  '' | *[!0-9]*) AFK_LEDGER_DONE_PCT=90 ;;
  *) { [ "${#AFK_LEDGER_DONE_PCT}" -le 3 ] && [ "$AFK_LEDGER_DONE_PCT" -ge 1 ] \
       && [ "$AFK_LEDGER_DONE_PCT" -le 100 ]; } || AFK_LEDGER_DONE_PCT=90 ;;
esac

# _afk_ledger_done_total <wt> -> "<done> <total>" for the spoke's task ledger, or nothing when no
# ledger is readable. It reconstructs the ledger from the newest transcript exactly as
# hub-status.sh:todos_for_path does — the Tasks system (TaskCreate/TaskUpdate tool_result pairs)
# with the last TodoWrite snapshot as the older-runtime fallback. The parse is DUPLICATED from
# that reader (trimmed to the counts) because #256's Scope confines edits to hub-afk.sh; keep the
# two copies in sync — if the transcript ledger shape changes, update todos_for_path AND this.
_afk_ledger_done_total() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  _AFK_LEDGER_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json
import os

tasks = {}          # task id -> {"status"}, insertion-ordered (Tasks system)
create_uses = set()  # TaskCreate tool_use ids awaiting their tool_result
update_uses = {}     # TaskUpdate tool_use id -> input (taskId fallback)
todos = None         # last TodoWrite snapshot (older-runtime fallback)
try:
    with open(os.environ["_AFK_LEDGER_JSONL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            typ = obj.get("type")
            content = (obj.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            if typ == "assistant":
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
            elif typ == "user":
                tur = obj.get("toolUseResult")
                if not isinstance(tur, dict):
                    continue
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                        continue
                    uid = block.get("tool_use_id")
                    if uid in create_uses:
                        tid = (tur.get("task") or {}).get("id")
                        if tid is not None:
                            tasks[str(tid)] = {"status": "pending"}
                    elif uid in update_uses:
                        tid = str(tur.get("taskId") or update_uses[uid].get("taskId") or "")
                        new = (tur.get("statusChange") or {}).get("to")
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

if entries:
    done = sum(1 for t in entries if t.get("status") == "completed")
    print(f"{done} {len(entries)}")
PYEOF
}

# _afk_ledger_near_complete <wt> -> rc 0 when the spoke's task ledger is readable, non-empty, and
# at least AFK_LEDGER_DONE_PCT% of its todos are completed (#256's "essentially done" signal). rc
# 1 on an unreadable / empty ledger or below-threshold progress — so a genuine runaway (no ledger,
# or low progress) is NEVER mistaken for a finishing spoke and stays reapable (AC2).
_afk_ledger_near_complete() {
  local out done total
  out="$(_afk_ledger_done_total "$1")" || return 1
  [ -n "$out" ] || return 1
  done="${out%% *}"; total="${out##* }"
  case "$done$total" in '' | *[!0-9]*) return 1 ;; esac
  [ "$total" -gt 0 ] || return 1
  [ "$(( done * 100 ))" -ge "$(( total * AFK_LEDGER_DONE_PCT ))" ]
}

# _spoke_has_work <wt> -> true when the worktree holds anything worth preserving on a crash:
# a commit above the branch point (_spoke_has_commits) OR a dirty tree (uncommitted WIP). The
# dead-pane recovery pass (issue #202 C) revives a crashed pane that has_work and re-dispatches
# one that does not — so an in-progress-but-uncommitted spoke is never torn down.
_spoke_has_work() {
  local wt="$1"
  _spoke_has_commits "$wt" && return 0
  [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]
}

# the once-per-window resume stamp: a spoke is auto-resumed at most ONCE per armed window
# (a second crash escalates to a human). Cleared on a fresh arm (_clear_resume_markers).
_afk_resumed_marker()  { printf '%s\n' "$(_afk_state_dir)/resumed-$1"; }
_afk_already_resumed() { [ -f "$(_afk_resumed_marker "$1")" ]; }
_afk_mark_resumed() {
  local m; m="$(_afk_resumed_marker "$1")"
  mkdir -p "$(dirname "$m")" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$m" 2>/dev/null || true
}
_clear_resume_markers() { rm -f "$(_afk_state_dir)"/resumed-* 2>/dev/null || true; }

# the once-per-window re-dispatch stamp (issue #202 C): a clean crashed worktree is torn
# down and re-dispatched at most ONCE per armed window (a second clean crash escalates to a
# human, so a persistently-crashing infra dep can't loop redispatch→crash forever). Cleared
# on a fresh arm alongside the resume markers.
_afk_redispatched_marker()  { printf '%s\n' "$(_afk_state_dir)/redispatched-$1"; }
_afk_already_redispatched() { [ -f "$(_afk_redispatched_marker "$1")" ]; }
_afk_mark_redispatched() {
  local m; m="$(_afk_redispatched_marker "$1")"
  mkdir -p "$(dirname "$m")" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$m" 2>/dev/null || true
}
_clear_redispatch_markers() { rm -f "$(_afk_state_dir)"/redispatched-* 2>/dev/null || true; }

# --- #255: the finished-turn-idle continue-nudge counter ----------------------
# A spoke that FINISHED its turn and stopped at the input prompt (pane alive, no dialog,
# transcript ends on a completed assistant turn — _transcript_finished_turn_idle) is NUDGED (a
# continue message injected into the LIVE session via the shared hardened injector) rather than
# killed + relaunched. Bounded: after AFK_NUDGE_MAX_ATTEMPTS nudges in one window the reaper
# falls back to the revive, so a spoke that will not resume is never nudged forever. The count
# is per-window (cleared on a fresh arm) like the resume/redispatch stamps.
: "${AFK_NUDGE_MAX_ATTEMPTS:=2}"
# Guard a non-numeric override (matching AFK_REANSWER_CEILING): a bareword would make the
# `[ count -lt $AFK_NUDGE_MAX_ATTEMPTS ]` test error and always fall through to the revive.
case "$AFK_NUDGE_MAX_ATTEMPTS" in '' | *[!0-9]*) AFK_NUDGE_MAX_ATTEMPTS=2 ;; esac
_afk_nudge_count_file() { printf '%s\n' "$(_afk_state_dir)/nudge-$1.count"; }
_afk_read_nudge_count() {
  local f n; f="$(_afk_nudge_count_file "$1")"
  n="$( [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' )"
  case "$n" in '' | *[!0-9]*) n=0 ;; esac
  printf '%s\n' "$n"
}
# _afk_incr_nudge_count <issue> -> bump and echo the new nudge count for this window.
_afk_incr_nudge_count() {
  local issue="$1" n
  mkdir -p "$(_afk_state_dir)" 2>/dev/null || true
  n=$(( $(_afk_read_nudge_count "$issue") + 1 ))
  _afk_atomic_write "$(_afk_nudge_count_file "$issue")" "$n" || true
  printf '%s\n' "$n"
}
_clear_nudge_counts() { rm -f "$(_afk_state_dir)"/nudge-*.count 2>/dev/null || true; }

# _afk_spoke_run_id <wt> -> the spoke's persisted spoke_run_id (worktree-new.sh stamps it
# at .ai-toolkit/spoke-run-id), so a resumed run groups under the SAME spoke in Langfuse.
# Synthesized from the branch + now-clock if the file is missing.
_afk_spoke_run_id() {
  local wt="$1" f id branch
  f="$wt/.ai-toolkit/spoke-run-id"
  [ -f "$f" ] && id="$(head -n1 "$f" 2>/dev/null | tr -d '[:space:]')"
  if [ -z "${id:-}" ]; then
    branch="$(git -C "$wt" branch --show-current 2>/dev/null)"
    id="${branch:-spoke}+$(afk_now)"
  fi
  printf '%s\n' "$id"
}

# _afk_resume_prompt <issue> -> the plain-English first message for the resumed session.
# Deliberately NOT a slash command: `/cycle` is not a real command (the skill is
# solo-cycle), so a seeded `/cycle` would fail and re-strand the spoke.
_afk_resume_prompt() {
  local issue="$1"
  cat <<EOF
Your session crashed and the AFK supervisor restored this window. Your committed work is
intact -- do NOT start over. Run /source-task $issue to re-anchor, re-read your task ledger
and the working tree to see where you left off, then continue the solo flow (RED -> GREEN ->
REVIEW -> PUSH) from there. Push each subtask and emit the ready marker when the issue's
acceptance criteria are all met. Do NOT self-land -- the hub lands #$issue.
EOF
}

# _afk_nudge_prompt <issue> -> the continue-nudge message for a finished-turn-idle spoke (#255).
# Unlike _afk_resume_prompt (a crash re-anchor for a relaunched session), this rides into the
# SAME live session: the spoke finished its turn and just stopped, so it only needs to be told
# nothing is blocking it and to pick the cycle back up — no re-anchor, no "your session crashed".
_afk_nudge_prompt() {
  local issue="$1"
  cat <<EOF
You finished your turn but stopped mid-cycle without continuing, and nothing is blocking you --
no question or permission dialog is pending. Re-read your task ledger and the working tree, then
continue the solo flow (RED -> GREEN -> REVIEW -> PUSH) from where you left off. Push each
subtask and emit the ready marker when the issue's acceptance criteria are all met. Do NOT
self-land -- the hub lands #$issue.
EOF
}

# _afk_route_subtask_prompt <spoke> <issue> -> the #278 message telling a LIVE spoke that a
# newly-filed issue sharing its scope has been queued onto its branch. Deliberately does NOT
# say "stop what you are doing": the spoke finishes its current subtask first, and the queue
# is consumed at the ready boundary — the only point where its tree is provably clean and
# pushed, so a fresh RED can never land in a tree with an in-flight push gate running.
_afk_route_subtask_prompt() {
  local spoke="$1" issue="$2" marker_dir
  marker_dir="$(wt_marker_script_dir "${_AFK_TOPLEVEL:-.}")"
  cat <<EOF
Issue #$issue was just filed and shares this spoke's scope, so it has been QUEUED onto THIS
branch as a subtask rather than spawning a second worktree (which would pay another full
spawn + first-push suite seed + review + land for the same files).

Finish what you are doing first -- do NOT abandon the current subtask. Then, at your next
clean-and-pushed boundary: run '/source-task $issue' to re-anchor on it, work its full
solo-cycle (RED -> GREEN -> REVIEW -> PUSH), and emit
'bash ${marker_dir}/spoke-push.sh --ready $issue' -- that clears it from your queue.

Check what you still owe with 'bash ${marker_dir}/spoke-ready.sh --queued $spoke'. Your
terminal 'bash ${marker_dir}/spoke-push.sh --ready $spoke' is REFUSED until that prints
nothing, so emit it only once the queue is empty. Do NOT self-land -- the hub lands these.
EOF
}

# _afk_finish_up_prompt <issue> -> the #256 finish-up nudge message for an over-ceiling spoke
# whose task ledger is near-complete: it is essentially DONE, so it is told to do the LAST step
# (verify committed + pushed, then emit ready / the final push), NOT to start fresh work.
_afk_finish_up_prompt() {
  local issue="$1" marker_dir
  # Name the marker-emitter path that EXISTS in the spoke's worktree (#271): `scripts` in the
  # ai-toolkit checkout, `.ai-toolkit/scripts` in a synced target — probed off the hub layout the
  # spoke shares. A hardcoded `.ai-toolkit/scripts/` here hands an ai-toolkit spoke a path the
  # deny-wall approves (textually in-tree) but that then fails to exec — the #271 failure mode.
  marker_dir="$(wt_marker_script_dir "${_AFK_TOPLEVEL:-.}")"
  cat <<EOF
You have run past the AFK time ceiling, but your task ledger shows you are essentially DONE --
almost every todo is complete and nothing is blocking you. Do the LAST step now: make sure your
work is committed and pushed, then emit the ready marker
(bash ${marker_dir}/spoke-push.sh --ready $issue) once the issue's acceptance criteria are
all met. If a final push is still pending, push it first. Do NOT start new work and do NOT
self-land -- the hub lands #$issue.
EOF
}

# _afk_pushed_unmarked_prompt <issue> -> the #305 nudge for a clean-pushed tip that carries NO
# ready marker and is NOT over the ceiling (the #200 shape #299 stranded on): the tree is pushed
# and clean but the spoke stopped without emitting ready. Distinct from _afk_finish_up_prompt (an
# over-ceiling finish-up) so the message is accurate -- it says nothing about a time ceiling. Names
# the marker-emitter path that EXISTS in the spoke's worktree (the #271 probe).
_afk_pushed_unmarked_prompt() {
  local issue="$1" marker_dir
  marker_dir="$(wt_marker_script_dir "${_AFK_TOPLEVEL:-.}")"
  cat <<EOF
Your branch is pushed and your working tree is clean, but you never emitted the ready marker for
#$issue -- and nothing is blocking you (no question or permission dialog is pending). If the
issue's acceptance criteria are ALL met, emit the ready marker now
(bash ${marker_dir}/spoke-push.sh --ready $issue). If you still owe subtasks, re-read your task
ledger and the working tree, then continue the solo flow (RED -> GREEN -> REVIEW -> PUSH) from
where you left off, pushing each subtask. Do NOT self-land -- the hub lands #$issue.
EOF
}


# _afk_continue_command <wt> <prompt> -> the `claude --continue '<prompt>'` launch
# command for a re-opened spoke window (crash resume, wedge respawn). Pure (returns the
# string) so it is inspectable in a test. It inline-exports the telemetry the window
# needs to keep reaching the collector — recovery must not fly blind (#108):
# AI_TOOLKIT_OTEL=1, the supervisor's OTLP endpoint, the workflow-span sink
# (AI_TOOLKIT_OTEL_SPAN_ENDPOINT, #126), and the re-pinned spoke_run_id. The
# auth header stays in the inherited env (never on the command line), exactly as
# worktree-new.sh does. `claude --continue` resumes the crashed session in the worktree.
# UPGRADE: replicate worktree-new.sh's full beta-tracing/raw-body env for per-tool parity.
_afk_continue_command() {
  local wt="$1" prompt="$2" run_id endpoint span_endpoint
  run_id="$(_afk_spoke_run_id "$wt")"
  endpoint="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4317}"
  # Workflow-span sink (#126), resume parity with worktree-new.sh: telemetry.sh's
  # cycle step:/script/hook spans are gated on this var and POST over OTLP-HTTP,
  # so it targets the collector's :4318 listener, not the gRPC endpoint above.
  span_endpoint="${AI_TOOLKIT_OTEL_SPAN_ENDPOINT:-http://localhost:4318}"
  printf 'AI_TOOLKIT_OTEL=1 OTEL_EXPORTER_OTLP_ENDPOINT=%s AI_TOOLKIT_OTEL_SPAN_ENDPOINT=%s OTEL_RESOURCE_ATTRIBUTES=%s claude --continue %s\n' \
    "$(printf '%q' "$endpoint")" "$(printf '%q' "$span_endpoint")" \
    "$(printf '%q' "spoke_run_id=$run_id")" "$(printf '%q' "$prompt")"
}

# _afk_resume_command <wt> <issue> -> the launch command for a crash-resumed window:
# a continue with the plain-English re-anchor prompt.
_afk_resume_command() { _afk_continue_command "$1" "$(_afk_resume_prompt "$2")"; }

# _afk_wedge_respawn_command <wt> <issue> <answer> -> the launch command for a pane
# respawned out of a wedged composer (#133): the ANSWER rides verbatim as the
# continuation prompt — the proven manual recovery, no supervisor preamble — so the
# respawn itself delivers what the inject could not. <issue> is unused but keeps the
# (wt, issue, ...) call-site symmetry with _afk_resume_command.
_afk_wedge_respawn_command() { _afk_continue_command "$1" "$3"; }

# _afk_open_spoke_window <wt> <issue> <cmd> -> open a fresh tmux window in the project
# session, cd'd into the worktree, running <cmd>. Mirrors worktree-new.sh's
# project-session window layout. rc 1 when tmux is unavailable or the window can't be
# opened. Shared by the crash resume and the wedge respawn (#133).
_afk_open_spoke_window() {
  local wt="$1" issue="$2" cmd="$3" sess win
  command -v tmux >/dev/null 2>&1 || return 1
  sess="$(wt_tmux_session "${MAIN_ROOT:-$(wt_main_root 2>/dev/null)}")"
  # Name the window with the branch SLUG (the "<issue>-<slug>" worktree-new.sh convention),
  # NOT the full "feature/<issue>-…" branch: _kill_spoke_window only matches "<issue>-"* /
  # "<issue>", so a full-branch name would orphan the reopened window on a later reap.
  win="$(git -C "$wt" branch --show-current 2>/dev/null)"; win="${win##*/}"; win="${win:-$issue}"
  tmux has-session -t "=$sess" 2>/dev/null || tmux new-session -d -s "$sess" -c "$wt" 2>/dev/null
  tmux new-window -t "=$sess:" -n "$win" -c "$wt" "$cmd; exec ${SHELL:-zsh}" 2>/dev/null || return 1
  # Pin the name so the running claude/zsh can't rename the window out of the kill match.
  tmux set-window-option -t "=$sess:$win" automatic-rename off 2>/dev/null || true
  return 0
}

# resume_spoke <wt> <issue> -> re-open the crashed spoke's window running the resume
# command; stamp the once-per-window marker and a success span. rc 1 when the window
# can't be opened (the caller then falls back to blocking).
resume_spoke() {
  local wt="$1" issue="$2"
  log "→ resume #$issue: pane crashed with work intact — re-adopting once"
  _afk_set_last_action "resume #$issue"
  if ! _afk_open_spoke_window "$wt" "$issue" "$(_afk_resume_command "$wt" "$issue")"; then
    log "  could not open a resume window for #$issue"
    return 1
  fi
  _afk_mark_resumed "$issue"
  stamp_progress_epoch "$issue"   # a deliberate revival resets the reap ceiling (#133)
  # Reset the IDLE clock too (#202 C review): recover_dead_panes resumes then reap_pass runs
  # in the SAME tick, and _spoke_idle_seconds measures the STALE transcript mtime (the fresh
  # window has not written yet) — not the progress epoch — so a resumed idle-crashed spoke
  # would be re-reaped as "live pane, likely hung" and its just-restored work blocked. The
  # answer-attempt epoch is the idle clock's exclusion, so stamping it reads the revived spoke
  # busy until its new session writes a transcript.
  stamp_answer_attempt "$issue"
  # #300 step 3: re-adopting a crashed-but-intact pane is a revive transition (cause distinct
  # from _revive_spoke's kill-and-relaunch — this one never kills, the pane was already dead).
  _afk_tlog_transition "$wt" "$issue" revived \
    "pane crashed with work intact — re-adopted in place once" '{"path":"resume"}'
  _afk_bump_count "$wt" relaunch-count   # #231: a relaunch — failure economics vs a clean run
  _afk_clear_park_episode "$wt"          # #231: a fresh run may re-park → count the next block anew
  _afk_emit_span "$wt" afk-resume success
  return 0
}

# respawn_wedged_spoke <wt> <issue> <answer> -> recover a wedged composer (an
# unterminated paste no keystroke can submit or clear, #123/#124): kill the spoke's
# window and reopen it running `claude --continue '<answer>'` under the same
# spoke_run_id — the respawn itself delivers the answer, so the park is resolved.
# Delivery is CONFIRMED like an inject: the continued session must start writing its
# transcript, else a window whose `claude` died instantly (dead auth, PATH) would be
# scored success and the answer silently lost. rc 1 when the window can't be
# reopened or never starts writing (the caller escalates).
respawn_wedged_spoke() {
  local wt="$1" issue="$2" answer="$3" before
  log "→ respawn #$issue: composer wedged (unterminated paste) — respawning the pane with the answer"
  before="$(_transcript_mtime "$wt")"
  _kill_spoke_window "$issue"
  if ! _afk_open_spoke_window "$wt" "$issue" "$(_afk_wedge_respawn_command "$wt" "$issue" "$answer")"; then
    log "  could not open a respawn window for #$issue"
    return 1
  fi
  if ! _transcript_advanced "$wt" "$before"; then
    log "  respawned window never started writing its transcript — escalating"
    return 1
  fi
  stamp_progress_epoch "$issue"   # a deliberate revival resets the reap ceiling (#133)
  _afk_bump_count "$wt" relaunch-count   # #231: a relaunch — failure economics vs a clean run
  _afk_clear_park_episode "$wt"          # #231: a fresh run may re-park → count the next block anew
  _afk_emit_span "$wt" afk-wedge-respawn success
  return 0
}

# --- #241 §7/§8: revive-first, warned-parked-LAST, never abandon -----------------
# The reaper no longer kills a stuck spoke into blocked/<issue>. Every former reap TAKES a
# revival first (kill any hung/crashed pane + relaunch `claude --continue`); only a spoke whose
# revival was ALREADY tried this window downgrades to warned-and-parked-LAST (warn + journal +
# arm the warned-retry backoff, retried at low frequency), NEVER killed or abandoned.

# AFK_WARN_ESCALATE_ATTEMPTS: #305 — the warn count after which a mode=afk warn-park that is
# STALLING scope-blocked dependents stops warn-parking silently and escalates blocked/<issue>. The
# count is the warned-retry `attempt` (exponential backoff), so the default 3 is ~7 min of standing
# failure (60 + 120 + 240s), not 3 ticks — long enough that a transient blip clears first, short
# enough that a night is never lost. A non-numeric override falls back so a typo can't disable the
# escalation (mirroring AFK_NUDGE_MAX_ATTEMPTS' guard).
: "${AFK_WARN_ESCALATE_ATTEMPTS:=3}"
case "$AFK_WARN_ESCALATE_ATTEMPTS" in '' | *[!0-9]*) AFK_WARN_ESCALATE_ATTEMPTS=3 ;; esac

# _afk_warn_attempt <issue> [lane] -> the warned-retry attempt count already tracked in
# _afk_warned_state_file ("<attempt>\t<next>"), or 0 when never warned on that lane. Reads the
# gate-broker record directly (the file has no field-1 reader — _afk_warned_next reads field 2).
_afk_warn_attempt() {
  local f a=0
  f="$(_afk_warned_state_file "$1" "${2:-}")"
  [ -f "$f" ] && IFS=$'\t' read -r a _ <"$f" 2>/dev/null || true
  case "$a" in '' | *[!0-9]*) a=0 ;; esac
  printf '%s\n' "$a"
}

# _warn_parked_last <wt> <issue> <reason> [park_kind=reap] -> the never-abandon replacement for
# reap_spoke: keep the spoke in rotation on the warned-retry backoff. NO window kill, NO
# blocked/<issue>. It HONORS the backoff — it warns + journals only when the spoke is DUE, and
# parks LAST SILENTLY inside the backoff window — so a permanently-stuck spoke is retried (and
# re-warned) at LOW frequency, not warned + gh-commented every 5-minute tick. reversible: the
# spoke's committed work is intact.
#
# #305 exception — the ONE place a warn-park is NOT cheap: an unattended (mode=afk) park that has
# persisted past AFK_WARN_ESCALATE_ATTEMPTS WHILE other issues are scope-blocked behind it. Silence
# there costs the whole window + everything queued (the #299 shape). So a DUE such park escalates a
# loud, reversible blocked/<issue> (the one marker hub-notify pings under a live drain) naming the
# stalled dependents, instead of warn-parking again. Gated on all three — a positive afk read, the
# attempt bound, AND real dependents — so it is inert for: attended parks (the human is the wall,
# AC2), worktree-less parks (dispatch failures, wt=""), and any park with nothing waiting behind it
# (warn-parking is genuinely harmless then). The irreversible/outward carve-out is untouched.
_warn_parked_last() {
  local wt="$1" issue="$2" reason="$3" park="${4:-reap}" lane
  lane="$(_afk_warned_lane "$park")"
  # Gate on the SAME lane broker_warn_continue arms for this park kind (#274): a land/review park
  # reads/arms auto_land's LAND lane, every other kind the default lane — so the due-check and the
  # arm stay on one clock. Inside the backoff → parked LAST silently this tick.
  _afk_warned_due "$issue" "" "$lane" || return 0
  # #305: past-bound afk park stalling dependents → escalate loudly instead of re-warning.
  if [ -n "$wt" ] && [ "$(_afk_spoke_mode "$wt")" = afk ] \
     && [ "$(_afk_warn_attempt "$issue" "$lane")" -ge "$AFK_WARN_ESCALATE_ATTEMPTS" ]; then
    local behind; behind="$(_afk_scope_blocked_behind "$issue")"
    if [ -n "$behind" ]; then
      local ereason="$reason — persisted past its warn bound (${AFK_WARN_ESCALATE_ATTEMPTS} warns) while STALLING scope-blocked dependents: $behind. Escalated blocked/$issue for a human (#305)."
      log "→ warn-escalate #$issue: $ereason"
      _afk_set_last_action "warn-escalate #$issue"
      broker_journal_decision "$issue" "$park" "$ereason" reversible
      _afk_park_terminal "$wt"
      _afk_escalate_blocked "$wt" "$issue" "$ereason"
      return 0
    fi
  fi
  log "→ warn-park-LAST #$issue: $reason"
  _afk_set_last_action "warn-park #$issue"
  broker_warn_continue "$wt" "$issue" "$park" "$reason" reversible
  # #231: this IS the live disaster-terminal path post-#241 — stamp outcome=blocked + build the
  # view (once per episode) so a never-landing spoke is distinguishable from a clean landing.
  _afk_park_terminal "$wt"
}

# _revive_spoke <wt> <issue> -> kill any hung/crashed window and relaunch the spoke via
# `claude --continue` under the same spoke_run_id, resetting the reap + idle clocks (#133/#202
# C: the fresh window hasn't written a transcript yet, so stamp the answer-attempt epoch or the
# same-tick reap_pass re-reaps it as idle). Marks the once-per-window revival. rc 1 when the
# window could not be opened (the caller warns + retries next tick).
_revive_spoke() {
  local wt="$1" issue="$2" bundle
  log "→ revive #$issue: killing any hung/crashed pane and relaunching (claude --continue)"
  _afk_set_last_action "revive #$issue"
  # #243: capture the hang forensics BEFORE the kill destroys them (a live pane leaves a bundle;
  # a spoke whose window is already gone echoes nothing and is skipped). Best-effort — a failed
  # capture never blocks the revive. (Also fires on the over-ceiling revive path; over-capture is
  # harmless and the fingerprint's silence delta distinguishes a real hang from a merely-slow run.)
  bundle="$(_afk_capture_hang_forensics "$wt" "$issue")"
  _kill_spoke_window "$issue"
  if ! _afk_open_spoke_window "$wt" "$issue" "$(_afk_resume_command "$wt" "$issue")"; then
    log "  could not open a revive window for #$issue"
    return 1
  fi
  _afk_mark_resumed "$issue"
  stamp_progress_epoch "$issue"
  stamp_answer_attempt "$issue"
  # #241 §10: a revival is a taken decision the morning review sees — journal it (a successful
  # revival is not a loud warned record, just an auditable journal line + span). #243: name the
  # forensics bundle in the journal line so the morning review can open it.
  broker_journal_decision "$issue" revive \
    "revived a hung/crashed pane (killed + relaunched claude --continue)${bundle:+ — hang forensics: $bundle}" reversible
  # #300 step 3: the drain reviving this spoke is a lifecycle transition — record it.
  _afk_tlog_transition "$wt" "$issue" revived \
    "killed a hung/crashed pane and relaunched claude --continue" \
    "{\"path\":\"revive\"${bundle:+,\"forensics\":\"$bundle\"}}"
  _afk_bump_count "$wt" relaunch-count   # #231: a relaunch — failure economics vs a clean run
  _afk_clear_park_episode "$wt"          # #231: a fresh run may re-park → count the next block anew
  _afk_emit_span "$wt" afk-revive success
  return 0
}

# _afk_nudge_spoke <wt> <issue> -> deliver a continue-nudge into the spoke's LIVE session via the
# shared hardened injector (inject_and_verify: paste-buffer + verified submit — the SAME primitive
# the answerer uses), then journal the taken decision (#255). Unlike a revive, a nudge does NOT
# reset the wall-clock reap ceiling (progress epoch): the caller only reaches here UNDER the
# ceiling, and an answer-attempt-shaped action must not buy a spoke a fresh full ceiling (cf. the
# #241 §8 "answer attempts must not reset the reap clock" note). It DOES stamp the answer-attempt
# epoch so the same-tick / next-tick reap does not immediately re-reap the just-nudged spoke off a
# stale transcript mtime (#202 C). rc 0 when the nudge delivered, else inject_and_verify's rc (the
# caller has already counted the attempt; a failed delivery just retries next tick until the
# budget falls back to a revive). Caller wraps this in _afk_run_with_heartbeat_fg because
# inject_and_verify polls up to AFK_INJECT_VERIFY_SECONDS.
_afk_nudge_spoke() {
  local wt="$1" issue="$2" target rc
  log "→ nudge #$issue: finished-turn-idle — injecting a continue message into the live session (no relaunch)"
  _afk_set_last_action "nudge #$issue"
  target="$(_spoke_pane_target "$wt")"
  if [ -z "$target" ]; then
    log "  no live pane for #$issue — cannot nudge"
    return 1
  fi
  stamp_answer_attempt "$issue"
  inject_and_verify "$wt" "$target" "$(_afk_nudge_prompt "$issue")"; rc=$?
  broker_journal_decision "$issue" nudge \
    "finished-turn-idle: injected a continue-nudge into the live session (no relaunch)" reversible
  # #300 step 3: the #255 nudge lane records its event (delivered vs retry via the rc), so a
  # reader can tell "the drain nudged this spoke" apart from "the spoke is silently idle".
  _afk_tlog_event "$wt" "$issue" nudge nudge \
    "{\"delivered\":$([ "$rc" -eq 0 ] && printf true || printf false)}"
  if [ "$rc" -eq 0 ]; then _afk_emit_span "$wt" afk-nudge success; else _afk_emit_span "$wt" afk-nudge retry; fi
  return "$rc"
}

# _afk_finish_up_nudge <wt> <issue> -> #256: an over-ceiling spoke whose ledger is near-complete
# gets a FINISH-UP nudge (emit ready / final push) injected into its LIVE session, instead of the
# kill + relaunch a blind ceiling reap would do. Mirrors _afk_nudge_spoke but carries the finish-up
# prompt and journals a DISTINCT `finish-up` decision + span, so the morning review sees "ceiling
# hit -> nudged to finish, not reaped" (AC3). Like the #255 nudge it stamps only the answer-attempt
# epoch, never the progress epoch — a finishing spoke must not buy a fresh full ceiling. rc mirrors
# inject_and_verify (the caller already counted the attempt; a failed delivery retries next tick
# until the shared nudge budget falls back to the revive).
_afk_finish_up_nudge() {
  local wt="$1" issue="$2" target rc
  log "→ finish-up #$issue: over the time ceiling but ledger near-complete — nudging it to emit ready / final push (no relaunch)"
  _afk_set_last_action "finish-up #$issue"
  target="$(_spoke_pane_target "$wt")"
  if [ -z "$target" ]; then
    log "  no live pane for #$issue — cannot nudge"
    return 1
  fi
  stamp_answer_attempt "$issue"
  inject_and_verify "$wt" "$target" "$(_afk_finish_up_prompt "$issue")"; rc=$?
  broker_journal_decision "$issue" finish-up \
    "time ceiling hit but ledger near-complete — nudged to finish (emit ready / final push), not reaped (#256)" reversible
  if [ "$rc" -eq 0 ]; then _afk_emit_span "$wt" afk-finish-up success; else _afk_emit_span "$wt" afk-finish-up retry; fi
  return "$rc"
}

# _afk_pushed_unmarked_nudge <wt> <issue> -> #305: the first rung of the pushed-but-unmarked ACT
# ladder. Injects the emit-ready / continue nudge (_afk_pushed_unmarked_prompt) into the LIVE
# session via the shared hardened injector, then journals a `markready` decision. Mirrors
# _afk_finish_up_nudge: stamps only the answer-attempt epoch (never the progress epoch — a nudge
# must not buy a fresh full ceiling), and rc mirrors inject_and_verify (the caller counts the
# attempt against the shared #255 budget). Caller wraps this in _afk_run_with_heartbeat_fg.
_afk_pushed_unmarked_nudge() {
  local wt="$1" issue="$2" target rc
  log "→ pushed-unmarked-nudge #$issue: clean pushed tip, no ready marker — injecting the emit-ready / continue nudge (no relaunch)"
  _afk_set_last_action "pushed-unmarked-nudge #$issue"
  target="$(_spoke_pane_target "$wt")"
  if [ -z "$target" ]; then
    log "  no live pane for #$issue — cannot nudge"
    return 1
  fi
  stamp_answer_attempt "$issue"
  inject_and_verify "$wt" "$target" "$(_afk_pushed_unmarked_prompt "$issue")"; rc=$?
  broker_journal_decision "$issue" markready \
    "pushed-but-unmarked: nudged the live session to emit ready / continue the cycle (no relaunch, #305)" reversible
  if [ "$rc" -eq 0 ]; then _afk_emit_span "$wt" afk-pushed-unmarked-nudge success; else _afk_emit_span "$wt" afk-pushed-unmarked-nudge retry; fi
  return "$rc"
}

# _afk_crash_reresume_or_escalate <wt> <issue> <reason> <retry_fn> -> the #310 crash terminus that
# replaces the eternal "parked LAST, retried at low frequency" warn (which never retried — only the
# warn re-fired). UNATTENDED ONLY: the whole ladder (re-resume + escalate) is gated on mode=afk, so
# an ATTENDED crashed-again spoke keeps today's warn-and-wait — the human is the wall (AC5). Under
# afk, on the warned-lane (default/reap) backoff cadence: while the warned-retry attempt count is
# UNDER AFK_WARN_ESCALATE_ATTEMPTS, genuinely RE-ATTEMPT the revival (<retry_fn> is _revive_spoke for
# a kill+relaunch site or resume_spoke for a re-adopt site) so a transient crash (an API blip, a
# sleep/wake) self-heals mid-window — advancing the backoff and journaling each try. Inside the
# backoff window it is a SILENT park (nothing is scheduled this tick, so nothing is claimed). Once
# the budget is spent it hands to the crash terminus. The message is HONEST: it names the scheduled
# retry (attempt k/N) or the escalation, never a retry that will not happen (AC4). The attempt count
# REUSES the warned-lane record (_afk_warn_attempt), so the retry cadence and the escalation bound
# share one clock — no separate marker to drift (reuse of AFK_WARN_ESCALATE_ATTEMPTS, not a new knob).
_afk_crash_reresume_or_escalate() {
  local wt="$1" issue="$2" reason="$3" retry_fn="$4" lane attempts max
  # AC5 regression pin: attended (and worktree-less) parks keep the old warn-and-parked-LAST — no
  # auto relaunch, no escalation. The entire #310 crash ladder is an unattended-drain behavior.
  if [ -z "$wt" ] || [ "$(_afk_spoke_mode "$wt")" != afk ]; then
    _warn_parked_last "$wt" "$issue" "$reason — parked LAST, retried at low frequency"
    return 0
  fi
  lane="$(_afk_warned_lane reap)"                       # the default/reap lane (empty)
  _afk_warned_due "$issue" "" "$lane" || return 0       # inside the backoff — parked LAST silently
  attempts="$(_afk_warn_attempt "$issue" "$lane")"
  max="$AFK_WARN_ESCALATE_ATTEMPTS"
  if [ "$attempts" -lt "$max" ]; then
    local msg="$reason — re-attempting the revival (attempt $(( attempts + 1 ))/$max)"
    log "→ crash-reresume #$issue: $msg"
    _afk_set_last_action "crash-reresume #$issue"
    broker_journal_decision "$issue" reap "$msg" reversible
    _afk_warned_arm "$issue" "$lane"                    # advance the backoff for the next attempt/escalation
    "$retry_fn" "$wt" "$issue" \
      || log "  crash-reresume #$issue: revival relaunch could not be started; retrying next cadence"
    return 0
  fi
  _afk_crash_escalate_or_park "$wt" "$issue" "$reason — resume budget exhausted (${max} attempts)"
}

# _afk_crash_escalate_or_park <wt> <issue> <reason> -> the terminus for a spoke whose crash-retry
# budget is spent (#310). Under an unattended drain (mode=afk) the parked issue IS the stalled work,
# so escalate a loud, reversible blocked/<issue> + notification EVEN WITH ZERO scope-blocked
# dependents (Principle 3 — act when unattended). This is a DEDICATED crash path, so _warn_parked_last's
# generic #305 dependents gate is deliberately left untouched (a benign land/backoff park must not
# escalate without dependents). blocked/ flips slot_state terminal, silencing the watchdog's dead-pane
# race instead of losing the rest of the window to it. Attended (mode != afk) keeps today's
# warn-and-wait — the human is the wall (the AC5 regression pin).
_afk_crash_escalate_or_park() {
  local wt="$1" issue="$2" reason="$3"
  if [ -n "$wt" ] && [ "$(_afk_spoke_mode "$wt")" = afk ]; then
    local ereason="$reason. Escalated blocked/$issue for a human (#310)."
    log "→ crash-escalate #$issue: $ereason"
    _afk_set_last_action "crash-escalate #$issue"
    broker_journal_decision "$issue" reap "$ereason" reversible
    _afk_park_terminal "$wt"
    _afk_escalate_blocked "$wt" "$issue" "$ereason"
    return 0
  fi
  _warn_parked_last "$wt" "$issue" "$reason"
}

# _afk_revive_or_park_last <wt> <issue> <reason> -> revive-first, then the #310 crash ladder. If a
# revival was already tried this window (_afk_already_resumed) the spoke enters the bounded re-revive
# -> escalate terminus (never abandoned, never an eternal warn); otherwise it revives once here.
_afk_revive_or_park_last() {
  local wt="$1" issue="$2" reason="$3"
  if _afk_already_resumed "$issue"; then
    _afk_crash_reresume_or_escalate "$wt" "$issue" "$reason — revival already tried this window" _revive_spoke
    return 0
  fi
  _revive_spoke "$wt" "$issue" \
    || _warn_parked_last "$wt" "$issue" "$reason — revival launch could not be started; retrying"
}

# _afk_finish_up_or_revive <wt> <issue> <reason> -> #256: the ceiling-hit decision. A spoke over
# the wall-clock ceiling is NOT automatically a runaway — if it shows a COMPLETION signal it is
# essentially DONE and one step from ready, so a blind revive (kill + relaunch) throws away
# finished work (the 2026-07-12 #241 incident: reaped at 33/33 todos, all committed + pushed).
# Prefer a finish-up nudge / the pushed-but-unmarked warn over a kill; only a spoke over the
# ceiling with NO completion signal is a true runaway to revive. Both signal branches are
# pane-alive-gated, so at the recover_dead_panes call site (dead pane) they collapse to the
# revive below — crashed-pane behavior is unchanged.
_afk_finish_up_or_revive() {
  local wt="$1" issue="$2" reason="$3"
  # Signal 1: a clean pushed-ahead tip with no marker (#200) — surface it actionably (re-run
  # --ready / land by hand), never kill. (In _reap_or_resume the live case is already caught
  # upstream; keeping it here makes the fn self-contained + correct for both call sites.)
  if _spoke_pane_alive "$wt" && _afk_pushed_but_unmarked "$wt" "$issue"; then
    _afk_warn_pushed_but_unmarked "$wt" "$issue"
    return 0
  fi
  # Signal 2: a near-complete task ledger on a finished-turn-idle pane — nudge it to finish up
  # (emit ready / final push) rather than relaunch. Gated on _transcript_finished_turn_idle so the
  # pane is genuinely at the prompt and the nudge can land (a near-complete-but-hung pane falls
  # through to the revive). Bounded by the SHARED per-window nudge budget (#255) so a spoke that
  # will not finish still falls through to the revive.
  if _spoke_pane_alive "$wt" \
     && _afk_ledger_near_complete "$wt" \
     && _transcript_finished_turn_idle "$wt" \
     && [ "$(_afk_read_nudge_count "$issue")" -lt "$AFK_NUDGE_MAX_ATTEMPTS" ]; then
    _afk_incr_nudge_count "$issue" >/dev/null
    _afk_run_with_heartbeat_fg _afk_finish_up_nudge "$wt" "$issue"
    return 0
  fi
  # No completion signal (low progress, no pushed work) — a true runaway. Revive-first, park-LAST.
  _afk_revive_or_park_last "$wt" "$issue" "$reason"
}

# _afk_warn_pushed_but_unmarked <wt> <issue> -> #200/#241/#305: the pushed-but-unmarked handler,
# dispatched on the spoke's execution mode. The shape is AMBIGUOUS — genuinely finished (the marker
# just failed, as #299) vs idle BETWEEN subtasks — and today's warn-and-wait is correct ONLY when a
# human is the wall. So:
#   * mode=attended -> keep the warn-and-parked-LAST warn (the human re-runs --ready or lands by
#     hand); NOT auto-marked, since auto-emitting ready could auto-LAND incomplete work onto main.
#   * mode=afk -> the human is NOT there, so warn-parking forever wastes the window (the #299
#     incident: a 10h stall that jammed everything scope-blocked behind it). ACT via the
#     nudge -> relaunch -> decide ladder (_afk_act_pushed_but_unmarked) instead.
# The mode is read from _afk_spoke_mode (gate-broker-permission.sh's empty-default helper, the
# deny-wall's fail-safe signal); the attended DEFAULT is applied HERE (afk only on a positive read),
# so a missing/unknown pointer keeps today's warn-and-wait — the conservative, regression-safe side.
_afk_warn_pushed_but_unmarked() {
  local wt="$1" issue="$2"
  if [ "$(_afk_spoke_mode "$wt")" = afk ]; then
    _afk_act_pushed_but_unmarked "$wt" "$issue"
    return
  fi
  _warn_parked_last "$wt" "$issue" \
    "pushed-but-unmarked (#200): clean tip, no ready/$issue marker — if finished, re-run 'spoke-push.sh --ready $issue' or land by hand" \
    markready
}

# _afk_act_pushed_but_unmarked <wt> <issue> -> #305: the ACT ladder for a mode=afk clean-pushed tip
# with no marker. nudge -> relaunch -> decide, each rung bounded by an existing per-window counter so
# it CANNOT loop forever (the AC1 "never warn-parks indefinitely" guarantee):
#   1. nudge   — a finished-turn-idle pane under the shared #255 nudge budget gets the emit-ready /
#                continue nudge injected into its LIVE session (no relaunch). This is exactly the
#                lane the pushed-but-unmarked warn short-circuited PAST before #305.
#   2. relaunch— nudge budget spent (or the pane is hung/dead) and not yet revived this window ->
#                _revive_spoke (kill + claude --continue; committed work survives, as #299's did).
#   3. decide  — nudge budget spent AND already revived -> _afk_decide_pushed_but_unmarked: a LOUD
#                terminal blocked/<issue> escalation (never an auto-land of ambiguous work).
# Because blocked/<issue> at the tip reads terminal (`done`) in slot_state, the decide rung takes the
# spoke OUT of the reap rotation — the ladder always terminates.
_afk_act_pushed_but_unmarked() {
  local wt="$1" issue="$2"
  if _spoke_pane_alive "$wt" \
     && _transcript_finished_turn_idle "$wt" \
     && [ "$(_afk_read_nudge_count "$issue")" -lt "$AFK_NUDGE_MAX_ATTEMPTS" ]; then
    _afk_incr_nudge_count "$issue" >/dev/null
    _afk_run_with_heartbeat_fg _afk_pushed_unmarked_nudge "$wt" "$issue"
    return 0
  fi
  if ! _afk_already_resumed "$issue"; then
    _revive_spoke "$wt" "$issue" && return 0
    # revival launch could not start — fall through to the terminal decision.
  fi
  _afk_decide_pushed_but_unmarked "$wt" "$issue"
}

# _afk_decide_pushed_but_unmarked <wt> <issue> -> #305: the ladder's terminal rung. Nudge + relaunch
# both failed to produce a marker, so the spoke is genuinely stuck at a clean-pushed tip. The
# acceptance evidence is on disk (clean tree, pushed==upstream), but the shape is ambiguous with
# idle-between-subtasks, so we do NOT auto-land — we escalate a LOUD, reversible blocked/<issue> (the
# one marker hub-notify pings under a live drain) naming any scope-blocked dependents, and journal
# the decision (#241) so the morning review can land it or re-run --ready.
_afk_decide_pushed_but_unmarked() {
  local wt="$1" issue="$2" behind reason
  behind="$(_afk_scope_blocked_behind "$issue")"
  reason="pushed-but-unmarked (#200/#305): clean pushed tip, no ready/$issue marker; nudge + relaunch both failed to produce it${behind:+ — STALLING scope-blocked dependents: $behind}. Landing evidence is on disk (clean tree, pushed==upstream); escalated blocked/$issue for a human to land or re-run --ready."
  broker_journal_decision "$issue" markready "$reason" reversible
  _afk_park_terminal "$wt"
  _afk_escalate_blocked "$wt" "$issue" "$reason"
}

# _reap_or_resume <wt> <issue> -> #241 §7/§8: revive-first, never block. A finished-but-unmarked
# spoke (#200) is auto-marked. Every other stuck spoke — over-ceiling runaway, hung LIVE pane
# (a frozen claude is a revival case, not a block), or crashed pane — is revived; a spoke whose
# revival was already tried this window is warned-and-parked-LAST, never reaped/abandoned.
_reap_or_resume() {
  local wt="$1" issue="$2"
  # #246 defense-in-depth: a spoke still parked on an answerable dialog (a permission prompt, a
  # PLAN gate, or an extractable question) must be ANSWERED, not revived — reviving via
  # `claude --continue` only re-raises the identical dialog (the parked->reaped->revived->parked
  # loop). slot_state already keeps a detected park out of `reap`, so this only fires on a
  # same-tick slot_state flicker (answer_pass and reap_pass re-derive state independently) or a
  # future regression. _spoke_still_parked is a POSITIVE signal, so an ambiguous read falls
  # through to the revive logic below — a genuinely hung, unparked pane is unaffected (#246 item 4).
  # Wrapped in _afk_run_with_heartbeat_fg like answer_pass's identical call (#170 ST2): the
  # answerer is a high-effort headless `claude` that can run for minutes, so without the heartbeat
  # stamper the --watchdog would declare the supervisor wedged and respawn it mid-answer.
  if _spoke_still_parked "$wt" "$issue"; then
    _afk_run_with_heartbeat_fg decide_and_act "$wt" "$issue"
    return 0
  fi
  # #200/#241: a live pane at a clean-pushed tip with no marker is warned-and-parked-LAST with an
  # actionable reason (NOT auto-marked/auto-landed — the shape is ambiguous with idle-between-subtasks).
  if _spoke_pane_alive "$wt" && _afk_pushed_but_unmarked "$wt" "$issue"; then
    _afk_warn_pushed_but_unmarked "$wt" "$issue"
    return 0
  fi
  if _spoke_over_any_ceiling "$issue" "$(afk_now)"; then
    # #256: not automatically a runaway — a near-complete ledger / clean pushed-ahead tip is
    # nudged to finish up (or warned), not blind-revived; only a NO-signal spoke revives.
    _afk_finish_up_or_revive "$wt" "$issue" "time ceiling: ran >${AFK_SPOKE_MAX_MINUTES}m without finishing"
  elif _spoke_pane_alive "$wt" \
       && _transcript_finished_turn_idle "$wt" \
       && [ "$(_afk_read_nudge_count "$issue")" -lt "$AFK_NUDGE_MAX_ATTEMPTS" ]; then
    # #255: the FINISHED-TURN-IDLE class — the spoke finished its turn and stopped at the input
    # prompt (transcript ends on a completed assistant turn, no pending tool_use), distinct from a
    # pane frozen MID-TOOL_USE. It gets a lightweight continue-nudge into the LIVE session, NOT a
    # kill + relaunch — bounded to AFK_NUDGE_MAX_ATTEMPTS nudges per window, after which it falls
    # through to the revive below. Wrapped in _afk_run_with_heartbeat_fg (inject_and_verify polls
    # up to AFK_INJECT_VERIFY_SECONDS) like answer_pass's decide_and_act call.
    _afk_incr_nudge_count "$issue" >/dev/null
    _afk_run_with_heartbeat_fg _afk_nudge_spoke "$wt" "$issue"
  elif _spoke_pane_alive "$wt"; then
    # #241 §8: a live-but-frozen claude (hung mid-tool_use), or a finished-turn-idle spoke past its
    # nudge budget, is a REVIVAL case (kill the hung pane + relaunch), not a terminal block. answer
    # attempts must not reset the reap clock, so this is a revival, not a re-answer.
    _afk_revive_or_park_last "$wt" "$issue" "went idle >${AFK_IDLE_MINUTES}m with a live pane and no marker — likely hung"
  elif ! _spoke_has_commits "$wt"; then
    _afk_revive_or_park_last "$wt" "$issue" "pane crashed with no committed work to preserve"
  elif _afk_already_resumed "$issue"; then
    _afk_crash_reresume_or_escalate "$wt" "$issue" "pane crashed again after an auto-resume" resume_spoke
  else
    resume_spoke "$wt" "$issue" \
      || _warn_parked_last "$wt" "$issue" "pane crashed and the auto-resume could not be launched — retrying"
  fi
}

# _warn_all_inflight <reason> -> WARN every in-flight spoke not already at a terminal marker
# (#241 §9). Called while the drain is halted on dead auth: an auth failure is NOT the spoke's
# fault, so it is warned (loud, re-fired by hub-notify), NEVER blocked — the drain resumes
# servicing it once auth recovers. Replaces the pre-#241 _block_all_inflight (which parked them).
_warn_all_inflight() {
  local reason="$1" path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "done" ] && continue
    broker_warn "$issue" "$reason"
  done < <(inflight_worktrees)
}

# _afk_service_auth_halt -> service a raised _AFK_AUTH_FAILED WITHOUT stopping the drain (#241 §9).
# Auth is the one true external blocker, but it only HALTS DISPATCH (the _AFK_AUTH_FAILED
# short-circuits), warns the in-flight spokes loudly + repeatedly, RE-PROBES auth each tick, and
# CLEARS the flag (resuming the drain) the moment auth recovers. The re-probe is _afk_probe_state
# (#249): a network blackout is distinguished from real auth-death so a mere outage never reads as
# recovery (that would resume dispatch into a dead network) — see the tri-state branches below.
_afk_service_auth_halt() {
  # #249: the re-probe is the SECOND _afk_auth_is_dead caller, so it distinguishes network-down
  # the same way. If the network dropped while halted we CANNOT confirm recovery — a bare
  # `! _afk_auth_is_dead` would misread the connection error as "auth recovered" and resume the
  # drain into a dead network. On offline: stay halted, record the outage, refresh idle clocks,
  # re-check next tick. On alive: auth recovered — clear the flag + the outage marker and resume.
  afk_write_heartbeat   # the probes are bounded curl/`claude` calls — keep the epoch fresh
  case "$(_afk_probe_state)" in
    offline)
      _afk_note_offline_tick
      ;;
    alive)  # auth recovered — clear the flag + any outage marker and resume the drain
      _AFK_AUTH_FAILED=0
      clear_offline_since
      log "/afk: auth recovered — resuming the drain"
      ;;
    *)  # auth-dead: network up but the token is still dead — stay halted, warn the fleet again
      log "/afk: subscription auth failed — dispatch HALTED (re-run /login on the host); re-probing each tick, NOT stopping the drain (#241 §9)"
      _warn_all_inflight "subscription auth failed — dispatch halted; re-run /login on the host (retrying auth each tick)"
      ;;
  esac
}

# _afk_auth_is_dead -> true when a bounded headless `claude` no-op reports an auth failure:
# the subscription token is dead so every spoke is stalled on auth, not individually hung.
# Detection mirrors the answerer's (is_auth_failure #170 ST7): a NONZERO exit AND an
# auth-failure signature together — a healthy probe (exit 0), or a nonzero exit without an
# auth signature (a transient blip, `claude` not on PATH), reads as alive so a hiccup never
# halts the drain. AFK_AUTH_PROBE_CMD overrides the probe (tests); AFK_AUTH_PROBE_TIMEOUT
# bounds it so a wedged probe can't itself freeze the reap.
_afk_auth_is_dead() {
  local cmd raw rc
  cmd="${AFK_AUTH_PROBE_CMD:-claude -p --no-session-persistence --model claude-opus-4-8 ok}"
  raw="$(_afk_with_timeout "${AFK_AUTH_PROBE_TIMEOUT:-30}" bash -c "$cmd" 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] && is_auth_failure "$raw"
}

# --- reachability probe: network-down as a THIRD outcome (issue #249) ----------
# A connectivity blackout (a hotspot dropout, a lost home connection during a remote drain) makes
# the auth probe above fail for the WRONG reason: a fleet that is merely OFFLINE used to read as
# "subscription token dead" and get mis-blocked, stopping the whole drain. Before concluding "token
# dead", the supervisor asks the only question that matters here — can this host reach the network
# at all — with a bounded curl HEAD. curl exits 0 on ANY HTTP response (even a 401 from the
# unauthenticated API root), and nonzero ONLY when it cannot connect / resolve / times out, so this
# needs no valid credentials; NO `--fail`, or the API's unauthenticated 4xx would misread as down.
# The probe is wrapped in _afk_with_timeout so a black-hole network can't hang the tick (the same
# discipline as the gh / auth probes). AFK_NET_PROBE_CMD overrides the whole probe (tests).
: "${AFK_NET_PROBE_URL:=https://api.anthropic.com}"
: "${AFK_NET_PROBE_TIMEOUT:=10}"
# _afk_network_is_down -> rc 0 (true) when the bounded reachability probe FAILS (no network), rc 1
# (false, "up") when it succeeds OR when the probe cannot run at all (no curl and no override) — an
# unrunnable probe must NEVER read as "down", or a curl-less host would suppress every reap for the
# whole window. AFK_CURL_BIN overrides the binary (default `curl`) so the fail-open is testable.
_afk_network_is_down() {
  local secs="${AFK_NET_PROBE_TIMEOUT:-10}" cmd
  case "$secs" in '' | *[!0-9]*) secs=10 ;; esac
  if [ -z "${AFK_NET_PROBE_CMD:-}" ] && ! command -v "${AFK_CURL_BIN:-curl}" >/dev/null 2>&1; then
    return 1   # cannot probe -> fail open to "up" (normal reaping proceeds)
  fi
  cmd="${AFK_NET_PROBE_CMD:-${AFK_CURL_BIN:-curl} -sI -o /dev/null --max-time $secs ${AFK_NET_PROBE_URL:-https://api.anthropic.com}}"
  ! _afk_with_timeout "$secs" bash -c "$cmd" >/dev/null 2>&1
}

# _afk_probe_state -> the tri-state the two _afk_auth_is_dead callers branch on (#249):
#   offline   — the reachability probe failed: skip the reap pass, ride out the outage.
#   auth-dead — network up AND the auth probe returned an auth signature: block-and-halt (unchanged).
#   alive     — network up AND auth healthy: proceed normally.
# The reachability probe runs FIRST and short-circuits, so a blackout is never mistaken for dead auth.
_afk_probe_state() {
  if _afk_network_is_down; then printf 'offline\n'; return; fi
  if _afk_auth_is_dead; then printf 'auth-dead\n'; return; fi
  printf 'alive\n'
}

# _afk_note_offline_tick -> the shared response to a network-outage tick (#249): record the
# offline-since epoch (idempotent — anchors the consecutive outage for --status), refresh every
# in-flight spoke's idle + soft-ceiling clocks so the blackout never accumulates into a reap/block,
# and log the outage with its running duration. The caller keeps the heartbeat stamped.
_afk_note_offline_tick() {
  stamp_offline_since
  _afk_refresh_offline_clocks
  log "/afk: network unreachable (OFFLINE for $(offline_minutes)m) — skipping the reap pass this tick, refreshing idle clocks, riding out the outage (#249)"
}

reap_pass() {
  local path issue probed=0
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "reap" ] || continue
    # Auth probe before the FIRST reap this tick (#170 ST7): if the subscription token is
    # dead, every idle spoke is stalled on auth, not hung — reaping them one-by-one would
    # block live work into dead auth. Probe once; on a real auth failure raise the global
    # stop flag and bail, letting the main loop's _afk_service_auth_halt WARN them + re-probe
    # (never block/stop — #241 §9).
    if [ "$probed" -eq 0 ]; then
      probed=1
      afk_write_heartbeat   # the probes are bounded curl/`claude` calls — keep the epoch fresh (#170 ST2)
      # #249: distinguish network-down from auth-dead BEFORE concluding "token dead". A blackout
      # means every idle spoke is stalled on a dead NETWORK, not a dead token — reaping them would
      # mis-block a merely-offline fleet. Skip the reap this tick, refresh idle clocks so the outage
      # never accumulates into a reap/block, and re-check next tick. Auth-dead keeps the #170 ST7
      # / #241 §9 behavior (raise the halt flag; the main loop WARNs + re-probes, never blocks/stops).
      case "$(_afk_probe_state)" in
        offline)
          _afk_note_offline_tick
          return 0
          ;;
        auth-dead)
          _AFK_AUTH_FAILED=1
          log "/afk: auth probe failed during reap — halting instead of reaping spokes into dead auth"
          return 0
          ;;
        *)  # alive: network up + auth healthy — clear any outage marker and reap normally
          clear_offline_since
          ;;
      esac
    fi
    _reap_or_resume "$path" "$issue"
    # The #246 park-guard may have run decide_and_act, whose answerer can raise _AFK_AUTH_FAILED
    # (dead subscription token) mid-loop. reap_pass is the last pass, so nothing checks the flag
    # after it — bail the loop now rather than revive the remaining over-ceiling survivors into
    # dead auth (the #170 ST7 harm the top-of-loop probe already guards against for the first reap).
    if [ "$_AFK_AUTH_FAILED" -eq 1 ]; then return 0; fi
  done < <(inflight_worktrees)
}

# --- dead-pane recovery each tick (issue #202 C) ------------------------------
# reap_pass only visits a spoke once the idle ceiling elapses, so a pane that CRASHES with
# work sat stranded for hours overnight (recovered by hand ~4x). recover_dead_panes runs
# EVERY tick and acts on the crash directly — no idle wait:
#   * dead pane + work (commits or dirty WIP) → resume in place ONCE (never reap work);
#     a second crash after the resume escalates (blocked/<issue>, needs a human).
#   * dead pane + clean (nothing to preserve) → tear the empty worktree down so the issue
#     RE-DISPATCHES (not escalated); a second clean crash after that escalates.
# A live pane and a terminal/parked spoke (done/waiting) are left untouched — reap_pass owns
# the idle/hung decision, auto_land owns done, the answerer owns waiting.

# _redispatch_dead_pane <wt> <issue> -> tear down a clean, empty crashed worktree so its
# issue returns to the backlog and re-dispatches next tick. Kills the window, then removes
# the worktree via worktree-done.sh (--force since the pane is dead; --no-code skips the
# editor-workspace edit). Records the once-per-window stamp on success. AFK_REDISPATCH_CMD
# overrides the teardown for tests. rc 1 when the teardown can't run (caller escalates).
_redispatch_dead_pane() {
  local wt="$1" issue="$2" wt_done run
  # #300 step 3: read the run id BEFORE the teardown — worktree-done.sh removes the worktree,
  # so .ai-toolkit/spoke-run-id is gone by the time we'd record the redispatched transition.
  run="$(_afk_spoke_run_id "$wt")"
  log "→ redispatch #$issue: pane crashed with no work to preserve — tearing down the empty worktree so it re-dispatches"
  _kill_spoke_window "$issue"
  if [ -n "${AFK_REDISPATCH_CMD:-}" ]; then
    bash -c "$AFK_REDISPATCH_CMD"; _afk_mark_redispatched "$issue"
    AFK_TLOG_RUN="$run" wt_tlog_transition "$issue" redispatched hub-afk.sh \
      "pane crashed with no work to preserve — tore down the empty worktree to re-dispatch" \
      '{"path":"redispatch-cmd"}'
    return 0
  fi
  wt_done="$(_afk_find_script "${WT_DONE:-}" worktree-done.sh)" \
    || { log "  worktree-done.sh not found — cannot re-dispatch #$issue"; return 1; }
  if bash "$wt_done" "$issue" --force --no-code >/dev/null 2>&1; then
    _afk_mark_redispatched "$issue"
    AFK_TLOG_RUN="$run" wt_tlog_transition "$issue" redispatched hub-afk.sh \
      "pane crashed with no work to preserve — tore down the empty worktree to re-dispatch" \
      '{"path":"worktree-done"}'
    return 0
  fi
  log "  worktree-done.sh failed for #$issue — leaving the worktree in place"
  return 1
}

recover_dead_panes() {
  local path issue state
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    state="$(slot_state "$path" "$issue")"
    # `done` is terminal regardless of liveness (a ready/accept/blocked marker is a human/gate
    # decision — never revive over it).
    case "$state" in done) continue ;; esac
    # `waiting` means "parked, the answer lane owns it" — but a park is only real if the AGENT is
    # there to be answered. #301: a dead agent whose pane still renders a stale dialog, or carries
    # a gate/<issue> tag at the tip (the #296/#299 shape), classifies `waiting` off scrollback / a
    # git tag that outlived the agent; skipping such a pane here would strand the very crash this
    # function exists to recover. So honor `waiting` (and hand the live pane to reap_pass) only
    # when the agent is alive — ST3 also stops slot_state emitting it, this is the belt to that
    # braces. Probed ONCE (a second _spoke_pane_alive is a second `tmux list-panes` — the #269 flake).
    if _spoke_pane_alive "$path"; then continue; fi        # live pane — reap_pass / answer lane own it
    # An over-ceiling runaway always blocks (as reap_pass does) — resume/re-dispatch never
    # applies. Checked first so a crashed-but-over-ceiling spoke is not revived here only to
    # be blocked by reap_pass in the same tick (the hard ceiling ignores fresh progress).
    # #241 §7: revive-first, warned-parked-LAST — never reap/block/abandon a crashed pane.
    if _spoke_over_any_ceiling "$issue" "$(afk_now)"; then
      # #256: same completion-signal gate as reap_pass. This path only runs for a DEAD pane, so
      # the pane-alive-gated signals collapse to the revive — crashed-pane behavior is unchanged.
      _afk_finish_up_or_revive "$path" "$issue" "time ceiling: ran >${AFK_SPOKE_MAX_MINUTES}m without finishing"
    elif _spoke_has_work "$path"; then
      if _afk_already_resumed "$issue"; then
        _afk_crash_reresume_or_escalate "$path" "$issue" "pane crashed again after an auto-resume" resume_spoke
      else
        resume_spoke "$path" "$issue" \
          || _warn_parked_last "$path" "$issue" "pane crashed and the auto-resume could not be launched — retrying"
      fi
    elif _afk_already_redispatched "$issue"; then
      _warn_parked_last "$path" "$issue" "pane crashed clean again after a re-dispatch — parked LAST, retried at low frequency"
    else
      _redispatch_dead_pane "$path" "$issue" \
        || _warn_parked_last "$path" "$issue" "pane crashed clean and the worktree teardown failed — retrying"
    fi
  done < <(inflight_worktrees)
}

