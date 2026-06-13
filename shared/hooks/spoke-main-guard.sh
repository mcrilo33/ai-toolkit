#!/usr/bin/env bash
# spoke-main-guard — PreToolUse / beforeShellExecution DENY hook.
#
# TOPOLOGY
#   This repo uses a parallel-worktree model: the MAIN CHECKOUT ("hub") stays on
#   the default branch and lands finished work; each task lives in a LINKED
#   WORKTREE ("spoke") on its own branch, driven by its own session that
#   worktree-new.sh stamps with WT_SPOKE.
#
# WHAT IT CLOSES (issue #32)
#   push-scope-guard (#26) blocks a spoke from PUSHING the default branch, but
#   not from MUTATING the local `main` ref. The #27/#31 spokes ran
#   worktree-land.sh from inside their worktrees, which merged their feature
#   branch into the shared local `main` ref BEFORE any push — the push deny
#   never even fired. This guard makes a spoke unable to touch the local default
#   branch at all.
#
# WHAT IT DENIES (only when this is a spoke — WT_SPOKE set, or a linked worktree)
#   • git checkout <default> / git switch <default>  (incl. -b/-c force-create)
#   • git merge … while <default> IS HEAD  (a merge always targets HEAD)
#   • git branch -f/--force/-M/-m/-c/-C/-d/-D <default>, and a bare
#     `git branch <default>` create
#   • git push to the LOCAL <default> ref (git push . <ref>:<default>) — a push
#     to a NAMED remote (origin:<default>) is push-scope-guard's domain
#   • git update-ref refs/heads/<default> …
#   • git reset … while <default> IS HEAD
#   • invoking worktree-land.sh (the hub-only land script)
#
# WHAT IT STILL ALLOWS (the sanctioned reconciliation flow must not break)
#   • git merge origin/<default> INTO the current feature branch — a merge
#     targets HEAD, so on a feature branch this never touches local <default>.
#   • git fetch origin (read-only).
#
# DISCIPLINE — this is a DENY guard (deny-or-silent), the mirror of the
#   allow-or-silent rm/push/chmod siblings: it builds on lib/scope-guard.sh's
#   quote-aware compound splitter, judges EVERY segment, and DENIES (exit 2, all
#   platforms via deny()) on the first forbidden form. Anything dynamic
#   ($-tokens), unparseable, or ambiguous degrades to SILENT (exit 0) — a deny
#   guard must never false-block legitimate work.
#
# CEILING (degrades to silent, like the other pattern hooks): it cannot see
#   through eval / `sh -c` / aliases, and — mirroring push-scope-guard — it
#   adjudicates the SESSION's repo, not a `git -C <other-checkout>` retarget;
#   the worktree-land.sh deny is the backstop for the script that does the -C
#   dance into the hub.
#
# Exit 2 = block, Exit 0 = allow.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"
source "$HOOK_DIR/lib/scope-guard.sh"

# ── Resolve the repository's default branch (same chain as hub-guard) ────────
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

# Strip the quote dressing git treats as inert ('main' names main).
sm_clean() {
  local t="${1//\'/}"
  printf '%s' "${t//\"/}"
}

# ── Per-subcommand judges ────────────────────────────────────────────────────
# Each reads the tokenized segment (SM_TOKS from index SM_AI), and on a
# forbidden form sets DENY_REASON and returns 1 (stops the walk); otherwise 0.

# checkout/switch: deny a switch to / force-create of the default branch. A file
# restore (`git checkout main -- file`) moves no ref → allowed.
sm_judge_checkout() {
  local sub="$1" k tok create=0 saw_ddash=0 firstpos="" poscount=0
  for ((k = SM_AI; k < ${#SM_TOKS[@]}; k++)); do
    tok=$(sm_clean "${SM_TOKS[$k]}")
    [ -z "$tok" ] && continue
    case "$tok" in
      --) saw_ddash=1; break ;;
      -b | -B | --orphan) [ "$sub" = "checkout" ] && create=1; continue ;;
      -c | -C | --create | --force-create) [ "$sub" = "switch" ] && create=1; continue ;;
      -*) continue ;;
    esac
    poscount=$((poscount + 1))
    [ -z "$firstpos" ] && firstpos="$tok"
  done
  [ "$saw_ddash" = "1" ] && return 0
  [ "$firstpos" = "$DEFAULT" ] || return 0
  # `git checkout main file` (no --) is an ambiguous file restore; only the sole
  # branch positional, a `switch`, or a force-create names the default ref.
  if [ "$create" = "1" ] || [ "$sub" = "switch" ] || [ "$poscount" = "1" ]; then
    DENY_REASON="$MSG_CHECKOUT"
    return 1
  fi
  return 0
}

# merge: a merge always targets HEAD, so it mutates the default branch iff HEAD
# IS the default branch. --abort/--continue/--quit resolve, not start, a merge.
sm_judge_merge() {
  local k
  for ((k = SM_AI; k < ${#SM_TOKS[@]}; k++)); do
    case "$(sm_clean "${SM_TOKS[$k]}")" in
      --abort | --continue | --quit) return 0 ;;
    esac
  done
  [ "$CURRENT" = "$DEFAULT" ] && { DENY_REASON="$MSG_MERGE"; return 1; }
  return 0
}

# branch: deny a force/move/delete/copy of the default ref, or a bare create.
sm_judge_branch() {
  local k tok force=0 any_flag=0 has_default=0 poscount=0
  for ((k = SM_AI; k < ${#SM_TOKS[@]}; k++)); do
    tok=$(sm_clean "${SM_TOKS[$k]}")
    [ -z "$tok" ] && continue
    case "$tok" in
      -f | --force | -M | -m | -C | -c | -d | -D | --delete) force=1; any_flag=1; continue ;;
      -*) any_flag=1; continue ;;
    esac
    poscount=$((poscount + 1))
    [ "$tok" = "$DEFAULT" ] && has_default=1
  done
  [ "$has_default" = "1" ] || return 0
  [ "$force" = "1" ] && { DENY_REASON="$MSG_BRANCH"; return 1; }
  [ "$any_flag" = "0" ] && [ "$poscount" = "1" ] && { DENY_REASON="$MSG_BRANCH"; return 1; }
  return 0
}

# reset: moves HEAD's branch ref; forbidden while the default branch IS HEAD.
sm_judge_reset() {
  [ "$CURRENT" = "$DEFAULT" ] && { DENY_REASON="$MSG_RESET"; return 1; }
  return 0
}

# update-ref: deny any direct write/delete of refs/heads/<default>.
sm_judge_update_ref() {
  local k
  for ((k = SM_AI; k < ${#SM_TOKS[@]}; k++)); do
    case "$(sm_clean "${SM_TOKS[$k]}")" in
      "refs/heads/$DEFAULT") DENY_REASON="$MSG_UPDATEREF"; return 1 ;;
    esac
  done
  return 0
}

# push: deny only a push to the LOCAL <default> ref. A named remote (origin) is
# push-scope-guard's concern; here the remote must be `.` or a filesystem path.
sm_judge_push() {
  local k tok remote="" dst skip=0
  local -a specs=()
  for ((k = SM_AI; k < ${#SM_TOKS[@]}; k++)); do
    tok=$(sm_clean "${SM_TOKS[$k]}")
    [ -z "$tok" ] && continue
    if [ "$skip" = "1" ]; then skip=0; continue; fi
    case "$tok" in
      --repo | --receive-pack | --exec | -o | --push-option) skip=1; continue ;;
      -*) continue ;;
    esac
    if [ -z "$remote" ]; then remote="$tok"; else specs+=("$tok"); fi
  done
  case "$remote" in
    . | ./ | file://* | */*) ;; # explicit local / filesystem path
    *) return 0 ;;              # a named remote (origin, upstream) → not local
  esac
  [ "${#specs[@]}" -gt 0 ] || return 0
  for tok in "${specs[@]}"; do
    tok="${tok#+}"
    case "$tok" in
      *:*) dst="${tok#*:}" ;;
      *) dst="$tok" ;;
    esac
    dst="${dst#refs/heads/}"
    [ "$dst" = "$DEFAULT" ] && { DENY_REASON="$MSG_PUSH"; return 1; }
  done
  return 0
}

# ── Per-segment validator (called by sg_walk_segments) ───────────────────────
# Returns 0 to keep walking, 1 (with DENY_REASON set) to deny.
sm_check_segment() {
  local seg="$1" t sub="" i n
  if printf '%s' "$seg" | grep -qE '(^|[[:space:]/])worktree-land\.sh([[:space:]]|$)'; then
    DENY_REASON="$MSG_LAND"
    return 1
  fi
  # Redirections are shell plumbing, not git args — neutralize before tokenizing.
  seg=$(printf '%s' "$seg" | sed -E 's/[0-9]*>>?[[:space:]]*[^[:space:]]+//g; s/[0-9]*<[[:space:]]*[^[:space:]]+//g; s/[0-9]*>&[0-9]+//g')
  SM_TOKS=()
  set -f
  for t in $seg; do SM_TOKS+=("$t"); done
  set +f
  n=${#SM_TOKS[@]}
  [ "$n" -gt 0 ] || return 0
  i=0
  while [ "$i" -lt "$n" ]; do
    case "${SM_TOKS[$i]}" in
      [A-Za-z_]*=*) i=$((i + 1)) ;;
      *) break ;;
    esac
  done
  { [ "$i" -lt "$n" ] && [ "${SM_TOKS[$i]}" = "git" ]; } || return 0
  i=$((i + 1))
  while [ "$i" -lt "$n" ]; do
    case "${SM_TOKS[$i]}" in
      -C | -c | --git-dir | --work-tree | --namespace | --exec-path) i=$((i + 2)) ;;
      --) i=$((i + 1)); break ;;
      -*) i=$((i + 1)) ;;
      *) sub="${SM_TOKS[$i]}"; i=$((i + 1)); break ;;
    esac
  done
  [ -n "$sub" ] || return 0
  SM_AI=$i
  case "$sub" in
    checkout | switch) sm_judge_checkout "$sub" ;;
    merge) sm_judge_merge ;;
    branch) sm_judge_branch ;;
    reset) sm_judge_reset ;;
    update-ref) sm_judge_update_ref ;;
    push) sm_judge_push ;;
    *) return 0 ;;
  esac
}

# ── Main ─────────────────────────────────────────────────────────────────────
INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")
[ -z "$COMMAND" ] && exit 0

# Quick gate: only the ref-touching git verbs and the land script are of interest.
# The flag group lists `-[cC] <val>` FIRST so `git -c x=y checkout` / `git -C p
# branch` consume their value as a unit — a bare `-[^space]+` would eat `-c` and
# orphan `x=y`, breaking the chain to the verb and silently bypassing the guard.
GATE_RE='worktree-land\.sh|(^|[;&|`(){}]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git([[:space:]]+(-[cC][[:space:]]+[^[:space:]]+|-[^[:space:]]+|--[^[:space:]]+))*[[:space:]]+(checkout|switch|merge|branch|reset|update-ref|push)\b'
printf '%s' "$COMMAND" | grep -qE "$GATE_RE" || exit 0

# ── Spoke vs hub: WT_SPOKE (role marker) or a linked-worktree git-dir ─────────
ROOT=$(project_root_from_payload "$INPUT")
GIT_DIR=$(git -C "$ROOT" rev-parse --absolute-git-dir 2>/dev/null || true)

IS_SPOKE=0
[ -n "${WT_SPOKE:-}" ] && IS_SPOKE=1
case "$GIT_DIR" in
  */.git/worktrees/*) IS_SPOKE=1 ;;
esac
[ "$IS_SPOKE" = "1" ] || exit 0

DEFAULT=$(hub_default_branch "$ROOT")
CURRENT=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)

MSG_LAND="worktree-land.sh lands a spoke into '$DEFAULT' — that is the hub's job. Emit your ready/<issue> marker (your push is the ship gate); the hub lands it. A spoke must never mutate the local '$DEFAULT' ref."
MSG_CHECKOUT="A spoke must never check out '$DEFAULT' — that ref belongs to the hub. Stay on your feature branch and reconcile with: git merge origin/$DEFAULT"
MSG_MERGE="HEAD is '$DEFAULT': this merge would mutate the local '$DEFAULT' ref. A spoke reconciles by merging origin/$DEFAULT INTO its feature branch, never into '$DEFAULT'."
MSG_BRANCH="A spoke must never force/move/delete or recreate the local '$DEFAULT' ref — that belongs to the hub."
MSG_PUSH="A spoke must never push to the local '$DEFAULT' ref. Ship your own branch instead: git push -u origin <feature>"
MSG_UPDATEREF="A spoke must never update refs/heads/$DEFAULT directly — the local '$DEFAULT' ref belongs to the hub."
MSG_RESET="HEAD is '$DEFAULT': git reset here would move the local '$DEFAULT' ref. A spoke never operates on '$DEFAULT'."

DENY_REASON=""
SM_TOKS=()
SM_AI=0

# sg_walk_segments splits on `;`, `&&`, `||`, `|` but NOT on grouping/subshell
# punctuation, so `(git checkout main)` or `true && { git checkout main; }` would
# reach the validator as one unparseable segment and slip through. Convert
# `( ) { }` and backticks to `;` first: outside quotes that creates the missing
# boundaries; inside quotes the substituted `;` stays inert (sg_walk_segments
# tracks quote state), so quoted content is unaffected. `$(` becomes `$;`, so a
# command substitution's inner command is judged as its own clause.
NORMALIZED=$(printf '%s' "$COMMAND" | tr '(){}`' ';;;;;')

# Walk every clause; the validator stops (and sets DENY_REASON) on the first
# forbidden form. Unparseable input makes the walk return non-zero with no
# DENY_REASON → we stay silent (deny-or-silent).
sg_walk_segments "$NORMALIZED" sm_check_segment || true

[ -n "$DENY_REASON" ] && deny "$DENY_REASON"
exit 0
