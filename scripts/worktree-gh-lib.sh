#!/usr/bin/env bash
#
# worktree-gh-lib.sh — GitHub lifecycle-label mirror for the worktree scripts.
# Source this file; do not execute it. It is sourced by scripts/worktree-lib.sh
# (the thin entry), never directly by consumers — so the helpers here can call the
# core wt_pgrep / wt_warn the entry defines (used by _wt_kill_tree) at call time.
#
# Extracted from worktree-lib.sh in issue #353 (see the gate-broker #275 / hub-afk
# #307 / hub-watchdog #308 precedent): a file every change must touch serializes the
# drain on its Scope: token (AFK Design Principle 7). This module owns the
# best-effort, time-bounded gh mirror (issue #236) that reflects the local spoke
# lifecycle onto the GitHub issue as status:*/mode:*/lane:* labels + a dispatch
# comment.

# --- GitHub lifecycle-label mirror (issue #236) -------------------------------
# Mirror the local spoke lifecycle onto its GitHub issue as labels + a dispatch
# comment, so the issue list shows what local state (worktree branches, git
# gate/ready/blocked tags, .afk-state) otherwise hides: dispatched, parked on a
# gate, ready, blocked, mode, lane. GitHub is a READ-ONLY mirror of the local
# markers — every write here is BEST-EFFORT and TIME-BOUNDED, so a failed / hung /
# absent / opted-out `gh` never fails a dispatch, a land, or a drain tick (the
# offline-safe local markers stay the source of truth).
#
# Single-writer contract (issue #236): dispatch (worktree-new.sh) stamps
# status:in-progress + mode:* + lane:spoke; the hub-ready-watch → hub-notify watch
# loop flips status:* on gate/ready/blocked marker transitions; hub-afk stamps
# status:blocked on a supervisor escalation; worktree-land clears them all at the
# issue-close step. spoke-ready.sh deliberately writes NO labels — the hub mirrors.
#
# Only issue-backed spokes mirror: express/quick/micro lanes carry no issue by
# construction, so callers gate the whole thing on a numeric issue id.

# The label taxonomy, as single sources of truth. status:* is mutually exclusive
# (a set swaps the sibling out); mode:*/lane:* ride alongside.
WT_GH_STATUS_LABELS="status:in-progress status:gate status:ready status:blocked"
WT_GH_MODE_LABELS="mode:afk mode:attended"
WT_GH_LANE_LABELS="lane:spoke"

# wt_gh_lifecycle_enabled — the mirror is ON by default; AI_TOOLKIT_GH_LIFECYCLE_LABELS=0
# is a clean full opt-out (parallel to #223's opt-IN afk:* scheduling labels, which
# convey a different thing). Any other value (incl. unset) stays on.
wt_gh_lifecycle_enabled() { [ "${AI_TOOLKIT_GH_LIFECYCLE_LABELS:-1}" != "0" ]; }

# _wt_gh_timeout_bin -> the installed coreutils timeout binary (timeout | gtimeout),
# or empty when neither is present (the default macOS hub ships neither — see the
# portable fallback in wt_gh).
_wt_gh_timeout_bin() {
  if command -v timeout >/dev/null 2>&1; then printf 'timeout\n'
  elif command -v gtimeout >/dev/null 2>&1; then printf 'gtimeout\n'
  fi
}

# _wt_kill_tree <pid> <signal> — signal a pid and all its descendants leaf-first, so a
# bounded gh's children (a forked git/curl helper) die with it rather than being orphaned
# holding the pipe open. Mirrors hub-afk's _afk_kill_tree but self-contained here so the
# lib carries no hub dependency; wt_pgrep -P matches by numeric parent pid under LC_ALL=C
# (ASCII-safe), so the non-ASCII `pgrep -f` hazard (#189) doesn't apply.
_wt_kill_tree() {
  local pid="$1" sig="$2" child
  for child in $(wt_pgrep -P "$pid" 2>/dev/null); do _wt_kill_tree "$child" "$sig"; done
  kill "-$sig" "$pid" 2>/dev/null || true
}

# _wt_gh_run <gh args...> — bounded gh returning gh's REAL exit code (0 success; nonzero
# on a gh failure OR a killed timeout; 127 when gh is absent). gh is ALWAYS time-bounded
# (AI_TOOLKIT_GH_TIMEOUT seconds, default 10): under the coreutils timeout when installed,
# else a self-contained portable fallback that backgrounds gh and kill-trees it past the
# deadline (SIGTERM, then SIGKILL after a short grace) — so a HUNG gh (a black-hole network,
# not clean-offline) can NEVER freeze a caller on a coreutils-less host (#170 guarantee).
# Every branch uses an `if`/`else` (never `cmd; return $?`) so capturing the rc can't itself
# trip a set -e caller's errexit. The seeder needs the real rc to tell a real create from a
# swallowed failure; callers that don't care use wt_gh (which discards it).
_wt_gh_run() {
  command -v gh >/dev/null 2>&1 || return 127
  local budget="${AI_TOOLKIT_GH_TIMEOUT:-10}"
  case "$budget" in '' | *[!0-9]*) budget=10 ;; esac
  local tbin; tbin="$(_wt_gh_timeout_bin)"
  if [ -n "$tbin" ]; then
    if "$tbin" "$budget" gh "$@" >/dev/null 2>&1; then return 0; else return $?; fi
  fi
  # Portable fallback: background gh + a detached killer that kill-trees it after the
  # budget. When gh finishes first the killer is cancelled immediately (no lingering
  # sleep), so the fast path stays fast.
  local grace="${AI_TOOLKIT_GH_KILL_AFTER:-2}"
  case "$grace" in '' | *[!0-9]*) grace=2 ;; esac
  local cmd_pid killer rc
  gh "$@" >/dev/null 2>&1 &
  cmd_pid=$!
  ( sleep "$budget"; _wt_kill_tree "$cmd_pid" TERM; sleep "$grace"; _wt_kill_tree "$cmd_pid" KILL ) \
    </dev/null >/dev/null 2>&1 &
  killer=$!
  if wait "$cmd_pid" 2>/dev/null; then rc=0; else rc=$?; fi
  _wt_kill_tree "$killer" TERM 2>/dev/null || true   # gh finished — cancel the pending killer
  wait "$killer" 2>/dev/null || true
  return "$rc"
}

# wt_gh <gh args...> — one BEST-EFFORT, time-bounded gh invocation. A no-op (rc 0) when
# the mirror is disabled or gh is absent; otherwise runs _wt_gh_run and DISCARDS its exit
# code. ALWAYS returns 0 — a gh failure or a killed hang must never abort a set -e caller
# mid-dispatch/land/tick. Used for the label edits and the dispatch comment, where the
# outcome doesn't gate anything.
wt_gh() {
  wt_gh_lifecycle_enabled || return 0
  _wt_gh_run "$@" || true
  return 0
}

# wt_gh_ensure_label <name> <color> <desc> — idempotently create/update a label
# (`gh label create --force` updates an existing one rather than erroring), so a
# later --add-label/--remove-label of it can never fail the whole edit for a missing
# repo label. RETURNS the real gh exit code (via _wt_gh_run) so the seeder can gate the
# persistent marker on a proven success. Call it in an `&&`/`||`/`if` context under set -e.
wt_gh_ensure_label() {
  _wt_gh_run label create "$1" --color "$2" --description "$3" --force
}

# _wt_gh_seed_dir -> the dir holding the once-per-repo seed marker. WT_GH_SEED_DIR
# overrides it (tests / a caller with no git dir); otherwise the git common dir
# (shared across a repo's worktrees, so ONE dispatch per repo seeds and every later
# dispatch/transition skips the label-create round-trips). Empty (rc 1) when neither
# resolves — the seeder then falls back to a per-process guard.
_wt_gh_seed_dir() {
  if [ -n "${WT_GH_SEED_DIR:-}" ]; then printf '%s' "$WT_GH_SEED_DIR"; return 0; fi
  local common
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -n "$common" ] || return 1
  case "$common" in /*) ;; *) common="$PWD/$common" ;; esac
  printf '%s' "$common"
}

# _wt_gh_seed_labels — ensure ALL status:*/mode:*/lane:* labels exist in the repo, so a
# later --add-label/--remove-label of any of them can never fail the whole edit for a
# missing repo label. Idempotent and cheap-once: a PERSISTENT once-per-repo marker under
# _wt_gh_seed_dir means only the FIRST *successful* dispatch/transition per repo pays the
# label-create round-trips (the review flagged the per-process guard re-seeding every
# dispatch). Falls back to a per-process shell guard when no seed dir resolves. All three
# label writers (set_status / apply_dispatch / clear) route through this, so a status-only
# transition still guarantees the mode/lane labels exist too.
#
# The marker is persisted ONLY when EVERY create succeeded (all_ok). A first seed whose gh
# calls fail — offline, unauthed, or a hung gh the timeout kills (the exact black-hole path
# #236 hardens) — must NOT stamp the marker, or it would permanently skip re-seeding and
# leave the mirror dead for the repo with no self-heal. On any failure the marker stays
# unwritten and the NEXT transition retries seeding (gh label create --force is idempotent,
# so a re-seed after a partial success is harmless). Recovery from a stale marker is simply
# `rm <git-common-dir>/.gh-lifecycle-labels-seeded`.
_WT_GH_LABELS_SEEDED=""
_wt_gh_seed_labels() {
  wt_gh_lifecycle_enabled || return 0
  command -v gh >/dev/null 2>&1 || return 0
  local dir marker all_ok=1
  dir="$(_wt_gh_seed_dir 2>/dev/null || true)"
  if [ -n "$dir" ]; then
    marker="$dir/.gh-lifecycle-labels-seeded"
    [ -f "$marker" ] && return 0
  else
    [ -n "$_WT_GH_LABELS_SEEDED" ] && return 0
  fi
  # `|| all_ok=0` keeps errexit from aborting on a failed create (the failure is on the
  # left of ||), and records that this seed pass is not fully proven.
  wt_gh_ensure_label "status:in-progress" "1d76db" "spoke dispatched, working" || all_ok=0
  wt_gh_ensure_label "status:gate"        "fbca04" "parked on a plan gate" || all_ok=0
  wt_gh_ensure_label "status:ready"       "0e8a16" "final push, awaiting land" || all_ok=0
  wt_gh_ensure_label "status:blocked"     "b60205" "escalated, needs a human" || all_ok=0
  wt_gh_ensure_label "mode:afk"      "5319e7" "unattended /afk drain spoke" || all_ok=0
  wt_gh_ensure_label "mode:attended" "c5def5" "attended (interactive) spoke" || all_ok=0
  wt_gh_ensure_label "lane:spoke"    "bfdadc" "issue-backed full-cycle spoke" || all_ok=0
  [ "$all_ok" = "1" ] || return 0   # a failed seed leaves NO marker so it self-heals
  if [ -n "$dir" ]; then
    mkdir -p "$dir" 2>/dev/null || true
    : > "$marker" 2>/dev/null || true
  else
    _WT_GH_LABELS_SEEDED=1
  fi
}

# wt_gh_set_status_label <issue> <status-label> — swap the issue's status:* label
# to <status-label> (e.g. status:gate), removing the other three. mode:*/lane:*
# are left intact (a gate/ready/blocked transition changes only the status). Used
# by the hub-notify watch loop and hub-afk's blocked escalation.
wt_gh_set_status_label() {
  local issue="$1" want="$2" args=() s
  wt_gh_lifecycle_enabled || return 0
  command -v gh >/dev/null 2>&1 || return 0
  _wt_gh_seed_labels
  args+=(--add-label "$want")
  for s in $WT_GH_STATUS_LABELS; do
    [ "$s" = "$want" ] && continue
    args+=(--remove-label "$s")
  done
  wt_gh issue edit "$issue" "${args[@]}"
}

# wt_gh_apply_dispatch_labels <issue> <mode> <lane> — stamp a freshly-dispatched
# spoke: status:in-progress + mode:<mode> + lane:<lane>, swapping out any stale
# status sibling or other-mode label a reused issue number carried. Best-effort.
wt_gh_apply_dispatch_labels() {
  local issue="$1" mode="$2" lane="$3" args=() s
  wt_gh_lifecycle_enabled || return 0
  command -v gh >/dev/null 2>&1 || return 0
  _wt_gh_seed_labels
  args+=(--add-label "status:in-progress" --add-label "mode:$mode" --add-label "lane:$lane")
  for s in $WT_GH_STATUS_LABELS; do
    [ "$s" = "status:in-progress" ] && continue
    args+=(--remove-label "$s")
  done
  for s in $WT_GH_MODE_LABELS; do
    [ "$s" = "mode:$mode" ] && continue
    args+=(--remove-label "$s")
  done
  wt_gh issue edit "$issue" "${args[@]}"
}

# wt_gh_clear_lifecycle_labels <issue> — remove every status:*/mode:*/lane:* label
# from the issue (a landed/torn-down spoke no longer has live state). The close
# comment worktree-land writes is separate and unchanged. Best-effort.
wt_gh_clear_lifecycle_labels() {
  local issue="$1" args=() s
  wt_gh_lifecycle_enabled || return 0
  command -v gh >/dev/null 2>&1 || return 0
  _wt_gh_seed_labels
  for s in $WT_GH_STATUS_LABELS $WT_GH_MODE_LABELS $WT_GH_LANE_LABELS; do
    args+=(--remove-label "$s")
  done
  wt_gh issue edit "$issue" "${args[@]}"
}

# wt_gh_dispatch_comment <issue> <body> — post the one-time dispatch comment
# linking the issue to its live spoke (branch, worktree, tmux window, spoke_run_id).
# Best-effort.
wt_gh_dispatch_comment() {
  wt_gh issue comment "$1" --body "$2"
}
