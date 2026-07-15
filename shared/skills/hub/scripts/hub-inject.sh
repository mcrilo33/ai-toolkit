#!/usr/bin/env bash
# hub-inject.sh — the ONE hardened tmux-inject + delivery-proof primitive (issue #251).
#
# Factored out of gate-broker.sh so BOTH the unattended /afk answerer (which sources this
# via gate-broker.sh) AND the tier-2 hub-watchdog (which sources it directly) drive spoke
# panes through a single tested helper. The paste-swallow / QCM-digit / bare-Enter-retry
# gotchas are fixed once, in one place, and can never drift between the two callers.
#
# The unit is "read a spoke's transcript state and inject into its pane":
#   * transcript locators  — _spoke_project_dir / _spoke_jsonl / _transcript_mtime
#   * transcript state     — _transcript_finished_turn_idle (the #255 finished-turn-idle read)
#   * pane keystrokes      — _spoke_pane_target / inject_answer / _composer_shows_text
#   * delivery proof       — _transcript_sizes / _answer_appended / _answer_delivered /
#                            _transcript_advanced
#   * the wrapper          — inject_and_verify (Esc-first menu cancel, send-keys -l, a
#                            SEPARATE Enter, a bare-Enter retry that NEVER re-pastes)
#   * permission dialog    — _pane_shows_permission_prompt / approve_permission / _deny_permission
#
# Sourceable on its own (the tests + the watchdog do). Run directly it exposes a thin CLI
# (inject / approve / deny / pane-target / permission?) so a caller without a bash-source
# seam can drive one action as an allowlistable subprocess.
set -uo pipefail

HUB_INJECT_SCRIPT_DIR="${HUB_INJECT_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# worktree-lib.sh gives us wt_realpath (pane-path canonicalization). Source it only when a
# parent has not already (gate-broker sources it before us); same dual-layout ladder as our
# siblings. HUB_INJECT_WT_LIB / AFK_WT_LIB win for tests.
if ! declare -F wt_realpath >/dev/null 2>&1; then
  _hi_top="${_AFK_TOPLEVEL:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
  for _cand in \
    "${HUB_INJECT_WT_LIB:-}" \
    "${AFK_WT_LIB:-}" \
    "$HUB_INJECT_SCRIPT_DIR/worktree-lib.sh" \
    "$HUB_INJECT_SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
    "${_hi_top:+$_hi_top/scripts/worktree-lib.sh}" \
    "${_hi_top:+$_hi_top/.ai-toolkit/scripts/worktree-lib.sh}"; do
    if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
  done
  unset _cand _hi_top
fi

# Guarded log fallback: gate-broker.sh defines its own log() before sourcing us, so this
# only fires for a standalone source (the watchdog / tests). Same stderr contract.
declare -F log >/dev/null 2>&1 || log() { printf '%s\n' "$*" >&2; }

# === the moved primitives (verbatim from gate-broker.sh, issue #251) ==========

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
# _transcript_mtime <wt_path> -> epoch mtime of the spoke's newest transcript, or empty.
# The registration signal for inject verification: it bumps when the spoke writes its
# next turn after an injected answer is submitted.
_transcript_mtime() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 0
  stat -f %m "$jsonl" 2>/dev/null || stat -c %Y "$jsonl" 2>/dev/null
}
# _transcript_finished_turn_idle <wt_path> -> rc 0 when the spoke's newest transcript ends
# with a COMPLETED assistant turn (no pending tool_use): the finished-turn-idle shape (#255).
# The spoke finished its turn and stopped at the input prompt without continuing the cycle --
# distinct from a pane frozen MID-TOOL_USE (a trailing unresolved tool_use, incl. the #240
# flushed-tool_use shape), which reads as rc 1 so the reaper revives it (kill + relaunch),
# while a finished-turn-idle spoke is nudged (a continue message injected into the LIVE session).
# Only a GENUINE typed user reply counts as the last turn: a trailing SYNTHETIC user write --
# a tool_result, a task-notification / system-reminder, or a skill/meta turn -- is skipped (the
# promptSource convention the sibling readers use), so a spoke that finished its turn and then
# received such a write is still nudge-able. Fail-CLOSED (rc 1) on no transcript / no
# python3 / parse error: an unprovable state falls through to the existing revive, preserving
# prior behavior. The pane-at-prompt + no-dialog half of the signal is already guaranteed by the
# reaper's upstream checks (_spoke_still_parked and _permission_pending are both false by then).
_transcript_finished_turn_idle() {
  local jsonl; jsonl="$(_spoke_jsonl "$1")"
  [ -n "$jsonl" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_JSONL="$jsonl" python3 2>/dev/null <<'PYEOF'
import json, os, sys

last_turn_type = None            # type of the last genuine user/assistant turn
last_assistant_has_tool_use = False
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
            if ttype not in ("user", "assistant"):
                continue          # notification / summary / system are not turns
            if ttype == "user" and (obj.get("promptSource") != "typed" or obj.get("isMeta")):
                # Only a GENUINE typed reply (a human in the pane, or the injector) is a real
                # user turn -- the same promptSource convention the sibling readers use
                # (gate-broker _gate_answer_landed, plan-gate-guard). A synthetic user turn --
                # a tool_result, a task-notification / system-reminder, or a skill/meta turn --
                # does NOT mean the spoke moved on, so it is skipped: a finished-turn-idle spoke
                # whose trailing turn is such a synthetic write is still nudge-able.
                continue
            last_turn_type = ttype
            if ttype == "assistant":
                content = (obj.get("message") or {}).get("content")
                blocks = content if isinstance(content, list) else []
                last_assistant_has_tool_use = any(
                    isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks
                )
except Exception:
    sys.exit(1)
# Finished-turn-idle: the last genuine turn is an assistant message that ended without a
# pending tool_use. Anything else is not proven idle at the prompt.
sys.exit(0 if last_turn_type == "assistant" and not last_assistant_has_tool_use else 1)
PYEOF
}
# _answer_needle <text> -> the shared delivery needle: the first ~40 chars of the
# answer's first line. One derivation feeds both delivery proofs (_composer_shows_text,
# _answer_appended) so the pane and transcript checks can never grep diverging strings.
_answer_needle() {
  local needle="${1%%$'\n'*}"
  printf '%s\n' "${needle:0:40}"
}
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
# _answer_delivered <wt> <text> <sizes> -> after _transcript_advanced succeeded, decide
# whether the answer actually LEFT the composer. The needle landing in an APPENDED
# type:"user" record is the SOLE positive proof (#281); a transcript advance is not, because
# the advance can be the injector's OWN doing.
#   rc 0 — the needle landed in appended bytes as a user record: delivered.
#   rc 0 — the scan is UNAVAILABLE (no python3 / no project dir): no evidence either way, so
#          keep the pre-#201 contract (advance alone = delivered) rather than escalate on
#          every degraded env.
#   rc 1 — the scan RAN and the needle is NOT there: NOT delivered, whatever the pane says.
#
# #281 removed the composer read from this decision. It used to rescue a not-found scan to
# "delivered" whenever the pane did not show the needle — two independent false-negatives
# that ANDed into a false positive: inject_answer's Esc-first menu-cancel (#74) DECLINES a
# spoke's live AskUserQuestion, and Claude Code writes that decline as its own type:"user"
# turn, so the transcript advances without the answer ever being submitted; meanwhile
# capture-pane wraps a 40-char needle across lines, so grep -qF misses the paste sitting in
# the composer. The drain logged "injected answer into #271" four times against an answer
# nobody read. The pane read still classifies wedge-vs-refuted in inject_and_verify — it is
# just no longer allowed to manufacture a delivery.
#
# The accepted cost (#281): a genuine submit whose needle the scan cannot find now takes one
# stray bare Enter and escalates, where it used to pass. A real submit records the needle in
# a user record and gets three scan chances (initial, post-grace, post-retry), and the retry
# is a bare Enter that never re-pastes (#133) — so the trade is a rare extra Enter against
# never again reporting an unread answer as delivered.
_answer_delivered() {
  local wt="$1" text="$2" sizes="$3" rc
  _answer_appended "$wt" "$text" "$sizes"; rc=$?
  [ "$rc" -eq 0 ] && return 0
  [ "$rc" -eq 2 ] && return 0
  return 1
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
    _answer_delivered "$wt" "$text" "$sizes" && return 0
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
    _answer_delivered "$wt" "$text" "$sizes" && return 0
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

# === CLI ======================================================================
# Direct invocation drives ONE action as an allowlistable subprocess; sourced use (the
# answerer via gate-broker, the watchdog, the tests) skips this and calls the funcs directly.
_hub_inject_cli() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    inject)
      local wt="${1:?wt required}" text="${2:?text required}" target
      target="$(_spoke_pane_target "$wt")"
      [ -n "$target" ] || { log "hub-inject: no pane for $wt"; return 1; }
      inject_and_verify "$wt" "$target" "$text" ;;
    approve)       approve_permission "${1:?wt required}" ;;
    deny)          _deny_permission "${1:?wt required}" "${2:?guidance required}" ;;
    pane-target)   _spoke_pane_target "${1:?wt required}" ;;
    permission?)   _pane_shows_permission_prompt "${1:?wt required}" ;;
    -h|--help|"")  sed -n '2,22p' "$0" ;;
    *)             log "hub-inject: unknown command '$cmd'"; return 2 ;;
  esac
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _hub_inject_cli "$@"
fi
