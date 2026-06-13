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
# WHAT IT AUTO-ALLOWS (issue #24)
#   A spoke's own-branch push IS its ship gate; it should not ALSO stall on a
#   permission ask.  When — and only when — every clause is a push that provably
#   resolves (via refspec or tracked upstream) to ONLY the spoke's own current
#   branch, the hook emits hookSpecificOutput.permissionDecision: "allow" and
#   the redundant prompt disappears.  This mirrors rm-scope-guard's discipline:
#   the decision is allow-or-silent, it NEVER denies on this path and never
#   weakens an existing deny (the allow is reached only after the loop above
#   finds nothing to enforce).  It is conservative by construction — force
#   (--force/-f/--force-with-lease/--force-if-includes/+refspec), --delete/:dst,
#   --mirror/--all, --no-verify, tag pushes, default/other-branch pushes, any
#   $-dynamic or otherwise unprovable token, a bare push without an own-branch
#   upstream, the hub side, and any non-push clause in the chain ALL fall
#   through to silent so the normal prompt still fires.  jq-less → silent.
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
#   The command is split into clauses on shell operators (; & | ( ) { }
#   backtick, newlines) and EVERY git push clause is adjudicated — a compliant
#   clause must not launder an out-of-scope one (`git push origin main && git
#   push origin <own>`), and a subshell or $(...) capture is not a disguise.
#   (Trade-off: a branch name containing parens would mis-split, but this
#   repo's worktree-new.sh convention is feature/<id>-<slug>.)  Within a
#   clause, redirections (2>&1, >file) are shell plumbing and are neutralized
#   before tokenizing; quote characters are stripped from tokens; a token
#   carrying an unexpanded $substitution cannot be adjudicated by a hook and
#   degrades to allow — but a CONCRETE refspec in the same clause is still
#   judged (a $var must not smuggle `main` through, including as the src of a
#   src:dst refspec).  A $token still consumes its argument position, so
#   `git push $OPTS origin <own>` reads `origin` as a refspec and false-warns
#   /denies — the accepted cost of not re-opening `git push $REMOTE main`.
#   `git -C <path> push` and `git -c key=val push` are parsed, but scope is
#   judged against the payload root's repo — the hook adjudicates the
#   session's worktree, not arbitrary other checkouts.
#
#   CEILING (degrades to allow, never false-blocks): like the other pattern
#   hooks, this cannot see through eval, `sh -c`, `/usr/bin/git`, or
#   keyword/word prefixes (`if (x); then git push …; fi`, `command git push`,
#   `${X}git push` — the masked prefix glues onto `git`).  A `$(cmd):dst` src
#   is orphaned by the $() split — the inner command is judged as its own
#   clause instead.
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
PUSH_RE='(^|[;&|`(){}]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git([[:space:]]+(-[cC][[:space:]]+[^[:space:]]+|-[^[:space:]]+|--[^[:space:]]+))*[[:space:]]+push\b'
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
# The -C/-c value alternative must mirror PUSH_RE — detection without parsing
# would misjudge `git -C x push origin main` as a bare push.
STRIP_PREFIX='s/^.*git([[:space:]]+(-[cC][[:space:]]+[^[:space:]]+|-[^[:space:]]+|--[^[:space:]]+))*[[:space:]]+push([[:space:]]+|$)//'

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
    # refspecs in the same clause still are, including the CONCRETE dst of a
    # src:dst refspec whose src is dynamic ($SHA:main is still a push to main).
    case "$tok" in
      *'$'*)
        if [ -z "$remote" ]; then
          remote="$tok"
        else
          case "$tok" in
            *:*)
              case "${tok#*:}" in
                *'$'*) dynamic=1 ;;        # dst itself dynamic → unjudgeable
                *)     refspecs+=("$tok") ;; # concrete dst → judged below
              esac
              ;;
            *) dynamic=1 ;;
          esac
        fi
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

# ── Is ONE clause a provably-safe own-branch push? (auto-allow gate) ─────────
# Stricter than judge_clause's silent-pass: this returns 0 ONLY when the clause
# is a push that resolves to the spoke's OWN current branch and carries nothing
# that makes the push non-routine. judge_clause stays the enforcement authority
# (it warns/denies the out-of-scope cases first); this is a conservative second
# opinion that gates the ALLOW emission. Anything it cannot prove → return 1, so
# the command stays silent and the normal permission prompt fires.
#
# Disqualifiers (return 1 → silent, never allow):
#   force (--force/--force-with-lease[=…]/--force-if-includes, a `+`-prefixed
#   refspec, or a short bundle carrying f like -f/-fu) — history rewrites;
#   --delete or a short bundle carrying d and a :dst refspec (deletes);
#   --mirror/--all/--branches (bulk); --tags/--follow-tags or a tag refspec
#   (tags are not task branches); --no-verify (skips the push hooks — the gate
#   this allow sits downstream of); any $-dynamic token (unprovable); and a bare
#   push whose tracked upstream is not the own branch.
clause_auto_allowable() {
  local clause="$1" tail tok spec src dst upstream
  local skip_next=0 remote="" had_refspec=0

  tail=$(printf '%s' "$clause" | sed -E "$STRIP_PREFIX" | strip_redirections)

  set -f
  for tok in $tail; do
    if [ "$skip_next" = "1" ]; then
      skip_next=0
      continue
    fi
    tok="${tok//\'/}"
    tok="${tok//\"/}"
    [ -z "$tok" ] && continue
    case "$tok" in
      --force | --force-with-lease | --force-with-lease=* | --force-if-includes | \
        --delete | --mirror | --all | --branches | --no-verify | --tags | --follow-tags)
        set +f
        return 1
        ;;
      # flags that consume the next token as their value (mirror judge_clause)
      --repo | --receive-pack | --exec | -o | --push-option)
        skip_next=1
        continue
        ;;
      # any other long flag is scope-neutral (e.g. --set-upstream, --quiet)
      --*) continue ;;
      # a single-dash short bundle is force/delete if it carries f or d
      # (`-f`, `-fu`, `-uf`, `-d`); git's parser splits bundled short options.
      -*[fd]*)
        set +f
        return 1
        ;;
      # other short flags are scope-neutral (-u, -q, -v, -n)
      -*) continue ;;
    esac
    # A $-dynamic token cannot be adjudicated → cannot prove own-branch.
    case "$tok" in
      *'$'*)
        set +f
        return 1
        ;;
    esac
    if [ -z "$remote" ]; then
      remote="$tok"
      continue
    fi
    had_refspec=1
    spec="${tok#+}"
    [ "$spec" != "$tok" ] && { set +f; return 1; } # +refspec = forced push
    case "$spec" in
      refs/tags/* | *:refs/tags/*)
        set +f
        return 1
        ;;
      *:*) src="${spec%%:*}"; dst="${spec#*:}" ;;
      *)   src="$spec";       dst="$spec" ;;
    esac
    [ -z "$src" ] && { set +f; return 1; } # :dst = delete
    dst="${dst#refs/heads/}"
    if [ "$dst" != "HEAD" ] && [ "$dst" != "$CURRENT" ]; then
      set +f
      return 1
    fi
  done
  set +f

  # Every concrete refspec named the own branch → safe.
  [ "$had_refspec" = "1" ] && return 0

  # Bare push: allow ONLY when the tracked upstream is the own branch. No
  # upstream (or a default/other-branch upstream) → cannot prove → silent.
  upstream=$(git -C "$ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
  [ -z "$upstream" ] && return 1
  dst="${upstream#*/}"
  [ "$dst" = "$CURRENT" ]
}

# ── Emit a cross-platform ALLOW decision (auto-approve, never deny) ──────────
# Claude reads hookSpecificOutput.permissionDecision; Cursor's
# beforeShellExecution reads the top-level permission. jq is required to emit
# valid JSON; without it (or on any platform that understands neither) the hook
# degrades to SILENT — the user's `Bash(git push)` ask stays the backstop.
allow() {
  local reason="$1"
  command -v jq &>/dev/null || exit 0
  telemetry_event "allow"
  jq -nc --arg r "$reason" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "allow",
      permissionDecisionReason: $r
    },
    permission: "allow"
  }'
  exit 0
}

# ── Split into clauses and judge EVERY git push clause ──────────────────────
# Backticks and ; & | ( ) { } become clause boundaries (newlines already
# are); `&&` and `||` collapse to one boundary.  Parens/braces matter both
# ways: `(git push origin main)` must not slip detection, and `OUT=$(git push
# -u origin <own>)` must not glue the `)` onto the refspec.  An fd-duplication
# like `2>&1` is neutralized FIRST (its `&` is shell plumbing, not a clause
# boundary), so it never severs into a `2>` + standalone `1` pair — that stray
# `1` is a runnable command name and must not be mistaken for benign residue on
# the auto-allow path.  Plain `>file`/`<file` redirects carry no separator, so
# they stay within their clause and are stripped there.
#
# Simple ${NAME} expansions are masked to $DYN FIRST, so the brace spelling of
# a dynamic src cannot sever at the {} boundary and orphan its concrete dst
# (`git push origin ${SHA}:main` must deny exactly like `$SHA:main`).  Only
# the plain ${NAME} form is masked — ${X:-...} could nest a $(git push …) that
# must keep splitting.  A $(cmd):dst src does stay orphaned by the split (the
# inner push is judged instead) — the accepted cost of seeing through $(...).
MASKED=$(printf '%s' "$COMMAND" | sed -E 's/\$\{[A-Za-z_][A-Za-z0-9_]*\}/$DYN/g')
CLAUSES=$(printf '%s\n' "$MASKED" | tr '`' '\n' | sed -E 's/[0-9]*>&[0-9]+//g; s/[;&|(){}]+/\n/g')

# AUTO_ALLOW gates the spoke ALLOW emission: it stays 1 only while EVERY
# non-blank clause is a provably-own-branch push.  A single non-push clause
# (`cd x && git push …`) or any clause judge_clause/clause_auto_allowable cannot
# vouch for drops it to 0 — the command then stays silent (normal prompt).
AUTO_ALLOW=1
PUSH_SEEN=0
while IFS= read -r CLAUSE; do
  case "$CLAUSE" in
    *[![:space:]]*) ;; # non-blank → consider
    *) continue ;;
  esac
  if printf '%s' "$CLAUSE" | grep -qE "$PUSH_RE"; then
    judge_clause "$CLAUSE" # enforces (deny/warn) on any out-of-scope clause
    PUSH_SEEN=1
    clause_auto_allowable "$CLAUSE" || AUTO_ALLOW=0
  else
    # A non-push clause drops the allow — we cannot vouch for an arbitrary
    # command (`cd x`, `tee log`, even a bare `1`) riding alongside the push.
    # Only PURE redirection residue is benign: a `>file`/`<file` fragment that
    # strip_redirections empties out.  fd-duplication (`2>&1`) was neutralized
    # before the clause split, so no standalone digit fragment reaches here;
    # any non-empty remainder is therefore a real command.
    RESIDUE=$(printf '%s' "$CLAUSE" | strip_redirections | tr -d '[:space:]')
    case "$RESIDUE" in
      ?*) AUTO_ALLOW=0 ;;
    esac
  fi
done <<EOF
$CLAUSES
EOF

# Reaching here means no clause enforced (a deny/warn would have exited): a
# spoke push proven to target only its own branch is auto-approved, removing the
# redundant ask.  Everything else falls through to silent.
if [ "$IS_SPOKE" = "1" ] && [ "$PUSH_SEEN" = "1" ] && [ "$AUTO_ALLOW" = "1" ]; then
  allow "push-scope-guard: a linked worktree pushing only its own current branch ($CURRENT) is in scope — auto-allowed (force/delete/mirror/all/tag/other-branch/default-branch pushes still prompt)"
fi

exit 0
