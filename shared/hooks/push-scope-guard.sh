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
#     job — always silent.  --delete (either spelling, flag or :branch refspec)
#     is teardown cleanup — silent.  --mirror, --all, and tag pushes are also
#     silently allowed.  Pushing some OTHER task's branch is suspicious (the
#     spoke should be shipping it) → advisory warn on every platform, never a
#     hard deny.
#
#   BARE PUSH (no refspecs):
#     Resolved against the tracked upstream.  Own branch → allow.  Default branch
#     upstream → enforce/warn.  No upstream → allow (git itself will refuse).
#
#   NON-PUSH COMMANDS and NON-REPO DIRS: immediate exit 0 (no-op).
#
# PARSING
#   The command is split into clauses on shell operators (; & | backtick,
#   newlines) and EVERY git push clause is adjudicated — a compliant clause
#   must not launder an out-of-scope one (`git push origin main && git push
#   origin <own>`).  Within a clause, redirections (2>&1, >file) are shell
#   plumbing and are neutralized before tokenizing; quote characters are
#   stripped from tokens; a token carrying an unexpanded $substitution cannot
#   be adjudicated by a hook and degrades to allow — but a CONCRETE refspec in
#   the same clause is still judged (a $var must not smuggle `main` through).
#   `git -C <path> push` is parsed, but scope is judged against the payload
#   root's repo — the hook adjudicates the session's worktree, not arbitrary
#   other checkouts.
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
PUSH_RE='(^|[;&|`]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git([[:space:]]+(-C[[:space:]]+[^[:space:]]+|-[^[:space:]]+|--[^[:space:]]+))*[[:space:]]+push\b'
if ! printf '%s' "$COMMAND" | grep -qE "$PUSH_RE"; then
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

OWN_MSG="'$DEFAULT' is published only by the hub's land step — a spoke ships its own branch: git push -u origin $CURRENT"
SCOPE_MSG="A spoke pushes only its own branch ($CURRENT). Use: git push -u origin $CURRENT"

# Strip the `git [global-opts] push` prefix from a clause, leaving the tail.
# The -C alternative must mirror PUSH_RE — detection without parsing would
# misjudge `git -C x push origin main` as a bare push.
STRIP_PREFIX='s/^.*git([[:space:]]+(-C[[:space:]]+[^[:space:]]+|-[^[:space:]]+|--[^[:space:]]+))*[[:space:]]+push([[:space:]]+|$)//'

# Neutralize redirections — shell plumbing, not refspecs.  Whole-word forms
# may carry an fd prefix (`2>&1`, `2> file`); a redirect GLUED to a word may
# not (`feature/30>log` pushes feature/30 — fd digits only count as their own
# word).  Quoted targets (`> "my log.txt"`) are consumed as one unit.
strip_redirections() {
  sed -E \
    -e 's/(^|[[:space:]])[0-9]*>&[0-9]+/\1/g' \
    -e "s/(^|[[:space:]])[0-9]*[<>]{1,2}[[:space:]]*(\"[^\"]*\"|'[^']*'|[^[:space:]]+)/\1/g" \
    -e 's/(^|[[:space:]])[0-9]*[<>]{1,2}//g' \
    -e 's/>&[0-9]+//g' \
    -e "s/[<>]{1,2}[[:space:]]*(\"[^\"]*\"|'[^']*'|[^[:space:]]+)//g" \
    -e 's/[<>]{1,2}//g'
}

# ── Adjudicate ONE git push clause ───────────────────────────────────────────
# Allowed clauses return 0; a violation enforces (exit 2 on Cursor) and exits 0
# so the advisory platforms get exactly one warning.
judge_clause() {
  local clause="$1" tail tok spec src dst upstream
  local delete=0 mirror=0 all=0 skip_next=0 dynamic=0 remote=""
  local refspecs=()

  tail=$(printf '%s' "$clause" | sed -E "$STRIP_PREFIX" | strip_redirections)

  # Tokenize. Globbing is off for the unquoted expansion — a refspec like
  # 'release/*' must not be rewritten by a matching filename in the cwd.
  set -f
  for tok in $tail; do
    if [ "$skip_next" = "1" ]; then
      skip_next=0
      continue
    fi
    # Quote characters are shell dressing, not refspec content ('x' names x).
    tok="${tok//\'/}"
    tok="${tok//\"/}"
    [ -z "$tok" ] && continue
    case "$tok" in
      --delete|-d)      delete=1; continue ;;
      --mirror)         mirror=1; continue ;;
      --all|--branches) all=1; continue ;;
      # flags that consume the next token as their value
      --repo|--receive-pack|--exec|-o|--push-option) skip_next=1; continue ;;
      # any other flag is scope-neutral, even with a $value (--force-with-lease=$SHA)
      -*) continue ;;
    esac
    # An unexpanded $substitution cannot be adjudicated by a hook: it consumes
    # its position (remote first, then refspec) but is never judged — concrete
    # refspecs in the same clause still are.
    case "$tok" in
      *'$'*)
        if [ -z "$remote" ]; then remote="$tok"; else dynamic=1; fi
        continue
        ;;
    esac
    if [ -z "$remote" ]; then
      remote="$tok"
    else
      refspecs+=("$tok")
    fi
  done
  set +f

  if [ "$IS_SPOKE" = "1" ]; then
    # --delete / --mirror / --all → always out of scope for a spoke
    if [ "$delete" = "1" ] || [ "$mirror" = "1" ] || [ "$all" = "1" ]; then
      ship_gate_enforce "$INPUT" "$SCOPE_MSG"
      exit 0
    fi

    if [ "${#refspecs[@]}" -gt 0 ]; then
      for spec in "${refspecs[@]}"; do
        spec="${spec#+}" # force marker
        case "$spec" in
          *:*) src="${spec%%:*}"; dst="${spec#*:}" ;;
          *)   src="$spec";       dst="$spec" ;;
        esac
        # Empty src means remote delete (:branch)
        if [ -z "$src" ]; then
          ship_gate_enforce "$INPUT" "$SCOPE_MSG"
          exit 0
        fi
        dst="${dst#refs/heads/}"
        if [ "$dst" = "HEAD" ] || [ "$dst" = "$CURRENT" ]; then
          : # ok
        elif [ "$dst" = "$DEFAULT" ]; then
          ship_gate_enforce "$INPUT" "$OWN_MSG"
          exit 0
        else
          ship_gate_enforce "$INPUT" "$SCOPE_MSG"
          exit 0
        fi
      done
      return 0
    fi

    # An explicit but unexpandable refspec → degrade to allow (never judge a
    # bare-push upstream the command did not name).
    if [ "$dynamic" = "1" ]; then
      return 0
    fi

    # Bare push (no refspecs) — resolve upstream
    upstream=$(git -C "$ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
    if [ -z "$upstream" ]; then
      return 0 # no upstream: git itself will refuse; degrade to allow
    fi
    dst="${upstream#*/}" # strip leading <remote>/ (first path segment only)
    if [ "$dst" = "$CURRENT" ]; then
      return 0
    elif [ "$dst" = "$DEFAULT" ]; then
      ship_gate_enforce "$INPUT" "$OWN_MSG"
      exit 0
    else
      ship_gate_enforce "$INPUT" "$SCOPE_MSG"
      exit 0
    fi
  fi

  # ── HUB rules (all advisory, never hard deny) ──────────────────────────────
  # --delete is teardown cleanup; --mirror / --all are sanctioned hub bulk ops.
  if [ "$delete" = "1" ] || [ "$mirror" = "1" ] || [ "$all" = "1" ]; then
    return 0
  fi
  if [ "${#refspecs[@]}" -gt 0 ]; then
    for spec in "${refspecs[@]}"; do
      spec="${spec#+}"
      case "$spec" in
        :*) continue ;; # refspec-form delete → teardown cleanup, silent
        refs/tags/*|*:refs/tags/*) continue ;; # release tags are not task branches
        *:*) dst="${spec#*:}" ;;
        *)   dst="$spec" ;;
      esac
      dst="${dst#refs/heads/}"
      if [ "$dst" != "HEAD" ] && [ "$dst" != "$DEFAULT" ] && [ "$dst" != "$CURRENT" ]; then
        warn "That task branch ($dst) belongs to its spoke worktree — the spoke ships it via its own push."
        exit 0
      fi
    done
  fi
  return 0
}

# ── Split into clauses and judge EVERY git push clause ──────────────────────
# Backticks and ; & | become clause boundaries (newlines already are); `&&`
# and `||` collapse to one boundary.  `2>&1` splits as `2>` + `1` — the
# residue is neutralized by strip_redirections, and the stray `1` clause
# contains no push, so it is skipped.
CLAUSES=$(printf '%s\n' "$COMMAND" | tr '`' '\n' | sed -E 's/[;&|]+/\n/g')

while IFS= read -r CLAUSE; do
  case "$CLAUSE" in
    *[![:space:]]*) ;; # non-blank → consider
    *) continue ;;
  esac
  printf '%s' "$CLAUSE" | grep -qE "$PUSH_RE" || continue
  judge_clause "$CLAUSE"
done <<EOF
$CLAUSES
EOF

exit 0
