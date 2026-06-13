#!/usr/bin/env bash
# hub-ready-watch.sh — proactive ready-to-land notifier for the planning hub.
#
# Turns hub-status.sh's on-demand `pushed → mergeable` check into a poll: each
# run detects spokes that have just finished and surfaces the exact `/land N`
# command, so the hub learns a spoke is done WITHOUT the user running /hub
# (issue #25). Run it on the hub (main checkout), ideally on a loop.
#
# The completion signal already exists and is git-native: a `ready/<issue>` tag
# the spoke pushes at its branch tip after its FINAL subtask (issue #16). This
# script only adds detection + surfacing — no new signal, and NO auto-merge.
#
# Each run:
#   1. git fetch --tags          (best-effort; offline is non-fatal)
#   2. git tag -l 'ready/*'       → the current completion-marker set
#   3. diff each marker (tag + the commit it points at) against a persisted
#      last-seen set, so a brand-new tag OR a force-moved one (git tag -f after
#      an extra push) counts as newly ready
#   4. for each newly-ready marker still pointing at its branch tip, print
#      "#N → run /land N  <branch>  ↑ahead ↓behind"
#   5. persist the current set as last-seen
#
# Read-only against the work: it never merges, never writes a branch or tag.
# The only state it writes is its own seen-file. The land stays human-invoked —
# this proposes the command, the user runs it.
set -uo pipefail

main_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Not inside a git repository." >&2
  exit 1
}
default_branch="$(git -C "$main_root" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')"
default_branch="${default_branch:-main}"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }

# The last-seen set persists across runs so only NEW completions surface. It
# lives in the git common dir (shared across worktrees, per-repo) by default;
# tests override it. Each line is "<tag> <sha>" — tracking the sha, not just the
# name, makes a force-moved marker (git tag -f) read as newly ready.
common_dir="$(git -C "$main_root" rev-parse --git-common-dir 2>/dev/null || echo .git)"
case "$common_dir" in
  /*) ;;
  *)  common_dir="$main_root/$common_dir" ;;
esac
seen_file="${HUB_READY_SEEN_FILE:-$common_dir/hub-ready-seen}"

# branch_for_issue <issue> → "<branch>\t<path>" for the worktree whose slug
# leads with this issue number (mirrors hub-status.sh's slug→issue parse), or
# non-zero if none. The hub holds the spoke's worktree until landing, so this
# resolves in the normal case; absence is the degraded (already-torn-down) case.
branch_for_issue() {
  local issue="$1" path branch slug num
  while IFS= read -r line; do
    path="$(awk '{print $1}' <<<"$line")"
    branch="$(sed -n 's/.*\[\(.*\)\].*/\1/p' <<<"$line")"
    [ -n "$branch" ] || continue
    slug="${branch##*/}"
    num="$(printf '%s' "$slug" | sed 's/^\([0-9]*\).*/\1/')"
    if [ "$num" = "$issue" ]; then
      printf '%s\t%s\n' "$branch" "$path"
      return 0
    fi
  done < <(git -C "$main_root" worktree list 2>/dev/null)
  return 1
}

# Best-effort fetch: a finished spoke's tag is already locally visible (shared
# ref store), so detection works offline; the fetch only catches tags pushed
# from elsewhere. Never let its failure abort the watcher.
git -C "$main_root" fetch --tags --quiet origin >/dev/null 2>&1 || true

# Current completion-marker set: "<tag> <sha>" per ready/<issue> tag.
current="$(
  git -C "$main_root" for-each-ref --format='%(refname:short) %(objectname)' 'refs/tags/ready/*' 2>/dev/null
)"

# Load the persisted seen set once (empty on first run → every marker is new).
seen=""
[ -f "$seen_file" ] && seen="$(cat "$seen_file" 2>/dev/null)"

emitted_header=""
while IFS=' ' read -r tag sha; do
  [ -n "$tag" ] || continue
  issue="${tag#ready/}"
  # Only numeric issues carry a ready marker; ignore anything malformed.
  case "$issue" in
    ''|*[!0-9]*) continue ;;
  esac
  # Already seen at this exact sha → not new. A force-moved marker has a fresh
  # sha and falls through as newly ready.
  if printf '%s\n' "$seen" | grep -qxF "$tag $sha"; then
    continue
  fi

  # Resolve the branch and gate on the marker still pointing at its tip — the
  # same rule hub-status.sh uses for `pushed → mergeable`. A stale marker
  # (extra push after tagging) is not a completion claim and must not fire.
  if ! read -r branch wt_path < <(branch_for_issue "$issue"); then
    continue   # worktree gone — can't verify the tip; stay silent (degraded)
  fi
  tip="$(git -C "$wt_path" rev-parse HEAD 2>/dev/null)"
  # Peel the tag to its commit (hub-status.sh's `^{commit}` idiom) so the tip
  # check holds for annotated tags too, not just lightweight ones — %(objectname)
  # above is the tag-object sha, which only equals the commit for lightweight tags.
  marker_commit="$(git -C "$main_root" rev-parse -q --verify "${tag}^{commit}" 2>/dev/null)"
  [ -n "$tip" ] && [ -n "$marker_commit" ] && [ "$marker_commit" = "$tip" ] || continue

  counts="$(git -C "$wt_path" rev-list --left-right --count "$default_branch...HEAD" 2>/dev/null)"
  behind="$(awk '{print $1}' <<<"$counts")"
  ahead="$(awk '{print $2}' <<<"$counts")"

  if [ -z "$emitted_header" ]; then
    bold "Ready to land"
    emitted_header=1
  fi
  printf '  #%s → run /land %s   %s   ↑%s ↓%s   (suite runs on land)\n' \
    "$issue" "$issue" "$branch" "${ahead:-?}" "${behind:-?}"
done <<<"$current"

# Persist the current set as last-seen. Markers that vanished (landed → tag
# consumed) drop out, so a future reuse of the issue number re-fires correctly.
# An empty set truncates the file rather than seeding a lone-newline line.
mkdir -p "$(dirname "$seen_file")" 2>/dev/null || true
if [ -n "$current" ]; then
  printf '%s\n' "$current" >"$seen_file" 2>/dev/null || true
else
  : >"$seen_file" 2>/dev/null || true
fi

exit 0
