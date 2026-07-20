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

# Source worktree-lib.sh for the best-effort gh lifecycle-label mirror (issue #236):
# hub-notify is the single writer that flips the issue's status:* label on a
# spoke-emitted gate/ready/blocked marker transition. Two-layout resolution mirrors
# hub-ready-watch's base-branch sourcing — the ai-toolkit checkout (four levels up in
# scripts/) and a synced target (co-located in .ai-toolkit/scripts/); HUB_NOTIFY_WT_LIB
# wins for tests. A missing lib simply leaves the mirror functions undefined, and the
# mirror pass below self-gates on their presence, so the notifier still runs.
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _cand in \
  "${HUB_NOTIFY_WT_LIB:-}" \
  "$_script_dir/worktree-lib.sh" \
  "$_script_dir/../../../../scripts/worktree-lib.sh"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

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
# .afk-state (AFK_STATE overrides). Under a LIVE drain the answerer services gate
# parks and the drain auto-lands ready spokes, so only blocked/<N> (the escalation
# a human must act on) should ping; attended, every class pings.
#
# Gate on supervisor LIVENESS, not mere state presence (issue #215). A crashed
# drain leaves .afk-state armed with no process behind it; keying suppression on
# "state non-empty" would then swallow every gate/ready ping forever — a silent
# degrade whose only recovery was --off or a respawn. So mirror hub-afk.sh's
# afk_supervisor_state: armed state counts as an active drain ONLY when the
# heartbeat's pid (its first whitespace field, AFK_HEARTBEAT overrides) is a live
# process. A gone/garbled/absent heartbeat pid is a STALE window — treated as
# attended here, so the pings still fire.
afk_state_file="${AFK_STATE:-$common_dir/.afk-state}"
afk_heartbeat_file="${AFK_HEARTBEAT:-$common_dir/.afk-heartbeat}"
afk_active=0
if [ -f "$afk_state_file" ] \
  && [ -n "$(head -n1 "$afk_state_file" 2>/dev/null | tr -d '[:space:]')" ]; then
  hb_line="$(head -n1 "$afk_heartbeat_file" 2>/dev/null || true)"
  hb_pid="${hb_line%% *}"
  case "$hb_pid" in
    '' | *[!0-9]*) ;;  # no/garbled heartbeat pid -> stale drain -> attended
    *) kill -0 "$hb_pid" 2>/dev/null && afk_active=1 ;;
  esac
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
  local kind="$1" issue="$2" tag="$3" gate reason subject qcm
  case "$kind" in
    ready)
      printf '#%s done → /land %s' "$issue" "$issue" ;;
    gate)
      gate="$(tag_field "$tag" contents:subject)"
      [ -n "$gate" ] || gate="gate"
      # When the gate-broker attended adapter (#155) has written a QCM surface for this
      # gate, point the human at it; otherwise the plain approve ping. The surface path
      # mirrors gate-broker.sh's _broker_qcm_dir exactly — GATE_BROKER_QCM_DIR wins, then
      # AFK_STATE_DIR (the state-dir override _afk_state_dir honors), then the git-common
      # -dir default — so a surface written by the broker is always found here.
      qcm="${GATE_BROKER_QCM_DIR:-${AFK_STATE_DIR:-$common_dir/ai-toolkit-afk}/gate-broker}/qcm-$issue.md"
      if [ -f "$qcm" ]; then
        printf '#%s parked at %s gate — resolve via the gate-broker QCM' "$issue" "$gate"
      else
        printf '#%s parked at %s gate — reply to approve' "$issue" "$gate"
      fi ;;
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
# pushed from elsewhere. Never let its failure abort the watcher. Skipped when
# HUB_NOTIFY_SKIP_FETCH=1 — hub-ready-watch already fetched tags this poll, so
# re-fetching would double the network round-trip (and the stall on a slow SSH).
if [ "${HUB_NOTIFY_SKIP_FETCH:-0}" != "1" ]; then
  git -C "$main_root" fetch --tags --quiet origin >/dev/null 2>&1 || true
fi

# Current marker set across the three watched namespaces: "<tag> <sha>" per tag.
current="$(
  git -C "$main_root" for-each-ref --format='%(refname:short) %(objectname)' \
    'refs/tags/gate/*' 'refs/tags/ready/*' 'refs/tags/blocked/*' 2>/dev/null
)"

# Load the persisted seen set once (empty on first run → every marker is new).
seen=""
[ -f "$seen_file" ] && seen="$(cat "$seen_file" 2>/dev/null)"

# The set to persist as last-seen. It is NOT simply "$current": a marker
# suppressed under a live drain is deliberately withheld from it (issue #215), so
# the suppressed transition re-evaluates — and pings — once the drain ends or the
# supervisor dies. Recording a suppressed marker seen would LOSE its ping, not
# defer it. Already-seen markers stay seen (steady state); newly-pinged ones
# become seen so they fire exactly once.
persist=""
while IFS=' ' read -r tag sha; do
  [ -n "$tag" ] || continue
  kind="${tag%%/*}"
  issue="${tag#*/}"
  # Only numeric issues carry a marker; ignore anything malformed.
  case "$issue" in
    '' | *[!0-9]*) continue ;;
  esac
  # Already seen at this exact sha → steady state, not a new transition; keep it
  # seen. A force-moved marker has a fresh sha and falls through as newly fired.
  if printf '%s\n' "$seen" | grep -qxF "$tag $sha"; then
    persist+="$tag $sha"$'\n'
    continue
  fi
  # Under a LIVE drain, suppress everything but blocked/<N> — and do NOT record it
  # seen, so the withheld ping is delivered on a later poll once the drain is over
  # (or crashed → stale, handled above). A live drain is expected to service
  # (answer) or escalate (blocked/<N>) each park before its window ends.
  if [ "$afk_active" -eq 1 ] && [ "$kind" != "blocked" ]; then
    continue
  fi
  msg="$(message_for "$kind" "$issue" "$tag")"
  [ -n "$msg" ] && notify "$msg"
  persist+="$tag $sha"$'\n'
done <<<"$current"

# Persist the notified/steady-state set as last-seen. Markers that vanished
# (landed → tag consumed) or were suppressed this run drop out, so a future reuse
# of the issue number — or the end of a drain — re-fires correctly. An empty set
# truncates the file rather than seeding a lone-newline line. Each entry already
# carries its trailing newline.
mkdir -p "$(dirname "$seen_file")" 2>/dev/null || true
if [ -n "$persist" ]; then
  printf '%s' "$persist" >"$seen_file" 2>/dev/null || true
else
  : >"$seen_file" 2>/dev/null || true
fi

# --- GitHub status-label mirror (issue #236) ----------------------------------
# hub-notify is the single writer that mirrors a spoke-emitted gate/ready/blocked
# marker transition onto its GitHub issue's status:* label. Runs as a SEPARATE pass
# with its OWN dedup seen-set (HUB_LABEL_SEEN_FILE), so the label flips exactly once
# per new marker sha — and DECOUPLED from the ping's afk suppression above: under a
# live drain the ping is withheld, but the label must still reflect state (that is
# the whole point of the remote mirror). Best-effort throughout: skipped entirely
# when the mirror is disabled, gh is absent, or worktree-lib didn't source (the
# helper is undefined) — in which case the label-seen-set is left untouched so the
# flip is retried once the precondition returns.
if command -v wt_gh_set_status_label >/dev/null 2>&1 \
  && command -v gh >/dev/null 2>&1 \
  && wt_gh_lifecycle_enabled; then
  label_seen_file="${HUB_LABEL_SEEN_FILE:-$common_dir/hub-notify-label-seen}"
  label_seen=""
  [ -f "$label_seen_file" ] && label_seen="$(cat "$label_seen_file" 2>/dev/null)"
  label_persist=""
  while IFS=' ' read -r tag sha; do
    [ -n "$tag" ] || continue
    kind="${tag%%/*}"
    issue="${tag#*/}"
    case "$issue" in '' | *[!0-9]*) continue ;; esac
    # Already mirrored at this exact sha → steady, keep it seen (no re-flip). A
    # force-moved marker has a fresh sha and falls through as a new transition.
    if printf '%s\n' "$label_seen" | grep -qxF "$tag $sha"; then
      label_persist+="$tag $sha"$'\n'
      continue
    fi
    case "$kind" in
      gate)    wt_gh_set_status_label "$issue" "status:gate" ;;
      ready)   wt_gh_set_status_label "$issue" "status:ready" ;;
      blocked) wt_gh_set_status_label "$issue" "status:blocked" ;;
      *)       continue ;;
    esac
    label_persist+="$tag $sha"$'\n'
  done <<<"$current"
  mkdir -p "$(dirname "$label_seen_file")" 2>/dev/null || true
  if [ -n "$label_persist" ]; then
    printf '%s' "$label_persist" >"$label_seen_file" 2>/dev/null || true
  else
    : >"$label_seen_file" 2>/dev/null || true
  fi
fi

# --- warned-record pings (issue #241) -----------------------------------------
# The /afk answerer now WARNS-and-continues instead of parking a spoke as blocked at a
# converted stop site: each taken decision writes a durable warned-<issue>.txt record under
# the state dir (mirrors gate-broker.sh's _afk_state_dir — AFK_STATE_DIR wins, else the
# git-common-dir default). Surface these as OS notifications — and, UNLIKE the once-deduped
# blocked ping, RE-FIRE on an interval (HUB_NOTIFY_WARN_REPEAT, default 600s) so a standing
# warning stays loud until the operator post-adjusts it. Never afk-suppressed: the warning IS
# the review surface, and #241's auth path relies on the loud REPEAT. The per-issue last-fire
# epoch persists in its own seen-set (HUB_NOTIFY_WARN_SEEN_FILE); HUB_NOTIFY_NOW pins the
# clock for tests.
warn_state_dir="${AFK_STATE_DIR:-$common_dir/ai-toolkit-afk}"
warn_seen_file="${HUB_NOTIFY_WARN_SEEN_FILE:-$common_dir/hub-notify-warn-seen}"
warn_repeat="${HUB_NOTIFY_WARN_REPEAT:-600}"
case "$warn_repeat" in '' | *[!0-9]*) warn_repeat=600 ;; esac
warn_now="${HUB_NOTIFY_NOW:-$(date +%s)}"
case "$warn_now" in '' | *[!0-9]*) warn_now="$(date +%s)" ;; esac
if [ -d "$warn_state_dir" ]; then
  warn_seen=""
  [ -f "$warn_seen_file" ] && warn_seen="$(cat "$warn_seen_file" 2>/dev/null)"
  warn_persist=""
  for wf in "$warn_state_dir"/warned-*.txt; do
    [ -f "$wf" ] || continue
    wbase="${wf##*/}"; wissue="${wbase#warned-}"; wissue="${wissue%.txt}"
    case "$wissue" in '' | *[!0-9]*) continue ;; esac
    wreason="$(cut -f2- "$wf" 2>/dev/null | head -n1)"
    [ -n "$wreason" ] || wreason="auto-decision taken — review"
    last="$(printf '%s\n' "$warn_seen" | awk -v i="$wissue" '$1 == i { print $2 }' | head -n1)"
    case "$last" in '' | *[!0-9]*) last="" ;; esac
    if [ -z "$last" ] || [ "$(( warn_now - last ))" -ge "$warn_repeat" ]; then
      notify "#$wissue WARNING — $wreason"
      last="$warn_now"
    fi
    warn_persist+="$wissue $last"$'\n'
  done
  mkdir -p "$(dirname "$warn_seen_file")" 2>/dev/null || true
  if [ -n "$warn_persist" ]; then
    printf '%s' "$warn_persist" >"$warn_seen_file" 2>/dev/null || true
  else
    : >"$warn_seen_file" 2>/dev/null || true
  fi
fi

# --- test-budget breach pings (issue #336) ------------------------------------
# The duration-budget watcher (scripts/test-budget-watch.sh) writes one
# test-budget-breach-<slug>.txt per CURRENT breach under the AFK state dir (the same dir
# the warned-* records above live in). Fire exactly ONE OS notification per NEW breach —
# keyed on the record's basename + a content hash, so a CHANGED breach (a worse regression)
# re-fires while a steady one stays quiet — with its OWN seen-set. Mode-aware like the
# gate/ready pings: suppressed under a LIVE drain (the watcher already filed a followup —
# the durable unattended record — so the desktop ping is for the ATTENDED operator), fired
# when attended. Skipping the whole pass under a drain leaves the seen-set untouched, so a
# withheld breach still fires once the drain ends. Quiet when nothing breaches.
budget_state_dir="${AFK_STATE_DIR:-$common_dir/ai-toolkit-afk}"
budget_seen_file="${HUB_NOTIFY_BUDGET_SEEN_FILE:-$common_dir/hub-notify-budget-seen}"
if [ "$afk_active" -eq 0 ] && [ -d "$budget_state_dir" ]; then
  budget_seen=""
  [ -f "$budget_seen_file" ] && budget_seen="$(cat "$budget_seen_file" 2>/dev/null)"
  budget_persist=""
  for bf in "$budget_state_dir"/test-budget-breach-*.txt; do
    [ -f "$bf" ] || continue                       # unmatched glob → literal, guarded here
    bbase="${bf##*/}"
    bhash="$(cksum < "$bf" 2>/dev/null | awk '{print $1}')"
    bmsg="$(head -n1 "$bf" 2>/dev/null)"
    [ -n "$bmsg" ] || bmsg="test-budget breach detected"
    if printf '%s\n' "$budget_seen" | grep -qxF "$bbase $bhash"; then
      budget_persist+="$bbase $bhash"$'\n'         # already pinged at this content → steady
      continue
    fi
    notify "$bmsg"
    budget_persist+="$bbase $bhash"$'\n'
  done
  mkdir -p "$(dirname "$budget_seen_file")" 2>/dev/null || true
  if [ -n "$budget_persist" ]; then
    printf '%s' "$budget_persist" >"$budget_seen_file" 2>/dev/null || true
  else
    : >"$budget_seen_file" 2>/dev/null || true
  fi
fi

# /afk drain-complete (issue #150): hub-afk writes <git-common-dir>/.afk-drain-complete
# with the landed count when a drain finishes (see hub-afk.sh _afk_emit_drain_complete).
# Fire ONE "/afk drain complete — <k> landed" ping and consume the file, so a completed
# drain notifies exactly once and the steady post-drain state never repeats it. Removed
# BEFORE the notify so a consume happens even if the notifier fails (fire-at-most-once).
# Independent of the marker seen-set and of afk-mode: the drain already cleared .afk-state
# before this runs, and a stale-armed window must not suppress a finished drain's summary.
# AFK_DRAIN_COMPLETE overrides the path (shared with hub-afk.sh); a non-numeric/empty
# count degrades to 0 rather than leaking a partial read into the message.
drain_complete_file="${AFK_DRAIN_COMPLETE:-$common_dir/.afk-drain-complete}"
if [ -f "$drain_complete_file" ]; then
  landed="$(head -n1 "$drain_complete_file" 2>/dev/null | tr -d '[:space:]')"
  case "$landed" in '' | *[!0-9]*) landed=0 ;; esac
  rm -f "$drain_complete_file" 2>/dev/null || true
  notify "/afk drain complete — $landed landed"
fi

exit 0
