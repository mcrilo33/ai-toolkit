#!/usr/bin/env bash
#
# telemetry-ingest-spoke.sh — automated post-run Langfuse ingestion for one spoke.
#
# Usage:
#   scripts/telemetry-ingest-spoke.sh <worktree-dir>       # land-time (raw bodies)
#   scripts/telemetry-ingest-spoke.sh --spoke-run-id <id>  # degraded re-run, id only
#
# worktree-land.sh calls this once, best-effort, AFTER the spoke's work has merged
# and pushed but BEFORE the tmux/worktree teardown — while the worktree (and its
# minted spoke identity + raw request bodies) still exists. The live OTel push
# (worktree-new.sh, AI_TOOLKIT_OTEL=1) only streams native traces; one post-run
# step completes the picture, so no operator ever has to hand-run it:
#
#   (#87) langfuse_spoke_tree.py — itemizes the loaded context (per tool/rule/skill
#         SCHEMA token sizes) from the raw request bodies the spoke dumped on disk,
#         and builds the spoketree-/spokecycle- views. The transcript backfill
#         (#92, langfuse_backfill.py) was retired in #140 — live capture is
#         complete by construction, so there is nothing left to heal.
#
# Contract: this NEVER fails the land. It gates, warns, and returns 0 on every
# path — a missing id, a not-an-OTel spoke, no Langfuse auth, or a failing step.
#
#   OTel gate     the raw-bodies dir exists only under AI_TOOLKIT_OTEL=1, so its
#                 presence is the durable "this was an OTel spoke" signal at land
#                 time. Absent → quiet skip (an ordinary spoke has nothing to push).
#   auth gate     LANGFUSE_BASIC_AUTH unset → one-line skip notice, return 0. The
#                 view builder needs it to reach Langfuse; its absence is not an error.
#   flush wait    a brief settle so the live native-trace push lands before we read
#                 it — the teardown SIGKILL would otherwise drop in-flight spans.
#                 Override with AI_TOOLKIT_INGEST_FLUSH_WAIT (seconds; 0 in tests).
#   retry         the view builder is retried up to AI_TOOLKIT_INGEST_RETRIES (default
#                 3) with an AI_TOOLKIT_INGEST_BACKOFF-based linear wait (default 5s),
#                 so a transient Langfuse hiccup mid-land is survived, not fatal (#151).
#
# Env for the step: LANGFUSE_HOST defaults to http://localhost:3000; the view builder
# runs under PYTHONPATH=<repo>/scripts with python3.12 (matching the telemetry
# package). The package is resolved relative to the repo checkout holding this
# script — NOT relative to the script itself: the synced copy lives at
# <target>/.ai-toolkit/scripts/ with no telemetry/ subpackage (issue #136).
set -uo pipefail

PROG="telemetry-ingest-spoke"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

warn() { printf '%s: %s\n' "$PROG" "$*" >&2; }
info() { printf '%s: %s\n' "$PROG" "$*"; }

# Two invocation modes (issue #151):
#   <worktree-dir>          land-time: read the worktree's spoke-run-id + raw-bodies
#                           and itemize the full loaded context (the OTel gate applies).
#   --spoke-run-id <id>     DEGRADED re-run after teardown: the worktree (and its
#                           raw-bodies) is gone, so rebuild from the id alone on the
#                           disk fallback — the recovery path when a land-time ingest
#                           was lost to a transient Langfuse outage.
WT_DIR=""
ARG_SPOKE_ID=""
# --rebuild (issue #156): purge the two prior view traces and wait until they are gone
# before re-posting, so a view-shape change fully replaces stale span bodies. Threaded
# straight through to the view builder.
REBUILD=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --spoke-run-id)   [ "$#" -ge 2 ] || { warn "--spoke-run-id needs a value"; exit 0; }
                      ARG_SPOKE_ID="$2"; shift 2 ;;
    --spoke-run-id=*) ARG_SPOKE_ID="${1#--spoke-run-id=}"; shift ;;
    --rebuild)        REBUILD="--rebuild"; shift ;;
    --)               shift; break ;;
    -*)               warn "unknown option: $1 — skipping Langfuse ingestion"; exit 0 ;;
    *)                [ -z "$WT_DIR" ] || { warn "unexpected extra argument: $1"; exit 0; }
                      WT_DIR="$1"; shift ;;
  esac
done

# BODY_DIR non-empty ⇒ land-time mode (itemize raw bodies + pin --root); empty ⇒
# degraded id-only mode (disk fallback). Resolve the spoke_run_id per mode.
if [ -n "$ARG_SPOKE_ID" ]; then
  SPOKE_RUN_ID="$ARG_SPOKE_ID"
  BODY_DIR=""
else
  [ -n "$WT_DIR" ] || { warn "usage: telemetry-ingest-spoke.sh <worktree-dir> | --spoke-run-id <id>"; exit 0; }
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
fi

# Auth gate: warn-and-continue, never fail the land. A manual re-run has no
# hand-exported credentials, so resolve them here the way the land does (#127):
# env first, then the shared ~/.afk-telemetry conf, via worktree-lib.sh's
# resolver — a synced sibling in every layout. Absent lib or unresolvable auth
# falls through to the existing skip notice.
if [ -f "$SCRIPT_DIR/worktree-lib.sh" ]; then
  # shellcheck source=worktree-lib.sh
  . "$SCRIPT_DIR/worktree-lib.sh"
  wt_resolve_langfuse_auth || true
fi
if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
  info "LANGFUSE_BASIC_AUTH unset — skipping post-run Langfuse ingestion for $SPOKE_RUN_ID"
  exit 0
fi

# Resolve the telemetry python package: sync_workflow_scripts ships this script
# to <target>/.ai-toolkit/scripts/ but never the package (issue #136), so the
# repo checkout's scripts/telemetry is the canonical home; the SCRIPT_DIR
# sibling stays as a fallback for a non-git install that co-locates it.
# env -u: an inherited git-hook GIT_DIR/GIT_WORK_TREE would override -C
# discovery and resolve a different checkout's package (this repo's documented
# hook-env leak class) — strip both so the answer is always THIS script's repo.
REPO_ROOT="$(env -u GIT_DIR -u GIT_WORK_TREE git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"
TELEMETRY_DIR=""
for _cand in ${REPO_ROOT:+"$REPO_ROOT/scripts/telemetry"} "$SCRIPT_DIR/telemetry"; do
  if [ -f "$_cand/langfuse_spoke_tree.py" ]; then TELEMETRY_DIR="$_cand"; break; fi
done
if [ -z "$TELEMETRY_DIR" ]; then
  warn "telemetry python package not found (probed ${REPO_ROOT:+$REPO_ROOT/scripts/telemetry and }$SCRIPT_DIR/telemetry) — skipping post-run Langfuse ingestion for $SPOKE_RUN_ID"
  exit 0
fi

# Settle the live push before reading it (teardown SIGKILL drops pending spans).
FLUSH_WAIT="${AI_TOOLKIT_INGEST_FLUSH_WAIT:-3}"
[ "$FLUSH_WAIT" = "0" ] || sleep "$FLUSH_WAIT" 2>/dev/null || true

export LANGFUSE_HOST="${LANGFUSE_HOST:-http://localhost:3000}"
export PYTHONPATH="$(dirname "$TELEMETRY_DIR")${PYTHONPATH:+:$PYTHONPATH}"

# Retry budget (issue #151): a co-located Langfuse starved by concurrent spokes can
# drop the request mid-HTTP, losing the trace for good once teardown removes the raw
# bodies. Retry the builder a few times with a linear backoff so a transient hiccup is
# survived. Best-effort throughout — every path still returns 0, never failing the land.
INGEST_RETRIES="${AI_TOOLKIT_INGEST_RETRIES:-3}"
INGEST_BACKOFF="${AI_TOOLKIT_INGEST_BACKOFF:-5}"
case "$INGEST_RETRIES" in '' | *[!0-9]* | 0) INGEST_RETRIES=1 ;; esac
case "$INGEST_BACKOFF" in '' | *[!0-9]*) INGEST_BACKOFF=5 ;; esac

# The step is best-effort: retry on failure, warn and return 0 once the budget is spent.
# UPGRADE: wrap the step in `timeout` (when available — macOS has none by default)
# if a slow/unreachable LANGFUSE_HOST ever stalls a land between push and teardown.
run_step() {
  local label="$1"; shift
  local attempt=1
  info "→ $label for $SPOKE_RUN_ID"
  while :; do
    python3.12 "$@" && return 0
    if [ "$attempt" -ge "$INGEST_RETRIES" ]; then
      warn "$label failed after ${attempt} attempt(s) (continuing) — re-run from the id alone: telemetry-ingest-spoke.sh --spoke-run-id $SPOKE_RUN_ID"
      return 0
    fi
    # 10# forces base-10: the digits-only guard still admits a leading-zero value
    # (08/09), which bare $(( )) would misread as invalid octal and abort the loop.
    warn "$label attempt ${attempt}/${INGEST_RETRIES} failed — retrying in $(( 10#$INGEST_BACKOFF * attempt ))s (transient Langfuse/load?)"
    sleep "$(( 10#$INGEST_BACKOFF * attempt ))" 2>/dev/null || true
    attempt=$(( attempt + 1 ))
  done
}

# Land-time mode itemizes the dumped request bodies and pins --root to the spoke
# checkout (the land runs from the hub root, so without --root the disk fallback would
# itemize the HUB's rules/skills/memory). The degraded id-only re-run has neither, so
# it runs on the bare id and langfuse_spoke_tree's disk fallback (cwd root).
BUILD_ARGS=("$TELEMETRY_DIR/langfuse_spoke_tree.py" "$SPOKE_RUN_ID")
if [ -n "$BODY_DIR" ]; then
  BUILD_ARGS+=(--request-bodies "$BODY_DIR" --root "$WT_DIR")
fi
[ -n "$REBUILD" ] && BUILD_ARGS+=("$REBUILD")
run_step "loaded-context itemization (#87)" "${BUILD_ARGS[@]}"

exit 0
