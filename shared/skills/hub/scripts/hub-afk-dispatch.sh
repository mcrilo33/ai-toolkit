#!/usr/bin/env bash
# hub-afk-dispatch.sh -- split out of hub-afk.sh (issue #307).
#
# The DISPATCH lane of the /afk supervisor: plan + batch dispatch and subtask routing --
# dispatch_batch, kickoff_for, the in-flight scope args, the concurrency cap + stagger, the
# dispatch-failure ceiling, the #278 subtask-chain packing/routing, and the afk:* GitHub
# status labels. A pure function-definition module sourced by the entry lib hub-afk.sh AFTER
# worktree-lib / gate-broker / log / afk_now and the entry's own state/time primitives, and
# BEFORE any function is called, so every cross-module helper resolves at call time. Not run
# on its own.
set -uo pipefail

# --- dispatch -----------------------------------------------------------------

# kickoff_for <issue> -> the spoke's first prompt: the standard ultra kickoff (the same
# handoff start-task / next-batch use). Under /afk the spoke runs in its normal attended
# posture — it pauses at its PLAN gate and asks questions as if a human were watching —
# and the supervisor's answerer plays the human. So the kickoff is deliberately the
# everyday one, NOT a "park, never ask" variant.
kickoff_for() {
  local n="$1" marker_dir
  # Name the marker-emitter path that EXISTS in the spoke's worktree (#271): `scripts` in the
  # ai-toolkit checkout, `.ai-toolkit/scripts` in a synced target. The spoke is cut from the hub
  # (_AFK_TOPLEVEL), which shares the layout, so probe it — a hardcoded `.ai-toolkit/scripts/`
  # nudge re-emits the nonexistent path this repo's deny-wall judges and cannot exec.
  marker_dir="$(wt_marker_script_dir "${_AFK_TOPLEVEL:-.}")"
  cat <<EOF
You're in a dedicated worktree for issue #$n. Your task contract is on disk at
.ai-toolkit/task.md (worktree-new.sh fetched it at spawn) — read it; no need to run
/source-task (that stays for re-anchor: run /source-task $n only if task.md is missing
or the issue was edited after spawn — that re-fetches the live issue). Before touching
code, break the task into a task ledger (TaskCreate, or
TodoWrite on older runtimes) — one todo per subtask × the solo-cycle steps that apply
(ANCHOR/RED/GREEN/REVIEW/PUSH), exactly one in_progress.

Honor the issue's Gate: line. If it is \`plan\` (the default for non-trivial work): the
PLAN gate comes first — explore the code, then print the full implementation plan (files,
approach, test strategy, open questions) as a normal visible message. Then emit the gate
marker AND hand it your plan, so the hub reads it from a scripted artifact rather than
parsing your transcript: write the plan to a gitignored scratch file (e.g.
\`.ai-toolkit/gate-plan.md\` — the .ai-toolkit/ dir is gitignored, so it never dirties your
tree or blocks the ready gate) and pass it with
\`bash ${marker_dir}/spoke-ready.sh --gate $n --plan-file .ai-toolkit/gate-plan.md\`
(or inline a short plan with \`--gate $n -m "<plan>"\`). That parks you at the gate; WAIT for
approval before writing code (before GREEN). If the gate is \`none\`, run autonomous straight through.

Then implement following the solo-cycle (/cycle: RED → GREEN → REVIEW → PUSH). Push your
own branch on every subtask without asking; when your ledger shows the issue's acceptance
criteria are all met, push the final subtask and emit the ready marker (bash
${marker_dir}/spoke-push.sh --ready $n) — also without asking. Still ask before
genuinely dangerous or irreversible ops (force-push, history rewrites, anything touching
\`main\`, deletions outside the worktree). Do NOT self-land — the hub lands #$n.
EOF
}

# _inflight_scope_args -> repeated `--inflight "<scope>"` flags, one per live spoke, so
# batch-plan holds back a ready issue that collides with work already running. The Scope:
# line is read from each in-flight issue's body (the same source batch-plan reads). When a
# live spoke's scope CANNOT be resolved (gh failed, or the issue has no Scope: line) its
# footprint is unknown, so we emit `--inflight *` (exclusive) and batch-plan holds back
# EVERY ready issue until it lands — failing CLOSED under unattended /afk (#74) rather than
# co-dispatching into an unknown-scope collision.
_inflight_scope_args() {
  local issue body scope
  while IFS= read -r issue; do
    [ -n "$issue" ] || continue
    # Bound the gh call (#170 ST1): a hung `gh issue view` used to freeze the tick. A
    # timeout / failure logs and leaves the scope unknown, which fails CLOSED below
    # (`--inflight *` holds back every ready issue) — never a silent empty scope.
    if ! body="$(_afk_with_timeout "$AFK_GH_TIMEOUT" gh issue view "$issue" --json body -q .body 2>/dev/null)"; then
      log "  gh issue view #$issue timed out or failed — treating its scope as unknown (exclusive)"
      body=""
    fi
    scope="$(printf '%s\n' "$body" | sed -n 's/^[[:space:]]*[Ss]cope:[[:space:]]*//p' | head -1)"
    printf -- '--inflight\n%s\n' "${scope:-*}"
  done < <(inflight_issues)
}

# _afk_scope_blocked_behind <issue> -> #305: the space-joined `#N` list of OPEN issues batch-plan
# is holding back specifically because their Scope: collides with THIS in-flight issue — i.e. the
# work stalled behind it. Runs the SAME `batch-plan.sh --explain` the label sync uses, seeded with
# the live in-flight scopes (_inflight_scope_args) + issue numbers (inflight_issues), and collects
# every line whose disposition is `blocked-by-scope:#<issue>` (verified empirically — batch-plan
# prints one such line per held issue, and a `— holds back #A, #B` suffix on the in-flight line).
# Best-effort + timeout-bounded like every gh/planner call on this path: EMPTY on any failure (no
# batch-plan, no python3, a planner error), so a naming miss never blocks the escalation it annotates.
_afk_scope_blocked_behind() {
  local issue="$1" bp out n
  case "$issue" in '' | *[!0-9]*) return 0 ;; esac
  bp="$(_afk_find_script "${BATCH_PLAN:-}" batch-plan.sh)" || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  local args=() line
  while IFS= read -r line; do args+=("$line"); done < <(_inflight_scope_args)
  while IFS= read -r n; do [ -n "$n" ] && args+=("--inflight-issue" "$n"); done < <(inflight_issues)
  out="$(_afk_with_timeout "$AFK_PLANNER_TIMEOUT" bash "$bp" --explain ${args[@]+"${args[@]}"} 2>/dev/null)" || return 0
  printf '%s\n' "$out" | awk -v want="blocked-by-scope:#${issue}" '$2 == want { printf "%s ", $1 }' | sed 's/ $//'
}

# --- concurrency cap + dispatch stagger (issue #151) --------------------------
# The hub had no ceiling on live spokes, so a wide batch drove load high enough to
# starve the co-located Langfuse (permanent trace loss). Cap the batch and stagger
# spawns so first-push full suites don't all land on the box at once.

# _afk_cores -> logical CPU count (nproc → sysctl → 1). LC_ALL=C guards the
# locale-formatted-number class this repo has been bitten by (ps/date).
_afk_cores() {
  local n
  n="$(LC_ALL=C nproc 2>/dev/null || LC_ALL=C sysctl -n hw.ncpu 2>/dev/null || printf '1')"
  case "$n" in '' | *[!0-9]*) n=1 ;; esac
  [ "$n" -ge 1 ] || n=1
  printf '%s\n' "$n"
}

# _afk_batch_config_env -> the AI_TOOLKIT_BATCH_* lines the config parser emits for
# settings/ai-toolkit.yml, or nothing when the parser/config can't be resolved (a
# synced target ships neither). Best-effort: the caller falls back to its own default.
_afk_batch_config_env() {
  local cfg_py cfg_yml
  cfg_py="$(_afk_find_script "${AFK_CONFIG_PY:-}" ai_toolkit_config.py)" || return 0
  cfg_yml="${AI_TOOLKIT_CONFIG:-${MAIN_ROOT:-$_AFK_TOPLEVEL}/settings/ai-toolkit.yml}"
  [ -f "$cfg_yml" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  python3 "$cfg_py" batch-env "$cfg_yml" 2>/dev/null || true
}

# _afk_dispatch_cap -> the max concurrent spokes. AFK_SPOKE_CAP wins (operator/test
# seam), else the config's concurrency_cap, else auto min(2, cores/4) — always ≥1.
_afk_dispatch_cap() {
  local cap="${AFK_SPOKE_CAP:-}" cores AI_TOOLKIT_BATCH_CAP=""
  if [ -z "$cap" ]; then
    eval "$(_afk_batch_config_env)" 2>/dev/null || true
    cap="${AI_TOOLKIT_BATCH_CAP:-}"
  fi
  if [ -z "$cap" ]; then
    cores="$(_afk_cores)"
    cap=$(( cores / 4 )); [ "$cap" -gt 2 ] && cap=2
  fi
  case "$cap" in '' | *[!0-9]*) cap=1 ;; esac
  [ "$cap" -ge 1 ] || cap=1
  printf '%s\n' "$cap"
}

# _afk_dispatch_stagger -> seconds between consecutive spawns in one batch.
# AFK_DISPATCH_STAGGER wins (0 disables; the test seam), else the config, else 45.
_afk_dispatch_stagger() {
  local s="${AFK_DISPATCH_STAGGER:-}" AI_TOOLKIT_BATCH_STAGGER=""
  if [ -z "$s" ]; then
    eval "$(_afk_batch_config_env)" 2>/dev/null || true
    s="${AI_TOOLKIT_BATCH_STAGGER:-}"
  fi
  [ -n "$s" ] || s=45
  case "$s" in *[!0-9]*) s=45 ;; esac
  printf '%s\n' "$s"
}

# --- dispatch-failure ceiling (issue #170 ST6) --------------------------------
# A worktree-new.sh that keeps failing for one issue (a malformed issue, a wedged infra
# dep) used to be retried silently every tick forever. Count consecutive failures per issue
# in the state dir; at AFK_DISPATCH_MAX_FAILURES (default 3) record a durable local block
# (the _afk_record_blocked_locally pattern, surfaced by --status) and skip that issue for
# the rest of the window. A success clears the counter. Cleared on a fresh arm.
: "${AFK_DISPATCH_MAX_FAILURES:=3}"
_afk_dispatch_fail_file() { printf '%s\n' "$(_afk_state_dir)/dispatch-fail-$1.count"; }
_afk_read_dispatch_failures() {
  local f n; f="$(_afk_dispatch_fail_file "$1")"
  n="$( [ -f "$f" ] && head -n1 "$f" 2>/dev/null | tr -d '[:space:]' )"
  case "$n" in '' | *[!0-9]*) n=0 ;; esac
  printf '%s\n' "$n"
}
# _afk_incr_dispatch_failures <issue> -> bump and echo the new consecutive-failure count.
_afk_incr_dispatch_failures() {
  local issue="$1" n dir
  dir="$(_afk_state_dir)"; mkdir -p "$dir" 2>/dev/null || true
  n=$(( $(_afk_read_dispatch_failures "$issue") + 1 ))
  _afk_atomic_write "$(_afk_dispatch_fail_file "$issue")" "$n" || true
  printf '%s\n' "$n"
}
_afk_clear_dispatch_failures() { rm -f "$(_afk_dispatch_fail_file "$1")" 2>/dev/null || true; }
_afk_clear_dispatch_fail_counts() { rm -f "$(_afk_state_dir)"/dispatch-fail-*.count 2>/dev/null || true; }
# _afk_dispatch_max_failures -> the ceiling, guarded to a positive integer. dispatch_batch
# computes this once per tick and compares each issue's count against the cached value.
_afk_dispatch_max_failures() {
  local max="${AFK_DISPATCH_MAX_FAILURES:-3}"
  case "$max" in '' | *[!0-9]* | 0) max=3 ;; esac
  printf '%s\n' "$max"
}

# --- subtask chain cap (issue #278) -------------------------------------------
# The most issues ONE spoke may carry: its own primary plus the packed/routed subtasks. A
# spoke's context window is finite (they reach ~45% on a single issue today), so an unbounded
# chain would exhaust it mid-run. Past the cap an issue is simply not packed/routed — it stays
# in the backlog and dispatches normally once the spoke lands, so nothing is ever dropped.
#
# Env-only for now. Every sibling dispatch knob (concurrency_cap, stagger) also reads
# settings/ai-toolkit.yml via `ai_toolkit_config.py batch-env`, but that file and the parser
# are outside this issue's Scope: line; surfacing this as `batch.subtask_chain_max` is a
# follow-up. AFK_SUBTASK_CHAIN_MAX stays the operator/test seam either way.
: "${AFK_SUBTASK_CHAIN_MAX:=3}"
_afk_subtask_chain_max() {
  local n="${AFK_SUBTASK_CHAIN_MAX:-3}"
  case "$n" in '' | *[!0-9]* | 0) n=3 ;; esac
  printf '%s\n' "$n"
}

# _afk_route_subtask_nudge <wt> <spoke> <issue> -> tell a LIVE spoke it just gained a subtask.
# Mirrors _afk_nudge_spoke/_afk_finish_up_nudge (same inject_and_verify + journal + span
# shape) but carries the routing prompt. Advisory: the queued marker is the authority —
# spoke-ready.sh refuses the terminal ready while the queue is non-empty, so a nudge that
# never lands only delays the pickup to the spoke's own ready boundary, it cannot lose the
# work. Stamps only the answer-attempt epoch, never the progress epoch: routing must not buy
# a spoke a fresh reap ceiling (the #241 rule).
_afk_route_subtask_nudge() {
  local wt="$1" spoke="$2" issue="$3" target rc
  log "→ route #$issue: packable into the live spoke #$spoke — queued as a subtask (no second worktree/suite seed)"
  _afk_set_last_action "route #$issue -> #$spoke"
  target="$(_spoke_pane_target "$wt")"
  [ -n "$target" ] || { log "  no live pane for #$spoke — queued anyway; it consumes at its ready boundary"; return 1; }
  stamp_answer_attempt "$spoke"
  inject_and_verify "$wt" "$target" "$(_afk_route_subtask_prompt "$spoke" "$issue")"; rc=$?
  broker_journal_decision "$spoke" route \
    "issue #$issue shares this spoke's scope — queued onto its branch as a subtask instead of a fresh spoke (#278)" reversible
  if [ "$rc" -eq 0 ]; then _afk_emit_span "$wt" afk-route success; else _afk_emit_span "$wt" afk-route retry; fi
  return "$rc"
}

# _afk_route_queued_subtasks <route-lines> <chain-max> -> trigger B of #278.
#
# Each line is "<issue> <spoke>": batch-plan judged <issue>'s scope to fit INSIDE the live
# <spoke>'s, so one spoke can ship both. Queue it there instead of letting it wait out that
# spoke's entire lifecycle (12-47 min of first-push suite seed alone) only to spawn a fresh
# worktree over the same files.
#
# The planner proposes; this decides, because only the drain holds the two facts that matter:
#
#   * CHAIN CAP — how full the spoke's queue already is. Past the cap the issue is left alone:
#     it stays in the backlog and dispatches normally once the spoke lands. Nothing dropped.
#   * PAST-FINAL-PUSH — whether the spoke already emitted its terminal ready. If so the entry
#     arrived too late to ever be consumed (the spoke is about to be landed and torn down), so
#     RECLAIM it: clear the queue and let the issue take a fresh dispatch. Deterministic, and
#     unlike nudging it to consume, it does not depend on the spoke still being alive.
#
# Writing an entry is always safe — it is a file in the hub's state dir and touches no spoke
# tree. Only CONSUMPTION can introduce RED tests, and that happens at the spoke's ready
# boundary, where clean-tree + HEAD==@{upstream} are already proven, so a subtask can never
# land RED into a tree with a push gate running.
_afk_route_queued_subtasks() {
  local lines="$1" chain_max="$2" issue spoke wt depth
  [ -n "$(printf '%s' "$lines" | tr -d '[:space:]')" ] || return 0
  while read -r issue spoke; do
    # Both fields must be present AND numeric. Checking the CONCATENATION would accept a
    # one-field line ("264" -> issue=264, spoke="") as all-digits; that only fails to route by
    # accident downstream (the awk lookup never matches an empty spoke), which is not a
    # property to lean on. Reject it here, where the shape is actually known.
    case "$issue" in '' | *[!0-9]*) continue ;; esac
    case "$spoke" in '' | *[!0-9]*) continue ;; esac
    wt="$(inflight_worktrees | awk -F'\t' -v n="$spoke" '$2 == n { print $1; exit }')"
    [ -n "$wt" ] || continue   # the spoke landed between the plan and here — next tick re-routes
    # Past-final-push: reclaim rather than queue into a spoke that is already done.
    if _ready_at_tip "$wt" "$spoke"; then
      if [ -n "$(read_queued_subtask "$spoke")" ]; then
        log "  reclaim queued subtask(s) from #$spoke — it already emitted its terminal ready; they fall back to a fresh dispatch"
        clear_queued_subtask "$spoke"
      fi
      continue
    fi
    # Already queued (the drain re-derives every tick): nothing to do, and re-nudging would
    # just spam the pane. stamp is idempotent, but the nudge is not.
    read_queued_subtask "$spoke" | grep -qxF "$issue" && continue
    # Chain cap counts the spoke's OWN issue plus everything queued.
    depth=$(( $(read_queued_subtask "$spoke" | grep -c '^[0-9]' || true) + 1 ))
    if [ "$depth" -ge "$chain_max" ]; then
      log "  not routing #$issue -> #$spoke — chain cap $chain_max reached (spoke already carries $depth); it dispatches fresh once #$spoke lands"
      continue
    fi
    stamp_queued_subtask "$spoke" "$issue"
    _afk_run_with_heartbeat_fg _afk_route_subtask_nudge "$wt" "$spoke" "$issue" || true
  done <<EOF
$lines
EOF
}

# dispatch_batch -> plan the next concurrent batch (batch-plan.sh, capped) and spawn a
# spoke for each issue not already in flight, seeded with the ultra kickoff and staggered
# so first-push suites don't all hit at once. A missing planner or dispatcher logs and is
# a no-op (the next tick retries). Since #278 the batch is a list of ordered GROUPS: a
# comma-joined unit spawns ONE spoke carrying its peers as subtasks, and `route:` lines hand
# a newly-packable issue to an already-live spoke instead of spawning a second worktree.
dispatch_batch() {
  [ "$_AFK_AUTH_FAILED" -eq 1 ] && return 0   # auth is dead — don't spawn spokes into it
  local bp wt_new inflight args=() batch n cap stagger spawned=0 fails max
  local raw units="" route_lines="" line unit primary subtasks member chain_max
  bp="$(_afk_find_script "${BATCH_PLAN:-}" batch-plan.sh)" || { log "batch-plan.sh not found — skipping dispatch"; return 0; }
  wt_new="$(_afk_find_script "${WT_NEW:-}" worktree-new.sh)" || { log "worktree-new.sh not found — skipping dispatch"; return 0; }
  inflight="$(inflight_issues)"
  while IFS= read -r line; do args+=("$line"); done < <(_inflight_scope_args)
  # Bound total live spokes: batch-plan truncates so (in-flight + dispatched) ≤ cap.
  cap="$(_afk_dispatch_cap)"
  stagger="$(_afk_dispatch_stagger)"
  chain_max="$(_afk_subtask_chain_max)"
  args+=("--cap" "$cap")
  # #278: --pack-max bounds a dispatch-time pack to the same chain cap routing uses, so a
  # spoke's context ceiling is one number however the subtasks got there. --route asks for the
  # `route:<issue> <spoke>` lines naming issues packable into an ALREADY-LIVE spoke — the pack
  # above can only group issues ready in the same tick, so without this an issue filed later
  # just waits out the live spoke's whole lifecycle before starting its own.
  args+=("--pack-max" "$chain_max" "--route")
  # Bound the planner (#170 ST1): a wedged batch-plan.sh used to hang the tick. A timeout
  # or nonzero exit logs and skips dispatch THIS tick (retry next tick) — never a silent
  # empty batch that would look like "nothing to dispatch".
  if ! raw="$(_afk_with_timeout "$AFK_PLANNER_TIMEOUT" bash "$bp" "${args[@]+"${args[@]}"}" 2>/dev/null)"; then
    log "batch-plan.sh timed out or failed — skipping dispatch this tick (retry next tick)"
    return 0
  fi
  # Split the planner's stdout into its two channels: the batch line (space-separated UNITS,
  # each a comma-joined group) and the `route:` lines. A bare `for n in $raw` would iterate
  # over "route:264" as if it were an issue number.
  while IFS= read -r line; do
    case "$line" in
      route:*) route_lines="${route_lines}${line#route:}"$'\n' ;;
      '')      ;;
      *)       [ -n "$units" ] || units="$line" ;;
    esac
  done <<EOF
$raw
EOF
  batch="$units"

  # --- trigger B: route a packable issue onto an already-live spoke ------------
  _afk_route_queued_subtasks "$route_lines" "$chain_max"

  max="$(_afk_dispatch_max_failures)"
  for unit in $batch; do
    # A unit is a comma-joined GROUP (#278): "263,265" ships both on ONE branch as ordered
    # subtasks. The primary leads (it names the branch slug, which inflight_worktrees and
    # worktree-land parse); the rest ride along via --subtasks.
    primary="${unit%%,*}"
    subtasks=""
    [ "$unit" != "$primary" ] && subtasks="${unit#*,}"
    # The in-flight guard below compares a BARE issue number, so it must see members, never
    # the whole token: "263,265" can never equal "263", and an un-split unit would silently
    # re-dispatch a live spoke. Skip the whole unit when ANY member is already in flight —
    # that spoke owns the scope, and re-spawning any of it is exactly the collision the
    # planner exists to prevent.
    for member in ${unit//,/ }; do
      if printf '%s\n' "$inflight" | grep -qxF "$member"; then
        primary=""
        break
      fi
    done
    [ -n "$primary" ] || continue
    n="$primary"
    # Dispatch is a LONG phase (a bounded planner, per-spawn staggers, worktree spawns);
    # stamp the heartbeat each iteration so the wedged-supervisor watchdog (#170 ST2) never
    # mistakes a busy dispatch for a hang and kills a working supervisor mid-spawn.
    afk_write_heartbeat
    # (the in-flight check moved above the loop body: it now runs PER MEMBER, since a grouped
    # token can never match a bare issue number)
    # Ceiling (#170 ST6): an issue that already failed to dispatch AFK_DISPATCH_MAX_FAILURES
    # times this window is durably blocked — skip it instead of retrying forever. Uses the
    # cached `max` (computed once above) rather than recomputing it per issue.
    [ "$(_afk_read_dispatch_failures "$n")" -ge "$max" ] && continue
    # Stagger consecutive spawns (before the 2nd onward), so the co-located Langfuse
    # isn't hit by several first-push full suites at the same instant.
    [ "$spawned" -gt 0 ] && [ "$stagger" -gt 0 ] && sleep "$stagger" 2>/dev/null || true
    if [ -n "$subtasks" ]; then
      log "→ dispatch #$n (+ packed subtasks ${subtasks//,/ } on the same branch)"
    else
      log "→ dispatch #$n"
    fi
    _afk_set_last_action "dispatch #$n"
    # --mode afk stamps the spoke's trace as drain-driven (#102); a hand-dispatched
    # spoke defaults to attended in worktree-new.sh. --subtasks (#278) hands the packed peers
    # to worktree-new, which seeds them into the spoke's queued-subtask channel and appends
    # the chain note to this kickoff.
    if bash "$wt_new" "$n" --type feature --mode afk ${subtasks:+--subtasks} ${subtasks:+"$subtasks"} \
         --prompt "$(kickoff_for "$n")"; then
      stamp_dispatch_epoch "$n"
      _afk_clear_dispatch_failures "$n"   # a success resets the consecutive-failure count
      spawned=$(( spawned + 1 ))
    else
      fails="$(_afk_incr_dispatch_failures "$n")"
      if [ "$fails" -ge "$max" ]; then
        # #241 §5: no durable BLOCK — warn (backoff-gated, low frequency) and skip this window; a
        # fresh arm retries. There is no spoke worktree yet, so pass an empty wt.
        log "  dispatch of #$n failed $fails times — warn-parking (retries next window, see --status)"
        _warn_parked_last "" "$n" "dispatch (worktree-new.sh) failed $fails consecutive times — retried at low frequency" dispatch
      else
        log "  dispatch of #$n failed ($fails/$max) — will retry next tick"
      fi
    fi
  done
}

# --- afk:* status labels on GitHub issues (issue #223) ------------------------
# Behind AFK_GH_STATUS_LABELS=1, the drain maintains ONE afk:* status label per open issue
# reflecting its scheduling disposition, so the GitHub issue LIST answers "what's running /
# why is this waiting" at a glance. The label set (AFK_STATUS_LABELS overrides it for tests):
#   afk:in-flight | afk:queued | afk:blocked-by-scope | afk:exclusive
# Deliberately WITHOUT the cross-issue "blocks #N" detail — that stays in `--explain`; the
# per-issue label is only the issue's own state. Disposition comes from batch-plan's
# `--explain-labels` (the SAME renderer the terminal view uses), so the two never drift.
#
# afk_sync_status_labels is a per-tick RECONCILE: it also strips the label from any issue no
# longer open/in-flight (closed/landed, now held, or dep-blocked), so "stripped on close" is
# satisfied from within the drain — worktree-land.sh / worktree-done.sh are never edited. The
# design cautions the issue calls out: update IN PLACE (swap the one label, never comment),
# write only on CHANGE (skip the gh edit when unchanged, bounding API use per tick), and stay
# BEST-EFFORT (a gh failure logs and continues, never breaks a tick).
: "${AFK_STATUS_LABELS:=afk:in-flight afk:queued afk:blocked-by-scope afk:exclusive}"

afk_status_labels_enabled() { [ "${AFK_GH_STATUS_LABELS:-}" = "1" ]; }

# _afk_seed_status_labels -> create the afk:* label set in the repo once per window (a marker
# in the state dir dedups). `gh label create --force` is idempotent (updates an existing
# label rather than erroring). Best-effort: a create failure never aborts a tick.
_afk_status_labels_seed_marker() { printf '%s\n' "$(_afk_state_dir)/status-labels-seeded"; }
_afk_seed_status_labels() {
  local m lbl; m="$(_afk_status_labels_seed_marker)"
  [ -f "$m" ] && return 0
  for lbl in $AFK_STATUS_LABELS; do
    _afk_with_timeout "$AFK_GH_TIMEOUT" gh label create "$lbl" --force >/dev/null 2>&1 || true
  done
  mkdir -p "$(_afk_state_dir)" 2>/dev/null || true
  printf '%s\n' "$(afk_now)" > "$m" 2>/dev/null || true
}
_afk_clear_status_labels_seed() { rm -f "$(_afk_status_labels_seed_marker)" 2>/dev/null || true; }

# afk_sync_status_labels -> reconcile every open issue's afk:* label to its scheduling
# disposition (and strip stale ones). A no-op unless AFK_GH_STATUS_LABELS=1. Best-effort
# throughout: a missing tool, a failed planner, or a failed gh edit logs and returns 0.
afk_sync_status_labels() {
  afk_status_labels_enabled || return 0
  command -v gh >/dev/null 2>&1 || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  local bp; bp="$(_afk_find_script "${BATCH_PLAN:-}" batch-plan.sh)" || return 0
  # Desired: `<num>\t<afk:label|->` per open issue, seeded with the live in-flight set so a
  # running spoke's issue is labelled afk:in-flight. A planner failure means "unknown" —
  # skip this tick rather than strip every label on a transient blip.
  local ifargs=() n desired
  while IFS= read -r n; do [ -n "$n" ] && ifargs+=("--inflight-issue" "$n"); done < <(inflight_issues)
  if ! desired="$(_afk_with_timeout "$AFK_PLANNER_TIMEOUT" bash "$bp" --explain-labels ${ifargs[@]+"${ifargs[@]}"} 2>/dev/null)"; then
    log "  afk labels: batch-plan --explain-labels failed — skipping label sync this tick"
    return 0
  fi
  [ -n "$desired" ] || return 0
  # Current issues across ALL states (a closed/landed issue must lose its label too). A plain
  # --state all list (not a `--search label:` query, whose default-state semantics are
  # unreliable) keeps this unambiguous; python filters to the afk:* holders. --limit bounds
  # the payload: a recently-closed issue needing a strip is always in the newest slice.
  local current
  if ! current="$(_afk_with_timeout "$AFK_GH_TIMEOUT" gh issue list --state all --limit 200 --json number,state,labels 2>/dev/null)"; then
    current="[]"
  fi
  _afk_seed_status_labels
  # Diff desired-vs-current in python (JSON label parsing is unpleasant in bash): emit one
  # `<num>\t<add|->\t<remove-csv|->` line per issue that actually CHANGES (write-on-change).
  local plan
  plan="$(_AFK_DESIRED="$desired" _AFK_CURRENT="$current" _AFK_LABELS="$AFK_STATUS_LABELS" python3 <<'PYEOF'
import json
import os

afk = set(os.environ.get("_AFK_LABELS", "").split())
desired = {}
for line in os.environ.get("_AFK_DESIRED", "").splitlines():
    if not line.strip():
        continue
    num, _tab, lab = line.partition("\t")
    num, lab = num.strip(), lab.strip()
    if num:
        desired[num] = lab  # an afk:* label, or "-" for held/dep-blocked (no label)

try:
    holders = json.loads(os.environ.get("_AFK_CURRENT", "") or "[]")
except Exception:
    holders = []
present = {}
for item in holders if isinstance(holders, list) else []:
    num = str(item.get("number"))
    present[num] = [l.get("name") for l in (item.get("labels") or []) if l.get("name") in afk]

for num in set(desired) | set(present):
    want = desired.get(num, "-")  # absent from the open backlog ⇒ strip whatever it carries
    have = present.get(num, [])
    if want == "-":
        if have:
            print(f"{num}\t-\t{','.join(have)}")
        continue
    if have == [want]:
        continue  # already correct — no gh call
    remove = [l for l in have if l != want]
    print(f"{num}\t{want}\t{','.join(remove) if remove else '-'}")
PYEOF
)"
  local issue add remove args
  while IFS=$'\t' read -r issue add remove; do
    [ -n "$issue" ] || continue
    args=()
    [ "$add" != "-" ] && args+=("--add-label" "$add")
    [ "$remove" != "-" ] && args+=("--remove-label" "$remove")
    [ "${#args[@]}" -gt 0 ] || continue
    if _afk_with_timeout "$AFK_GH_TIMEOUT" gh issue edit "$issue" ${args[@]+"${args[@]}"} >/dev/null 2>&1; then
      log "  afk label #$issue → ${add}${remove:+ (was $remove)}"
    else
      log "  afk labels: gh issue edit #$issue failed (best-effort) — continuing"
    fi
  done < <(printf '%s\n' "$plan")
}

