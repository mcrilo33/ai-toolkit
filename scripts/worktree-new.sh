#!/usr/bin/env bash
#
# worktree-new.sh — create an isolated git worktree for one task and wire it into
# a "multiple terminals + one review window" workflow:
#   - folds the worktree into your single VS Code review window (`code --add`)
#   - opens a tmux window cd'd into it in session 0, launching `claude`
#
# One task = one issue = one branch = one checkout = its own staging area, hooks,
# and .review/ approval artifacts (the isolation solo-cycle/close-task assume).
#
# Usage:
#   scripts/worktree-new.sh <issue> [slug] [type] [flags]
#
#   <issue>  GitHub issue number (or a bare slug for ad-hoc work)
#   [slug]   short branch slug; derived from the issue title when omitted (needs gh)
#   [type]   feature | fix | chore   (default: feature)
#
#   -t, --type <t>   branch type (feature|fix|chore) — unambiguous, beats the
#                    positional [type] slot
#   --prompt <text>  seed the spawned claude with this first message (e.g. /source
#                    or a task kickoff) — used by the start-task skill to dispatch
#   --new-window     open a SEPARATE VS Code window instead of code --add
#   --no-code        don't touch VS Code
#   --no-terminal    don't spawn a tmux/terminal window
#   --no-agent       spawn the terminal but don't launch `claude` in it
#
# Env: WT_AGENT_MODEL / WT_AGENT_EFFORT pin the spawned agent's model and effort
#      (defaults: fable / max).
#
# Examples:
#   scripts/worktree-new.sh 42                          # feature/42-<title>, review window + tmux
#   scripts/worktree-new.sh 57 null-pointer fix
#   scripts/worktree-new.sh refactor-sync -t chore      # chore/refactor-sync (ad-hoc + type)
#   scripts/worktree-new.sh 42 --prompt "/source"       # spoke starts anchored to the issue
#
set -euo pipefail

WT_PROG="worktree-new"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

# --- parse flags vs positionals ----------------------------------------------
POSITIONAL=()
OPEN_MODE="add"        # add | new-window | none
SPAWN_TERMINAL=1
LAUNCH_AGENT=1
PROMPT=""              # seed the spawned claude with this first message
TYPE_FLAG=""           # --type overrides the positional type (no footgun)
while [ "$#" -gt 0 ]; do
  case "$1" in
    --new-window)  OPEN_MODE="new-window"; shift ;;
    --no-code)     OPEN_MODE="none"; shift ;;
    --no-terminal) SPAWN_TERMINAL=0; shift ;;
    --no-agent)    LAUNCH_AGENT=0; shift ;;
    -t|--type)     [ "$#" -ge 2 ] || wt_die "--type needs a value"; TYPE_FLAG="$2"; shift 2 ;;
    --type=*)      TYPE_FLAG="${1#--type=}"; shift ;;
    --prompt)      [ "$#" -ge 2 ] || wt_die "--prompt needs a value"; PROMPT="$2"; shift 2 ;;
    --prompt=*)    PROMPT="${1#--prompt=}"; shift ;;
    -*)            wt_die "unknown option: $1" ;;
    *)             POSITIONAL+=("$1"); shift ;;
  esac
done

[ "${#POSITIONAL[@]}" -ge 1 ] || wt_die "usage: worktree-new.sh <issue> [slug] [type] [flags]"
ISSUE="${POSITIONAL[0]}"
SLUG_ARG="${POSITIONAL[1]:-}"
TYPE="${TYPE_FLAG:-${POSITIONAL[2]:-feature}}"   # --type wins, else 3rd positional, else feature

case "$TYPE" in
  feature|fix|chore) ;;
  *) wt_die "type must be one of: feature, fix, chore (got '$TYPE')" ;;
esac

# --- locate the main checkout ------------------------------------------------
# Resolve the MAIN worktree root, so creating from inside an existing worktree
# still places siblings next to the real checkout (not next to a worktree).
git rev-parse --git-dir >/dev/null 2>&1 || wt_die "run this from inside your checkout (cd into the repo first)"
REPO_ROOT="$(wt_main_root)" || wt_die "could not locate the main worktree"
cd "$REPO_ROOT"

# --- derive slug + branch ----------------------------------------------------
# An explicitly-passed slug is still slugified, so spaces or odd characters can
# never produce an invalid git ref.
if [ -n "$SLUG_ARG" ]; then
  SLUG="$(wt_slugify "$SLUG_ARG")"
elif [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  if command -v gh >/dev/null 2>&1; then
    TITLE="$(gh issue view "$ISSUE" --json title -q .title 2>/dev/null || true)"
    if [ -n "$TITLE" ]; then
      SLUG="$(wt_slugify "$TITLE")"
    else
      wt_warn "could not fetch issue #$ISSUE title (gh failed, not authed, or no such issue);"
      wt_warn "falling back to a slug from the number — pass an explicit slug to override."
      SLUG="$(wt_slugify "$ISSUE")"
    fi
  else
    wt_warn "gh not found; cannot fetch issue #$ISSUE title — using the number as the slug."
    SLUG="$(wt_slugify "$ISSUE")"
  fi
else
  SLUG="$(wt_slugify "$ISSUE")"
fi
[ -n "$SLUG" ] || wt_die "could not derive a branch slug; pass one explicitly"

# Branch: feature/<id>-<slug> for numeric issues, <type>/<slug> for ad-hoc.
# (This convention is what source-task / solo-cycle / commit-quality expect —
# which is why we keep the script instead of native `claude -w`'s worktree-<name>.)
if [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  BRANCH="${TYPE}/${ISSUE}-${SLUG}"
  WT_TAG="$ISSUE"
else
  BRANCH="${TYPE}/${SLUG}"
  WT_TAG="$SLUG"
fi

WT_DIR="$(dirname "$REPO_ROOT")/$(basename "$REPO_ROOT")-${WT_TAG}"

# --- create the worktree -----------------------------------------------------
git worktree prune                       # drop stale registrations first
[ -e "$WT_DIR" ] && wt_die "path already exists: $WT_DIR (open it, or remove it first)"
git fetch origin --quiet 2>/dev/null || true

if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  wt_die "branch already exists locally: ${BRANCH} — check it out, or pass a different slug"
fi
if git show-ref --verify --quiet "refs/remotes/origin/${BRANCH}"; then
  wt_warn "branch ${BRANCH} already exists on origin; the new worktree starts a fresh local branch."
fi

echo "→ creating worktree  $WT_DIR"
echo "→ new branch         $BRANCH"
git worktree add "$WT_DIR" -b "$BRANCH"

# .claude/ is gitignored runtime config (skills, hooks, settings) synced from
# shared/. `git worktree add` checks out only TRACKED files, so without this copy
# the worktree has no active skills/hooks. (`.worktreeinclude` would handle this,
# but it only runs for native `claude -w` worktrees, not `git worktree add`.)
# .review/ and *.bak are excluded — .review/ is per-checkout approval state that
# must start empty, or a push could pass on another worktree's approval.
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

echo
echo "✓ worktree ready: $WT_DIR"
echo "  branch:         $BRANCH"

# --- 1. fold into the single VS Code review window ---------------------------
if [ "$OPEN_MODE" != none ]; then
  if command -v code >/dev/null 2>&1; then
    case "$OPEN_MODE" in
      add)
        echo "→ adding to your VS Code review window (code --add)"
        code --add "$WT_DIR" \
          || wt_warn "no VS Code window to add to — open one, then run: code --add \"$WT_DIR\""
        ;;
      new-window)
        echo "→ opening a separate VS Code window"
        code "$WT_DIR"
        ;;
    esac
  else
    wt_warn "'code' CLI not found — in VS Code run: Shell Command: Install 'code' in PATH"
  fi
fi

# --- 2. spawn a terminal/tmux window for the agent ---------------------------
# Build the launch command, optionally seeded with a first prompt that claude
# receives as its initial message (e.g. "/source", or a task kickoff).
# Model+effort are pinned at dispatch time so spokes stay deterministic even
# when user-global settings change; override via WT_AGENT_MODEL / WT_AGENT_EFFORT.
AGENT_CMD="CLAUDE_EFFORT=$(printf '%q' "${WT_AGENT_EFFORT:-max}") claude --model $(printf '%q' "${WT_AGENT_MODEL:-fable}")"
[ -n "$PROMPT" ] && AGENT_CMD="$AGENT_CMD $(printf '%q' "$PROMPT")"

if [ "$SPAWN_TERMINAL" -eq 1 ]; then
  SPAWNED=0
  if command -v tmux >/dev/null 2>&1; then
    win_name="${BRANCH##*/}"
    # ensure session 0 (the spoke home) exists, detached if need be; '=' pins
    # the target to an exact session name so e.g. '0-foo' can never match
    if tmux has-session -t '=0' 2>/dev/null || tmux new-session -d -s 0 -c "$REPO_ROOT" 2>/dev/null; then
      # The launch command is the window's own shell command, not keystrokes:
      # typing it via send-keys raced interactive-zsh init (eaten Enter, zvm) —
      # issue #15. `exec $SHELL` keeps the window alive after claude exits.
      if [ "$LAUNCH_AGENT" -eq 1 ]; then
        win="$(tmux new-window -t '=0:' -P -F '#{window_id}' -n "$win_name" -c "$WT_DIR" \
               "$AGENT_CMD; exec ${SHELL:-zsh}")"
      else
        win="$(tmux new-window -t '=0:' -P -F '#{window_id}' -n "$win_name" -c "$WT_DIR")"
      fi
      # pin name so the running process can't clobber it
      tmux set-window-option -t "$win" automatic-rename off
      tmux set-window-option -t "$win" allow-rename off
      echo "→ opened tmux window '$win_name' ($win) in session 0"
      if [ "$LAUNCH_AGENT" -eq 1 ]; then
        [ -n "$PROMPT" ] && echo "  launched: claude (seeded with first prompt)" || echo "  launched: claude"
      fi
      # print the exact jump command so the caller can copy-paste
      if [ -n "${TMUX:-}" ]; then
        echo "  tmux switch-client -t '0:${win_name}'"
      else
        echo "  tmux attach -t 0 \\; select-window -t '0:${win_name}'"
      fi
      SPAWNED=1
    fi
  fi
  if [ "$SPAWNED" -eq 0 ]; then
    echo
    echo "  Start the agent in a new terminal window:"
    [ "$LAUNCH_AGENT" -eq 1 ] && echo "    cd \"$WT_DIR\" && $AGENT_CMD" || echo "    cd \"$WT_DIR\""
  fi
fi

echo
echo "  Then in that session, run:  /source   (anchor to the issue, then /cycle)"
