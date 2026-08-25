#!/usr/bin/env bash
# telemetry.sh — the unified workflow span emit layer (schema v1).
#
# Single source of truth for appending one **span** object per event to
#   ${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl
# ONLY when AI_TOOLKIT_TELEMETRY=1 (otherwise a silent no-op that creates
# nothing). One append-only JSONL event type models the whole hub/spoke
# workflow: lifecycle, steps, hooks, scripts (and, later, via the parser,
# skills/agents/todos/human). See docs/telemetry-span-schema.md for the frozen
# contract that downstream issues (parser + dashboard) build against.
#
# SECOND SINK (Issue #83): when AI_TOOLKIT_OTEL_SPAN_ENDPOINT names a local OTLP
# collector, telemetry_emit_span ALSO POSTs the span as OTLP/HTTP-JSON so it
# surfaces in Langfuse grouped under the spoke's session. This sink is INDEPENDENT
# of AI_TOOLKIT_TELEMETRY — it fires on the endpoint var alone (so AI_TOOLKIT_OTEL
# spokes get it even with the events.jsonl push layer off), and is metadata-only +
# invisible just like the push sink.
#
# This file is sourced by BOTH the hook lib (shared/hooks/lib/utils.sh) and the
# worktree/cycle scripts, so it is intentionally self-contained: it defines its
# own minimal project-root resolver and never depends on utils.sh.
#
# PRIVACY CONTRACT (enforced by tests/unit/test_telemetry_span.py):
#   Metadata only. `repo` is a basename, NEVER a path. We NEVER log commands,
#   messages, file paths, or any payload content — only three fixed metadata fields
#   are read out of the hook payload: `session_id`, `hook_event_name`, and (for a
#   ToolUse event) the opaque `tool_use_id`. Nothing else.
#
# INVISIBILITY CONTRACT:
#   Zero bytes on stdout/stderr; never changes the caller's exit code. The whole
#   emit body is redirected and failure-swallowed.

# ── time helpers ────────────────────────────────────────────────────
# Epoch milliseconds, best-effort across platforms. Tiers, fastest first:
#   1. bash 5 $EPOCHREALTIME (seconds.microseconds)  2. python3  3. GNU date %N
#   4. second-precision date (coarse, but always a valid integer).
_telemetry_now_ms() {
  if [ -n "${EPOCHREALTIME:-}" ]; then
    # "1700000000.123456" -> keep ms (first 3 frac digits). $EPOCHREALTIME is
    # locale-formatted: a dot on the C locale, but a COMMA on comma-decimal
    # locales (e.g. fr_FR), so normalize the separator to a dot before splitting
    # seconds/fraction (#352, same locale trap #189 hardened for the process
    # probes). The fractional field is not fixed-width ("…0.5" means 500ms, not
    # 5ms), so right-pad with zeros BEFORE truncating to 3.
    local rt="${EPOCHREALTIME/,/.}"
    local s="${rt%%.*}" frac="${rt#*.}"
    [ "$frac" = "$rt" ] && frac=""   # no separator -> no fraction
    # Inert-safe (invisibility / AFK Principle 6): only all-digit fields reach
    # the arithmetic. On any unexpected format, fall through to the slower tiers
    # rather than surfacing an arithmetic error to the (unredirected) caller.
    case "$s" in ''|*[!0-9]*) s="" ;; esac
    case "$frac" in *[!0-9]*) frac="" ;; esac
    if [ -n "$s" ]; then
      # 10# on BOTH fields: a leading-zero seconds or fraction (e.g. "089" or
      # "045") must be read base-10, not parsed as invalid octal. Safe here
      # because the guards above rejected empty/non-digit s and frac.
      local f="${frac}000"
      printf '%d' "$(( 10#$s * 1000 + 10#${f:0:3} ))"
      return
    fi
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import time; print(int(time.time()*1000))' 2>/dev/null && return
  fi
  local ns
  ns=$(date +%s%N 2>/dev/null)
  case "$ns" in
    *N|"" ) printf '%d' "$(( $(date +%s) * 1000 ))" ;;   # BSD date: %N unsupported
    *)      printf '%d' "$(( ns / 1000000 ))" ;;
  esac
}

# ISO-8601 UTC, second precision (matches the legacy telemetry_event ts).
_telemetry_iso_utc() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

# ISO-8601 UTC for a given epoch-ms value (so ts_start reflects the real start,
# not emit time). Falls back to now() if conversion is unavailable.
_telemetry_iso_from_ms() {
  # NOTE: split declaration — a combined `local ms="$1" secs=$(( ms / 1000 ))`
  # evaluates the arithmetic before `ms` is bound on bash 3.2 (macOS), zeroing
  # `secs` and pinning ts_start to 1970. Keep these on separate lines.
  local ms="$1"
  local secs=$(( ms / 1000 ))
  date -u -r "$secs" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || date -u -d "@$secs" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || _telemetry_iso_utc
}

# ── id helper ───────────────────────────────────────────────────────
# A short opaque span id. Carries no payload content — purely random hex.
_telemetry_span_id() {
  if [ -n "${RANDOM:-}" ]; then
    printf '%04x%04x%04x' "$RANDOM" "$RANDOM" "$RANDOM"
    return
  fi
  od -An -N6 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' || echo "span"
}

# N random bytes as lowercase hex (2*N chars) — for the OTLP trace/span ids
# (trace=16 bytes/32 hex, span=8 bytes/16 hex). Random metadata only, no content.
# Prefers /dev/urandom; falls back to $RANDOM (≈15 bits each) when it is absent.
_telemetry_hex() {
  local bytes="$1" out need
  out=$(od -An -N"$bytes" -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')
  if [ -n "$out" ]; then
    printf '%s' "$out"
    return
  fi
  need=$(( bytes * 2 ))
  out=""
  while [ "${#out}" -lt "$need" ]; do
    out="$out$(printf '%04x' "$((RANDOM))")"
  done
  printf '%s' "${out:0:$need}"
}

# ── context resolvers ───────────────────────────────────────────────
# Project root: prefer Cursor's env, then the Claude payload's workspace_roots,
# else walk up from $PWD to a .git marker. Self-contained (no utils.sh dep).
_telemetry_project_root() {
  local input="${INPUT:-}" root="" dir
  if [ -n "${CURSOR_PROJECT_DIR:-}" ]; then
    echo "$CURSOR_PROJECT_DIR"
    return
  fi
  if [ -n "$input" ] && command -v jq >/dev/null 2>&1; then
    root=$(printf '%s' "$input" | jq -r '.workspace_roots[0] // empty' 2>/dev/null)
  fi
  if [ -n "$root" ] && [ "$root" != "null" ]; then
    echo "$root"
    return
  fi
  dir="$(pwd)"
  while [ "$dir" != "/" ]; do
    [ -e "$dir/.git" ] && { echo "$dir"; return; }
    dir=$(dirname "$dir")
  done
  pwd
}

# session_id: read ONLY this one field out of the hook payload — never anything
# else (privacy). Empty when absent / no jq.
_telemetry_session_id() {
  local input="${INPUT:-}"
  [ -n "$input" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null
}

# hook_event_name: the hook's raising condition (PreToolUse/PostToolUse/SessionStart/
# Stop/UserPromptSubmit/SubagentStop/Notification/PreCompact). Read out of the payload
# like session_id — a fixed-enum metadata field, never content (Issue #82). Recorded on
# kind=hook spans as the trigger. Empty when absent / no payload / no jq.
_telemetry_hook_event() {
  local input="${INPUT:-}"
  [ -n "$input" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  printf '%s' "$input" | jq -r '.hook_event_name // empty' 2>/dev/null
}

# tool_name of the Pre/PostToolUse event a hook is handling (Bash/Write/Read/…) — a fixed
# metadata field read like hook_event, never content. Recorded on kind=hook OTLP spans so the
# Langfuse node names the tool it guarded. Empty when absent / no payload / no jq.
_telemetry_hook_tool_name() {
  local input="${INPUT:-}"
  [ -n "$input" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null
}

# tool_use_id of the Pre/PostToolUse event a hook is handling — the same opaque id the
# parser derives the tool node from, so the hook span nests under its triggering tool
# (Issue #82). Read ONLY for a ToolUse event; other hook events carry no tool to nest
# under (they keep their turn/session parent). Empty when not a tool event / no jq.
_telemetry_hook_tool_use_id() {
  local input="${INPUT:-}"
  [ -n "$input" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  case "$(printf '%s' "$input" | jq -r '.hook_event_name // empty' 2>/dev/null)" in
    PreToolUse | PostToolUse) ;;
    *) return 0 ;;
  esac
  printf '%s' "$input" | jq -r '.tool_use_id // empty' 2>/dev/null
}

# spoke_run_id: minted at worktree-new, written to <root>/.ai-toolkit/spoke-run-id.
# Every span emitted inside that worktree reads it; empty when not in a spoke.
_telemetry_spoke_run_id() {
  local root="$1" f="$1/.ai-toolkit/spoke-run-id"
  [ -f "$f" ] || return 0
  # First line only; trim whitespace. The file holds a metadata id, no content.
  head -n1 "$f" 2>/dev/null | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

# parent-span pointer: the tool_use_id of the Bash call currently running, written
# to <root>/.ai-toolkit/parent-span by the parent-span-export PreToolUse hook
# (Issue #66). A child script or native git-hook reads it to learn its causal
# parent when no more specific id was exported into its env. Empty when absent.
_telemetry_parent_span_file() {
  local f="$1/.ai-toolkit/parent-span"
  [ -f "$f" ] || return 0
  # First line only; trim whitespace. The file holds a metadata id, no content.
  head -n1 "$f" 2>/dev/null | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

# workflow_rev: the ai-toolkit short SHA active at emit time (the A/B anchor).
# Precedence: explicit override → synced-target manifest → ai-toolkit checkout
# git SHA → VERSION file → empty. The manifest matters because a hook running
# inside a synced TARGET repo must report ai-toolkit's rev, not the target's.
_telemetry_workflow_rev() {
  local root="$1" rev=""
  if [ -n "${AI_TOOLKIT_WORKFLOW_REV:-}" ]; then
    echo "$AI_TOOLKIT_WORKFLOW_REV"
    return
  fi
  local manifest="$root/.ai-toolkit-manifest.json"
  if [ -f "$manifest" ] && command -v jq >/dev/null 2>&1; then
    rev=$(jq -r '.toolkit_rev // empty' "$manifest" 2>/dev/null)
    [ -n "$rev" ] && { echo "$rev"; return; }
  fi
  # ai-toolkit checkout itself: the toolkit IS this repo when shared/ + the sync
  # script are present. Use the working-tree SHA.
  if [ -d "$root/shared" ] && [ -f "$root/scripts/sync-to-repo.sh" ]; then
    rev=$(git -C "$root" rev-parse --short HEAD 2>/dev/null) || rev=""
    [ -n "$rev" ] && { echo "$rev"; return; }
  fi
  if [ -f "$root/VERSION" ]; then
    head -n1 "$root/VERSION" 2>/dev/null | tr -d '[:space:]'
    return
  fi
}

# Current branch of the project root (metadata; part of the schema).
_telemetry_branch() {
  git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || true
}

# ── OTLP / Langfuse fan-out sink (Issue #83) ────────────────────────
# One OTLP stringValue attribute object, value minimally JSON-escaped. Only
# caller-constant metadata is ever passed in (kind/phase/status/decision/ids).
_telemetry_otlp_attr() {
  local key="$1" val
  val=$(printf '%s' "$2" | sed 's/\\/\\\\/g; s/"/\\"/g')
  printf '{"key":"%s","value":{"stringValue":"%s"}}' "$key" "$val"
}

# Fan one workflow span out to Langfuse: POST a single OTLP/HTTP-JSON span to the
# local collector at $AI_TOOLKIT_OTEL_SPAN_ENDPOINT, which maps the resource
# spoke_run_id onto a Langfuse session so the node lands under the spoke. This is
# the SECOND, INDEPENDENT sink — gated by the endpoint var (checked by the caller),
# never by AI_TOOLKIT_TELEMETRY.
#
# PRIVACY: only the caller-constant metadata the args carry (kind/name/phase/
# status/decision/reason/human.wait_ms) plus the spoke_run_id and — for a kind=hook
# span only — three fixed payload metadata fields (hook_event/tool_name/tool_use_id)
# reach the body; never a path/command/message/tool_input field.
# INVISIBILITY: curl runs detached with all output redirected and every failure
# swallowed (`( … & ) || true`), so it adds zero bytes and never alters the
# caller's exit code.
#
# Args (positional, from telemetry_emit_span):
#   kind name phase status start_ms human_type human_wait_ms decision reason
_telemetry_emit_otlp_span() {
  local kind="$1" name="$2" phase="$3" status="$4" start_ms="$5" \
        human_type="$6" human_wait_ms="$7" decision="$8" reason="$9"
  {
    local endpoint root spoke_run_id label label_esc \
          now_ms start_ns end_ns trace_id span_id attrs payload duration_ms
    endpoint="${AI_TOOLKIT_OTEL_SPAN_ENDPOINT%/}"
    root=$(_telemetry_project_root)
    spoke_run_id=$(_telemetry_spoke_run_id "$root")

    # Readable short label: "<kind>:<phase>" when a phase is set (gate:gate,
    # step:green, lifecycle:spawn); else the caller's name.
    if [ -n "$phase" ]; then
      label="${kind}:${phase}"
    else
      label="$name"
    fi
    label_esc=$(printf '%s' "$label" | sed 's/\\/\\\\/g; s/"/\\"/g')

    # Span nanos from start_ms (ns = ms * 1e6); no start_ms → start = end = now.
    now_ms=$(_telemetry_now_ms)
    duration_ms=0
    if [ -n "$start_ms" ]; then
      start_ns="${start_ms}000000"
      end_ns="${now_ms}000000"
      duration_ms=$(( now_ms - start_ms ))
      [ "$duration_ms" -lt 0 ] && duration_ms=0
    else
      start_ns="${now_ms}000000"
      end_ns="$start_ns"
    fi

    trace_id=$(_telemetry_hex 16)
    span_id=$(_telemetry_hex 8)

    # Attributes (all stringValue, jq-free): kind always; phase + status; and on a
    # human span the gate's decision (= status) + its wait.
    attrs=$(_telemetry_otlp_attr workflow.kind "$kind")
    [ -n "$phase" ] && attrs="$attrs,$(_telemetry_otlp_attr workflow.phase "$phase")"
    attrs="$attrs,$(_telemetry_otlp_attr status "$status")"
    if [ -n "$human_type" ]; then
      attrs="$attrs,$(_telemetry_otlp_attr decision "$status")"
      attrs="$attrs,$(_telemetry_otlp_attr human.wait_ms "${human_wait_ms:-0}")"
    fi

    # Hook descriptive attributes (Issue #82 fan-out). ONLY for kind=hook, each
    # omitted when empty. The payload reads reuse the captured INPUT (same mechanism
    # as the push sink) and are guarded to hook spans so a step/lifecycle/script span
    # is byte-for-byte unchanged. `decision` is the explicit --decision flag when the
    # caller passed one, else derived from status; `reason` is the optional --reason
    # flag. `duration_ms` mirrors the push sink's computed span duration.
    if [ "$kind" = "hook" ]; then
      local hook_event tool_name tool_use_id hook_decision
      hook_event=$(_telemetry_hook_event)
      [ -n "$hook_event" ] && attrs="$attrs,$(_telemetry_otlp_attr hook_event "$hook_event")"
      tool_name=$(_telemetry_hook_tool_name)
      [ -n "$tool_name" ] && attrs="$attrs,$(_telemetry_otlp_attr tool_name "$tool_name")"
      tool_use_id=$(_telemetry_hook_tool_use_id)
      [ -n "$tool_use_id" ] && attrs="$attrs,$(_telemetry_otlp_attr tool_use_id "$tool_use_id")"
      if [ -n "$decision" ]; then
        hook_decision="$decision"
      else
        case "$status" in
          success) hook_decision="allow" ;;
          deny)    hook_decision="deny" ;;
          warn)    hook_decision="warn" ;;
          failure) hook_decision="block" ;;
          *)       hook_decision="" ;;
        esac
      fi
      [ -n "$hook_decision" ] && attrs="$attrs,$(_telemetry_otlp_attr decision "$hook_decision")"
      [ -n "$reason" ] && attrs="$attrs,$(_telemetry_otlp_attr reason "$reason")"
      attrs="$attrs,$(_telemetry_otlp_attr duration_ms "$duration_ms")"
    fi

    payload="{\"resourceSpans\":[{\"resource\":{\"attributes\":[$(_telemetry_otlp_attr service.name claude-code),$(_telemetry_otlp_attr spoke_run_id "$spoke_run_id")]},\"scopeSpans\":[{\"scope\":{\"name\":\"ai-toolkit.workflow\"},\"spans\":[{\"traceId\":\"$trace_id\",\"spanId\":\"$span_id\",\"name\":\"$label_esc\",\"kind\":1,\"startTimeUnixNano\":\"$start_ns\",\"endTimeUnixNano\":\"$end_ns\",\"attributes\":[$attrs]}]}]}]}"

    ( printf '%s' "$payload" \
        | curl -sS -X POST -H 'Content-Type: application/json' \
            --data @- "$endpoint/v1/traces" >/dev/null 2>&1 & ) || true
  } >/dev/null 2>&1 || true
  return 0
}

# ── the emit helper ─────────────────────────────────────────────────
# Append one span (schema v1) to events.jsonl. Opt-in + invisible + failure-
# swallowed. Flag-based so callers pass only what they have; everything else is
# resolved from context or defaulted to null.
#
# Usage:
#   telemetry_emit_span --kind <k> --name <n> [--phase <p>] [--status <s>]
#                       [--start-ms <ms>] [--span-id <id>] [--parent-id <id>]
#                       [--human-type <t>] [--human-wait-ms <ms>]
#                       [--decision <d>] [--reason <r>]
#
#   --kind     lifecycle|step|hook|script|skill|agent|todo|human|rule (required)
#   --name     span name, a constant (worktree-new | commit-gauntlet | ...) — a
#              caller-supplied label, NEVER payload content (required)
#   --phase    spawn|land|teardown|red|green|review|push (default: null)
#   --status   success|failure|deny|warn|skipped (default: success)
#   --start-ms epoch-ms when the span opened; ts_start + duration_ms derive from
#              it. Omitted → ts_start = now, duration_ms = 0.
#   --decision OTLP-only (kind=hook): the gate's outcome (allow|deny|warn|block|…).
#              Omitted → derived from status. Not written to events.jsonl.
#   --reason   OTLP-only (kind=hook): a short caller-supplied reason for the decision
#              (a constant, never payload content). Omitted/empty → attr omitted.
#   --parent-id  nesting parent. Resolution order (Issue #66): this flag →
#              $TELEMETRY_PARENT_ID (in-process override) → $AI_TOOLKIT_PARENT_SPAN
#              (correlation id a parent shell exports for a child) → [hook only,
#              Issue #82] the payload's .tool_use_id on a Pre/PostToolUse event (so
#              the hook nests under its triggering tool) → <root>/.ai-toolkit/parent-span
#              (the Bash tool_use_id recorded by the parent-span-export hook) → the spoke
#              root ($spoke_run_id, so an in-spoke span hangs off the spoke instead of
#              orphaning) → null when outside a spoke.
telemetry_emit_span() {
  # Parse args FIRST, so both sinks below can read them. Each sink then fires
  # under its OWN gate: the events.jsonl push layer on AI_TOOLKIT_TELEMETRY=1, the
  # OTLP/Langfuse fan-out (Issue #83) on AI_TOOLKIT_OTEL_SPAN_ENDPOINT — independent,
  # so an AI_TOOLKIT_OTEL spoke gets OTLP spans even with the push layer off.
  local kind="" name="" phase="" status="success" start_ms="" span_id="" \
        parent_id="${TELEMETRY_PARENT_ID:-}" human_type="" human_wait_ms="" \
        decision="" reason=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --kind)          kind="$2"; shift 2 ;;
      --name)          name="$2"; shift 2 ;;
      --phase)         phase="$2"; shift 2 ;;
      --status)        status="$2"; shift 2 ;;
      --start-ms)      start_ms="$2"; shift 2 ;;
      --span-id)       span_id="$2"; shift 2 ;;
      --parent-id)     parent_id="$2"; shift 2 ;;
      --human-type)    human_type="$2"; shift 2 ;;
      --human-wait-ms) human_wait_ms="$2"; shift 2 ;;
      --decision)      decision="$2"; shift 2 ;;
      --reason)        reason="$2"; shift 2 ;;
      *)               shift ;;   # ignore unknowns; never fail a caller
    esac
  done

  # ── OTLP/Langfuse sink (Issue #83) — independent of the push gate below. ──
  if [ -n "${AI_TOOLKIT_OTEL_SPAN_ENDPOINT:-}" ] && command -v curl >/dev/null 2>&1; then
    _telemetry_emit_otlp_span \
      "$kind" "$name" "$phase" "$status" "$start_ms" "$human_type" "$human_wait_ms" \
      "$decision" "$reason"
  fi

  # ── events.jsonl push sink — the original schema-v1 behavior, unchanged. ──
  [ "${AI_TOOLKIT_TELEMETRY:-}" = "1" ] || return 0

  {
    local dir end_ms ts_start ts_end duration_ms root repo branch \
          session_id spoke_run_id workflow_rev
    dir="${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}"
    mkdir -p "$dir"

    end_ms=$(_telemetry_now_ms)
    if [ -n "$start_ms" ]; then
      ts_start=$(_telemetry_iso_from_ms "$start_ms")
      duration_ms=$(( end_ms - start_ms ))
      [ "$duration_ms" -lt 0 ] && duration_ms=0
    else
      ts_start=$(_telemetry_iso_utc)
      duration_ms=0
    fi
    ts_end=$(_telemetry_iso_utc)

    [ -n "$span_id" ] || span_id=$(_telemetry_span_id)
    root=$(_telemetry_project_root)
    repo=$(basename "$root"); [ -n "$repo" ] || repo="unknown"
    branch=$(_telemetry_branch "$root")
    session_id=$(_telemetry_session_id)
    spoke_run_id=$(_telemetry_spoke_run_id "$root")
    workflow_rev=$(_telemetry_workflow_rev "$root")

    # parent_id (Issue #66 — script causality). Resolution order:
    #   1. --parent-id flag / $TELEMETRY_PARENT_ID (set above) — explicit caller.
    #   2. $AI_TOOLKIT_PARENT_SPAN — the cross-exec id a parent shell exports for a
    #      child script (and that a native git-hook inherits from its git command).
    #   3. <root>/.ai-toolkit/parent-span — the current Bash tool_use_id recorded by
    #      the parent-span-export hook, for an agent-run script / native git-hook
    #      whose env carries no id (the boundary a hook cannot reach via env).
    #   4. the spoke root ($spoke_run_id) — so an in-spoke span hangs off the spoke
    #      rather than orphaning at null. Outside a spoke this is empty, so parent_id
    #      stays null — the pre-#66 contract.
    [ -n "$parent_id" ] || parent_id="${AI_TOOLKIT_PARENT_SPAN:-}"
    #   2.5 (Issue #82, hook-scoped). A hook firing on a Pre/PostToolUse event nests
    #       under the tool it guards: the payload's .tool_use_id is the same id the
    #       parser derives the tool node from. More specific than the parent-span file
    #       (the last Bash tool_use_id, stale for a non-Bash tool), so it outranks it.
    #       Only a kind=hook span reads it — a step/script/lifecycle span never does.
    if [ -z "$parent_id" ] && [ "$kind" = "hook" ]; then
      parent_id="$(_telemetry_hook_tool_use_id)"
    fi
    [ -n "$parent_id" ] || parent_id="$(_telemetry_parent_span_file "$root")"
    [ -n "$parent_id" ] || parent_id="$spoke_run_id"

    # The hook's raising condition, recorded only on kind=hook spans (Issue #82).
    local hook_event=""
    if [ "$kind" = "hook" ]; then
      hook_event="$(_telemetry_hook_event)"
    fi

    if command -v jq >/dev/null 2>&1; then
      local human='null'
      if [ -n "$human_type" ]; then
        human=$(jq -nc --arg t "$human_type" --argjson w "${human_wait_ms:-0}" \
          '{type: $t, wait_ms: $w}')
      fi
      # Empty strings for optional join keys serialize as JSON null, so the
      # parser sees a clean absent value rather than "".
      jq -nc \
        --arg span_id "$span_id" \
        --arg parent_id "$parent_id" \
        --arg spoke_run_id "$spoke_run_id" \
        --arg session_id "$session_id" \
        --arg workflow_rev "$workflow_rev" \
        --arg repo "$repo" \
        --arg branch "$branch" \
        --arg kind "$kind" \
        --arg name "$name" \
        --arg phase "$phase" \
        --arg ts_start "$ts_start" \
        --arg ts_end "$ts_end" \
        --argjson duration_ms "$duration_ms" \
        --arg status "$status" \
        --argjson human "$human" \
        --arg hook_event "$hook_event" \
        '{
          span_id: $span_id,
          parent_id: (if $parent_id == "" then null else $parent_id end),
          spoke_run_id: (if $spoke_run_id == "" then null else $spoke_run_id end),
          session_id: (if $session_id == "" then null else $session_id end),
          workflow_rev: (if $workflow_rev == "" then null else $workflow_rev end),
          repo: $repo,
          branch: (if $branch == "" then null else $branch end),
          kind: $kind,
          name: $name,
          phase: (if $phase == "" then null else $phase end),
          ts_start: $ts_start,
          ts_end: $ts_end,
          duration_ms: $duration_ms,
          status: $status,
          human: $human,
          hook_event: (if $hook_event == "" then null else $hook_event end),
          summary: null,
          emits: null,
          sidecar_session: null,
          agent_link: null,
          tokens_in: null,
          tokens_out: null,
          cost_usd: null
        }' >> "$dir/events.jsonl"
    else
      # jq-less fallback. Emits the same shape with a string-only escaper; the
      # only free-text fields (kind/name/phase/status) are caller constants.
      _telemetry_json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
      _telemetry_or_null() { [ -n "$1" ] && printf '"%s"' "$(_telemetry_json_str "$1")" || printf 'null'; }
      local human_json='null'
      [ -n "$human_type" ] && human_json="{\"type\":\"$(_telemetry_json_str "$human_type")\",\"wait_ms\":${human_wait_ms:-0}}"
      printf '{"span_id":"%s","parent_id":%s,"spoke_run_id":%s,"session_id":%s,"workflow_rev":%s,"repo":"%s","branch":%s,"kind":"%s","name":"%s","phase":%s,"ts_start":"%s","ts_end":"%s","duration_ms":%d,"status":"%s","human":%s,"hook_event":%s,"summary":null,"emits":null,"sidecar_session":null,"agent_link":null,"tokens_in":null,"tokens_out":null,"cost_usd":null}\n' \
        "$(_telemetry_json_str "$span_id")" \
        "$(_telemetry_or_null "$parent_id")" \
        "$(_telemetry_or_null "$spoke_run_id")" \
        "$(_telemetry_or_null "$session_id")" \
        "$(_telemetry_or_null "$workflow_rev")" \
        "$(_telemetry_json_str "$repo")" \
        "$(_telemetry_or_null "$branch")" \
        "$(_telemetry_json_str "$kind")" \
        "$(_telemetry_json_str "$name")" \
        "$(_telemetry_or_null "$phase")" \
        "$ts_start" "$ts_end" "$duration_ms" \
        "$(_telemetry_json_str "$status")" \
        "$human_json" \
        "$(_telemetry_or_null "$hook_event")" >> "$dir/events.jsonl"
    fi
  } >/dev/null 2>&1 || true
  return 0
}

# ── hook auto-span ──────────────────────────────────────────────────
# Every hook that sources the hook lib (utils.sh) emits exactly one kind=hook
# span at exit, with status = the decision recorded by deny()/warn() (else
# success). A tiny atexit stack lets a hook that needs its OWN cleanup compose
# with the span instead of clobbering the single bash EXIT-trap slot.

_TELEMETRY_ATEXIT=()
_TELEMETRY_HOOK_STATUS="success"

# Register a command to run when the hook exits. Use this from a hook INSTEAD of
# `trap '…' EXIT`, which would replace the span trap. Cleanups run regardless of
# the opt-in gate (so containment/cleanup is never silently skipped).
telemetry_atexit() {
  _TELEMETRY_ATEXIT+=("$1")
}

# Record the hook's decision; the span emitted at exit carries it as `status`.
telemetry_set_status() {
  _TELEMETRY_HOOK_STATUS="${1:-success}"
}

# Cycle steps are NO LONGER emitted as flat per-commit kind=step spans (#100):
# the assembler derives them from the todo ledger (TaskCreate subject +
# TaskUpdate in_progress/completed window). Gate hooks emit only their kind=hook
# span; the step-semantic view is reconstructed downstream from the ledger.
#
# EXCEPTION (#139): the REVIEW and PUSH ledger windows have no automatic
# in-window emission in Claude Code (RED/GREEN get their commit-gate hook spans
# for free), so their step containers render near-empty. The two gates that
# anchor those steps — the review-stamp gate and spoke-push.sh — emit ONE
# gate-time marker span each through the idempotent helper below.

# telemetry_mark_cycle_step <phase> [key] [start_ms]
#
# Emit the gate-time cycle-step marker span — kind=step, name=solo-cycle,
# phase=<phase> (OTLP label "step:<phase>") — ONCE per (phase, key). The
# sentinel file <root>/.ai-toolkit/cycle-step-<phase> records the last emitted
# key; an equal key skips the emission, so a gate retry or a re-push never
# duplicates the marker. <key> defaults to the project root's HEAD sha; an
# unresolvable key emits unconditionally (telemetry over dedup) and writes no
# sentinel. Fires only when a sink is active (AI_TOOLKIT_TELEMETRY=1 or
# AI_TOOLKIT_OTEL_SPAN_ENDPOINT); an inactive run also writes NO sentinel, so
# enabling telemetry later still emits for the same key. Same invisibility
# contract as the rest of this lib: zero output, never changes the caller's
# exit code.
telemetry_mark_cycle_step() {
  {
    [ "${AI_TOOLKIT_TELEMETRY:-}" = "1" ] || [ -n "${AI_TOOLKIT_OTEL_SPAN_ENDPOINT:-}" ] \
      || return 0
    local phase="${1:-}" key="${2:-}" start_ms="${3:-}" root sentinel
    [ -n "$phase" ] || return 0
    root=$(_telemetry_project_root)
    [ -n "$key" ] || key=$(git -C "$root" rev-parse HEAD 2>/dev/null) || key=""
    sentinel="$root/.ai-toolkit/cycle-step-$phase"
    if [ -n "$key" ] && [ "$(head -n1 "$sentinel" 2>/dev/null)" = "$key" ]; then
      return 0
    fi
    telemetry_emit_span --kind step --name solo-cycle --phase "$phase" \
      --start-ms "$start_ms"
    if [ -n "$key" ]; then
      mkdir -p "$root/.ai-toolkit"
      printf '%s\n' "$key" > "$sentinel"
    fi
  } >/dev/null 2>&1 || true
  return 0
}

# EXIT-trap handler: run registered cleanups, then emit the single hook span.
# Never changes the hook's exit status — it does not call `exit`, and the span
# write is failure-swallowed. basename "$0" is the hook script name (metadata).
_telemetry_hook_exit() {
  local rc=$? cmd
  for cmd in "${_TELEMETRY_ATEXIT[@]:-}"; do
    [ -n "$cmd" ] && { eval "$cmd" >/dev/null 2>&1 || true; }
  done
  if [ "${AI_TOOLKIT_TELEMETRY:-}" = "1" ]; then
    telemetry_emit_span --kind hook --name "$(basename "$0")" \
      --status "${_TELEMETRY_HOOK_STATUS:-success}" \
      --start-ms "${_TELEMETRY_HOOK_START_MS:-}"
  fi
  return $rc
}

# Arm the hook span once. Called by utils.sh at source time so the start clock
# is captured near the hook's beginning. The clock is only read when telemetry
# is on, so a disabled run pays no extra `date`; the trap is always installed so
# registered cleanups still run when telemetry is off.
telemetry_arm_hook_span() {
  [ -n "${_TELEMETRY_HOOK_ARMED:-}" ] && return 0
  _TELEMETRY_HOOK_ARMED=1
  [ "${AI_TOOLKIT_TELEMETRY:-}" = "1" ] && _TELEMETRY_HOOK_START_MS="$(_telemetry_now_ms)"
  trap '_telemetry_hook_exit' EXIT
  return 0
}
