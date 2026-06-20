#!/usr/bin/env bash
#
# worktree-quick.sh — create an isolated worktree for the /quick express lane
# (issue #89), then hand the path back so the CURRENT (hub) session enters it.
#
# It is a trimmed worktree-new.sh: same worktree + branch + gitignored .claude/
# copy + spoke_run_id + .ai-toolkit/ exclude, but it DELIBERATELY drops all the
# spoke process ceremony — no GitHub issue, no kickoff prompt, no tmux window, no
# separate `claude` agent. The lane drops *process*, not *quality*: the push-time
# gates (lint/typecheck/tests) still fire from inside the worktree.
#
# Because the work is driven from the hub session, whose cwd is the main checkout
# on the default branch, hub-guard.sh would deny its commits. So the script drops
# the explicit `hub-guard-allow` escape-hatch marker in the common git-dir — the
# conscious override hub-guard.sh honors. The land/teardown removes it.
#
# Usage:
#   scripts/worktree-quick.sh <slug> [-t quick|chore]
#
#   <slug>           short branch slug (slugified; spaces/odd chars are safe)
#   -t, --type <t>   branch type: quick (default) | chore
#
# Prints the worktree path on the final line so the caller can `cd` into it.
#
set -euo pipefail

WT_PROG="worktree-quick"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

# Span start clock for the lifecycle/spawn span emitted at the end.
WT_T0="$(wt_now_ms)"

# --- parse flags vs positionals ----------------------------------------------
POSITIONAL=()
TYPE="quick"
while [ "$#" -gt 0 ]; do
  case "$1" in
    -t|--type) [ "$#" -ge 2 ] || wt_die "--type needs a value"; TYPE="$2"; shift 2 ;;
    --type=*)  TYPE="${1#--type=}"; shift ;;
    -*)        wt_die "unknown option: $1" ;;
    *)         POSITIONAL+=("$1"); shift ;;
  esac
done

[ "${#POSITIONAL[@]}" -ge 1 ] || wt_die "usage: worktree-quick.sh <slug> [-t quick|chore]"
SLUG_ARG="${POSITIONAL[0]}"

case "$TYPE" in
  quick|chore) ;;
  *) wt_die "type must be one of: quick, chore (got '$TYPE')" ;;
esac

# --- locate the main checkout ------------------------------------------------
# Resolve the MAIN worktree root so a quick lane created from inside an existing
# worktree still places siblings next to the real checkout.
git rev-parse --git-dir >/dev/null 2>&1 || wt_die "run this from inside your checkout (cd into the repo first)"
REPO_ROOT="$(wt_main_root)" || wt_die "could not locate the main worktree"
cd "$REPO_ROOT"

# --- derive slug + branch ----------------------------------------------------
SLUG="$(wt_slugify "$SLUG_ARG")"
[ -n "$SLUG" ] || wt_die "could not derive a branch slug; pass one explicitly"
BRANCH="${TYPE}/${SLUG}"
WT_DIR="$(dirname "$REPO_ROOT")/$(basename "$REPO_ROOT")-${SLUG}"

# --- create the worktree -----------------------------------------------------
git worktree prune                       # drop stale registrations first
[ -e "$WT_DIR" ] && wt_die "path already exists: $WT_DIR (open it, or remove it first)"
if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  wt_die "branch already exists locally: ${BRANCH} — check it out, or pass a different slug"
fi

echo "→ creating worktree  $WT_DIR"
echo "→ new branch         $BRANCH"
git worktree add "$WT_DIR" -b "$BRANCH"

# --- set the .ai-toolkit/ exclude (resolved for this worktree) ---------------
# Make .ai-toolkit/ ignored via the repo's git exclude rather than trusting a
# committed .gitignore, so the minted run-id never lands untracked (which would
# break worktree-done's `git worktree remove`). See worktree-new.sh for the
# full rationale.
EXCLUDE_FILE="$(git -C "$WT_DIR" rev-parse --git-path info/exclude 2>/dev/null || true)"
if [ -n "$EXCLUDE_FILE" ]; then
  mkdir -p "$(dirname "$EXCLUDE_FILE")"
  grep -qxF '.ai-toolkit/' "$EXCLUDE_FILE" 2>/dev/null \
    || printf '%s\n' '.ai-toolkit/' >> "$EXCLUDE_FILE"
fi

# --- mint the spoke_run_id ---------------------------------------------------
SPOKE_RUN_ID="${BRANCH}+$(date +%s)"
mkdir -p "$WT_DIR/.ai-toolkit"
printf '%s\n' "$SPOKE_RUN_ID" > "$WT_DIR/.ai-toolkit/spoke-run-id"
echo "→ spoke_run_id       $SPOKE_RUN_ID"

# --- copy the gitignored .claude/ runtime config -----------------------------
# `git worktree add` checks out only TRACKED files, so without this copy the
# worktree has no active skills/hooks. .review/ is per-checkout approval state
# that must start empty; *.bak / worktrees/ are excluded too.
if [ -d "$REPO_ROOT/.claude" ]; then
  echo "→ copying .claude/ runtime config (gitignored; skills + hooks + settings)"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '.review/' --exclude 'worktrees/' --exclude '*.bak' \
      "$REPO_ROOT/.claude/" "$WT_DIR/.claude/"
  else
    cp -R "$REPO_ROOT/.claude" "$WT_DIR/.claude"
    rm -rf "$WT_DIR/.claude/.review" "$WT_DIR/.claude/worktrees"
    find "$WT_DIR/.claude" -name '*.bak' -type f -delete
  fi
fi

# --- grant the hub-guard escape hatch ----------------------------------------
# Drop the explicit override marker in the COMMON git-dir — the exact path
# hub-guard.sh inspects (it resolves the git-dir from the hub's main checkout).
# While present, the hub session may commit into this worktree; worktree-done.sh
# removes it on teardown.
# UPGRADE: the marker is a single global toggle, so a teardown of one /quick lane
# re-blocks any other concurrent lane's hub-driven commits until re-granted —
# add per-lane refcounting if concurrent /quick lanes become common.
COMMON_GIT_DIR="$(git -C "$REPO_ROOT" rev-parse --absolute-git-dir)"
: > "$COMMON_GIT_DIR/hub-guard-allow"
echo "→ granted hub-guard bypass (hub-guard-allow) for hub-session commits"

# --- telemetry: spawn lifecycle marker + script run-node ---------------------
# Attributed to the new worktree (emitted with it as CWD), carrying the
# spoke_run_id minted above. No-op unless AI_TOOLKIT_TELEMETRY=1.
wt_emit_lifecycle "worktree-quick" "spawn" "success" "$WT_T0" "$WT_DIR"
wt_emit_script "worktree-quick" "success" "$WT_T0" "$WT_DIR"

echo
echo "✓ quick worktree ready: $WT_DIR"
echo "  branch:               $BRANCH"
echo
echo "  Enter it from this session, then iterate conversationally:"
echo "    cd $WT_DIR"
echo "$WT_DIR"
