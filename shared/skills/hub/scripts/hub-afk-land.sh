#!/usr/bin/env bash
# hub-afk-land.sh -- split out of hub-afk.sh (issue #307).
#
# The LAND lane of the /afk supervisor: auto_land + the land-retry and #285 conflict-
# resolution lanes, the review-gate consult, the auto-answer pass, and the ready/blocked
# tip probes (plus the #285 conflict-resolve prompt, whose resolution lane lives here). A
# pure function-definition module sourced by the entry lib hub-afk.sh AFTER worktree-lib /
# gate-broker / log / afk_now and the entry's own state/time primitives, and BEFORE any
# function is called, so every cross-module helper resolves at call time. Not run on its own.
set -uo pipefail

# _afk_conflict_resolve_prompt <issue> -> the #285 resolution message for a spoke whose land
# hit a DETERMINISTIC merge conflict (a sibling landed edits to a file this spoke also owns).
# The hub cannot resolve it — the spoke must merge the base branch on its side and re-push, so
# the hub re-lands on the fresh tip. Names the marker-emitter path that EXISTS in the spoke's
# worktree (the #271 probe) so the re-emit step doesn't hand it a path the deny-wall approves
# textually but that fails to exec.
_afk_conflict_resolve_prompt() {
  local issue="$1" marker_dir base
  marker_dir="$(wt_marker_script_dir "${_AFK_TOPLEVEL:-.}")"
  base="$(_afk_default_ref "${_AFK_TOPLEVEL:-.}")"; base="${base:-origin/main}"
  cat <<EOF
The hub could NOT land your branch: it CONFLICTS with $base because a sibling task landed
changes to a file you also edited. Your committed work is intact -- resolve the conflict ON
THE SPOKE so the hub can re-land on your fresh tip:
  1. git fetch origin
  2. merge the base branch into yours (git merge $base) and RESOLVE the conflicts
  3. re-run your tests to confirm green
  4. push your branch
  5. re-emit the ready marker: bash ${marker_dir}/spoke-push.sh --ready $issue
Do NOT self-land -- the hub lands #$issue once your tip is mergeable again.
EOF
}
# --- auto-land + reap passes --------------------------------------------------

# The heartbeat must reflect PROGRESS, not merely child existence (#202 B): a land/answer
# that HANGS keeps its child alive, so stamping "while the child runs" kept the epoch fresh
# forever and defeated the stale-tick watchdog. So a single phase's stamping is BOUNDED to
# AFK_PHASE_MAX_SECONDS (a generous multiple of any legit phase); once a phase runs past it,
# stamping stops so the epoch ages, --status reads STALLED, and the watchdog respawns the
# wedged tree. A phase that COMPLETES always gets a final stamp (completion IS progress), so
# a merely slow-but-finishing land never triggers a false respawn. 0 disables the cap.
: "${AFK_PHASE_MAX_SECONDS:=900}"
_afk_phase_max_seconds() {
  local s="${AFK_PHASE_MAX_SECONDS:-900}"
  case "$s" in '' | *[!0-9]*) s=900 ;; esac
  printf '%s\n' "$s"
}

# _afk_heartbeat_stamper <ppid> -> the backgrounded stamp loop shared by the fg runner: every
# AFK_LAND_HEARTBEAT_SECONDS stamp the SUPERVISOR's pid (<ppid>, passed explicitly so a
# reparented orphan can't stamp the wrong pid), until the supervisor dies (orphan guard —
# `kill -0 <ppid>` fails once the parent is gone, so a stamper that outlived a killed
# supervisor stops instead of racing the respawn with a dead pid) or the phase runs past the
# AFK_PHASE_MAX_SECONDS cap (the hang surfaces). Returns when either bound is hit.
_afk_heartbeat_stamper() {
  local ppid="$1" interval maxs elapsed=0
  interval="${AFK_LAND_HEARTBEAT_SECONDS:-30}"; case "$interval" in '' | *[!0-9]* | 0) interval=30 ;; esac
  maxs="$(_afk_phase_max_seconds)"
  while :; do
    kill -0 "$ppid" 2>/dev/null || return 0                     # supervisor gone — stop (orphan guard)
    { [ "$maxs" -ne 0 ] && [ "$elapsed" -ge "$maxs" ]; } && return 0   # phase hung — stop stamping
    afk_write_heartbeat_pid "$ppid"
    sleep "$interval" 2>/dev/null || true
    elapsed=$(( elapsed + interval ))
  done
}

# _afk_run_with_heartbeat <cmd...> -> run <cmd...> (backgrounded) while stamping the heartbeat
# every AFK_LAND_HEARTBEAT_SECONDS, so the epoch stays honest through the longest tick phase
# (#133 item 4) — but BOUNDED to AFK_PHASE_MAX_SECONDS so a hung child surfaces (#202 B). The
# stamp loop runs in THIS shell, so a killed supervisor stops stamping outright (no orphan).
# Returns the command's exit code (a failed land must still escalate).
_afk_run_with_heartbeat() {
  local child rc slept elapsed=0 interval="${AFK_LAND_HEARTBEAT_SECONDS:-30}" maxs
  case "$interval" in '' | *[!0-9]* | 0) interval=30 ;; esac
  maxs="$(_afk_phase_max_seconds)"
  "$@" &
  child=$!
  while kill -0 "$child" 2>/dev/null; do
    # Stamp PROGRESS, not child-existence: stop refreshing once the phase runs past the cap
    # so a hung land ages the epoch and the watchdog respawns the tree (#202 B).
    { [ "$maxs" -eq 0 ] || [ "$elapsed" -lt "$maxs" ]; } && afk_write_heartbeat
    # Re-check the child every second within the stamp interval — a full-interval
    # sleep would hold the tick up to AFK_LAND_HEARTBEAT_SECONDS after a fast land.
    slept=0
    while [ "$slept" -lt "$interval" ] && kill -0 "$child" 2>/dev/null; do
      sleep 1 2>/dev/null || true
      slept=$(( slept + 1 ))
    done
    elapsed=$(( elapsed + slept ))
  done
  wait "$child"; rc=$?
  afk_write_heartbeat   # the child COMPLETED — progress — so always stamp (a slow-but-done land is not wedged)
  return "$rc"
}

# _afk_run_with_heartbeat_fg <cmd...> -> the same guarantee as _afk_run_with_heartbeat, but
# for a command that must run in the CURRENT shell because it sets a variable the caller reads
# (answer_pass's decide_and_act raises the process-global _AFK_AUTH_FAILED; backgrounding it
# would lose the assignment in a subshell). So the STAMPER is backgrounded instead
# (_afk_heartbeat_stamper), carrying the supervisor's pid + the orphan/phase-cap guards.
# Returns the command's exit code.
_afk_run_with_heartbeat_fg() {
  local stamper rc
  _afk_heartbeat_stamper "$$" &
  stamper=$!
  "$@"; rc=$?
  kill "$stamper" 2>/dev/null || true
  wait "$stamper" 2>/dev/null || true
  afk_write_heartbeat   # the command returned — progress — stamp the supervisor's pid
  return "$rc"
}

# _ready_at_tip <wt_path> <issue> -> true when ready/<issue> points at the branch tip.
# Only a ready/ marker is auto-landed: accept/ awaits a human sign-off and blocked/ is
# already a parked terminal state.
_ready_at_tip() {
  local wt="$1" issue="$2" tip marker
  tip="$(git -C "$wt" rev-parse HEAD 2>/dev/null)" || return 1
  marker="$(git -C "$wt" rev-parse -q --verify "refs/tags/ready/${issue}^{commit}" 2>/dev/null)"
  [ -n "$marker" ] && [ "$marker" = "$tip" ]
}

# _blocked_at_tip <wt_path> <issue> -> true when blocked/<issue> points at the branch tip.
# A deterministic land failure (a genuine merge conflict) escalates blocked/<issue> at the
# tip, right where ready/<issue> still sits — so auto_land skips a blocked-at-tip issue to
# escalate ONCE instead of re-attempting the same failure every tick (the merge→fail→reset→
# merge loop, #144). reconcile_markers clears the tag once the spoke commits fresh work on
# top (it falls behind the tip), so the issue becomes landable again after a real fix.
_blocked_at_tip() {
  local wt="$1" issue="$2" tip marker
  tip="$(git -C "$wt" rev-parse HEAD 2>/dev/null)" || return 1
  marker="$(git -C "$wt" rev-parse -q --verify "refs/tags/blocked/${issue}^{commit}" 2>/dev/null)"
  [ -n "$marker" ] && [ "$marker" = "$tip" ]
}

# _afk_review_verdict <wt> -> the verdict of the spoke's most-recent code-review artifact
# (APPROVE | REQUEST_CHANGES), or empty when no `.review/*.json` exists. Review evidence is
# written per reviewed diff as `.review/<hash>.json` by the review-stamp MCP; the LATEST by
# ISO-8601 `timestamp` wins, so a spoke that earned a REQUEST_CHANGES and then fixed it (a
# newer APPROVE) reads clean. Pure bash + grep (no jq dependency); ISO-8601 Z timestamps
# sort chronologically as plain strings.
# Same-second tie-break (#152): review-stamp's timestamp has 1-second resolution and a review
# can finish in <1s, so an APPROVE and a REQUEST_CHANGES can share the latest second. Such a
# tie resolves CONSERVATIVELY to REQUEST_CHANGES — the gate never lands on an ambiguous
# second — and the outcome does not depend on `.review/*.json` glob order.
# UPGRADE: this trusts an artifact's verdict field WITHOUT checking its HMAC signature (the
#   advisory reviewer-sep push gate is the authenticity layer today) and picks by timestamp,
#   not by binding to the pushed diff. Binding to the tip diff hash (utils.sh review_diff_hash
#   <wt> <base> range) AND verifying the signature would close both the "APPROVE then gut
#   before ready" ordering and the forge-an-APPROVE axis — once hub-afk can share utils.sh's
#   hash + verify recipe without its source-time side effects (set -e + per-hook span arm).
_afk_review_verdict() {
  local wt="$1"
  local dir="$wt/.review" f ts v latest="" verdict=""
  [ -d "$dir" ] || { printf '%s' ""; return 0; }
  for f in "$dir"/*.json; do
    [ -f "$f" ] || continue
    v="$(grep -oE '"verdict"[[:space:]]*:[[:space:]]*"[^"]*"' "$f" | head -1 | sed 's/.*: *"//;s/"$//')"
    [ -n "$v" ] || continue
    ts="$(grep -oE '"timestamp"[[:space:]]*:[[:space:]]*"[^"]*"' "$f" | head -1 | sed 's/.*: *"//;s/"$//')"
    ts="${ts:-0000}"   # a timestamp-less artifact sorts lowest — never wins over a stamped one
    if [ -z "$latest" ] || [[ "$ts" > "$latest" ]]; then
      latest="$ts"; verdict="$v"
    elif [ "$ts" = "$latest" ] && [ "$v" = "REQUEST_CHANGES" ]; then
      verdict="$v"   # conservative tie-break: a same-second REQUEST_CHANGES blocks the land
    fi
  done
  printf '%s' "$verdict"
}

# --- stranded ready+blocked land-retry budget (issue #202 D) ------------------
# A finished tip carrying BOTH ready/<issue> and blocked/<issue> hit a TRANSIENT land
# failure (a diverged-merge blip, a momentary push rejection). The tip is final, so the
# spoke never commits fresh work for reconcile_markers to clear the stale block — and the
# old "skip a blocked-at-tip issue" logic skip-landed it EVERY tick forever (recovered by
# hand with a manual `blocked/<N>` delete). auto_land now RETRIES the land up to
# AFK_LAND_RETRY_MAX times (per issue, this window); once the budget is spent it escalates
# VISIBLY (a durable local record --status surfaces) instead of spinning silently.
: "${AFK_LAND_RETRY_MAX:=1}"
_afk_land_retry_file() { printf '%s\n' "$(_afk_state_dir)/land-retry-$1.count"; }
_afk_read_land_retries() {
  local f n; f="$(_afk_land_retry_file "$1")"
  n="$( [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' )"
  case "$n" in '' | *[!0-9]*) n=0 ;; esac
  printf '%s\n' "$n"
}
_afk_incr_land_retries() {
  local issue="$1" n dir
  dir="$(_afk_state_dir)"; mkdir -p "$dir" 2>/dev/null || true
  n=$(( $(_afk_read_land_retries "$issue") + 1 ))
  _afk_atomic_write "$(_afk_land_retry_file "$issue")" "$n" || true
}
_afk_clear_land_retries() { rm -f "$(_afk_land_retry_file "$1")" 2>/dev/null || true; }
_afk_clear_land_retry_counts() { rm -f "$(_afk_state_dir)"/land-retry-*.count 2>/dev/null || true; }
_afk_land_retry_max() {
  local max="${AFK_LAND_RETRY_MAX:-1}"
  case "$max" in '' | *[!0-9]*) max=1 ;; esac
  printf '%s\n' "$max"
}

# --- #285: the conflicted-land resolution lane --------------------------------
# A DETERMINISTIC merge conflict (worktree-land exit WT_LAND_CONFLICT_EXIT=4) is a pure
# function of the two tips: re-running the identical land is futile until one tip moves. So
# auto_land records a per-issue, per-window fingerprint "<branch_tip> <main_tip>" and, while it
# is UNCHANGED, does NOT re-run the expensive land — it routes to a resolution lane instead
# (relaunch a reaped spoke reusing its spoke_run_id, or inject a live one, with a merge the base
# branch -> resolve -> re-push -> re-emit ready instruction). When the spoke resolves and the tip
# moves, the fingerprint no longer matches and auto_land re-lands on the fresh tip.
: "${WT_LAND_CONFLICT_EXIT:=4}"
case "$WT_LAND_CONFLICT_EXIT" in '' | *[!0-9]*) WT_LAND_CONFLICT_EXIT=4 ;; esac

_afk_land_conflict_fp_file() { printf '%s\n' "$(_afk_state_dir)/land-conflict-$1"; }
_afk_read_land_conflict_fp() {
  local f; f="$(_afk_land_conflict_fp_file "$1")"
  [ -f "$f" ] && head -n1 "$f" 2>/dev/null || true
}
_afk_write_land_conflict_fp() {
  local issue="$1" fp="$2"
  mkdir -p "$(_afk_state_dir)" 2>/dev/null || true
  _afk_atomic_write "$(_afk_land_conflict_fp_file "$issue")" "$fp" || true
}
_afk_clear_land_conflict_fp() { rm -f "$(_afk_land_conflict_fp_file "$1")" 2>/dev/null || true; }
_afk_clear_land_conflict_fps() { rm -f "$(_afk_state_dir)"/land-conflict-* 2>/dev/null || true; }

# _afk_land_conflict_fingerprint <wt> -> "<branch_tip> <main_tip>": the pair a conflict is a pure
# function of. main_tip is the local default branch the land merges INTO (_afk_local_default_sha),
# so a sibling land advancing main is detected as a moved fingerprint too.
_afk_land_conflict_fingerprint() {
  local wt="$1" bt mt
  bt="$(git -C "$wt" rev-parse HEAD 2>/dev/null || true)"
  mt="$(_afk_local_default_sha)"
  printf '%s %s\n' "$bt" "$mt"
}
# _afk_land_conflict_unchanged <wt> <issue> -> true when a recorded conflict fingerprint exists
# AND the current tips still match it (an identical re-land would deterministically re-conflict).
_afk_land_conflict_unchanged() {
  local wt="$1" issue="$2" prev
  prev="$(_afk_read_land_conflict_fp "$issue")"
  [ -n "$prev" ] || return 1
  [ "$prev" = "$(_afk_land_conflict_fingerprint "$wt")" ]
}

# The resolution-lane budget is DISTINCT from the crash-resume budget (_afk_resumed_marker): a
# conflict revive must neither consume nor be starved by the once-per-window crash-resume stamp.
# It records the SPOKE branch tip at dispatch, so a re-land triggered by a sibling advancing main
# (which moves the land fingerprint but NOT the spoke's own tip) does NOT re-inject the resolve
# prompt into a spoke already resolving — only a spoke that moved its OWN tip (genuine progress)
# earns a fresh dispatch (#285 review). Per-window (cleared on a fresh arm).
_afk_conflict_resolved_marker() { printf '%s\n' "$(_afk_state_dir)/conflict-resolved-$1"; }
_afk_already_conflict_resolved() { [ -f "$(_afk_conflict_resolved_marker "$1")" ]; }
_afk_read_conflict_resolved_tip() {
  local m; m="$(_afk_conflict_resolved_marker "$1")"
  [ -f "$m" ] && head -n1 "$m" 2>/dev/null || true
}
_afk_mark_conflict_resolved() {
  local issue="$1" tip="${2:-}" m
  m="$(_afk_conflict_resolved_marker "$issue")"
  mkdir -p "$(dirname "$m")" 2>/dev/null || true
  printf '%s\n' "$tip" > "$m" 2>/dev/null || true
}
_afk_clear_conflict_resolved() { rm -f "$(_afk_conflict_resolved_marker "$1")" 2>/dev/null || true; }
_clear_conflict_resolve_markers() { rm -f "$(_afk_state_dir)"/conflict-resolved-* 2>/dev/null || true; }

# _afk_conflict_resolve_relaunch <wt> <issue> -> DEAD/reaped pane: relaunch the spoke reusing its
# spoke_run_id (via _afk_continue_command) with the resolve prompt. Resets the reap + idle clocks
# (the fresh window has not written a transcript yet). rc 1 when the window can't be opened.
_afk_conflict_resolve_relaunch() {
  local wt="$1" issue="$2"
  log "→ conflict-resolve #$issue: relaunching the reaped spoke to merge the base branch + resolve + re-push"
  _afk_set_last_action "conflict-resolve #$issue"
  if ! _afk_open_spoke_window "$wt" "$issue" \
       "$(_afk_continue_command "$wt" "$(_afk_conflict_resolve_prompt "$issue")")"; then
    log "  could not open a conflict-resolve window for #$issue"
    return 1
  fi
  stamp_progress_epoch "$issue"
  stamp_answer_attempt "$issue"
  broker_journal_decision "$issue" conflict-resolve \
    "relaunched the reaped spoke to merge the base branch + resolve the land conflict + re-push" reversible
  _afk_bump_count "$wt" relaunch-count
  _afk_emit_span "$wt" afk-conflict-resolve success
  return 0
}
# _afk_conflict_resolve_inject <wt> <issue> -> LIVE pane: inject the resolve prompt into the
# running session (no relaunch — never kill a working spoke). rc mirrors inject_and_verify.
_afk_conflict_resolve_inject() {
  local wt="$1" issue="$2" target rc
  log "→ conflict-resolve #$issue: injecting merge-base + resolve + re-push into the live session (no relaunch)"
  _afk_set_last_action "conflict-resolve #$issue"
  target="$(_spoke_pane_target "$wt")"
  if [ -z "$target" ]; then
    log "  no live pane for #$issue — cannot inject"
    return 1
  fi
  stamp_answer_attempt "$issue"
  inject_and_verify "$wt" "$target" "$(_afk_conflict_resolve_prompt "$issue")"; rc=$?
  broker_journal_decision "$issue" conflict-resolve \
    "injected merge the base branch + resolve + re-push into the live session (no relaunch)" reversible
  if [ "$rc" -eq 0 ]; then _afk_emit_span "$wt" afk-conflict-resolve success; else _afk_emit_span "$wt" afk-conflict-resolve retry; fi
  return "$rc"
}
# _afk_route_conflict_resolution <wt> <issue> -> dispatch ONE resolution per distinct conflict:
# inject a live pane, relaunch a dead one. On a successful dispatch mark the distinct budget; a
# failed dispatch (or a repeat while the tip is unchanged) warn-parks LAST on the LAND lane so
# the watchdog escalates needs-human-land only after the drain's resolution genuinely fell short.
_afk_route_conflict_resolution() {
  local wt="$1" issue="$2" tip
  tip="$(git -C "$wt" rev-parse HEAD 2>/dev/null || true)"
  # Already dispatched for THIS spoke tip? Then the spoke has not progressed since — a re-land
  # here was triggered by a sibling advancing main, not by the spoke. Warn-park LAST, never
  # re-inject into a spoke already told to resolve; only a moved spoke tip earns a fresh dispatch.
  if _afk_already_conflict_resolved "$issue" && [ "$(_afk_read_conflict_resolved_tip "$issue")" = "$tip" ]; then
    _warn_parked_last "$wt" "$issue" "land conflicts deterministically; resolution already dispatched — waiting for the spoke to merge the base branch + resolve + re-push" land
    return 0
  fi
  if _spoke_pane_alive "$wt"; then
    if _afk_run_with_heartbeat_fg _afk_conflict_resolve_inject "$wt" "$issue"; then
      _afk_mark_conflict_resolved "$issue" "$tip"
    else
      _warn_parked_last "$wt" "$issue" "land conflicts; live-pane resolve-inject did not register — retrying at low frequency" land
    fi
  elif _afk_conflict_resolve_relaunch "$wt" "$issue"; then
    _afk_mark_conflict_resolved "$issue" "$tip"
  else
    _warn_parked_last "$wt" "$issue" "land conflicts and the resolution relaunch could not start — retrying at low frequency" land
  fi
}

# --- #339: recover a REVERSIBLE dirty hub so a mergeable land is not stalled forever ---------
# The land script refuses to land while the hub checkout carries a TRACKED modification
# (`git status --porcelain -uno`) — a generic exit-1 die auto_land cannot tell apart from a
# transient push rejection, so the old lane warn-parked + retried "at low frequency" FOREVER.
# But a tracked-mod dirty hub never clears itself (the #333 self-sync .gitignore block, a stray
# *.bak, a half-finished manual edit), so every retry re-failed identically until the watchdog
# fired auto-land-skipped 900s later (#338) — a silent stall (principle #2) escalating a
# REVERSIBLE condition (principle #3). The lane now ACTS: detect the dirty hub EXPLICITLY (git
# status, never inferred from a shared exit code — principle #1), stash it so the guard passes,
# land, then restore. Only a hub whose dirt cannot be safely stashed is escalated for a human.
#
# The hub root is MAIN_ROOT (the checkout the supervisor arms + lands on). When it is unset — the
# land unit tests that drive auto_land with a stubbed land script and no MAIN_ROOT — recovery is
# INERT (an absent hub root is "unknown", never a basis to mutate a working tree — principle #6),
# so the lane never stashes the real repo the test coprocess runs in.
_afk_hub_root() { printf '%s\n' "${MAIN_ROOT:-}"; }

# _afk_hub_is_dirty -> true when the hub checkout carries a tracked modification (the exact
# condition the land script's dirty-hub guard refuses on; untracked ignored via -uno). False
# when the hub root is unknown — never stash blind.
_afk_hub_is_dirty() {
  local root; root="$(_afk_hub_root)"
  [ -n "$root" ] || return 1
  [ -n "$(git -C "$root" status --porcelain -uno 2>/dev/null)" ]
}

# _afk_stash_hub <issue> -> set the hub's tracked dirt aside (git stash) so the dirty-hub land
# guard passes; journal the reversible action for the morning audit (principle #3). rc 0 when the
# hub is now clean (a stash was created — the caller MUST restore after the land), rc 1 when the
# dirt could not be set aside (not safely reversible — the caller escalates instead of looping).
_afk_stash_hub() {
  local issue="$1" root; root="$(_afk_hub_root)"
  git -C "$root" stash push -m "afk-land-$issue-$(afk_now)" >/dev/null 2>&1 || true
  # Still dirty ⇒ the dirt could not be set aside — escalate. (A partial stash that left the tree
  # dirty is preserved in the stash list for the escalation-handling human, never silently dropped.)
  _afk_hub_is_dirty && return 1
  log "  #$issue: stashed a dirty hub checkout so the land can proceed (reversible; restored after)"
  broker_journal_decision "$issue" land "stashed a dirty hub checkout before landing; restored after" reversible
  return 0
}

# _afk_restore_hub <issue> -> re-apply the dirt stashed by _afk_stash_hub after the land. A pop
# that does not apply cleanly (rare: the land does not touch the stashed paths) KEEPS the stash
# entry AND leaves conflict markers in the tree, so the changes are never dropped — it warns
# LOUDLY (principle #2/#6). The next tick reads that tree dirty and re-stashes it (loud, not a
# silent loop — the warn re-fires when due), so a human sees it rather than losing the dirt.
_afk_restore_hub() {
  local issue="$1" root; root="$(_afk_hub_root)"
  if git -C "$root" stash pop >/dev/null 2>&1; then
    log "  #$issue: restored the hub checkout dirt after landing"
    return 0
  fi
  log "  #$issue: could not cleanly restore the stashed hub dirt — it is preserved in the hub git stash"
  broker_warn "$issue" "hub-dirt stash did not re-apply cleanly after landing — preserved in the hub git stash; a human should reconcile it"
  return 1
}

# auto_land -> land every ready/<issue> spoke. The ready/<issue> marker is the readiness
# contract (enforced by _ready_at_tip above), so a foreign ready/<issue> left by a parallel
# session is adopted and landed by default (#95). A failed land (merge conflict) emits
# blocked/<issue> and the drain continues; a landed spoke frees its scope + its dependents'
# blockers for the next tick's plan. Set AFK_LAND_FOREIGN=0 to restore the dispatched-only
# isolation (skip any ready/<issue> with no dispatch epoch) so concurrent sessions don't
# surprise-land each other's work (#74).
#
# Trust the ready-marker green (#144): the ready/<issue> marker IS the green contract — the
# spoke's own ship gate already ran the full suite on this exact tree before emitting it (and
# _ready_at_tip proved marker == tip). So the land runs with --skip-tests: re-running the
# suite at land time is redundant AND self-flakes under a live drain, because a diverged land
# builds a merge commit whose gate re-runs the whole suite and the tripwire / worktree-land /
# test-select tests collide with the concurrent land moving refs (#140). Manual `/land` keeps
# its diverged-merge gate — the trust is applied by this caller, not baked into worktree-land.
# UPGRADE: if trusting a merge commit's untested combined tree ever proves too loose, swap
#   --skip-tests here for a fast merge-sanity check (pytest --collect-only + changed-file
#   tests) on diverged lands only — cheap, and it never runs the ref-colliding suites.
#
# Escalate ONCE, never loop (#144): a deterministic land failure escalates blocked/<issue> at
# the tip, but ready/<issue> still sits there too, so a naive re-survey would re-land → fail →
# reset → re-land forever (#140). auto_land skips any issue already carrying blocked/<issue>
# at its tip (_blocked_at_tip); reconcile_markers revives it once the spoke commits a real fix.
#
# The reasoning code-review verdict is the /afk test-gutting gate (#143), ON by default again
# (#183): auto_land lands ONLY on a clean APPROVE verdict — a REQUEST_CHANGES (the reviewer
# flagged gutting) or no review at all escalates to blocked/<issue> instead. It defaulted OFF
# under #152 because the #143 gate false-positive-escalated clean lands whose spokes left no
# verdict artifact in the reader's format (reviews that finished in <1s), bricking the whole
# drain (#151). That failure class is now closed at the SOURCE by #172: every ready/<issue>
# emission through spoke-ready requires an APPROVE artifact bound to the tip, so a ready with
# no clean verdict can only be a hand-crafted bypass — escalating it is the intended gate, not
# a false positive. Set AFK_REVIEW_GATE=0 to opt back out (restore the #152 land-anything
# behavior); the mechanical anti-gutting scan stays the advisory residual signal either way.
auto_land() {
  local wt_land path issue verdict max tries land_log land_rc land_before hub_stashed
  wt_land="$(_afk_find_script "${WT_LAND:-}" worktree-land.sh)" || { log "worktree-land.sh not found — skipping land"; return 0; }
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    _ready_at_tip "$path" "$issue" || continue
    # #241/#274: pace the per-issue land attempt on the LAND-lane warned-retry backoff. A prior
    # land failure / unclean-review / retry-exhausted warn armed the LAND lane (auto_land's own
    # park kinds — land, review); while it is pending this spoke is skipped (parked LAST), so a
    # permanently-conflicted land is re-attempted at LOW frequency (worktree-land is expensive) —
    # not every tick. Reading the LAND lane (not the shared file) is the #274 fix: an ANSWER-lane
    # re-answer backoff no longer starves the land of a ready spoke (#269). A fresh (never-warned)
    # spoke is always due, so the first land attempt is never delayed; the ready→done transition
    # clears the lane (slot_state, #274) and a successful land clears it below. Never a silent skip
    # (#274 AC3): log the reason + next-due epoch so "drain still pacing" is distinguishable from
    # "drain abandoned it" (the watchdog's auto-land-skipped escalation).
    if ! _afk_warned_due "$issue" "" land; then
      log "  skip land #$issue — land-lane retry backoff pending (next-due $(_afk_warned_next "$issue" land), now $(afk_now)); retrying at low frequency"
      continue
    fi
    # #285: a recorded DETERMINISTIC conflict whose tips are UNCHANGED would re-conflict
    # identically — do NOT re-run the expensive land; route to the resolution lane instead.
    if _afk_land_conflict_unchanged "$path" "$issue"; then
      log "  skip re-land #$issue — land conflicts deterministically and tips are unchanged; routing to the resolution lane (no identical re-land)"
      _afk_route_conflict_resolution "$path" "$issue"
      continue
    fi
    # The fingerprint moved (spoke resolved + re-pushed, or a sibling advanced main): the stale
    # fingerprint no longer describes the pending land, so drop it and fall through to a fresh
    # attempt. The resolution budget is NOT cleared here — it is keyed on the spoke's own tip
    # (_afk_route_conflict_resolution), so a main-only advance never re-injects (#285 review).
    _afk_clear_land_conflict_fp "$issue"
    if _blocked_at_tip "$path" "$issue"; then
      # ready+blocked at a finished tip = a TRANSIENT land failure. Retry the land up to
      # AFK_LAND_RETRY_MAX times, then escalate VISIBLY — never skip-land it forever (#202 D).
      max="$(_afk_land_retry_max)"; tries="$(_afk_read_land_retries "$issue")"
      if [ "$tries" -ge "$max" ]; then
        # #241 §5: no longer terminal — warn + retry on the warned-retry backoff (low frequency),
        # never park blocked/<issue>. The land is re-attempted on later ticks/windows.
        log "  land #$issue still fails after $tries retry attempt(s) — warn-parking on the backoff (#241)"
        _warn_parked_last "$path" "$issue" "land retried $tries time(s) and still fails at a finished tip — retrying at low frequency" land
        continue
      fi
      log "  retry land #$issue — ready+blocked coexist at a finished tip (transient land failure); clearing blocked/$issue and re-landing (attempt $(( tries + 1 ))/$max)"
      _afk_incr_land_retries "$issue"
      git -C "$path" tag -d "blocked/$issue" >/dev/null 2>&1 || true
      git -C "$path" push origin ":refs/tags/blocked/$issue" >/dev/null 2>&1 || true
      # fall through to the land attempt below
    fi
    if [ "${AFK_LAND_FOREIGN:-1}" = "0" ] && [ -z "$(read_dispatch_epoch "$issue")" ]; then
      log "  skip land #$issue — foreign (no dispatch epoch) and AFK_LAND_FOREIGN=0"
      continue
    fi
    if [ "${AFK_REVIEW_GATE:-1}" != "0" ]; then
      verdict="$(_afk_review_verdict "$path")"
      if [ "$verdict" != "APPROVE" ]; then
        # #241 §6: never silent block. Per AFK_REVIEW_GATE_ON_UNCLEAN:
        #   retry (DEFAULT, safe) — warn + retry; do NOT auto-land, since a ready/<issue> with an
        #     unclean/missing verdict is a #172-bypass and landing it ships possibly-test-gutted
        #     code to main. The loud warning surfaces it for the human.
        #   land — warn LOUDLY + land anyway (records the unclean verdict for post-review), the
        #     operator's explicit opt-in to §6's land-with-warning (the "mint a hub-side review
        #     first" step is not implementable here). UPGRADE: mint a hub-side review attempt.
        if [ "${AFK_REVIEW_GATE_ON_UNCLEAN:-retry}" = "land" ]; then
          broker_warn "$issue" "LANDING despite an unclean review verdict (${verdict:-no review}) — possible test-gutting; review post-hoc"
          # outward: the land merges+pushes to shared main (others pull it, CI fires) — not merely a scope change.
          broker_journal_decision "$issue" review "landed despite unclean review verdict (${verdict:-no review})" outward
          # fall through to land
        else
          _warn_parked_last "$path" "$issue" "code-review verdict not clean (${verdict:-no review}) — warn + retry; set AFK_REVIEW_GATE_ON_UNCLEAN=land to land with a warning" review
          continue
        fi
      fi
    fi
    log "→ land #$issue"
    _afk_set_last_action "land #$issue"
    # #339: a dirty hub checkout (a tracked modification — the #333 sync .gitignore litter, a
    # half-finished manual edit) makes the land script refuse with a generic exit-1 die that this
    # lane would warn-park + retry forever (a silent stall surfacing only via the watchdog's 900s
    # auto-land-skipped). A tracked-mod dirty hub is REVERSIBLE: stash it so the guard passes
    # (restored after the land); only an un-stashable hub escalates for a human (warn-park LAST).
    hub_stashed=0
    if _afk_hub_is_dirty; then
      if _afk_stash_hub "$issue"; then
        hub_stashed=1
      else
        _warn_parked_last "$path" "$issue" "hub checkout is dirty and could not be safely stashed for landing — needs a human to clean the hub" land
        continue
      fi
    fi
    # Capture the land's output to a per-issue log (#198): the old >/dev/null discarded exactly
    # what an operator needs when a land half-completes. mkdir so the log write can't fail on a
    # not-yet-created state dir. _afk_run_with_heartbeat returns worktree-land's exit code.
    land_log="$(_afk_state_dir)/land-$issue.log"; mkdir -p "$(_afk_state_dir)" 2>/dev/null || true
    # Bracket the land with the local default-branch SHA so a supervisor-scope merge is
    # detectable from the pre..post diff (#250 self-update DETECT).
    land_before="$(_afk_local_default_sha)"
    # #300 step 3: read the run id BEFORE the land — a clean land tears the worktree down, so
    # .ai-toolkit/spoke-run-id is gone by the time we record the reaped transition.
    land_run="$(_afk_spoke_run_id "$path")"
    _afk_run_with_heartbeat bash "$wt_land" "$issue" --skip-tests >"$land_log" 2>&1; land_rc=$?
    # #339: restore the pre-land dirt on EVERY land outcome (success, teardown-incomplete,
    # conflict, or failure) before the rc-branch dispatch, so the hub returns to its prior state.
    [ "$hub_stashed" -eq 1 ] && _afk_restore_hub "$issue"
    if [ "$land_rc" -eq 0 ]; then
      log "  landed #$issue"
      _afk_clear_land_retries "$issue"   # a successful land resets the retry budget (#202 D)
      _afk_clear_warned "$issue"         # #241: progress → drop the land's warned-retry backoff
      _afk_incr_landed   # tally for the drain-complete notification (#150)
      _afk_detect_selfupdate "$land_before" "$(_afk_local_default_sha)" "$issue"  # #250
      # #300 step 3: the drain reaping the landed spoke is a lifecycle transition (distinct from
      # worktree-land.sh's own `landed` — this is the DRAIN acting on completion, tallied + torn down).
      AFK_TLOG_RUN="$land_run" wt_tlog_transition "$issue" reaped hub-afk.sh \
        "landed and reaped by the drain" "{\"land_log\":\"$land_log\"}"
    elif [ "$land_rc" -eq 3 ]; then
      # Sentinel (#198 / #202 I): main ADVANCED but a teardown step failed — the code IS
      # shipped, so NEVER stamp blocked over merged work. Tally it and point at the log.
      log "  landed #$issue but teardown incomplete (worktree-land exit 3) — see $land_log; NOT escalating (main already advanced)"
      _afk_clear_land_retries "$issue"
      _afk_clear_warned "$issue"         # #241: shipped → drop the warned-retry backoff
      _afk_incr_landed
      _afk_detect_selfupdate "$land_before" "$(_afk_local_default_sha)" "$issue"  # #250: shipped ⇒ still deploy
      # #300 step 3: shipped, so record the reap — the evidence flags the incomplete teardown so a
      # reader sees a reaped spoke whose worktree may still be on disk.
      AFK_TLOG_RUN="$land_run" wt_tlog_transition "$issue" reaped hub-afk.sh \
        "landed; teardown incomplete (worktree-land exit 3)" \
        "{\"land_log\":\"$land_log\",\"teardown\":\"incomplete\"}"
    elif [ "$land_rc" -eq "$WT_LAND_CONFLICT_EXIT" ]; then
      # #285: a DETERMINISTIC merge conflict — record the tip fingerprint and route to the
      # resolution lane (relaunch/inject the spoke to merge the base branch + resolve + re-push).
      # NOT a generic warn-park: re-running the identical land is futile until a tip moves.
      log "  land #$issue conflicts with the base branch (exit $land_rc) — routing to the resolution lane (see $land_log)"
      _afk_write_land_conflict_fp "$issue" "$(_afk_land_conflict_fingerprint "$path")"
      _afk_route_conflict_resolution "$path" "$issue"
    else
      # #241 §5: a TRANSIENT / non-conflict auto-land failure (push rejection, dirty-tree guard,
      # etc. — any non-conflict wt_die exit) warns + retries on the backoff instead of parking
      # blocked/<issue>. The land is re-attempted on later ticks. (Exit 4 = a deterministic
      # conflict is handled above; this branch is every OTHER non-zero exit.)
      _warn_parked_last "$path" "$issue" "auto-land failed (non-conflict, exit $land_rc) — retrying at low frequency (see $land_log)" land
    fi
  done < <(inflight_worktrees)
}

# answer_pass -> auto-answer every waiting spoke. reap_pass -> reap every hung/overrun one.
answer_pass() {
  local path issue
  while IFS=$'\t' read -r path issue; do
    [ -n "$issue" ] || continue
    # Keep the heartbeat stamping THROUGH the answerer (a high-effort headless `claude`
    # that can run for minutes) so a legitimately long answer never trips the wedged-
    # supervisor respawn (#170 ST2). The foreground variant preserves decide_and_act's
    # _AFK_AUTH_FAILED assignment (a backgrounded command would lose it in its subshell).
    if [ "$(slot_state "$path" "$issue")" = "waiting" ]; then
      _afk_set_last_action "answer #$issue"
      _afk_run_with_heartbeat_fg decide_and_act "$path" "$issue"
    fi
  done < <(inflight_worktrees)
}
