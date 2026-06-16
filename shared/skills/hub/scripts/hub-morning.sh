#!/usr/bin/env bash
# hub-morning.sh — the night-mode morning report (issue #40, Phase 4).
#
# Assembles the three terminal markers + a pre-computed land-triage into a worklist
# sorted FASTEST -> SLOWEST human effort, so waking up to a drained night queue is a
# few rubber-stamps and a couple of decisions, not an archaeology dig:
#   LAND      ready/N, merges clean, agent-approved      -> rubber-stamp /land N
#   EYEBALL   accept/N, built + pushed + agent-reviewed   -> glance, then land/send back
#   THINK     blocked/N (the parked blocker is the body)  -> answer + re-queue
#   CONFLICTS ready/N whose throwaway merge hit a conflict -> hand-resolution
# A gate/N (still parked at the PLAN gate) is a footer, not a worklist tier.
#
# Each row carries the diff size, the trust summary (the marker's annotated-tag
# body — e.g. "code-review rejected 2x"), the per-spoke cost (reused from the #35
# pull layer via telemetry/morning.py, best-effort), and the exact next command.
#
# LAND-TRIAGE (--triage) merges each ready branch onto the default in a HERMETIC
# detached temp worktree and probes ONLY for a merge conflict — NO pytest (the real
# test gate fires at /land's push; running pytest here would re-enter the
# GIT_DIR-leak/tripwire hazard). The supervisor calls it at end-of-night so the
# 07:00 report is instant; the report degrades to "merges unknown" if it is absent.
#
# Read-only against the work (the triage temp worktree is created + removed under
# mktemp, never the real default branch). Run on the hub (main checkout).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source hub-night.sh for inflight_worktrees / _night_state_dir. hub-night's
# source-guard means this only defines functions, never runs its supervisor.
_hub_night="${HUB_NIGHT:-$SCRIPT_DIR/hub-night.sh}"
if [ -r "$_hub_night" ]; then
  # shellcheck source=hub-night.sh
  . "$_hub_night"
else
  echo "hub-morning: cannot source hub-night.sh (set HUB_NIGHT)" >&2
  exit 1
fi

MAIN_ROOT="${MAIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}"

# _repo_is_real — the #55 fixture-leak guard, mirroring dashboard/queries.py's
# real-spoke filter on the marker side: the morning worklist renders only for the
# real toolkit checkout. A stray sandbox/test hub (basename not the toolkit name)
# must not surface its markers as a fake worklist. The prefix is overridable via
# HUB_REAL_REPO_PREFIX (default 'ai-toolkit'); an empty prefix disables the guard
# (the unit harness uses this to run report() over its temp repo).
_repo_is_real() {
  local prefix="${HUB_REAL_REPO_PREFIX-ai-toolkit}"
  [ -z "$prefix" ] && return 0
  case "$(basename "${MAIN_ROOT:-}")" in
    "$prefix"*) return 0 ;;
    *)          return 1 ;;
  esac
}

# default_branch -> origin/HEAD, else main/master, else main.
default_branch() {
  local def=""
  def="$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')" \
    || def=""
  if [ -n "$def" ]; then printf '%s' "$def"; return; fi
  for def in main master; do
    git show-ref --verify --quiet "refs/heads/$def" 2>/dev/null && { printf '%s' "$def"; return; }
  done
  printf 'main'
}

# tier_for_marker <kind> [mergeable] -> the effort tier, or empty for a non-tier
# (gate/N, still parked at PLAN). A ready marker is LAND unless triage found a
# conflict (then CONFLICTS); an unknown mergeability is optimistically LAND (the
# real merge check is /land's push).
tier_for_marker() {
  case "$1" in
    ready)   [ "${2:-}" = "conflict" ] && printf 'CONFLICTS\n' || printf 'LAND\n' ;;
    accept)  printf 'EYEBALL\n' ;;
    blocked) printf 'THINK\n' ;;
    *)       printf '\n' ;;
  esac
}

# next_command <tier> <issue> -> the exact next action for that row.
next_command() {
  case "$1" in
    LAND)      printf 'run /land %s\n' "$2" ;;
    EYEBALL)   printf 'glance, then /land %s or send it back\n' "$2" ;;
    THINK)     printf 'read the blocker, answer + re-queue #%s\n' "$2" ;;
    CONFLICTS) printf 'hand-resolve: rebase #%s on the default, then /land %s\n' "$2" "$2" ;;
    *)         printf '\n' ;;
  esac
}

# probe_merge <repo> <ref> <default> -> clean|conflict|unknown. Merges <ref> onto
# <default> in a HERMETIC detached temp worktree (mktemp), probes for a conflict,
# aborts + removes the worktree. GIT_* are stripped for the merge so a leaked hook
# env can't retarget the real repo. NO pytest — conflict probing only.
probe_merge() {
  local repo="$1" ref="$2" default="$3" base wt verdict=unknown
  base="$(mktemp -d)"
  wt="$base/probe"
  if git -C "$repo" worktree add -q --detach "$wt" "$default" 2>/dev/null; then
    if env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE \
         git -C "$wt" merge --no-commit --no-ff "$ref" >/dev/null 2>&1; then
      verdict=clean
    else
      verdict=conflict
    fi
    env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE \
      git -C "$wt" merge --abort >/dev/null 2>&1 || true
    git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || true
  fi
  rm -rf "$base" 2>/dev/null || true
  printf '%s\n' "$verdict"
}

# _wt_for_issue <issue> -> the in-flight worktree path for the issue, or empty.
_wt_for_issue() {
  inflight_worktrees | awk -F'\t' -v i="$1" '$2 == i {print $1; exit}'
}

_triage_cache() { printf '%s/land-triage\n' "$(_night_state_dir)"; }

# all terminal + gate markers as "<kind>/<issue>" lines.
_markers() {
  git -C "$MAIN_ROOT" for-each-ref --format='%(refname:short)' \
    'refs/tags/ready/*' 'refs/tags/accept/*' 'refs/tags/blocked/*' 'refs/tags/gate/*' 2>/dev/null
}

# _at_tip <issue> <ref> -> true when the marker points at the spoke's branch tip
# (the hub-status.sh mergeable rule); a stale marker (extra push after tagging) is
# not a completion claim. With no live worktree it is trusted (already torn down).
_at_tip() {
  local issue="$1" ref="$2" wt tip mc
  wt="$(_wt_for_issue "$issue")"
  [ -n "$wt" ] || return 0
  tip="$(git -C "$wt" rev-parse HEAD 2>/dev/null)"
  mc="$(git -C "$MAIN_ROOT" rev-parse -q --verify "${ref}^{commit}" 2>/dev/null)"
  [ -n "$tip" ] && [ "$mc" = "$tip" ]
}

# land_triage_all — pre-compute the merge-conflict verdict for every ready marker
# and cache it ("<issue> <clean|conflict>" per line). Called by the supervisor at
# end-of-night so the morning report is instant.
land_triage_all() {
  local def cache ref issue mc verdict
  def="$(default_branch)"
  cache="$(_triage_cache)"
  mkdir -p "$(dirname "$cache")" 2>/dev/null || true
  git -C "$MAIN_ROOT" worktree prune 2>/dev/null || true   # idempotent restart
  : > "$cache"
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    issue="${ref##*/}"
    _at_tip "$issue" "$ref" || continue
    mc="$(git -C "$MAIN_ROOT" rev-parse -q --verify "${ref}^{commit}" 2>/dev/null)"
    [ -n "$mc" ] || continue
    verdict="$(probe_merge "$MAIN_ROOT" "$mc" "$def")"
    printf '%s %s\n' "$issue" "$verdict" >> "$cache"
  done < <(git -C "$MAIN_ROOT" for-each-ref --format='%(refname:short)' 'refs/tags/ready/*')
  return 0
}

# _cost_for <issue> -> the spoke's cost via telemetry/morning.py, or empty
# (best-effort: a missing telemetry/ccusage must never break the report).
_cost_for() {
  local py="${MORNING_PY:-$MAIN_ROOT/scripts/telemetry/morning.py}"
  [ -r "$py" ] || return 0
  PYTHONPATH="${PYTHONPATH:-}:$MAIN_ROOT/scripts" python3 "$py" --cost-for "$1" 2>/dev/null || true
}

report() {
  if ! _repo_is_real; then
    echo "hub-morning: '$(basename "${MAIN_ROOT:-?}")' is not a real toolkit checkout" \
         "(HUB_REAL_REPO_PREFIX=${HUB_REAL_REPO_PREFIX-ai-toolkit}) — skipping worklist"
    return 0
  fi
  local def cache ref kind issue merge tier diff trust cost line wt
  local land="" eyeball="" think="" conflicts="" footer=""
  def="$(default_branch)"
  cache="$(_triage_cache)"
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    kind="${ref%%/*}"
    issue="${ref##*/}"
    _at_tip "$issue" "$ref" || continue   # stale marker — not a completion claim
    merge="$(awk -v i="$issue" '$1 == i {print $2}' "$cache" 2>/dev/null || true)"
    tier="$(tier_for_marker "$kind" "$merge")"
    if [ -z "$tier" ]; then
      footer="${footer}  #${issue} — still parked at the PLAN gate"$'\n'
      continue
    fi
    diff=""
    wt="$(_wt_for_issue "$issue")"
    [ -n "$wt" ] && diff="$(git -C "$wt" diff "${def}...HEAD" --shortstat 2>/dev/null | sed 's/^[[:space:]]*//')"
    trust="$(git -C "$MAIN_ROOT" tag -l --format='%(contents:body)' "$ref" 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
    cost="$(_cost_for "$issue")"
    line="  #${issue}  ${diff:-no diff}"
    [ -n "$cost" ] && line="${line}  \$${cost}"
    [ -n "$trust" ] && line="${line}  trust: ${trust}"
    line="${line}  → $(next_command "$tier" "$issue")"
    case "$tier" in
      LAND)      land="${land}${line}"$'\n' ;;
      EYEBALL)   eyeball="${eyeball}${line}"$'\n' ;;
      THINK)     think="${think}${line}"$'\n' ;;
      CONFLICTS) conflicts="${conflicts}${line}"$'\n' ;;
    esac
  done < <(_markers)

  echo "Night morning report — worklist (fastest → slowest human effort)"
  [ -f "$cache" ] || echo "  (land-triage not pre-computed — run: hub-morning.sh --triage)"
  echo
  printf 'LAND — rubber-stamp /land:\n%s\n' "${land:-  (none)}"
  printf 'EYEBALL — glance then land/send back:\n%s\n' "${eyeball:-  (none)}"
  printf 'THINK — answer the blocker + re-queue:\n%s\n' "${think:-  (none)}"
  printf 'CONFLICTS — land needs hand-resolution:\n%s\n' "${conflicts:-  (none)}"
  [ -n "$footer" ] && printf 'Still parked at PLAN (night reviewer not yet done):\n%s\n' "$footer"
  return 0
}

# _echoed_file -> the seen-file recording which markers were mirrored to a gh
# issue comment (one line per "<kind>/<issue>"), so a re-run never double-comments.
_echoed_file() { printf '%s/echoed-comments\n' "$(_night_state_dir)"; }

# echo_marker_comments — AC#3's "+ issue comments": mirror each TERMINAL marker
# (ready/accept/blocked) at its branch tip to a gh issue comment whose body is the
# marker's tag reason. The spoke stays gh-READ-ONLY (it writes only the annotated
# tag); the hub, which has gh-write, echoes the human-facing comment here. The
# non-terminal gate/<N> PLAN park is deliberately NOT echoed. Idempotent (the
# seen-file) and best-effort (no gh, or a gh failure, logs and continues — a
# missing comment must never break the night).
echo_marker_comments() {
  command -v gh >/dev/null 2>&1 || { echo "hub-morning: gh not found — skipping comment echo" >&2; return 0; }
  local seen ref kind issue body
  seen="$(_echoed_file)"
  mkdir -p "$(dirname "$seen")" 2>/dev/null || true
  touch "$seen" 2>/dev/null || true
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    kind="${ref%%/*}"
    issue="${ref##*/}"
    _at_tip "$issue" "$ref" || continue
    grep -qxF "$ref" "$seen" 2>/dev/null && continue   # already echoed once
    body="$(git -C "$MAIN_ROOT" tag -l --format='%(contents:body)' "$ref" 2>/dev/null \
      | sed '/^[[:space:]]*$/d' | head -5)"
    [ -n "$body" ] || body="$kind"
    if gh issue comment "$issue" --body "night-mode ${kind}/${issue}: ${body}" >/dev/null 2>&1; then
      printf '%s\n' "$ref" >> "$seen"
    else
      echo "hub-morning: gh issue comment $issue failed (non-fatal)" >&2
    fi
  done < <(git -C "$MAIN_ROOT" for-each-ref --format='%(refname:short)' \
             'refs/tags/ready/*' 'refs/tags/accept/*' 'refs/tags/blocked/*')
  return 0
}

main() {
  case "${1:-}" in
    --triage) land_triage_all ;;
    --comments) echo_marker_comments ;;
    ""|--report) report ;;
    -h|--help) echo "usage: hub-morning.sh [--report|--triage|--comments]" >&2; return 0 ;;
    *) echo "hub-morning: unknown argument: $1" >&2; return 2 ;;
  esac
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
