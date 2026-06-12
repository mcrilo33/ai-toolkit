#!/usr/bin/env bash
# push-scope-guard — PreToolUse / beforeShellExecution hook guarding git push scope.
#
# TOPOLOGY
#   This repo uses a parallel-worktree model: the MAIN CHECKOUT ("hub") stays on
#   the default branch and acts as the planning hub; each task lives in a LINKED
#   WORKTREE ("spoke") on its own branch, driven by its own session.
#
# WHAT IT ENFORCES
#   SPOKE (linked worktree — git-dir matches */.git/worktrees/*):
#     A spoke may ONLY push its own current branch.  Anything else — pushing the
#     default branch, another task's branch, an explicit :dst refspec that names
#     the wrong ref, --delete, --mirror, or --all — is out of scope.
#     Enforcement is via ship_gate_enforce: hard DENY (exit 2) on Cursor's
#     beforeShellExecution, advisory warn + exit 0 on Claude/Copilot.
#
#   HUB (main checkout):
#     Publishing the default branch or the current branch is exactly the hub's
#     job — always silent.  --delete is teardown cleanup — silent.  --mirror and
#     --all are also silently allowed.  Pushing some OTHER task's branch is
#     suspicious (the spoke should be shipping it) → advisory warn on every
#     platform, never a hard deny.
#
#   BARE PUSH (no refspecs):
#     Resolved against the tracked upstream.  Own branch → allow.  Default branch
#     upstream → enforce/warn.  No upstream → allow (git itself will refuse).
#
#   NON-PUSH COMMANDS and NON-REPO DIRS: immediate exit 0 (no-op).
#
# PER-PLATFORM ENFORCEMENT
#   ship_gate_enforce "$INPUT" "<msg>" (from lib/utils.sh):
#     • Cursor beforeShellExecution → deny() → exit 2 (hard block).
#     • All other platforms          → warn() → return 0 (advisory).
#
# Exit 2 = block (Cursor only), Exit 0 = allow.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

# ── Read payload and extract shell command ───────────────────────────────────
INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

# No command → not a shell-execution event; nothing to guard.
[ -z "$COMMAND" ] && exit 0

# Only act when the command contains a git push at a command boundary.
# We deliberately do NOT match `gh pr` here — only `git push`.
if ! printf '%s' "$COMMAND" | grep -qE '(^|[;&|`]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git([[:space:]]+(-[^[:space:]]+|--[^[:space:]]+|-C[[:space:]]+[^[:space:]]+))*[[:space:]]+push\b'; then
  exit 0
fi

# ── Resolve project root and verify we are in a git repo ────────────────────
ROOT=$(project_root_from_payload "$INPUT")
GIT_DIR=$(git -C "$ROOT" rev-parse --absolute-git-dir 2>/dev/null || true)
[ -z "$GIT_DIR" ] && exit 0

# ── Spoke vs hub: a linked worktree's git-dir lives under .git/worktrees/ ───
IS_SPOKE=0
case "$GIT_DIR" in
  */.git/worktrees/*) IS_SPOKE=1 ;;
esac

# ── Resolve default branch (same chain as hub-guard) ────────────────────────
hub_default_branch() {
  local root="$1" def
  def=$(git -C "$root" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||') || true
  [ -n "$def" ] && { printf '%s' "$def"; return 0; }
  def=$(git -C "$root" config --get init.defaultBranch 2>/dev/null || true)
  if [ -n "$def" ] && git -C "$root" show-ref --verify --quiet "refs/heads/$def" 2>/dev/null; then
    printf '%s' "$def"
    return 0
  fi
  for def in main master; do
    git -C "$root" show-ref --verify --quiet "refs/heads/$def" 2>/dev/null && { printf '%s' "$def"; return 0; }
  done
  printf 'main'
}

DEFAULT=$(hub_default_branch "$ROOT")
CURRENT=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
[ -z "$CURRENT" ] && exit 0

# ── Parse the first git push clause from the command string ─────────────────
# Extract the tail of the first `git [global-opts] push <tail>`: take the rest
# of its line (the -C alternative must appear in BOTH regexes — detection
# without parsing would misjudge `git -C x push origin main` as a bare push),
# neutralize redirections (`2>&1`, `>file` are shell plumbing, not refspecs),
# then cut at the first remaining shell operator (; & | backtick).
PUSH_TAIL=$(printf '%s' "$COMMAND" \
  | grep -oE '(^|[;&|`]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git([[:space:]]+(-C[[:space:]]+[^[:space:]]+|-[^[:space:]]+|--[^[:space:]]+))*[[:space:]]+push([[:space:]].*)?$' \
  | head -1 \
  | sed -E 's/^.*git([[:space:]]+(-C[[:space:]]+[^[:space:]]+|-[^[:space:]]+|--[^[:space:]]+))*[[:space:]]+push[[:space:]]*//' \
  | sed -E 's/[0-9]*>&[0-9]+//g; s/[0-9]*[<>]{1,2}[[:space:]]*[^[:space:];&|]+//g; s/[0-9]*[<>]{1,2}//g' \
  || true)
PUSH_TAIL="${PUSH_TAIL%%[;&|\`]*}"

# Flags we care about
DELETE=0; MIRROR=0; ALL=0
REMOTE=""
REFSPECS=()

# Tokenize PUSH_TAIL. Globbing is off for the unquoted expansion — a refspec
# like 'release/*' must not be rewritten by a matching filename in the cwd.
_skip_next=0
set -f
for _tok in $PUSH_TAIL; do
  if [ "$_skip_next" = "1" ]; then
    _skip_next=0
    continue
  fi
  # Quote characters are shell dressing, not refspec content ('x' names x).
  _tok="${_tok//\'/}"
  _tok="${_tok//\"/}"
  [ -z "$_tok" ] && continue
  # An unexpanded $substitution cannot be adjudicated by a hook — degrade to
  # allow rather than misread it as a foreign refspec (never false-block).
  case "$_tok" in *'$'*) exit 0 ;; esac
  case "$_tok" in
    --delete|-d)      DELETE=1 ;;
    --mirror)         MIRROR=1 ;;
    --all|--branches) ALL=1 ;;
    # flags that consume the next token as their value
    --repo|--receive-pack|--exec|-o|--push-option)
      _skip_next=1 ;;
    # any other flag → ignore
    -*)               ;;
    # non-flag: first is remote, rest are refspecs
    *)
      if [ -z "$REMOTE" ]; then
        REMOTE="$_tok"
      else
        REFSPECS+=("$_tok")
      fi
      ;;
  esac
done
set +f

# ── Helper: normalize a refspec dst to a bare branch name ───────────────────
# Strips refs/heads/ prefix. Returns the result on stdout.
_normalize_dst() {
  local dst="$1"
  # Strip refs/heads/ prefix
  dst="${dst#refs/heads/}"
  printf '%s' "$dst"
}

# ── SPOKE rules ──────────────────────────────────────────────────────────────
if [ "$IS_SPOKE" = "1" ]; then
  OWN_MSG="'$DEFAULT' is published only by the hub's land step — a spoke ships its own branch: git push -u origin $CURRENT"
  SCOPE_MSG="A spoke pushes only its own branch ($CURRENT). Use: git push -u origin $CURRENT"

  # --delete / --mirror / --all → always out of scope for a spoke
  if [ "$DELETE" = "1" ] || [ "$MIRROR" = "1" ] || [ "$ALL" = "1" ]; then
    ship_gate_enforce "$INPUT" "$SCOPE_MSG"
    exit 0
  fi

  # Explicit refspecs
  if [ "${#REFSPECS[@]}" -gt 0 ]; then
    for _spec in "${REFSPECS[@]}"; do
      # Strip leading + (force marker)
      _spec="${_spec#+}"
      # Determine dst
      if printf '%s' "$_spec" | grep -q ':'; then
        _src="${_spec%%:*}"
        _dst="${_spec#*:}"
      else
        _src="$_spec"
        _dst="$_spec"
      fi
      # Empty src means remote delete (:branch)
      if [ -z "$_src" ]; then
        ship_gate_enforce "$INPUT" "$SCOPE_MSG"
        exit 0
      fi
      _dst_norm=$(_normalize_dst "$_dst")
      if [ "$_dst_norm" = "HEAD" ] || [ "$_dst_norm" = "$CURRENT" ]; then
        : # ok
      elif [ "$_dst_norm" = "$DEFAULT" ]; then
        ship_gate_enforce "$INPUT" "$OWN_MSG"
        exit 0
      else
        ship_gate_enforce "$INPUT" "$SCOPE_MSG"
        exit 0
      fi
    done
    exit 0
  fi

  # Bare push (no refspecs) — resolve upstream
  UPSTREAM=$(git -C "$ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
  if [ -z "$UPSTREAM" ]; then
    # No upstream: git itself will refuse; degrade to allow
    exit 0
  fi
  # Strip leading <remote>/ (first path segment only)
  UPSTREAM_BRANCH="${UPSTREAM#*/}"
  if [ "$UPSTREAM_BRANCH" = "$CURRENT" ]; then
    exit 0
  elif [ "$UPSTREAM_BRANCH" = "$DEFAULT" ]; then
    ship_gate_enforce "$INPUT" "$OWN_MSG"
    exit 0
  else
    ship_gate_enforce "$INPUT" "$SCOPE_MSG"
    exit 0
  fi
fi

# ── HUB rules (all advisory, never hard deny) ────────────────────────────────
# --delete from hub = teardown cleanup → silent allow
[ "$DELETE" = "1" ] && exit 0
# --mirror / --all → silent allow
if [ "$MIRROR" = "1" ] || [ "$ALL" = "1" ]; then
  exit 0
fi

# Explicit refspecs: warn only when pushing a branch that isn't DEFAULT or CURRENT
if [ "${#REFSPECS[@]}" -gt 0 ]; then
  for _spec in "${REFSPECS[@]}"; do
    _spec="${_spec#+}"
    if printf '%s' "$_spec" | grep -q ':'; then
      _dst="${_spec#*:}"
    else
      _dst="$_spec"
    fi
    _dst_norm=$(_normalize_dst "$_dst")
    if [ "$_dst_norm" = "HEAD" ] || [ "$_dst_norm" = "$DEFAULT" ] || [ "$_dst_norm" = "$CURRENT" ]; then
      : # ok
    else
      warn "That task branch ($_dst_norm) belongs to its spoke worktree — the spoke ships it via its own push."
      exit 0
    fi
  done
  exit 0
fi

# Bare push from hub → allow silently
exit 0
