#!/usr/bin/env bash
#
# telemetry-ingest-spoke.sh — automated post-run Langfuse ingestion for one spoke.
#
# Usage:
#   scripts/telemetry-ingest-spoke.sh <worktree-dir>
#
# worktree-land.sh calls this once, best-effort, AFTER the spoke's work has merged
# and pushed but BEFORE the tmux/worktree teardown — while the worktree (and its
# minted spoke identity + raw request bodies) still exists. The live OTel push
# (worktree-new.sh, AI_TOOLKIT_OTEL=1) only streams native traces; two post-run
# steps complete the picture, so no operator ever has to hand-run them:
#
#   (#87) langfuse_spoke_tree.py — itemizes the loaded context (per tool/rule/skill
#         SCHEMA token sizes) from the raw request bodies the spoke dumped on disk.
#   (#92) langfuse_backfill.py --thinking — adds extended-thinking, true causal
#         uuid/parentUuid edges, and coverage of un-instrumented sessions from the
#         transcript.
#
# Contract: this NEVER fails the land. It gates, warns, and returns 0 on every
# path — a missing id, a not-an-OTel spoke, no Langfuse auth, or a failing step.
#
#   OTel gate     the raw-bodies dir exists only under AI_TOOLKIT_OTEL=1, so its
#                 presence is the durable "this was an OTel spoke" signal at land
#                 time. Absent → quiet skip (an ordinary spoke has nothing to push).
#   auth gate     LANGFUSE_BASIC_AUTH unset → one-line skip notice, return 0. The
#                 ingesters need it to reach Langfuse; its absence is not an error.
#   flush wait    a brief settle so the live native-trace push lands before we read
#                 it — the teardown SIGKILL would otherwise drop in-flight spans.
#                 Override with AI_TOOLKIT_INGEST_FLUSH_WAIT (seconds; 0 in tests).
#
# Env for the steps: LANGFUSE_HOST defaults to http://localhost:3000; the ingesters
# run under PYTHONPATH=<scripts> with python3.12 (matching the telemetry package).
set -uo pipefail

PROG="telemetry-ingest-spoke"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

warn() { printf '%s: %s\n' "$PROG" "$*" >&2; }
info() { printf '%s: %s\n' "$PROG" "$*"; }

WT_DIR="${1:-}"
[ -n "$WT_DIR" ] || { warn "usage: telemetry-ingest-spoke.sh <worktree-dir>"; exit 0; }

AIT_DIR="$WT_DIR/.ai-toolkit"
ID_FILE="$AIT_DIR/spoke-run-id"
BODY_DIR="$AIT_DIR/raw-bodies"

# OTel gate: no raw-bodies dir → not an AI_TOOLKIT_OTEL spoke; nothing to ingest.
[ -d "$BODY_DIR" ] || exit 0

# The id every ingester keys on; without it there is nothing to assemble.
if [ ! -r "$ID_FILE" ]; then
  warn "no spoke-run-id under $AIT_DIR — skipping Langfuse ingestion"
  exit 0
fi
SPOKE_RUN_ID="$(head -n1 "$ID_FILE" | tr -d '[:space:]')"
[ -n "$SPOKE_RUN_ID" ] || { warn "spoke-run-id file is empty — skipping Langfuse ingestion"; exit 0; }

# Auth gate: warn-and-continue, never fail the land.
if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
  info "LANGFUSE_BASIC_AUTH unset — skipping post-run Langfuse ingestion for $SPOKE_RUN_ID"
  exit 0
fi

# Settle the live push before reading it (teardown SIGKILL drops pending spans).
FLUSH_WAIT="${AI_TOOLKIT_INGEST_FLUSH_WAIT:-3}"
[ "$FLUSH_WAIT" = "0" ] || sleep "$FLUSH_WAIT" 2>/dev/null || true

export LANGFUSE_HOST="${LANGFUSE_HOST:-http://localhost:3000}"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

# Each step is best-effort: warn on failure, press on to the next, return 0.
# UPGRADE: wrap each step in `timeout` (when available — macOS has none by default)
# if a slow/unreachable LANGFUSE_HOST ever stalls a land between push and teardown.
run_step() {
  local label="$1"; shift
  info "→ $label for $SPOKE_RUN_ID"
  python3.12 "$@" || warn "$label failed (continuing) — re-run by hand: python3.12 $*"
}

# --root pins the spoke checkout: the land script runs from the hub root, so without
# it langfuse_spoke_tree's disk fallback (when no usable request body is found) would
# itemize the HUB's rules/skills/memory instead of the spoke's.
run_step "loaded-context itemization (#87)" \
  "$SCRIPT_DIR/telemetry/langfuse_spoke_tree.py" "$SPOKE_RUN_ID" \
  --request-bodies "$BODY_DIR" --root "$WT_DIR"
# --worktree scopes the transcript backfill to THIS spoke's own Claude Code project dir
# (derived from WT_DIR), so it reads only the spoke's sessions/resumes — never the hub or
# sibling worktrees. Without it the backfill scans every session on the machine and
# cross-attaches unrelated reasoning/content (Issues #92/#98).
run_step "transcript backfill (#92)" \
  "$SCRIPT_DIR/telemetry/langfuse_backfill.py" "$SPOKE_RUN_ID" --thinking --worktree "$WT_DIR"

exit 0
