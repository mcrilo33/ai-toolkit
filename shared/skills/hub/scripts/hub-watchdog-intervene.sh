#!/usr/bin/env bash
# hub-watchdog-intervene.sh -- split out of hub-watchdog.sh (issue #308).
#
# The INTERVENTION + INSTRUMENT half of the tier-2 watchdog: the scripted intervention
# actions (_wd_intervene_*: answer / revive / reconcile / landmark / rearm) with their
# revive-marker + landmark + dead-pane-sweep helpers, the fire/dedup/classify machinery and
# the headless bug-scoper defect-filing, and the autonomy ledger/score + morning report. A
# pure function-definition module sourced by the entry lib hub-watchdog.sh AFTER worktree-lib
# / gate-broker / hub-inject and the entry's own primitives, and BEFORE any function is
# called, so every cross-module helper (and the entry's _wd_run_conditions dispatcher that
# calls into here) resolves at call time. Not run on its own.
set -uo pipefail

_wd_intervene_answer() {   # route to the reasoner/answer lane directly
  local wt="$1" issue="$2"
  # #283 AC4: NEVER inject into a permission dialog — that is the BROKER's lane, with its own
  # classifier, timers and re-answer ceiling. Answering one is how the watchdog ends up servicing a
  # park it does not own (#271) and interrupting a live tool call (#89): on #276 this armed
  # re-answers that churned against a dialog the broker was already clearing. The detector no
  # longer fires on a permission park, so this is the second lock — it guards any direct caller.
  # Gated on `permission` specifically, not on "is answer lane": an UNKNOWN lane keeps the historic
  # behaviour (the detector, not this seam, is where the race-vs-strand call is made).
  if [ "$(_wd_park_lane "$wt" "$issue")" = "permission" ]; then
    _wd_log "deferring answer intervention on #$issue — the park is a permission dialog (broker's lane)"
    return 0
  fi
  # #265 AC4: defer when the supervisor is mid-service on this same park — a second answer here
  # duplicate-injects and races the in-flight answerer (the #89 hazard) + wastes a costly run.
  if _wd_supervisor_servicing "$issue"; then
    _wd_log "deferring answer intervention on #$issue — supervisor is mid-service on this park"
    return 0
  fi
  if [ -n "${HUB_WATCHDOG_ANSWER_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_ANSWER_CMD" hub-watchdog "$wt" "$issue" >/dev/null 2>&1 || true; return 0; fi
  command -v decide_and_act >/dev/null 2>&1 && decide_and_act "$wt" "$issue" >/dev/null 2>&1 || true
}
# _wd_revive_marker <issue> -> the once-per-window revive BUDGET marker (#297). The
# `wd-fire-dedup-` stem is deliberate: _clear_progress_state's wd-fire-dedup-* glob drops it on a
# fresh arm, so the budget gets a per-window lifetime with no edit to that file. It is a BUDGET, not
# a firing — nothing calls `_wd_clear_fired revive`, so the spawn stays spent for the whole window
# even after the condition resolves, mirroring the drain's resumed-<issue> ("a second crash
# escalates to a human", hub-afk.sh's _afk_already_resumed). The dead-pane sweep's narrower
# wd-fire-dedup-dead-pane-* glob does not match this stem.
# The DIRECTORY resolves like _wd_filed_marker's (state-dir first), NOT like _wd_fired_marker's
# (ledger-dir first): the only glob that clears this lives in _clear_progress_state and looks solely
# in _afk_state_dir, so minting the marker beside a relocated HUB_WATCHDOG_LEDGER would strand it
# past every future arm and leave that spoke permanently un-revivable. A budget that outlives its
# window is a worse failure than one cleared early — the ledger firing is what a human reads either
# way, but a stranded budget silently disables the whole lane.
_wd_revive_marker() {
  local dir
  if command -v _afk_state_dir >/dev/null 2>&1; then dir="$(_afk_state_dir)"; else dir="$(_wd_common_dir)"; fi
  printf '%s\n' "$dir/wd-fire-dedup-revive-$1"
}
_wd_already_revived() { [ -f "$(_wd_revive_marker "$1")" ]; }

# _wd_mark_revived <issue> <pid> -> record `<ts>\t<pid>` for this window's revive, non-zero when the
# record could NOT be written. The caller must then refuse to spawn: an unrecordable budget is an
# unbounded one, and this lane's failure directions are asymmetric — every other miss costs one
# deferred revive, while this one costs the #297 spawn storm (a fresh headless claude every tick,
# forever). Fail closed. The pid is the liveness half: a revive that orphans a headless run leaves
# an operator something to inspect and kill instead of hunting stray processes by hand.
_wd_mark_revived() {
  local m; m="$(_wd_revive_marker "$1")"
  mkdir -p "$(dirname "$m")" 2>/dev/null || true
  printf '%s\t%s\n' "$(_wd_now)" "${2:--}" > "$m" 2>/dev/null || return 1
  [ -f "$m" ]
}

_wd_intervene_revive() {   # claude --continue revive in the worktree
  local wt="$1" issue="$2" pid=""
  # #290 AC3: NEVER resume a session into a worktree that is finished or being torn down. On #284
  # this launched `nohup claude --continue` inside a worktree the land removed seconds later — a
  # headless run against a vanishing cwd. The detector's done-epoch guard already stops the
  # dispatcher path; this is the second lock, for any DIRECT caller (mirroring the permission-lane
  # re-check in _wd_intervene_answer). Reading the epoch live is right here: a direct caller has not
  # necessarily run the slot_state that would clear it.
  if [ -n "$(_wd_done_epoch "$issue")" ]; then
    _wd_log "deferring revive on #$issue — the spoke is done-stamped (terminal, not a hang)"
    return 0
  fi
  if _wd_land_in_flight "$issue" "$(_wd_now)"; then
    _wd_log "deferring revive on #$issue — a land is in flight (its teardown is removing the worktree)"
    return 0
  fi
  # #297: ONE revive per issue per armed window. The dedup marker in _wd_fire gates only the ledger
  # append, so before this the intervention re-ran every tick the condition held — and since no
  # revive advances the epoch the detector measures, and a headless claude creates no pane, the
  # condition held forever: a fresh run per minute, dozens concurrent. A second crash is now a
  # human's call, exactly as it is for the drain's own resume lane.
  #
  # NOT paired with a "the drain is working this issue" defer keyed on _wd_last_action: that record
  # is the drain's LAST action, not its CURRENT one, and is never cleared mid-window — so the
  # drain's own give-up label (`warn-park #<issue>`) would read as "busy here" and disable this lane
  # for the rest of the window, on exactly the abandoned spoke tier-2 exists to catch. The narrow
  # drain-resume overlap this would have covered is bounded to a single extra run by the budget
  # below; _wd_land_in_flight (above) still covers the mid-land race, on a freshness-bounded signal.
  if _wd_already_revived "$issue"; then
    _wd_log "deferring revive on #$issue — this window's revive budget is already spent"
    return 0
  fi
  # Claim the budget BEFORE launching, and refuse to launch if it cannot be recorded: the record is
  # the only thing bounding this lane, so an unwritable state dir must cost a revive, not restore
  # the spawn storm. The seam owns its whole launch (tests + operator overrides), so it claims here.
  if [ -n "${HUB_WATCHDOG_REVIVE_CMD:-}" ]; then
    _wd_mark_revived "$issue" "-" || { _wd_log "refusing revive on #$issue — could not record the revive budget"; return 0; }
    bash -c "$HUB_WATCHDOG_REVIVE_CMD" hub-watchdog "$wt" "$issue" >/dev/null 2>&1 || true
    return 0
  fi
  command -v claude >/dev/null 2>&1 || return 0
  # A worktree already torn down cannot be revived into. Checked BEFORE the claim: the spawn below
  # is async, so this is the one launch failure observable in time to keep the budget unspent.
  [ -d "$wt" ] || { _wd_log "deferring revive on #$issue — worktree $wt is gone"; return 0; }
  _wd_mark_revived "$issue" pending \
    || { _wd_log "refusing revive on #$issue — could not record the revive budget"; return 0; }
  # `exec` so the backgrounded subshell BECOMES claude: $! is then the revived run's own pid, not a
  # short-lived wrapper's — the pid recorded below has to be the one an operator can kill. The
  # subshell keeps the cd off the caller's cwd.
  # TRADE-OFF: a claude that dies at startup still spends the window's budget, where hub-afk's
  # resume_spoke retries (it launches a tmux window and can read the failure synchronously; we
  # detach a headless run and cannot). Bounded-without-retry is the deliberate choice: the ledger
  # firing + filed defect that precede this call are what put a human on the spoke, and retry-per-
  # tick without a backoff is the defect being fixed. The `pending` claim above stands if we die here.
  ( cd "$wt" 2>/dev/null && exec nohup claude --continue >/dev/null 2>&1 ) &
  pid=$!
  _wd_mark_revived "$issue" "$pid" || true
}
_wd_intervene_reconcile() {  # clear the stale blocked/ marker (local + remote)
  local wt="$1" issue="$2"
  if [ -n "${HUB_WATCHDOG_RECONCILE_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_RECONCILE_CMD" hub-watchdog "$wt" "$issue" >/dev/null 2>&1 || true; return 0; fi
  git -C "$wt" tag -d "blocked/$issue" >/dev/null 2>&1 || true
  git -C "$wt" push origin ":refs/tags/blocked/$issue" >/dev/null 2>&1 || true
}
# ESCALATE-ONLY (#251 final ruling): the watchdog NEVER lands — a tier-2 loop must not ship to
# main ("hub lands, never self-land"). It raises a human land marker (a needs-human-land/<issue>
# tag) so a person lands it; the defect is filed either way (_wd_fire).
_wd_intervene_landmark() {
  local wt="$1" issue="$2"
  if [ -n "${HUB_WATCHDOG_LANDMARK_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_LANDMARK_CMD" hub-watchdog "$wt" "$issue" >/dev/null 2>&1 || true; return 0; fi
  git -C "$wt" tag -f "needs-human-land/$issue" >/dev/null 2>&1 || true
}

# _wd_landmark_repo -> a git checkout whose shared ref store holds the needs-human-land/<issue>
# tags. The per-spoke worktree is gone after a land, so the sweep reads the hub toplevel
# (captured at startup); HUB_WATCHDOG_LANDMARK_REPO overrides (tests point it at a scratch repo).
_wd_landmark_repo() { printf '%s\n' "${HUB_WATCHDOG_LANDMARK_REPO:-$_WD_TOPLEVEL}"; }

# _wd_clear_landed_landmarks -> self-clear the escalation (#263). A needs-human-land/<issue>
# raised by condition 4 dangles after the drain lands the branch on its very next tick — neither
# auto_land's success path nor reconcile_markers removes it, so a human is pointed at
# already-shipped work. Each tick, drop every needs-human-land/<issue> whose issue is CLOSED (a
# closed issue was landed): delete the tag local + remote (mirrors _wd_intervene_reconcile). A
# still-open issue keeps its tag — a human genuinely still owes that land. Best-effort throughout.
_wd_clear_landed_landmarks() {
  local repo tag issue
  repo="$(_wd_landmark_repo)"
  [ -n "$repo" ] || return 0
  while IFS= read -r tag; do
    [ -n "$tag" ] || continue
    issue="${tag#needs-human-land/}"
    case "$issue" in '' | *[!0-9]*) continue ;; esac
    _wd_issue_open "$issue" && continue          # still open ⇒ a human still owes the land
    git -C "$repo" tag -d "$tag" >/dev/null 2>&1 || true
    git -C "$repo" push origin ":refs/tags/$tag" >/dev/null 2>&1 || true
    # The landed issue is gone from the in-flight loop, so the dispatcher's else-clear never runs
    # for it — clear its condition-4 firing markers here so the autonomy score re-arms (#263/#285).
    _wd_clear_fired auto-land-skipped "$issue"
    _wd_clear_fired conflicted-land "$issue"
    _wd_clear_fired accept-unsigned "$issue"   # #292: same lane, same landmark → same re-arm
    _wd_log "cleared resolved landmark $tag (issue #$issue closed/landed)"
  done < <(git -C "$repo" tag -l 'needs-human-land/*' 2>/dev/null)
}
# _wd_sweep_dead_pane_markers <in-flight issues> -> drop every dangling wd-fire-dedup-dead-pane-<N>
# (#290 AC5). Condition 2 raises no needs-human-land tag, so _wd_clear_landed_landmarks never
# revisits its firings; and the dispatcher's else-clear runs ONLY for in-flight worktrees, which a
# landed issue no longer has — so its marker dangles for the rest of the run and a genuine later
# recurrence would stay deduped into silence. Mirrors the landmark sweep, including its fail-safe:
# _wd_issue_open reads an ambiguous state (gh down, empty query) as OPEN, so an outage never
# mass-clears live markers.
# <in-flight issues> is this tick's space-separated issue list; those are the DISPATCHER's to clear
# (their detector may still be firing, and re-arming the dedup mid-fire would let one unresolved
# condition double-count in the ledger, #263). Scoped to the dead-pane stem on purpose: the other
# conditions' markers dangle the same way, but auto-land-skipped/conflicted-land are already swept
# by the landmark sweep, and widening this glob would re-arm park markers whose detector can still
# fire on a closed issue. Best-effort throughout.
_wd_sweep_dead_pane_markers() {
  local inflight=" ${1:-} " dir f issue
  dir="$(dirname "$(_wd_ledger_file)")"
  [ -d "$dir" ] || return 0
  for f in "$dir"/wd-fire-dedup-dead-pane-*; do
    [ -e "$f" ] || continue
    issue="${f##*-}"
    case "$issue" in '' | *[!0-9]*) continue ;; esac    # never resolve a non-numeric stem as an issue
    # Space-padded on BOTH sides (the list, above, and the pattern): a bare *"$issue"* would let
    # #4 match an in-flight list containing 14 or 284 and silently skip a real dangling marker.
    case "$inflight" in *" $issue "*) continue ;; esac  # still in flight ⇒ the dispatcher's to clear
    _wd_issue_open "$issue" && continue                 # still open ⇒ a real unresolved condition
    _wd_clear_fired dead-pane "$issue"
    _wd_log "cleared dangling dead-pane firing marker for landed/closed #$issue"
  done
}

_wd_intervene_rearm() {   # re-arm the crashed drain (self-update aware via hub-afk --reconcile)
  if [ -n "${HUB_WATCHDOG_REARM_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_REARM_CMD" hub-watchdog >/dev/null 2>&1 || true; return 0; fi
  local afk=""
  command -v _afk_find_script >/dev/null 2>&1 && afk="$(_afk_find_script "${HUB_WATCHDOG_AFK_BIN:-}" hub-afk.sh || true)"
  [ -n "$afk" ] && bash "$afk" --reconcile >/dev/null 2>&1 || true
}

# --- the intervention-ledger + firing hook ------------------------------------
# _wd_ledger_file -> the per-run intervention-ledger (one JSONL firing per line). Under the
# drain's state dir so hub-status / the morning report find it; HUB_WATCHDOG_LEDGER overrides.
_wd_ledger_file() {
  if [ -n "${HUB_WATCHDOG_LEDGER:-}" ]; then printf '%s\n' "$HUB_WATCHDOG_LEDGER"; return; fi
  local dir
  if command -v _afk_state_dir >/dev/null 2>&1; then dir="$(_afk_state_dir)"; else dir="$(_wd_common_dir)"; fi
  printf '%s\n' "$dir/intervention-ledger.jsonl"
}

# _wd_json_escape <str> -> minimal JSON string-body escape (defer to the broker's when present so
# the ledger and the #241 decision-journal never diverge on escaping).
_wd_json_escape() {
  if command -v _broker_json_escape >/dev/null 2>&1; then _broker_json_escape "$1"; return; fi
  local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '%s' "$s"
}

# --- classify + file the defect (the instrument, issue #251) ------------------
# Every firing is classified {afk-defect | novel-decision} so a genuine, first-of-its-kind
# human decision (the reasoner CORRECTLY escalating a real judgment call) is not mis-filed as an
# afk bug. The 5 conditions are drain shortfalls ⇒ afk-defect by default; a park-unanswered whose
# spoke the reasoner deliberately escalated (a blocked/ marker exists) is a novel-decision. The
# HUB_WATCHDOG_CLASSIFY_CMD seam overrides the whole decision (echo the class).
: "${HUB_WATCHDOG_AFK_DEFECT_LABEL:=afk-defect}"

# _wd_blocked_tag_epoch <wt> <issue> -> when blocked/<issue> was raised, or empty. `creatordate`
# reads the tagger date of an ANNOTATED tag (what spoke-ready.sh actually emits: `git tag -f -a`)
# and falls back to the commit date for a lightweight one, so both shapes answer.
_wd_blocked_tag_epoch() {
  local wt="$1" issue="$2" ts
  ts="$(git -C "$wt" for-each-ref --format='%(creatordate:unix)' "refs/tags/blocked/${issue}" 2>/dev/null)"
  case "$ts" in '' | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "$ts"
}

# _wd_escalation_is_live <wt> <issue> -> true when the blocked/<issue> tag at the tip belongs to the
# park episode CURRENTLY pending, rather than an older, already-answered one.
# Why this is not just "the tag is at the tip": a blocked tag is only ever cleared by a later COMMIT
# (both _clear_stale_blocked_marker and _wd_intervene_reconcile gate on the tag being a STRICT
# ancestor of the tip). A human answering clears nothing; the spoke resuming clears nothing. So a
# spoke that escalates, gets answered, resumes and re-parks on a NEW question — all before its first
# commit, the common shape, since escalations usually precede any RED/GREEN — would carry its old
# tag at the tip forever and have EVERY later park classified novel-decision. That silences real
# drain defects and inflates the #251 autonomy score: the dangerous direction, since the score's
# whole purpose is to be honest about afk's shortfalls.
# The park onset names the episode actually pending (stamp-once, re-stamped when the park's context
# changes — #283). An onset strictly NEWER than the tag means the pending park began after the
# escalation, so this is a different question and its non-answer is a real defect. Unmeasurable
# either side ⇒ trust the tag (the historic reading); ties ⇒ live, since the escalation is stamped
# during the episode it belongs to.
_wd_escalation_is_live() {
  local wt="$1" issue="$2" tag_ts onset
  tag_ts="$(_wd_blocked_tag_epoch "$wt" "$issue")"
  case "$tag_ts" in '' | *[!0-9]*) return 0 ;; esac
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  case "$onset" in '' | *[!0-9]*) return 0 ;; esac
  [ "$onset" -gt "$tag_ts" ] && return 1
  return 0
}

# _wd_classify <condition> <issue> [wt] -> the firing's class. <wt> is optional: supervisor-dead
# has no worktree, and a direct caller may omit it (the tag check is then simply skipped).
_wd_classify() {
  local condition="$1" issue="$2" wt="${3:-}"
  if [ -n "${HUB_WATCHDOG_CLASSIFY_CMD:-}" ]; then
    bash -c "$HUB_WATCHDOG_CLASSIFY_CMD" hub-watchdog "$condition" "$issue" "$wt" 2>/dev/null; return
  fi
  case "$condition" in
    accept-unsigned)
      # #292: accept/<N> at the tip is the drain behaving CORRECTLY — spoke-ready's human-eyeball
      # terminal, which auto_land deliberately never lands ("accept/ awaits a human sign-off"). The
      # wait is a by-design human decision, not a drain shortfall, so it must not auto-file a bug
      # against afk (which spawned a fresh bug-scoper per accept-spoke per run — this issue's own
      # provenance). The escalation still ledgers and still raises the landmark; only the CLASS
      # changes, and only FILING is class-gated (_wd_fire).
      # It does NOT spare the #251 autonomy score, contrary to what #292 assumed: _wd_autonomy_score
      # counts _wd_intervention_count (every ledger line), not the class-filtered _wd_defect_count,
      # so a novel-decision docks the score exactly like a defect. That is pre-existing and shared
      # with the park-unanswered escape hatch — _wd_report deliberately prints `interventions` and
      # `defects_filed` as separate figures — so making the score class-aware is a change to #251's
      # instrument, not something to smuggle in here. Tracked separately.
      printf 'novel-decision\n'; return ;;
    park-unanswered)
      # A deliberate escalation is a real human call, not an afk bug. TWO signals say so, and only
      # the second was checked before (#297): the blocked/<issue> TAG at the spoke's tip — what
      # spoke-ready actually emits, and the signal the dispatcher already trusts in
      # _wd_detect_mergeable_skipped — and the durable local record, which gate-broker-markers.sh
      # writes ONLY when that tag's push FAILS (the #109 fallback). Reading the record alone meant
      # the COMMON case (the push succeeded, so no file exists) was misfiled as an afk-defect: a
      # bogus auto-filed bug against afk, and the #251 autonomy score docked for the reasoner
      # behaving correctly.
      # Two bounds keep this from over-silencing, both in the direction that MATTERS (a wrong
      # novel-decision hides a real defect and flatters the autonomy score):
      #   * AT-TIP, not merely present — a tag the spoke has committed past is stale (#103), and
      #     live state wins, so that firing IS a real defect;
      #   * and the escalation must still be the LIVE one (see _wd_escalation_is_live).
      # NOT applied to park-undeliverable: that tag is emitted BY a delivery failure (gate-broker's
      # _escalate_blocked when the inject cannot be verified), so reading it as "a human call" would
      # silence exactly the defect class #288 AC3 added that condition to surface — the drain
      # failing to deliver an answer is afk's shortfall, not a novel human decision, and it must
      # keep filing. The durable record is skipped there for the identical reason: same writer.
      if [ -n "$wt" ] && _wd_tag_at_tip "$wt" blocked "$issue" && _wd_escalation_is_live "$wt" "$issue"; then
        printf 'novel-decision\n'; return
      fi
      if command -v _afk_blocked_record >/dev/null 2>&1 && [ -f "$(_afk_blocked_record "$issue" 2>/dev/null)" ]; then
        printf 'novel-decision\n'; return
      fi ;;
  esac
  printf 'afk-defect\n'
}

# _wd_filed_marker <condition> <issue> -> the per-run dedup marker (one file per condition+issue)
# so a persistent condition files ONE defect, not one per tick.
_wd_filed_marker() {
  local dir
  if command -v _afk_state_dir >/dev/null 2>&1; then dir="$(_afk_state_dir)"; else dir="$(_wd_common_dir)"; fi
  printf '%s\n' "$dir/wd-filed-$1-$2"
}

# _wd_fired_marker <condition> <issue> -> the per-run FIRING-dedup marker. NOTE: distinct from
# _wd_filed_marker above (near-homonym) — that one dedups the defect FILING (`wd-filed-`), this
# one dedups the ledger FIRING (`wd-fire-dedup-`, a deliberately non-colliding stem so no glob
# conflates the two families). One firing per condition+issue while unresolved: _wd_fire skips
# the ledger append when this exists, so a persistent condition (or an in-flight land racing
# condition 4) logs ONE intervention, not one per tick — the #251 autonomy score is not
# double-penalized (#263). Co-located with the ledger so it inherits the ledger's dir + test
# isolation (HUB_WATCHDOG_LEDGER points at tmp). Per-window state: cleared when the condition
# resolves, and on a fresh drain arm (_clear_progress_state) so it never leaks across windows.
_wd_fired_marker() {
  local dir; dir="$(dirname "$(_wd_ledger_file)")"
  printf '%s\n' "$dir/wd-fire-dedup-$1-$2"
}
_wd_clear_fired() { rm -f "$(_wd_fired_marker "$1" "$2")" 2>/dev/null || true; }

# _wd_seed_afk_defect_label -> create the afk-defect label once per run (a marker dedups). A red
# defect color matching the repo's `bug` convention; best-effort. HUB_WATCHDOG_LABEL_CMD overrides.
_wd_seed_afk_defect_label() {
  local marker
  marker="$(_wd_filed_marker label seeded)"
  [ -f "$marker" ] && return 0
  if [ -n "${HUB_WATCHDOG_LABEL_CMD:-}" ]; then
    bash -c "$HUB_WATCHDOG_LABEL_CMD" hub-watchdog "$HUB_WATCHDOG_AFK_DEFECT_LABEL" >/dev/null 2>&1 || true
  elif command -v gh >/dev/null 2>&1; then
    gh label create "$HUB_WATCHDOG_AFK_DEFECT_LABEL" --color d73a4a \
      --description "A watchdog-detected afk drain shortfall (issue #251)" --force >/dev/null 2>&1 || true
  fi
  mkdir -p "$(dirname "$marker")" 2>/dev/null || true
  : > "$marker" 2>/dev/null || true
}

# _wd_open_defect_exists <condition> <issue> -> true when an OPEN afk-defect issue already covers
# this firing, so we append/skip instead of filing a duplicate. Searches by the label + the
# condition slug + the source issue number. gh unavailable reads as "no dup" (fall through to
# file); HUB_WATCHDOG_DEDUP_CMD overrides (echo a nonempty match ⇒ exists).
_wd_open_defect_exists() {
  local condition="$1" issue="$2" hits
  if [ -n "${HUB_WATCHDOG_DEDUP_CMD:-}" ]; then
    hits="$(bash -c "$HUB_WATCHDOG_DEDUP_CMD" hub-watchdog "$condition" "$issue" 2>/dev/null)"
    [ -n "$hits" ]; return
  fi
  command -v gh >/dev/null 2>&1 || return 1
  hits="$(gh issue list --state open --label "$HUB_WATCHDOG_AFK_DEFECT_LABEL" \
    --search "$condition #$issue" --json number -q '.[].number' 2>/dev/null)"
  [ -n "$hits" ]
}

# _wd_file_defect <condition> <issue> <reason> -> file (or dedup) the afk-defect via a headless
# bug-scoper on the hub-agent trackable surface. Gated by HUB_WATCHDOG_FILE (default on; the tests
# default it off). Per-run + open-issue deduped. HUB_WATCHDOG_SCOPER_CMD is the dispatch seam.
_wd_file_defect() {
  local condition="$1" issue="$2" reason="$3" marker
  [ "${HUB_WATCHDOG_FILE:-1}" = "1" ] || return 0
  marker="$(_wd_filed_marker "$condition" "$issue")"
  [ -f "$marker" ] && return 0                              # already filed this run
  if _wd_open_defect_exists "$condition" "$issue"; then     # an open afk-defect already covers it
    _wd_log "defect for [$condition] #$issue already open — not duplicating"
    mkdir -p "$(dirname "$marker")" 2>/dev/null || true; : > "$marker" 2>/dev/null || true
    return 0
  fi
  _wd_seed_afk_defect_label
  local prompt="afk drain shortfall detected by hub-watchdog: [$condition] on #$issue — $reason. \
Investigate why the /afk drain did not self-handle this, derive the Scope:/Gate: footer, and file \
ONE afk-defect issue (label $HUB_WATCHDOG_AFK_DEFECT_LABEL). Dedup against open issues first."
  if [ -n "${HUB_WATCHDOG_SCOPER_CMD:-}" ]; then
    bash -c "$HUB_WATCHDOG_SCOPER_CMD" hub-watchdog "$condition" "$issue" "$reason" >/dev/null 2>&1 || true
  else
    local ha=""
    command -v _afk_find_script >/dev/null 2>&1 && ha="$(_afk_find_script "${HUB_WATCHDOG_HUB_AGENT:-}" hub-agent.sh || true)"
    if [ -n "$ha" ]; then
      bash "$ha" "scope-${condition}-${issue}" --purpose "afk-defect: $condition #$issue" \
        -- claude -p "$prompt" >/dev/null 2>&1 || true
    fi
  fi
  mkdir -p "$(dirname "$marker")" 2>/dev/null || true; : > "$marker" 2>/dev/null || true
  _wd_log "filed afk-defect for [$condition] #$issue via headless bug-scoper"
}

# _wd_fire <condition> <issue> <reason> -> record ONE intervention firing: classify it, append a
# JSONL line to the intervention-ledger (with the class), log it, and — for an afk-defect — file
# it via the headless bug-scoper (deduped). Every firing is a bug report against afk; subtask 5's
# autonomy score counts these lines. Best-effort.
# <wt> ($4) is optional and threaded through to _wd_classify, which needs it to read the
# blocked/<issue> tag at the spoke's tip (#297). supervisor-dead has no worktree and passes none.
_wd_fire() {
  local condition="$1" issue="$2" reason="$3" wt="${4:-}" lf klass marker
  marker="$(_wd_fired_marker "$condition" "$issue")"
  [ -f "$marker" ] && return 0   # already fired this unresolved occurrence — dedupe ledger + file
  klass="$(_wd_classify "$condition" "$issue" "$wt")"
  case "$klass" in afk-defect | novel-decision) ;; *) klass="afk-defect" ;; esac
  lf="$(_wd_ledger_file)"
  mkdir -p "$(dirname "$lf")" 2>/dev/null || true
  printf '{"ts":%s,"condition":"%s","issue":"%s","class":"%s","reason":"%s"}\n' \
    "$(_wd_now)" "$(_wd_json_escape "$condition")" "$(_wd_json_escape "$issue")" \
    "$(_wd_json_escape "$klass")" "$(_wd_json_escape "$reason")" >> "$lf" 2>/dev/null || true
  _wd_log "FIRING [$condition] #${issue} (${klass}) — ${reason}"
  [ "$klass" = "afk-defect" ] && _wd_file_defect "$condition" "$issue" "$reason"
  mkdir -p "$(dirname "$marker")" 2>/dev/null || true; : > "$marker" 2>/dev/null || true
  return 0
}

# --- the autonomy score + morning report (issue #251) -------------------------
# The whole point: a run with ZERO firings means afk was autonomous for that workload. The
# score = 1 − (interventions / spokes serviced) makes that measurable — 1.0 is the pass
# criterion for "afk autonomous on this backlog". The report is the morning artifact:
# interventions taken, defects filed, and the score, to stdout + a best-effort telemetry span.

# _wd_intervention_count -> firings this run = lines in the intervention-ledger (0 when absent).
# `grep -c` on an existing file with zero matches prints "0" AND exits 1, so a naive
# `[ -f ] && grep -c || echo 0` double-prints "0\n0" and corrupts the report — capture instead.
_wd_intervention_count() {
  local lf n; lf="$(_wd_ledger_file)"
  [ -f "$lf" ] || { printf '0\n'; return; }
  n="$(grep -c . "$lf" 2>/dev/null)" || true
  printf '%s\n' "${n:-0}"
}

# _wd_defect_count -> afk-defect firings (the drain-shortfall subset; novel-decisions excluded).
_wd_defect_count() {
  local lf n; lf="$(_wd_ledger_file)"
  [ -f "$lf" ] || { printf '0\n'; return; }
  n="$(grep -c '"class":"afk-defect"' "$lf" 2>/dev/null)" || true
  printf '%s\n' "${n:-0}"
}

# _wd_spokes_serviced -> distinct spokes the drain dispatched this run = the dispatch-<issue>.epoch
# files in the drain state dir (the SAME record auto_land keys on). 0 when none.
_wd_spokes_serviced() {
  local dir n=0 f
  if command -v _afk_state_dir >/dev/null 2>&1; then dir="$(_afk_state_dir)"; else dir="$(_wd_common_dir)"; fi
  for f in "$dir"/dispatch-*.epoch; do [ -e "$f" ] && n=$((n + 1)); done
  printf '%s\n' "$n"
}

# _wd_autonomy_score -> 1 − (interventions / spokes), to 3 decimals. No spokes ⇒ 1.000 when there
# were also no interventions (nothing happened, trivially autonomous), else 0.000 (interventions
# with no serviced spoke — e.g. a bare supervisor-death — is pure non-autonomy).
_wd_autonomy_score() {
  local interventions spokes
  interventions="$(_wd_intervention_count)"
  spokes="$(_wd_spokes_serviced)"
  # LC_ALL=C: a non-C host locale makes awk's %.3f emit a comma decimal (1,000), which breaks
  # every downstream parse of the score — the recurring locale trap in this repo.
  LC_ALL=C awk -v i="$interventions" -v s="$spokes" 'BEGIN {
    if (s == 0) { printf "%.3f\n", (i == 0 ? 1 : 0); exit }
    v = 1 - (i / s); if (v < 0) v = 0; printf "%.3f\n", v
  }'
}

# _wd_report -> the morning artifact: one summary line to stdout + a best-effort telemetry span
# (kind=agent, name hub-watchdog:report) so the score lands in the observability surface too.
_wd_report() {
  local interventions defects spokes score
  interventions="$(_wd_intervention_count)"
  defects="$(_wd_defect_count)"
  spokes="$(_wd_spokes_serviced)"
  score="$(_wd_autonomy_score)"
  printf 'hub-watchdog: interventions=%s defects_filed=%s spokes_serviced=%s autonomy_score=%s\n' \
    "$interventions" "$defects" "$spokes" "$score"
  if [ -z "${HUB_WATCHDOG_NO_TELEMETRY:-}" ] && command -v telemetry_emit_span >/dev/null 2>&1; then
    telemetry_emit_span --kind agent --name "hub-watchdog:report" \
      --attr "autonomy_score=$score" --attr "interventions=$interventions" \
      --attr "spokes_serviced=$spokes" >/dev/null 2>&1 || true
  fi
}

