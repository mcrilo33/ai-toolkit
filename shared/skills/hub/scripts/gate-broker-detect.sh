#!/usr/bin/env bash
# gate-broker-detect.sh -- split out of gate-broker.sh (issue #275).
#
# A pure function-definition module of the gate-broker core. Sourced by the entry lib
# gate-broker.sh AFTER worktree-lib/hub-inject/log/afk_now and BEFORE any function is
# called, so every cross-module helper resolves at call time. Not run on its own.
set -uo pipefail

# --- transcript helpers (newest .jsonl in the spoke's Claude project dir) -----
# Every stat fallback below probes GNU `-c %Y` FIRST, BSD `-f %m` second (#289, the same
# ordering fix #132 made in worktree-lib.sh). The reverse order breaks on GNU coreutils:
# there `-f` selects filesystem-status mode and takes no inline format, so `%m` is read as
# a file operand -- GNU errors on it yet still PRINTS a multi-line fs block for the real
# file and exits nonzero, so the `||` fallback ALSO runs and the capture holds both the
# garbage and the epoch. BSD instead rejects `-c` cleanly (usage error, empty stdout), so
# GNU-first is the order that fails safe on both.
_transcript_idle_seconds() {
  local jsonl mtime; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  mtime="$(stat -c %Y "$jsonl" 2>/dev/null || stat -f %m "$jsonl" 2>/dev/null)"
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
    mt="$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null)"
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
#
# #241 review N2: folding the answer-attempt epoch into the idle reference is the DELIBERATE
# #133 trade-off — a spoke sitting on a buffered/undelivered answer (or a frozen-but-alive
# claude whose inject didn't land) reads BUSY, so the reaper never kills it mid-delivery. This
# does NOT strand such a spoke: the separate WALL-CLOCK ceiling (_spoke_over_any_ceiling,
# AFK_SPOKE_MAX_MINUTES × the hard multiplier) ignores the answer-attempt fold and still fires,
# and under #241 §7 that ceiling REVIVES the spoke (kill + relaunch) rather than abandoning it.
# So the fold is safe by construction and is intentionally NOT gated on inject success.
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
gate_ids = set()      # tool_use ids of gate emissions, to detect a FAILED one (is_error)
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
                    # A real reply — a human typing in the pane, or the broker's own tmux
                    # inject — is recorded as a TYPED string-content user turn, not a text
                    # block. That reply resolves the PLAN gate, so un-latch gate_plan just as
                    # the list-content text-block branch does (#313). Mirror _gate_answer_landed
                    # (#204): only a typed, non-meta submission counts, so every synthetic
                    # string-content harness turn leaves a still-unanswered park latched.
                    if (isinstance(content, str) and content.strip()
                            and obj.get("promptSource") == "typed" and not obj.get("isMeta")):
                        gate_plan = ""
                continue
            if last_type == "assistant":
                asks, texts, gate_id = [], [], None
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
                            gate_id = block.get("id") or True   # True: a park with no id (fixtures)
                if texts:
                    last_asst_text = "\n".join(t for t in texts if t).strip()
                pending = asks or None
                # A PLAN-gate park = prose plan + a `spoke-ready.sh --gate` Bash, no
                # AskUserQuestion. Remember the plan so the answerer has it to reason about; the
                # emission's tool_result (below) can still un-latch it if it FAILED.
                if gate_id:
                    gate_plan = last_asst_text
                    if gate_id is not True:
                        gate_ids.add(gate_id)
            elif last_type == "user":
                # A gate emission that resolved with is_error (a hook DENY or a script failure)
                # never established a park (issue #271): un-latch the plan so a spoke that keeps
                # working is read as busy, not `waiting` — the phantom park the watchdog answered.
                # This reads Claude Code's current shape: a failed/denied tool_use surfaces as a
                # user-turn tool_result carrying the tool_use_id and a truthy is_error.
                for b in content:
                    if (isinstance(b, dict) and b.get("type") == "tool_result"
                            and b.get("tool_use_id") in gate_ids and b.get("is_error")):
                        gate_plan = ""
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
# _detect_agent_dead <wt_path> -> #301: rc 0 when the spoke's agent is PROVEN gone. A thin,
# GUARDED shim over hub-inject's _spoke_agent_dead: a standalone source of this module (no
# hub-inject in the chain) falls through to "not dead" (rc 1) rather than erroring, preserving
# the pre-#301 park behavior exactly. Every waiting classification below is gated through it so a
# pane whose agent died — but whose scrollback dialog or gate/<issue> tag lingers — is read as the
# crash it is, not a live park the answer lane keeps trying (and failing) to serve.
_detect_agent_dead() {
  declare -F _spoke_agent_dead >/dev/null 2>&1 || return 1
  _spoke_agent_dead "$1"
}

# slot_state <wt_path> <issue> -> done|waiting|reap|busy.
#   done    — a TERMINAL marker (ready/accept/blocked) at the branch tip.
#   waiting — parked on a question / gate / permission dialog, AGENT ALIVE (auto-answer it; never
#             reaped, regardless of ceiling — park detection precedes both reap verdicts, #246). A
#             dead agent whose park signal lingers in scrollback / a git tag is NOT waiting (#301).
#   reap    — over the wall-clock ceiling, or idle past AFK_IDLE_MINUTES, AND with no
#             detectable pending park (a hung/working spoke, not a park).
#   busy    — actively working (or just spawned, no transcript yet).
slot_state() {
  local wt_path="$1" issue="$2" tip marker kind age
  tip="$(git -C "$wt_path" rev-parse HEAD 2>/dev/null)"
  if [ -n "$tip" ]; then
    for kind in ready accept; do
      marker="$(git -C "$wt_path" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)"
      # Stamp the un-landed clock on the FIRST done tick (#263) so the watchdog measures the
      # ceiling from here, not a progress epoch that pre-aged during a pre-ready park.
      if [ "$marker" = "$tip" ]; then
        # #274: a fresh ready/accept marker at the tip is genuine progress — drop the stale
        # warned-retry backoff (both lanes), per _afk_clear_warned's "fresh marker → stale" contract.
        # Gated on the done epoch being unstamped so it fires ONCE on the transition (mirrors
        # stamp_done_epoch_once): an unconditional clear each done tick would wipe a land failure's
        # own land-lane backoff and defeat the #241 low-frequency land-retry pacing.
        [ -n "$(read_done_epoch "$issue")" ] || _afk_clear_warned "$issue"
        stamp_done_epoch_once "$issue"; printf 'done\n'; return
      fi
    done
    # blocked/<issue> at the tip is terminal ONLY if the spoke is not still parked. A
    # spurious blocked/<N> (a false escalation) over a spoke still on a question / permission
    # dialog would otherwise strand it — read as done, never re-answered, never reaped until
    # the window ends (#171-subtask-3). If it is still parked on an extractable prompt, read
    # it as waiting (re-answerable); reconcile_markers keeps clearing the tag once commits
    # land on top.
    if [ "$(git -C "$wt_path" rev-parse -q --verify "refs/tags/blocked/${issue}^{commit}" 2>/dev/null)" = "$tip" ]; then
      # …still parked on a live agent ⇒ re-answerable. A DEAD agent's lingering dialog is NOT a
      # live park: fall through to `done` (blocked is terminal; a human already owns it — never
      # auto-revived over the escalation, unlike the gate/question cases below).
      if { [ -n "$(extract_pending_question "$wt_path")" ] || _permission_pending "$wt_path"; } \
         && ! _detect_agent_dead "$wt_path"; then
        stamp_park_onset_epoch_once "$issue"; printf 'waiting\n'; return
      fi
      printf 'done\n'; return
    fi
    # A pushed gate/<issue> at the tip = parked at the PLAN gate → waiting, never reaped.
    # The gate is a prose plan + this tag (no AskUserQuestion), so extract_pending_question
    # can't see it. Checking at the tip is self-clearing: once approved and the spoke
    # commits its first RED/GREEN, the tip moves past the gate commit and it reads busy.
    if [ "$(git -C "$wt_path" rev-parse -q --verify "refs/tags/gate/${issue}^{commit}" 2>/dev/null)" = "$tip" ]; then
      # #301: a gate/<issue> tag OUTLIVES the process (a git tag is the most durable phantom-park
      # source there is), so the #296/#299 crash — agent dead, tag still at the tip — kept reading
      # `waiting` and was never revived. Only a LIVE agent at the gate is a real park; a dead one
      # falls through to busy/reap so recover_dead_panes revives it in place.
      if ! _detect_agent_dead "$wt_path"; then
        stamp_park_onset_epoch_once "$issue"; printf 'waiting\n'; return
      fi
    fi
  fi
  # Ledger progress (a tip advance since the last tick) refreshes the ceiling before
  # it is measured — a revived spoke is not re-reaped off its stale dispatch epoch.
  _afk_note_tip_progress "$wt_path" "$issue"
  # Park detection precedes BOTH reaps (#246): an answerable park — a pending question or a
  # permission dialog — is serviced by the answer lane, so it classifies `waiting` however long
  # it has been parked, never `reap`. Pre-#246 the wall-clock ceiling reap ran first, so an
  # over-ceiling permission-parked spoke was reaped + revived (claude --continue), which only
  # re-raised the identical dialog: parked -> reaped -> revived -> parked forever. The doom-loop a
  # genuinely-stuck dialog could form is bounded NOT here but in the answer lane
  # (broker_service_gate's _broker_reanswer_exhausted / AFK_REANSWER_CEILING + the _afk_warned_arm
  # backoff, escalating to blocked/<issue> on a real judgment call), so park-wins is unconditional.
  # #301: `&& ! _detect_agent_dead` — a question/dialog left by an agent that has since died is a
  # crash, not a live park. The probe runs only AFTER the cheap park signal is already true (&&
  # short-circuits), so a busy spoke never pays for it.
  if [ -n "$(extract_pending_question "$wt_path")" ] && ! _detect_agent_dead "$wt_path"; then
    stamp_park_onset_epoch_once "$issue"; printf 'waiting\n'; return
  fi
  # A pending permission dialog (a CC confirmation prompt, no transcript entry) is decided by
  # the supervisor's classifier, so it waits — never reaped as idle (#149) or over-ceiling (#246).
  if _permission_pending "$wt_path" && ! _detect_agent_dead "$wt_path"; then
    stamp_park_onset_epoch_once "$issue"; printf 'waiting\n'; return
  fi
  # Past every park check ⇒ the spoke is NOT parked (busy/reap). Reset its park-onset clock so a
  # later re-park measures the watchdog's park-unanswered ceiling from the NEW onset, not a stale
  # one (#265). Placed here, not in _afk_note_tip_progress above: that runs BEFORE the two waiting
  # returns just above, so clearing there would clear-then-restamp a still-parked spoke every tick.
  clear_park_onset_epoch "$issue"
  if _spoke_over_any_ceiling "$issue" "$(afk_now)"; then printf 'reap\n'; return; fi
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

# _spoke_still_parked <wt> <issue> -> true when the spoke is currently parked on SOMETHING (a
# permission dialog, a PLAN gate, or an extractable question) — regardless of whether it is the
# SAME prompt as before. #241 §4 uses this to tell a genuine park-change (recompute) from a
# spoke that has MOVED ON and is actively working (no park → drop, preserving the #89 no-inject
# -mid-turn guard). A positive park signal, so an ambiguous read fails toward "moved on" (drop).
_spoke_still_parked() {
  local wt="$1" issue="$2"
  # #301: a dead agent has no LIVE park to service — its dialog / gate tag is scrollback and git
  # state that outlived the process. Fail toward "moved on" (not parked) so _reap_or_resume
  # REVIVES it rather than routing it back to the answerer, which would only inject into a shell.
  _detect_agent_dead "$wt" && return 1
  _permission_pending "$wt" && return 0
  _gate_parked "$wt" "$issue" && return 0
  [ -n "$(extract_pending_question "$wt")" ]
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
