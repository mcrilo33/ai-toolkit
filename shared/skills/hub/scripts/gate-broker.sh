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

# Our OWN location, ALWAYS from THIS file's BASH_SOURCE -- never the inherited SCRIPT_DIR,
# which the /afk self-copy supervisor sets to a temp dir holding only hub-afk.sh (#262). The
# sibling-source blocks below resolve from here FIRST so a co-located hub-inject.sh (and the
# checkout's ../../../../scripts/ tree) is found regardless of who sourced us or what
# SCRIPT_DIR they passed down.
_GB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
  "$_GB_DIR/worktree-lib.sh" \
  "$_GB_DIR/../../../../scripts/worktree-lib.sh" \
  "$SCRIPT_DIR/worktree-lib.sh" \
  "$SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/scripts/worktree-lib.sh}" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/worktree-lib.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

log() { printf '%s\n' "$*" >&2; }

# --- source hub-inject.sh (the ONE hardened tmux-inject + delivery-proof unit) -
# The spoke-pane injection + transcript-delivery primitives (issue #251) live in
# hub-inject.sh so the /afk answerer (us) and the tier-2 hub-watchdog share one tested
# helper. Always a co-located sibling — in the checkout AND a synced .ai-toolkit/scripts/
# target — so it resolves from $_GB_DIR (OUR own dir) regardless of the inherited SCRIPT_DIR.
# The _AFK_TOPLEVEL fallbacks mirror the worktree-lib block; without them a self-copy
# supervisor (SCRIPT_DIR = a temp dir with only hub-afk.sh) left every moved helper undefined
# and the drain serviced nothing (#262). AFK_HUB_INJECT wins for tests. Sourced AFTER
# log()/worktree-lib so its guarded fallbacks defer to ours.
for _cand in \
  "${AFK_HUB_INJECT:-}" \
  "$_GB_DIR/hub-inject.sh" \
  "$SCRIPT_DIR/hub-inject.sh" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/shared/skills/hub/scripts/hub-inject.sh}" \
  "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/hub-inject.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

# --- now-clock ----------------------------------------------------------------
# Current time, overridable via AFK_NOW for tests/cron.
afk_now() { printf '%s\n' "${AFK_NOW:-$(date +%s)}"; }

# --- source the gate-broker functional modules (fail-CLOSED per #211) ----------
# The core is split into gate-broker-<stage>.sh modules (issue #275) so disjoint afk
# subtasks stop colliding on one multi-thousand-line Scope: token. These are pure
# function-definition files, sourced AFTER worktree-lib/hub-inject/log/afk_now and BEFORE
# any function is called; markers is first (it owns _afk_state_dir, read by every module's
# helpers at call time).
#
# FAIL-CLOSED (constraint 3 / #211): the modules back the deny-wall (classify_danger /
# judge_permission / afk_danger_guard_decide). The sibling-source loops above skip a missing
# candidate SILENTLY (fail-open); applied here a missing module would leave the wall partial
# and the shim (source ... || exit 0) would drop the wall entirely (the #262 drain-serviced-
# nothing + unguarded-bypass-spoke failure). So: an explicit [ -r ] guard before each source
# (source of a missing file can exit the shell as a special builtin, escaping traps), and on
# ANY miss a self-contained deny-wall override is installed at the END of this file (after
# every real definition, so it wins).
_GB_MODULES_OK=1
for _mod in markers detect classify danger answerer permission; do
  _gbm=""
  for _cand in \
    "$_GB_DIR/gate-broker-$_mod.sh" \
    "$SCRIPT_DIR/gate-broker-$_mod.sh" \
    "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/shared/skills/hub/scripts/gate-broker-$_mod.sh}" \
    "${_AFK_TOPLEVEL:+$_AFK_TOPLEVEL/.ai-toolkit/scripts/gate-broker-$_mod.sh}"; do
    if [ -n "$_cand" ] && [ -r "$_cand" ]; then _gbm="$_cand"; break; fi
  done
  if [ -n "$_gbm" ]; then
    . "$_gbm"
  else
    log "gate-broker: FATAL required module gate-broker-$_mod.sh missing/unreadable -- failing CLOSED (deny-wall held up)"
    _GB_MODULES_OK=0
  fi
done
unset _mod _gbm _cand _GB_DIR


# --- tmux injection + telemetry -----------------------------------------------

# _scan_appended_turns <wt_path> <sizes> <mode> -> scan the transcript bytes APPENDED after the
# <sizes> snapshot for a matching record. <mode> selects the filter:
#   typed    — ONLY a genuine typed prompt submission (type:"user", promptSource=="typed", not
#              isMeta); SYNTHETIC harness user turns (tool_results, <system-reminder> /
#              <task-notification>, skill/meta, SDK/system) do NOT match — the #240 non-turn class.
#   activity — the above OR the spoke's OWN assistant work (an assistant record with a tool_use,
#              e.g. Edit/Write/Bash, or non-empty text).
# rc 0 a match landed, rc 1 none, rc 2 unavailable (no python3 / no project dir / interpreter crash).
# Only appended regions are read (offset from <sizes>); a rotated/truncated file rescans from 0.
_scan_appended_turns() {
  local wt="$1" sizes="$2" mode="$3" dir
  dir="$(_spoke_project_dir "$wt")"
  [ -d "$dir" ] || return 2
  command -v python3 >/dev/null 2>&1 || return 2
  _AFK_DIR="$dir" _AFK_SIZES="$sizes" _AFK_MODE="$mode" python3 2>/dev/null <<'PYEOF'
import glob, json, os, sys

mode = os.environ.get("_AFK_MODE", "activity")
offsets = {}
for line in os.environb.get(b"_AFK_SIZES", b"").splitlines():
    size, _, path = line.partition(b"\t")
    if path:
        try:
            offsets[os.fsdecode(path)] = int(size)
        except ValueError:
            pass


def matches(record):
    if not isinstance(record, dict):
        return False
    # `any` mode: ANY appended record is a spoke-side write — the ISOLATED reasoner (#237 cwd=snap,
    # --no-session-persistence) never writes the live transcript, so a #240 tool_result-only
    # self-resume still proves the spoke touched the tree (#247 residual 2, the fail-safe's DROP arm).
    if mode == "any":
        return True
    kind = record.get("type")
    # A genuine typed human/self reply — shared by both modes (mirrors _gate_answer_landed).
    if kind == "user" and record.get("promptSource") == "typed" and not record.get("isMeta"):
        return True
    if mode == "typed":
        return False
    # activity mode also counts the spoke's OWN assistant work: a tool_use (it ran Edit/Write/
    # Bash) or a non-empty text turn. The isolated reasoner never writes the live transcript.
    if kind == "assistant":
        msg = record.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    return True
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    return True
    return False


for path in glob.glob(os.path.join(os.environ["_AFK_DIR"], "*.jsonl")):
    try:
        with open(path, "rb") as fh:
            offset = offsets.get(path, 0)
            fh.seek(0, 2)
            if offset > fh.tell():  # rotated/truncated since the snapshot
                # typed mode fails toward a from-0 rescan (fail-toward-pre-#201). activity mode must
                # NOT: a from-0 scan would match the PRE-park record (an AskUserQuestion IS an
                # assistant tool_use) and mask a real escape (rc 0 -> drop). Skip the file instead,
                # so the caller reads "no activity" (rc 1) and fails SAFE (voids) on a lost boundary.
                if mode in ("activity", "any"):
                    continue
                offset = 0
            fh.seek(offset)
            appended = fh.read()
    except OSError:
        continue
    for line in appended.splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        if matches(record):
            sys.exit(0)
sys.exit(3)
PYEOF
  case $? in 0) return 0 ;; 3) return 1 ;; *) return 2 ;; esac
}

# _user_turn_appended <wt_path> <sizes> -> did a GENUINE typed reply land in transcript bytes
# appended after the <sizes> snapshot? The "the spoke MOVED ON" signal (#241 §4). The staleness
# recompute gates on the DEFINITE "no genuine reply" (rc 1); rc 0 (a typed reply landed) or rc 2
# (cannot tell) both fall to the #89-safe drop. rc 0 found, rc 1 none, rc 2 unavailable.
_user_turn_appended() { _scan_appended_turns "$1" "$2" typed; }

# _spoke_activity_appended <wt_path> <sizes> -> did a GENUINE spoke turn (a typed reply OR the
# spoke's OWN assistant work) land in appended transcript bytes? The read-only void's #244
# discriminator: a spoke that self-resumed mid-GREEN and edited its own tree ALWAYS leaves a turn
# here, while the isolated reasoner (#237 cwd=snap, --no-session-persistence) never writes the live
# transcript and a #240 non-turn bump / a HEAD-moving commit-escape is NOT a spoke turn. So a tree
# diff with NO activity (rc 1) is a reasoner escape (VOID). rc 2 (cannot scan) is treated as a
# breach too — fail SAFE, mirroring the unverifiable-fingerprint escalation. rc 0 found, rc 1 none,
# rc 2 unavailable (no python3 / no project dir / crash).
_spoke_activity_appended() { _scan_appended_turns "$1" "$2" activity; }

# _spoke_touched_transcript <wt_path> <sizes> -> did the spoke append ANY record to its live
# transcript since the <sizes> snapshot? The #247 fail-safe's positive spoke signal: a weaker bar
# than a full "turn" (it also counts a #240 tool_result-only self-resume — residual 2), sound
# because the ISOLATED reasoner never writes the live transcript, so any appended record is the
# spoke. Used when the reasoner audit did NOT prove a write (rw_rc 1/2): DROP on a positive touch,
# else fail SAFE and VOID. rc 0 touched, rc 1 none, rc 2 unavailable (no python3 / no project dir).
_spoke_touched_transcript() { _scan_appended_turns "$1" "$2" any; }

# _reasoner_wrote_live_tree <raw-answerer-output> <wt> -> the #247 option (c) attribution
# primitive: audit the REASONER's OWN tool_use stream (from `--output-format stream-json`) for a
# write that could reach the LIVE tree, instead of attributing a whole-tree diff by the spoke's
# transcript (the #244 discriminator, which is leaky at the edges because the diff carries no
# evidence of WHO wrote it). Since #237 the reasoner runs in a snapshot COPY (cwd=snap), so its
# RELATIVE writes land in the copy and never touch the live tree; the ONLY live-tree vector is an
# ABSOLUTE path under <wt> (a write tool targeting $wt/…, or a mutating Bash referencing the
# absolute $wt path / `git -C $wt`). This scans for exactly that:
#   - a write tool (Write/Edit/MultiEdit/NotebookEdit) whose path input is absolute and under <wt>;
#   - a Bash whose command references the absolute <wt> path AND is NOT a read-only git verb
#     (mirrors _reasoner_bash_readonly, and also recognises the `git -C <wt> <verb>` form).
# rc 0 a live-tree write is PROVEN (VOID even amid coincident spoke activity — closes residual 1);
# rc 1 the stream parsed but shows NO modelled live-tree write; rc 2 the input is not an auditable
# stream (a plain-text answerer stub / no stream / no python3). The caller treats ONLY rc 0 as proof:
# on rc 1 / rc 2 it does NOT trust the audit alone (an escape via a vector this does not model must
# still fail SAFE), so it attributes the diff to the spoke ONLY on a positive spoke-transcript signal
# and otherwise VOIDs. The raw stream is delivered via a temp FILE (not argv/env) so a verbose
# stream that echoes large tool_result payloads never trips ARG_MAX. Uses python3 like the scanners.
_reasoner_wrote_live_tree() {
  local raw="$1" wt="$2" rawfile rc
  command -v python3 >/dev/null 2>&1 || return 2
  # Deliver the raw stream via a temp FILE (path in env), never in argv/env directly: a verbose
  # stream that echoes large tool_result payloads would trip ARG_MAX. mktemp mirrors run_answerer.
  rawfile="$(mktemp 2>/dev/null)" || return 2
  printf '%s' "$raw" > "$rawfile"
  _AFK_WT="$wt" _AFK_RAWFILE="$rawfile" python3 2>/dev/null <<'PYEOF'
import json, os, re, sys

wt = os.environ.get("_AFK_WT", "")
cands = {c for c in (wt, os.path.realpath(wt) if wt else "") if c}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
RO_GIT = ("status", "diff", "log", "show", "rev-parse", "branch", "ls-files", "cat-file")


def path_under_wt(p):
    if not isinstance(p, str) or not p.startswith("/"):
        return False  # a relative path writes into the #237 snapshot copy, not the live tree
    # Compare both the raw and the symlink-resolved form so a /tmp-vs-/private/tmp alias (or any
    # symlinked component) on either side still matches — cands already carries realpath(wt).
    forms = {p}
    try:
        forms.add(os.path.realpath(p))
    except Exception:
        pass
    return any(f == c or f.startswith(c.rstrip("/") + "/") for f in forms for c in cands)


def references_wt(text):
    # Boundary-aware: <wt> followed by a non-path char (/ , whitespace, quote, EOL) — never a bare
    # substring, so a SIBLING worktree like `<wt>-2` / `<wt>.bak` is not mistaken for the live tree.
    return any(re.search(re.escape(c) + r"(?![\w.-])", text) for c in cands)


def bash_mutates_wt(cmd):
    if not isinstance(cmd, str) or not references_wt(cmd):
        return False  # does not reference the absolute live-tree path at all
    # Command chaining / an output redirect could smuggle a write past a leading read-only verb
    # (`git -C $wt status && rm $wt/x`, `... > $wt/x`) — treat such a compound as a mutation. A bare
    # pipe is NOT included: `git -C $wt log | head` stays a read. (The reasoner is never told $wt's
    # absolute path — #239 — so ANY command referencing it is already off-posture; voiding is safe.)
    if any(t in cmd for t in (">", ";", "&&", "||", "$(", "`")):
        return True
    m = re.match(r"git\s+(?:-C\s+\S+\s+)?(\S+)", cmd.strip())
    if m and m.group(1) in RO_GIT:
        return False  # a LONE read-only `git [-C <wt>] status/diff/…` inspection — cannot mutate
    return True  # any other command referencing the absolute live path is a potential live write


saw_stream = False
with open(os.environ["_AFK_RAWFILE"], encoding="utf-8", errors="replace") as fh:
    for raw_line in fh:
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue  # non-JSON (a plain-text answerer stub) — not an auditable stream event
        if not isinstance(obj, dict):
            continue
        if obj.get("type") in ("system", "assistant", "user", "result"):
            saw_stream = True
        if obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            inp = block.get("input") or {}
            if not isinstance(inp, dict):
                continue
            if name in WRITE_TOOLS and any(path_under_wt(inp.get(k)) for k in ("file_path", "path", "notebook_path")):
                sys.exit(0)
            if name == "Bash" and bash_mutates_wt(inp.get("command")):
                sys.exit(0)
sys.exit(3 if saw_stream else 4)
PYEOF
  rc=$?
  rm -f "$rawfile" 2>/dev/null || true
  case "$rc" in 0) return 0 ;; 3) return 1 ;; *) return 2 ;; esac
}

# afk_emit_decision <wt_path> <status> -> one kind=agent span per auto-answer decision,
# attributed to the SPOKE (emit with the worktree as CWD, like worktree-lib does), so the
# decision surfaces on the observability dashboard. Metadata only — the question→answer
# text rides the answerer's own sidecar session (the dashboard's node summary), never the
# span (the telemetry privacy contract logs no payload). No-op when telemetry is off.
# _afk_emit_span <wt> <name> <status> -> the shared one-span emitter (kind=agent, phase
# review), attributed to the spoke. No-op when telemetry is off or the worktree is gone.
# <status> is normally success|warn|deny; #277 adds `fast-path` for a WAIVED PLAN gate
# (auto-approved without the reasoner) so the waive is distinguishable in the trace and never
# silently folded into a normal answer span.
_afk_emit_span() {
  command -v telemetry_emit_span >/dev/null 2>&1 || return 0
  local wt="$1" name="$2" status="$3"
  [ -d "$wt" ] || return 0
  ( cd "$wt" && telemetry_emit_span --kind agent --name "$name" --phase review --status "$status" ) || true
  return 0
}
afk_emit_decision() { _afk_emit_span "$1" afk-answer "$2"; }


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

# --- PLAN-gate fast-path (issue #277) -----------------------------------------
# Most drain PLAN gates are bug-scoper-filed issues that already carry root-cause mechanics
# and fix directions; the spoke's posted plan is then a near-restatement of that same body,
# and spending a full high-effort run_answerer round trip (3-5 min, plus the #265/#271
# park-window watchdog exposure) on it adds nothing. When the posted plan is substantively a
# RESTATEMENT of the issue body we WAIVE the reasoner and auto-approve here, and record the
# waive so the hub stays AWARE of it: a park:gate journal line + gh issue comment (the durable
# record the hub-status waived-gates ledger surfaces and that survives the land) and a distinct
# fast-path span. Anything that is NOT a confident restatement falls through to the full reasoner
# unchanged. Disable with AFK_FASTPATH=0.
#
# HONEST TRADE-OFF: the coverage metric is a bag-of-words CONTAINMENT proxy (does the plan say
# anything the body did not?), so it detects "the plan adds new design" but NOT "the plan OMITS
# a required step" — a plan that silently drops a body requirement can still score high and be
# waived. The reasoner would have flagged the omission; the fast path trades that check for the
# saved round trip. The downstream RED/GREEN/REVIEW + acceptance gates remain the backstop that
# a plan-gate approval never was the only guard against an incomplete implementation.

# _broker_try_fastpath_gate <wt> <issue> <mode> -> rc 0 when the gate was WAIVED (auto-approved +
# injected + recorded — the caller returns), rc 1 when the fast path does not apply (attended mode,
# disabled, no POSTED plan artifact, not a restatement, no pane, or the inject failed — the caller
# falls through to run_answerer). UNATTENDED-only: an attended reviewer chose to watch the drain, so
# they get the reasoner's plan assessment (or the QCM), never a silent waive.
# The plan is read HERE from _read_gate_artifact (the SCRIPTED handoff a spoke wrote with
# spoke-ready.sh --gate), NEVER the caller's transcript-extraction fallback: a bare --gate park that
# wrote no artifact is NOT waivable — its transcript narration is not a plan the spoke authored, and
# (issue-derived) it scores high coverage, so trusting it could auto-approve a plan never written
# (#277 review). Reading the artifact HERE rather than taking the caller's already-fallback-merged
# plan re-reads it (the caller reads it again for the reasoner question) — a DELIBERATE trade: the
# shared read is exactly what let the fallback reach the waive, and a second small file read is
# nothing against a reasoner path that costs minutes. Synchronous (one gh call + a local python
# coverage check), so the minutes-long staleness the reasoner path guards against does not apply —
# no _still_parked_same recompute needed.
_broker_try_fastpath_gate() {
  local wt="$1" issue="$2" mode="$3" plan body cov target
  [ "$mode" = unattended ] || return 1
  [ "${AFK_FASTPATH:-1}" != 0 ] || return 1
  plan="$(_read_gate_artifact "$wt" "$issue")"
  [ -n "$plan" ] || return 1   # no real posted artifact -> never fast-path the transcript fallback
  body="$(_broker_issue_body "$issue")"
  cov="$(_broker_plan_is_restatement "$plan" "$body")" || return 1   # rc 1 -> not a restatement
  target="$(_spoke_pane_target "$wt")"
  [ -n "$target" ] || return 1
  # Deliver the approval through the SAME hardened path the reasoned-ANSWER branch uses.
  stamp_answer_attempt "$issue"
  inject_and_verify "$wt" "$target" \
    "Approved — the posted plan restates the issue contract; proceed to implementation." \
    || return 1
  log "  fast-path auto-approved #$issue (plan restates issue body, coverage ${cov:-?})"
  _consume_gate_tag "$wt" "$issue"
  _afk_clear_warned "$issue"   # a waive is genuine progress → drop any warned-retry backoff
  clear_answer_drop "$issue"   # #288: a waive is a delivery — any prior drop record is moot
  # broker_journal_decision (not the file-only _broker_journal_line): it ALSO posts a best-effort
  # gh issue comment, the DURABLE record that survives the land so an operator reviewing a landed
  # issue can still tell it was fast-pathed and why. park kind `gate` is distinct from answer/permission.
  broker_journal_decision "$issue" gate \
    "fast-path auto-approved: plan restates issue body (coverage ${cov:-?})" reversible
  afk_emit_decision "$wt" fast-path
  return 0
}

# decide_and_act <wt_path> <issue> -> reason about a parked spoke and act: inject the
# answer, or escalate to blocked/<issue>. Fail-safe: an answerer that returns no decision
# (or an answer we cannot inject) escalates rather than guessing.
broker_service_gate() {
  local wt="$1" issue="$2" mode="${3:-unattended}" depth="${4:-0}" question orig_question raw rc decision kind text target was_gate=0 inject_diagnosed=0
  # Self-heal a stale gate tag (issue #204): if gate/<issue> is at the tip but the spoke
  # already resumed past its PLAN gate (a late / external / attended approval that never ran
  # the confirmed-inject path), consume the stale tag and stop — do NOT re-answer, and do NOT
  # count it against the re-answer ceiling (checked BEFORE it, so a resumed spoke heals even
  # once exhausted). The plan-gate-guard self-heals the same signal from the spoke side.
  #
  # #288 AC1/AC4: this is the exact moment the broker PROVES the park episode ended — end it
  # right here rather than waiting for a later slot_state tick to happen to observe "not
  # parked" (the #277 gap: the watchdog fired off a park-onset that outlived the episode it was
  # stamped for). Clear the onset, the answer-lane warned-retry backoff, AND any answer-drop
  # record (review: a re-park on the SAME tip/signature would otherwise inherit a stale drop
  # count from the now-resolved episode), so a later re-park gets a fresh ceiling instead of
  # inheriting one exhausted by the resolved episode's own retries.
  if _gate_parked "$wt" "$issue" && _gate_answer_landed "$wt"; then
    log "  #$issue resumed past its PLAN gate outside the broker — consuming the stale gate/$issue tag"
    _consume_gate_tag "$wt" "$issue"
    clear_park_onset_epoch "$issue"
    _afk_clear_warned "$issue"
    clear_answer_drop "$issue"
    return 0
  fi
  # A prior tick found the reasoner mutated the live tree for this gate (#237). The mutation
  # perturbs the (tip, sig) ceiling every tick (a tree write flips the pending command), so a
  # DURABLE void marker — not the ceiling — is what throttles the mutating reasoner. #241 §5: the
  # void is no longer terminal-forever — it is BACKOFF-paced. Inside the warned-retry backoff:
  # skip (parked LAST). Once the backoff elapses: clear the marker for ONE supervised retry (if
  # the reasoner mutates again this tick it re-voids + re-arms a longer backoff). Checked before
  # the ceiling on purpose. A fresh arm clears both the marker and the backoff.
  if _broker_gate_voided "$issue"; then
    _afk_warned_due "$issue" || return 0                             # inside the backoff → parked LAST
    rm -f "$(_broker_voided_marker "$issue")" 2>/dev/null || true    # backoff elapsed → allow one retry
  fi
  # Re-answer ceiling (#203 finding 1): a legitimately-escalated spoke parked on the SAME
  # prompt must not re-run the reasoner/classifier every tick forever. After the ceiling on
  # the SAME (tip, prompt-signature) the gate is terminal — it stays blocked/<issue> at the
  # tip from the prior escalation — until the prompt changes or the tip moves. Checked before
  # BOTH the permission path (#203 finding 4's compound dialog) and the answerer path.
  local park_sig; park_sig="$(_broker_park_signature "$wt" "$issue")"
  # An APPROVE already DELIVERED for this exact park (#294): the same (tip, sig) AND the same gated
  # tool_use is still pending, so the dialog on the pane is the one we just answered — not a new
  # ask. Without this the ceiling below recomputed the same key, found the counter still under
  # AFK_REANSWER_CEILING, and re-approved the identical command a second time (the #135/#188
  # two-concurrent-gates shape). Keyed on the tool_use id too, so a repeatable safe command the
  # spoke re-issues VERBATIM at the same tip is a NEW dialog and is still served.
  #
  # Checked BEFORE the ceiling, like the void marker above and for the same reason: a skipped tick
  # must not burn ceiling budget, or a stale pane would exhaust it and warn + arm the #241 backoff
  # over a spoke that never failed at anything. It needs no extra _permission_pending call either —
  # only a perm: signature can match a served record, so an answer/gate park never does (the tick's
  # single pane read stays single, #269).
  #
  # NEVER terminal (the void block's shape): approve_permission verifies only that the transcript
  # mtime advanced, not that the dialog was consumed, so an approve whose keypress never landed
  # leaves the identical park pending — a permanent skip would strand it. Inside the window: skip.
  # Once it elapses: drop the marker for ONE supervised re-serve, and the ceiling paces from there.
  if _broker_permission_served "$wt" "$issue" "$park_sig"; then
    _broker_served_skip_due "$issue" || return 0                  # inside the window → already served
    clear_permission_served "$issue"                              # elapsed → one supervised re-serve
  fi
  if _broker_reanswer_exhausted "$wt" "$issue" "$park_sig"; then
    # #241 §5: the ceiling is no longer TERMINAL — it warns and retries on an exponential
    # backoff, so a doom-loop is throttled by the growing curve, not abandoned. On the FIRST
    # exhaustion: warn, arm the backoff, and pause. Inside the backoff window: skip (parked
    # LAST). Once the backoff elapses: warn, re-arm a longer backoff, and fall through for ONE
    # supervised retry (the counter stays exhausted, so each window yields a single re-run).
    local ws; ws="$(_afk_warned_state_file "$issue")"
    if [ ! -f "$ws" ]; then
      broker_warn "$issue" "re-answer ceiling reached on the same prompt — backing off (retried on the curve, #241)"
      broker_journal_decision "$issue" ceiling "re-answer ceiling reached; backing off on unchanged park" reversible
      _afk_warned_arm "$issue"
      return 0
    fi
    if ! _afk_warned_due "$issue"; then return 0; fi   # inside the backoff window → parked LAST
    broker_warn "$issue" "re-answer backoff elapsed — one supervised retry on the same prompt (#241)"
    broker_journal_decision "$issue" ceiling "re-answer backoff elapsed; supervised retry" reversible
    # ARM the next (longer) backoff HERE, before the retry runs, so the pause is guaranteed
    # regardless of the retry's OUTCOME. This is the ONLY arm that paces a MECHANICAL classifier
    # auto-approve (line ~2091) which, on success, leaves the same (tip, park-sig) intact and
    # neither arms nor clears — without it a re-appearing auto-approvable dialog would re-warn +
    # re-run every tick (hub-review B1-cluster regression). A retry that instead self-arms (reasoned
    # DENY / ESCALATE) advances the counter a SECOND time; that double-step only GROWS the backoff
    # (strictly more conservative, bounded by the cap) and is cleared the moment the tip advances
    # (genuine progress), so it never strands a recoverable spoke. A guard that suppressed the
    # second arm was tried and reverted (#241 review r2.2): a tick-scoped global leaked into the
    # reap/land passes that also call broker_warn_continue and into the depth+1 recursion, which
    # was strictly worse than the benign double-step it removed.
    _afk_warned_arm "$issue"
    # fall through for one supervised retry; the arm above paces the next
  fi
  # A pending permission dialog is decided by the rules classifier, not the answerer (#149). The
  # tick's already-captured signature rides along (#294): a delivered approve records the park it
  # served, and re-deriving the signature after the delivery could name a DIFFERENT park (the #288
  # note_answer_drop lesson).
  if _permission_pending "$wt"; then _decide_permission "$wt" "$issue" "$park_sig"; return; fi
  # Snapshot the transcript clock BEFORE the park checks: a write landing between
  # this and the pre-inject re-check must count as movement (review nit, ST2).
  local parked_mtime; parked_mtime="$(_transcript_mtime "$wt")"
  local parked_sizes; parked_sizes="$(_transcript_sizes "$wt")"   # #241 §4: detect a real reply vs a non-turn write
  _gate_parked "$wt" "$issue" && was_gate=1
  orig_question="$(extract_pending_question "$wt")"
  question="$orig_question"
  if [ "$was_gate" -eq 1 ]; then
    # Route a PLAN-gate park to approve/amend-the-POSTED-PLAN — generic transcript
    # re-extraction is what replayed the seed six times in #124. PREFER the scripted plan
    # artifact (issue #175: a script reads what a script wrote) over transcript extraction;
    # orig_question (the transcript walk) stays as the fallback for an unextractable gate
    # park (rotated transcript, no gate Bash record) or a bare --gate that wrote no artifact.
    # #277 fast-path: a POSTED plan artifact that merely RESTATES the issue body is auto-approved
    # here WITHOUT the expensive run_answerer round trip (the reason step below). The helper reads
    # the artifact itself and fires ONLY on a real posted plan — never the transcript fallback — so
    # a bare --gate park below still reasons. Anything not a confident restatement falls through.
    if _broker_try_fastpath_gate "$wt" "$issue" "$mode"; then return 0; fi
    local plan; plan="$(_read_gate_artifact "$wt" "$issue")"
    [ -n "$plan" ] || plan="$orig_question"
    question="The spoke is parked at its PLAN gate; below is the plan it posted. Approve it or state precise amendments to it. Do NOT restate or re-issue the task itself.

${plan:-(the plan prose could not be extracted — approve or amend from the issue contract above)}"
  elif [ -z "$question" ]; then
    return 0
  fi
  log "→ answering #$issue (parked on input)"
  # Read-only guard (subtask B): fingerprint the LIVE worktree around the reason step.
  # Since #237 the reasoner runs in a throwaway snapshot COPY (cwd=snap), so its relative
  # writes land in the copy and the live-tree fingerprint is a should-never-fire backstop —
  # its one remaining true purpose is catching an ABSOLUTE-path escape (a reasoner tool
  # writing `$wt/…` / `git -C $wt`, which bypasses cwd=snap). Detection is the hard guarantee
  # independent of the LLM's tool-allowlist. #247: a live-tree diff is now ATTRIBUTED by the
  # reasoner's own tool_use audit (_reasoner_wrote_live_tree), not the spoke transcript.
  local fp_before; fp_before="$(_broker_worktree_fingerprint "$wt")"
  raw="$(run_answerer "$issue" "$question" "$wt")"; rc=$?
  # #247: run_answerer streams stream-json for the audit; the text parsers below (auth,
  # parse_decision, parse_decision_field) read the NORMALIZED final text, the audit reads $raw.
  local raw_text; raw_text="$(_normalize_answerer_output "$raw")"
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
    # The live tree changed during the reason step. Once the tree moved under the answerer its
    # answer is derived from a stale tree and must NEVER be injected (#244 review): the tree-changed
    # path ALWAYS returns here, so we never fall through to the ANSWER branch's recompute (which
    # would re-baseline fp_before and inject over the mutated tree).
    # #247 option (c): ATTRIBUTE the diff by the REASONER's OWN tool_use audit (whose whole-tree-diff
    # attribution the spoke transcript could only guess at — #244 residuals). Only a PROVEN reasoner
    # write (rw_rc 0) short-circuits to void amid spoke activity; when the audit does NOT prove a write
    # we do NOT trust it alone — an escape via a vector the audit does not model must still fail SAFE
    # (#244's "unconfirmed change => VOID"), so we attribute to the spoke ONLY on a positive spoke
    # signal and otherwise VOID.
    _reasoner_wrote_live_tree "$raw" "$wt"; local rw_rc=$? do_void=0
    if [ "$rw_rc" -eq 0 ]; then
      # The audit PROVES a reasoner live-tree write (a write tool under $wt / a mutating $wt-absolute
      # Bash). Void even amid COINCIDENT genuine spoke activity — closes #244 residual 1.
      do_void=1
    elif [ "$rw_rc" -eq 1 ]; then
      # The audit saw the reasoner's stream and found no modelled live-tree write. Attribute the diff
      # to the spoke ONLY on a positive transcript TOUCH — any appended record, since the isolated
      # reasoner writes nothing to the live transcript, so a #240 tool_result-only self-resume still
      # proves the spoke (closes residual 2). Spoke totally silent + a tree diff the audit could not
      # attribute ⇒ an unmodelled escape ⇒ fail SAFE and VOID (the #244 posture, restored).
      _spoke_touched_transcript "$wt" "$parked_sizes"; local touch_rc=$?
      [ "$touch_rc" -ne 0 ] && do_void=1
    else
      # rw_rc 2: the audit is UNAVAILABLE (a plain-text answerer / no stream-json / no python3) —
      # fall back to the #244 spoke-activity signal, unchanged: void UNLESS a genuine spoke turn
      # landed (fail SAFE on rc 2 there too, mirroring the unverifiable-fingerprint escalation). This
      # is what keeps every #244 answerer-stub test green — those stubs carry no auditable stream.
      _spoke_activity_appended "$wt" "$parked_sizes"; local act_rc=$?
      [ "$act_rc" -ne 0 ] && do_void=1
    fi
    if [ "$do_void" -eq 1 ]; then
      # Stamp the durable void marker FIRST so the top-of-function backoff short-circuit paces the
      # mutating reasoner across ticks — the (tip, sig) ceiling can't, since the tree write perturbs
      # it every tick. #241 §5: no longer terminal; warn + back off. 'unknown' reversibility: the
      # reasoner ESCAPED #237 snapshot isolation and wrote the LIVE tree — a should-never-fire event
      # the morning review must triage from the benign.
      _broker_mark_voided "$issue"
      log "  reasoner mutated the read-only worktree of #$issue — voiding its answer (backoff-paced; #241)"
      _broker_on_human_decision "$mode" "$wt" "$issue" \
        "the gate reasoner mutated the read-only worktree — its answer is voided; review the live tree" unknown
      return 0
    fi
    # No reasoner write: the diff is the spoke's own concurrent edit (the #234 self-resume) or a
    # sibling's. DROP the stale answer, never inject (a recompute would re-baseline the fingerprint
    # and inject mid-turn, #89): no gate-voided marker, no blocked/<issue> on an actively-working
    # spoke. A fresh park next tick is serviced anew.
    log "  #$issue's live tree changed but the reasoner did not write it — dropping the stale answer (#247)"
    note_answer_drop "$wt" "$issue" "$park_sig" "live tree changed but the reasoner did not write it (#247)"
    return 0
  fi
  # The answerer is the supervisor's own `claude`; if its credentials are dead, every
  # other `claude` (the spokes, the next tick's answerer) is dead too. We treat it as an
  # auth failure only when the answerer EXITED NONZERO and its output carries an auth
  # signature — a healthy answer that merely discusses auth exits 0 and is unaffected.
  # Raise the global stop flag so the supervisor pauses DISPATCH and re-probes (#241 §9). WARN
  # this spoke (an auth failure is not its fault — never block it); the drain resumes servicing
  # it once auth recovers, rather than parking it blocked/<issue>. #247: read the RAW stream (not
  # the normalized text) so an auth signature in a dropped stream-json event is never missed.
  if [ "$rc" -ne 0 ] && is_auth_failure "$raw"; then
    _AFK_AUTH_FAILED=1
    broker_warn_continue "$wt" "$issue" auth \
      "subscription auth failed — token could not refresh; re-run /login on the host (drain paused, re-probing)" reversible
    return 0
  fi
  decision="$(parse_decision "$raw_text")"
  kind="${decision%%$'\t'*}"
  text="${decision#*$'\t'}"
  if [ "$kind" = "ANSWER" ] && [ -n "$text" ]; then
    # Park freshness gates EVERYTHING: if the spoke moved on while the answerer
    # reasoned, nothing happens regardless of the answer's content — injecting would
    # land mid-turn (#129/#89) and even a seed-replay escalation would stamp a
    # spurious blocked/<issue> on an actively-working spoke.
    if ! _still_parked_same "$wt" "$issue" "$was_gate" "$orig_question" "$parked_mtime"; then
      # #241 §4: the park may have CHANGED (a new prompt), or a non-turn write may have bumped
      # the transcript mtime while the spoke is STILL parked (the recurring-false-staleness that
      # stranded #240). Recompute against the CURRENT park in the same pass (depth-bounded to one
      # re-run) ONLY when the spoke is still parked AND no USER TURN landed since the park — a
      # DEFINITE no-reply (rc 1). Preserve #89/#129: a reply landing (rc 0) or an unreadable
      # transcript (rc 2) means the spoke may have moved on, so drop rather than inject mid-turn.
      _user_turn_appended "$wt" "$parked_sizes"; local _ut_rc=$?
      if [ "$depth" -lt 1 ] && [ "$_ut_rc" -eq 1 ] && _spoke_still_parked "$wt" "$issue"; then
        log "  #$issue still parked on a refreshed prompt (no reply landed) — recomputing against the current park (#241)"
        broker_service_gate "$wt" "$issue" "$mode" "$(( depth + 1 ))"
        return $?
      fi
      log "  #$issue is no longer parked on that prompt — dropping the stale answer (spoke moved on)"
      note_answer_drop "$wt" "$issue" "$park_sig" "no longer parked on that prompt (spoke moved on)"
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
          _afk_clear_warned "$issue"   # #241: genuine progress → drop this issue's warned-retry backoff
          clear_answer_drop "$issue"   # #288: a delivery landed — any prior drop record is moot
          # #241 review B2: record the taken answer for morning review. Read the reasoner's own
          # 'WARN:' note and 'REVERSIBILITY:' class off the reply. A WARN or a non-reversible class
          # is a NOTEWORTHY decision → a loud warned record + a journal line WITH a gh comment. A
          # routine reversible answer is a cheap FILE-ONLY journal line (no per-answer gh spam).
          local ans_rev_raw ans_rev ans_warn ans_ref
          # Persist the reasoner's own output and journal a ref to it (#281): the record says
          # WHAT was decided, the ref says WHY. Empty when the save failed — the pre-#281
          # behavior — so a lost audit trail never costs the delivered answer.
          ans_ref="$(_broker_save_reasoning "$issue" "$raw_text")"
          ans_rev_raw="$(parse_decision_field "$raw_text" REVERSIBILITY)"
          # Normalize to the first ALPHABETIC RUN (portable lowercasing, tolerant of quotes,
          # parens, or a trailing period around the class word) so 'Reversible', 'reversible.',
          # and '"irreversible"' all classify correctly. Gate the warn on the RAW presence, not
          # the normalized value: a present-but-non-reversible class (even one that normalizes to
          # empty, e.g. all-punctuation noise) must fail SAFE to a loud warned record, never
          # silently collapse to routine the way a bare trailing-strip did (#241 review).
          ans_rev="$(printf '%s' "$ans_rev_raw" | tr '[:upper:]' '[:lower:]' | grep -oE '[a-z]+' | head -n1 || true)"
          ans_warn="$(parse_decision_field "$raw_text" WARN)"
          if [ -n "$ans_warn" ] || { [ -n "$ans_rev_raw" ] && [ "$ans_rev" != reversible ]; }; then
            # The clear above dropped the retry BACKOFF (progress); this warned record is the
            # DELIBERATE loud review flag for the noteworthy decision — not a stale leftover.
            broker_warn "$issue" "answered [${ans_rev:-unknown}]${ans_warn:+ — WARN: $ans_warn}"
            broker_journal_decision "$issue" answer "injected answer${ans_warn:+ (WARN: $ans_warn)}" "${ans_rev:-unknown}" "$ans_ref"
          else
            _broker_journal_line "$issue" answer "injected answer (routine)" "${ans_rev:-reversible}" "$ans_ref"
          fi
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
  # A diagnosed wedge/refuted inject (rc 2/3) is genuinely UNCERTAIN — the paste may have
  # partially landed — so journal it 'unknown' for triage; an ESCALATE/no-decision is reversible.
  local decision_rev=reversible
  [ "$inject_diagnosed" -eq 1 ] && decision_rev=unknown
  _broker_on_human_decision "$mode" "$wt" "$issue" "$text" "$decision_rev"
}

decide_and_act() { broker_service_gate "$1" "$2" unattended; }

# _broker_on_human_decision <mode> <wt> <issue> <reason> -> route a decision that the answerer
# could not resolve into an injectable answer (a voided/mutated read-only tree, an unverifiable
# fingerprint, an ESCALATE/no-decision reasoner reply, an inject failure). The ONE mode-divergent
# seam of the shared core. Attended: present a structured QCM on a dedicated per-gate surface
# (#155). Unattended (/afk): #241 no longer parks blocked/<issue> — it WARNS loudly, journals the
# taken decision, and keeps the spoke serviced (retried on the warned-retry backoff). The reason
# IS the decision text; these are reversible (the answer is voided/undelivered, the spoke's work
# is intact and re-serviceable).
_broker_on_human_decision() {
  local mode="$1" wt="$2" issue="$3" reason="$4" rev="${5:-reversible}"
  if [ "$mode" = attended ] && command -v _broker_present_qcm >/dev/null 2>&1; then
    _broker_present_qcm "$wt" "$issue" "$reason"
    return
  fi
  # <rev> is the reversibility of the DECISION taken (void/decline the answer, retry) — almost
  # always reversible. Callers pass 'unknown' when the underlying EVENT is genuinely uncertain
  # (a reasoner that escaped snapshot isolation and wrote the live tree; a wedge whose paste may
  # have partially landed) so the morning review can triage those out of the benign default.
  broker_warn_continue "$wt" "$issue" answer "$reason" "$rev"
}

# --- fail-CLOSED deny-wall (issue #275 / #211) --------------------------------
# Installed LAST so it overrides the real afk_danger_guard_decide / afk_permission_hook_decide
# ONLY when a required module above failed to load. A deny-wall that cannot load its
# classifiers must DENY (a walled-shut spoke beats an unguarded bypass spoke); the allow-only
# permission hook must never auto-approve. Both are self-contained here -- they must not touch
# a missing module's helpers.
if [ "${_GB_MODULES_OK:-1}" != 1 ]; then
  afk_danger_guard_decide() {
    cat >/dev/null 2>&1 || true
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"},"permission":"deny"}\n' \
      "afk-danger-guard: gate-broker module failed to load -- failing closed"
  }
  afk_permission_hook_decide() { cat >/dev/null 2>&1 || true; return 0; }
fi
