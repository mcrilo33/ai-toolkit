#!/usr/bin/env bash
# hub-afk.sh — unattended backlog-drain supervisor for the planning hub (issue #71).
#
# The single toggle that ties the parallel-worktrees workflow together: for a bounded
# window (or until the backlog is empty), keep the backlog draining as fast as the
# dependency graph allows, with ZERO human input. It is the sole unattended
# backlog-drain supervisor.
#
# Each supervisor tick does four mechanical things and exactly ONE reasoning thing:
#   1. PLAN + DISPATCH the next concurrent batch via batch-plan.sh (#70), seeding each
#      spoke with the standard ultra kickoff. Already-in-flight spokes are picked up.
#   2. AUTO-ANSWER every spoke parked on a question / gate (⚠ WAITING ON INPUT): extract
#      the prompt from its transcript, hand it to an ANSWERER (a headless `claude -p` with
#      a thinking budget) that follows the `afk-answering` rule, and inject the returned
#      answer into the spoke's tmux pane. This single reasoning step is the sanctioned
#      exception to the scripted control plane; everything else stays scripted. Each
#      decision is emitted as a telemetry span so it surfaces on the #35 dashboard.
#   3. AUTO-LAND every ready/<issue> via worktree-land.sh (suite + merge + push + teardown
#      + close). A land frees a dependent's blocker and its scope, so the next tick's plan
#      unlocks more. A failed land emits blocked/<issue> and the drain continues.
#   4. REAP hung / idle / over-ceiling spokes (blocked/<issue>), so a doom-loop can't burn
#      the whole window.
# It STOPS at the time bound, or — in drain mode — when the backlog is empty and nothing
# is in flight. No report artifact is written: the observability dashboard is the single
# source of truth for what happened.
#
# Knobs (env, with defaults):
#   AFK_TICK_SECONDS=120         supervisor poll interval
#   AFK_WATCHDOG_SECONDS=60      watchdog poll interval (respawn a crashed supervisor)
#   AFK_SPOKE_MAX_MINUTES=180    wall-clock ceiling per spoke before a reap
#   AFK_IDLE_MINUTES=30          a spoke idle this long with no marker AND not waiting → reap
#   AFK_ANSWERER_CMD             the answerer command (default: claude -p --model claude-fable-5)
#   AFK_ANSWERER_EFFORT=high     thinking budget for the answerer (exported as CLAUDE_EFFORT)
#   AFK_NOW                      override "now" (epoch seconds) — testing/cron
#   AFK_STATE                    state-file path (default: <git-common-dir>/.afk-state)
#   AFK_HEARTBEAT                heartbeat-file path (default: <git-common-dir>/.afk-heartbeat)
#   AFK_RULE_FILE                path to the afk-answering rule (auto-resolved otherwise)
#   WT_NEW / WT_LAND / SPOKE_READY / BATCH_PLAN   override the resolved sibling scripts
#   AFK_WT_LIB                   override the sourced worktree-lib.sh
#   AI_TOOLKIT_OTEL              telemetry opt-out: =0 disables the preflight (#108);
#                                unset/anything else ⇒ enabled (the SSOT-for-unattended default)
#   AFK_TELEMETRY_CONF           optional conf file for LANGFUSE_BASIC_AUTH / LANGFUSE_HOST
#                                when env leaves auth unset (env wins) [default: ~/.afk-telemetry]
#   AFK_PORT_WAIT_TRIES/SLEEP    collector/bridge re-probe attempts + interval after a launch
#   CLAUDE_PROJECTS_DIR          transcript root (default: $HOME/.claude/projects)
#   AFK_REMOTE_HOST / AFK_REMOTE_REPO / AFK_REMOTE_SESSION / AFK_REMOTE_DRAIN_CMD
#                                --remote target config (or a sourced AFK_REMOTE_CONF file)
#
# Usage:
#   hub-afk.sh <duration>        # e.g. 90, 30m, 1h, 1h30m — drain for that long, then stop
#   hub-afk.sh until <HH:MM>     # drain until the next HH:MM, then stop
#   hub-afk.sh drain             # drain until the backlog is empty + nothing in flight
#   hub-afk.sh --remote          # launch a detached `drain` on a configured always-on Mac
#   hub-afk.sh --status          # report the active window, "off", or "STALE" (crashed)
#   hub-afk.sh --off             # stop the supervisor + watchdog (clears the state file)
#   hub-afk.sh --once            # run a single tick and exit (tests / external cron)
#   hub-afk.sh --watchdog        # the keeper loop: respawn the supervisor if it crashes
#                                #   (auto-spawned on arm; rarely run by hand)
#
# Run it on the hub (main checkout, on the default branch). Read-only against the work
# except for dispatching, answering, landing, and reaping spokes. --remote runs the drain
# on a different machine instead (see docs/remote-afk.md).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${AFK_TICK_SECONDS:=120}"
: "${AFK_SPOKE_MAX_MINUTES:=180}"
: "${AFK_IDLE_MINUTES:=30}"
: "${AFK_ANSWERER_EFFORT:=high}"

# Raised to 1 when the answerer's own `claude` reports an auth failure (the
# subscription token could not refresh): a process-global the main loop reads to halt
# instead of spinning into dead auth. decide_and_act runs in the same shell as the
# loop, so the assignment propagates up.
_AFK_AUTH_FAILED=0

log() { printf '%s\n' "$*" >&2; }

# --- source worktree-lib.sh (the shared date/time + worktree helpers) ---------
# Resolution covers both layouts: the ai-toolkit checkout (scripts/worktree-lib.sh,
# four levels up from this hub script) and a synced target (co-located flat in
# .ai-toolkit/scripts/). AFK_WT_LIB wins for tests.
_AFK_TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null || true)"
for _cand in \
  "${AFK_WT_LIB:-}" \
  "$SCRIPT_DIR/worktree-lib.sh" \
  "$SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/scripts/worktree-lib.sh}" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/worktree-lib.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

# --- now-clock ----------------------------------------------------------------
# Current time, overridable via AFK_NOW for tests/cron.
afk_now() { printf '%s\n' "${AFK_NOW:-$(date +%s)}"; }

# --- window spec → end epoch (the pure time layer) ----------------------------

# parse_duration <spec> -> seconds, or empty (rc 1) when the spec is not a duration.
# Accepts a bare number (minutes), <N>h, <N>m, and <N>h<M>m. Zero / empty / malformed
# returns empty so compute_end_epoch can reject it rather than starting a 0-length window.
parse_duration() {
  local spec="$1" total=0 h="" m=""
  case "$spec" in
    *[!0-9hm]* | '') return 1 ;;                 # only digits, 'h', 'm'
    *h*m) h="${spec%%h*}"; m="${spec#*h}"; m="${m%m}" ;;
    *h)   h="${spec%h}"; m=0 ;;
    *m)   h=0; m="${spec%m}" ;;
    *)    h=0; m="$spec" ;;                       # bare number ⇒ minutes
  esac
  case "$h$m" in '' | *[!0-9]*) return 1 ;; esac   # guard empty fields (e.g. "hm")
  total=$(( h * 3600 + m * 60 ))
  [ "$total" -gt 0 ] || return 1
  printf '%s\n' "$total"
}

# compute_end_epoch <spec...> <now> -> the end epoch, or the literal `drain`, on stdout;
# rc 1 on an unrecognized spec. The spec is everything before the trailing now epoch:
#   drain                  -> `drain`            (no clock bound)
#   until <HH:MM>          -> next HH:MM at/after now
#   <duration>             -> now + parsed seconds
compute_end_epoch() {
  local now spec secs target
  now="${@: -1}"                                   # last arg is the now epoch
  set -- "${@:1:$#-1}"                             # everything before it is the spec
  spec="$*"
  case "$1" in
    drain) printf 'drain\n'; return 0 ;;
    until)
      [ -n "${2:-}" ] || return 1
      target="$(wt_epoch_at "$(wt_date_ymd "$now")" "$2")" || return 1
      [ -n "$target" ] || return 1
      [ "$target" -le "$now" ] && target=$(( target + 86400 ))
      printf '%s\n' "$target"; return 0 ;;
    *)
      secs="$(parse_duration "$spec")" || return 1
      printf '%s\n' "$(( now + secs ))"; return 0 ;;
  esac
}

# window_expired <state> <now> -> true (rc 0) when a clock-bound window has elapsed.
# `drain` and an empty state never expire by the clock.
window_expired() {
  local state="$1" now="$2"
  case "$state" in '' | drain | *[!0-9]*) return 1 ;; esac
  [ "$now" -ge "$state" ]
}

# minutes_remaining <state> <now> -> whole minutes left in a clock-bound window (>=0),
# or empty for drain / off (no clock bound). Used by --status.
minutes_remaining() {
  local state="$1" now="$2" rem
  case "$state" in '' | drain | *[!0-9]*) return 0 ;; esac
  rem=$(( (state - now) / 60 ))
  [ "$rem" -lt 0 ] && rem=0
  printf '%s\n' "$rem"
}

# --- state file ---------------------------------------------------------------
# The window bound persists out of the work tree (under the git common dir) so it
# survives a supervisor restart and a second shell can flip it off mid-run.

afk_state_file() {
  if [ -n "${AFK_STATE:-}" ]; then printf '%s\n' "$AFK_STATE"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/.afk-state"
}

afk_write_state() { printf '%s\n' "$1" > "$(afk_state_file)"; }
afk_read_state()  { local f; f="$(afk_state_file)"; [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' || true; }
afk_clear_state() { rm -f "$(afk_state_file)" 2>/dev/null || true; afk_clear_heartbeat; _afk_clear_unattended; }

# --- heartbeat (issue #107) ---------------------------------------------------
# Each supervisor tick stamps "<pid> <last_tick_epoch>" here so a second shell (and the
# watchdog) can tell a LIVE supervisor from a stale state file. A silent crash (the
# supervisor exited 0 mid-tick) leaves .afk-state armed with no process behind it, and
# without this --status would echo a `draining` run that is gone (#107). The pid is THIS
# supervisor's; cross-checking its liveness (kill -0) is the truth .afk-state cannot give.
afk_heartbeat_file() {
  if [ -n "${AFK_HEARTBEAT:-}" ]; then printf '%s\n' "$AFK_HEARTBEAT"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/.afk-heartbeat"
}
afk_write_heartbeat() { printf '%s %s\n' "$$" "$(afk_now)" > "$(afk_heartbeat_file)" 2>/dev/null || true; }
afk_read_heartbeat()  { local f; f="$(afk_heartbeat_file)"; [ -f "$f" ] && head -n1 "$f" 2>/dev/null || true; }
afk_clear_heartbeat() { rm -f "$(afk_heartbeat_file)" 2>/dev/null || true; }

# _afk_pid_alive <pid> -> true when <pid> is a live process. An empty / non-numeric pid is
# never alive (guards `kill` against a bareword and a truncated partial heartbeat).
_afk_pid_alive() {
  case "${1:-}" in '' | *[!0-9]*) return 1 ;; esac
  kill -0 "$1" 2>/dev/null
}

# afk_supervisor_state -> off | live | stale: the GROUND TRUTH of whether a supervisor is
# actually running, cross-checking .afk-state against the heartbeat pid (#107):
#   off   — no window armed (.afk-state empty).
#   live  — a window is armed AND the heartbeat pid is a live process.
#   stale — a window is armed but the heartbeat pid is gone, or there is no heartbeat —
#           the supervisor crashed and the state file is lying.
# UPGRADE: also require a recent tick (afk_now - heartbeat epoch < a few tick intervals)
# if pid reuse — the OS recycling a crashed supervisor's pid onto an unrelated process —
# ever produces a false `live`. kill -0 alone is the issue's spec and reuse is rare here.
afk_supervisor_state() {
  [ -n "$(afk_read_state)" ] || { printf 'off\n'; return; }
  local hb pid; hb="$(afk_read_heartbeat)"; pid="${hb%% *}"
  if _afk_pid_alive "$pid"; then printf 'live\n'; else printf 'stale\n'; fi
}

# _afk_heartbeat_age_minutes -> whole minutes since the last tick stamp, or empty when
# there is no heartbeat. Used by --status to report how long ago the supervisor ticked.
_afk_heartbeat_age_minutes() {
  local hb tick; hb="$(afk_read_heartbeat)"; [ -n "$hb" ] || return 0
  tick="${hb##* }"
  case "$tick" in '' | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "$(( ($(afk_now) - tick) / 60 ))"
}

# --- unattended marker --------------------------------------------------------
# While a window is armed the supervisor drops a marker under the git common dir (shared
# with every spoke worktree). anti-gutting-scan.sh reads it to fail CLOSED on a
# test-gutting diff for /afk-dispatched spokes — no human is watching to catch it (#74).
_afk_unattended_marker() { printf '%s\n' "$(_afk_state_dir)/unattended"; }
_afk_set_unattended() {
  local m; m="$(_afk_unattended_marker)"
  mkdir -p "$(dirname "$m")" 2>/dev/null || true
  : > "$m" 2>/dev/null || true
}
_afk_clear_unattended() { rm -f "$(_afk_unattended_marker)" 2>/dev/null || true; }

# --- per-spoke dispatch epochs (the wall-clock reap reference) ----------------
# Also the record of WHICH issues THIS run dispatched: a dispatch epoch exists only for
# a spoke this run spawned, so auto_land lands only those (not a foreign ready/<issue>
# from a parallel session). AFK_STATE_DIR overrides the location for tests.
_afk_state_dir() {
  if [ -n "${AFK_STATE_DIR:-}" ]; then printf '%s\n' "$AFK_STATE_DIR"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/ai-toolkit-afk"
}
stamp_dispatch_epoch() {
  local dir; dir="$(_afk_state_dir)"
  mkdir -p "$dir" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$dir/dispatch-$1.epoch" 2>/dev/null || true
}
read_dispatch_epoch() {
  local f; f="$(_afk_state_dir)/dispatch-$1.epoch"
  [ -f "$f" ] && cat "$f" 2>/dev/null || true
}
# _clear_dispatch_epochs -> drop every dispatch epoch so the "dispatched by this run"
# set starts empty for a freshly-armed window. Without this a stale epoch from a prior
# window could make a foreign ready/<issue> look like one we dispatched.
_clear_dispatch_epochs() {
  local dir; dir="$(_afk_state_dir)"
  rm -f "$dir"/dispatch-*.epoch 2>/dev/null || true
}

# --- sibling-script resolution ------------------------------------------------
# Find a workflow script across the checkout + synced layouts; the first existing
# candidate wins. An explicit override (passed as $1) short-circuits.
_afk_find_script() {
  local override="$1" name="$2" cand
  for cand in \
    "$override" \
    "$SCRIPT_DIR/$name" \
    "$SCRIPT_DIR/../../../../scripts/$name" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/scripts/$name" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/.ai-toolkit/scripts/$name" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/.ai-toolkit/scripts/hub/$name"; do
    [ -n "$cand" ] && [ -f "$cand" ] && { printf '%s\n' "$cand"; return 0; }
  done
  return 1
}

# --- in-flight survey ---------------------------------------------------------
# "<path>\t<issue>" per task worktree whose branch slug leads with an issue number.
# Built on worktree-lib's wt_task_worktrees so the hub and these helpers agree on
# what counts as a task worktree.
inflight_worktrees() {
  local main path br slug num
  main="$(wt_main_root 2>/dev/null)" || return 0
  while IFS=$'\t' read -r path br; do
    [ -n "$path" ] || continue
    slug="${br##*/}"
    num="$(printf '%s' "$slug" | sed 's/^\([0-9]*\).*/\1/')"
    [ -n "$num" ] && printf '%s\t%s\n' "$path" "$num"
  done < <(wt_task_worktrees "$main")
}
inflight_issues() { inflight_worktrees | cut -f2; }

# --- transcript helpers (newest .jsonl in the spoke's Claude project dir) -----
_spoke_project_dir() {
  local wt_path="$1" projects_root slug
  projects_root="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
  slug="$(printf '%s' "$wt_path" | sed 's/[^A-Za-z0-9]/-/g')"
  printf '%s\n' "$projects_root/$slug"
}
_spoke_jsonl() {
  local dir; dir="$(_spoke_project_dir "$1")"
  [ -d "$dir" ] || return 0
  ls -t "$dir"/*.jsonl 2>/dev/null | head -1
}
_transcript_idle_seconds() {
  local jsonl mtime; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  mtime="$(stat -f %m "$jsonl" 2>/dev/null || stat -c %Y "$jsonl" 2>/dev/null)"
  [ -n "$mtime" ] || return 0
  printf '%s\n' "$(( $(afk_now) - mtime ))"
}
# _transcript_mtime <wt_path> -> epoch mtime of the spoke's newest transcript, or empty.
# The registration signal for inject verification: it bumps when the spoke writes its
# next turn after an injected answer is submitted.
_transcript_mtime() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  stat -f %m "$jsonl" 2>/dev/null || stat -c %Y "$jsonl" 2>/dev/null
}

# extract_pending_question <wt_path> -> the prompt the spoke is parked on, or empty when
# it is NOT waiting. The same waiting signal hub-status.sh surfaces (an open
# AskUserQuestion, or a trailing notification entry) — but here we return the actual
# question + options / trailing assistant message so the answerer has something to reason
# about. Empty output ⇒ not waiting, so this doubles as the auto-answer trigger.
extract_pending_question() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  _AFK_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json, os

pending = None        # list of formatted AskUserQuestion questions, or None
last_asst_text = ""   # text of the most recent assistant message
gate_plan = ""        # plan prose of a PLAN-gate park (spoke-ready.sh --gate), or ""
last_type = None
try:
    with open(os.environ["_AFK_JSONL"]) as fh:
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
                    pending = None
                continue
            if last_type == "assistant":
                asks, texts, gate_here = [], [], False
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        texts.append(block.get("text") or "")
                    elif block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                        for q in (block.get("input") or {}).get("questions") or []:
                            lines = [f"Q: {q.get('question', '').strip()}"]
                            for opt in q.get("options") or []:
                                label = (opt.get("label") or "").strip()
                                desc = (opt.get("description") or "").strip()
                                lines.append(f"  - {label}: {desc}" if desc else f"  - {label}")
                            asks.append("\n".join(lines))
                    elif block.get("type") == "tool_use" and block.get("name") == "Bash":
                        if "spoke-ready.sh --gate" in ((block.get("input") or {}).get("command") or ""):
                            gate_here = True
                if texts:
                    last_asst_text = "\n".join(t for t in texts if t).strip()
                pending = asks or None
                # A PLAN-gate park = prose plan + a `spoke-ready.sh --gate` Bash, no
                # AskUserQuestion. Remember the plan so the answerer has it to reason about.
                if gate_here:
                    gate_plan = last_asst_text
            elif last_type == "user":
                # A real human reply (a text block) means the spoke is no longer parked;
                # a tool_result-only user turn (e.g. the gate Bash's result) does NOT.
                if any(isinstance(b, dict) and b.get("type") == "text" for b in content):
                    pending = None
                    gate_plan = ""
except Exception:
    pass

out = ""
if pending:
    out = "\n\n".join(pending)
elif last_type == "notification":
    out = last_asst_text
elif gate_plan:
    out = gate_plan
# Bound the payload so a huge plan message can't blow up the answerer prompt.
print(out[:4000].strip())
PYEOF
}

# --- slot state ---------------------------------------------------------------
# slot_state <wt_path> <issue> -> done|waiting|reap|busy.
#   done    — a TERMINAL marker (ready/accept/blocked) at the branch tip.
#   waiting — parked on a question / gate (auto-answer it; never reaped).
#   reap    — over the wall-clock ceiling, or idle past AFK_IDLE_MINUTES with no marker.
#   busy    — actively working (or just spawned, no transcript yet).
slot_state() {
  local wt_path="$1" issue="$2" tip marker kind epoch age
  tip="$(git -C "$wt_path" rev-parse HEAD 2>/dev/null)"
  if [ -n "$tip" ]; then
    for kind in ready accept blocked; do
      marker="$(git -C "$wt_path" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)"
      [ "$marker" = "$tip" ] && { printf 'done\n'; return; }
    done
    # A pushed gate/<issue> at the tip = parked at the PLAN gate → waiting, never reaped.
    # The gate is a prose plan + this tag (no AskUserQuestion), so extract_pending_question
    # can't see it. Checking at the tip is self-clearing: once approved and the spoke
    # commits its first RED/GREEN, the tip moves past the gate commit and it reads busy.
    if [ "$(git -C "$wt_path" rev-parse -q --verify "refs/tags/gate/${issue}^{commit}" 2>/dev/null)" = "$tip" ]; then
      printf 'waiting\n'; return
    fi
  fi
  epoch="$(read_dispatch_epoch "$issue")"
  if spoke_over_ceiling "$epoch" "$(afk_now)"; then printf 'reap\n'; return; fi
  if [ -n "$(extract_pending_question "$wt_path")" ]; then printf 'waiting\n'; return; fi
  age="$(_transcript_idle_seconds "$wt_path")"
  if [ -n "$age" ] && [ "$age" -gt $(( AFK_IDLE_MINUTES * 60 )) ]; then printf 'reap\n'; return; fi
  printf 'busy\n'
}

# spoke_over_ceiling <dispatch_epoch> <now> -> true when a spoke has run longer than
# AFK_SPOKE_MAX_MINUTES. An empty/non-numeric epoch or clock reads as "not over" (can't
# measure → never reap), guarding `set -u` arithmetic against a bareword.
spoke_over_ceiling() {
  local epoch="$1" now="$2"
  case "$epoch" in '' | *[!0-9]*) return 1 ;; esac
  case "$now" in '' | *[!0-9]*) return 1 ;; esac
  [ "$(( (now - epoch) / 60 ))" -gt "$AFK_SPOKE_MAX_MINUTES" ]
}

# --- the answerer (the one reasoning step) ------------------------------------

# _rule_file -> the afk-answering rule path, across both layouts; empty if unfound.
_rule_file() {
  local cand
  for cand in \
    "${AFK_RULE_FILE:-}" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/.claude/rules/afk-answering.md" \
    "$SCRIPT_DIR/../../../../shared/rules/afk-answering.md" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/shared/rules/afk-answering.md"; do
    [ -n "$cand" ] && [ -f "$cand" ] && { printf '%s\n' "$cand"; return 0; }
  done
  return 1
}

# build_answerer_prompt <issue> <question> -> the full prompt for the answerer: the
# governing rule, the issue contract, and the parked prompt. Self-contained so the
# headless answerer needs no project context loaded.
build_answerer_prompt() {
  local issue="$1" question="$2" rule body
  rule="$(_rule_file)" && rule="$(cat "$rule")" \
    || rule="Answer in the interest of the issue contract and repo conventions; prefer the spoke's own recommended option; escalate (output 'ESCALATE: <reason>') only when the decision is irreversible, outward-facing, or scope-changing. Otherwise output 'ANSWER: <reply>'."
  body="$(gh issue view "$issue" --json title,body -q '.title + "\n\n" + .body' 2>/dev/null || echo "(issue #$issue body unavailable)")"
  cat <<EOF
$rule

## Issue contract (#$issue)

$body

## The spoke's parked prompt

$question

Decide per the policy above. End your reply with exactly one line: 'ANSWER: <reply>' or 'ESCALATE: <reason>'.
EOF
}

# run_answerer <issue> <question> -> the answerer's raw output (stdout AND stderr), and
# its exit status as the function's return code. The answerer is a headless `claude -p`
# (overridable via AFK_ANSWERER_CMD for tests), run with a thinking budget; the prompt is
# passed on stdin so a long contract never hits argv limits. stderr is folded into the
# captured stream (NOT discarded) because the CLI prints credential failures there and
# exits nonzero — the auth-failure detector needs both the message and the exit code.
# parse_decision is line-anchored, so interleaved stderr noise never pollutes a decision.
run_answerer() {
  local prompt; prompt="$(build_answerer_prompt "$1" "$2")"
  local cmd="${AFK_ANSWERER_CMD:-claude -p --model claude-fable-5}"
  CLAUDE_EFFORT="$AFK_ANSWERER_EFFORT" bash -c "$cmd" <<<"$prompt" 2>&1
}

# parse_decision <raw-answerer-output> -> "ANSWER\t<text>" or "ESCALATE\t<reason>" on
# stdout, or empty when the answerer emitted no decision line. The LAST matching line
# wins (the answerer reasons first, then concludes). Decisions are SINGLE-LINE by
# construction (the grep is line-anchored) — inject_answer and _afk_continue_command
# rely on this; supporting multi-line answers would re-trigger the bracketed-paste
# hazard (#123/#124) and the quoting hazard on the respawn command line.
parse_decision() {
  local line kind rest
  line="$(printf '%s\n' "$1" | grep -E '^(ANSWER|ESCALATE):' | tail -1)"
  [ -n "$line" ] || return 0
  kind="${line%%:*}"
  rest="${line#*:}"
  rest="${rest#"${rest%%[![:space:]]*}"}"          # ltrim
  printf '%s\t%s\n' "$kind" "$rest"
}

# is_auth_failure <raw-answerer-output> -> true (rc 0) when the text carries a Claude /
# Anthropic auth-failure signature (dead credentials / token could not refresh). Matched
# case-insensitively against the known wordings. The CALLER additionally gates on the
# answerer having EXITED NONZERO (decide_and_act) — auth discussion in a healthy answer
# exits 0 and is never treated as a failure — so this predicate can favor recall without
# a false positive halting the whole drain. The /login signature is still anchored to the
# CLI's "run [`claude `]/login" phrasing so prose like "run the /login migration" misses.
is_auth_failure() {
  printf '%s' "$1" | grep -Eqi \
    'authentication_error|invalid (x-)?api[ -]?key|invalid bearer token|oauth (token|authentication)|run `?(claude )?/login|401|unauthorized|credit balance is too low'
}

# --- tmux injection + telemetry -----------------------------------------------

# _spoke_pane_target <wt_path> -> "session:window" of the spoke's pane, or empty.
# Paths are canonicalized on both sides (wt_realpath): a worktree under a symlinked
# root (/tmp → /private/tmp on macOS) would otherwise miss its pane, drop a valid
# answer, and silently escalate.
_spoke_pane_target() {
  local wt_path="$1" target path want
  command -v tmux >/dev/null 2>&1 || return 0
  want="$(wt_realpath "$wt_path")"; want="${want:-$wt_path}"
  while IFS=$'\t' read -r target path; do
    [ "$(wt_realpath "$path")" = "$want" ] && { printf '%s\n' "$target"; return 0; }
  done < <(tmux list-panes -a -F '#{session_name}:#{window_index}'$'\t''#{pane_current_path}' 2>/dev/null)
  return 0
}

# inject_answer <pane_target> <text> -> type the answer into the spoke and submit it.
# A PLAN gate renders as an interactive AskUserQuestion MENU (tab/arrow/enter) that
# IGNORES typed free text, so the most common gate is never answered by a bare inject
# (issue #74). We send Esc FIRST: it cancels the menu, surfaces the questions as text,
# and opens a free-text prompt — and is a no-op (nothing typed yet to clear) when the
# spoke is already at a plain text prompt. A short, tunable pause lets that prompt
# re-render before we type. Then `send-keys -l` sends the text literally (no key-name
# interpretation) and a separate Enter submits — the gotcha-proof re-drive pattern.
inject_answer() {
  local target="$1" text="$2"
  command -v tmux >/dev/null 2>&1 || return 1
  [ -n "$target" ] || return 1
  tmux send-keys -t "$target" Escape 2>/dev/null || return 1
  sleep "${AFK_INJECT_MENU_PAUSE:-0.3}" 2>/dev/null || true
  tmux send-keys -t "$target" -l -- "$text" 2>/dev/null || return 1
  tmux send-keys -t "$target" Enter 2>/dev/null || return 1
}

# _composer_shows_text <pane_target> <text> -> true when the pane still displays the
# answer — its needle: the first ~40 chars of the first line — i.e. the paste is
# buffered in the composer, not submitted (#133). Fail-OPEN: an unreadable pane
# (capture error, no tmux) reads as "not shown", so the caller escalates instead of
# wedge-respawning a pane it cannot observe.
_composer_shows_text() {
  local target="$1" text="$2" needle
  needle="${text%%$'\n'*}"
  needle="${needle:0:40}"
  [ -n "$needle" ] || return 1
  tmux capture-pane -p -t "$target" 2>/dev/null | grep -qF -- "$needle"
}

# _transcript_advanced <wt_path> <baseline_mtime> -> true once the spoke's newest
# transcript mtime exceeds the baseline, polling up to AFK_INJECT_VERIFY_SECONDS in
# AFK_INJECT_POLL_SECONDS steps. An empty baseline (no prior transcript) means any
# transcript now is progress. Used to confirm an injected answer actually registered.
_transcript_advanced() {
  local wt="$1" before="$2" budget poll waited=0 now
  budget="${AFK_INJECT_VERIFY_SECONDS:-20}"
  poll="${AFK_INJECT_POLL_SECONDS:-2}"
  while : ; do
    now="$(_transcript_mtime "$wt")"
    if [ -n "$now" ] && { [ -z "$before" ] || [ "$now" -gt "$before" ]; }; then return 0; fi
    [ "$waited" -ge "$budget" ] && return 1
    sleep "$poll" 2>/dev/null || true
    waited=$(( waited + poll ))
  done
}

# inject_and_verify <wt_path> <pane_target> <text> -> deliver the answer and CONFIRM
# it registered (the spoke's transcript advanced). The retry is a bare Enter, NEVER a
# re-paste: the common failure is a buffered paste whose submitting Enter was lost, and
# the old full re-inject duplicated the answer on top of it (#133, from #123/#124).
#   rc 0 — registered (the transcript advanced; the answer took).
#   rc 2 — WEDGED: the text survived the Enter-only retry (an unterminated paste no
#          keystroke can submit or clear) — the caller respawns the pane.
#   rc 1 — not registered and no text observable in the composer — the caller escalates.
inject_and_verify() {
  local wt="$1" target="$2" text="$3" before baseline_shows=0
  before="$(_transcript_mtime "$wt")"
  # Baseline BEFORE pasting: a short answer often also appears in the rendered
  # question above the composer. If the needle was already visible pre-inject,
  # post-retry presence proves nothing — never classify wedged off a pre-existing
  # match (a false wedge would kill a live pane where rc 1 safely escalates).
  _composer_shows_text "$target" "$text" && baseline_shows=1
  inject_answer "$target" "$text" || return 1
  _transcript_advanced "$wt" "$before" && return 0
  log "  injected answer did not register — retrying with a bare Enter (never a re-paste)"
  tmux send-keys -t "$target" Enter 2>/dev/null || true
  _transcript_advanced "$wt" "$before" && return 0
  [ "$baseline_shows" -eq 0 ] && _composer_shows_text "$target" "$text" && return 2
  return 1
}

# afk_emit_decision <wt_path> <status> -> one kind=agent span per auto-answer decision,
# attributed to the SPOKE (emit with the worktree as CWD, like worktree-lib does), so the
# decision surfaces on the observability dashboard. Metadata only — the question→answer
# text rides the answerer's own sidecar session (the dashboard's node summary), never the
# span (the telemetry privacy contract logs no payload). No-op when telemetry is off.
# _afk_emit_span <wt> <name> <status> -> the shared one-span emitter (kind=agent, phase
# review), attributed to the spoke. No-op when telemetry is off or the worktree is gone.
_afk_emit_span() {
  command -v telemetry_emit_span >/dev/null 2>&1 || return 0
  local wt="$1" name="$2" status="$3"
  [ -d "$wt" ] || return 0
  ( cd "$wt" && telemetry_emit_span --kind agent --name "$name" --phase review --status "$status" ) || true
  return 0
}
afk_emit_decision() { _afk_emit_span "$1" afk-answer "$2"; }

# _consume_gate_tag <wt_path> <issue> -> drop the gate/<issue> marker once a PLAN-gate
# answer has been injected. slot_state reads the LOCAL tag at the tip, so deleting the local
# tag is what closes the window between "answered" and the spoke committing its first code
# (the tip still equals the gate commit until then, and an untouched tag would re-read as
# waiting and re-answer the same gate). The remote delete is cosmetic (dashboard /
# hub-status) and best-effort. Never aborts the loop.
_consume_gate_tag() {
  local wt="$1" issue="$2"
  git -C "$wt" tag -d "gate/$issue" >/dev/null 2>&1 || true
  git -C "$wt" push origin ":refs/tags/gate/$issue" >/dev/null 2>&1 || true
}

# decide_and_act <wt_path> <issue> -> reason about a parked spoke and act: inject the
# answer, or escalate to blocked/<issue>. Fail-safe: an answerer that returns no decision
# (or an answer we cannot inject) escalates rather than guessing.
decide_and_act() {
  local wt="$1" issue="$2" question raw rc decision kind text target
  question="$(extract_pending_question "$wt")"
  [ -n "$question" ] || return 0
  log "→ answering #$issue (parked on input)"
  raw="$(run_answerer "$issue" "$question")"; rc=$?
  # The answerer is the supervisor's own `claude`; if its credentials are dead, every
  # other `claude` (the spokes, the next tick's answerer) is dead too. We treat it as an
  # auth failure only when the answerer EXITED NONZERO and its output carries an auth
  # signature — a healthy answer that merely discusses auth exits 0 and is unaffected.
  # Raise the global stop flag and block THIS spoke so the failure surfaces as
  # blocked/<issue> on the dashboard rather than spinning the loop; the main loop halts.
  if [ "$rc" -ne 0 ] && is_auth_failure "$raw"; then
    _AFK_AUTH_FAILED=1
    _escalate_blocked "$wt" "$issue" \
      "subscription auth failed — token could not refresh; re-run /login on the host"
    return 0
  fi
  decision="$(parse_decision "$raw")"
  kind="${decision%%$'\t'*}"
  text="${decision#*$'\t'}"
  if [ "$kind" = "ANSWER" ] && [ -n "$text" ]; then
    target="$(_spoke_pane_target "$wt")"
    if [ -z "$target" ]; then
      text="could not locate spoke pane to inject the answer"
    else
      inject_and_verify "$wt" "$target" "$text"; rc=$?
      if [ "$rc" -eq 0 ]; then
        log "  injected answer into #$issue"
        _consume_gate_tag "$wt" "$issue"
        afk_emit_decision "$wt" success
        return 0
      elif [ "$rc" -eq 2 ] && respawn_wedged_spoke "$wt" "$issue" "$text"; then
        # The wedged composer was recovered by a pane respawn that carries the answer
        # as its --continue prompt — delivered, same success contract as an inject.
        _consume_gate_tag "$wt" "$issue"
        afk_emit_decision "$wt" success
        return 0
      elif [ "$rc" -eq 2 ]; then
        # The old window is dead and the answer text lives nowhere else — carry its
        # head in the blocked reason so the returning human need not re-derive it.
        text="composer wedged and the pane respawn could not be confirmed — needs a human; the undelivered answer began: $(printf '%.120s' "${text%%$'\n'*}")"
      else
        log "  answer to #$issue did not register — escalating"
        text="answer did not register in the spoke (inject not confirmed) — needs a human"
      fi
    fi
  elif [ "$kind" = "ESCALATE" ]; then
    [ -n "$text" ] || text="answerer escalated (no reason given)"
  else
    text="answerer returned no decision — escalating for human review"
  fi
  _escalate_blocked "$wt" "$issue" "$text"
}

# --- durable local block record (issue #109, AC2) -----------------------------
# spoke-ready.sh emits blocked/<issue> by `git tag` + `git push -f origin blocked/<issue>`;
# that push can fail for any reason (no/unreachable remote, a transient network drop, a
# push-hook error) — and in the #103 incident the reap logged `could not emit blocked/103`
# and dropped it. When the tag can't be pushed after retries, a blocked state is recorded
# LOCALLY instead, so it is NEVER silently dropped: --status surfaces this record for the
# operator returning from AFK. Cleared on a fresh arm (a current-window view).
_afk_blocked_record() { printf '%s\n' "$(_afk_state_dir)/blocked-$1.txt"; }
_afk_record_blocked_locally() {
  local issue="$1" reason="$2" f
  f="$(_afk_blocked_record "$issue")"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\n' "$(afk_now)" "$reason" > "$f" 2>/dev/null \
    || log "  WARNING: could not write a durable block record for #$issue at $f"
}
_clear_blocked_records() { rm -f "$(_afk_state_dir)"/blocked-*.txt 2>/dev/null || true; }

# _escalate_blocked <wt_path> <issue> <reason> -> emit blocked/<issue> on the spoke's
# behalf via spoke-ready.sh, RETRYING the push, and falling back to a durable local record
# when it still can't be emitted — escalation never fails silently (#109). Always emits a
# deny decision span. Best-effort; never aborts the loop.
_escalate_blocked() {
  local wt="$1" issue="$2" reason="$3" sr tries i=0 ok=0
  log "  escalate #$issue: $reason"
  tries="${AFK_ESCALATE_TRIES:-3}"
  case "$tries" in '' | *[!0-9]*) tries=3 ;; esac   # guard the loop arithmetic
  sr="$(_afk_find_script "${SPOKE_READY:-}" spoke-ready.sh)" || sr=""
  if [ -n "$sr" ]; then
    while [ "$i" -lt "$tries" ]; do
      if ( cd "$wt" && "$sr" --blocked "$issue" -m "$reason" ) >/dev/null 2>&1; then ok=1; break; fi
      i=$(( i + 1 ))
      [ "$i" -lt "$tries" ] && sleep "${AFK_ESCALATE_SLEEP:-1}" 2>/dev/null || true
    done
  fi
  if [ "$ok" -ne 1 ]; then
    log "  could not push blocked/$issue after $tries tries — recording it durably (see --status)"
    _afk_record_blocked_locally "$issue" "$reason"
  fi
  afk_emit_decision "$wt" deny
}

# --- reaping ------------------------------------------------------------------
_kill_spoke_window() {
  local issue="$1" target name
  command -v tmux >/dev/null 2>&1 || return 0
  tmux list-windows -a -F '#{session_name}:#{window_index} #{window_name}' 2>/dev/null \
  | while read -r target name; do
      case "$name" in "${issue}-"* | "$issue") tmux kill-window -t "$target" 2>/dev/null || true ;; esac
    done
}
reap_spoke() {
  local wt="$1" issue="$2" reason="$3"
  log "→ reap #$issue: $reason"
  _kill_spoke_window "$issue"
  _escalate_blocked "$wt" "$issue" "$reason"
}

# --- crash ≠ hang: auto-resume-once a pane-dead spoke (issue #109) -------------
# A reaped spoke is not always hung. The reaper abandoned #103 as "idle, likely hung"
# when its tmux PANE had crashed but its committed work was intact. So before declaring
# blocked we distinguish a DEAD pane (session crashed → re-adopt the worktree ONCE,
# reusing the spoke_run_id) from a LIVE-but-idle pane (truly hung → block).

# _spoke_pane_alive <wt> -> true when a live tmux pane maps to the worktree. Empty target
# (the spoke's pane crashed / its window is gone) ⇒ dead.
_spoke_pane_alive() { [ -n "$(_spoke_pane_target "$1")" ]; }

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
  if ! _afk_open_spoke_window "$wt" "$issue" "$(_afk_resume_command "$wt" "$issue")"; then
    log "  could not open a resume window for #$issue"
    return 1
  fi
  _afk_mark_resumed "$issue"
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
  _afk_emit_span "$wt" afk-wedge-respawn success
  return 0
}

# _reap_or_resume <wt> <issue> -> decide a reaped spoke's fate. An over-ceiling runaway
# always blocks (resume never applies). Otherwise it went idle: crash ≠ hang — a LIVE pane
# is truly hung (block); a DEAD pane with commits is auto-resumed ONCE in place; a dead
# pane with nothing to preserve, or one already resumed this window, is blocked.
_reap_or_resume() {
  local wt="$1" issue="$2"
  if spoke_over_ceiling "$(read_dispatch_epoch "$issue")" "$(afk_now)"; then
    reap_spoke "$wt" "$issue" "time ceiling: ran >${AFK_SPOKE_MAX_MINUTES}m without finishing"
  elif _spoke_pane_alive "$wt"; then
    reap_spoke "$wt" "$issue" "went idle >${AFK_IDLE_MINUTES}m with a live pane and no marker — likely hung"
  elif ! _spoke_has_commits "$wt"; then
    reap_spoke "$wt" "$issue" "pane crashed with no committed work to preserve — needs a human"
  elif _afk_already_resumed "$issue"; then
    reap_spoke "$wt" "$issue" "pane crashed again after an auto-resume — needs a human"
  else
    resume_spoke "$wt" "$issue" \
      || reap_spoke "$wt" "$issue" "pane crashed and the auto-resume could not be launched — needs a human"
  fi
}

# _block_all_inflight <reason> -> emit blocked/<issue> for every in-flight spoke not
# already at a terminal marker. Called on an auth-failure stop so the dashboard shows
# every affected spoke as blocked (no orphaned window left silently stuck on dead auth)
# rather than just the one whose answerer surfaced the failure.
_block_all_inflight() {
  local reason="$1" path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "done" ] && continue
    _escalate_blocked "$path" "$issue" "$reason"
  done < <(inflight_worktrees)
}

# --- dispatch -----------------------------------------------------------------

# kickoff_for <issue> -> the spoke's first prompt: the standard ultra kickoff (the same
# handoff start-task / next-batch use). Under /afk the spoke runs in its normal attended
# posture — it pauses at its PLAN gate and asks questions as if a human were watching —
# and the supervisor's answerer plays the human. So the kickoff is deliberately the
# everyday one, NOT a "park, never ask" variant.
kickoff_for() {
  local n="$1"
  cat <<EOF
You're in a dedicated worktree for issue #$n. Run /source to anchor to issue #$n and read
it. Before touching code, break the issue body into a task ledger (TaskCreate, or
TodoWrite on older runtimes) — one todo per subtask × the solo-cycle steps that apply
(ANCHOR/RED/GREEN/REVIEW/PUSH), exactly one in_progress.

Honor the issue's Gate: line. If it is \`plan\` (the default for non-trivial work): the
PLAN gate comes first — explore the code, then print the full implementation plan (files,
approach, test strategy, open questions) as a normal visible message, emit the gate marker
(bash .ai-toolkit/scripts/spoke-ready.sh --gate $n) so the hub sees you parked, and WAIT
for approval before writing code (before GREEN). If the gate is \`none\`, run autonomous
straight through.

Then implement following the solo-cycle (/cycle: RED → GREEN → REVIEW → PUSH). Push your
own branch on every subtask without asking; when your ledger shows the issue's acceptance
criteria are all met, push the final subtask and emit the ready marker (bash
.ai-toolkit/scripts/spoke-push.sh --ready $n) — also without asking. Still ask before
genuinely dangerous or irreversible ops (force-push, history rewrites, anything touching
\`main\`, deletions outside the worktree). Do NOT self-land — the hub lands #$n.
EOF
}

# _inflight_scope_args -> repeated `--inflight "<scope>"` flags, one per live spoke, so
# batch-plan holds back a ready issue that collides with work already running. The Scope:
# line is read from each in-flight issue's body (the same source batch-plan reads). When a
# live spoke's scope CANNOT be resolved (gh failed, or the issue has no Scope: line) its
# footprint is unknown, so we emit `--inflight *` (exclusive) and batch-plan holds back
# EVERY ready issue until it lands — failing CLOSED under unattended /afk (#74) rather than
# co-dispatching into an unknown-scope collision.
_inflight_scope_args() {
  local issue body scope
  while IFS= read -r issue; do
    [ -n "$issue" ] || continue
    body="$(gh issue view "$issue" --json body -q .body 2>/dev/null || true)"
    scope="$(printf '%s\n' "$body" | sed -n 's/^[[:space:]]*[Ss]cope:[[:space:]]*//p' | head -1)"
    printf -- '--inflight\n%s\n' "${scope:-*}"
  done < <(inflight_issues)
}

# dispatch_batch -> plan the next concurrent batch (batch-plan.sh) and spawn a spoke for
# each issue not already in flight, seeded with the ultra kickoff. A missing planner or
# dispatcher logs and is a no-op (the next tick retries).
dispatch_batch() {
  [ "$_AFK_AUTH_FAILED" -eq 1 ] && return 0   # auth is dead — don't spawn spokes into it
  local bp wt_new inflight args=() batch n
  bp="$(_afk_find_script "${BATCH_PLAN:-}" batch-plan.sh)" || { log "batch-plan.sh not found — skipping dispatch"; return 0; }
  wt_new="$(_afk_find_script "${WT_NEW:-}" worktree-new.sh)" || { log "worktree-new.sh not found — skipping dispatch"; return 0; }
  inflight="$(inflight_issues)"
  while IFS= read -r line; do args+=("$line"); done < <(_inflight_scope_args)
  batch="$(bash "$bp" "${args[@]+"${args[@]}"}" 2>/dev/null || true)"
  for n in $batch; do
    printf '%s\n' "$inflight" | grep -qxF "$n" && continue   # already in flight (idempotent)
    log "→ dispatch #$n"
    # --mode afk stamps the spoke's trace as drain-driven (#102); a hand-dispatched
    # spoke defaults to attended in worktree-new.sh.
    if bash "$wt_new" "$n" --type feature --mode afk --prompt "$(kickoff_for "$n")"; then
      stamp_dispatch_epoch "$n"
    else
      log "  dispatch of #$n failed — will retry next tick"
    fi
  done
}

# --- auto-land + reap passes --------------------------------------------------

# _ready_at_tip <wt_path> <issue> -> true when ready/<issue> points at the branch tip.
# Only a ready/ marker is auto-landed: accept/ awaits a human sign-off and blocked/ is
# already a parked terminal state.
_ready_at_tip() {
  local wt="$1" issue="$2" tip marker
  tip="$(git -C "$wt" rev-parse HEAD 2>/dev/null)" || return 1
  marker="$(git -C "$wt" rev-parse -q --verify "refs/tags/ready/${issue}^{commit}" 2>/dev/null)"
  [ -n "$marker" ] && [ "$marker" = "$tip" ]
}

# auto_land -> land every ready/<issue> spoke. The ready/<issue> marker is the readiness
# contract (enforced by _ready_at_tip above), so a foreign ready/<issue> left by a parallel
# session is adopted and landed by default (#95). A failed land (merge conflict / suite
# fail) emits blocked/<issue> and the drain continues; a landed spoke frees its scope + its
# dependents' blockers for the next tick's plan. Set AFK_LAND_FOREIGN=0 to restore the
# dispatched-only isolation (skip any ready/<issue> with no dispatch epoch) so concurrent
# sessions don't surprise-land each other's work (#74).
auto_land() {
  local wt_land path issue
  wt_land="$(_afk_find_script "${WT_LAND:-}" worktree-land.sh)" || { log "worktree-land.sh not found — skipping land"; return 0; }
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    _ready_at_tip "$path" "$issue" || continue
    if [ "${AFK_LAND_FOREIGN:-1}" = "0" ] && [ -z "$(read_dispatch_epoch "$issue")" ]; then
      log "  skip land #$issue — foreign (no dispatch epoch) and AFK_LAND_FOREIGN=0"
      continue
    fi
    log "→ land #$issue"
    if bash "$wt_land" "$issue" >/dev/null 2>&1; then
      log "  landed #$issue"
    else
      _escalate_blocked "$path" "$issue" "auto-land failed (merge conflict or suite failure) — needs a human"
    fi
  done < <(inflight_worktrees)
}

# answer_pass -> auto-answer every waiting spoke. reap_pass -> reap every hung/overrun one.
answer_pass() {
  local path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "waiting" ] && decide_and_act "$path" "$issue"
  done < <(inflight_worktrees)
}
reap_pass() {
  local path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "reap" ] || continue
    _reap_or_resume "$path" "$issue"
  done < <(inflight_worktrees)
}

# --- marker reconciliation (issue #109, AC3) ----------------------------------
# Live state wins over a stale marker. When the reaper blocked a spoke whose pane had only
# crashed, the spoke could be auto-resumed and resume committing — leaving blocked/<issue>
# stranded BEHIND the tip, falsely flagging an actively-working spoke (the #103 coexistence).
# Each tick reconciles: a blocked/<issue> tag with fresh commits on top of it is cleared
# (local + best-effort remote) and its durable record dropped. A ready/<issue> behind the
# tip needs no action — slot_state and _ready_at_tip already require a marker AT the tip, so
# a behind-tip ready is already ignored (live state wins).

# _afk_clear_blocked_record <issue> -> drop one issue's durable local block record once its
# branch advances past the stale marker (paired with _afk_record_blocked_locally).
_afk_clear_blocked_record() { rm -f "$(_afk_blocked_record "$1")" 2>/dev/null || true; }

# _blocked_tag_is_stale <wt> <issue> -> true when a local blocked/<issue> tag exists and the
# tip has fresh commits ON TOP of it (the tag is an ancestor of, but not equal to, the tip):
# the spoke resumed and is committing, so the blocked marker no longer reflects live state.
_blocked_tag_is_stale() {
  local wt="$1" issue="$2" tag tip
  tag="$(git -C "$wt" rev-parse -q --verify "refs/tags/blocked/${issue}^{commit}" 2>/dev/null)" || return 1
  [ -n "$tag" ] || return 1
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  [ "$tag" != "$tip" ] || return 1
  git -C "$wt" merge-base --is-ancestor "$tag" "$tip" 2>/dev/null
}

# _clear_stale_blocked_marker <wt> <issue> -> delete a stale blocked/<issue> (local + remote)
# and drop its durable record. Best-effort; never aborts the loop.
_clear_stale_blocked_marker() {
  local wt="$1" issue="$2"
  log "→ reconcile #$issue: branch advanced past blocked/$issue — clearing the stale marker"
  git -C "$wt" tag -d "blocked/$issue" >/dev/null 2>&1 || true
  git -C "$wt" push origin ":refs/tags/blocked/$issue" >/dev/null 2>&1 || true
  _afk_clear_blocked_record "$issue"
}

# reconcile_markers -> clear every stale blocked/<issue> across the in-flight set so the
# dashboard, hub-status and the durable record reflect live state, not a marker the spoke
# has since moved past. Run first each tick: slot_state never reads a behind-tip marker as
# done anyway, so this is for the external view + durable record, not the passes' own reads.
reconcile_markers() {
  local path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    _blocked_tag_is_stale "$path" "$issue" && _clear_stale_blocked_marker "$path" "$issue"
  done < <(inflight_worktrees)
}

# --- the supervisor tick + stop condition -------------------------------------

# supervise_tick -> one full pass: reconcile stale markers, dispatch the next batch, answer
# parked spokes, land the ready ones, reap the hung ones. Each pass re-surveys the in-flight
# set, so a spoke that changed state earlier in the tick is seen fresh.
supervise_tick() {
  reconcile_markers
  dispatch_batch
  answer_pass
  # If the answer pass detected a dead subscription token, skip land + reap this tick:
  # both would shell out to a `claude`/suite that is just as dead. The main loop blocks
  # the in-flight spokes and stops.
  [ "$_AFK_AUTH_FAILED" -eq 1 ] && return 0
  auto_land
  reap_pass
}

# afk_done <state> <now> -> true when the supervisor should stop: the window was turned off
# (no state), a clock-bound window expired, or the backlog is drained (the planner returns
# an empty batch AND nothing is in flight).
afk_done() {
  local state="$1" now="$2" bp inflight_count batch
  [ -n "$state" ] || return 0
  window_expired "$state" "$now" && return 0
  inflight_count="$(inflight_issues | grep -c '^[0-9]' || true)"
  [ "$inflight_count" -eq 0 ] || return 1
  bp="$(_afk_find_script "${BATCH_PLAN:-}" batch-plan.sh)" || return 1
  batch="$(bash "$bp" 2>/dev/null || true)"
  [ -z "$(printf '%s' "$batch" | tr -d '[:space:]')" ]
}

# --- watchdog (auto-restart a crashed supervisor, issue #107) ------------------
# A silent supervisor crash (exit 0 mid-tick) leaves .afk-state armed with no process
# draining — an in-flight spoke runs on with no answerer. The watchdog is a thin outer
# keeper that, every AFK_WATCHDOG_SECONDS, respawns the supervisor whenever a window is
# armed (afk_supervisor_state == stale) but no live pid is stamping the heartbeat. The
# respawn is a NO-ARG resume: it reads the persisted .afk-state and re-adopts in-flight
# spokes idempotently (dispatch_batch skips already-in-flight issues), so a restart
# recovers orphans without re-dispatching or re-arming. Exactly one watchdog runs per
# checkout (a pidfile dedups), and it exits when --off clears the state.
: "${AFK_WATCHDOG_SECONDS:=60}"

# _afk_self -> the path to THIS script, so the watchdog respawns the same supervisor.
_afk_self() { printf '%s\n' "${BASH_SOURCE[0]}"; }

# _afk_resume_launch -> the shell command the watchdog runs to respawn a crashed
# supervisor: a detached, NO-ARG launch of this script. No window spec ⇒ it resumes the
# persisted window (re-adopting spokes) rather than arming a fresh one. Pure (returns the
# string) so it is inspectable in a test without launching a real supervisor.
_afk_resume_launch() { printf 'nohup bash %s >/dev/null 2>&1 &' "$(_afk_self)"; }

# _afk_watchdog_respawn -> respawn the supervisor. AFK_RESPAWN_CMD overrides the launch
# for tests; otherwise the no-arg resume above. Best-effort; never aborts the watchdog.
_afk_watchdog_respawn() {
  if [ -n "${AFK_RESPAWN_CMD:-}" ]; then bash -c "$AFK_RESPAWN_CMD"; return 0; fi
  bash -c "$(_afk_resume_launch)"
  return 0
}

# watchdog_tick -> one watchdog check, printing the observed supervisor state:
#   off       — no window armed; the watchdog should stop.
#   live      — a supervisor is alive and stamping the heartbeat; nothing to do.
#   respawned — the window is armed but the supervisor is gone; respawn it.
watchdog_tick() {
  case "$(afk_supervisor_state)" in
    off)  printf 'off\n' ;;
    live) printf 'live\n' ;;
    stale)
      log "/afk watchdog: supervisor gone but window still armed — respawning"
      _afk_watchdog_respawn
      printf 'respawned\n' ;;
  esac
}

# _afk_watchdog_file -> the watchdog pidfile (under the git common dir), recording the
# live watchdog's pid so only one runs. AFK_WATCHDOG_FILE overrides it for tests.
_afk_watchdog_file() {
  if [ -n "${AFK_WATCHDOG_FILE:-}" ]; then printf '%s\n' "$AFK_WATCHDOG_FILE"; return; fi
  local common; common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=".git"
  printf '%s\n' "$common/.afk-watchdog"
}
# _afk_watchdog_alive -> true when the recorded watchdog pid is a live process.
_afk_watchdog_alive() {
  local f pid; f="$(_afk_watchdog_file)"; [ -f "$f" ] || return 1
  pid="$(head -n1 "$f" 2>/dev/null | tr -d '[:space:]')"
  _afk_pid_alive "$pid"
}

# _afk_spawn_watchdog -> launch a background watchdog UNLESS one is already alive (so a
# re-arm, or a no-arg resume, never stacks keepers). AFK_WATCHDOG_SPAWN_CMD overrides the
# launch for tests. Best-effort; never aborts the caller.
_afk_spawn_watchdog() {
  _afk_watchdog_alive && return 0
  if [ -n "${AFK_WATCHDOG_SPAWN_CMD:-}" ]; then bash -c "$AFK_WATCHDOG_SPAWN_CMD"; return 0; fi
  nohup bash "$(_afk_self)" --watchdog >/dev/null 2>&1 &
  # Record the child pid immediately so the next tick's dedup check sees it alive before
  # the watchdog itself writes the pidfile (closes the launch→pidfile startup race).
  printf '%s\n' "$!" > "$(_afk_watchdog_file)" 2>/dev/null || true
  return 0
}

# watchdog_loop -> the --watchdog keeper: record this pid, then each interval respawn the
# supervisor if it has crashed, until the window is turned off (--off). Cooperative: it
# exits on the first `off` it observes, so --off stops it within one watchdog interval.
watchdog_loop() {
  printf '%s\n' "$$" > "$(_afk_watchdog_file)" 2>/dev/null || true
  trap 'rm -f "$(_afk_watchdog_file)" 2>/dev/null || true' EXIT
  while :; do
    [ "$(watchdog_tick)" = "off" ] && { log "/afk watchdog: window off — exiting"; break; }
    sleep "$AFK_WATCHDOG_SECONDS"
  done
}

# --- remote launch (--remote) -------------------------------------------------
# Launch a detached, caffeinate-wrapped backlog drain on a configured always-on Mac over
# SSH (issue #73). The home Mac runs the drain unattended on the SAME Claude subscription
# (its spokes and answerers read ~/.claude); this is the cross-network trigger (a Tailscale
# hostname reachable from any network). Configured by env or a sourced conf file:
#   AFK_REMOTE_HOST      the always-on Mac's (Tailscale) hostname             [required]
#   AFK_REMOTE_REPO      the repo path on that host                           [required]
#   AFK_REMOTE_SESSION   the detached tmux session name               [default: afk]
#   AFK_REMOTE_DRAIN_CMD the command run under caffeinate on the host  [default: the
#                        supervisor script itself — see AFK_REMOTE_DEFAULT_DRAIN]
#   AFK_REMOTE_CONF      a shell snippet sourced for the above defaults [default: ~/.afk-remote]
#   AFK_SSH              the ssh binary (override for tests)           [default: ssh]
#
# The default launched command runs THIS supervisor script directly (hub-afk.sh drain) —
# NOT `claude "/afk drain"`. A bare `claude <prompt>` opens an interactive session and
# would stall unattended on a permission prompt before arming the supervisor; running the
# script is exactly what the /afk skill does locally, and it self-drives to backlog-empty.
# Override AFK_REMOTE_DRAIN_CMD (e.g. for a synced target's .ai-toolkit/ path) as needed.
AFK_REMOTE_DEFAULT_DRAIN="bash shared/skills/hub/scripts/hub-afk.sh drain"

# _load_remote_conf -> source the optional conf file for AFK_REMOTE_* defaults, with an
# explicit env value WINNING over the file (save env, source, restore the saved values).
_load_remote_conf() {
  local conf="${AFK_REMOTE_CONF:-$HOME/.afk-remote}"
  [ -f "$conf" ] || return 0
  local s_host="${AFK_REMOTE_HOST:-}" s_repo="${AFK_REMOTE_REPO:-}" \
        s_session="${AFK_REMOTE_SESSION:-}" s_drain="${AFK_REMOTE_DRAIN_CMD:-}"
  # shellcheck disable=SC1090
  . "$conf" 2>/dev/null || true
  [ -n "$s_host" ] && AFK_REMOTE_HOST="$s_host"
  [ -n "$s_repo" ] && AFK_REMOTE_REPO="$s_repo"
  [ -n "$s_session" ] && AFK_REMOTE_SESSION="$s_session"
  [ -n "$s_drain" ] && AFK_REMOTE_DRAIN_CMD="$s_drain"
  return 0
}

# build_remote_launch_cmd <repo> <session> <drain> -> the command run ON the remote host:
# cd into the repo and start a DETACHED tmux session that runs <drain> under `caffeinate -s`
# (keep the Mac awake for the whole drain). repo + session are single-quoted; <drain> is
# left unquoted so it can carry its own args/flags.
build_remote_launch_cmd() {
  local repo="$1" session="$2" drain="$3"
  printf "cd '%s' && tmux new -d -s '%s' 'caffeinate -s %s'\n" \
    "$repo" "$session" "$drain"
}

# remote_reattach_cmd <host> <session> -> the one-liner the user runs to attach to the
# unattended session (printed after a successful launch). -t forces the tty an attach needs.
remote_reattach_cmd() {
  printf "ssh %s -t 'tmux attach -t %s'\n" "$1" "$2"
}

# remote_launch -> resolve the remote config, SSH-launch the detached drain, CONFIRM the
# tmux session came up (so we never claim success on a silent failure), and print the
# reattach command. rc 2 on missing config, rc 1 on an ssh / confirmation failure.
remote_launch() {
  _load_remote_conf
  local host="${AFK_REMOTE_HOST:-}" repo="${AFK_REMOTE_REPO:-}" \
        session="${AFK_REMOTE_SESSION:-afk}" drain="${AFK_REMOTE_DRAIN_CMD:-$AFK_REMOTE_DEFAULT_DRAIN}" \
        ssh="${AFK_SSH:-ssh}" remote_cmd
  if [ -z "$host" ]; then
    log "/afk --remote: set AFK_REMOTE_HOST (the always-on Mac's Tailscale hostname) — see docs/remote-afk.md"
    return 2
  fi
  if [ -z "$repo" ]; then
    log "/afk --remote: set AFK_REMOTE_REPO (the repo path on $host) — see docs/remote-afk.md"
    return 2
  fi
  remote_cmd="$(build_remote_launch_cmd "$repo" "$session" "$drain")"
  log "→ launching unattended drain on $host (tmux session '$session')"
  # No -t here: tmux new -d detaches, so forcing a tty only triggers ssh's
  # "Pseudo-terminal will not be allocated" warning when the trigger has no tty (cron).
  if ! "$ssh" "$host" "$remote_cmd"; then
    log "/afk --remote: ssh launch failed — is $host reachable (Tailscale up)?"
    return 1
  fi
  if ! "$ssh" "$host" tmux has-session -t "$session" 2>/dev/null; then
    log "/afk --remote: launched but tmux session '$session' not found on $host — check the host"
    return 1
  fi
  log "✓ launched on $host — draining unattended until the backlog is empty"
  remote_reattach_cmd "$host" "$session"
  return 0
}

# --- telemetry preflight (issue #108) -----------------------------------------
# AFK's contract is that the dashboard is the single source of truth for an unattended
# run: a spoke spawned while the otelcol collector (:4317) / Langfuse bridge (:4319) are
# down, or LANGFUSE_BASIC_AUTH is unset, silently loses its telemetry — and #106's
# spoke-side wt_otel_*_preflight only WARNS and launches anyway, a per-spawn line that
# scrolls past with no human watching. So the SUPERVISOR runs ONE loud preflight before
# the first dispatch: resolve + export auth (so every spoke inherits working
# credentials), bring the collector and bridge up idempotently (reusing worktree-lib's
# launchers), and REFUSE TO ARM if any of the three can't be wired. AI_TOOLKIT_OTEL=0 is
# the sole opt-out — unset is treated as enabled, the SSOT-for-unattended default.
#
#   AFK_TELEMETRY_CONF   optional conf file sourced for LANGFUSE_BASIC_AUTH / LANGFUSE_HOST
#                        (env wins each field independently) [default: ~/.afk-telemetry]
#   AFK_PORT_WAIT_TRIES  re-probe attempts after a launch before declaring a port DOWN [10]
#   AFK_PORT_WAIT_SLEEP  seconds between those re-probes (a slow container start)        [1]

# afk_telemetry_enabled -> true unless AI_TOOLKIT_OTEL=0 (the sole opt-out; unset ⇒ on).
afk_telemetry_enabled() { [ "${AI_TOOLKIT_OTEL:-}" != "0" ]; }

# afk_resolve_telemetry_auth -> resolve LANGFUSE_BASIC_AUTH (env first, then the optional
# conf file — env wins, mirroring _load_remote_conf) and EXPORT it + LANGFUSE_HOST so
# every dispatched spoke inherits working credentials. Also exports AI_TOOLKIT_OTEL=1 so
# spokes opt in to native OTel. rc 1 when no auth can be resolved (caller refuses to arm).
afk_resolve_telemetry_auth() {
  local conf="${AFK_TELEMETRY_CONF:-$HOME/.afk-telemetry}"
  # Source the conf for BOTH fields (auth and host) with env winning each independently —
  # the same save-source-restore precedence as _load_remote_conf — so an operator can set
  # auth in the env and still pick host up from the file (and vice versa).
  if [ -f "$conf" ]; then
    local s_auth="${LANGFUSE_BASIC_AUTH:-}" s_host="${LANGFUSE_HOST:-}"
    # shellcheck disable=SC1090
    . "$conf" 2>/dev/null || true
    [ -n "$s_auth" ] && LANGFUSE_BASIC_AUTH="$s_auth"
    [ -n "$s_host" ] && LANGFUSE_HOST="$s_host"
  fi
  [ -n "${LANGFUSE_BASIC_AUTH:-}" ] || return 1
  export LANGFUSE_BASIC_AUTH
  export LANGFUSE_HOST="${LANGFUSE_HOST:-http://localhost:3000}"
  export AI_TOOLKIT_OTEL=1
}

# afk_ensure_port <port> <launch-fn> <repo_root> -> ensure something LISTENs on <port>:
# a no-op when already up; otherwise run <launch-fn> <repo_root> and re-probe up to
# AFK_PORT_WAIT_TRIES times (so a slow container start isn't a false DOWN). rc 1 when the
# port is still down after the launch — the caller turns that into a refuse-to-arm.
afk_ensure_port() {
  local port="$1" launch="$2" repo_root="$3" tries="${AFK_PORT_WAIT_TRIES:-10}" i=0
  wt_port_listening "$port" && return 0
  "$launch" "$repo_root"
  while [ "$i" -lt "$tries" ]; do
    wt_port_listening "$port" && return 0
    i=$((i + 1))
    sleep "${AFK_PORT_WAIT_SLEEP:-1}"
  done
  wt_port_listening "$port"
}

# afk_telemetry_preflight <repo_root> -> the one loud preflight the supervisor runs before
# the first dispatch. A no-op (rc 0) when telemetry is opted out. Otherwise resolve+export
# auth (refuse if none), then bring the collector up BEFORE the bridge (the collector forks
# to the bridge), refusing if either won't come up. rc 0 ⇒ wired; rc 1 ⇒ refuse to arm.
afk_telemetry_preflight() {
  local repo_root="$1"
  afk_telemetry_enabled || return 0
  if ! afk_resolve_telemetry_auth; then
    log "/afk: telemetry preflight FAILED — LANGFUSE_BASIC_AUTH unset and no conf to resolve it; refusing to arm (set it, or AI_TOOLKIT_OTEL=0 to run without telemetry)"
    return 1
  fi
  if ! afk_ensure_port 4317 wt_collector_launch "$repo_root"; then
    log "/afk: telemetry preflight FAILED — otelcol collector down on :4317 and won't come up; refusing to arm (set AI_TOOLKIT_OTEL=0 to run without telemetry)"
    return 1
  fi
  if ! afk_ensure_port 4319 wt_bridge_launch "$repo_root"; then
    log "/afk: telemetry preflight FAILED — Langfuse bridge down on :4319 and won't come up; refusing to arm (set AI_TOOLKIT_OTEL=0 to run without telemetry)"
    return 1
  fi
  log "/afk: telemetry preflight OK — collector :4317, bridge :4319, auth present"
  return 0
}

# afk_have_telemetry_auth -> true when LANGFUSE_BASIC_AUTH is resolvable (env, or a
# non-empty value from the conf file) WITHOUT mutating the caller's environment — the
# READ-ONLY check --status uses. It resolves the conf in a SUBSHELL so a status read has
# no side effect on the parent env, yet agrees EXACTLY with the preflight's
# afk_resolve_telemetry_auth (which sources + requires a non-empty value) — a commented-out
# or empty assignment reports missing in both, so --status never claims OK for a conf the
# supervisor would have refused to arm on.
afk_have_telemetry_auth() {
  [ -n "${LANGFUSE_BASIC_AUTH:-}" ] && return 0
  local conf="${AFK_TELEMETRY_CONF:-$HOME/.afk-telemetry}"
  [ -f "$conf" ] || return 1
  # shellcheck disable=SC1090
  ( . "$conf" 2>/dev/null; [ -n "${LANGFUSE_BASIC_AUTH:-}" ] )
}

# afk_telemetry_status -> a one-line, READ-ONLY telemetry health summary for --status: the
# up/down of the collector (:4317) and bridge (:4319) and whether auth is resolvable, e.g.
#   /afk: telemetry OK (collector up, bridge up, auth present)
#   /afk: telemetry DOWN (collector down, bridge up, auth missing)
# Prints nothing when telemetry is opted out (AI_TOOLKIT_OTEL=0). Probes only — never
# launches anything (unlike the preflight), so a status read is free of side effects.
afk_telemetry_status() {
  afk_telemetry_enabled || return 0
  local c b a overall
  wt_port_listening 4317 && c=up || c=down
  wt_port_listening 4319 && b=up || b=down
  afk_have_telemetry_auth && a=present || a=missing
  if [ "$c" = up ] && [ "$b" = up ] && [ "$a" = present ]; then overall=OK; else overall=DOWN; fi
  printf '/afk: telemetry %s (collector %s, bridge %s, auth %s)\n' "$overall" "$c" "$b" "$a"
}

# --- CLI ----------------------------------------------------------------------

# _afk_status_state_line <state> <now> -> echo the window's state line: STALE (#107) when
# the supervisor pid is gone, else draining / window-elapsed / "Nm remaining".
_afk_status_state_line() {
  local state="$1" now="$2" rem age
  # Cross-check the heartbeat before trusting the state file: a window armed in
  # .afk-state but no live supervisor pid means the process crashed and the state file
  # is lying (#107). Report STALE rather than echoing `draining` / `Nm remaining`.
  if [ "$(afk_supervisor_state)" = "stale" ]; then
    age="$(_afk_heartbeat_age_minutes)"
    if [ -n "$age" ]; then
      echo "/afk: STALE — last tick ${age}m ago, supervisor process not found (run /afk --off to clear, or the watchdog will respawn it)"
    else
      echo "/afk: STALE — no heartbeat, supervisor process not found (run /afk --off to clear, or the watchdog will respawn it)"
    fi
    return 0
  fi
  if [ "$state" = "drain" ]; then echo "/afk: draining (no clock bound — stops when the backlog is empty)"; return 0; fi
  if window_expired "$state" "$now"; then echo "/afk: window elapsed (supervisor will stop on its next tick)"; return 0; fi
  rem="$(minutes_remaining "$state" "$now")"
  echo "/afk: on — ${rem}m remaining (until $(wt_date_ymd "$state") $(date -r "$state" +%H:%M 2>/dev/null || date -d "@$state" +%H:%M))"
}

# afk_blocked_locally_status -> a one-line summary of issues with a DURABLE local block
# record (escalation could not push the tag, #109), or nothing when there are none. The
# operator returning from AFK reads --status, so a block that never reached the dashboard
# must still surface here — never silently dropped.
afk_blocked_locally_status() {
  local dir f issue list=""
  dir="$(_afk_state_dir)"
  [ -d "$dir" ] || return 0
  for f in "$dir"/blocked-*.txt; do
    [ -e "$f" ] || continue
    issue="${f##*/blocked-}"; issue="${issue%.txt}"
    list="${list:+$list, }#$issue"
  done
  [ -n "$list" ] || return 0
  printf '/afk: locally blocked (escalation could not push the tag — needs a human): %s [%s]\n' \
    "$list" "$dir"
}

_status() {
  local state now
  state="$(afk_read_state)"; now="$(afk_now)"
  if [ -z "$state" ]; then
    echo "/afk: off"
    # A durable escalation outlives the drain — surface it even when off, so the operator
    # returning from AFK sees a block that never reached the dashboard (#109).
    afk_blocked_locally_status
    return 0
  fi
  _afk_status_state_line "$state" "$now"
  # For a live (or stale) drain, surface telemetry health too: the dashboard is the SSOT,
  # so the operator must be able to see whether it's actually receiving data (#108). A
  # no-op line when telemetry is opted out (AI_TOOLKIT_OTEL=0).
  afk_telemetry_status
  afk_blocked_locally_status
}

main() {
  # --remote triggers a drain on a DIFFERENT machine and operates only on the remote repo,
  # so it needs no local checkout — handle it before the git-repo guard.
  if [ "${1:-}" = "--remote" ]; then remote_launch; return $?; fi

  MAIN_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { log "not inside a git repository"; return 1; }

  # Subcommands that do not start the LOCAL supervisor loop.
  case "${1:-}" in
    --status)   _status; return 0 ;;
    --off)      afk_clear_state; echo "/afk: off (state cleared; the supervisor + watchdog stop on their next tick)"; return 0 ;;
    --watchdog) watchdog_loop; return $? ;;
    -h|--help)  sed -n '2,57p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
  esac

  local once=0
  if [ "${1:-}" = "--once" ]; then
    once=1
  elif [ -n "${1:-}" ]; then
    # A window spec: compute + persist the end bound before the first tick.
    local end
    end="$(compute_end_epoch "$@" "$(afk_now)")" || { log "unrecognized window: '$*' (use <duration>, 'until HH:MM', or 'drain')"; return 2; }
    # Telemetry preflight BEFORE arming: an unattended drain must not dispatch spokes into
    # a dead telemetry pipeline (the dashboard is the SSOT). Refuse to arm — write no state,
    # never reach the loop — when collector/bridge/auth can't be wired (#108).
    afk_telemetry_preflight "$MAIN_ROOT" || return 2
    afk_write_state "$end"
    _clear_dispatch_epochs   # fresh window ⇒ empty "dispatched by this run" set
    _clear_resume_markers    # fresh window ⇒ every spoke gets its one auto-resume again
    _clear_blocked_records   # fresh window ⇒ --status shows only THIS run's durable blocks
    _afk_set_unattended      # arm the fail-closed anti-gutting tripwire for spokes
    log "/afk: armed ($([ "$end" = drain ] && echo 'drain — until the backlog is empty' || echo "until $(wt_date_ymd "$end") $(date -r "$end" +%H:%M 2>/dev/null || date -d "@$end" +%H:%M)"))"
  fi

  while :; do
    afk_write_heartbeat   # stamp this tick before working, so a crash mid-tick is visible
    # Keep exactly one watchdog alive (idempotent: a no-op while one runs, respawns it if
    # it died). Doing this each tick — not just at arm — means the supervisor and watchdog
    # heal each other: neither is a single silent point of failure (#107). Skipped for
    # --once (a one-shot cron tick must not leave a background keeper behind).
    [ "$once" -eq 0 ] && _afk_spawn_watchdog
    supervise_tick
    if [ "$_AFK_AUTH_FAILED" -eq 1 ]; then
      log "/afk: subscription auth failed — blocking in-flight spokes and stopping (re-run /login on the host)"
      _block_all_inflight "subscription auth failed — token could not refresh; re-run /login on the host"
      afk_clear_state; break
    fi
    [ "$once" -eq 1 ] && break
    if afk_done "$(afk_read_state)" "$(afk_now)"; then
      log "/afk: done"; afk_clear_state; break
    fi
    sleep "$AFK_TICK_SECONDS"
  done
  return 0
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
