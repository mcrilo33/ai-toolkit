#!/usr/bin/env bash
# hub-agent.sh — run a hub-side agent on a trackable surface (issue #245).
#
# Hub-side agent work — pre-land code-reviews, bug-scopers, delta re-reviews —
# otherwise runs invisibly inside the operator's own session: no tmux window, no
# hub-status row, no telemetry. Spokes already get all three. This helper closes
# that gap by giving any headless agent the same surface:
#
#   - a tmux window named `hub:<label>` in the project session (watchable live),
#   - output teed to `<hub-agents-dir>/<label>.log` (survives the run),
#   - start/end records in `<hub-agents-dir>/journal.jsonl` that hub-status reads
#     into its "Hub agents" section,
#   - the headless agent launched with the native-OTel env prefix so its token
#     cost streams to Langfuse (grouped under a `hub-<label>-…` run session),
#     plus one best-effort kind=agent boundary span marking the run.
#
# Usage:
#   hub-agent.sh <label> [--purpose "<text>"] [--no-window] -- <command...>
#
#   <label>       short slug naming the run (e.g. review-236, scope-240). Used
#                 verbatim as the tmux window name and log filename (sanitized to
#                 [A-Za-z0-9._-]).
#   --purpose     one-line human description shown in the hub-status row.
#   --no-window   run the command inline (foreground) instead of in a tmux window
#                 — the automatic fallback when tmux is unavailable.
#   --            everything after it is the command to run (required).
#
# Example (from the hub, before a manual land):
#   .ai-toolkit/scripts/hub-agent.sh review-236 --purpose "pre-land review #236" \
#     -- claude -p "/code-review 236"
#
# `--exec` is the internal worker mode the dispatch re-invokes inside the window;
# it is not a user-facing entry point.
set -uo pipefail

WT_PROG="hub-agent"

# Absolute path to THIS script, so the tmux window (or the inline fallback) can
# re-invoke the worker mode regardless of cwd.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"

# Source the shared worktree/telemetry helpers (wt_tmux_session, the native-OTel
# prefix, the collector/bridge preflights, telemetry_emit_span). Same dual-layout
# ladder hub-status.sh uses: co-located sibling in a synced target, else the
# repo-root scripts/ in the ai-toolkit checkout.
for _cand in \
  "$_script_dir/worktree-lib.sh" \
  "$_script_dir/../../../../scripts/worktree-lib.sh" \
  "$REPO_ROOT/scripts/worktree-lib.sh" \
  "$REPO_ROOT/.ai-toolkit/scripts/worktree-lib.sh"; do
  if [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

# The hub-agents dir (journal + logs). Overridable for tests; else anchored at the
# hub checkout's gitignored .ai-toolkit/.
_ha_agents_dir() {
  if [ -n "${AI_TOOLKIT_HUB_AGENTS_DIR:-}" ]; then
    printf '%s' "$AI_TOOLKIT_HUB_AGENTS_DIR"
  else
    printf '%s' "${REPO_ROOT:-.}/.ai-toolkit/hub-agents"
  fi
}

# Sanitize a label to a filesystem/window-safe slug (metadata, no content).
_ha_safe_label() { printf '%s' "$1" | sed 's/[^A-Za-z0-9._-]/-/g'; }

_ha_iso() { date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true; }
_ha_epoch() { date +%s 2>/dev/null || true; }

# Append one JSON object (key val key val ...) to the journal. Best-effort: a
# missing python3 simply skips the record — the agent still runs. pid/ts_epoch are
# coerced to numbers so hub-status can compute an age.
_ha_journal_append() {
  local jf="$1"; shift
  command -v python3 >/dev/null 2>&1 || return 0
  python3 - "$@" >>"$jf" 2>/dev/null <<'PY'
import json, sys
kv = sys.argv[1:]
obj = {kv[i]: kv[i + 1] for i in range(0, len(kv) - 1, 2)}
for k in ("pid", "ts_epoch"):
    if k in obj:
        try:
            obj[k] = int(obj[k])
        except ValueError:
            pass
print(json.dumps(obj))
PY
}

# --- worker mode -------------------------------------------------------------
# Runs INSIDE the tmux window (or inline). Brackets the command with journal
# records, streams its output to the log, and emits the telemetry boundary span.
mode_exec() {
  local label="" log="" start_ms="" purpose=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --log)       log="$2"; shift 2 ;;
      --start-ms)  start_ms="$2"; shift 2 ;;
      --purpose)   purpose="$2"; shift 2 ;;
      --)          shift; break ;;
      *)           [ -z "$label" ] && label="$1"; shift ;;
    esac
  done
  [ -n "$label" ] || wt_die "internal: --exec needs a label"
  [ "$#" -gt 0 ] || wt_die "internal: --exec needs a command after --"

  local safe_label agents_dir journal run_id
  safe_label="$(_ha_safe_label "$label")"
  agents_dir="$(_ha_agents_dir)"
  journal="$agents_dir/journal.jsonl"
  [ -n "$log" ] || log="$agents_dir/${safe_label}.log"
  mkdir -p "$agents_dir" "$(dirname "$log")" 2>/dev/null || true
  # run_id keys this run in Langfuse AND in the hub-status live view; the pid
  # disambiguates two same-label dispatches within the same second.
  run_id="hub-${safe_label}-$(_ha_epoch)-$$"
  [ -n "$start_ms" ] || start_ms="$(wt_now_ms)"

  _ha_journal_append "$journal" \
    event start label "$safe_label" purpose "$purpose" pid "$$" \
    ts "$(_ha_iso)" ts_epoch "$(_ha_epoch)" run_id "$run_id" log "$log"

  # Native-OTel launch prefix (issue #83), so the headless agent streams its own
  # trace and token cost lands in Langfuse under the run_id's session. Empty when
  # opted out (AI_TOOLKIT_OTEL != 1) — the full opt-out. Resolve the toggle into
  # the REAL AI_TOOLKIT_OTEL env (default-on like worktree-new.sh) and export it:
  # wt_native_otel_prefix self-gates on that exact var, so a local copy would leave
  # the prefix silently empty and the agent uninstrumented.
  wt_resolve_telemetry_config 2>/dev/null || true
  AI_TOOLKIT_OTEL="${AI_TOOLKIT_OTEL:-${AI_TOOLKIT_OTEL_DEFAULT:-1}}"
  export AI_TOOLKIT_OTEL
  local prefix=""
  if [ "$AI_TOOLKIT_OTEL" = "1" ] && command -v wt_native_otel_prefix >/dev/null 2>&1; then
    local body_dir="$agents_dir/${safe_label}.raw-bodies"
    mkdir -p "$body_dir" 2>/dev/null || true
    wt_default_span_endpoint
    prefix="$(wt_native_otel_prefix "$run_id" "$body_dir")"
  fi

  # Run the command with the prefix env, teed to the log. PIPESTATUS[0] is the
  # command's own exit (not tee's) so the status survives the pipe.
  local cmd_q="" a
  for a in "$@"; do cmd_q="${cmd_q}$(printf '%q ' "$a")"; done
  eval "${prefix}${cmd_q}" 2>&1 | tee "$log"
  local status="${PIPESTATUS[0]}"
  local span_status="success"
  [ "$status" -eq 0 ] || span_status="failure"

  # Carry run_id on the end record so hub-status retires THIS run, not a sibling
  # dispatched under the same label (the live view keys on run_id).
  _ha_journal_append "$journal" \
    event end label "$safe_label" run_id "$run_id" status "$span_status" \
    ts "$(_ha_iso)" ts_epoch "$(_ha_epoch)"

  # Telemetry boundary span (kind=agent), a best-effort marker of the run. It is
  # emitted from the hub checkout, which has no spoke-run-id file, so it does NOT
  # join the agent's own hub-<label> Langfuse session — the token cost lands there
  # via the native OTel stream above, not this span. Resolve hub-side Langfuse auth
  # the way worktree-land does (env -> ~/.afk-telemetry) so the sink can fire;
  # best-effort by contract — telemetry never fails the run.
  wt_resolve_langfuse_auth >/dev/null 2>&1 || true
  if command -v telemetry_emit_span >/dev/null 2>&1; then
    telemetry_emit_span --kind agent --name "hub-agent:${safe_label}" \
      --start-ms "$start_ms" --status "$span_status" 2>/dev/null || true
  fi

  exit "$status"
}

# --- dispatch mode -----------------------------------------------------------
mode_dispatch() {
  local label="" purpose="" no_window=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --purpose)   purpose="$2"; shift 2 ;;
      --no-window) no_window=1; shift ;;
      --)          shift; break ;;
      -*)          wt_die "unknown flag '$1'" ;;
      *)           [ -z "$label" ] && label="$1"; shift ;;
    esac
  done
  [ -n "$label" ] || wt_die "no label — usage: hub-agent.sh <label> [--purpose <t>] -- <command>"
  [ "$#" -gt 0 ] || wt_die "no command given — pass it after '--': hub-agent.sh <label> -- <command>"

  local safe_label agents_dir log start_ms
  safe_label="$(_ha_safe_label "$label")"
  agents_dir="$(_ha_agents_dir)"
  log="$agents_dir/${safe_label}.log"
  mkdir -p "$agents_dir" 2>/dev/null || true
  start_ms="$(wt_now_ms)"

  # Bring up the collector + Langfuse bridge best-effort so the agent's native
  # stream has a sink (idempotent, never fails the dispatch). Opt-out safe.
  wt_resolve_telemetry_config 2>/dev/null || true
  AI_TOOLKIT_OTEL="${AI_TOOLKIT_OTEL:-${AI_TOOLKIT_OTEL_DEFAULT:-1}}"
  export AI_TOOLKIT_OTEL
  if [ "$AI_TOOLKIT_OTEL" = "1" ]; then
    command -v wt_otel_collector_preflight >/dev/null 2>&1 && wt_otel_collector_preflight "$REPO_ROOT" || true
    command -v wt_otel_bridge_preflight >/dev/null 2>&1 && wt_otel_bridge_preflight "$REPO_ROOT" || true
  fi

  # Build the worker re-invocation (absolute self path so it resolves in the
  # window's shell).
  local -a worker=(bash "$SELF" --exec "$safe_label" --log "$log" --start-ms "$start_ms")
  [ -n "$purpose" ] && worker+=(--purpose "$purpose")
  worker+=(--)
  worker+=("$@")

  # Prefer a tmux window (watchable + auto-closes on completion). Fall back to an
  # inline foreground run when tmux is unavailable or --no-window is set.
  if [ "$no_window" -eq 0 ] && command -v tmux >/dev/null 2>&1 && [ -n "$REPO_ROOT" ]; then
    local sess win_name cmd_str a
    sess="$(wt_tmux_session "$REPO_ROOT")"
    win_name="hub:${safe_label}"
    if tmux has-session -t "=$sess" 2>/dev/null \
      || tmux new-session -d -s "$sess" -c "$REPO_ROOT" 2>/dev/null; then
      cmd_str=""
      for a in "${worker[@]}"; do cmd_str="${cmd_str}$(printf '%q ' "$a")"; done
      # No `exec $SHELL` tail: the window closes when the agent completes (#245).
      local win
      # A failed new-window (server limit, transient error) must NOT be reported as
      # success — the agent would never run. Require a non-empty window id, else
      # fall through to the inline run so the review actually happens.
      if win="$(tmux new-window -t "=$sess:" -P -F '#{window_id}' -n "$win_name" \
             -c "$REPO_ROOT" "$cmd_str" 2>/dev/null)" && [ -n "$win" ]; then
        tmux set-window-option -t "$win" automatic-rename off 2>/dev/null || true
        tmux set-window-option -t "$win" allow-rename off 2>/dev/null || true
        echo "→ hub agent '$safe_label' running in tmux window '$win_name' ($win)"
        echo "  log: $log"
        if [ -n "${TMUX:-}" ]; then
          echo "  tmux select-window -t '${sess}:${win_name}'"
        else
          echo "  tmux attach -t '${sess}' \\; select-window -t '${sess}:${win_name}'"
        fi
        return 0
      fi
    fi
    wt_warn "tmux present but window spawn failed — running inline"
  fi

  # Inline fallback: run the worker in the foreground, propagating its exit code.
  echo "→ hub agent '$safe_label' running inline (no tmux window)"
  echo "  log: $log"
  "${worker[@]}"
}

# --- entry point -------------------------------------------------------------
if [ "${1:-}" = "--exec" ]; then
  shift
  mode_exec "$@"
fi
mode_dispatch "$@"
