#!/usr/bin/env bash
# gate-broker-permission.sh -- split out of gate-broker.sh (issue #275).
#
# A pure function-definition module of the gate-broker core. Sourced by the entry lib
# gate-broker.sh AFTER worktree-lib/hub-inject/log/afk_now and BEFORE any function is
# called, so every cross-module helper resolves at call time. Not run on its own.
set -uo pipefail

# --- permission-dialog detection + handling (issue #149) ----------------------
# A permission dialog is the agent `waiting` in Orca's state (03 #8): `worktree ps` reports the
# gated tool's name and input, so there is no pane to scrape and no transcript to re-parse.
# classify_permission decides it; these helpers see it and deliver the decision.
# _decide_permission is reached from decide_and_act, which routes a permission-pending spoke here
# instead of to the answerer.

# extract_pending_command <wt_path> -> the command the waiting agent is gated on (Bash -> its
# command string; Read -> "Read <file_path>"; any other tool -> the tool name, so the classifier
# escalates non-Bash tools like browser/computer/mcp), the same string afk_permission_hook_decide
# builds. Empty when the agent is not waiting, or when Orca's toolInput looks truncated (an ellipsis
# tail): a clipped command could hide a risky segment behind a benign prefix, so it is read as
# unreadable and the caller declines it. NOT capped: the classifier is default-deny (#257).
extract_pending_command() {
  local wt="$1" name input
  [ "$(orca_agent_state "$wt" 2>/dev/null)" = waiting ] || return 0
  name="$(orca_agent_field "$wt" toolName 2>/dev/null)"
  input="$(orca_agent_field "$wt" toolInput 2>/dev/null)"
  case "$input" in *"…") return 0 ;; esac
  case "$name" in
    Bash) printf '%s\n' "$input" ;;
    Read) if [ -n "$input" ]; then printf 'Read %s\n' "$input"; else printf 'Read\n'; fi ;;
    *) printf '%s\n' "$name" ;;
  esac
}

# _permission_pending <wt_path> -> true when the agent is `waiting` on a permission dialog (any
# tool but AskUserQuestion, which extract_pending_question reads). DETECTION is decoupled from
# EXTRACTION (#269): a waiting agent IS a park even when the command is unreadable;
# _decide_permission handles that (decline + warn, never park).
_permission_pending() {
  [ "$(orca_agent_state "$1" 2>/dev/null)" = waiting ] \
    && [ "$(orca_agent_field "$1" toolName 2>/dev/null)" != AskUserQuestion ]
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

# _reason_permission <wt> <issue> <cmd> <classify_reason> [park_sig] -> the reasoner
# decides a permission dialog the fixed rules would NOT auto-approve (#241 §2: the reasoner decides
# even irreversible asks). It runs in run_answerer's read-only snapshot copy and answers
# 'ANSWER: APPROVE' or 'ANSWER: DENY: <reversible path>'. APPROVE delivers Yes; DENY (or any
# unclear reply — the safe default) declines the dialog and injects the reversible-path guidance.
# Either way the taken decision is warned + journaled with its reversibility class, and the spoke
# is NEVER parked. park_sig identifies the park being decided and is the CALLER's, captured before
# the minutes-long reason step.
_reason_permission() {
  local wt="$1" issue="$2" cmd="$3" why="$4" sig="${5:-}" want="${6:-}" q raw rc ans text rev guidance arc
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
  # #300 step 3b: label the reasoner's answer_computed event on the PERMISSION lane (run_answerer
  # defaults to the answer lane); the env scopes to this one call.
  raw="$(AFK_TLOG_LANE=permission run_answerer "$issue" "$q" "$wt")"; rc=$?
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
      # #300 step 3b: the reasoned permission verdict — approve_decided, keyed by the park episode
      # the caller already captured ($sig). The delivery (approval_injected) is hub-inject's.
      _gb_lane_event "$issue" approve_decided permission "$sig" \
        '{"decision":"approve","kind":"reasoned"}'
      # #241 review B1: journal the decision to the FILE BEFORE approve_permission delivers the
      # keypress (durable if the inject crashes/races the command it authorized) — file-only, so
      # no network gh-comment sits on the spoke's unblock critical path. The pre-keypress line is
      # PROVISIONAL ("APPROVING", present-continuous) so a per-record read can never mistake the
      # in-flight intent for a delivered-and-ran approval; the OUTCOME line (delivered / FAILED)
      # is written after (#241 review).
      _broker_journal_line "$issue" permission "reasoner APPROVING (delivery pending): $cmd" "${rev:-unknown}"
      # Thread the issue + lane + episode so hub-inject's approval_injected delivery event keys on
      # the broker's KNOWN issue (explicit beats the branch-slug fallback) and carries the episode.
      arc=0
      AFK_TLOG_ISSUE="$issue" AFK_TLOG_LANE=permission \
        AFK_TLOG_EPISODE="$(_gb_episode_key "$issue" "$sig")" approve_permission "$wt" "$want" || arc=$?
      if [ "$arc" -eq 0 ]; then
        _broker_journal_line "$issue" permission "reasoner APPROVED (delivered): $cmd" "${rev:-unknown}"
        _reason_permission_record "$wt" "$issue" "reasoner APPROVED (delivered): $cmd" "${rev:-unknown}"
      elif [ "$arc" -eq 3 ]; then
        # The dialog went away (or was replaced) while the reasoner ran: nothing was sent, nothing
        # failed -- no warn, no backoff arm; the next tick re-reads whatever is waiting.
        _broker_journal_line "$issue" permission "reasoner APPROVED but the dialog was gone or replaced -- nothing sent: $cmd" "${rev:-unknown}"
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
      # #300 step 3b: the reasoned decline is the same lane decision, recorded with decision=deny.
      _gb_lane_event "$issue" approve_decided permission "$sig" \
        '{"decision":"deny","kind":"reasoned"}'
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

# _decide_permission <wt_path> <issue> [park_sig] -> classify the spoke's pending permission dialog
# and act. AUTO-APPROVE a safe scoped self-op (mechanical fast path, unchanged, unwarned). Anything
# the fixed rules will not auto-approve — an ESCALATE verdict or an unreadable command — no longer
# parks the spoke: it routes to the always-answering reasoner (#241) which approves a safe
# command or declines-and-redirects a risky one, warning + journaling the taken decision.
#
# park_sig is the caller's already-captured signature of the park being decided; self-derived when
# absent so a direct caller still works.
_decide_permission() {
  local wt="$1" issue="$2" sig="${3:-}" cmd cmd_display decision kind reason want arc
  cmd="$(extract_pending_command "$wt")"
  want="$(orca_agent_field "$wt" toolInput 2>/dev/null)"   # the dialog being judged: approve only THIS one
  if [ -z "$cmd" ]; then
    # Unreadable command: cannot classify. Decline it (the reversible action) + warn — never
    # park. The spoke gets a denial and keeps going; the backoff paces any retry. Nothing is
    # served here (a decline is not an approve), so this path needs no signature at all.
    stamp_answer_attempt "$issue"
    _deny_permission "$wt" "Declined an unreadable permission command — re-issue it in a clearer form." || true
    broker_warn_continue "$wt" "$issue" permission "declined an unreadable permission command" reversible
    return 0
  fi
  # Self-derived only for a direct caller (broker_service_gate passes its own): after the unreadable
  # early return, so the decline path never pays _broker_park_signature's pane read (#269).
  [ -n "$sig" ] || sig="$(_broker_park_signature "$wt" "$issue")"
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
    # #300 step 3b: the MECHANICAL auto-approve is a lane decision too — approve_decided with
    # kind=mechanical, so a detector tells the fixed-rule fast path from a reasoned verdict.
    _gb_lane_event "$issue" approve_decided permission "$sig" \
        '{"decision":"approve","kind":"mechanical"}'
    # Stamp the delivery attempt FIRST: the approve→resume window must not read as idle.
    stamp_answer_attempt "$issue"
    # Thread the issue + lane + episode so hub-inject's approval_injected delivery event keys on
    # the broker's KNOWN issue (explicit beats the branch-slug fallback) and carries the episode.
    arc=0
    AFK_TLOG_ISSUE="$issue" AFK_TLOG_LANE=permission \
      AFK_TLOG_EPISODE="$(_gb_episode_key "$issue" "$sig")" approve_permission "$wt" "$want" || arc=$?
    if [ "$arc" -eq 0 ]; then
      log "  approved permission for #$issue"
      afk_emit_decision "$wt" success
      return 0
    fi
    # Nothing to approve (the dialog went away or was replaced): not a failure, nothing to retry.
    [ "$arc" -ne 3 ] || { log "  #$issue: the dialog is gone or was replaced -- nothing approved"; return 0; }
    # Delivery failed — warn + retry on the backoff, never park (#241).
    broker_warn_continue "$wt" "$issue" permission "could not deliver the approval to the spoke — will retry" reversible
    return 0
  fi
  # ESCALATE: the fixed rules will not auto-approve this one. The reasoner decides it (#241) —
  # approve a safe/reversible command, or decline an irreversible one and name the reversible
  # path — and warns + journals the taken decision. Never park.
  _reason_permission "$wt" "$issue" "$cmd" "$reason" "$sig" "$want"
}

# --- programmatic PreToolUse permission decision (issue #253) ------------------
# The Orca answering path above (extract_pending_command + approve_permission) detects and
# OPERATES a TUI dialog after it appears — the brittle surface
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
  # Spoke self-limit: an issue (the identity record, else the branch slug) AND a live supervisor.
  # Either missing ⇒ stay silent so the normal permission flow (and any attended user) is
  # untouched.
  issue="$(_afk_wt_identity_issue "$wt")" || issue=""
  if [ -z "$issue" ]; then
    br="$(git -C "$wt" branch --show-current 2>/dev/null)" || return 0
    slug="${br##*/}"; issue="${slug%%[!0-9]*}"
  fi
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

# _afk_wt_identity_issue <wt> -> the numeric issue the worktree's identity record names, or
# nothing (rc 1) when it has no record (#361, S2b). <wt> is a tool cwd and may be a subdirectory,
# so the record is read from the git toplevel (as _afk_spoke_mode reads the mode file). The reader
# itself is loaded by hub-inject.sh; absent => no record is ever read and the caller falls back
# to the branch slug, exactly as before the record existed.
_afk_wt_identity_issue() {
  local root
  declare -F ai_toolkit_identity_issue_at >/dev/null 2>&1 || return 1
  root="$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)" || return 1
  [ -n "$root" ] || return 1
  ai_toolkit_identity_issue_at "$root" 2>/dev/null
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
  # Issue number (best-effort, for the fail-safe gate + the journal): the identity record, else the
  # branch slug. Empty on a detached HEAD or a non-issue branch with no record -- which is EXACTLY
  # why it must NOT be the primary gate.
  issue="$(_afk_wt_identity_issue "$wt")" || issue=""
  if [ -z "$issue" ]; then
    br="$(git -C "$wt" branch --show-current 2>/dev/null || true)"
    slug="${br##*/}"; issue="${slug%%[!0-9]*}"
  fi
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
