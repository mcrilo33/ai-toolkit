#!/usr/bin/env bash
# gate-broker.sh — the shared gate-broker core (issue #155).
#
# ONE machine services a parked spoke gate for BOTH the unattended /afk supervisor and the
# attended reviewer. hub-afk.sh sources this file and drives it as the *unattended* adapter
# (decide_and_act -> broker_service_gate ... unattended); the attended QCM adapter (subtask
# C) drives the same core. The six shared stages:
#
#   1. DETECT   a parked gate: gate/<N> at the tip (_gate_parked) or a pending question /
#               permission dialog (slot_state == waiting, _permission_pending).
#   2. EXTRACT  the plan/prompt the spoke parked on (extract_pending_question), or the
#               command a permission dialog is gating (extract_pending_command).
#   3. REASON   one fresh ephemeral context per gate (run_answerer -> parse_decision),
#               governed by the afk-answering rule. (Subtask B adds read-only worktree
#               evidence + the decisions-digest seed.)
#   4. CLASSIFY obvious, safe scoped self-ops decided by a fixed rules table
#               (classify_permission); a genuine judgment call routes out to the adapter.
#   5. INJECT   the ONE hardened injector (inject_and_verify): Esc-first menu cancel,
#               send-keys -l, a SEPARATE Enter, a bare-Enter retry that never re-pastes,
#               wedge -> pane respawn. Shared by both modes so the paste bugs are fixed once.
#   6. LOG      an auto-answer decision span (afk_emit_decision). (Subtask D adds the
#               automatable-decisions log + codification pass.)
#
# broker_service_gate <wt> <issue> [mode] is the orchestrator; the ONLY mode-divergent seam
# is _broker_on_human_decision (unattended -> escalate blocked/<N>; attended -> QCM).
#
# Sourceable on its own (the tests do): it pulls worktree-lib.sh and defines every helper it
# needs. respawn_wedged_spoke (a supervisor-lifecycle recovery) is the one outward call,
# reached by a runtime existence-check so a standalone/attended broker degrades to escalate.
set -uo pipefail

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# Defaults the moved reasoner/detector read directly (set -u safe when sourced standalone;
# idempotent when hub-afk.sh already set them).
: "${AFK_SPOKE_MAX_MINUTES:=180}"
: "${AFK_IDLE_MINUTES:=30}"
: "${AFK_ANSWERER_EFFORT:=high}"
# Warned-retry backoff (issue #241): a converted stop site parks a spoke LAST rather than
# abandoning it — warn, then re-service on an exponential backoff so a persistently-failing
# spoke is retried rarely (doom-loop safety by the curve, not by abandonment; #144/#140/#202).
: "${AFK_WARN_BACKOFF_BASE:=60}"
: "${AFK_WARN_BACKOFF_CAP:=1800}"

# --- source worktree-lib.sh (the shared date/time + worktree helpers) ---------
# Resolution covers both layouts: the ai-toolkit checkout (scripts/worktree-lib.sh, four
# levels up) and a synced target (.ai-toolkit/scripts/). AFK_WT_LIB wins for tests.
_AFK_TOPLEVEL="${_AFK_TOPLEVEL:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
for _cand in \
  "${AFK_WT_LIB:-}" \
  "$SCRIPT_DIR/worktree-lib.sh" \
  "$SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/scripts/worktree-lib.sh}" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/worktree-lib.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

log() { printf '%s\n' "$*" >&2; }

# --- now-clock ----------------------------------------------------------------
# Current time, overridable via AFK_NOW for tests/cron.
afk_now() { printf '%s\n' "${AFK_NOW:-$(date +%s)}"; }

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

# --- event spool (issue #176) -------------------------------------------------
# The event-driven wake path: a spoke ANNOUNCES a state change (a marker push, a
# permission/question park) by dropping one content-free file per event, named
# <epoch>-<issue>-<type>, into this spool and SIGUSR1-ing the supervisor. Events are
# WAKE-UPS, not state — on wake the supervisor re-derives everything via slot_state, so a
# duplicate, stale, or lost event is safe (a lost one is caught by the next full sweep).
# The reader (hub-afk.sh) lives here; the two writers (spoke-ready.sh + the Notification
# hook) deploy to different dirs than the reader and inline the same tiny emit, sharing
# only this filename contract.
afk_event_dir() { printf '%s\n' "$(_afk_state_dir)/events"; }

# afk_drain_event_issues -> print each DISTINCT issue number that has a spooled event
# (one per line, sorted) and delete every spool file. Malformed names (no <issue> field)
# are dropped silently. Draining and dedup happen in one pass: the caller re-derives each
# named spoke's state via slot_state, so servicing an issue once per drain is enough.
afk_drain_event_issues() {
  local dir f base issue; dir="$(afk_event_dir)"
  [ -d "$dir" ] || return 0
  for f in "$dir"/*; do
    [ -f "$f" ] || continue
    base="${f##*/}"                      # <epoch>-<issue>-<type>
    issue="${base#*-}"; issue="${issue%%-*}"   # middle field
    case "$issue" in '' | *[!0-9]*) ;; *) printf '%s\n' "$issue" ;; esac
    rm -f "$f" 2>/dev/null || true
  done | sort -un
}

# --- per-spoke progress + answer-attempt epochs (issue #133, subtask 3) --------
# progress-<issue>.epoch — the reap ceiling's reference alongside the dispatch epoch:
# stamped when the branch tip advances between ticks, on a resume/respawn, and when a
# stale blocked marker is cleared, so a deliberately revived spoke gets a fresh
# ceiling instead of an instant re-reap (#123/#128).
# answer-attempt-<issue>.epoch — the idle clock's exclusion: stamped when the
# supervisor attempts an answer delivery, so time spent with a buffered/undelivered
# answer never reads as idle (the reaper killed #125 mid-delivery).
_stamp_issue_epoch() {
  local name="$1" issue="$2" dir
  dir="$(_afk_state_dir)"
  mkdir -p "$dir" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$dir/$name-$issue.epoch" 2>/dev/null || true
}
_read_issue_epoch() {
  local f; f="$(_afk_state_dir)/$1-$2.epoch"
  [ -f "$f" ] && cat "$f" 2>/dev/null || true
}
stamp_progress_epoch()  { _stamp_issue_epoch progress "$1"; }
read_progress_epoch()   { _read_issue_epoch progress "$1"; }
stamp_answer_attempt()  { _stamp_issue_epoch answer-attempt "$1"; }
read_answer_attempt()   { _read_issue_epoch answer-attempt "$1"; }
# Fresh window ⇒ no stale progress/attempt state: a leftover answer-attempt epoch
# would suppress a legitimate idle reap in the next window; a leftover re-answer counter
# (#203) would strand a spoke at a ceiling reached in a prior window; a leftover gate-voided /
# terminal-logged marker (#237) would keep a since-resolved gate terminal across windows.
_clear_progress_state() {
  local dir; dir="$(_afk_state_dir)"
  rm -f "$dir"/progress-*.epoch "$dir"/answer-attempt-*.epoch "$dir"/tip-* \
    "$dir"/reanswer-* "$dir"/gate-voided-* "$dir"/terminal-logged-* 2>/dev/null || true
}

# --- re-answer ceiling (issue #203, finding 1) --------------------------------
# #171's blocked-at-tip→waiting fix made a parked spoke re-answerable with NO attempt
# ceiling: a legitimately-escalated spoke (answerer ESCALATE, timeout, unconfirmable
# inject) stays on the SAME prompt, and every tick re-ran the full 900s reasoner to reach
# the same ESCALATE — a doom-loop starving the tick and burning the subscription. The
# ceiling caps attempts on the SAME (tip, prompt-signature); a changed prompt or a moved
# tip resets it. Keyed like the decisions-log signature machinery (a content hash here).

# _broker_park_signature <wt> <issue> -> a stable hash of WHATEVER prompt the spoke is
# parked on (a permission command, a PLAN-gate plan, or an AskUserQuestion), or empty when
# nothing is extractable. Empty ⇒ the ceiling never engages (fail-open to answering).
_broker_park_signature() {
  local wt="$1" issue="$2" basis=""
  if _permission_pending "$wt"; then
    basis="perm:$(extract_pending_command "$wt")"
  elif _gate_parked "$wt" "$issue"; then
    basis="gate:$(_read_gate_artifact "$wt" "$issue")"
    [ "$basis" = "gate:" ] && basis="gate:$(extract_pending_question "$wt")"
  else
    basis="q:$(extract_pending_question "$wt")"
  fi
  case "$basis" in perm: | gate: | q:) return 0 ;; esac    # nothing extractable
  printf '%s' "$basis" | shasum -a 256 2>/dev/null | awk '{print $1}'
}

# _reanswer_state_file <issue> -> the per-issue counter file: "<tip>\t<sig>\t<count>".
_reanswer_state_file() { printf '%s\n' "$(_afk_state_dir)/reanswer-$1"; }

# _broker_reanswer_exhausted <wt> <issue> <sig> -> rc 0 (EXHAUSTED — be terminal, skip the
# reasoner) when the SAME (tip, sig) has already been attempted AFK_REANSWER_CEILING (default
# 2) times; otherwise rc 1 AND this attempt is RECORDED (the counter bumped). A changed tip
# or signature resets the counter. An empty signature never suppresses (fail-open).
_broker_reanswer_exhausted() {
  local wt="$1" issue="$2" sig="$3" ceiling tip f prev_tip="" prev_sig="" prev_n=0
  [ -n "$sig" ] || return 1
  ceiling="${AFK_REANSWER_CEILING:-2}"
  case "$ceiling" in '' | *[!0-9]*) ceiling=2 ;; esac
  [ "$ceiling" -lt 1 ] && ceiling=1   # floor at 1: a 0 ceiling would strand every gate unanswered
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)"
  f="$(_reanswer_state_file "$issue")"
  if [ -f "$f" ]; then
    IFS=$'\t' read -r prev_tip prev_sig prev_n < "$f" 2>/dev/null || true
    case "$prev_n" in '' | *[!0-9]*) prev_n=0 ;; esac
  fi
  if [ "$prev_tip" != "$tip" ] || [ "$prev_sig" != "$sig" ]; then prev_n=0; fi   # new context
  [ "$prev_n" -ge "$ceiling" ] && return 0                                       # exhausted
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\t%s\n' "$tip" "$sig" "$(( prev_n + 1 ))" > "$f" 2>/dev/null || true
  return 1
}

# --- terminal gate markers (issue #237) ---------------------------------------
# A reasoner mutation-void is terminal on the FIRST occurrence: the reasoner wrote the
# spoke's live tree, so a human is required regardless of the parked prompt or branch tip.
# Unlike the (tip, sig) re-answer ceiling — which the mutation itself perturbs, since the
# write moves the tip and flips the pending command, resetting that counter every tick — this
# marker is durable and independent of both, so a voided gate never re-runs the reasoner.
# Cleared only on a fresh arm (_clear_progress_state), a current-window view.
_broker_voided_marker() { printf '%s\n' "$(_afk_state_dir)/gate-voided-$1"; }
_broker_gate_voided()   { [ -f "$(_broker_voided_marker "$1")" ]; }
_broker_mark_voided() {
  local issue="$1" f; f="$(_broker_voided_marker "$issue")"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$f" 2>/dev/null || true
}

# _broker_log_terminal_once <issue> <key> <msg> -> log <msg> only the FIRST tick a gate
# becomes terminal for <key>; a later tick on the same key stays silent, so a terminal gate
# never re-emits its "terminal" line on every event wake (issue #237). <key> folds in whatever
# the terminal state keys on (tip + signature for the re-answer ceiling), so a genuinely NEW
# terminal context (a moved tip / changed prompt) logs afresh.
_broker_terminal_log_file() { printf '%s\n' "$(_afk_state_dir)/terminal-logged-$1"; }
_broker_log_terminal_once() {
  local issue="$1" key="$2" msg="$3" f prev=""
  f="$(_broker_terminal_log_file "$issue")"
  [ -f "$f" ] && prev="$(cat "$f" 2>/dev/null)"
  [ "$prev" = "$key" ] && return 0
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\n' "$key" > "$f" 2>/dev/null || true
  log "$msg"
}

# _afk_note_tip_progress <wt> <issue> -> observe ledger progress as branch-tip
# advance: the first sighting records the tip WITHOUT stamping; a differing tip on a
# later tick stamps progress and re-records. Best-effort; never aborts the caller.
_afk_note_tip_progress() {
  local wt="$1" issue="$2" tip dir f last
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 0
  [ -n "$tip" ] || return 0
  dir="$(_afk_state_dir)"; f="$dir/tip-$issue"
  last="$( [ -f "$f" ] && cat "$f" 2>/dev/null )"
  if [ -z "$last" ]; then
    mkdir -p "$dir" 2>/dev/null || true
    printf '%s\n' "$tip" > "$f" 2>/dev/null || true
  elif [ "$last" != "$tip" ]; then
    printf '%s\n' "$tip" > "$f" 2>/dev/null || true
    stamp_progress_epoch "$issue"
  fi
  return 0
}

# _afk_ceiling_epoch <issue> -> the wall-clock ceiling's reference epoch:
# max(dispatch, progress). Empty when neither exists (spoke_over_ceiling reads that
# as "can't measure → never reap").
_afk_ceiling_epoch() {
  local issue="$1" d p
  d="$(read_dispatch_epoch "$issue")"
  p="$(read_progress_epoch "$issue")"
  case "$d" in '' | *[!0-9]*) d=0 ;; esac
  case "$p" in '' | *[!0-9]*) p=0 ;; esac
  [ "$p" -gt "$d" ] && d="$p"
  [ "$d" -gt 0 ] && printf '%s\n' "$d"
  return 0
}

# _spoke_over_any_ceiling <issue> <now> -> the reaper's full ceiling test. Progress
# DEFERS the soft ceiling (a revived / committing spoke gets fresh AFK_SPOKE_MAX_MINUTES
# from its last progress), but the dispatch epoch keeps an ABSOLUTE backstop at
# AFK_SPOKE_HARD_CEILING_MULT (default 3) x AFK_SPOKE_MAX_MINUTES — without it a
# doom-loop that commits every <180m would evade the reaper for the whole drain
# window, the exact outcome the reaper exists to prevent (ST3 review).
_spoke_over_any_ceiling() {
  local issue="$1" now="$2" d mult
  spoke_over_ceiling "$(_afk_ceiling_epoch "$issue")" "$now" && return 0
  d="$(read_dispatch_epoch "$issue")"
  case "$d" in '' | *[!0-9]*) return 1 ;; esac
  case "$now" in '' | *[!0-9]*) return 1 ;; esac
  mult="${AFK_SPOKE_HARD_CEILING_MULT:-3}"
  case "$mult" in '' | *[!0-9]*) mult=3 ;; esac
  [ "$(( (now - d) / 60 ))" -gt "$(( AFK_SPOKE_MAX_MINUTES * mult ))" ]
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
# _task_output_mtime <wt_path> -> newest mtime among the harness background-task output
# files for this worktree's sessions, or empty when none exist. A spoke waiting on a
# background workflow (a code-review) writes nothing to its transcript (#180), so the
# reaper's idle clock reads a stale transcript mtime and kills it as hung. The harness
# streams each background task's stdout to <tmp>/claude-*/<munged-wt>/*/tasks/*.output as
# it runs — a fresh write there is the missing "still working" signal. AFK_TASKS_ROOT
# overrides the tmp root (tests; defaults to macOS /private/tmp).
_task_output_mtime() {
  local wt_path="$1" slug root newest="" mt f
  slug="$(printf '%s' "$wt_path" | sed 's/[^A-Za-z0-9]/-/g')"
  root="${AFK_TASKS_ROOT:-/private/tmp}"
  for f in "$root"/claude-*/"$slug"/*/tasks/*.output; do
    [ -f "$f" ] || continue        # no match: the glob stays literal, skipped here
    mt="$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null)"
    [ -n "$mt" ] || continue
    if [ -z "$newest" ] || [ "$mt" -gt "$newest" ]; then newest="$mt"; fi
  done
  [ -n "$newest" ] && printf '%s\n' "$newest"
}
# _spoke_idle_seconds <wt_path> <issue> -> idle seconds for the REAPER's clock: since the
# LATEST of three references — the transcript's last write, the supervisor's last
# answer-delivery attempt, and the newest background-task output write. Time with a
# buffered/undelivered answer is not idle (#133; the reaper killed #125 right as its
# answer was delivered); neither is a spoke waiting on a background workflow that writes
# nothing to its transcript (#180; the reaper killed a healthy #168 mid code-review).
# These signals EXTEND the idle reference only — the wall-clock ceiling (#133) is checked
# separately and stays untouched. Empty when no reference exists (same "can't measure"
# contract as _transcript_idle_seconds).
_spoke_idle_seconds() {
  local wt="$1" issue="$2" ref attempt task
  ref="$(_transcript_mtime "$wt")"
  attempt="$(read_answer_attempt "$issue")"
  case "$attempt" in
    '' | *[!0-9]*) : ;;
    *) if [ -z "$ref" ] || [ "$attempt" -gt "$ref" ]; then ref="$attempt"; fi ;;
  esac
  # A task-output write only EXTENDS an existing reference — it never creates
  # measurability on its own. tmp is not cleared between runs, so a lingering .output from
  # a prior incarnation at a reused worktree path would otherwise drag a transcript-less
  # fresh spoke out of the "can't measure -> busy" guard and into a bogus idle reap off a
  # stale mtime (#180 review). Unlike the answer-attempt epoch (cleared per window), the
  # task-output signal can be stale, so it must not stand alone.
  task="$(_task_output_mtime "$wt")"
  case "$task" in
    '' | *[!0-9]*) : ;;
    *) if [ -n "$ref" ] && [ "$task" -gt "$ref" ]; then ref="$task"; fi ;;
  esac
  [ -n "$ref" ] || return 0
  printf '%s\n' "$(( $(afk_now) - ref ))"
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

# _is_seed_replay <wt_path> <text> -> true when <text> substantially replays the
# spoke's SEED prompt (the first user message in its transcript): normalized-whitespace,
# case-folded containment of the answer's first 200 chars in the seed, or of the whole
# seed in the answer. Short answers (< AFK_SEED_REPLAY_MIN_CHARS, default 80) are
# exempt — option labels legitimately appear inside a long kickoff. #124: the answerer
# echoed the kickoff back into a parked spoke six ticks in a row; a replay is never
# injected. Unreadable transcript / no python ⇒ not a replay (fail-open to answering).
_is_seed_replay() {
  local wt="$1" text="$2" jsonl
  jsonl="$(_spoke_jsonl "$wt")"
  [ -n "$jsonl" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_JSONL="$jsonl" _AFK_TEXT="$text" python3 2>/dev/null <<'PYEOF'
import json, os, re, sys

def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()

seed = ""
try:
    with open(os.environ["_AFK_JSONL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "user":
                continue
            content = (obj.get("message") or {}).get("content") or []
            if isinstance(content, str) and content.strip():
                seed = content
                break
            if isinstance(content, list):
                texts = [b.get("text") or "" for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                if any(t.strip() for t in texts):
                    seed = "\n".join(texts)
                    break
except Exception:
    sys.exit(1)

ans = norm(os.environ.get("_AFK_TEXT", ""))
seed = norm(seed)
try:
    floor = int(os.environ.get("AFK_SEED_REPLAY_MIN_CHARS", "80"))
except ValueError:
    floor = 80
replay = bool(seed) and len(ans) >= floor and (ans[:200] in seed or seed in ans)
sys.exit(0 if replay else 1)
PYEOF
}

# --- slot state ---------------------------------------------------------------
# slot_state <wt_path> <issue> -> done|waiting|reap|busy.
#   done    — a TERMINAL marker (ready/accept/blocked) at the branch tip.
#   waiting — parked on a question / gate (auto-answer it; never reaped).
#   reap    — over the wall-clock ceiling, or idle past AFK_IDLE_MINUTES with no marker.
#   busy    — actively working (or just spawned, no transcript yet).
slot_state() {
  local wt_path="$1" issue="$2" tip marker kind age
  tip="$(git -C "$wt_path" rev-parse HEAD 2>/dev/null)"
  if [ -n "$tip" ]; then
    for kind in ready accept; do
      marker="$(git -C "$wt_path" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)"
      [ "$marker" = "$tip" ] && { printf 'done\n'; return; }
    done
    # blocked/<issue> at the tip is terminal ONLY if the spoke is not still parked. A
    # spurious blocked/<N> (a false escalation) over a spoke still on a question / permission
    # dialog would otherwise strand it — read as done, never re-answered, never reaped until
    # the window ends (#171-subtask-3). If it is still parked on an extractable prompt, read
    # it as waiting (re-answerable); reconcile_markers keeps clearing the tag once commits
    # land on top.
    if [ "$(git -C "$wt_path" rev-parse -q --verify "refs/tags/blocked/${issue}^{commit}" 2>/dev/null)" = "$tip" ]; then
      if [ -n "$(extract_pending_question "$wt_path")" ] || _permission_pending "$wt_path"; then
        printf 'waiting\n'; return
      fi
      printf 'done\n'; return
    fi
    # A pushed gate/<issue> at the tip = parked at the PLAN gate → waiting, never reaped.
    # The gate is a prose plan + this tag (no AskUserQuestion), so extract_pending_question
    # can't see it. Checking at the tip is self-clearing: once approved and the spoke
    # commits its first RED/GREEN, the tip moves past the gate commit and it reads busy.
    if [ "$(git -C "$wt_path" rev-parse -q --verify "refs/tags/gate/${issue}^{commit}" 2>/dev/null)" = "$tip" ]; then
      printf 'waiting\n'; return
    fi
  fi
  # Ledger progress (a tip advance since the last tick) refreshes the ceiling before
  # it is measured — a revived spoke is not re-reaped off its stale dispatch epoch.
  _afk_note_tip_progress "$wt_path" "$issue"
  if _spoke_over_any_ceiling "$issue" "$(afk_now)"; then printf 'reap\n'; return; fi
  if [ -n "$(extract_pending_question "$wt_path")" ]; then printf 'waiting\n'; return; fi
  # A pending permission dialog (a CC confirmation prompt, no transcript entry) is decided by
  # the supervisor's classifier, so it waits — never reaped as idle (#149).
  if _permission_pending "$wt_path"; then printf 'waiting\n'; return; fi
  age="$(_spoke_idle_seconds "$wt_path" "$issue")"
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

# _gate_parked <wt> <issue> -> true when a gate/<issue> tag sits AT the branch tip:
# the spoke is parked at its PLAN gate. The same check slot_state does inline; here
# for the answerer's gate routing and its pre-inject re-check (#133).
_gate_parked() {
  local wt="$1" issue="$2" tip
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  [ -n "$tip" ] || return 1
  [ "$(git -C "$wt" rev-parse -q --verify "refs/tags/gate/${issue}^{commit}" 2>/dev/null)" = "$tip" ]
}

# _gate_answer_landed <wt> -> rc 0 when the spoke's transcript shows a GENUINE human/hub
# reply — a TYPED prompt submission (promptSource == "typed"): a human typing in the pane,
# or the broker's own tmux inject — AFTER the assistant turn that ran `spoke-ready.sh
# --gate`, i.e. the PLAN-gate approval reply already landed. Used to self-heal a STALE gate
# tag (issue #204): _consume_gate_tag ran only on the broker's confirmed-inject path, so an
# answer that registered late, a wedge respawn started OUTSIDE the broker, or ANY
# attended/manual reply in the pane left gate/<N> at the tip — re-read as "waiting" and
# re-answered, and (with the #204 guard) wedging the resumed spoke. Every synthetic user
# turn the harness injects (tool_results, <task-notification>/<system-reminder>, skill/meta
# turns, SDK/system prompts) carries a non-"typed" promptSource (or none), so it can NOT
# false-consume the tag on a spoke still awaiting its first approval. A (re-)park supersedes
# an earlier approval. Fail-CLOSED (rc 1): no transcript, no python3, or no typed post-park
# turn means "cannot prove a reply landed" → the broker services the gate as before. The
# plan-gate-guard's approval_in_transcript mirrors this so both sides read the same signal.
_gate_answer_landed() {
  local wt="$1" jsonl
  jsonl="$(_spoke_jsonl "$wt")"
  [ -n "$jsonl" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json, os, sys

parked = False
approved = False
try:
    with open(os.environ["_AFK_JSONL"], encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            ttype = obj.get("type")
            content = (obj.get("message") or {}).get("content")
            if ttype == "assistant":
                for block in content if isinstance(content, list) else []:
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("name") == "Bash"
                            and "spoke-ready.sh --gate" in ((block.get("input") or {}).get("command") or "")):
                        parked = True       # a (re-)park supersedes any earlier approval
                        approved = False
            elif ttype == "user" and parked:
                # ONLY a typed prompt submission is a genuine reply — harness-injected user
                # turns (tool_results, notifications, skill/meta, SDK/system) are not.
                if obj.get("promptSource") == "typed" and not obj.get("isMeta"):
                    approved = True
except Exception:
    sys.exit(1)
sys.exit(0 if approved else 1)
PYEOF
}

# _gate_artifact_path <wt> <issue> -> the gate plan artifact path (<wt>/.ai-toolkit/
# gate-<issue>.md). The single owner of that layout, shared by _read_gate_artifact and
# _consume_gate_tag (spoke-ready.sh writes the same path from the spoke side, #175). Falls
# back to <wt> as the root when rev-parse can't resolve a toplevel (a non-git path in a test).
_gate_artifact_path() {
  local wt="$1" issue="$2" root
  root="$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$wt")"
  printf '%s\n' "$root/.ai-toolkit/gate-$issue.md"
}

# _read_gate_artifact <wt> <issue> -> the plan the spoke wrote to its gate artifact
# (<wt>/.ai-toolkit/gate-<issue>.md, written by spoke-ready.sh --gate, issue #175), or empty
# when absent. The SCRIPTED handoff channel the gate route PREFERS over parsing the spoke
# transcript (extract_pending_question): a script reads what a script wrote, no heuristic.
# Empty (fall back to the transcript) when the spoke parked without writing one (a bare --gate).
_read_gate_artifact() {
  local wt="$1" issue="$2" f
  f="$(_gate_artifact_path "$wt" "$issue")"
  [ -f "$f" ] || return 0
  # Cap at 4000 CHARACTERS (matching extract_pending_question's out[:4000]) so a huge plan
  # can't blow up the answerer prompt AND a multibyte plan is never split mid-character —
  # head -c would cut on bytes. python3 is the broker's existing text tool (the
  # extract_pending_question path); when it is unavailable the untruncated plan
  # (spoke-authored, bounded in practice) is safer than a byte-truncated one.
  if command -v python3 >/dev/null 2>&1; then
    _AFK_GATE_FILE="$f" python3 -c \
      'import os,sys; sys.stdout.write(open(os.environ["_AFK_GATE_FILE"], encoding="utf-8", errors="replace").read()[:4000])' \
      2>/dev/null
  else
    cat "$f" 2>/dev/null
  fi
}

# _still_parked_same <wt> <issue> <was_gate> <question> <before_mtime> -> true when the
# spoke is still parked on the SAME prompt the answerer reasoned about. The answerer
# takes minutes; a spoke that moved on meanwhile (a human replied, the turn resumed)
# must not receive the stale answer mid-turn (#129/#89), and a spoke now parked on a
# DIFFERENT question needs a fresh answer, not this one. Three signals, ALL required:
#   - the transcript has not moved since the answerer started (<before_mtime>) — any
#     write means activity, and a gate tag alone can't be trusted: it stays at the tip
#     until the FIRST COMMIT, so a spoke that self-approved and kept coding (#117), or
#     a human approving in-pane, still reads "parked" by the tag;
#   - for a gate park, the gate/<issue> tag is still at the tip;
#   - the extraction is unchanged (catches a same-second write mtime can't see; for an
#     unextractable gate park this is the vacuous "" = "").
_still_parked_same() {
  local wt="$1" issue="$2" was_gate="$3" question="$4" before="$5"
  [ "$(_transcript_mtime "$wt")" = "$before" ] || return 1
  if [ "$was_gate" -eq 1 ]; then
    _gate_parked "$wt" "$issue" || return 1
  fi
  [ "$(extract_pending_question "$wt")" = "$question" ]
}

# _spoke_moved_on <wt> <before_mtime> -> true ONLY when the spoke's transcript has a NEW
# write since <before_mtime>: a positive, confident signal that it is actively working. The
# escalation freshness-gate (#171-subtask-2) uses this rather than !_still_parked_same so it
# fails SAFE: an unreadable clock (empty / non-numeric mtime) or a non-numeric baseline reads
# as "cannot confirm movement" → NOT moved on → the escalation is still stamped. Dropping an
# escalation is only warranted on demonstrated activity, never on an ambiguous probe (review).
_spoke_moved_on() {
  local wt="$1" before="$2" now
  now="$(_transcript_mtime "$wt")"
  case "$now" in '' | *[!0-9]* ) return 1 ;; esac
  case "$before" in '' | *[!0-9]* ) return 1 ;; esac
  [ "$now" -gt "$before" ]
}

# --- the answerer (the one reasoning step) ------------------------------------

# --- read-only worktree reasoner (issue #155, subtask B) ----------------------
# The gate reasoner gets READ-ONLY access to the spoke's LIVE worktree (cwd) so it can
# verify a decision against real state — uncommitted/staged included — before auto-
# answering: evidence, not a pattern-guess. Two enforcement layers:
#   1. PREVENTION — run with a read-only tool allowlist (the code-review/Explore
#      posture: Read/Grep/Glob + a narrow read-only git helper; never Edit/Write).
#   2. DETECTION — a content fingerprint of the worktree taken before and after the
#      reason step; ANY change is a read-only BREACH, so the answer is voided and the
#      gate routes to a human. Detection is the HARD guarantee: it does not depend on
#      the LLM honoring the allowlist.

# reasoner_allowed_tools -> the read-only allowlist passed to the headless reasoner
# (comma-joined for `claude --allowedTools`). Read/Grep/Glob plus narrow read-only git
# verbs via scoped Bash patterns — enough to inspect the tree and run status/diff to
# verify a plan, nothing that can mutate it. AFK_REASONER_TOOLS overrides.
# UPGRADE: confirm the exact `claude --allowedTools` list/pattern syntax against the
# installed CLI version if the reasoner ever reports a read tool it should have.
reasoner_allowed_tools() {
  printf '%s\n' "${AFK_REASONER_TOOLS:-Read,Grep,Glob,Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git rev-parse:*)}"
}

# _reasoner_bash_readonly <inner> -> rc 0 when a scoped Bash allow pattern's inner
# command is a read-only git verb (git status/diff/log/show/rev-parse/branch/ls-files/
# cat-file), rc 1 otherwise. Keeps a `Bash(...)` allow from smuggling a mutating verb
# (git push/commit/reset, rm, chmod, …) past assert_readonly_tools.
_reasoner_bash_readonly() {
  case "$1" in
    'git status'* | 'git diff'* | 'git log'* | 'git show'* | 'git rev-parse'* \
      | 'git branch'* | 'git ls-files'* | 'git cat-file'*) return 0 ;;
    *) return 1 ;;
  esac
}

# assert_readonly_tools <comma-list> -> rc 0 when every tool is read-only, rc 1 when any
# is a mutating tool (Edit/Write/MultiEdit/NotebookEdit), a bare unrestricted Bash, or a
# scoped Bash(...) whose inner verb is NOT a read-only git verb. Anything unrecognised is
# denied (default-deny). Parses by hand (no word-splitting) so a glob in a Bash(...)
# pattern never expands.
assert_readonly_tools() {
  local rest="$1" tok inner
  while [ -n "$rest" ]; do
    tok="${rest%%,*}"
    if [ "$tok" = "$rest" ]; then rest=""; else rest="${rest#*,}"; fi
    tok="${tok#"${tok%%[![:space:]]*}"}"; tok="${tok%"${tok##*[![:space:]]}"}"   # trim
    [ -n "$tok" ] || continue
    case "$tok" in
      Read | Grep | Glob | LS | WebFetch | WebSearch | TodoRead) ;;
      'Bash('*')')                                     # a scoped Bash verb: vet it
        inner="${tok#Bash(}"; inner="${inner%)}"
        _reasoner_bash_readonly "$inner" || return 1 ;;
      *) return 1 ;;                                    # mutating / bare Bash / unknown -> deny
    esac
  done
  return 0
}

# _broker_worktree_fingerprint <wt> -> a content hash of the LIVE worktree's TRACKED content
# PLUS its untracked-not-ignored files: each path + its CURRENT working-tree content. A
# tracked edit, a staged addition, a deletion, OR a brand-new untracked-not-ignored file all
# change it. IGNORED files stay excluded on purpose (issue #168): a parked spoke is not a
# frozen worktree — its own still-finishing push gate writes `.testmondata`, OTel dumps land
# under `.ai-toolkit/`, etc. Those runtime artifacts are git-ignored, so they must not be
# blamed on the read-only reasoner. `--others --exclude-standard` (issue #203) closes the
# creation gap #168 opened — a reasoner that CREATES a new untracked file used to be invisible
# here, mutating the tree unprevented AND undetected — while keeping the #168 ignored-artifact
# class safe (the exclude honors .gitignore, .git/info/exclude, AND the global excludesFile).
# `sort -zu` makes the combined listing order-stable. THIS WORKTREE'S HEAD is folded in too
# (issue #239): `git rev-parse HEAD` so a reasoner ref write that moves HEAD (`git commit` /
# `update-ref` of the checked-out branch) — which the index/working-tree content scan can never
# see — still changes the fingerprint, backstopping the snapshot isolation should it ever
# regress. Deliberately NOT `git for-each-ref`: on a linked worktree that lists the SHARED refs,
# so ordinary concurrent /afk-drain activity (a sibling spoke's push, a hub auto-land advancing
# main, a background fetch) would flip the fingerprint and terminally FALSE-void a correct
# answer — the concurrent-sibling false-BREACH class this repo already fights. HEAD reflects only
# THIS worktree's own branch tip, immune to sibling ref churn. UPGRADE: to also catch a ref
# write that does NOT move HEAD (a stray tag / non-checked-out branch), fingerprint the
# worktree's own per-worktree refs specifically — never the shared ref namespace.
# Empty (stable) for a non-git or missing path, so a non-worktree reasoner never trips a
# false breach.
_broker_worktree_fingerprint() {
  local wt="$1"
  [ -d "$wt" ] || return 0
  (
    cd "$wt" 2>/dev/null || exit 0
    git rev-parse --git-dir >/dev/null 2>&1 || exit 0
    {
      git ls-files -z --cached --others --exclude-standard 2>/dev/null | sort -zu |
        while IFS= read -r -d '' f; do
          printf '%s\0' "$f"
          if [ -f "$f" ]; then git hash-object "$f" 2>/dev/null || printf 'ERR'; else printf 'GONE'; fi
          printf '\0'
        done
      printf 'HEAD\0'; git rev-parse -q --verify HEAD 2>/dev/null || printf 'NONE'; printf '\0'
    } |
      shasum -a 256 2>/dev/null | awk '{print $1}'
  )
}

# _broker_worktree_unchanged <wt> <before_fingerprint> -> rc 0 when the worktree is
# byte-for-byte what it was at <before_fingerprint>, rc 1 when the reasoner mutated it.
_broker_worktree_unchanged() {
  local wt="$1" before="$2" after
  after="$(_broker_worktree_fingerprint "$wt")"
  [ "$before" = "$after" ]
}

# _broker_is_git_worktree <wt> -> rc 0 when <wt> is a real git worktree (so a NON-empty
# fingerprint is expected). Used to fail SAFE: an empty fingerprint for a git worktree
# means the fingerprint tooling (shasum/git) is missing and the read-only guard can't
# verify — which must escalate, not silently pass.
_broker_is_git_worktree() {
  [ -d "$1" ] && git -C "$1" rev-parse --git-dir >/dev/null 2>&1
}

# _broker_snapshot_worktree <wt> <dest> -> populate <dest> with a throwaway COPY of <wt>'s
# content so the reasoner can run there (cwd=<dest>) instead of the spoke's LIVE tree — real
# write isolation (#237), the "verify agent worktree isolation" prior art: even a tool that
# ignores the read-only allowlist writes into the copy, never the spoke's tree. rc 0 on a
# populated copy, rc 1 when <wt> is not a git worktree (the caller then runs in-place and the
# fingerprint void still guards). The copy carries ONLY the tracked + untracked-not-ignored
# set (the SAME set _broker_worktree_fingerprint measures) plus the .git linkage, so a per-tick
# copy never recurses the ignored heavy trees (.venv, .testmondata*, .ai-toolkit/ OTel dumps).
# `cp -R` preserves the worktree's uncommitted + untracked state — fidelity `git worktree add`
# (committed-HEAD only) can't give — so the reasoner's read git verbs still reflect real state.
# LINKED-WORKTREE GITDIR ISOLATION (#239): a spoke is always a LINKED worktree, whose `.git` is
# a gitfile still pointing at the SHARED common gitdir. Copying that pointer verbatim (`cp -R`)
# leaves git WRITE-verbs in the copy (a tool that ignores the read-only allowlist) resolving to
# the real shared refs — `git commit`/`update-ref` in the copy moved the live HEAD/branch tip
# and the content-only fingerprint never saw it. So for the gitfile case we give the copy a
# PRIVATE, self-contained gitdir (_broker_private_gitdir): the object store is shared READ-ONLY
# via `objects/info/alternates` (no per-tick object copy), while refs/HEAD/index are copied so
# read verbs still reflect real state AND every write lands in the copy's own gitdir. The
# main-checkout `.git`-DIRECTORY fast path stays `cp -R` — a self-contained dir is already
# isolated wholesale.
_broker_snapshot_worktree() {
  local wt="$1" dest="$2" f
  _broker_is_git_worktree "$wt" || return 1
  # Provide the git linkage first so read-only git verbs resolve, then the exact fingerprint
  # set — never the ignored heavy trees. A `.git` DIRECTORY copies wholesale (already isolated);
  # a linked-worktree GITFILE gets a private gitdir so writes can't reach the shared common dir.
  if [ -d "$wt/.git" ]; then
    cp -R "$wt/.git" "$dest/.git" 2>/dev/null
  elif [ -f "$wt/.git" ]; then
    # Best-effort (like the old `cp -R … 2>/dev/null`): even a partial/failed private gitdir is
    # still a PRIVATE $dest/.git — never a gitfile pointing at the shared common dir — so keeping
    # the copy preserves write isolation. A hard `return 1` here would make run_answerer fall
    # back to running the reasoner in the LIVE tree, silently dropping the very isolation this
    # provides; the reasoner's git reads just degrade if the private gitdir is incomplete.
    _broker_private_gitdir "$wt" "$dest" || true
  fi
  (
    cd "$wt" 2>/dev/null || exit 0
    git ls-files -z --cached --others --exclude-standard 2>/dev/null |
      while IFS= read -r -d '' f; do
        [ -f "$f" ] || continue
        mkdir -p "$dest/$(dirname "$f")" 2>/dev/null || true
        cp -p "$f" "$dest/$f" 2>/dev/null || true
      done
  )
  return 0
}

# _broker_private_gitdir <wt> <dest> -> build a PRIVATE, self-contained gitdir at <dest>/.git
# for a LINKED worktree <wt> (whose own `.git` is a gitfile at the shared common gitdir), so a
# git write-verb in the copy writes ONLY here — never the shared refs (#239). Objects are shared
# READ-ONLY via alternates (cheap: no per-tick copy of the object store); the shared refs +
# packed-refs are copied so read verbs reflect real state and a ref write lands locally; HEAD +
# index come from the per-worktree gitdir so `git status`/`diff` reflect the spoke's real
# uncommitted state; the real common config is copied (with worktree-specific bits neutralized)
# so any `[extensions]` carry over. rc 1 on a failure the caller treats as best-effort — a
# partial $dest/.git is still private, so write isolation holds either way.
# UPGRADE: the ref copy assumes the `files` ref backend; a `reftable`-backend repo keeps refs in
# a `reftable/` dir, not `refs/` + `packed-refs`, and would need that copied instead.
_broker_private_gitdir() {
  local wt="$1" dest="$2" common gitdir
  common="$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null)" || return 1
  gitdir="$(git -C "$wt" rev-parse --absolute-git-dir 2>/dev/null)" || return 1
  [ -n "$common" ] && [ -n "$gitdir" ] || return 1
  case "$common" in /*) ;; *) common="$wt/$common" ;; esac   # resolve a relative common dir
  mkdir -p "$dest/.git/objects/info" "$dest/.git/refs" || return 1
  printf '%s\n' "$common/objects" > "$dest/.git/objects/info/alternates"
  cp -R "$common/refs/." "$dest/.git/refs/" 2>/dev/null || true
  [ -f "$common/packed-refs" ] && cp "$common/packed-refs" "$dest/.git/packed-refs" 2>/dev/null
  cp "$gitdir/HEAD" "$dest/.git/HEAD" 2>/dev/null || return 1
  [ -f "$gitdir/index" ] && cp "$gitdir/index" "$dest/.git/index" 2>/dev/null
  # Copy the REAL common config (not a hardcoded version-0 stub) so any `[extensions]` the shared
  # repo needs — objectformat=sha256, etc. — carry over and the shared objects still parse; then
  # neutralize the worktree-specific bits so the copy is a plain non-bare worktree rooted at $dest.
  if [ -f "$common/config" ]; then
    cp "$common/config" "$dest/.git/config" 2>/dev/null
  else
    printf '[core]\n\tbare = false\n' > "$dest/.git/config"
  fi
  git -C "$dest" config core.bare false 2>/dev/null || true
  git -C "$dest" config --unset core.worktree 2>/dev/null || true
  return 0
}

# read_decisions_digest <issue> -> a compact digest of THIS spoke's prior gate outcomes,
# seeded into the reasoner for cross-gate consistency (NOT the old transcript, which
# replayed the seed in #124). Reads the automatable-decisions log (subtask D's writer),
# filtered to this issue; empty when the log is absent. Shared line format (with #155
# subtask D): <ts>\t<issue>\t<gate_type>\t<signature>\t<decision>.
read_decisions_digest() {
  local issue="$1" log
  log="$(_afk_state_dir)/decisions.log"
  [ -f "$log" ] || return 0
  awk -F'\t' -v issue="$issue" '$2 == issue { printf "- %s: %s (%s)\n", $3, $5, $4 }' "$log" 2>/dev/null || true
}

# --- automatable-decisions log + codification (issue #155, subtask D) ----------
# Every automatable PERMISSION decision (the mechanical classify_permission verdict — the
# codifiable class; a reasoner ANSWER is free text and a plan gate is a judgment call, so
# neither is logged) is recorded with a normalized SIGNATURE so recurrences of the same
# command shape collide; an on-demand codification pass then proposes deterministic rules
# for signatures that recur unanimously — graduating common gates out of the LLM in BOTH
# modes (the "scripted control plane, not LLM" payoff, generalizing #149's git-reset
# self-stage rule into a learning pipeline). Proposal-only: a human reviews before any
# rule is appended to the classifier table.

# _normalize_command_shape <command> -> the command's verb skeleton: each ;/&&/||/|
# segment reduced to "<verb>-<subcommand>" (flags/args/paths dropped), joined by '+'. So
# `git reset -q; git add tests/x.py` and `git reset HEAD; git add a.py` both normalize to
# `git-reset+git-add`. Parses tokens by hand (no word-splitting) so a glob never expands.
_normalize_command_shape() {
  local cmd="$1" norm seg out="" verb rest sub part
  # Split on the same operators classify_permission does (&& and || before the single &
  # and | so they are not pre-split); the single & must split too, or `git status & rm`
  # would sign as only `git-status`.
  norm="${cmd//&&/$'\n'}"; norm="${norm//||/$'\n'}"
  norm="${norm//&/$'\n'}"; norm="${norm//|/$'\n'}"; norm="${norm//;/$'\n'}"
  while IFS= read -r seg; do
    seg="${seg#"${seg%%[![:space:]]*}"}"; seg="${seg%"${seg##*[![:space:]]}"}"
    [ -n "$seg" ] || continue
    verb="${seg%% *}"
    rest="${seg#"$verb"}"; rest="${rest#"${rest%%[![:space:]]*}"}"
    sub="${rest%% *}"
    case "$sub" in '' | -*) sub="" ;; esac        # a flag / nothing isn't a subcommand
    part="$verb"; [ -n "$sub" ] && part="$verb-$sub"
    out="${out:+$out+}$part"
  done <<<"$norm"
  printf '%s\n' "$out"
}

# _broker_decision_signature <gate_type> <shape> -> a stable signature for the decision.
# A permission gate's shape is its command (normalized to the verb skeleton); other gate
# types sign as the gate type itself (a plan gate is a judgment call, not codifiable).
_broker_decision_signature() {
  local gate_type="$1" shape="$2"
  case "$gate_type" in
    permission) _normalize_command_shape "$shape" ;;
    *) printf '%s\n' "$gate_type" ;;
  esac
}

# log_decision <issue> <gate_type> <shape> <decision> -> append one automatable-decisions
# record: <ts>\t<issue>\t<gate_type>\t<signature>\t<decision>. Exactly the format
# read_decisions_digest (subtask B) consumes. Best-effort; never aborts the caller.
log_decision() {
  local issue="$1" gate_type="$2" shape="$3" decision="$4" sig log
  sig="$(_broker_decision_signature "$gate_type" "$shape")"
  log="$(_afk_state_dir)/decisions.log"
  mkdir -p "$(dirname "$log")" 2>/dev/null || true
  printf '%s\t%s\t%s\t%s\t%s\n' "$(afk_now)" "$issue" "$gate_type" "$sig" "$decision" \
    >>"$log" 2>/dev/null || true
}

# codify_decisions [min_count] -> propose a deterministic rule for every signature that
# recurs at least <min_count> times (default 2) with a UNANIMOUS decision. Output is a
# PROPOSAL a human reviews before it is codified into classify_permission — never an
# auto-applied rule. A single-occurrence or a conflicting signature proposes nothing. The
# signature drops flags/args, so the proposal carries a "verify destructive flag variants"
# caveat: the human must confirm the shape is safe across the flags classify_permission
# distinguishes before codifying. Malformed lines (missing signature/decision) are skipped.
codify_decisions() {
  local min="${1:-2}" log
  log="$(_afk_state_dir)/decisions.log"
  [ -f "$log" ] || return 0
  awk -F'\t' -v min="$min" '
    $4 != "" && $5 != "" {
      sig=$4; dec=$5; count[sig]++
      if (!(sig in decision)) decision[sig]=dec
      else if (decision[sig]!=dec) conflict[sig]=1 }
    END {
      for (s in count)
        if (count[s] >= min && !(s in conflict))
          printf "RULE: %s -> %s (%d occurrences, unanimous; verify destructive flag variants)\n", s, decision[s], count[s]
    }' "$log" 2>/dev/null | sort || true
}

# --- decision journal + warn-and-continue (issue #241) ------------------------
# The /afk answerer ALWAYS answers: every former terminal stop site (escalate-blocked, reap,
# ceiling, void, inject-failure, dispatch/land/auth halts) now TAKES the best action, WARNS
# loudly to four surfaces (drain log + hub-notify ping + --status + this decision journal),
# and parks the spoke LAST on the warned-retry backoff — never abandoned. The journal is the
# post-adjust surface: the operator reads it in the morning and reverses whatever was wrong.

# _broker_journal_file -> the per-run decision journal (one JSON line per taken decision).
_broker_journal_file() { printf '%s\n' "$(_afk_state_dir)/decision-journal.jsonl"; }

# _broker_json_escape <s> -> escape a value for a JSON string literal. A decision/reason can
# be built from captured tool output (git/gh/build lines carry \r, \t, and other C0 controls),
# and JSON forbids raw control characters in a string — so escape \ and ", space out the
# common whitespace for readability, then DROP any remaining C0 byte so the journal line stays
# valid JSONL a strict parser accepts. LC_ALL=C makes the byte range literal on this non-C host.
_broker_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"   # backslashes first, else the quote-escapes below get doubled
  s="${s//\"/\\\"}"
  s="${s//$'\t'/ }"; s="${s//$'\n'/ }"; s="${s//$'\r'/ }"   # keep the record one physical line
  printf '%s' "$s" | LC_ALL=C tr -d '\000-\037'
}

# broker_journal_decision <issue> <park_kind> <decision> <reversibility> [reasoning_ref] ->
# append one structured JSONL record (ts, issue, park, decision, reversibility, reasoning_ref)
# AND post a best-effort GitHub issue comment, so the morning review reads either surface.
# reversibility is one of reversible|outward|scope|irreversible|unknown. Best-effort; never
# aborts the caller.
broker_journal_decision() {
  local issue="$1" park="$2" decision="$3" rev="${4:-unknown}" ref="${5:-}" f
  f="$(_broker_journal_file)"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '{"ts":%s,"issue":"%s","park":"%s","decision":"%s","reversibility":"%s","reasoning_ref":"%s"}\n' \
    "$(afk_now)" "$(_broker_json_escape "$issue")" "$(_broker_json_escape "$park")" \
    "$(_broker_json_escape "$decision")" "$(_broker_json_escape "$rev")" \
    "$(_broker_json_escape "$ref")" >>"$f" 2>/dev/null || true
  _broker_journal_gh_comment "$issue" "$park" "$decision" "$rev"
  return 0
}

# _broker_journal_gh_comment <issue> <park> <decision> <rev> -> best-effort issue comment
# recording the taken decision (#241 §10). Opt-out via AFK_JOURNAL_GH_COMMENT=0; no-op when
# gh is absent. Never aborts.
_broker_journal_gh_comment() {
  [ "${AFK_JOURNAL_GH_COMMENT:-1}" = 0 ] && return 0
  command -v gh >/dev/null 2>&1 || return 0
  local issue="$1" park="$2" decision="$3" rev="$4" body
  # Wrap the decision in backticks: a decision containing `#123` or `@name` would otherwise
  # render as a cross-issue link / user mention on GitHub, back-referencing unrelated issues.
  body="AFK auto-decision [$rev] on the $park park: \`$decision\` (review and post-adjust if wrong)"
  # Route through the TIME-BOUNDED runner so a hung gh (a black-hole network) can never
  # freeze the servicing tick — this is on the synchronous answer path. _wt_gh_run bounds
  # gh at AI_TOOLKIT_GH_TIMEOUT and returns its real rc (which we discard). Fall back to a
  # raw best-effort gh only when worktree-lib.sh did not source (the helper is undefined).
  if command -v _wt_gh_run >/dev/null 2>&1; then
    _wt_gh_run issue comment "$issue" --body "$body" || true
  else
    gh issue comment "$issue" --body "$body" >/dev/null 2>&1 || true
  fi
  return 0
}

# _broker_warned_record <issue> -> the durable, human-facing warned record: "<ts>\t<reason>".
# --status surfaces it and hub-notify pings on it (re-fired on an interval, unlike the
# once-deduped blocked ping). Distinct from blocked-<issue>.txt so the two states never blur.
_broker_warned_record() { printf '%s\n' "$(_afk_state_dir)/warned-$1.txt"; }

# broker_warn <issue> <reason> -> the loud, repeatable WARNING surface: log a WARNING line and
# overwrite the durable warned record (latest warning wins). Best-effort; never aborts.
broker_warn() {
  local issue="$1" reason="$2" f
  reason="${reason//$'\n'/ }"; reason="${reason//$'\r'/ }"   # keep the record one line (hub-notify cut -f2-)
  log "  WARNING: #$issue $reason"
  f="$(_broker_warned_record "$issue")"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\n' "$(afk_now)" "$reason" >"$f" 2>/dev/null || true
  return 0
}

# _afk_warned_state_file <issue> -> the backoff bookkeeping: "<attempt>\t<next_retry_epoch>".
_afk_warned_state_file() { printf '%s\n' "$(_afk_state_dir)/warned-state-$1"; }

# _afk_warned_arm <issue> -> advance the warned-retry backoff: read the prior attempt count
# (0 if none), schedule the next retry at now + min(BASE * 2^attempt, CAP), and persist
# "<attempt+1>\t<next>". Exponential so a standing failure is retried ever more rarely.
_afk_warned_arm() {
  local issue="$1" f base cap attempt=0 delay now i=0
  base="${AFK_WARN_BACKOFF_BASE:-60}"; case "$base" in '' | *[!0-9]*) base=60 ;; esac
  cap="${AFK_WARN_BACKOFF_CAP:-1800}"; case "$cap" in '' | *[!0-9]*) cap=1800 ;; esac
  f="$(_afk_warned_state_file "$issue")"
  if [ -f "$f" ]; then IFS=$'\t' read -r attempt _ <"$f" 2>/dev/null || true; fi
  case "$attempt" in '' | *[!0-9]*) attempt=0 ;; esac
  delay="$base"
  while [ "$i" -lt "$attempt" ] && [ "$delay" -lt "$cap" ]; do delay=$(( delay * 2 )); i=$(( i + 1 )); done
  [ "$delay" -gt "$cap" ] && delay="$cap"
  now="$(afk_now)"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\n' "$(( attempt + 1 ))" "$(( now + delay ))" >"$f" 2>/dev/null || true
}

# _afk_warned_due <issue> [now] -> rc 0 when the spoke is due for a retry (never warned, or the
# backoff window has elapsed), rc 1 when still inside the backoff (parked LAST this tick).
_afk_warned_due() {
  local issue="$1" now="${2:-$(afk_now)}" f next=""
  f="$(_afk_warned_state_file "$issue")"
  [ -f "$f" ] || return 0
  IFS=$'\t' read -r _ next <"$f" 2>/dev/null || true
  case "$next" in '' | *[!0-9]*) return 0 ;; esac
  [ "$now" -ge "$next" ]
}

# _afk_clear_warned <issue> -> drop one spoke's warned record + backoff (called on genuine
# progress: a tip advance or a fresh marker means the warned state is stale).
_afk_clear_warned() {
  rm -f "$(_afk_warned_state_file "$1")" "$(_broker_warned_record "$1")" 2>/dev/null || true
}
# _clear_warned_records -> drop every warned record + backoff for a freshly-armed window.
_clear_warned_records() {
  local dir; dir="$(_afk_state_dir)"
  rm -f "$dir"/warned-*.txt "$dir"/warned-state-* 2>/dev/null || true
}

# broker_warn_continue <wt> <issue> <park_kind> <decision> <reversibility> -> the #241
# replacement for _escalate_blocked at a converted stop site: warn loudly, journal the taken
# decision, advance the backoff, emit a warn span, and RETURN — the spoke stays in rotation
# (no blocked tag, no pane kill). It is retried on the backoff until it makes progress.
broker_warn_continue() {
  local wt="$1" issue="$2" park="$3" decision="$4" rev="${5:-unknown}"
  broker_warn "$issue" "$decision"
  broker_journal_decision "$issue" "$park" "$decision" "$rev"
  _afk_warned_arm "$issue"
  afk_emit_decision "$wt" warn
  return 0
}

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

# build_answerer_prompt <issue> <question> -> the full prompt for the reasoner: the
# governing rule, the issue contract, the read-only-worktree posture + evidence contract,
# a decisions-digest of this spoke's prior gate outcomes, and the parked prompt.
# Self-contained so the headless reasoner needs no project context loaded. The reasoner's
# cwd is the #237 snapshot COPY (created in run_answerer before this is called), so the
# posture points at "a throwaway copy (your cwd)" and — deliberately (#239) — never
# discloses the live worktree's absolute path, which used to invite an absolute-path write
# into the real tree.
# _default_answerer_policy -> the built-in fallback policy shipped when the afk-answering rule
# file is absent. #241: the reasoner ALWAYS answers — it never escalates-and-parks. It is kept
# in lockstep with shared/rules/afk-answering.md by a binding test, so both surfaces retire the
# ESCALATE output token and both instruct the ANSWER + REVERSIBILITY lines.
_default_answerer_policy() {
  cat <<'POLICY'
Answer in the interest of the issue contract and repo conventions; prefer the spoke's own
recommended option. You ALWAYS answer — you never escalate and park the spoke for a human.
For an irreversible, outward-facing, or scope-changing ask, choose the REVERSIBLE, in-scope
alternative when one exists (that IS the answer — e.g. do not force-push; rebase onto a new
branch instead; deny a destructive command and tell the spoke the reversible path); only when
no reversible alternative exists do you decide on the merits. Precede your decision with a
'REVERSIBILITY: reversible|outward|scope|irreversible' line naming the class, and add a
'WARN: <what the human should double-check>' line whenever you take a critical, irreversible,
outward-facing, or scope-changing decision so it is loudly recorded for morning post-review.
End with exactly one final line: 'ANSWER: <reply>'.
POLICY
}

build_answerer_prompt() {
  local issue="$1" question="$2" rule body digest
  rule="$(_rule_file)" && rule="$(cat "$rule")" \
    || rule="$(_default_answerer_policy)"
  body="$(gh issue view "$issue" --json title,body -q '.title + "\n\n" + .body' 2>/dev/null || echo "(issue #$issue body unavailable)")"
  digest="$(read_decisions_digest "$issue")"
  cat <<EOF
$rule

## Issue contract (#$issue)

$body

## Read-only worktree access

You have READ-ONLY access to a throwaway COPY of the spoke's worktree (your cwd). Use your
read/search tools to verify the decision against the code as it ACTUALLY is — confirm a command
touches only the spoke's own files, that a posted plan matches real state, and so on.
You must NOT edit, stage, commit, or push anything: the tree is read-only and any write
voids your answer. When you auto-answer, cite the worktree EVIDENCE you checked on an
'EVIDENCE:' line before your final decision line.

## Prior gate decisions for this spoke (decisions-digest)

${digest:-(none recorded yet)}

## The spoke's parked prompt

$question

Decide per the policy above — you ALWAYS answer, never escalate-and-park. Precede your
decision with a 'REVERSIBILITY: reversible|outward|scope|irreversible' line, and a
'WARN: <what to double-check>' line for any critical, irreversible, outward-facing, or
scope-changing call. End with exactly one final line: 'ANSWER: <reply>'.
EOF
}

# --- bounding the reasoner (issue #171, subtask 1) ----------------------------
# An untimed headless `claude` can hang the whole tick; every reasoner run is bounded so a
# wedged answerer never freezes the supervisor. Expiry yields no decision line, so the gate
# fails SAFE to escalate (blocked/<issue>) — the existing no-decision fail-safe.

# _afk_answerer_timeout -> the reasoner's wall-clock budget in seconds. AFK_ANSWERER_TIMEOUT
# tunes it (default 900); a non-numeric OR non-positive override (0 disables the bound in
# both `timeout` and perl `alarm`) falls back to the default, so the cap is never silently
# lifted (#171 review).
_afk_answerer_timeout() {
  local s="${AFK_ANSWERER_TIMEOUT:-900}"
  case "$s" in '' | *[!0-9]* ) s=900 ;; esac
  [ "$s" -lt 1 ] && s=900
  printf '%s\n' "$s"
}

# _broker_run_bounded <secs> <cmd...> -> run <cmd...> (prompt on this function's stdin) under
# a <secs> wall-clock cap and return its exit code (nonzero on expiry). PREFERS hub-afk's
# shared _afk_with_timeout when the supervisor sourced it (issue #170): it tree-kills a
# wedged grandchild via _afk_kill_tree, so a hung `claude` can't keep run_answerer's capture
# pipe open and re-hang the tick. Reused (not re-implemented) via a runtime existence check —
# the same seam gate-broker uses for respawn_wedged_spoke — so the bound has one owner. Falls
# back to a self-contained bound only for a STANDALONE / attended broker without hub-afk (the
# tests): coreutils timeout/gtimeout, then a perl(alarm) wrapper (SIGALRM survives exec and
# terminates a runaway), then best-effort unbounded.
_broker_run_bounded() {
  local secs="$1"; shift
  if command -v _afk_with_timeout >/dev/null 2>&1; then _afk_with_timeout "$secs" "$@"; return; fi
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; return; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout "$secs" "$@"; return; fi
  if command -v perl >/dev/null 2>&1; then
    # UPGRADE: unlike _afk_with_timeout this does not reap a wedged grandchild — only reached
    # in a hub-less standalone/attended context where a long-lived `claude` grandchild is not
    # expected; production routes through _afk_with_timeout above.
    perl -e 'alarm shift @ARGV; exec @ARGV or exit 127' "$secs" "$@"; return
  fi
  "$@"   # no bounding tool available — best-effort unbounded
}

# run_answerer <issue> <question> [wt] -> the reasoner's raw output (stdout AND stderr),
# and its exit status as the function's return code. The reasoner is a headless `claude
# -p` (overridable via AFK_ANSWERER_CMD for tests), run with a thinking budget and a
# READ-ONLY tool allowlist; the prompt is passed on stdin so a long contract never hits
# argv limits. When <wt> is a directory it becomes the reasoner's cwd, so its read-only
# tools verify against the spoke's live state (the mutation guard in broker_service_gate
# is what makes that safe). The run is bounded by AFK_ANSWERER_TIMEOUT (_broker_run_bounded)
# so a hung `claude` never freezes the tick; expiry reads as no decision → escalate.
# stderr is folded into the captured stream (NOT discarded)
# because the CLI prints credential failures there and exits nonzero — the auth-failure
# detector needs both the message and the exit code. parse_decision is line-anchored, so
# interleaved stderr noise never pollutes a decision.
#
# --no-session-persistence stays belt-and-suspenders for #164. The original collision: the
# reasoner ran with cwd=<wt>, so a persisted transcript landed in the SAME
# ~/.claude/projects/<munged-wt>/ dir as the spoke's own, shadowing it — `_spoke_jsonl` picks
# the newest jsonl there, so every `_still_parked_same` check saw the transcript "move" and
# dropped the answer as stale, stranding the spoke. The #237 write-isolation snapshot already
# removes that collision at the root: the reasoner's cwd is now a mktemp copy, so any persisted
# transcript maps to the copy's OWN munged dir — disjoint from <wt>'s. We keep the flag anyway
# so no throwaway transcript is written for the snapshot path at all. It does NOT touch
# CLAUDE_CONFIG_DIR, so keychain credentials/auth are unaffected.
# UPGRADE: if a deployed `claude` lacks --no-session-persistence it exits nonzero with no
# decision, so the gate fails SAFE (escalates to blocked/<issue>) rather than stranding —
# but auto-answering silently stops; drop the flag / switch to filtering the reasoner's
# jsonl out of _spoke_jsonl if the installed CLI ever loses it.
run_answerer() {
  local issue="$1" question="$2" wt="${3:-}"
  local tools; tools="$(reasoner_allowed_tools)"
  local cmd="${AFK_ANSWERER_CMD:-claude -p --no-session-persistence --model claude-opus-4-8 --allowedTools '$tools'}"
  local secs; secs="$(_afk_answerer_timeout)"
  # Write isolation (#237): run the reasoner against a throwaway COPY of the worktree, not the
  # spoke's LIVE tree — so even a tool that ignores the read-only allowlist writes into the
  # copy. The reasoner's cwd moves to the snapshot; broker_service_gate still fingerprints the
  # real $wt, now a should-never-fire backstop. On any copy failure (no mktemp, non-git tree),
  # fall back to running in-place: the fingerprint void remains the guard. The snapshot is
  # built BEFORE the prompt (#239) so the posture can point cwd at the copy and never disclose $wt.
  local snap="" run_dir="$wt"
  if [ -n "$wt" ] && [ -d "$wt" ]; then
    snap="$(mktemp -d 2>/dev/null)" || snap=""
    if [ -n "$snap" ] && _broker_snapshot_worktree "$wt" "$snap"; then
      run_dir="$snap"
    elif [ -n "$snap" ]; then
      rm -rf "$snap" 2>/dev/null || true; snap=""
    fi
  fi
  local prompt; prompt="$(build_answerer_prompt "$issue" "$question")"
  # Deliver the prompt via a temp file the wrapped command re-opens with `exec <`, NOT only
  # the here-string: the bound (_afk_with_timeout's portable fallback) BACKGROUNDS the
  # command, and POSIX assigns a backgrounded job's stdin to /dev/null — a plain here-string
  # would be lost, starving the reasoner of its prompt. `exec <file` reopens stdin inside the
  # backgrounded shell, so the prompt survives every bound path. The here-string stays as a
  # fallback for when mktemp is unavailable (the foreground timeout/perl paths keep stdin).
  local pf rc; pf="$(mktemp 2>/dev/null)" || pf=""
  [ -n "$pf" ] && { printf '%s' "$prompt" > "$pf"; cmd="exec <'$pf'; $cmd"; }
  # _broker_run_bounded caps the reasoner (#171): a hung `claude` never freezes the tick.
  # stderr is folded in (2>&1) so the auth-failure detector still sees credential messages.
  (
    [ -n "$run_dir" ] && [ -d "$run_dir" ] && cd "$run_dir"
    CLAUDE_EFFORT="$AFK_ANSWERER_EFFORT" _broker_run_bounded "$secs" bash -c "$cmd" <<<"$prompt" 2>&1
  )
  rc=$?
  [ -n "$pf" ] && rm -f "$pf"
  [ -n "$snap" ] && rm -rf "$snap" 2>/dev/null || true
  return "$rc"
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

# parse_decision_field <raw-answerer-output> <KEYWORD> -> the trimmed value of the LAST
# '<KEYWORD>: <value>' line (empty when absent). #241 reads the reasoner's 'REVERSIBILITY:'
# class and 'WARN:' note off the same single-line convention as the ANSWER line, so a taken
# decision carries its reversibility class + human-review flag into the decision journal.
# <KEYWORD> must be a metacharacter-free literal (callers pass REVERSIBILITY / WARN); it is
# interpolated into an ERE. The value is both l- and r-trimmed so a class enum compares exact.
parse_decision_field() {
  local raw="$1" key="$2" line rest
  line="$(printf '%s\n' "$raw" | grep -E "^${key}:" | tail -1)"
  [ -n "$line" ] || return 0
  rest="${line#*:}"
  rest="${rest#"${rest%%[![:space:]]*}"}"          # ltrim
  rest="${rest%"${rest##*[![:space:]]}"}"          # rtrim
  printf '%s\n' "$rest"
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

# --- the permission classifier (issue #149) -----------------------------------
# A spoke under /afk stalls on Claude Code PERMISSION dialogs (distinct from the
# question/gate parks the answerer handles): the FIRST RED-commit selective stage
# `git reset -q; git add <own file>` prompts and, unanswered, the spoke idles until
# reaped. classify_permission decides such a dialog the way a human would — but by a
# fixed rules table, not the reasoning answerer, since the decision is mechanical and
# must be conservative. It is the unit-tested heart of the supervisor's permission
# handling (the tmux detection + injection that drives it lives in decide_and_act).

# _pytest_seg_scoped <segment> -> rc 0 when a `pytest` / `python -m pytest` segment carries a
# genuine SCOPING argument (a path or node-id), rc 1 otherwise. A bare `pytest`, one carrying
# only flags (`pytest -q`, `pytest -x`), OR one whose only non-flag token is a value belonging
# to a selection option (`pytest -k foo`, `pytest -m slow`, `pytest -p plugin`) still collects
# the WHOLE suite, whose escaped tests rewrite real refs (#135) — the full-suite ref-rewind
# hazard (#203). A separate-token value of such an option is therefore SKIPPED, not counted as
# a path. Tokens are walked by hand (no word-splitting) so a glob argument never expands.
_pytest_seg_scoped() {
  local seg="$1" rest tok skip_val=0
  case "$seg" in
    'python -m pytest'*)          rest="${seg#python -m pytest}" ;;
    'python3 -m pytest'*)         rest="${seg#python3 -m pytest}" ;;
    '.venv/bin/python -m pytest'*) rest="${seg#.venv/bin/python -m pytest}" ;;
    'pytest'*)                    rest="${seg#pytest}" ;;
    *) return 1 ;;
  esac
  while [ -n "$rest" ]; do
    rest="${rest#"${rest%%[![:space:]]*}"}"          # ltrim
    [ -n "$rest" ] || break
    tok="${rest%%[[:space:]]*}"                       # first token
    rest="${rest#"$tok"}"
    if [ "$skip_val" -eq 1 ]; then skip_val=0; continue; fi   # a prior option's value token
    case "$tok" in
      # separate-token value options: the NEXT token is a value, not a scoping path.
      -k | -m | -p | -c | -o | -W | -n | -r | --rootdir | --deselect | --ignore \
        | --ignore-glob | --confcutdir | --override-ini) skip_val=1 ;;
      -*) ;;                                          # any other flag (incl. --opt=value)
      *) return 0 ;;                                  # a genuine non-flag token = a path/node-id
    esac
  done
  return 1
}

# --- benign in-worktree mutation lane (issue #203, finding 4) ------------------
# A confirmation dialog on a COMPOUND command (cd into the worktree, mv a stashed file from
# the scratchpad, chmod +x it, stash pop, targeted pytest) used to classify as one opaque
# "risky" string and escalate, wedging the whole drain. These helpers let classify_permission
# APPROVE segments whose writes are confined to the spoke's OWN worktree or its session
# scratchpad — the spoke already has unrestricted Edit/Write there, so a chmod on its own new
# hook script carries no additional risk. .git/ internals and secret-like paths stay denied.

# _broker_path_physically_in <abs> <wt> <tasks> -> rc 0 when <abs>, with ALL symlinks
# resolved, is physically under the worktree or the tasks root and NOT under <wt>/.git; rc 1
# otherwise. Closes the symlink-indirection escape a textual check cannot see: a logically
# in-tree path (e.g. `.venv/bin/python3`, a symlink worktree-new.sh points out of tree) can
# physically resolve anywhere. os.path.realpath resolves the existing prefix — following a
# final symlink FILE (the overwrite case) — and appends any not-yet-created tail, so it works
# for create targets too. Fails CLOSED (rc 1) without python3: an unverifiable mutation path
# is denied, not trusted (a false deny escalates — the safe direction).
_broker_path_physically_in() {
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_ABS="$1" _AFK_WT="$2" _AFK_TASKS="$3" python3 2>/dev/null <<'PYEOF'
import os, sys

abs_ = os.path.realpath(os.environ["_AFK_ABS"])
wt = os.path.realpath(os.environ["_AFK_WT"])
tasks = os.path.realpath(os.environ["_AFK_TASKS"])

def under(p, root):
    return p == root or p.startswith(root.rstrip("/") + "/")

if not (under(abs_, wt) or under(abs_, tasks)):
    sys.exit(1)
# Reject any `.git` path component, case-INSENSITIVELY: macOS's default filesystem is
# case-insensitive, so `.GIT` addresses the same dir as `.git` and a literal-`.git` guard
# alone misses it; this also covers a nested repo's `.git` anywhere under the roots.
if any(part.lower() == ".git" for part in abs_.split(os.sep)):
    sys.exit(1)
sys.exit(0)
PYEOF
}

# _broker_resolve_in_roots <path> <cwd> <wt> <slug> <tasks> -> print <path>'s absolute form
# (resolved against <cwd>) IF it lies under the worktree <wt> or the spoke's session
# scratchpad (<tasks>/claude-*/<slug>/…), and NOT under <wt>/.git; else rc 1. TWO layers:
# a textual containment check (fast, and the only one that can bound the scratchpad glob),
# THEN a physical symlink-resolving check (_broker_path_physically_in) — both must pass.
# Any token the shell would EXPAND to a different path (traversal, variable/command
# substitution, tilde, brace or glob metacharacters) is rejected outright: a textual
# resolver cannot see through those, and a false deny escalates — the safe direction.
_broker_resolve_in_roots() {
  local p="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" abs
  # Reject any token the shell rewrites at execution to a path the textual/realpath checks
  # cannot see: traversal (`..`), variable/command substitution (`$`, backtick), tilde, brace
  # and glob metacharacters, quoting/escaping (`"` `'` `\`), and redirection (`>` `<`). Two
  # are load-bearing beyond the obvious: a leading quote/backslash (`rm "/etc/x"`) makes the
  # `/*` absolute test below miss it so it is joined onto the worktree cwd as if relative, and
  # a redirection (`cd foo>/etc/x`) hides an out-of-tree target the shell splits off — this
  # resolver is the cd-handler's ONLY guard, so it must reject `>`/`<` that _permission_seg_safe
  # rejects on the mutation path. realpath treats all these as ordinary chars, so an escaped
  # target would pass containment yet the shell mutates the real path. A false deny escalates.
  case "$p" in
    *'..'* | *'$'* | *'`'* | '~'* | *'{'* | *'}'* | *'*'* | *'?'* | *'['* | *']'* \
      | *'"'* | *"'"* | *'\'* | *'>'* | *'<'*) return 1 ;;
  esac
  case "$p" in /*) abs="$p" ;; *) abs="$cwd/$p" ;; esac
  # Collapse `/./` and duplicate slashes textually (no glob, no fs touch). The replacement
  # is `$sl` (a bare slash held in a var), NOT a literal `\/`: bash keeps the backslash in a
  # `${var//pat/repl}` replacement string, so `\/` would corrupt the path (`/x/./y`→`/x\/y`).
  local sl=/
  while case "$abs" in */./* | *//*) true ;; *) false ;; esac; do
    abs="${abs//\/.\//$sl}"; abs="${abs//\/\//$sl}"
  done
  abs="${abs%/.}"                                  # a trailing `/.` (bare `.` target) → the dir
  abs="${abs%/}"; [ -n "$abs" ] || abs="/"
  case "$abs" in "$wt"/.git | "$wt"/.git/*) return 1 ;; esac      # never .git internals (textual)
  case "$abs" in
    "$wt" | "$wt"/*) ;;                                           # under the worktree
    "$tasks"/claude-*/"$slug"/*) ;;                               # under the scratchpad
    *) return 1 ;;
  esac
  _broker_path_physically_in "$abs" "$wt" "$tasks" || return 1   # symlink-resolved containment
  printf '%s\n' "$abs"
}

# _broker_seg_secretlike <token> -> rc 0 when a path token looks like a secret (a mutation of
# it is never in the benign lane, even inside the worktree). Mirrors the repo's own secret
# .gitignore classes (.env, *.pem) plus the common credential filenames. Matched case-
# INSENSITIVELY (via tr — bash 3.2 lacks `${v,,}`): macOS's default filesystem is case-
# insensitive, so `.ENV` addresses the same inode as `.env` and must not slip the guard
# (mirroring the case-folded `.git` component check in _broker_path_physically_in).
_broker_seg_secretlike() {
  local base lower path_lower
  base="${1##*/}"
  lower="$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')"
  case "$lower" in
    .env | .env.* | *.pem | *.key | *.p12 | id_rsa | id_dsa | id_ecdsa | id_ed25519 \
      | .netrc | credentials | .npmrc | .pypirc) return 0 ;;
  esac
  path_lower="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$path_lower" in */.ssh/* | */.aws/* | */.gnupg/*) return 0 ;; esac
  return 1
}

# _permission_seg_mutation_ok <segment> <cwd> <wt> <slug> <tasks> -> rc 0 when a mutating
# segment (mv/cp/rm/mkdir/chmod) touches ONLY paths under the worktree or the spoke's
# scratchpad, none secret-like, none the worktree root itself. Tokens are walked by hand (no
# word-splitting) so a glob argument never expands. Inert (rc 1) without a worktree context.
_permission_seg_mutation_ok() {
  local seg="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" verb rest tok resolved saw_path=0 mode_pending=0
  [ -n "$wt" ] || return 1
  verb="${seg%% *}"
  case "$verb" in
    mv | cp | rm | mkdir | chmod) ;;
    *) return 1 ;;
  esac
  [ "$verb" = chmod ] && mode_pending=1        # chmod's first non-flag token is the mode
  rest="${seg#"$verb"}"
  while [ -n "$rest" ]; do
    rest="${rest#"${rest%%[![:space:]]*}"}"    # ltrim
    [ -n "$rest" ] || break
    tok="${rest%%[[:space:]]*}"                 # first token
    rest="${rest#"$tok"}"
    # `-t DIR` / `-tDIR` / `--target-directory[=DIR]` (GNU mv/cp) hide the DESTINATION inside
    # a flag; the glued/`=`-form would be skipped as a flag and its out-of-tree target never
    # checked. Deny the whole segment when one appears — a false deny escalates (BSD mv/cp on
    # the macOS host lacks -t, but this repo also runs on Linux/GNU coreutils).
    case "$tok" in
      -t | -t?* | --target-directory | --target-directory=*) return 1 ;;
    esac
    case "$tok" in -*) continue ;; esac         # a flag (mv -f, mkdir -p, …)
    if [ "$mode_pending" -eq 1 ]; then mode_pending=0; continue; fi
    _broker_seg_secretlike "$tok" && return 1
    resolved="$(_broker_resolve_in_roots "$tok" "$cwd" "$wt" "$slug" "$tasks")" || return 1
    [ "$resolved" = "$wt" ] && return 1         # never target the worktree root itself
    saw_path=1
  done
  [ "$saw_path" -eq 1 ]
}

# _permission_seg_exec_ok <segment> <cwd> <wt> <slug> <tasks> -> rc 0 when the segment EXECUTES
# a spoke-authored in-tree script via a `./<relative-path>` invocation whose executable resolves
# under the worktree or the spoke's session scratchpad (via _broker_resolve_in_roots — the same
# scope the mutation lane uses — which rejects `..`, absolute paths, `.git`, and shell
# metacharacters). Trailing args are opaque to WHICH code runs and are left to the
# script; the segment-level substitution/redirection reject in _permission_seg_safe has already
# fired before this is reached. Inert (rc 1) without a worktree context. Approving this is a
# worktree-trust-boundary call (#240): the gate protects SHARED state — main, the remote, sibling
# worktrees, out-of-tree paths — and trusts the spoke inside its OWN worktree, where it already
# has auto-accepted edits and where an APPROVEd targeted pytest already runs spoke-authored code.
_permission_seg_exec_ok() {
  local seg="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" tok resolved
  [ -n "$wt" ] || return 1
  tok="${seg%%[[:space:]]*}"                   # the executable (first token)
  case "$tok" in './'*) ;; *) return 1 ;; esac # only the relative ./ self-op form
  _broker_seg_secretlike "$tok" && return 1
  resolved="$(_broker_resolve_in_roots "$tok" "$cwd" "$wt" "$slug" "$tasks")" || return 1
  [ "$resolved" = "$wt" ] && return 1          # never "execute" the worktree root itself
  return 0
}

# _permission_seg_safe <segment> [cwd wt slug tasks] -> true when ONE command segment is a
# safe scoped self-op the spoke legitimately runs on its OWN worktree: the same vetted class
# worktree-new.sh seeds into the spoke allowlist (unstage/stage, own-file pytest,
# read-only helpers). A segment carrying command substitution, backticks, or a
# redirection is never safe — those could smuggle a destructive op behind a safe
# prefix. `git reset`'s working-tree-mutating modes (`--hard`/`--merge`/`--keep`) are
# rejected before the safe `git reset` prefix matches — only unstage/uncommit is safe.
# Everything unrecognised is unsafe (default-deny).
_permission_seg_safe() {
  local seg="$1" cwd="${2:-}" wt="${3:-}" slug="${4:-}" tasks="${5:-}"
  case "$seg" in
    *'$('* | *'`'* | *'>'* | *'<'*) return 1 ;;   # substitution / redirection smuggling
  esac
  # Benign in-worktree mutation lane (#203): when we know the spoke's worktree, a mutating
  # verb (mv/cp/rm/mkdir/chmod) is decided ENTIRELY by the lane — approve when confined to
  # the worktree or its scratchpad, else deny. Deciding it here (not falling through) is what
  # keeps the legacy relative-only `chmod +x` rule below from re-approving a lane MISS such as
  # `chmod +x .git/hooks/pre-commit`. Without worktree context the lane is inert and these
  # verbs fall through to the context-free rules (the relative-only chmod rule / default-deny).
  case "$seg" in
    'mv '* | 'cp '* | 'rm '* | 'mkdir '* | 'chmod '*)
      if [ -n "$wt" ]; then
        _permission_seg_mutation_ok "$seg" "$cwd" "$wt" "$slug" "$tasks" && return 0
        return 1
      fi ;;
  esac
  # Benign in-worktree EXECUTION lane (#240): running the spoke's OWN in-tree script
  # (`./path/to/script.sh`) is a scoped self-op, decided ENTIRELY by the lane when a worktree
  # is known — mirroring the mutation lane above so the context-free rules below never re-judge
  # it. Without a worktree context the lane is inert and `./…` falls through to default-deny.
  case "$seg" in
    './'*)
      if [ -n "$wt" ]; then
        _permission_seg_exec_ok "$seg" "$cwd" "$wt" "$slug" "$tasks" && return 0
        return 1
      fi ;;
  esac
  case "$seg" in
    *'--hard'* | *'--merge'* | *'--keep'*) return 1 ;;  # reset modes that touch the worktree
    'git reset' | 'git reset '* ) return 0 ;;      # unstage/uncommit only — worktree-local
    'git add' | 'git add '* ) return 0 ;;          # stage — worktree-confined
    'git status' | 'git status '* | 'git diff' | 'git diff '* ) return 0 ;;
    'git log' | 'git log '* | 'git show' | 'git show '* ) return 0 ;;
    'git rev-parse' | 'git rev-parse '* | 'git branch --show-current' ) return 0 ;;
    'git fetch' | 'git fetch '* ) return 0 ;;
    # git stash is worktree/stash-local (never touches main or the remote): pop/apply restore
    # the spoke's own stashed work, push/save stash it, list/show inspect it (#203 finding 4).
    'git stash' | 'git stash pop'* | 'git stash apply'* | 'git stash push'* \
      | 'git stash save'* | 'git stash list'* | 'git stash show'* ) return 0 ;;
    # pytest MUST carry a NON-FLAG argument (a path / node-id): a bare `pytest` OR one
    # carrying only flags (`pytest -q`, `pytest -x`) still runs the whole suite, whose
    # escaped tests rewrite real refs (#135) — the full-suite ref-rewind hazard. Requiring
    # a token (not merely any token) closes the flag-only bypass (#203).
    'pytest '* | 'python -m pytest '* | 'python3 -m pytest '* | '.venv/bin/python -m pytest '* )
      _pytest_seg_scoped "$seg" && return 0 || return 1 ;;
    'ls' | 'ls '* | 'cat '* | 'head '* | 'tail '* | 'wc' | 'wc '* ) return 0 ;;
    'grep '* | 'rg '* | 'echo' | 'echo '* | 'tree' | 'tree '* ) return 0 ;;
    'find '* )
      # A read-only find is a fine self-op, but any side-effecting primary is not: `-delete`
      # destroys files; `-exec`/`-execdir`/`-ok`/`-okdir` spawn processes; `-fprint`/`-fprintf`/
      # `-fprint0`/`-fls` write to an arbitrary file. Deny them all (#171 + review). `-print`/
      # `-printf` write only to stdout and stay allowed. Over-denial (a filename that happens to
      # contain one of these) escalates to a human, the safe direction for a default-deny guard.
      case "$seg" in *-delete* | *-exec* | *-ok* | *-fprint* | *-fls* ) return 1 ;; esac
      return 0 ;;
    'chmod +x '* )
      # chmod +x only on a RELATIVE, in-tree path. Reject an absolute target (a leading `/` or
      # a later ` /` token like `chmod +x a /bin/x`) and any `..` that would traverse out of the
      # spoke's worktree (#171 + review). A false deny (a filename containing `..`) escalates.
      case "$seg" in *' /'* | 'chmod +x /'* | *'..'* ) return 1 ;; esac
      return 0 ;;
    * ) return 1 ;;
  esac
}

# --- read-only Read tool lane (issue #181) ------------------------------------
# A spoke parks on a `Read` PERMISSION dialog for a legitimate, write-free research read —
# a hub script/hook (#175 parked on Read(<hub>/.git/hooks/pre-push)) or a sibling worktree.
# extract_pending_command surfaces such a park as "Read <file_path>"; classify_permission
# AUTO-APPROVES it when the target is confined to the repo family (the main root + its
# worktrees) and is not secret-like. A Read mutates nothing, so — unlike the write lane above
# — .git internals are readable; only the global secret classes (~/.ssh, ~/.aws, *.pem,
# id_rsa*, credential confs) stay denied. Every OTHER non-Bash tool arrives as a bare name and
# keeps default-deny.

# _broker_repo_family_roots <wt> -> print each repo-family root (the main worktree PLUS every
# linked worktree, from `git worktree list`), realpath-canonicalized, one per line. Empty when
# <wt> is not a git worktree. This is the read scope a spoke legitimately studies.
_broker_repo_family_roots() {
  local wt="$1" line p
  [ -n "$wt" ] || return 0
  git -C "$wt" worktree list --porcelain 2>/dev/null | while IFS= read -r line; do
    case "$line" in
      'worktree '*) p="${line#worktree }"; wt_realpath "$p" ;;
    esac
  done
}

# _broker_read_in_family <path> <wt> -> print <path>'s realpath (symlinks followed) IF it
# resolves under some repo-family root, else rc 1. Resolves <path> against the worktree cwd when
# relative, mirroring _broker_path_physically_in. The printed realpath lets the caller re-check
# the secret class on the RESOLVED surface (a benign-named in-family symlink to a key, or a
# trailing-slash form, evades a raw-path-only check). Fails CLOSED (rc 1) without python3 or a
# resolvable family — an unverifiable read escalates, the safe direction.
_broker_read_in_family() {
  local path="$1" wt="${2:-}" roots
  roots="$(_broker_repo_family_roots "$wt")"
  [ -n "$roots" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_PATH="$path" _AFK_WT="$wt" _AFK_ROOTS="$roots" python3 2>/dev/null <<'PYEOF'
import os, sys

path = os.environ["_AFK_PATH"]
wt = os.environ.get("_AFK_WT", "")
if not os.path.isabs(path) and wt:
    path = os.path.join(wt, path)
abs_ = os.path.realpath(path)

def under(p, root):
    return p == root or p.startswith(root.rstrip("/") + "/")

for root in os.environ["_AFK_ROOTS"].splitlines():
    if root and under(abs_, os.path.realpath(root)):
        print(abs_)
        sys.exit(0)
sys.exit(1)
PYEOF
}

# _classify_read_tool <path> [wt] -> print "APPROVE" or "ESCALATE<TAB><reason>" for a Read of
# <path>. APPROVE only when <path> is a CLEAN inert path, confined to the repo family, and not
# secret-like. rc is always 0 (the verdict is on stdout, like classify_permission).
#
# The clean-path guard is load-bearing security, not cosmetics: extract_pending_command emits a
# Bash tool_use as its RAW command string in the same slot a Read emits "Read <file_path>", so a
# Bash command whose text starts with "Read " (e.g. `Read x.py; rm -rf ~`) would otherwise enter
# this lane and SKIP classify_permission's operator-split default-deny — auto-approving arbitrary
# shell. A genuine Read file_path is a single inert path, so any whitespace / shell metacharacter /
# operator / traversal makes the target unapprovable here (a false deny escalates — the safe
# direction). The secret class is then checked on BOTH the raw path and its resolved realpath, so
# an in-family symlink with a benign name (notes.txt -> deploy.pem) can't launder a key.
#
# Two properties of the whitespace rejection are load-bearing, DO NOT weaken blindly:
#   - It is a DENYLIST: safety rests on the reject set covering every shell control / expansion /
#     quoting metacharacter. Extend the set, never trim it.
#   - `*[[:space:]]*` rejects an embedded NEWLINE too (a case-glob matches it), closing the
#     newline-as-command-separator variant. If anyone ever relaxes this to allow spaced paths,
#     ONLY space/tab may be re-admitted — a re-allowed newline reopens the masquerade.
# Known limitation: a worktree whose ROOT path itself contains whitespace makes every family read
# non-clean, so the feature degrades to always-escalate for that checkout (safe, but silent).
_classify_read_tool() {
  local path="$1" wt="${2:-}" abs
  if [ -z "$path" ]; then
    printf 'ESCALATE\t%s\n' "Read with no target"
    return 0
  fi
  case "$path" in
    *[[:space:]]* | *';'* | *'&'* | *'|'* | *'$'* | *'`'* | '~'* | *'..'* \
      | *'{'* | *'}'* | *'*'* | *'?'* | *'['* | *']'* | *'"'* | *"'"* | *'\'* \
      | *'>'* | *'<'* | *'('* | *')'*)
      printf 'ESCALATE\t%s\n' "read target is not a clean path: $path"
      return 0 ;;
  esac
  if _broker_seg_secretlike "$path"; then
    printf 'ESCALATE\t%s\n' "secret-like read target: $path"
    return 0
  fi
  abs="$(_broker_read_in_family "$path" "$wt")" || {
    printf 'ESCALATE\t%s\n' "read outside the repo family: $path"
    return 0
  }
  if _broker_seg_secretlike "$abs"; then
    printf 'ESCALATE\t%s\n' "secret-like read target (resolved): $abs"
    return 0
  fi
  printf 'APPROVE\n'
}

# classify_permission <command> [worktree] -> "APPROVE" or "ESCALATE<TAB><reason>".
# DEFAULT-DENY: the command is APPROVEd only when EVERY segment (split on ; && || |) is a
# safe scoped self-op, so a single risky segment in a chain escalates the whole. When the
# spoke's <worktree> is known, the compound is DECOMPOSED and `cd` is tracked so the benign
# in-worktree mutation lane (#203, finding 4) can approve writes confined to the worktree or
# its scratchpad. Anything unrecognised — main-touching, force-push, history rewrite, an
# out-of-tree deletion, network fetch, browser/computer/mcp tool, or a bare non-Bash tool
# name — ESCALATEs, naming the offending command so the block record is actionable.
classify_permission() {
  local cmd="$1" wt="${2:-}" norm seg saw_seg=0 cwd="" slug="" tasks="" target new_cwd
  if [ -n "$wt" ]; then
    slug="$(printf '%s' "$wt" | sed 's/[^A-Za-z0-9]/-/g')"
    tasks="${AFK_TASKS_ROOT:-/private/tmp}"
    cwd="$wt"                                       # the compound starts in the worktree
  fi
  # A non-Bash READ tool invocation arrives as "Read <file_path>" (extract_pending_command
  # carries the target). It is decided ENTIRELY by the read lane (#181), BEFORE operator-
  # splitting so a path with shell-ish characters is never chopped into bogus segments.
  case "$cmd" in
    'Read '*) _classify_read_tool "${cmd#Read }" "$wt"; return 0 ;;
  esac
  # Normalise the shell operators to newlines, longest first so `||` is not split by `|`
  # and `&&` is not split by a single `&`. The single `&` (background) MUST also split, or
  # `echo x & rm -rf /` would match the safe `echo ` prefix and never inspect the tail.
  norm="${cmd//&&/$'\n'}"
  norm="${norm//&/$'\n'}"
  norm="${norm//||/$'\n'}"
  norm="${norm//|/$'\n'}"
  norm="${norm//;/$'\n'}"
  while IFS= read -r seg; do
    seg="${seg#"${seg%%[![:space:]]*}"}"           # ltrim
    seg="${seg%"${seg##*[![:space:]]}"}"           # rtrim
    [ -n "$seg" ] || continue
    saw_seg=1
    # cd-tracking within the compound: a `cd` into a path that stays under the worktree/
    # scratchpad updates the current dir for the following segments' relative paths; a `cd`
    # that escapes (or a bare `cd` → $HOME, or no worktree context) escalates the whole.
    case "$seg" in
      'cd '*)
        target="${seg#cd }"; target="${target#"${target%%[![:space:]]*}"}"
        # An empty target (`cd` → $HOME) or a `-`-prefixed one (`cd -`/`--`/`-P`/`-L` → $OLDPWD
        # or $HOME) navigates OUT of the tree — never a literal in-tree dir. Reject before the
        # resolver, which would otherwise read `--` as an in-tree directory name and track a
        # bogus cwd. A real dir starting with `-` is always reachable as `./-x`.
        case "$target" in '' | -*) printf 'ESCALATE\t%s\n' "risky or unrecognised command: $cmd"; return 0 ;; esac
        if [ -n "$wt" ] && new_cwd="$(_broker_resolve_in_roots "$target" "$cwd" "$wt" "$slug" "$tasks")"; then
          cwd="$new_cwd"; continue
        fi
        printf 'ESCALATE\t%s\n' "risky or unrecognised command: $cmd"; return 0 ;;
    esac
    if ! _permission_seg_safe "$seg" "$cwd" "$wt" "$slug" "$tasks"; then
      printf 'ESCALATE\t%s\n' "risky or unrecognised command: $cmd"
      return 0
    fi
  done <<< "$norm"
  # An empty / all-whitespace command has no segment to vouch for — never approve nothing.
  [ "$saw_seg" -eq 1 ] || { printf 'ESCALATE\t%s\n' "empty or unreadable command"; return 0; }
  printf 'APPROVE\n'
}

# --- permission-dialog detection + handling (issue #149) ----------------------
# A permission dialog is a pane-only surface — a Claude Code confirmation prompt with no
# transcript entry of its OWN — but the tool_use it is gating IS flushed to the JSONL as an
# UNRESOLVED block (no matching tool_result) for the whole park. So the dialog is detected
# from the pane (the only "a dialog is up" signal) and the command it gates is read from that
# unresolved tool_use. classify_permission decides it; these helpers see it and deliver the
# decision. _decide_permission is reached from decide_and_act, which routes a
# permission-pending spoke here instead of to the answerer.

# extract_pending_command <wt_path> -> the command of the spoke's trailing UNRESOLVED
# assistant tool_use — the one a permission dialog is gating (Bash -> its command string;
# Read -> "Read <file_path>"; any other tool -> the tool name, so the classifier escalates
# non-Bash tools like browser/computer/mcp). A tool_use is UNRESOLVED when no later
# tool_result carries its id; the PRIOR calls a parked spoke already completed are resolved
# and MUST be skipped (#240: returning the last resolved tool surfaced a phantom "Write" and
# escalated a spoke that needed no human). Empty when nothing is unresolved -> the caller
# escalates honestly ("unreadable command"), never on a stale resolved tool name.
extract_pending_command() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  _AFK_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json, os

# Two passes over the transcript: first collect every tool_result's tool_use_id (a
# tool_result always trails its tool_use in file order, so resolution can only be known
# after a full read), then pick the LAST tool_use whose id is NOT among them — the one the
# permission dialog is still gating. Prior, already-resolved calls are skipped (#240).
tool_uses = []            # ordered (id, name, input) of every assistant tool_use
resolved = set()          # tool_use_ids that a later tool_result has settled
try:
    with open(os.environ["_AFK_JSONL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            content = (obj.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use" and obj.get("type") == "assistant":
                    tool_uses.append(
                        (block.get("id"), (block.get("name") or "").strip(), block.get("input") or {})
                    )
                elif btype == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid:
                        resolved.add(tid)
except Exception:
    tool_uses = []

cmd = ""
for tid, name, inp in reversed(tool_uses):
    if tid in resolved:       # a completed call the spoke already ran — never the pending one
        continue
    if not isinstance(inp, dict):
        inp = {}
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
    elif name == "Read":
        # Carry the Read TARGET alongside the name (#181) so the classifier can vet the
        # path — a repo-family read is auto-approvable, a bare name is not.
        fp = (inp.get("file_path") or "").strip()
        cmd = f"{name} {fp}" if fp else name
    elif name:
        cmd = name
    break                     # the trailing unresolved tool_use is the pending command
print(cmd[:2000].strip())
PYEOF
}

# _pane_shows_permission_prompt <wt_path> -> true when the spoke's pane shows a Claude Code
# permission dialog. The signature regex is tunable via AFK_PERMISSION_PROMPT_RE. Fail-CLOSED
# (return 1) when tmux or the pane is unavailable: an unobservable pane is never treated as a
# pending permission, so slot_state's read of a no-tmux spoke is unchanged.
_pane_shows_permission_prompt() {
  local wt="$1" target re
  re="${AFK_PERMISSION_PROMPT_RE:-Do you want to proceed\?}"
  command -v tmux >/dev/null 2>&1 || return 1
  target="$(_spoke_pane_target "$wt")"
  [ -n "$target" ] || return 1
  tmux capture-pane -p -t "$target" 2>/dev/null | grep -Eq -- "$re"
}

# _permission_pending <wt_path> -> true when the spoke is parked on a permission dialog we can
# act on: the pane shows the prompt AND the command it is trying to run is readable. The single
# gate slot_state and decide_and_act share.
_permission_pending() {
  local wt="$1"
  _pane_shows_permission_prompt "$wt" || return 1
  [ -n "$(extract_pending_command "$wt")" ]
}

# approve_permission <wt_path> -> select "Yes" on the pending permission dialog and confirm the
# spoke resumed. Sends "1" then a SEPARATE Enter — option 1 is "Yes" (this once), NEVER option 2
# ("Yes, don't ask again"), so nothing is silently broadened — then verifies the transcript
# advanced. rc 0 approved; rc 1 no pane / not confirmed (the caller escalates).
approve_permission() {
  local wt="$1" target before
  command -v tmux >/dev/null 2>&1 || return 1
  target="$(_spoke_pane_target "$wt")"
  [ -n "$target" ] || return 1
  before="$(_transcript_mtime "$wt")"
  tmux send-keys -t "$target" 1 2>/dev/null || return 1
  tmux send-keys -t "$target" Enter 2>/dev/null || return 1
  _transcript_advanced "$wt" "$before"
}

# _deny_permission <wt_path> <guidance> -> decline the pending permission dialog and tell the
# spoke the reversible path: the hardened injector Esc-cancels the menu, then submits <guidance>
# as a new message. Best-effort (rc from inject_and_verify) — a failed delivery still lets the
# caller warn + retry on the backoff, never park.
_deny_permission() {
  local wt="$1" guidance="$2" target
  target="$(_spoke_pane_target "$wt")"
  [ -n "$target" ] || return 1
  inject_and_verify "$wt" "$target" "$guidance" >/dev/null 2>&1
}

# _reason_permission <wt> <issue> <cmd> <classify_reason> -> the reasoner decides a permission
# dialog the fixed rules would NOT auto-approve (#241 §2: the reasoner decides even irreversible
# asks). It runs in run_answerer's read-only snapshot copy and answers 'ANSWER: APPROVE' or
# 'ANSWER: DENY: <reversible path>'. APPROVE delivers Yes; DENY (or any unclear reply — the safe
# default) declines the dialog and injects the reversible-path guidance. Either way the taken
# decision is warned + journaled with its reversibility class, and the spoke is NEVER parked.
_reason_permission() {
  local wt="$1" issue="$2" cmd="$3" why="$4" q raw ans text rev guidance
  q="The spoke is parked on a PERMISSION dialog and wants to run this command:

$cmd

The mechanical classifier would not auto-approve it ($why). Decide: APPROVE only if it is
safe, reversible, and in-scope (touches the spoke's own worktree; no default branch, no
force-push, no history rewrite, no deletion outside the worktree, no outward/network action);
otherwise DENY. NEVER approve an irreversible, destructive, or outward command — DENY it and
name the reversible path. Your ANSWER line MUST begin with 'APPROVE' or 'DENY: <the reversible
path to tell the spoke>'."
  # Stamp the attempt FIRST so the reason→deliver window never reads as idle (#202 C).
  stamp_answer_attempt "$issue"
  raw="$(run_answerer "$issue" "$q" "$wt")"
  ans="$(parse_decision "$raw")"
  text="${ans#*$'\t'}"
  rev="$(parse_decision_field "$raw" REVERSIBILITY)"; [ -n "$rev" ] || rev=unknown
  # NB: the classifier verdict (ESCALATE) is already recorded in decisions.log by the caller;
  # the reasoned approve/deny lands in the decision journal via broker_warn_continue, NOT in
  # decisions.log — that log codifies only the MECHANICAL classifier (#155 D).
  case "$text" in
    APPROVE*)
      if approve_permission "$wt"; then
        broker_warn_continue "$wt" "$issue" permission "reasoner APPROVED: $cmd" "$rev"
      else
        broker_warn_continue "$wt" "$issue" permission "reasoner APPROVED but delivery failed: $cmd" "$rev"
      fi ;;
    *)
      # DENY, or any reply that does not clearly approve — the safe default is to decline.
      guidance="${text#DENY:}"; guidance="${guidance#"${guidance%%[![:space:]]*}"}"
      [ -n "$guidance" ] || guidance="Declined that command — take the reversible, in-scope path instead."
      _deny_permission "$wt" "$guidance" || true
      broker_warn_continue "$wt" "$issue" permission "reasoner DENIED ($cmd): $guidance" "${rev:-reversible}" ;;
  esac
}

# _decide_permission <wt_path> <issue> -> classify the spoke's pending permission dialog and act.
# AUTO-APPROVE a safe scoped self-op (mechanical fast path, unchanged, unwarned). Anything the
# fixed rules will not auto-approve — an ESCALATE verdict or an unreadable command — no longer
# parks the spoke: it routes to the always-answering reasoner (#241) which approves a safe
# command or declines-and-redirects a risky one, warning + journaling the taken decision.
_decide_permission() {
  local wt="$1" issue="$2" cmd decision kind reason
  cmd="$(extract_pending_command "$wt")"
  if [ -z "$cmd" ]; then
    # Unreadable command: cannot classify. Decline it (the reversible action) + warn — never
    # park. The spoke gets a denial and keeps going; the backoff paces any retry.
    stamp_answer_attempt "$issue"
    _deny_permission "$wt" "Declined an unreadable permission command — re-issue it in a clearer form." || true
    broker_warn_continue "$wt" "$issue" permission "declined an unreadable permission command" reversible
    return 0
  fi
  decision="$(classify_permission "$cmd" "$wt")"
  kind="${decision%%$'\t'*}"
  reason="${decision#*$'\t'}"
  # Record the classifier's VERDICT (both APPROVE and ESCALATE) for the codification pass,
  # not just successful approvals — otherwise every logged line is APPROVE and codify's
  # unanimity check is vacuous. Logging both makes a flag-dependent signature (`git reset
  # -q` APPROVE vs `git reset --hard` ESCALATE, which share the signature git-reset+git-add)
  # correctly read as a CONFLICT, so codify never proposes it as a safe unanimous rule (#155 D).
  log_decision "$issue" permission "$cmd" "$kind"
  if [ "$kind" = "APPROVE" ]; then
    log "→ auto-approving safe permission for #$issue: $cmd"
    # Stamp the delivery attempt FIRST: the approve→resume window must not read as idle.
    stamp_answer_attempt "$issue"
    if approve_permission "$wt"; then
      log "  approved permission for #$issue"
      afk_emit_decision "$wt" success
      return 0
    fi
    # Delivery failed — warn + retry on the backoff, never park (#241).
    broker_warn_continue "$wt" "$issue" permission "could not deliver the approval to the spoke — will retry" reversible
    return 0
  fi
  # ESCALATE: the fixed rules will not auto-approve this one. The reasoner decides it (#241) —
  # approve a safe/reversible command, or decline an irreversible one and name the reversible
  # path — and warns + journals the taken decision. Never park.
  _reason_permission "$wt" "$issue" "$cmd" "$reason"
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

# _answer_needle <text> -> the shared delivery needle: the first ~40 chars of the
# answer's first line. One derivation feeds both delivery proofs (_composer_shows_text,
# _answer_appended) so the pane and transcript checks can never grep diverging strings.
_answer_needle() {
  local needle="${1%%$'\n'*}"
  printf '%s\n' "${needle:0:40}"
}

# _composer_shows_text <pane_target> <text> -> true when the pane still displays the
# answer's needle, i.e. the paste is buffered in the composer, not submitted (#133).
# Fail-OPEN: an unreadable pane (capture error, no tmux) reads as "not shown", so the
# caller escalates instead of wedge-respawning a pane it cannot observe.
_composer_shows_text() {
  local target="$1" text="$2" needle
  needle="$(_answer_needle "$text")"
  [ -n "$needle" ] || return 1
  tmux capture-pane -p -t "$target" 2>/dev/null | grep -qF -- "$needle"
}

# _transcript_sizes <wt_path> -> one "size<TAB>path" line per jsonl in the spoke's
# project dir (empty when none). The pre-inject snapshot _answer_appended scans past:
# delivery proof is the answer landing in bytes APPENDED after this point, so a canned
# answer already sitting in an older record can neither satisfy nor disable the check.
_transcript_sizes() {
  local dir f
  dir="$(_spoke_project_dir "$1")"
  [ -d "$dir" ] || return 0
  for f in "$dir"/*.jsonl; do
    [ -e "$f" ] || continue
    printf '%s\t%s\n' "$(stat -f %z "$f" 2>/dev/null || stat -c %s "$f" 2>/dev/null)" "$f"
  done
}

# _answer_appended <wt_path> <text> <sizes> -> did the answer land as a USER record in
# transcript bytes appended after the <sizes> snapshot? A submitted answer is recorded
# as a user turn in the session jsonl, so a fresh match is positive proof the composer
# let go (#201) — the pane alone cannot give it, because a successful submit also
# ECHOES the message into the scrollback and keeps the needle visible. The match is
# byte-level against the needle's JSON-encoded form (quotes/backslashes cannot hide
# it; JSON keeps non-ASCII as raw UTF-8, and a needle byte-truncated mid-character by
# a C-locale slice still matches as a byte prefix), then the matching line must parse
# as a type:"user" record — a non-turn write coincidentally quoting the answer (a
# re-rendered question record, a foreign sidecar) is NOT proof (#201 review). Only
# appended regions are read — never a full transcript rescan; a rotated/unstat-able
# file degrades to a from-0 scan of that file (fail-toward-pre-#201, accepted).
# rc 0 found, rc 1 not found, rc 2 scan unavailable (no python3 / no project dir /
# interpreter died — a crash exits 1 in python, so "not found" is the DISTINCT exit 3
# and everything else maps to 2). Callers must treat 2 as "no evidence either way".
_answer_appended() {
  local wt="$1" text="$2" sizes="$3" needle dir
  needle="$(_answer_needle "$text")"
  [ -n "$needle" ] || return 1
  dir="$(_spoke_project_dir "$wt")"
  [ -d "$dir" ] || return 2
  command -v python3 >/dev/null 2>&1 || return 2
  _AFK_DIR="$dir" _AFK_NEEDLE="$needle" _AFK_SIZES="$sizes" python3 2>/dev/null <<'PYEOF'
import glob, json, os, sys

raw = os.environb.get(b"_AFK_NEEDLE", b"")
for i, byte in enumerate(raw):
    if byte < 0x20 and byte not in (9, 13):  # control char the escape map can't encode
        raw = raw[:i]
        break
if not raw:
    sys.exit(4)  # no usable needle: unavailable, not "not found"
needle = (
    raw.replace(b"\\", b"\\\\")
    .replace(b'"', b'\\"')
    .replace(b"\t", b"\\t")
    .replace(b"\r", b"\\r")
)
offsets = {}
for line in os.environb.get(b"_AFK_SIZES", b"").splitlines():
    size, _, path = line.partition(b"\t")
    if path:
        try:
            offsets[os.fsdecode(path)] = int(size)
        except ValueError:
            pass
for path in glob.glob(os.path.join(os.environ["_AFK_DIR"], "*.jsonl")):
    try:
        with open(path, "rb") as fh:
            offset = offsets.get(path, 0)
            fh.seek(0, 2)
            if offset > fh.tell():  # rotated/truncated since the snapshot: rescan
                offset = 0
            fh.seek(offset)
            appended = fh.read()
    except OSError:
        continue
    for line in appended.splitlines():
        if needle not in line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue  # partial flush at the offset boundary: not a record yet
        if isinstance(record, dict) and record.get("type") == "user":
            sys.exit(0)
sys.exit(3)
PYEOF
  case $? in 0) return 0 ;; 3) return 1 ;; *) return 2 ;; esac
}

# _answer_delivered <wt> <target> <text> <sizes> -> after _transcript_advanced
# succeeded, decide whether the answer actually LEFT the composer (#201: an advance
# alone scored two wedged pastes as "injected answer into #182" while the answer sat
# unsubmitted). NOT delivered only on the full #182 signature — positive evidence the
# composer still holds the text: the scan worked, the needle did NOT land in appended
# transcript bytes (so a pane match is not the echo of a genuine submit), and a
# readable pane still shows it. Everything short of that keeps the pre-#201 contract
# (advance alone = delivered): an unobservable pane or unavailable scan is NO evidence
# of a wedge, and a false success parks one spoke where a false NOT-delivered would
# stray-Enter, escalate, or respawn healthy panes on every echoed submit.
_answer_delivered() {
  local wt="$1" target="$2" text="$3" sizes="$4" rc
  _answer_appended "$wt" "$text" "$sizes"; rc=$?
  [ "$rc" -eq 0 ] && return 0
  [ "$rc" -eq 2 ] && return 0
  _composer_shows_text "$target" "$text" || return 0
  return 1
}

# _transcript_advanced <wt_path> <baseline_mtime> -> true once the spoke's newest
# transcript mtime exceeds the baseline, polling up to AFK_INJECT_VERIFY_SECONDS in
# AFK_INJECT_POLL_SECONDS steps. An empty baseline (no prior transcript) means any
# transcript now is progress. Used to confirm an injected answer actually registered.
_transcript_advanced() {
  local wt="$1" before="$2" budget poll waited=0 now
  # 60s (was 20): a slow first token after submit was misread as "did not register",
  # feeding a false escalation that #171-subtask-3 then made sticky (#171-subtask-4).
  budget="${AFK_INJECT_VERIFY_SECONDS:-60}"
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
# it registered: the spoke's transcript advanced AND the composer let go of the text
# (#201: a non-turn write bumping the newest jsonl made "a file moved" score two
# wedged pastes as success). The retry is a bare Enter, NEVER a re-paste: the common
# failure is a buffered paste whose submitting Enter was lost, and the old full
# re-inject duplicated the answer on top of it (#133, from #123/#124).
#   rc 0 — delivered (the transcript advanced and the composer released the answer).
#   rc 2 — WEDGED: the text survived the Enter-only retry (an unterminated paste no
#          keystroke can submit or clear) — the caller respawns the pane.
#   rc 3 — REFUTED: the transcript advanced but delivery was positively disproven (a
#          readable pane still shows the needle and no user record landed in appended
#          bytes — the #182 signature, minus the wedge-classifiable pane state). The
#          advance is EXPLAINED: callers must NOT read it as the spoke moving on — a
#          moved-on drop here leaves the gate tag and re-pastes forever (#201 review).
#   rc 1 — not registered and no text observable in the composer — the caller escalates.
inject_and_verify() {
  local wt="$1" target="$2" text="$3" before baseline_shows=0 sizes vetoed=0
  before="$(_transcript_mtime "$wt")"
  # Baseline BEFORE pasting: a short answer often also appears in the rendered
  # question above the composer. If the needle was already visible pre-inject,
  # post-retry presence proves nothing — never classify wedged off a pre-existing
  # match (a false wedge would kill a live pane where rc 1 safely escalates).
  _composer_shows_text "$target" "$text" && baseline_shows=1
  sizes="$(_transcript_sizes "$wt")"
  inject_answer "$target" "$text" || return 1
  if _transcript_advanced "$wt" "$before"; then
    _answer_delivered "$wt" "$target" "$text" "$sizes" && return 0
    # The advance may have raced the submit's own user-record write by milliseconds:
    # one grace re-check before treating the veto as real (#201 review).
    sleep "${AFK_INJECT_POLL_SECONDS:-2}" 2>/dev/null || true
    _answer_appended "$wt" "$text" "$sizes" && return 0
    vetoed=1
    # #201: the advance was a non-turn write while the paste sat unsubmitted.
    # Re-baseline so the retry waits for REAL post-Enter progress, then fall
    # through to the same bare-Enter / wedge path a plain non-advance takes.
    before="$(_transcript_mtime "$wt")"
    log "  transcript advanced but the answer never left the composer — NOT delivered (#201)"
  fi
  log "  injected answer did not register — retrying with a bare Enter (never a re-paste)"
  tmux send-keys -t "$target" Enter 2>/dev/null || true
  if _transcript_advanced "$wt" "$before"; then
    _answer_delivered "$wt" "$target" "$text" "$sizes" && return 0
    vetoed=1
  fi
  # Last look before classifying: the bare-Enter submit can land in the same whole
  # second as the re-baseline (the mtime advance never fires) — the appended user
  # record, not the clock, is the truth (#201 review).
  _answer_appended "$wt" "$text" "$sizes" && return 0
  [ "$baseline_shows" -eq 0 ] && _composer_shows_text "$target" "$text" && return 2
  [ "$vetoed" -eq 1 ] && return 3
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
  # Drop the scripted plan artifact too (issue #175): once the gate is answered the plan
  # handoff is spent, and a lingering gate-<N>.md would feed a stale plan to a later re-park.
  rm -f "$(_gate_artifact_path "$wt" "$issue")" 2>/dev/null || true
}

# --- attended QCM surface (issue #155, subtask C) -----------------------------
# In attended mode a human-decision gate is serviced by an INTERACTIVE per-gate context
# that owns present + capture + inject (NOT the hub, which is only NOTIFIED via hub-notify
# #146). The upstream reasoning stays one-shot (run_answerer); when it returns a
# human-decision, _broker_present_qcm renders a structured QCM (summary + reviewer advice
# + freeform escape) on a dedicated per-gate surface, waits for the reviewer's reply HERE,
# and injects it into the spoke via the shared injector — off the pane, off the hub chat.
# One interactive context per gate; it closes after.

# _broker_qcm_dir -> the directory holding per-gate QCM surfaces. GATE_BROKER_QCM_DIR
# overrides (shared with hub-notify.sh, whose gate ping points the human at the surface).
_broker_qcm_dir() { printf '%s\n' "${GATE_BROKER_QCM_DIR:-$(_afk_state_dir)/gate-broker}"; }

# _broker_qcm_surface <issue> -> the per-gate QCM surface path.
_broker_qcm_surface() { printf '%s\n' "$(_broker_qcm_dir)/qcm-$1.md"; }

# _broker_qcm_clear <issue> -> drop a resolved gate's surface.
_broker_qcm_clear() { rm -f "$(_broker_qcm_surface "$1")" 2>/dev/null || true; }

# build_qcm <issue> <summary> <advice> -> write the structured QCM surface: the parked
# prompt (summary — the spoke's own options recommended-first, as it posted them), the
# reviewer's advice, and the freeform-escape instruction. Human-readable + a record, and
# its existence is the flag hub-notify keys the "resolve via QCM" ping on.
build_qcm() {
  local issue="$1" summary="$2" advice="$3" surface
  surface="$(_broker_qcm_surface "$issue")"
  mkdir -p "$(dirname "$surface")" 2>/dev/null || true
  cat > "$surface" <<EOF
# Gate broker · QCM for #$issue

## Summary — what the spoke is parked on

$summary

## Reviewer advice

$advice

## Your decision

Reply with the option you want (the spoke listed its own options above, recommended
first), or type any freeform instruction — it is injected verbatim into the spoke. An
empty reply defers the gate (escalated as blocked/$issue for later).
EOF
}

# _broker_present_qcm <wt> <issue> <advice> -> the ATTENDED human-decision route AND the
# interactive per-gate context: render the QCM, present it, read the reviewer's reply from
# THIS context's stdin, and inject it into the spoke via the shared injector. Empty reply
# -> defer (escalate). The hub only ever gets the hub-notify ping; the resolution happens
# here. UPGRADE: offer the spoke's discrete options as numbered one-key picks (parse them
# out of the summary) once the extract carries structured option labels.
_broker_present_qcm() {
  local wt="$1" issue="$2" advice="$3" summary reply target
  summary="$(extract_pending_question "$wt")"
  [ -n "$summary" ] || summary="(the spoke's parked prompt could not be extracted; decide from the advice + the issue contract)"
  build_qcm "$issue" "$summary" "$advice"
  {
    printf '\n=== Gate broker · #%s — resolve this gate ===\n\n' "$issue"
    printf '## Summary\n%s\n\n' "$summary"
    printf '## Reviewer advice\n%s\n\n' "$advice"
    printf 'Your reply (injected into the spoke; an empty reply defers the gate): '
  } >&2
  # `|| true`, NOT `|| reply=""`: an EOF that arrives right after a newline-less reply
  # returns non-zero with $reply already populated — clobbering it would turn a genuine
  # approval (typed then Ctrl-D) into a spurious block. The [ -z ] below still defers on a
  # truly empty/EOF reply.
  IFS= read -r reply || true
  if [ -z "$reply" ]; then
    _escalate_blocked "$wt" "$issue" "attended reviewer deferred the gate — $advice"
    _broker_qcm_clear "$issue"
    return 0
  fi
  target="$(_spoke_pane_target "$wt")"
  if [ -n "$target" ] && inject_and_verify "$wt" "$target" "$reply"; then
    log "  injected the reviewer's reply into #$issue"
    _consume_gate_tag "$wt" "$issue"
    afk_emit_decision "$wt" success
    _broker_qcm_clear "$issue"
  else
    _escalate_blocked "$wt" "$issue" "attended QCM: could not inject the reviewer's reply into the spoke — needs a human"
    _broker_qcm_clear "$issue"
  fi
  return 0
}

# decide_and_act <wt_path> <issue> -> reason about a parked spoke and act: inject the
# answer, or escalate to blocked/<issue>. Fail-safe: an answerer that returns no decision
# (or an answer we cannot inject) escalates rather than guessing.
broker_service_gate() {
  local wt="$1" issue="$2" mode="${3:-unattended}" question orig_question raw rc decision kind text target was_gate=0 inject_diagnosed=0
  # Self-heal a stale gate tag (issue #204): if gate/<issue> is at the tip but the spoke
  # already resumed past its PLAN gate (a late / external / attended approval that never ran
  # the confirmed-inject path), consume the stale tag and stop — do NOT re-answer, and do NOT
  # count it against the re-answer ceiling (checked BEFORE it, so a resumed spoke heals even
  # once exhausted). The plan-gate-guard self-heals the same signal from the spoke side.
  if _gate_parked "$wt" "$issue" && _gate_answer_landed "$wt"; then
    log "  #$issue resumed past its PLAN gate outside the broker — consuming the stale gate/$issue tag"
    _consume_gate_tag "$wt" "$issue"
    return 0
  fi
  # A prior tick found the reasoner mutated the live tree for this gate (#237): that verdict is
  # terminal on FIRST occurrence — a human is required — and independent of tip/signature, which
  # the mutation itself perturbs. Skip the reasoner entirely; the first-occurrence escalation
  # already stamped blocked/<issue>. Silent here (it was logged once when first voided); a fresh
  # arm clears the marker. Checked before the (tip, sig) ceiling on purpose: keying a void on
  # that ceiling would let the mutation reset it every tick and re-run forever.
  if _broker_gate_voided "$issue"; then return 0; fi
  # Re-answer ceiling (#203 finding 1): a legitimately-escalated spoke parked on the SAME
  # prompt must not re-run the reasoner/classifier every tick forever. After the ceiling on
  # the SAME (tip, prompt-signature) the gate is terminal — it stays blocked/<issue> at the
  # tip from the prior escalation — until the prompt changes or the tip moves. Checked before
  # BOTH the permission path (#203 finding 4's compound dialog) and the answerer path.
  local park_sig; park_sig="$(_broker_park_signature "$wt" "$issue")"
  if _broker_reanswer_exhausted "$wt" "$issue" "$park_sig"; then
    # Log the terminal state ONCE per (tip, sig) — not on every event wake (#237). A moved tip
    # or changed prompt is a new key that resets the ceiling AND logs afresh when it re-exhausts.
    local _tip; _tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)"
    _broker_log_terminal_once "$issue" "ceiling:${_tip}:${park_sig}" \
      "  #$issue re-answer ceiling reached on the same prompt — leaving it terminal (no re-answer)"
    return 0
  fi
  # A pending permission dialog is decided by the rules classifier, not the answerer (#149).
  if _permission_pending "$wt"; then _decide_permission "$wt" "$issue"; return; fi
  # Snapshot the transcript clock BEFORE the park checks: a write landing between
  # this and the pre-inject re-check must count as movement (review nit, ST2).
  local parked_mtime; parked_mtime="$(_transcript_mtime "$wt")"
  _gate_parked "$wt" "$issue" && was_gate=1
  orig_question="$(extract_pending_question "$wt")"
  question="$orig_question"
  if [ "$was_gate" -eq 1 ]; then
    # Route a PLAN-gate park to approve/amend-the-POSTED-PLAN — generic transcript
    # re-extraction is what replayed the seed six times in #124. PREFER the scripted plan
    # artifact (issue #175: a script reads what a script wrote) over transcript extraction;
    # orig_question (the transcript walk) stays as the fallback for an unextractable gate
    # park (rotated transcript, no gate Bash record) or a bare --gate that wrote no artifact.
    local plan; plan="$(_read_gate_artifact "$wt" "$issue")"
    [ -n "$plan" ] || plan="$orig_question"
    question="The spoke is parked at its PLAN gate; below is the plan it posted. Approve it or state precise amendments to it. Do NOT restate or re-issue the task itself.

${plan:-(the plan prose could not be extracted — approve or amend from the issue contract above)}"
  elif [ -z "$question" ]; then
    return 0
  fi
  log "→ answering #$issue (parked on input)"
  # Read-only guard (subtask B): fingerprint the LIVE worktree around the reason step.
  # The reasoner gets read-only access (cwd=wt) to verify against real state; if the tree
  # changed across the step it MUTATED a read-only worktree, so its answer is untrustworthy
  # — void it and route to a human (escalate unattended / QCM attended), regardless of
  # content. Detection is the hard guarantee independent of the LLM's tool-allowlist.
  local fp_before; fp_before="$(_broker_worktree_fingerprint "$wt")"
  raw="$(run_answerer "$issue" "$question" "$wt")"; rc=$?
  if _broker_is_git_worktree "$wt" && [ -z "$fp_before" ]; then
    # Fail SAFE: a git worktree with an empty fingerprint means the fingerprint tooling
    # is unavailable, so we cannot verify the reasoner stayed read-only. Never trust an
    # unverifiable answer — escalate rather than pass.
    log "  could not fingerprint #$issue's worktree to verify read-only — escalating"
    _broker_on_human_decision "$mode" "$wt" "$issue" \
      "could not fingerprint the worktree to verify the reasoner stayed read-only — needs a human"
    return 0
  fi
  if ! _broker_worktree_unchanged "$wt" "$fp_before"; then
    # Stamp the durable void marker FIRST so this gate is terminal on the first occurrence
    # (#237): later ticks short-circuit at the top and never re-run the reasoner. This log
    # fires exactly once — the short-circuit keeps the mutation branch from re-running.
    _broker_mark_voided "$issue"
    log "  reasoner mutated the read-only worktree of #$issue — voiding its answer (terminal; a human is required)"
    _broker_on_human_decision "$mode" "$wt" "$issue" \
      "the gate reasoner mutated the read-only worktree — its answer is voided; needs a human"
    return 0
  fi
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
    # Park freshness gates EVERYTHING: if the spoke moved on while the answerer
    # reasoned, nothing happens regardless of the answer's content — injecting would
    # land mid-turn (#129/#89) and even a seed-replay escalation would stamp a
    # spurious blocked/<issue> on an actively-working spoke.
    if ! _still_parked_same "$wt" "$issue" "$was_gate" "$orig_question" "$parked_mtime"; then
      log "  #$issue is no longer parked on that prompt — dropping the stale answer"
      return 0
    elif _is_seed_replay "$wt" "$text"; then
      log "  answer to #$issue replays the spoke's own seed prompt — suppressing (#124)"
      text="answerer replayed the spoke's seed prompt — suppressed; needs a human"
    else
      target="$(_spoke_pane_target "$wt")"
      if [ -z "$target" ]; then
        text="could not locate spoke pane to inject the answer"
      else
        # Stamp the delivery attempt FIRST: from here until the answer registers the
        # spoke may sit on a buffered answer, and that window must not read as idle.
        stamp_answer_attempt "$issue"
        inject_and_verify "$wt" "$target" "$text"; rc=$?
        if [ "$rc" -eq 0 ]; then
          log "  injected answer into #$issue"
          _consume_gate_tag "$wt" "$issue"
          afk_emit_decision "$wt" success
          return 0
        elif [ "$rc" -eq 2 ] && command -v respawn_wedged_spoke >/dev/null 2>&1 && respawn_wedged_spoke "$wt" "$issue" "$text"; then
          # The wedged composer was recovered by a pane respawn that carries the answer
          # as its --continue prompt — delivered, same success contract as an inject.
          _consume_gate_tag "$wt" "$issue"
          afk_emit_decision "$wt" success
          return 0
        elif [ "$rc" -eq 2 ]; then
          # The old window is dead and the answer text lives nowhere else — carry its
          # head in the blocked reason so the returning human need not re-derive it.
          text="composer wedged and the pane respawn could not be confirmed — needs a human; the undelivered answer began: $(printf '%.120s' "${text%%$'\n'*}")"
          inject_diagnosed=1
        elif [ "$rc" -eq 3 ]; then
          log "  answer to #$issue never left the composer (delivery refuted) — escalating"
          text="answer never left the composer (delivery refuted, #201) — needs a human"
          inject_diagnosed=1
        else
          log "  answer to #$issue did not register — escalating"
          text="answer did not register in the spoke (inject not confirmed) — needs a human"
        fi
      fi
    fi
  elif [ "$kind" = "ESCALATE" ]; then
    [ -n "$text" ] || text="answerer escalated (no reason given)"
  else
    text="answerer returned no decision — escalating for human review"
  fi
  # Park freshness gates the ESCALATE / no-decision / inject-failure escalation too, not just
  # the ANSWER inject (#171-subtask-2): the answerer takes minutes (or timed out), and if the
  # spoke moved on meanwhile (a human replied, the turn resumed) stamping blocked/<N> would
  # strand an actively-working spoke — worse now that a blocked-at-tip park is re-answerable
  # (#171-subtask-3). A late-registered inject also drops here rather than double-escalating.
  # Uses _spoke_moved_on (a POSITIVE transcript-advanced signal), NOT !_still_parked_same:
  # an ambiguous probe must NOT drop a real escalation (review) — only demonstrated activity
  # does. (The ANSWER branch's own pre-inject re-check stays _still_parked_same: there,
  # dropping on uncertainty is the safe direction — it just skips a possibly-stale inject.)
  # EXCEPT when the injector itself diagnosed a wedge/refuted delivery (rc 2/3): there the
  # advance is EXPLAINED by the very non-turn write that triggered the diagnosis, so reading
  # it as "moved on" would drop every #201 escalation and re-paste onto the wedged composer
  # forever, with no blocked/<issue> ever stamped (#201 review, CONFIRMED).
  if [ "$inject_diagnosed" -eq 0 ] && _spoke_moved_on "$wt" "$parked_mtime"; then
    log "  #$issue transcript advanced while reasoning — dropping the escalation (spoke moved on)"
    return 0
  fi
  _broker_on_human_decision "$mode" "$wt" "$issue" "$text"
}

decide_and_act() { broker_service_gate "$1" "$2" unattended; }

# _broker_on_human_decision <mode> <wt> <issue> <reason> -> route a decision that is the
# human's to make (the ONE mode-divergent seam of the shared core). Unattended (/afk):
# escalate to blocked/<issue> so the returning operator resolves it. Attended: present a
# structured QCM on a dedicated per-gate surface (subtask C, #155); until that adapter is
# defined it also escalates, so a parked spoke is never left hanging.
_broker_on_human_decision() {
  local mode="$1" wt="$2" issue="$3" reason="$4"
  if [ "$mode" = attended ] && command -v _broker_present_qcm >/dev/null 2>&1; then
    _broker_present_qcm "$wt" "$issue" "$reason"
    return
  fi
  _escalate_blocked "$wt" "$issue" "$reason"
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

