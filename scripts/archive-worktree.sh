#!/usr/bin/env bash
#
# archive-worktree.sh — Orca's `scripts.archive` hook: spool an OTel spoke's raw request
# bodies + identity out of the worktree before Orca removes it (issue #362, Orca migration S3).
#
# Why a spool, not an ingest: Orca runs the archive hook BEFORE removal, re-runs it on every
# removal attempt, and kills it at ~120 s (which ABORTS the removal). The land-time Langfuse
# ingest is unbounded, so land keeps ingesting synchronously before teardown; this hook only
# covers removals OUTSIDE land (abandon/manual), where .ai-toolkit/raw-bodies would otherwise die
# with the worktree. It is copy-only: no network, no Python, no `orca`.
#
# Usage: archive-worktree.sh [worktree-dir]   # default: $ORCA_WORKTREE_PATH, else the cwd
#
# Layout: <git-common-dir>/ai-toolkit-afk/ingest-spool/<spoke_run_id>/.ai-toolkit/
#         {raw-bodies,spoke-run-id,lane,mode,identity}  ("/" in the id becomes "__"; mirrors the worktree, so
#         `telemetry-ingest-spoke.sh <spool>/<id>` takes the land-time path: raw bodies are
#         itemized and the id is read from the spooled file). The spool sits inside the shared
#         git dir, so the ingest's git-derived enrichments (branch, issue, commit range) read
#         the MAIN checkout there; the spool consumer must account for that (follow-up).
#
# A failure leaves <spool-root>/<dirname>.failed next to the spool (a later success clears it).
#
# Safe under a kill at any point: the new spool is built in a sibling temp dir, the old final
# is renamed to a sibling (never deleted before the new one is in place), then removed. Stale
# siblings of an earlier killed run are cleared at the start, so a re-run converges.
#
# Best-effort, always exit 0 (AFK Design Principle #6): an archive failure must never block a
# removal. Failures are loud on stderr (Principle #2); a non-OTel worktree is a quiet no-op.
set -uo pipefail

warn() { printf 'archive-worktree: %s\n' "$*" >&2; }

WT="${1:-${ORCA_WORKTREE_PATH:-}}"
[ -n "$WT" ] || WT="$(pwd)"

AIT="$WT/.ai-toolkit"
# OTel gate (same as telemetry-ingest-spoke.sh): raw-bodies exists only under AI_TOOLKIT_OTEL=1.
[ -d "$AIT/raw-bodies" ] || exit 0

ID=""
[ -r "$AIT/spoke-run-id" ] && ID="$(head -n1 "$AIT/spoke-run-id" | tr -d '[:space:]')"
if [ -z "$ID" ]; then
  warn "no usable spoke-run-id under $AIT though raw-bodies exists — NOT spooling $WT"
  exit 0
fi
# Real ids are "<branch>+<epoch>" and the branch carries slashes (feature/362-x+17...), so the
# spool dir name flattens "/" to "__" to stay ONE directory level; the ingest re-reads the real
# id from the spoke-run-id file, never from the dir name.
DIRNAME="${ID//\//__}"
case "$DIRNAME" in
  .* | *[!A-Za-z0-9._+-]*)
    warn "spoke-run-id '$ID' cannot be mapped to a safe spool dir name — NOT spooling $WT"
    exit 0 ;;
esac

GCD="$(env -u GIT_DIR -u GIT_WORK_TREE git -C "$WT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || GCD=""
if [ -z "$GCD" ]; then
  warn "$WT is not inside a git worktree — NOT spooling $ID"
  exit 0
fi

ROOT="$GCD/ai-toolkit-afk/ingest-spool"
FINAL="$ROOT/$DIRNAME"
TMP="$ROOT/$DIRNAME.tmp.$$"
OLD="$ROOT/$DIRNAME.old.$$"

mkdir -p "$ROOT" 2>/dev/null || { warn "cannot create spool root $ROOT — NOT spooling $ID"; exit 0; }

# Nobody watches an Orca hook's stderr, so a failure also leaves a durable marker next to the
# spool (AFK Design Principle #2); a later successful run clears it.
FAILED="$ROOT/$DIRNAME.failed"
fail() {
  warn "$1 — NOT spooling $ID"
  printf '%s\n' "$1" > "$FAILED" 2>/dev/null
  rm -rf "$TMP" 2>/dev/null
  exit 0
}

# Settle what an earlier KILLED run left behind. A sibling whose pid suffix is still alive
# belongs to a concurrent run and is left alone. A dead run's .old is the previous good spool:
# put it back if the final is missing (so a failure below cannot lose it), else drop it.
for d in "$ROOT/$DIRNAME".tmp.* "$ROOT/$DIRNAME".old.*; do
  [ -e "$d" ] || continue
  pid="${d##*.}"
  case "$pid" in '' | *[!0-9]*) ;; *) if kill -0 "$pid" 2>/dev/null; then continue; fi ;; esac
  case "$d" in
    *.old.*) [ -e "$FINAL" ] || { mv "$d" "$FINAL" 2>/dev/null && continue; } ;;
  esac
  rm -rf "$d" 2>/dev/null
done

if ! mkdir -p "$TMP/.ai-toolkit" 2>/dev/null \
  || ! cp -R "$AIT/raw-bodies" "$TMP/.ai-toolkit/raw-bodies" 2>/dev/null; then
  fail "could not copy raw-bodies into $TMP"
fi
for f in spoke-run-id lane mode identity; do
  [ -f "$AIT/$f" ] || continue
  cp "$AIT/$f" "$TMP/.ai-toolkit/$f" 2>/dev/null || warn "could not copy $f into the spool"
done

# Swap: final -> .old, tmp -> final, delete .old. The final is never absent for longer than
# one rename, so a kill at ~120 s cannot leave the spool missing.
if [ -e "$FINAL" ] && ! mv "$FINAL" "$OLD" 2>/dev/null; then
  fail "could not move the previous spool aside (kept as is)"
fi
if ! mv "$TMP" "$FINAL" 2>/dev/null; then
  [ -e "$OLD" ] && mv "$OLD" "$FINAL" 2>/dev/null
  fail "could not move the new spool into $FINAL"
fi
rm -rf "$OLD" "$FAILED" 2>/dev/null
exit 0
