#!/usr/bin/env bash
# hub-notify.sh — the hub is the single notifier (issue #146).
#
# Spokes silence their own idle notifications (the synced config forces
# preferredNotifChannel=notifications_disabled), so per-turn spoke noise is
# gone. This watcher restores the ONE channel that matters: it fires a single OS
# notification per NEW lifecycle transition the hub already tracks —
#   gate/<N>    → "#N parked at <gate> — reply to approve"
#   ready/<N>   → "#N done → /land N"
#   blocked/<N> → "#N BLOCKED — <reason>"
# (mode-gating — silencing gate/ready under a live /afk drain — lands in the
# next subtask; this layer fires every class unconditionally.)
# keyed on the same git-native marker tags the spoke pushes (issue #16). Run it
# on the hub (main checkout), ideally on the existing hub loop next to
# hub-ready-watch.sh; quiet when there is no new transition.
#
# Dedupe mirrors hub-ready-watch: a persisted last-seen set of "<tag> <sha>"
# lines under the git common dir, so a brand-new tag OR a force-moved one (git
# tag -f after another push) reads as a fresh transition, and a steady state
# fires nothing. Unlike hub-ready-watch it does NOT gate on a live worktree at
# the branch tip — blocked/<N> is emitted exactly when a spoke is reaped / torn
# down, so requiring a resolvable worktree would drop the pings that matter
# most. The marker's appearance IS the transition.
#
# Read-only against the work: it never merges, tags, or writes a branch. The
# only state it writes is its own seen-file.
set -uo pipefail

main_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Not inside a git repository." >&2
  exit 1
}

# The last-seen set persists across runs (git common dir, shared across
# worktrees, per-repo) so only NEW transitions surface. Each line is
# "<tag> <sha>"; tracking the sha, not just the name, makes a force-moved marker
# (git tag -f) read as newly fired. Tests override the path.
common_dir="$(git -C "$main_root" rev-parse --git-common-dir 2>/dev/null || echo .git)"
case "$common_dir" in
  /*) ;;
  *)  common_dir="$main_root/$common_dir" ;;
esac
seen_file="${HUB_NOTIFY_SEEN_FILE:-$common_dir/hub-notify-seen}"

# afk-mode (issue #146): hub-afk arms a drain window by writing its end bound to
# .afk-state (AFK_STATE overrides), read here exactly as afk_read_state does —
# first line, whitespace-trimmed, non-empty ⇒ armed. Under a drain the answerer
# services gate parks and the drain auto-lands ready spokes, so only blocked/<N>
# (the escalation a human must act on) should ping; attended, every class pings.
#
# UPGRADE: this uses afk_read_state semantics (state non-empty), NOT
# afk_supervisor_state (live vs stale via the heartbeat pid). A crashed/stale
# drain — window still armed, supervisor gone — therefore keeps suppressing
# gate/ready with no answerer actually running. Bounded: the hub-afk watchdog
# normally respawns a crashed supervisor and --status surfaces STALE. Add a
# heartbeat-pid liveness check (see hub-afk.sh afk_supervisor_state) here if
# stale-drain over-suppression ever bites.
afk_state_file="${AFK_STATE:-$common_dir/.afk-state}"
afk_active=0
if [ -f "$afk_state_file" ] \
  && [ -n "$(head -n1 "$afk_state_file" 2>/dev/null | tr -d '[:space:]')" ]; then
  afk_active=1
fi

# notify <message> — fire exactly one OS notification. HUB_NOTIFY_CMD (an
# executable receiving the message as $1) overrides the default osascript for
# tests and non-macOS hosts. Never let a notifier failure abort the watcher.
notify() {
  local msg="$1"
  if [ -n "${HUB_NOTIFY_CMD:-}" ]; then
    "$HUB_NOTIFY_CMD" "$msg" || true
    return
  fi
  # Escape for the AppleScript string literal: backslashes FIRST (else the next
  # step's inserted backslashes get doubled), then double-quotes. A raw
  # backslash in a blocked reason (a Windows path, a regex) would otherwise make
  # osascript fail to compile and — because of the trailing `|| true` — silently
  # drop the very ping that matters most.
  local esc="${msg//\\/\\\\}"
  esc="${esc//\"/\\\"}"
  osascript -e "display notification \"$esc\" with title \"ai-toolkit hub\"" \
    >/dev/null 2>&1 || true
}

# tag_field <tag> <field> — read a single annotated-tag content field
# (contents:subject / contents:body), empty when absent.
tag_field() {
  git -C "$main_root" for-each-ref --format="%($2)" "refs/tags/$1" 2>/dev/null
}

# message_for <kind> <issue> <tag> — the notification text for a marker class.
# The class comes from the tag namespace (robust); subject/body only enrich.
# Assumes the ANNOTATED markers spoke-ready.sh emits (gate subject "plan";
# blocked subject "blocked" + reason in the body). On a lightweight tag,
# %(contents:*) returns the pointed-to COMMIT's message — degraded input only.
message_for() {
  local kind="$1" issue="$2" tag="$3" gate reason subject
  case "$kind" in
    ready)
      printf '#%s done → /land %s' "$issue" "$issue" ;;
    gate)
      gate="$(tag_field "$tag" contents:subject)"
      [ -n "$gate" ] || gate="gate"
      printf '#%s parked at %s gate — reply to approve' "$issue" "$gate" ;;
    blocked)
      reason="$(tag_field "$tag" contents:body)"
      if [ -z "$reason" ]; then
        subject="$(tag_field "$tag" contents:subject)"
        [ "$subject" = "blocked" ] || reason="$subject"
      fi
      [ -n "$reason" ] || reason="needs a human"
      printf '#%s BLOCKED — %s' "$issue" "$reason" ;;
  esac
}

# Best-effort fetch: a finished spoke's marker is already locally visible
# (shared ref store), so detection works offline; the fetch only catches tags
# pushed from elsewhere. Never let its failure abort the watcher.
git -C "$main_root" fetch --tags --quiet origin >/dev/null 2>&1 || true

# Current marker set across the three watched namespaces: "<tag> <sha>" per tag.
current="$(
  git -C "$main_root" for-each-ref --format='%(refname:short) %(objectname)' \
    'refs/tags/gate/*' 'refs/tags/ready/*' 'refs/tags/blocked/*' 2>/dev/null
)"

# Load the persisted seen set once (empty on first run → every marker is new).
seen=""
[ -f "$seen_file" ] && seen="$(cat "$seen_file" 2>/dev/null)"

while IFS=' ' read -r tag sha; do
  [ -n "$tag" ] || continue
  kind="${tag%%/*}"
  issue="${tag#*/}"
  # Only numeric issues carry a marker; ignore anything malformed.
  case "$issue" in
    '' | *[!0-9]*) continue ;;
  esac
  # Already seen at this exact sha → steady state, not a new transition. A
  # force-moved marker has a fresh sha and falls through as newly fired.
  if printf '%s\n' "$seen" | grep -qxF "$tag $sha"; then
    continue
  fi
  # Under a live drain, suppress everything but blocked/<N>. The marker is still
  # persisted as seen below, so it never re-pings once attended resumes — this
  # assumes a live drain services (answers) or escalates (blocked/<N>) every
  # suppressed park before its window ends (see the UPGRADE note above).
  if [ "$afk_active" -eq 1 ] && [ "$kind" != "blocked" ]; then
    continue
  fi
  msg="$(message_for "$kind" "$issue" "$tag")"
  [ -n "$msg" ] && notify "$msg"
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
