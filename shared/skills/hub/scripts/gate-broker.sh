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
# would suppress a legitimate idle reap in the next window.
_clear_progress_state() {
  local dir; dir="$(_afk_state_dir)"
  rm -f "$dir"/progress-*.epoch "$dir"/answer-attempt-*.epoch "$dir"/tip-* 2>/dev/null || true
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
# _spoke_idle_seconds <wt_path> <issue> -> idle seconds for the REAPER's clock: since
# the transcript's last write OR the supervisor's last answer-delivery attempt,
# whichever is later — time with a buffered/undelivered answer is not idle (#133;
# the reaper killed #125 right as its answer was delivered). Empty when neither
# reference exists (same "can't measure" contract as _transcript_idle_seconds).
_spoke_idle_seconds() {
  local wt="$1" issue="$2" ref attempt
  ref="$(_transcript_mtime "$wt")"
  attempt="$(read_answer_attempt "$issue")"
  case "$attempt" in
    '' | *[!0-9]*) : ;;
    *) if [ -z "$ref" ] || [ "$attempt" -gt "$ref" ]; then ref="$attempt"; fi ;;
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

# _broker_worktree_fingerprint <wt> -> a content hash of the LIVE worktree: each
# tracked-or-untracked (non-ignored) file's path + its CURRENT working-tree content. A
# new file, a deletion, or a content edit all change it. Empty (stable) for a non-git or
# missing path, so a non-worktree reasoner never trips a false breach.
_broker_worktree_fingerprint() {
  local wt="$1"
  [ -d "$wt" ] || return 0
  (
    cd "$wt" 2>/dev/null || exit 0
    git rev-parse --git-dir >/dev/null 2>&1 || exit 0
    git ls-files -z --cached --others --exclude-standard 2>/dev/null |
      while IFS= read -r -d '' f; do
        printf '%s\0' "$f"
        if [ -f "$f" ]; then git hash-object "$f" 2>/dev/null || printf 'ERR'; else printf 'GONE'; fi
        printf '\0'
      done |
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

# build_answerer_prompt <issue> <question> [wt] -> the full prompt for the reasoner: the
# governing rule, the issue contract, the read-only-worktree posture + evidence contract,
# a decisions-digest of this spoke's prior gate outcomes, and the parked prompt.
# Self-contained so the headless reasoner needs no project context loaded.
build_answerer_prompt() {
  local issue="$1" question="$2" wt="${3:-}" rule body digest at=""
  rule="$(_rule_file)" && rule="$(cat "$rule")" \
    || rule="Answer in the interest of the issue contract and repo conventions; prefer the spoke's own recommended option; escalate (output 'ESCALATE: <reason>') only when the decision is irreversible, outward-facing, or scope-changing. Otherwise output 'ANSWER: <reply>'."
  body="$(gh issue view "$issue" --json title,body -q '.title + "\n\n" + .body' 2>/dev/null || echo "(issue #$issue body unavailable)")"
  digest="$(read_decisions_digest "$issue")"
  [ -n "$wt" ] && at=" at $wt"
  cat <<EOF
$rule

## Issue contract (#$issue)

$body

## Read-only worktree access

You have READ-ONLY access to the spoke's worktree$at (your cwd). Use your read/search
tools to verify the decision against the code as it ACTUALLY is — confirm a command
touches only the spoke's own files, that a posted plan matches real state, and so on.
You must NOT edit, stage, commit, or push anything: the tree is read-only and any write
voids your answer. When you auto-answer, cite the worktree EVIDENCE you checked on an
'EVIDENCE:' line before your final decision line.

## Prior gate decisions for this spoke (decisions-digest)

${digest:-(none recorded yet)}

## The spoke's parked prompt

$question

Decide per the policy above. End your reply with exactly one line: 'ANSWER: <reply>' or 'ESCALATE: <reason>'.
EOF
}

# run_answerer <issue> <question> [wt] -> the reasoner's raw output (stdout AND stderr),
# and its exit status as the function's return code. The reasoner is a headless `claude
# -p` (overridable via AFK_ANSWERER_CMD for tests), run with a thinking budget and a
# READ-ONLY tool allowlist; the prompt is passed on stdin so a long contract never hits
# argv limits. When <wt> is a directory it becomes the reasoner's cwd, so its read-only
# tools verify against the spoke's live state (the mutation guard in broker_service_gate
# is what makes that safe). stderr is folded into the captured stream (NOT discarded)
# because the CLI prints credential failures there and exits nonzero — the auth-failure
# detector needs both the message and the exit code. parse_decision is line-anchored, so
# interleaved stderr noise never pollutes a decision.
run_answerer() {
  local issue="$1" question="$2" wt="${3:-}"
  local prompt; prompt="$(build_answerer_prompt "$issue" "$question" "$wt")"
  local tools; tools="$(reasoner_allowed_tools)"
  local cmd="${AFK_ANSWERER_CMD:-claude -p --model claude-fable-5 --allowedTools '$tools'}"
  if [ -n "$wt" ] && [ -d "$wt" ]; then
    ( cd "$wt" && CLAUDE_EFFORT="$AFK_ANSWERER_EFFORT" bash -c "$cmd" <<<"$prompt" 2>&1 )
  else
    CLAUDE_EFFORT="$AFK_ANSWERER_EFFORT" bash -c "$cmd" <<<"$prompt" 2>&1
  fi
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

# --- the permission classifier (issue #149) -----------------------------------
# A spoke under /afk stalls on Claude Code PERMISSION dialogs (distinct from the
# question/gate parks the answerer handles): the FIRST RED-commit selective stage
# `git reset -q; git add <own file>` prompts and, unanswered, the spoke idles until
# reaped. classify_permission decides such a dialog the way a human would — but by a
# fixed rules table, not the reasoning answerer, since the decision is mechanical and
# must be conservative. It is the unit-tested heart of the supervisor's permission
# handling (the tmux detection + injection that drives it lives in decide_and_act).

# _permission_seg_safe <segment> -> true when ONE command segment is a safe scoped
# self-op the spoke legitimately runs on its OWN worktree: the same vetted class
# worktree-new.sh seeds into the spoke allowlist (unstage/stage, own-file pytest,
# read-only helpers). A segment carrying command substitution, backticks, or a
# redirection is never safe — those could smuggle a destructive op behind a safe
# prefix. `git reset`'s working-tree-mutating modes (`--hard`/`--merge`/`--keep`) are
# rejected before the safe `git reset` prefix matches — only unstage/uncommit is safe.
# Everything unrecognised is unsafe (default-deny).
_permission_seg_safe() {
  local seg="$1"
  case "$seg" in
    *'$('* | *'`'* | *'>'* | *'<'*) return 1 ;;   # substitution / redirection smuggling
  esac
  case "$seg" in
    *'--hard'* | *'--merge'* | *'--keep'*) return 1 ;;  # reset modes that touch the worktree
    'git reset' | 'git reset '* ) return 0 ;;      # unstage/uncommit only — worktree-local
    'git add' | 'git add '* ) return 0 ;;          # stage — worktree-confined
    'git status' | 'git status '* | 'git diff' | 'git diff '* ) return 0 ;;
    'git log' | 'git log '* | 'git show' | 'git show '* ) return 0 ;;
    'git rev-parse' | 'git rev-parse '* | 'git branch --show-current' ) return 0 ;;
    'git fetch' | 'git fetch '* | 'git stash list' ) return 0 ;;
    'pytest' | 'pytest '* ) return 0 ;;
    'python -m pytest' | 'python -m pytest '* ) return 0 ;;
    'python3 -m pytest' | 'python3 -m pytest '* ) return 0 ;;
    '.venv/bin/python -m pytest' | '.venv/bin/python -m pytest '* ) return 0 ;;
    'ls' | 'ls '* | 'cat '* | 'head '* | 'tail '* | 'wc' | 'wc '* ) return 0 ;;
    'grep '* | 'rg '* | 'find '* | 'echo' | 'echo '* | 'tree' | 'tree '* ) return 0 ;;
    'chmod +x '* ) return 0 ;;
    * ) return 1 ;;
  esac
}

# classify_permission <command> -> "APPROVE" or "ESCALATE<TAB><reason>". DEFAULT-DENY:
# the command is APPROVEd only when EVERY segment (split on ; && || |) is a safe scoped
# self-op, so a single risky segment in a chain escalates the whole. Anything unrecognised
# — main-touching, force-push, history rewrite, deletion, network fetch, browser/computer/
# mcp tool, or a bare non-Bash tool name — ESCALATEs, naming the offending command so the
# block record is actionable.
classify_permission() {
  local cmd="$1" norm seg saw_seg=0
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
    if ! _permission_seg_safe "$seg"; then
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
# transcript entry — so it is detected from the pane (the only signal) plus the command the
# spoke is trying to run (its trailing transcript tool_use). classify_permission decides it;
# these helpers see it and deliver the decision. _decide_permission is reached from
# decide_and_act, which routes a permission-pending spoke here instead of to the answerer.

# extract_pending_command <wt_path> -> the command of the spoke's trailing assistant
# tool_use (Bash -> its command string; any other tool -> the tool name, so the classifier
# escalates non-Bash tools like browser/computer/mcp). Empty when unreadable. Mirrors
# extract_pending_question's transcript walk.
extract_pending_command() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  _AFK_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json, os

cmd = ""
try:
    with open(os.environ["_AFK_JSONL"]) as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            content = (obj.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = (block.get("name") or "").strip()
                if name == "Bash":
                    c = ((block.get("input") or {}).get("command") or "").strip()
                    if c:
                        cmd = c
                elif name:
                    cmd = name
except Exception:
    pass
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

# _decide_permission <wt_path> <issue> -> classify the spoke's pending permission dialog and
# act: AUTO-APPROVE a safe scoped self-op (inject "Yes"), or ESCALATE a risky/unreadable one to
# blocked/<issue>. classify_permission is the policy; this is the tmux delivery around it.
_decide_permission() {
  local wt="$1" issue="$2" cmd decision kind reason
  cmd="$(extract_pending_command "$wt")"
  if [ -z "$cmd" ]; then
    _escalate_blocked "$wt" "$issue" "permission dialog with an unreadable command — needs a human"
    return 0
  fi
  decision="$(classify_permission "$cmd")"
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
    _escalate_blocked "$wt" "$issue" "could not deliver permission approval to the spoke — needs a human"
    return 0
  fi
  _escalate_blocked "$wt" "$issue" "$reason — needs a human"
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
  local wt="$1" issue="$2" mode="${3:-unattended}" question orig_question raw rc decision kind text target was_gate=0
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
    # re-extraction is what replayed the seed six times in #124. The fallback keeps an
    # unextractable gate park (rotated transcript, no gate Bash record) answerable
    # instead of silently stranded.
    question="The spoke is parked at its PLAN gate; below is the plan it posted. Approve it or state precise amendments to it. Do NOT restate or re-issue the task itself.

${orig_question:-(the plan prose could not be extracted from the transcript — approve or amend from the issue contract above)}"
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
    log "  reasoner mutated the read-only worktree of #$issue — voiding its answer"
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

