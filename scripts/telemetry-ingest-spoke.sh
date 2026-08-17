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
# But never failing the land is not the same as being quiet about it (issue #319). A skip
# that is EXPECTED stays silent; a skip that means the pipeline is BROKEN fires an alarm —
# a warn plus one OS notification, so the signal leaves a land log nobody reads. See alarm().
#
#   OTel gate     the raw-bodies dir exists only under AI_TOOLKIT_OTEL=1, so its
#                 presence is the durable "this was an OTel spoke" signal at land
#                 time. Absent → quiet skip (an ordinary spoke has nothing to push).
#   auth gate     LANGFUSE_BASIC_AUTH unset → one-line skip notice, return 0. The
#                 view builder needs it to reach Langfuse; its absence is not an error.
#   package gate  BROKEN → alarm. Past the two gates above, ingestion was expected, so a
#                 missing package is a broken install (the #319 outage: 51 drain lands).
#   retry spend   BROKEN → alarm. Spending the whole budget means the outage is not
#                 transient, and the spoke's scores die with the raw bodies at teardown.
#   flush wait    a brief settle so the live native-trace push lands before we read
#                 it — the teardown SIGKILL would otherwise drop in-flight spans.
#                 Override with AI_TOOLKIT_INGEST_FLUSH_WAIT (seconds; 0 in tests).
#   retry         the view builder is retried up to AI_TOOLKIT_INGEST_RETRIES (default
#                 3) with an AI_TOOLKIT_INGEST_BACKOFF-based linear wait (default 5s),
#                 so a transient Langfuse hiccup mid-land is survived, not fatal (#151).
#
# Env for the step: LANGFUSE_HOST defaults to http://localhost:3000; the view builder
# runs under PYTHONPATH=<dirname of the resolved package> with python3.12 (matching the
# telemetry package). The package resolves to the repo checkout's scripts/telemetry when
# this script runs from one, else to the CO-LOCATED sibling the sync now ships beside it
# (issue #319) — which is the live path for a synced target and for the /afk drain's
# self-copy. See the resolution block below.
set -uo pipefail

PROG="telemetry-ingest-spoke"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

warn() { printf '%s: %s\n' "$PROG" "$*" >&2; }
info() { printf '%s: %s\n' "$PROG" "$*"; }

# alarm <message> — the LOUD path for a BROKEN ingest (issue #319; AFK Design Principle 2).
#
# Not every skip is a breakage. The OTel and auth gates below are expected states and stay
# quiet. But an ingest that was expected and is now impossible — the package gone, or the
# builder dead after every retry — silently costs cycle-step scores on a land nobody is
# watching. That is what happened: the warn DID print, in 51 land logs over 4 days, and the
# only signal that ever reached a human was an empty dashboard widget. So the stderr line is
# kept as the forensic record, and the alarm ALSO leaves the log via one OS notification.
#
# AI_TOOLKIT_NOTIFY_CMD (an executable handed the message as $1) overrides the default
# osascript for tests and non-macOS hosts — the same seam hub-notify.sh uses for HUB_NOTIFY_CMD.
# Best-effort throughout: a notifier that is missing, broken, or non-macOS must never fail the
# land it is only observing (Principle 6), hence the `|| true` on every path.
# UPGRADE: this duplicates hub-notify.sh's notify(); if a third caller appears, lift a shared
# wt_notify into worktree-lib.sh (already sourced here) rather than copy it again.
alarm() {
  local msg="$1"
  warn "BROKEN — $msg"
  if [ -n "${AI_TOOLKIT_NOTIFY_CMD:-}" ]; then
    "$AI_TOOLKIT_NOTIFY_CMD" "$PROG: $msg" >/dev/null 2>&1 || true
    return 0
  fi
  # Escape for the AppleScript string literal: backslashes FIRST (else the next step's
  # inserted backslashes get doubled), then double-quotes — a raw backslash or quote in a
  # path would otherwise fail to compile and, because of the trailing `|| true`, silently
  # drop the very ping that matters most (the bug hub-notify.sh already learned).
  local esc="${msg//\\/\\\\}"
  esc="${esc//\"/\\\"}"
  osascript -e "display notification \"$esc\" with title \"ai-toolkit: telemetry ingest BROKEN\"" \
    >/dev/null 2>&1 || true
  return 0
}

# --- per-issue cycle-time sources (#280) --------------------------------------
# Gather the lifecycle-timeline instants + drain-window snapshot the view builder cannot derive
# from the Langfuse traces alone (the on-disk afk epochs/ledger + the gh filed time + the land
# instant) into one JSON the builder reads via --lifecycle. Every source is best-effort: an absent
# one is omitted from the JSON, and the builder skips the dependent metric — never a wrong value.

# _lifecycle_afk_state_dir <wt> -> the drain's shared afk state dir, matching gate-broker.sh's
# canonical `_afk_state_dir`: the AFK_STATE_DIR override wins, else the git-common-dir sibling the
# broker/watchdog key on. Empty when neither resolves (the checkout has no git dir).
_lifecycle_afk_state_dir() {
  if [ -n "${AFK_STATE_DIR:-}" ]; then printf '%s\n' "$AFK_STATE_DIR"; return 0; fi
  local common
  common="$(git -C "$1" rev-parse --git-common-dir 2>/dev/null)" || return 0
  # rev-parse may print a relative ".git"; resolve it against the worktree so the path is absolute.
  case "$common" in /*) : ;; *) common="$1/$common" ;; esac
  printf '%s\n' "$common/ai-toolkit-afk"
}

# _lifecycle_read_epoch <file> -> the unix-second epoch in <file> when it holds a bare integer,
# else nothing (a missing/blank/non-numeric file yields no value, so the field is omitted).
_lifecycle_read_epoch() {
  local v
  [ -r "$1" ] || return 0
  v="$(head -n1 "$1" 2>/dev/null | tr -d '[:space:]')"
  case "$v" in '' | *[!0-9]*) return 0 ;; *) printf '%s\n' "$v" ;; esac
}

# write_lifecycle_sources <wt> <out> -> assemble the lifecycle JSON at <out>, or return 1 to skip.
# Skips (return 1) only when the issue number is not derivable from the branch — the dispatch epochs,
# the gh filed time, and the ledger are all keyed by issue number, so an ad-hoc (non-numeric) spoke
# has no per-issue timeline to gather.
write_lifecycle_sources() {
  local wt="$1" out="$2" branch issue state_dir filed dispatched answer landed window_start spokes interventions
  branch="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null)" || return 1
  issue="${branch#*/}"; issue="${issue%%-*}"   # feature/<issue>-<slug> -> <issue>
  case "$issue" in '' | *[!0-9]*) return 1 ;; esac
  landed="$(date +%s)"
  # filed: the issue's creation instant (ISO). Run gh from the worktree so it resolves THIS spoke's
  # repo (not the land's cwd remote). Best-effort — no gh / not authed / no such issue omits the leg.
  filed="$( (cd "$wt" && gh issue view "$issue" --json createdAt -q .createdAt) 2>/dev/null)" || filed=""
  # Default the drain-derived locals so the JSON block below never reads an unset var
  # under `set -u` on bash>=4.4 when no afk state dir exists (issue #284): they are
  # assigned only inside the state-dir branch, which the land case outside a live drain
  # (and CI) skips. spokes/interventions are already `:-`-guarded there.
  dispatched="" answer="" window_start=""
  state_dir="$(_lifecycle_afk_state_dir "$wt")"
  if [ -n "$state_dir" ] && [ -d "$state_dir" ]; then
    dispatched="$(_lifecycle_read_epoch "$state_dir/dispatch-$issue.epoch")"
    answer="$(_lifecycle_read_epoch "$state_dir/answer-attempt-$issue.epoch")"
    spokes=0; window_start=""
    local f v
    for f in "$state_dir"/dispatch-*.epoch; do
      [ -e "$f" ] || continue
      spokes=$((spokes + 1))
      v="$(_lifecycle_read_epoch "$f")"
      [ -n "$v" ] || continue
      { [ -z "$window_start" ] || [ "$v" -lt "$window_start" ]; } && window_start="$v"
    done
    interventions=0
    [ -r "$state_dir/intervention-ledger.jsonl" ] && \
      interventions="$(grep -c . "$state_dir/intervention-ledger.jsonl" 2>/dev/null)" || interventions=0
    case "$interventions" in '' | *[!0-9]*) interventions=0 ;; esac
  fi
  {
    printf '{"issue":"%s","landed":%s' "$issue" "$landed"
    [ -n "$filed" ]        && printf ',"filed":"%s"' "$filed"
    [ -n "$dispatched" ]   && printf ',"dispatched":%s' "$dispatched"
    [ -n "$answer" ]       && printf ',"answer_attempt":%s' "$answer"
    [ -n "$window_start" ] && printf ',"window_start":%s' "$window_start"
    [ -n "${spokes:-}" ]   && printf ',"spokes_serviced":%s' "$spokes"
    [ -n "${interventions:-}" ] && printf ',"interventions":%s' "$interventions"
    printf '}\n'
  } > "$out" 2>/dev/null || return 1
  return 0
}

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
  #
  # LOUD (#319): past the OTel gate this is BROKEN, not benign. worktree-new.sh writes
  # spoke-run-id unconditionally when it mints .ai-toolkit/, and creates raw-bodies only
  # under AI_TOOLKIT_OTEL=1 — strictly later. So a raw-bodies dir implies the id was
  # written, and its absence here means something removed or truncated it. The scores are
  # lost exactly as they are for a missing package, and warning into the same unread land
  # log is the #319 shape all over again.
  if [ ! -r "$ID_FILE" ]; then
    alarm "no spoke-run-id under $AIT_DIR though raw-bodies exists (an OTel spoke is minted with one) — NO Langfuse ingestion for $WT_DIR"
    exit 0
  fi
  SPOKE_RUN_ID="$(head -n1 "$ID_FILE" | tr -d '[:space:]')"
  [ -n "$SPOKE_RUN_ID" ] || { alarm "spoke-run-id file at $ID_FILE is empty — NO Langfuse ingestion for $WT_DIR"; exit 0; }
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

# Resolve the telemetry python package. Two candidates, in order:
#   1. <repo>/scripts/telemetry — a toolkit checkout (the hub), where the package is the
#      canonical source and always wins.
#   2. $SCRIPT_DIR/telemetry — the CO-LOCATED sibling. sync_workflow_scripts ships the
#      package next to this script (issue #319), so this is the live resolution for a synced
#      target AND for the /afk drain's temp self-copy — which is not a git checkout, so
#      candidate 1 resolves to nothing there and this is the only one left. It is NOT a
#      vestigial non-git fallback: deleting it re-opens #319 and every drain land silently
#      stops ingesting. (Before #319 the sync shipped only the .sh files, which is why the
#      old comment here claimed the synced copy had no telemetry/ subpackage — issue #136.)
# env -u: an inherited git-hook GIT_DIR/GIT_WORK_TREE would override -C
# discovery and resolve a different checkout's package (this repo's documented
# hook-env leak class) — strip both so the answer is always THIS script's repo.
REPO_ROOT="$(env -u GIT_DIR -u GIT_WORK_TREE git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"
TELEMETRY_DIR=""
for _cand in ${REPO_ROOT:+"$REPO_ROOT/scripts/telemetry"} "$SCRIPT_DIR/telemetry"; do
  if [ -f "$_cand/langfuse_spoke_tree.py" ]; then TELEMETRY_DIR="$_cand"; break; fi
done
if [ -z "$TELEMETRY_DIR" ]; then
  # LOUD (#319): this is an OTel spoke and auth resolved, so ingestion was EXPECTED — the
  # package's absence is a broken install, not a benign skip, and it costs this spoke's
  # cycle-step scores for good once teardown removes the raw bodies.
  alarm "telemetry python package not found (probed ${REPO_ROOT:+$REPO_ROOT/scripts/telemetry and }$SCRIPT_DIR/telemetry) — NO Langfuse ingestion for $SPOKE_RUN_ID; re-sync the target (scripts/sync-to-repo.sh) to restore it"
  exit 0
fi

# Settle the live push before reading it (teardown SIGKILL drops pending spans).
FLUSH_WAIT="${AI_TOOLKIT_INGEST_FLUSH_WAIT:-3}"
[ "$FLUSH_WAIT" = "0" ] || sleep "$FLUSH_WAIT" 2>/dev/null || true

# env -> config default (LANGFUSE_HOST_DEFAULT, set by wt_resolve_langfuse_auth's
# telemetry-config resolve above, issue #228) -> hardcoded local Langfuse.
export LANGFUSE_HOST="${LANGFUSE_HOST:-${LANGFUSE_HOST_DEFAULT:-http://localhost:3000}}"
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
      # LOUD (#319): the retry budget existed to survive a TRANSIENT hiccup. Spending it
      # whole means the outage is not transient (Langfuse down, no PyYAML in the target, a
      # bad interpreter) and this spoke's scores are lost — as silently as a missing package.
      alarm "$label failed after ${attempt} attempt(s) — NO Langfuse ingestion for $SPOKE_RUN_ID; re-run from the id alone: telemetry-ingest-spoke.sh --spoke-run-id $SPOKE_RUN_ID"
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
  # Repo name (#231): stamp a repo:<name> trace tag so cross-project cost/latency is comparable.
  # Prefer the origin remote's repo basename (strip a trailing .git); fall back to the checkout dir
  # basename for a git-less / remote-less worktree. Best-effort — an empty name just omits the tag.
  REPO_NAME="$(git -C "$WT_DIR" remote get-url origin 2>/dev/null \
    | sed -E 's#.*[/:]##; s#\.git$##')"
  [ -n "$REPO_NAME" ] || REPO_NAME="$(basename "$WT_DIR")"
  [ -n "$REPO_NAME" ] && BUILD_ARGS+=(--repo "$REPO_NAME")
  # Commit timeline nodes (#162): dump the spoke branch's <base>..HEAD commits with numstat for
  # the view builder to synthesize commit:<sha7> nodes. Best-effort — a checkout whose base cannot
  # be resolved (or no commits ahead) yields no dump, so no --commits is passed. The unit separator
  # (\037) delimits the format fields so a commit subject never collides with them.
  #
  # The base is the PRE-MERGE default tip handed in by worktree-land.sh via AI_TOOLKIT_COMMIT_BASE
  # (issue #344): the land runs this AFTER the merge pushes, so origin/main already contains HEAD
  # (worktrees share remote-tracking refs) and origin/main..HEAD is EMPTY. PRE_SHA predates the
  # merge, so PRE_SHA..HEAD captures the spoke's own commits. Fallback to origin/main only for a
  # manual re-run that hands in no base. Resolve-or-skip (#344 guard): an unresolvable base
  # (bare-branch/--local, bad sha) SKIPS --commits rather than falling back to the empty post-push
  # range — absence of a dump is not evidence of zero churn.
  COMMIT_BASE="${AI_TOOLKIT_COMMIT_BASE:-origin/main}"
  COMMITS_DUMP="$AIT_DIR/commits.dump"
  US=$'\037'
  if BASE_SHA="$(git -C "$WT_DIR" rev-parse -q --verify "${COMMIT_BASE}^{commit}" 2>/dev/null)" \
     && git -C "$WT_DIR" log --numstat --format="commit${US}%H${US}%aI${US}%s" \
          "${BASE_SHA}..HEAD" > "$COMMITS_DUMP" 2>/dev/null && [ -s "$COMMITS_DUMP" ]; then
    BUILD_ARGS+=(--commits "$COMMITS_DUMP")
  fi
  # Per-issue cycle-time sources (#280): gather the timeline instants + drain-window snapshot the
  # view builder cannot see from the traces alone into one JSON file and pass --lifecycle. Best-
  # effort throughout — any source that is absent is simply omitted from the JSON, and the builder
  # skips the dependent metric rather than emitting a wrong value; a total failure here never fails
  # the land.
  LIFECYCLE_JSON="$AIT_DIR/lifecycle.json"
  if write_lifecycle_sources "$WT_DIR" "$LIFECYCLE_JSON"; then
    BUILD_ARGS+=(--lifecycle "$LIFECYCLE_JSON")
  fi
fi
[ -n "$REBUILD" ] && BUILD_ARGS+=("$REBUILD")
run_step "loaded-context itemization (#87)" "${BUILD_ARGS[@]}"

exit 0
