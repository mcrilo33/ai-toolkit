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
#   AFK_TICK_SECONDS=300         supervisor poll interval
#   AFK_SPOKE_MAX_MINUTES=180    wall-clock ceiling per spoke before a reap
#   AFK_IDLE_MINUTES=30          a spoke idle this long with no marker AND not waiting → reap
#   AFK_ANSWERER_CMD             the answerer command (default: claude -p --model opus)
#   AFK_ANSWERER_EFFORT=high     thinking budget for the answerer (exported as CLAUDE_EFFORT)
#   AFK_NOW                      override "now" (epoch seconds) — testing/cron
#   AFK_STATE                    state-file path (default: <git-common-dir>/.afk-state)
#   AFK_RULE_FILE                path to the afk-answering rule (auto-resolved otherwise)
#   WT_NEW / WT_LAND / SPOKE_READY / BATCH_PLAN   override the resolved sibling scripts
#   AFK_WT_LIB                   override the sourced worktree-lib.sh
#   CLAUDE_PROJECTS_DIR          transcript root (default: $HOME/.claude/projects)
#   AFK_REMOTE_HOST / AFK_REMOTE_REPO / AFK_REMOTE_SESSION / AFK_REMOTE_CLAUDE
#                                --remote target config (or a sourced AFK_REMOTE_CONF file)
#
# Usage:
#   hub-afk.sh <duration>        # e.g. 90, 30m, 1h, 1h30m — drain for that long, then stop
#   hub-afk.sh until <HH:MM>     # drain until the next HH:MM, then stop
#   hub-afk.sh drain             # drain until the backlog is empty + nothing in flight
#   hub-afk.sh --remote          # launch a detached `drain` on a configured always-on Mac
#   hub-afk.sh --status          # report the active window (or "off")
#   hub-afk.sh --off             # stop the supervisor (clears the state file)
#   hub-afk.sh --once            # run a single tick and exit (tests / external cron)
#
# Run it on the hub (main checkout, on the default branch). Read-only against the work
# except for dispatching, answering, landing, and reaping spokes. --remote runs the drain
# on a different machine instead (see docs/remote-afk.md).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${AFK_TICK_SECONDS:=300}"
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
afk_clear_state() { rm -f "$(afk_state_file)" 2>/dev/null || true; }

# --- per-spoke dispatch epochs (the wall-clock reap reference) ----------------
_afk_state_dir() {
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
                asks, texts = [], []
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
                if texts:
                    last_asst_text = "\n".join(t for t in texts if t).strip()
                pending = asks or None
            elif last_type == "user":
                pending = None
except Exception:
    pass

out = ""
if pending:
    out = "\n\n".join(pending)
elif last_type == "notification":
    out = last_asst_text
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
  local cmd="${AFK_ANSWERER_CMD:-claude -p --model opus}"
  CLAUDE_EFFORT="$AFK_ANSWERER_EFFORT" bash -c "$cmd" <<<"$prompt" 2>&1
}

# parse_decision <raw-answerer-output> -> "ANSWER\t<text>" or "ESCALATE\t<reason>" on
# stdout, or empty when the answerer emitted no decision line. The LAST matching line
# wins (the answerer reasons first, then concludes).
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
# `send-keys -l` sends the text literally (no key-name interpretation), then a separate
# Enter submits — the gotcha-proof pattern for re-driving an idle spoke.
inject_answer() {
  local target="$1" text="$2"
  command -v tmux >/dev/null 2>&1 || return 1
  [ -n "$target" ] || return 1
  tmux send-keys -t "$target" -l -- "$text" 2>/dev/null || return 1
  tmux send-keys -t "$target" Enter 2>/dev/null || return 1
}

# afk_emit_decision <wt_path> <status> -> one kind=agent span per auto-answer decision,
# attributed to the SPOKE (emit with the worktree as CWD, like worktree-lib does), so the
# decision surfaces on the observability dashboard. Metadata only — the question→answer
# text rides the answerer's own sidecar session (the dashboard's node summary), never the
# span (the telemetry privacy contract logs no payload). No-op when telemetry is off.
afk_emit_decision() {
  command -v telemetry_emit_span >/dev/null 2>&1 || return 0
  local wt="$1" status="$2"
  [ -d "$wt" ] || return 0
  ( cd "$wt" && telemetry_emit_span --kind agent --name afk-answer --phase review --status "$status" ) || true
  return 0
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
    if inject_answer "$target" "$text"; then
      log "  injected answer into #$issue"
      afk_emit_decision "$wt" success
      return 0
    fi
    log "  could not inject into #$issue — escalating"
    text="could not locate spoke pane to inject the answer"
  elif [ "$kind" = "ESCALATE" ]; then
    [ -n "$text" ] || text="answerer escalated (no reason given)"
  else
    text="answerer returned no decision — escalating for human review"
  fi
  _escalate_blocked "$wt" "$issue" "$text"
}

# _escalate_blocked <wt_path> <issue> <reason> -> emit blocked/<issue> on the spoke's
# behalf via spoke-ready.sh, and a deny decision span. Best-effort; never aborts the loop.
_escalate_blocked() {
  local wt="$1" issue="$2" reason="$3" sr
  log "  escalate #$issue: $reason"
  sr="$(_afk_find_script "${SPOKE_READY:-}" spoke-ready.sh)" \
    && ( cd "$wt" && "$sr" --blocked "$issue" -m "$reason" ) >/dev/null 2>&1 \
    || log "  could not emit blocked/$issue"
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
# line is read from each in-flight issue's body (the same source batch-plan reads).
_inflight_scope_args() {
  local issue body scope
  while IFS= read -r issue; do
    [ -n "$issue" ] || continue
    body="$(gh issue view "$issue" --json body -q .body 2>/dev/null || true)"
    scope="$(printf '%s\n' "$body" | sed -n 's/^[[:space:]]*[Ss]cope:[[:space:]]*//p' | head -1)"
    [ -n "$scope" ] && printf -- '--inflight\n%s\n' "$scope"
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
    if bash "$wt_new" "$n" --type feature --prompt "$(kickoff_for "$n")"; then
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

# auto_land -> land every ready/<issue> spoke. A failed land (merge conflict / suite fail)
# emits blocked/<issue> and the drain continues; a landed spoke frees its scope + its
# dependents' blockers for the next tick's plan.
auto_land() {
  local wt_land path issue
  wt_land="$(_afk_find_script "${WT_LAND:-}" worktree-land.sh)" || { log "worktree-land.sh not found — skipping land"; return 0; }
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    _ready_at_tip "$path" "$issue" || continue
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
  local path issue reason
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    [ "$(slot_state "$path" "$issue")" = "reap" ] || continue
    if spoke_over_ceiling "$(read_dispatch_epoch "$issue")" "$(afk_now)"; then
      reason="time ceiling: ran >${AFK_SPOKE_MAX_MINUTES}m without finishing"
    else
      reason="went idle >${AFK_IDLE_MINUTES}m with no marker and not waiting — likely hung"
    fi
    reap_spoke "$path" "$issue" "$reason"
  done < <(inflight_worktrees)
}

# --- the supervisor tick + stop condition -------------------------------------

# supervise_tick -> one full pass: dispatch the next batch, answer parked spokes, land the
# ready ones, reap the hung ones. Each pass re-surveys the in-flight set, so a spoke that
# changed state earlier in the tick is seen fresh.
supervise_tick() {
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

# --- remote launch (--remote) -------------------------------------------------
# Launch a detached, caffeinate-wrapped `/afk drain` on a configured always-on Mac over
# SSH (issue #73). The home Mac runs the drain unattended on the SAME Claude subscription;
# this is the cross-network trigger (a Tailscale hostname reachable from any network). The
# host is configured by env or a sourced conf file:
#   AFK_REMOTE_HOST     the always-on Mac's (Tailscale) hostname            [required]
#   AFK_REMOTE_REPO     the repo path on that host                          [required]
#   AFK_REMOTE_SESSION  the detached tmux session name              [default: afk]
#   AFK_REMOTE_CLAUDE   the claude invocation on the host (may carry flags) [default: claude]
#   AFK_REMOTE_CONF     a shell snippet sourced for the above defaults [default: ~/.afk-remote]
#   AFK_SSH             the ssh binary (override for tests)          [default: ssh]

# _load_remote_conf -> source the optional conf file for AFK_REMOTE_* defaults, with an
# explicit env value WINNING over the file (save env, source, restore the saved values).
_load_remote_conf() {
  local conf="${AFK_REMOTE_CONF:-$HOME/.afk-remote}"
  [ -f "$conf" ] || return 0
  local s_host="${AFK_REMOTE_HOST:-}" s_repo="${AFK_REMOTE_REPO:-}" \
        s_session="${AFK_REMOTE_SESSION:-}" s_claude="${AFK_REMOTE_CLAUDE:-}"
  # shellcheck disable=SC1090
  . "$conf" 2>/dev/null || true
  [ -n "$s_host" ] && AFK_REMOTE_HOST="$s_host"
  [ -n "$s_repo" ] && AFK_REMOTE_REPO="$s_repo"
  [ -n "$s_session" ] && AFK_REMOTE_SESSION="$s_session"
  [ -n "$s_claude" ] && AFK_REMOTE_CLAUDE="$s_claude"
  return 0
}

# build_remote_launch_cmd <repo> <session> <claude> -> the command run ON the remote host:
# cd into the repo and start a DETACHED tmux session that runs `/afk drain` under
# `caffeinate -s` (keep the Mac awake for the whole drain). repo + session are single-
# quoted; <claude> is left unquoted so AFK_REMOTE_CLAUDE can carry flags.
build_remote_launch_cmd() {
  local repo="$1" session="$2" claude="$3"
  printf "cd '%s' && tmux new -d -s '%s' 'caffeinate -s %s \"/afk drain\"'\n" \
    "$repo" "$session" "$claude"
}

# remote_reattach_cmd <host> <session> -> the one-liner the user runs to attach to the
# unattended session (printed after a successful launch).
remote_reattach_cmd() {
  printf "ssh %s -t 'tmux attach -t %s'\n" "$1" "$2"
}

# remote_launch -> resolve the remote config, SSH-launch the detached drain, CONFIRM the
# tmux session came up (so we never claim success on a silent failure), and print the
# reattach command. rc 2 on missing config, rc 1 on an ssh / confirmation failure.
remote_launch() {
  _load_remote_conf
  local host="${AFK_REMOTE_HOST:-}" repo="${AFK_REMOTE_REPO:-}" \
        session="${AFK_REMOTE_SESSION:-afk}" claude="${AFK_REMOTE_CLAUDE:-claude}" \
        ssh="${AFK_SSH:-ssh}" remote_cmd
  if [ -z "$host" ]; then
    log "/afk --remote: set AFK_REMOTE_HOST (the always-on Mac's Tailscale hostname) — see docs/remote-afk.md"
    return 2
  fi
  if [ -z "$repo" ]; then
    log "/afk --remote: set AFK_REMOTE_REPO (the repo path on $host) — see docs/remote-afk.md"
    return 2
  fi
  remote_cmd="$(build_remote_launch_cmd "$repo" "$session" "$claude")"
  log "→ launching unattended /afk drain on $host (tmux session '$session')"
  if ! "$ssh" "$host" -t "$remote_cmd"; then
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

# --- CLI ----------------------------------------------------------------------

_status() {
  local state now rem
  state="$(afk_read_state)"; now="$(afk_now)"
  if [ -z "$state" ]; then echo "/afk: off"; return 0; fi
  if [ "$state" = "drain" ]; then echo "/afk: draining (no clock bound — stops when the backlog is empty)"; return 0; fi
  if window_expired "$state" "$now"; then echo "/afk: window elapsed (supervisor will stop on its next tick)"; return 0; fi
  rem="$(minutes_remaining "$state" "$now")"
  echo "/afk: on — ${rem}m remaining (until $(wt_date_ymd "$state") $(date -r "$state" +%H:%M 2>/dev/null || date -d "@$state" +%H:%M))"
}

main() {
  MAIN_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { log "not inside a git repository"; return 1; }

  # Subcommands that do not start the LOCAL loop.
  case "${1:-}" in
    --status) _status; return 0 ;;
    --off)    afk_clear_state; echo "/afk: off (state cleared; the supervisor stops on its next tick)"; return 0 ;;
    --remote) remote_launch; return $? ;;
    -h|--help) sed -n '2,53p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
  esac

  local once=0
  if [ "${1:-}" = "--once" ]; then
    once=1
  elif [ -n "${1:-}" ]; then
    # A window spec: compute + persist the end bound before the first tick.
    local end
    end="$(compute_end_epoch "$@" "$(afk_now)")" || { log "unrecognized window: '$*' (use <duration>, 'until HH:MM', or 'drain')"; return 2; }
    afk_write_state "$end"
    log "/afk: armed ($([ "$end" = drain ] && echo 'drain — until the backlog is empty' || echo "until $(wt_date_ymd "$end") $(date -r "$end" +%H:%M 2>/dev/null || date -d "@$end" +%H:%M)"))"
  fi

  while :; do
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
