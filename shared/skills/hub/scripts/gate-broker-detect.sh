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
# extract_pending_question <wt_path> -> the prompt the spoke is parked on, or empty when it is NOT
# waiting (so this doubles as the auto-answer trigger). A worker's `ask` arrives as an inbox
# `question` (the PLAN gate: the agent reads `working` while it blocks, so only the inbox shows it);
# a stray AskUserQuestion dialog is the agent `waiting` on that tool, its questions in toolInput.
# Never a transcript parse.
extract_pending_question() {
  local wt="$1" q
  q="$(orca_inbox_question "$wt" body 2>/dev/null)" || q=""
  if [ -n "$q" ]; then printf '%s\n' "${q:0:4000}"; return 0; fi
  [ "$(orca_agent_state "$wt" 2>/dev/null)" = waiting ] || return 0
  [ "$(orca_agent_field "$wt" toolName 2>/dev/null)" = AskUserQuestion ] || return 0
  orca_agent_field "$wt" toolInput 2>/dev/null | jq -r '[.questions[]? | "Q: \(.question // "")\n"
    + ([.options[]? | "  - \(.label // "")" + (if .description then ": \(.description)" else "" end)] | join("\n"))]
    | join("\n\n")' 2>/dev/null | head -c 4000
}
# _pending_question_id <wt> -> the inbox id of the question the spoke is parked on (empty for a
# permission dialog or a stray AskUserQuestion): the recorded id a reply answers.
_pending_question_id() { orca_inbox_question "$1" id 2>/dev/null || true; }

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
# _spoke_agent_dead <wt_path> -> rc 0 ONLY when Orca reports the spoke's worker `exited`. A missing
# record, a failed read, `live` and `unverifiable` are all rc 1: unknown is never a basis for
# recovery (AFK principle #6), and a park signal from a dead worker (a gate/<issue> tag outlives
# the process) is the crash it is, not a live park the answer lane keeps trying to serve (#301).
_spoke_agent_dead() { [ "$(orca_worker_liveness "$1" 2>/dev/null)" = exited ]; }

# _spoke_turn_done <wt_path> -> rc 0 when the spoke's agent finished its turn and is idle at the
# prompt: Orca reports it `done` and no question is waiting in the inbox (the #255 finished-turn-
# idle shape). Such a spoke is NUDGED (a continue message into the live session), not restarted.
# Unknown is rc 1, which falls through to the revive path -- the pre-#365 fail-closed posture.
_spoke_turn_done() {
  [ "$(orca_agent_state "$1" 2>/dev/null)" = done ] && [ -z "$(_pending_question_id "$1")" ]
}

# _afk_note_orca_state <wt> <issue> -> S7 measurement: a lifecycle span for the first agent state the
# drain sees after dispatch (dispatch -> first state), and a `script` span orca:liveness-<verdict> on
# each CHANGE of the worker's liveness. The last-seen values live in per-window marker files, written
# by this function only; best-effort, an emit or marker failure never reaches the tick.
_afk_note_orca_state() {
  local wt="$1" issue="$2" dir st live last d
  command -v _hi_span >/dev/null 2>&1 || return 0
  dir="$(_afk_state_dir)"
  if [ ! -f "$dir/first-state-$issue" ]; then
    st="$(orca_agent_state "$wt" 2>/dev/null)"
    case "$st" in working | waiting | done)
      d="$(read_dispatch_epoch "$issue")"
      _hi_span "$wt" --kind lifecycle --name orca:first-agent-state --phase spawn --status success \
        ${d:+--start-ms "$(( d * 1000 ))"}
      mkdir -p "$dir" 2>/dev/null; : > "$dir/first-state-$issue" 2>/dev/null || true ;;
    esac
  fi
  live="$(orca_worker_liveness "$wt" 2>/dev/null)"
  case "$live" in live | unverifiable | exited) ;; *) return 0 ;; esac
  last="$(cat "$dir/liveness-$issue" 2>/dev/null)"
  [ "$last" = "$live" ] && return 0
  _hi_span "$wt" --kind script --name "orca:liveness-$live" --phase review --status success
  mkdir -p "$dir" 2>/dev/null; printf '%s\n' "$live" > "$dir/liveness-$issue" 2>/dev/null || true
}

# _afk_warn_unknown_state <wt> <issue> <what> -> the LOUD half of "unknown is never a basis for
# action" (principles 2 + 6): when Orca cannot say what a spoke is doing, nothing is recovered, reaped
# or blocked, but the drain WARNS (rate-limited to once per AFK_UNKNOWN_WARN_SECONDS, default 600, so a
# stuck-unknown spoke is visible instead of silently held). Counts nothing and arms no backoff.
_afk_warn_unknown_state() {
  local issue="$2" f now last gap="${AFK_UNKNOWN_WARN_SECONDS:-600}"
  case "$gap" in '' | *[!0-9]*) gap=600 ;; esac
  f="$(_afk_state_dir)/unknown-$issue"; now="$(afk_now)"
  last="$(cat "$f" 2>/dev/null)"
  case "$last" in '' | *[!0-9]*) ;; *) [ "$(( now - last ))" -lt "$gap" ] && return 0 ;; esac
  mkdir -p "$(dirname "$f")" 2>/dev/null; printf '%s\n' "$now" > "$f" 2>/dev/null || true
  command -v broker_warn >/dev/null 2>&1 && broker_warn "$issue" "Orca cannot say what #$issue is doing ($3) -- holding it, taking NO recovery or blocked action"
  return 0
}

# --- the reconciler (issue #304) ----------------------------------------------
# slot_state is a pure READ of the #300 log; where the log DIVERGES from ground truth it heals the
# log by APPENDING a visible actor:reconciler transition, never a silent epoch stamp (#300 principle
# 1). Steady observation — the log already agrees — writes nothing, so a status read is side-effect
# free (#304 AC2). These helpers are the ONLY writers slot_state now invokes.

# _afk_record_reconciled <issue> <to> [episode] -> append a reconciler transition recording ground
# truth <to>, but ONLY when the log tail does not already record it (a divergence). Evidence NAMES
# the divergence so healing is auditable: a cold start (empty log — afk_current_state "unknown") is
# marked lossy (no prior history to trust, #304 AC4); otherwise the stale recorded state is named.
# For a park, a CHANGED episode is a new state even when the state NAME is unchanged. Best-effort:
# no-ops without the log write API (#300 contract) and never fails the caller.
_afk_record_reconciled() {
  local issue="$1" to="$2" episode="${3:-}" cur ev
  command -v afk_tlog_transition >/dev/null 2>&1 || return 0
  command -v afk_current_state >/dev/null 2>&1 || return 0
  cur="$(afk_current_state "$issue" 2>/dev/null)"
  if [ "$cur" = "$to" ]; then
    [ -n "$episode" ] || return 0                                          # settled: no divergence
    [ "$(afk_current_episode "$issue" 2>/dev/null)" = "$episode" ] && return 0
  fi
  if [ "$cur" = unknown ]; then
    ev="{\"lossy\":true,\"actual\":\"$to\"}"                               # cold start, no history
  else
    ev="{\"expected\":\"$cur\",\"actual\":\"$to\"}"
  fi
  afk_tlog_transition "$issue" "$to" reconciler ground-truth "$ev" "$episode"
}

# _afk_note_park_context <wt> <issue> -> seed the park episode context and echo the episode
# "<sig>:<onset>" the recorded `parked` transition carries — the SAME key _gb_episode_key resolves
# and the broker stamps on its answer-lane events, so the transition and the lane events share one
# episode and the watchdog's episode-keyed service reads engage (#304 AC1). Empty episode when
# nothing is extractable (a park with no signature): the transition still records, just without an
# episode, and the detector falls back to the epoch side-channels there (#300).
#
# The park-onset+park-sig pair has ONE owner — note_park_episode (gate-broker-markers.sh) — which
# re-stamps the onset (and credits the closing episode) EXACTLY when the signature changes. The drain
# must NOT write park-sig itself: a decoupled write in the drain's format would front-run
# note_park_episode's prev!=key guard, letting a waiting->waiting episode change slip past it and
# strand a stale onset — the #276/#283 fused-onset false-fire, and an AFK-principle-#5 violation. So
# delegate: stamp-once seeds the onset FLOOR for a signature-less park (note_park_episode no-ops on an
# empty sig), then note_park_episode owns the coupled onset+sig roll-over.
# UPGRADE: note_park_episode re-derives the signature via a pane read slot_state's park probe just
# did — thread the already-read signal through if the extra per-park capture ever matters (#269).
_afk_note_park_context() {
  local wt="$1" issue="$2"
  stamp_park_onset_epoch_once "$issue"
  command -v note_park_episode >/dev/null 2>&1 && note_park_episode "$wt" "$issue" >/dev/null 2>&1
  command -v _gb_episode_key >/dev/null 2>&1 && _gb_episode_key "$issue" 2>/dev/null || true
}

# _afk_reconcile_park <wt> <issue> -> record a live park: seed the episode context, then append the
# visible `parked` transition on a divergence (a new park episode). The single park-recording site
# the four waiting branches of slot_state share.
_afk_reconcile_park() {
  local wt="$1" issue="$2" episode
  episode="$(_afk_note_park_context "$wt" "$issue")"
  _afk_record_reconciled "$issue" parked "$episode"
}

# slot_state <wt_path> <issue> -> done|waiting|reap|busy. Park signals come from Orca, never a
# pane or a transcript: an inbox `question` (a PLAN gate or any worker `ask`; the agent reads
# `working` meanwhile, so agent state alone never classifies a gate park), or the agent `waiting`
# on a permission dialog. A gate/<issue> tag is the durable record, not a park signal.
#   done    — a TERMINAL marker (ready/accept/blocked) at the branch tip.
#   waiting — parked on a question / gate / permission dialog, AGENT ALIVE (auto-answer it; never
#             reaped, regardless of ceiling — park detection precedes both reap verdicts, #246). An
#             exited worker whose park signal lingers is NOT waiting (#301).
#   reap    — over the wall-clock ceiling, or idle past AFK_IDLE_MINUTES, AND with no
#             detectable pending park (a hung/working spoke, not a park).
#   busy    — actively working (or just spawned, no transcript yet).
slot_state() {
  local wt_path="$1" issue="$2" tip marker kind age
  tip="$(git -C "$wt_path" rev-parse HEAD 2>/dev/null)"
  if [ -n "$tip" ]; then
    for kind in ready accept; do
      marker="$(git -C "$wt_path" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)"
      # Record the terminal state so the watchdog's un-landed clock (read_done_epoch, now a log
      # projection) measures from the ready/accepted transition (#263), not a progress epoch that
      # pre-aged during a pre-ready park.
      if [ "$marker" = "$tip" ]; then
        # #274: a fresh ready/accept marker at the tip is genuine progress — drop the stale
        # warned-retry backoff (both lanes), per _afk_clear_warned's "fresh marker → stale" contract.
        # Gated on read_done_epoch (now the log projection) being empty so it fires ONCE on the
        # transition: an unconditional clear each done tick would wipe a land failure's own land-lane
        # backoff and defeat the #241 low-frequency land-retry pacing.
        [ -n "$(read_done_epoch "$issue")" ] || _afk_clear_warned "$issue"
        # #304: record the terminal state as a VISIBLE reconciler transition (read_done_epoch
        # projects its onset) instead of a silent done-epoch stamp. ready -> ready, accept ->
        # accepted (spoke-ready.sh's own log vocabulary).
        case "$kind" in
          ready) _afk_record_reconciled "$issue" ready ;;
          accept) _afk_record_reconciled "$issue" accepted ;;
        esac
        printf 'done\n'; return
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
         && ! _spoke_agent_dead "$wt_path"; then
        _afk_reconcile_park "$wt_path" "$issue"; printf 'waiting\n'; return
      fi
      printf 'done\n'; return
    fi
  fi
  _afk_note_orca_state "$wt_path" "$issue" || true
  # Ledger progress (a tip advance since the last tick) refreshes the ceiling before
  # it is measured — a revived spoke is not re-reaped off its stale dispatch epoch.
  _afk_note_tip_progress "$wt_path" "$issue"
  # Park detection precedes BOTH reaps (#246): an answerable park — a pending question or a
  # permission dialog — is serviced by the answer lane, so it classifies `waiting` however long
  # it has been parked, never `reap`. Pre-#246 the wall-clock ceiling reap ran first, so an
  # over-ceiling permission-parked spoke was reaped + revived (restarted), which only
  # re-raised the identical dialog: parked -> reaped -> revived -> parked forever. The doom-loop a
  # genuinely-stuck dialog could form is bounded NOT here but in the answer lane
  # (broker_service_gate's _broker_reanswer_exhausted / AFK_REANSWER_CEILING + the _afk_warned_arm
  # backoff, escalating to blocked/<issue> on a real judgment call), so park-wins is unconditional.
  # #301: `&& ! _spoke_agent_dead` — a question/dialog left by an agent that has since died is a
  # crash, not a live park. The probe runs only AFTER the cheap park signal is already true (&&
  # short-circuits), so a busy spoke never pays for it.
  if [ -n "$(extract_pending_question "$wt_path")" ] && ! _spoke_agent_dead "$wt_path"; then
    _afk_reconcile_park "$wt_path" "$issue"; printf 'waiting\n'; return
  fi
  # A pending permission dialog (a CC confirmation prompt, no transcript entry) is decided by
  # the supervisor's classifier, so it waits — never reaped as idle (#149) or over-ceiling (#246).
  if _permission_pending "$wt_path" && ! _spoke_agent_dead "$wt_path"; then
    _afk_reconcile_park "$wt_path" "$issue"; printf 'waiting\n'; return
  fi
  # Orca could not say (a failed read, no worker record): unknown is never a basis to drop a park
  # clock or reap a spoke (principle 6) -- hold it busy until Orca answers.
  if [ "$(orca_agent_state "$wt_path" 2>/dev/null)" = unknown ] \
     || { orca_inbox_question "$wt_path" id >/dev/null 2>&1; [ "$?" -eq 2 ]; }; then
    _afk_warn_unknown_state "$wt_path" "$issue" "no readable agent state or inbox"
    printf 'busy\n'; return
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

# _gate_artifact_path <wt> <issue> -> the gate plan artifact path (<wt>/.ai-toolkit/
# gate-<issue>.md). The single owner of that layout, read by _read_gate_artifact; spoke-ready.sh --gate
# writes it and, on an approve, removes it, from the spoke side (#175). Falls
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

# _still_parked_same <wt> <issue> <was_gate> <question> <qid> -> true when the spoke is still parked
# on the SAME prompt the answerer reasoned about. The answerer takes minutes; a spoke that moved on
# meanwhile (a reply landed, the turn resumed) must not receive the stale answer (#129/#89), and a
# spoke now parked on a DIFFERENT question needs a fresh answer, not this one. A question is the
# same while the inbox still holds the recorded <qid>; with no id (a stray AskUserQuestion dialog)
# it is the same while the extracted prompt is unchanged. Read fresh: the tick cache predates the
# reason step, so it is reset first.
_still_parked_same() {
  local wt="$1" question="$4" qid="$5"
  orca_tick_reset
  if [ -n "$qid" ]; then [ "$(_pending_question_id "$wt")" = "$qid" ]; return; fi
  [ -n "$question" ] && [ "$(extract_pending_question "$wt")" = "$question" ]
}

# _spoke_still_parked <wt> <issue> -> true when the spoke is currently parked on SOMETHING (a
# permission dialog or a question / gate) -- not necessarily the SAME prompt as before. #241 §4 uses
# this to tell a genuine park-change (recompute) from a spoke that has MOVED ON and is working (no
# park -> drop, preserving the #89 no-inject-mid-turn guard). A positive park signal, so an
# ambiguous read fails toward "moved on". An exited worker has no live park (#301).
_spoke_still_parked() {
  local wt="$1"
  _spoke_agent_dead "$wt" && return 1
  _permission_pending "$wt" && return 0
  [ -n "$(extract_pending_question "$wt")" ]
}

# _spoke_moved_on <wt> <question> <qid> -> true ONLY on a positive signal that the spoke is no
# longer parked on the prompt the answerer reasoned about: Orca answered (a failed read is NOT
# "moved on") and the recorded question is gone from the inbox (or, with no id, the agent is no
# longer waiting). The escalation freshness-gate (#171-subtask-2) uses it so an ambiguous probe
# never drops a real escalation.
_spoke_moved_on() {
  local wt="$1" question="$2" qid="$3" cur rc=0
  orca_tick_reset
  if [ -n "$qid" ]; then
    cur="$(orca_inbox_question "$wt" id 2>/dev/null)" || rc=$?
    [ "$rc" -eq 2 ] && return 1
    [ "$cur" != "$qid" ]; return
  fi
  case "$(orca_agent_state "$wt" 2>/dev/null)" in
    unknown) return 1 ;;
    waiting) [ "$(extract_pending_question "$wt")" != "$question" ] ;;
    *) return 0 ;;
  esac
}
