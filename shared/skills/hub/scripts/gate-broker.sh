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
for _mod in markers detect classify danger answerer; do
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
# NB: NOT truncated since #257 -- this command feeds the default-deny classify_permission and the
# _reason_permission prompt in the pane path. Truncating a benign prefix off a risky tail could
# hide the risky segment and mis-approve it, exactly as #253 avoided for afk_permission_hook_decide.
# The 2000-char DISPLAY cap now lives at the log call sites in _decide_permission via cmd_display,
# not here. The other consumers tolerate the full command: _permission_pending tests non-emptiness
# and _broker_park_signature hashes the basis.
# Plain ASCII, no backticks/parens: bash 3.2 mis-parses those inside a heredoc.
print(cmd.strip())
PYEOF
}

# _permission_pending <wt_path> -> true when the spoke is parked on a permission dialog. #269
# (#254 option b): DETECTION is decoupled from EXTRACTION. A shown pane dialog IS a park even
# when extract_pending_command is empty -- the gated tool_use is not flushed while the dialog is
# pending (the #240/#254 finding), so ANDing a non-empty command made a real park read as FALSE,
# and the reaper (_reap_or_resume) fell past the park check into "likely hung -> revive",
# re-raising the identical dialog. The pane is the "a dialog is up" signal -- but the prompt
# PHRASE alone is not enough: it can appear in a spoke's OWN rendered output (a spoke maintaining
# the afk subsystem git-shows the file that defines the phrase), a #240/#89-class false park
# (#269 review). So require BOTH the phrase (_pane_shows_permission_prompt) AND the live dialog's
# interactive affordance -- a numbered Yes/No option line the real menu draws but a plain text
# echo does not. The #240 guard holds: NO pane dialog -> false (no phantom park on a stale
# RESOLVED tool). _decide_permission reads the command separately and handles an unreadable one
# (decline + warn, never park). Fail-closed on no tmux/pane. The single gate slot_state and
# decide_and_act share. The pane is captured ONCE and both patterns are grepped from that copy:
# a second capture-pane doubled the tmux subprocess load and, more importantly, its extra
# failure surface destabilized the park signature under heavy load (a flaked capture flipped the
# park verdict, resetting the re-answer ceiling -- #269 final review NIT + a load-flake fix). The
# phrase default MIRRORS _pane_shows_permission_prompt (hub-inject.sh) and reads the SAME
# AFK_PERMISSION_PROMPT_RE override, so an operator retune stays consistent across both.
_permission_pending() {
  local wt="$1" target pane
  command -v tmux >/dev/null 2>&1 || return 1
  target="$(_spoke_pane_target "$wt")"
  [ -n "$target" ] || return 1
  pane="$(tmux capture-pane -p -t "$target" 2>/dev/null)" || return 1
  printf '%s\n' "$pane" | grep -Eq -- "${AFK_PERMISSION_PROMPT_RE:-Do you want to proceed\?}" || return 1
  printf '%s\n' "$pane" | grep -Eq -- "${AFK_PERMISSION_AFFORD_RE:-[0-9]+\.[[:space:]]+(Yes|No)}"
}

# _reason_permission_record <wt> <issue> <decision> <rev> -> the post-DELIVERY record for a
# reasoned permission decision: a loud warned record, a gh comment (off the keypress critical
# path), the warned-retry backoff arm, and the warn span. The caller writes the cheap FILE
# journal line BEFORE the keypress (durability); this reflects the delivered OUTCOME after.
_reason_permission_record() {
  local wt="$1" issue="$2" decision="$3" rev="$4"
  broker_warn "$issue" "$decision"
  _broker_journal_gh_comment "$issue" permission "$decision" "$rev"
  _afk_warned_arm "$issue"
  afk_emit_decision "$wt" warn
}

# _reason_permission <wt> <issue> <cmd> <classify_reason> -> the reasoner decides a permission
# dialog the fixed rules would NOT auto-approve (#241 §2: the reasoner decides even irreversible
# asks). It runs in run_answerer's read-only snapshot copy and answers 'ANSWER: APPROVE' or
# 'ANSWER: DENY: <reversible path>'. APPROVE delivers Yes; DENY (or any unclear reply — the safe
# default) declines the dialog and injects the reversible-path guidance. Either way the taken
# decision is warned + journaled with its reversibility class, and the spoke is NEVER parked.
_reason_permission() {
  local wt="$1" issue="$2" cmd="$3" why="$4" q raw rc ans text rev guidance
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
  raw="$(run_answerer "$issue" "$q" "$wt")"; rc=$?
  # #247: run_answerer streams stream-json; normalize ONCE to the final text for the DECISION
  # parsers. is_auth_failure reads the RAW stream (below) so an auth signature in a dropped event
  # is never missed.
  local raw_text; raw_text="$(_normalize_answerer_output "$raw")"
  # Auth failure is the one true external blocker (#73): a dead supervisor token yields an
  # auth-error blob, not a decision — and parse_decision would fall to the DENY default and
  # inject a SPURIOUS denial into the live dialog. Detect it (rc != 0 AND an auth signature),
  # raise the global halt flag (the supervisor pauses DISPATCH + re-probes, #241 §9), and return
  # without injecting. #241 §9: WARN the spoke (an auth failure is not the spoke's fault — never
  # block it); the drain resumes servicing it once auth recovers.
  if [ "$rc" -ne 0 ] && is_auth_failure "$raw"; then
    _AFK_AUTH_FAILED=1
    broker_warn_continue "$wt" "$issue" auth "subscription auth failed — token could not refresh; re-run /login on the host (drain paused, re-probing)" reversible
    return 0
  fi
  ans="$(parse_decision "$raw_text")"
  text="${ans#*$'\t'}"
  rev="$(parse_decision_field "$raw_text" REVERSIBILITY)"
  # NB: the classifier verdict (ESCALATE) is already recorded in decisions.log by the caller; the
  # reasoned approve/deny is journaled here (a FILE line before the keypress + a gh comment after,
  # via _reason_permission_record), NOT in decisions.log — that log codifies only the MECHANICAL
  # classifier (#155 D).
  case "$text" in
    APPROVE*)
      # #241 review B1: journal the decision to the FILE BEFORE approve_permission delivers the
      # keypress (durable if the inject crashes/races the command it authorized) — file-only, so
      # no network gh-comment sits on the spoke's unblock critical path. The pre-keypress line is
      # PROVISIONAL ("APPROVING", present-continuous) so a per-record read can never mistake the
      # in-flight intent for a delivered-and-ran approval; the OUTCOME line (delivered / FAILED)
      # is written after (#241 review).
      _broker_journal_line "$issue" permission "reasoner APPROVING (delivery pending): $cmd" "${rev:-unknown}"
      if approve_permission "$wt"; then
        _broker_journal_line "$issue" permission "reasoner APPROVED (delivered): $cmd" "${rev:-unknown}"
        _reason_permission_record "$wt" "$issue" "reasoner APPROVED (delivered): $cmd" "${rev:-unknown}"
      else
        # A delivery failure is distinct on the DURABLE surfaces (a FAILED journal line + gh),
        # so the morning review never reads an undelivered approval as "authorized and ran".
        _broker_journal_line "$issue" permission "reasoner APPROVED but delivery FAILED: $cmd" "${rev:-unknown}"
        _reason_permission_record "$wt" "$issue" "reasoner APPROVED but delivery FAILED: $cmd" "${rev:-unknown}"
      fi ;;
    *)
      # DENY, or any reply that does not clearly approve — the safe default is to decline. Only a
      # DENY-prefixed reply carries guidance (with or without the colon); anything else uses the
      # default decline message rather than injecting the raw reply.
      case "$text" in
        DENY*)
          guidance="${text#DENY}"; guidance="${guidance#:}"
          guidance="${guidance#"${guidance%%[![:space:]]*}"}" ;;   # ltrim
        *) guidance="" ;;
      esac
      [ -n "$guidance" ] || guidance="Declined that command — take the reversible, in-scope path instead."
      # B1 generalized to DENY (#241 review): a provisional FILE line before _deny_permission
      # injects (survives a crash between inject and record), then the OUTCOME. The delivery rc is
      # NOT swallowed: a failed redirect (dead pane / failed inject) is journaled DISTINCTLY, so a
      # review never reads a stuck spoke as cleanly redirected. Decline-and-redirect is reversible
      # by construction, so default the class to reversible.
      _broker_journal_line "$issue" permission "reasoner DENYING (redirect pending) ($cmd): $guidance" "${rev:-reversible}"
      if _deny_permission "$wt" "$guidance"; then
        _broker_journal_line "$issue" permission "reasoner DENIED ($cmd): $guidance" "${rev:-reversible}"
        _reason_permission_record "$wt" "$issue" "reasoner DENIED ($cmd): $guidance" "${rev:-reversible}"
      else
        _broker_journal_line "$issue" permission "reasoner DENIED but redirect delivery FAILED ($cmd): $guidance" "${rev:-reversible}"
        _reason_permission_record "$wt" "$issue" "reasoner DENIED but redirect delivery FAILED ($cmd): $guidance" "${rev:-reversible}"
      fi ;;
  esac
}

# _decide_permission <wt_path> <issue> -> classify the spoke's pending permission dialog and act.
# AUTO-APPROVE a safe scoped self-op (mechanical fast path, unchanged, unwarned). Anything the
# fixed rules will not auto-approve — an ESCALATE verdict or an unreadable command — no longer
# parks the spoke: it routes to the always-answering reasoner (#241) which approves a safe
# command or declines-and-redirects a risky one, warning + journaling the taken decision.
_decide_permission() {
  local wt="$1" issue="$2" cmd cmd_display decision kind reason
  cmd="$(extract_pending_command "$wt")"
  if [ -z "$cmd" ]; then
    # Unreadable command: cannot classify. Decline it (the reversible action) + warn — never
    # park. The spoke gets a denial and keeps going; the backoff paces any retry.
    stamp_answer_attempt "$issue"
    _deny_permission "$wt" "Declined an unreadable permission command — re-issue it in a clearer form." || true
    broker_warn_continue "$wt" "$issue" permission "declined an unreadable permission command" reversible
    return 0
  fi
  # #257: classify the WHOLE command (uncapped) so a risky tail past 2000 chars can't hide behind
  # a benign prefix. The 2000-char cap is DISPLAY-only now, applied to a copy used solely for the
  # log/codify surfaces HERE (log_decision's signature + the drain log line) — kept byte-identical
  # to pre-fix. The classifier and the _reason_permission prompt get the full $cmd; the reasoner
  # path (its file journal + gh comment) then deliberately carries the untruncated command on its
  # OWN surfaces, so a human reviewing a genuine escalation sees the whole thing.
  cmd_display="${cmd:0:2000}"
  decision="$(classify_permission "$cmd" "$wt")"
  kind="${decision%%$'\t'*}"
  reason="${decision#*$'\t'}"
  # Record the classifier's VERDICT (both APPROVE and ESCALATE) for the codification pass,
  # not just successful approvals — otherwise every logged line is APPROVE and codify's
  # unanimity check is vacuous. Logging both makes a flag-dependent signature (`git reset
  # -q` APPROVE vs `git reset --hard` ESCALATE, which share the signature git-reset+git-add)
  # correctly read as a CONFLICT, so codify never proposes it as a safe unanimous rule (#155 D).
  log_decision "$issue" permission "$cmd_display" "$kind"
  if [ "$kind" = "APPROVE" ]; then
    log "→ auto-approving safe permission for #$issue: $cmd_display"
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

# --- programmatic PreToolUse permission decision (issue #253) ------------------
# The pane-answering path above (extract_pending_command + _pane_shows_permission_prompt +
# approve_permission) detects and OPERATES a TUI dialog after it appears — the brittle surface
# behind the #240/#246/#238 bug family (new dialog shapes, glyphs, and timing windows keep
# breaking the scraper). afk_permission_hook_decide moves the COMMON case OFF the pane entirely:
# a spoke-side PreToolUse hook runs classify_permission on the gated tool call BEFORE any dialog
# and AUTO-APPROVES a benign scoped self-op, so no dialog is ever shown and there is nothing to
# scrape. It reuses the SAME classify_permission verdict (one source of truth), journals the
# auto-approve per #241, and NEVER denies: an ESCALATE — or any un-gated context — stays silent
# (exit 0, no output), so the existing scope-guard hooks' denies remain authoritative and the
# rare genuine escalation still falls through to the drain reasoner / pane path. (A2 — the hook
# itself reasons and returns deny-with-reason to fully retire the pane — is a deferred follow-up.)
#
# COMPOUND LIMIT (#259): Claude Code evaluates a compound Bash command PER-SEGMENT against
# permissions.allow (deny > ask > allow > default-prompt), and this whole-command `allow` does
# NOT satisfy that per-segment check. So the hook suppresses the dialog only for a STANDALONE
# benign op; a compound whose tail segment matches no allow rule (the #238 `chmod +x X && ./X`)
# still prompts despite the allow. The deterministic layer for that class is worktree-new.sh's
# dispatch-time exec-lane seed (`Bash(./:*)`) — this fn stays the standalone + #241-journal path.

# _afk_supervisor_live <wt> -> rc 0 when a LIVE /afk supervisor heartbeat governs <wt>. This is
# the hook's self-limit: it auto-approves ONLY inside a running drain, never in an attended
# session. Mirrors afk-notify-wake.sh's gate — the .afk-heartbeat pidfile in the git-common-dir
# (AFK_HEARTBEAT overrides for tests) names a running pid. Fails CLOSED (rc 1) on any gap.
_afk_supervisor_live() {
  local wt="$1" common hb pid
  common="$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null)" || return 1
  case "$common" in /*) ;; *) common="$wt/$common" ;; esac   # rev-parse may print a relative dir
  hb="${AFK_HEARTBEAT:-$common/.afk-heartbeat}"
  [ -f "$hb" ] || return 1
  read -r pid _ < "$hb" 2>/dev/null || return 1
  case "$pid" in '' | *[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2>/dev/null
}

# _afk_hook_emit_allow <reason> -> print the PreToolUse allow verdict. Mirrors chmod-scope-guard's
# shape: hookSpecificOutput.permissionDecision for Claude Code + a top-level `permission` for
# Cursor's beforeShellExecution, so the auto-approve is understood on both. jq when present, a
# hand-rolled literal otherwise.
_afk_hook_emit_allow() {
  local reason="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -nc --arg r "$reason" '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "allow",
        permissionDecisionReason: $r
      },
      permission: "allow"
    }'
  else
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"%s"},"permission":"allow"}\n' "$reason"
  fi
}

# _afk_hook_emit_deny <reason> -> print the PreToolUse deny verdict (issue #261). Mirrors
# _afk_hook_emit_allow's dual shape (Claude hookSpecificOutput + a top-level Cursor `permission`).
# The reasons this hook emits are controlled ASCII category strings (the resolver already rejected
# quote/metachar paths), so the hand-rolled fallback needs no JSON escaping -- same contract as
# _afk_hook_emit_allow.
_afk_hook_emit_deny() {
  local reason="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -nc --arg r "$reason" '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: $r
      },
      permission: "deny"
    }'
  else
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"},"permission":"deny"}\n' "$reason"
  fi
}

# afk_permission_hook_decide -> read a Claude Code PreToolUse payload on stdin and print an
# `allow` verdict IFF classify_permission APPROVEs the gated tool call inside a live drain;
# otherwise print nothing. Always rc 0 (a PreToolUse allow-only hook must never fail a session).
# The command string is rebuilt EXACTLY as extract_pending_command does (Bash -> its command;
# Read -> "Read <file_path>"; any other tool -> the tool name) so the hook and the pane path
# classify identically. Gated on a spoke branch (issue-numbered slug) AND a live supervisor, so
# an attended session and the hub checkout are untouched.
afk_permission_hook_decide() {
  local payload wt cmd parsed br slug issue decision kind
  payload="$(cat)"
  command -v python3 >/dev/null 2>&1 || return 0
  # One python pass: line 1 = cwd, the remainder = the classifier command string (a Bash command
  # may itself contain newlines, so cmd is everything AFTER the first line, not just line 2).
  parsed="$(_AFK_HOOK_PAYLOAD="$payload" python3 2>/dev/null <<'PYEOF'
import json, os

try:
    obj = json.loads(os.environ.get("_AFK_HOOK_PAYLOAD") or "{}")
except Exception:
    obj = {}
if not isinstance(obj, dict):
    obj = {}
name = (obj.get("tool_name") or "").strip()
inp = obj.get("tool_input")
if not isinstance(inp, dict):
    inp = {}
cwd = (obj.get("cwd") or "").strip()
if name == "Bash":
    cmd = (inp.get("command") or "").strip()
elif name == "Read":
    fp = (inp.get("file_path") or "").strip()
    cmd = f"{name} {fp}" if fp else name
elif name:
    cmd = name
else:
    cmd = ""
print(cwd)
# NB: NOT truncated -- the 2000-char cap lives in the _decide_permission cmd_display log-only
# copy, never in extract_pending_command since #257, because a silent auto-approve must classify
# the WHOLE command. Truncating a benign prefix off a risky tail could hide the risky segment
# and mis-approve it with no dialog. Since
# classify_permission is default-deny, an over-long or unrecognised command just escalates.
# (Plain ASCII + no backticks/parens here: bash 3.2 mis-parses those inside a $()-nested
# heredoc.)
print(cmd.strip())
PYEOF
)"
  # No newline ⇒ python emitted only the cwd line (empty command) — nothing to vouch for.
  case "$parsed" in *$'\n'*) ;; *) return 0 ;; esac
  # Line 1 is the cwd, the rest is the command. This assumes the payload cwd has no embedded
  # newline (Claude Code sets it to a real dir path, never attacker-controlled). If one ever did,
  # wt takes only the first line and the remainder prepends to cmd — which only makes classify
  # STRICTER (extra bogus segments) and fails a bad `git -C "$wt"` below, so it fails CLOSED.
  wt="${parsed%%$'\n'*}"
  cmd="${parsed#*$'\n'}"
  [ -n "$cmd" ] || return 0
  [ -n "$wt" ] || wt="$(pwd)"
  # Spoke self-limit: an issue-numbered branch slug AND a live supervisor. Either missing ⇒ stay
  # silent so the normal permission flow (and any attended user) is untouched.
  br="$(git -C "$wt" branch --show-current 2>/dev/null)" || return 0
  slug="${br##*/}"; issue="${slug%%[!0-9]*}"
  case "$issue" in '' | *[!0-9]*) return 0 ;; esac
  _afk_supervisor_live "$wt" || return 0
  decision="$(classify_permission "$cmd" "$wt")"
  kind="${decision%%$'\t'*}"
  # NEVER deny: only APPROVE emits a verdict; ESCALATE (or anything else) stays silent so the
  # scope-guard denies stay authoritative and the reasoner/pane path still handles the rare case.
  [ "$kind" = APPROVE ] || return 0
  # #241: journal the hook-layer auto-approve (file only — a per-approve gh comment would be
  # spam) BEFORE emitting the verdict, so a decision made with no dialog is auditable. A hook
  # auto-approve is a benign scoped self-op by construction, hence reversible.
  _broker_journal_line "$issue" permission "hook auto-approved: $cmd" reversible
  _afk_hook_emit_allow "afk-permission-hook: classify_permission APPROVEd a benign scoped self-op inside a live drain — auto-allowed (no dialog; ESCALATE and everything else still prompt)"
}

# --- programmatic PreToolUse deny-wall (issue #261) ---------------------------
# Under bypassPermissions an afk spoke raises NO permission dialog (worktree-new.sh --mode afk),
# so a PreToolUse deny-hook is the ONLY safety boundary -- and a deny-hook STILL fires and its
# permissionDecision:"deny" is honored under bypass (proven on CC v2.1.207). afk_danger_guard_decide
# is that wall's decision fn: read a PreToolUse payload, run three tiers, DENY the dangerous ones:
#   Tier 2  classify_danger == DENY        -> journal + emit permissionDecision:"deny"  (deny-first)
#   Tier 1  classify_permission == APPROVE -> silent allow (bypass runs it; no judge, no journal)
#   Tier 3  judge_permission               -> DANGEROUS/fail-closed => journal + deny; SAFE => allow
# Tier 2 runs BEFORE Tier 1 on purpose: classify_permission (built for the old prompt-approve
# model) APPROVEs any read verb, so a `cat ~/.ssh/id_rsa` secret read would be Tier-1-approved and
# never reach the deny list -- checking classify_danger first closes that gap; both static checks
# are cheap (no LLM), so deny-first costs nothing. It reuses the SAME classifiers the drain trusts
# (one source of truth). afk-permission-hook (#253) is left in place and untouched -- its allow is
# redundant-but-harmless under bypass; THIS hook only adds DENY (which wins in CC).

# _afk_spoke_mode <wt> -> print the spoke's execution mode from <root>/.ai-toolkit/mode
# (whitespace-trimmed), or empty when the file is missing / unreadable / <wt> is not a git tree.
# The mode is the load-bearing signal for the deny-wall gate (see afk_danger_guard_decide): the
# file is written by worktree-new.sh at spawn and is gitignored (info/exclude), so it SURVIVES a
# branch checkout / detach -- unlike the branch name, which does not.
_afk_spoke_mode() {
  local wt="$1" root
  root="$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null)" || return 0
  [ -n "$root" ] || return 0
  [ -f "$root/.ai-toolkit/mode" ] || return 0
  tr -d '[:space:]' < "$root/.ai-toolkit/mode" 2>/dev/null || return 0
}

# afk_danger_guard_decide -> read a Claude Code PreToolUse payload on stdin and print a `deny`
# verdict for a boundary-crossing / judge-dangerous command inside an afk (bypass) spoke; else
# print nothing (the command runs under bypass). Always rc 0 (a PreToolUse hook must never fail a
# session). The command string is rebuilt EXACTLY as extract_pending_command / afk_permission_hook_
# decide do, so all three classify identically. Gated on an issue-numbered spoke branch AND the
# fail-safe mode gate (never the hub / ad-hoc / positively-attended).
afk_danger_guard_decide() {
  local payload parsed wt cmd br slug issue decision djson dreason verdict vkind vreason
  payload="$(cat)"
  command -v python3 >/dev/null 2>&1 || return 0
  parsed="$(_AFK_HOOK_PAYLOAD="$payload" python3 2>/dev/null <<'PYEOF'
import json, os

try:
    obj = json.loads(os.environ.get("_AFK_HOOK_PAYLOAD") or "{}")
except Exception:
    obj = {}
if not isinstance(obj, dict):
    obj = {}
name = (obj.get("tool_name") or "").strip()
inp = obj.get("tool_input")
if not isinstance(inp, dict):
    inp = {}
cwd = (obj.get("cwd") or "").strip()
if name == "Bash":
    cmd = (inp.get("command") or "").strip()
elif name == "Read":
    fp = (inp.get("file_path") or "").strip()
    cmd = f"{name} {fp}" if fp else name
elif name:
    cmd = name
else:
    cmd = ""
print(cwd)
# NOT truncated: a deny-wall must classify the WHOLE command -- a truncated benign prefix could
# hide a risky tail. Plain ASCII, no backticks in this comment: bash 3.2 mis-parses those nested.
print(cmd.strip())
PYEOF
)"
  case "$parsed" in *$'\n'*) ;; *) return 0 ;; esac
  wt="${parsed%%$'\n'*}"; cmd="${parsed#*$'\n'}"
  [ -n "$cmd" ] || return 0
  [ -n "$wt" ] || wt="$(pwd)"
  # Issue number (best-effort, for the fail-safe gate + the journal). Empty on a detached HEAD
  # or a non-issue branch -- which is EXACTLY why it must NOT be the primary gate.
  br="$(git -C "$wt" branch --show-current 2>/dev/null || true)"
  slug="${br##*/}"; issue="${slug%%[!0-9]*}"
  case "$issue" in *[!0-9]*) issue="" ;; esac
  # MODE GATE (fail-safe, mode-first -- #261 review BLOCKER). A positively-read `afk` mode means
  # this spoke launched under bypassPermissions, so the wall is ACTIVE on ANY branch: `git bisect`
  # / `rebase` / `checkout <sha>` detach HEAD or move off the issue branch, and the wall must NOT
  # silently drop then (the .ai-toolkit/mode file survives the checkout; the branch name does not).
  # `attended` -> INERT (the human is the wall). A missing / unreadable / ambiguous mode keeps the
  # wall ACTIVE only for an issue-numbered spoke branch (a corrupted spoke); the hub (on main, no
  # mode file) and ad-hoc lanes stay INERT so hub operations are never walled.
  case "$(_afk_spoke_mode "$wt")" in
    attended) return 0 ;;
    afk) ;;
    *) [ -n "$issue" ] || return 0 ;;
  esac
  # Tier 2 (static deny) first -- see the header for why it precedes Tier 1.
  djson="$(classify_danger "$cmd" "$wt")"
  if [ "${djson%%$'\t'*}" = DENY ]; then
    dreason="${djson#*$'\t'}"
    _broker_journal_line "$issue" permission "tier2 deny: $cmd -- $dreason" scope
    _afk_hook_emit_deny "afk-danger-guard tier-2: $dreason"
    return 0
  fi
  # Tier 1 -- a benign scoped self-op the deny list already cleared: allow silently, skip the judge.
  decision="$(classify_permission "$cmd" "$wt")"
  [ "${decision%%$'\t'*}" = APPROVE ] && return 0
  # Tier 3 -- the toolless LLM judge on the residue. Fail-closed (DANGEROUS) => deny.
  verdict="$(judge_permission "$cmd" "$issue")"
  vkind="${verdict%%$'\t'*}"
  if [ "$vkind" = SAFE ]; then
    _broker_journal_line "$issue" permission "tier3 judge SAFE: $cmd" reversible
    return 0
  fi
  vreason="${verdict#*$'\t'}"
  _broker_journal_line "$issue" permission "tier3 judge DENY: $cmd -- $vreason" scope
  _afk_hook_emit_deny "afk-danger-guard tier-3: $vreason"
  return 0
}

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
  if _gate_parked "$wt" "$issue" && _gate_answer_landed "$wt"; then
    log "  #$issue resumed past its PLAN gate outside the broker — consuming the stale gate/$issue tag"
    _consume_gate_tag "$wt" "$issue"
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
  # A pending permission dialog is decided by the rules classifier, not the answerer (#149).
  if _permission_pending "$wt"; then _decide_permission "$wt" "$issue"; return; fi
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
          # #241 review B2: record the taken answer for morning review. Read the reasoner's own
          # 'WARN:' note and 'REVERSIBILITY:' class off the reply. A WARN or a non-reversible class
          # is a NOTEWORTHY decision → a loud warned record + a journal line WITH a gh comment. A
          # routine reversible answer is a cheap FILE-ONLY journal line (no per-answer gh spam).
          local ans_rev_raw ans_rev ans_warn
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
            broker_journal_decision "$issue" answer "injected answer${ans_warn:+ (WARN: $ans_warn)}" "${ans_rev:-unknown}"
          else
            _broker_journal_line "$issue" answer "injected answer (routine)" "${ans_rev:-reversible}"
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
