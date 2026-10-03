#!/usr/bin/env bash
# coordinator.sh [--run R] [--answer auto|human] [--cap N] [--until HH:MM] [--drain] [--status]: the Run loop (06 section 4,
# "The coordinator"). One foreground process in an Orca terminal on the main checkout, the single consumer of ONE Run
# (--run re-binds it with run-use; none creates one). Loop: fill slots to --cap (dispatch.sh) -> check --wait -> route each message
# -> ack the delivery. question: answer.sh (auto) or the human, who types the answer on this terminal; worker_done succeeded: land.sh
# --review inline (lands are serialized by construction); a rejected review, red CI or a conflict goes back to the SAME worker for a
# bounded number of rounds, then the issue is labelled blocked; worker_done failed / escalation: blocked. Every 10th empty wait: a sweep.
# Stops at --until, or with --drain when nothing is ready and no worker is live. No state files: rounds, workers and issues are read
# back from Orca (worker-list, worktree list) and GitHub. Sub-commands are overridable for tests: DISPATCH_CMD LAND_CMD ANSWER_CMD NOTIFY_CMD.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
load_env
run="${RUN:-}"; answer=auto; cap="${CONCURRENCY_CAP:-3}"; until=""; drain=0; status=0; reply=0; reply_id=""; reply_body=""
while [ $# -gt 0 ]; do
  case "$1" in
    --run) run="${2:-}"; shift ;; --answer) answer="${2:-}"; shift ;;
    --cap) cap="${2:-}"; shift ;; --until) until="${2:-}"; shift ;;
    --drain) drain=1 ;; --status) status=1 ;;
    --reply) reply=1; reply_id="${2:-}"; reply_body="${3:-}"; shift $(($# > 2 ? 2 : $# - 1)) ;;
    *) usage_exit "usage: coordinator.sh [--run R] [--answer auto|human] [--cap N] [--until HH:MM] [--drain] [--status] | --run R --reply <msg-id> approve|'revise: ...'" ;;
  esac; shift
done
case "$answer" in auto | human) ;; *) usage_exit "--answer takes auto or human" ;; esac
case "$cap$until" in *[!0-9:]* | '') usage_exit "--cap takes a number and --until HH:MM" ;; esac
# The only state v2 keeps: queued human replies, one file per question (name = message id, content = the reply line), in a 0700 dir
# outside every worktree. Only the Run's bound terminal can reply (Orca attests terminal identity), so a human queues with --reply
# from ANY terminal and the loop sends it at its next wake. AITK_STATE_DIR replaces ~/.ai-toolkit/coordinator; <run-id>/replies is appended.
rdir() { echo "${AITK_STATE_DIR:-$HOME/.ai-toolkit/coordinator}/$run/replies"; }
if [ "$reply" = 1 ]; then
  [ -n "$run" ] || usage_exit "--reply needs --run <run-id>"
  case "$reply_id" in '' | *[!A-Za-z0-9_-]*) usage_exit "bad message id '$reply_id'" ;; esac
  case "$reply_body" in
    approve) ;;
    revise:*[![:space:]]*) reply_body="${reply_body#revise:}"; reply_body="revise: ${reply_body#"${reply_body%%[![:space:]]*}"}" ;;
    *) usage_exit "the reply is 'approve' or 'revise: <change>'" ;;
  esac
  d="$(rdir)"; (umask 077; mkdir -p "$d"); chmod 700 "$d"
  printf '%s\n' "$reply_body" > "$d/.$reply_id.tmp" && mv "$d/.$reply_id.tmp" "$d/$reply_id"
  echo "reply to $reply_id queued: the coordinator sends it at its next wake"; exit 0
fi
DISPATCH_CMD="${DISPATCH_CMD:-$here/dispatch.sh}"; LAND_CMD="${LAND_CMD:-$here/land.sh}"; ANSWER_CMD="${ANSWER_CMD:-$here/answer.sh}"
cd "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"

log() { printf '%s coordinator: %s\n' "$(date +%H:%M:%S)" "$*"; }
notify() {   # one desktop notification (NOTIFY_CMD replaces it in tests); never fatal
  log "$1"
  if [ -n "${NOTIFY_CMD:-}" ]; then "$NOTIFY_CMD" "$1" || true
  else osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title "ai-toolkit"' -e 'end run' "$1" > /dev/null 2>&1 \
    || warn "desktop notification failed (osascript): the reply line is in this log and in the issue comment"; fi
}
comment() { gh issue comment "$1" -b "$2" > /dev/null || warn "cannot comment on #$1"; }
wl() {   # every page of the Run's workers (newest first, 100 a page), in the shape of one reply; live = dispatchStatus "dispatched"
  local cur="" out all="[]"
  while :; do
    out="$(orca_json orchestration worker-list --run "$run" --limit 100 ${cur:+--cursor "$cur"})" || return 1
    all="$(jq -c --argjson o "$out" '. + $o.result.workers' <<< "$all")"; cur="$(jq -r '.result.page.nextCursor // empty' <<< "$out")"
    [ -n "$cur" ] || break
  done
  jq -nc --argjson w "$all" '{result: {workers: $w}}'
}
pj() { jq -r --arg k "$2" '(.payload // "{}" | if type == "string" then fromjson else . end)[$k] // empty' <<< "$1"; }   # payload field of a message
question_of() { local q; q="$(pj "$1" question)"; [ -n "$q" ] || q="$(jq -r '.body // ""' <<< "$1")"; printf '%s' "$q"; }
show_q() { printf '%s\n' "$1" | fold -s -w 100 | head -n 25 | sed 's/^/  | /' || true; }   # the question as the human must read it: wrapped, at most 25 lines
gate_flag() {   # $1 worktree path, $2 message id, $3 question: the stable Orca surfaces of a pending human gate: the worktree comment and a bell on this terminal
  orca_json worktree set --worktree "path:$1" --comment "GATE waiting: $(printf '%s' "$3" | tr '\n' ' ' | cut -c1-80) | reply: coordinator.sh --reply $2 approve" > /dev/null 2>&1 || warn "cannot set the worktree comment"
  printf '\a' > "${COORD_BELL_TTY:-/dev/tty}" 2> /dev/null || true
}
mins() { echo $((10#${1%:*} * 60 + 10#${1#*:})); }
now_min() { mins "${AI_TOOLKIT_NOW:-$(date +%H:%M)}"; }

pending() {   # {open: questions nobody has replied to (waiting for the human), answered: ids with a reply, truncated: the page was full}
  orca_json orchestration inbox --limit 200 --full | jq -c --arg r "$run" '.result.messages as $all | [$all[] | select(.run_id == $r)] as $m
    | {open: [$m[] | select(.type == "question") | select(. as $q | $m | any(.id != $q.id and .thread_id == $q.id) | not)],
       answered: [$m[] | select(.thread_id != null and .thread_id != .id) | .thread_id], truncated: ($all | length >= 200)}'
}
replycmd() { printf 'bash %s --run %s --reply %s approve' "$here/coordinator.sh" "$run" "$1"; }
if [ "$status" = 1 ]; then   # read-only: the Run, its live workers, and the questions nobody has replied to
  [ -n "$run" ] || run="$(orca_json orchestration run-current | jq -r '.result.run.id // empty')"
  log "run: $run"; echo "live workers:"
  wl | jq -r '.result.workers[] | select(.dispatchStatus == "dispatched") | "  \(.dispatchId) \(.resource.worktreeId | sub("^.*::"; "")) \(.projection.liveness.verdict)"'
  echo "pending questions:"
  pending | jq -c '.open[]' | while IFS= read -r m; do id="$(jq -r .id <<< "$m")"; echo "  $id"; show_q "$(question_of "$m")"; echo "    reply: $(replycmd "$id")   (or 'revise: <change>')"; done
  exit 0
fi

: "${ORCA_TERMINAL_HANDLE:?run me from an Orca terminal on the main checkout}"
H="$ORCA_TERMINAL_HANDLE"
if [ -n "$run" ]; then
  out="$(orca_mutate orchestration run-use --id "$run" --from "$H" 2>&1)" || case "$out" in
    *consumer_fenced*) die "Run $run is held by another live terminal (consumer_fenced): stop that coordinator first" ;;
    *) die "run-use failed: $out" ;;
  esac
else
  run="$(orca_mutate orchestration run-create --objective "ai-toolkit coordinator" --from "$H" | jq -r '.result.run.id // empty')"
  [ -n "$run" ] || die "run-create failed"
fi
log "run $run, cap $cap, answer $answer${until:+, until $until}"

# ctx <dispatch>: sets disp task term wtp issue from the worker's Orca row and the issue linked to its worktree.
ctx() {
  local r; disp="$1"
  r="$(wl | jq -c --arg d "$1" '[.result.workers[] | select(.dispatchId == $d)][0] // empty')"
  [ -n "$r" ] || { warn "unknown dispatch '$1'"; return 1; }
  task="$(jq -r '.taskId // empty' <<< "$r")"; term="$(jq -r '.agentTerminalHandle // empty' <<< "$r")"
  wtp="$(jq -r '.resource.worktreeId | sub("^.*::"; "")' <<< "$r")"
  issue="$(orca_json worktree list | jq -r --arg p "$wtp" '[.result.worktrees[] | select(.path == $p) | .linkedIssue // empty][0] // empty')"
  [ -n "$issue" ] || { warn "no issue is linked to $wtp"; return 1; }
}
rounds() { wl | jq --arg p "::$wtp" '[.result.workers[] | select(.resource.worktreeId | endswith($p))] | length - 1'; }   # dispatches so far - 1
release() { [ -z "$1" ] || orca_mutate orchestration worker-release --dispatch "$1" > /dev/null 2>&1 || orca_mutate orchestration worker-stop --dispatch "$1" > /dev/null 2>&1 || true; }
block() {   # $1 = why. Label, comment, notify, free the slot; the worktree stays for the human.
  gh issue edit "$issue" --add-label blocked > /dev/null 2>&1 \
    || { gh label create blocked --color B60205 > /dev/null 2>&1 || true; gh issue edit "$issue" --add-label blocked > /dev/null || warn "cannot label #$issue"; }
  comment "$issue" "blocked: $1"; notify "#$issue blocked: $1"; release "$disp"
}
redispatch() {   # $1 = max rounds, $2 = spec, $3 = why blocked once the rounds are spent. A fresh terminal and a NEW Task (dispatch.sh --address):
  local r out; r="$(rounds)"   # worker-start refuses --task with --spec, and Orca refuses a new task on the idle two-step terminal
  [ "$r" -lt "$1" ] || { block "$3"; return 0; }
  out="$(RUN="$run" "$DISPATCH_CMD" --address "$2" "$issue" 2>&1)" || { warn "re-dispatch: $out"; block "re-dispatch failed"; return 0; }
  orca terminal close --terminal "$term" --json > /dev/null 2>&1 || true   # the old, idle terminal
  comment "$issue" "round $((r + 1)): $2"
}

drain_replies() {   # send the queued human replies as the bound consumer. Dropped: a malformed body, a question seen answered, or (page not full)
  local f id body q pend   # unknown. Kept and retried: a failed send, or an id the full inbox page does not show (it may be older than the page)
  ls "$(rdir)"/* > /dev/null 2>&1 || return 0
  pend="$(pending)" || return 0
  for f in "$(rdir)"/*; do
    [ -f "$f" ] || continue
    id="${f##*/}"; body="$(head -n 1 "$f")"; q="$(jq -c --arg i "$id" '[.open[] | select(.id == $i)][0] // empty' <<< "$pend")"
    case "$body" in
      approve | revise:*[![:space:]]*) ;;
      *) warn "reply to $id dropped: malformed body"; rm -f "$f"; continue ;;
    esac
    if [ -z "$q" ]; then
      if jq -e --arg i "$id" '(.answered | index($i)) != null or (.truncated | not)' <<< "$pend" > /dev/null; then warn "reply to $id dropped: no such unanswered question"
      else warn "reply to $id kept: the inbox page is truncated and does not show that question"; continue; fi
    elif orca_mutate orchestration reply --run "$run" --from "$H" --id "$id" --body "$body" > /dev/null; then
      log "gate $id answered by the human: $body"; if ctx "$(pj "$q" dispatchId)"; then comment "$issue" "Gate answered by the human: $body"; orca_json worktree set --worktree "path:$wtp" --comment "" > /dev/null 2>&1 || warn "cannot clear the worktree comment"; fi
    else warn "reply to $id failed, it stays queued"; continue; fi
    rm -f "$f"
  done
}
on_question() {
  local id q ans body warns
  id="$(jq -r .id <<< "$1")"; q="$(question_of "$1")"
  ctx "$(pj "$1" dispatchId)" || { notify "gate question $id comes from an unknown worker: reply by hand"; return 1; }
  if [ "$answer" = auto ] && ans="$(printf '%s' "$q" | "$ANSWER_CMD" "$wtp")" && body="$(head -n 1 <<< "$ans")" \
    && orca_mutate orchestration reply --run "$run" --from "$H" --id "$id" --body "$body" > /dev/null; then
    log "#$issue gate answered: $body"; warns="$(sed -n '/^WARN:/p' <<< "$ans")"
    [ -z "$warns" ] || { comment "$issue" "Gate answered \"$body\" by answer.sh; please double-check: $warns"; notify "#$issue: $warns"; }
  else   # human mode, no usable answer, or the reply failed: never a blind approve, never waiting: the human queues a reply with --reply
    comment "$issue" "A gate question needs a human (message $id): ${q:0:500} -- Reply from any terminal: $(replycmd "$id")   (or end it with: revise: <change>)"
    show_q "$q"; notify "#$issue: gate question waiting: $(replycmd "$id")"; gate_flag "$wtp" "$id" "$q"
  fi
}
on_done() {
  local out rc=0 branch bl last
  ctx "$(pj "$1" dispatchId)" || return 1
  [ "$(pj "$1" outcome)" = succeeded ] || { block "worker_done failed: $(jq -r '.body // ""' <<< "$1")"; return 0; }
  branch="$(git -C "$wtp" branch --show-current)"
  # No --dispatch: land finds and releases EVERY dispatch of the worktree, earlier address rounds included.
  out="$(RUN="$run" "$LAND_CMD" --review "$issue" 2>&1)" || rc=$?
  printf '%s\n' "$out"; last="$(tail -n 1 <<< "$out")"
  case $rc in
    0) log "#$issue landed" ;;
    3) bl="$(sed -n 's/^BLOCKER: //p' <<< "$out" | awk '{ printf "%s(%d) %s", (NR > 1 ? " " : ""), NR, $0 }')"   # no blocker lines = no usable verdict: nothing the worker could address
       if [ -n "$bl" ]; then redispatch 2 "address: the independent review requested changes. Fix each blocker test-first, push, then send worker_done again. Blockers: $bl" "review still rejects after 2 rounds"
       else block "the independent review gave no usable verdict"; fi ;;
    4 | 5) case "$last" in
        *"timed out"* | *"keeps moving"*) block "$last" ;;
        *) redispatch 1 "address: the land was refused ($last). Fix it on your branch (merge origin/$BASE_BRANCH and resolve conflicts, or fix the failing checks), push, then send worker_done again." "$last" ;;
      esac ;;
    6) RUN="$run" "$LAND_CMD" --cleanup-only --branch "$branch" --tip "$(git rev-parse HEAD)" --dispatch "$disp" "$issue" \
         || block "landed, but cleanup is incomplete (finish with land.sh --cleanup-only $issue); blocked so the still-open issue is not dispatched again" ;;
    *) block "land.sh exited $rc: $last" ;;
  esac
}
handle() {
  case "$(jq -r .type <<< "$1")" in
    question) on_question "$1" ;;
    worker_done) on_done "$1" ;;
    escalation) ctx "$(pj "$1" dispatchId)" && block "escalation: $(jq -r '.body // ""' <<< "$1")" ;;
  esac
}

fill() {   # dispatch ready issues until $cap workers are live; ready=0 once --next finds nothing
  local live n rc i out
  live="$(wl | jq '[.result.workers[] | select(.dispatchStatus == "dispatched")] | length')"; ready=1
  for ((i = live; i < cap; i++)); do
    n="$(RUN="$run" "$DISPATCH_CMD" --next --dry-run)" && rc=0 || rc=$?
    case $rc in 0) ;; 3) ready=0; break ;; *) warn "dispatch --next failed ($rc)"; break ;; esac
    if out="$(RUN="$run" "$DISPATCH_CMD" "$n" 2>&1)"; then log "dispatched #$n"
    else   # may have left a half-made worktree behind: remove it and park the issue
      warn "dispatch #$n failed: $out"; orca_json worktree rm --worktree "issue:$n" --force > /dev/null 2>&1 || true
      issue="$n"; disp=""; block "dispatch failed: $(tail -n 1 <<< "$out")"
    fi
  done
}
sweep() {   # a worker whose process exited without worker_done (and was not relaunched): one relaunch, then blocked
  local d
  for d in $(wl | jq -r '.result.workers as $w | $w[] | select(.dispatchStatus == "dispatched" and .projection.liveness.verdict == "exited")
      | .resource.worktreeId as $p | select([$w[] | select(.resource.worktreeId == $p and .dispatchStatus == "dispatched" and .projection.liveness.verdict != "exited")] | length == 0) | .dispatchId'); do
    ctx "$d" || continue
    if [ "$(rounds)" -lt 1 ]; then
      log "#$issue: worker $d exited without worker_done, relaunching"; RUN="$run" "$DISPATCH_CMD" --retry-of "$d" --task "$task" "$issue" > /dev/null || block "relaunch failed"
    else block "worker exited again without worker_done"; fi
  done
}

tick=0; empties=0; start="$(now_min)"; budget=0; errf="$(mktemp)"; trap 'rm -f "$errf"' EXIT
[ -z "$until" ] || budget=$(((($(mins "$until") - start) + 1440) % 1440))
while :; do
  tick=$((tick + 1))
  if [ -n "$until" ] && [ $((($(now_min) - start + 1440) % 1440)) -ge "$budget" ]; then log "reached $until"; break; fi
  [ -z "${COORD_MAX_TICKS:-}" ] || [ "$tick" -le "$COORD_MAX_TICKS" ] || break
  fill
  if [ "$drain" = 1 ] && [ "$ready" = 0 ] && [ "$(wl | jq '[.result.workers[] | select(.dispatchStatus == "dispatched")] | length')" = 0 ]; then log "drained"; break; fi
  drain_replies
  rc=0; wait_ms="${COORD_WAIT_MS:-300000}"   # 30 s while a question waits for the human: its reply is picked up soon
  [ "$(pending | jq '.open | length')" -eq 0 ] 2> /dev/null || wait_ms="${COORD_WAIT_PENDING_MS:-30000}"
  out="$(orca_json orchestration check --run "$run" --terminal "$H" --wait --types question,worker_done,escalation --timeout-ms "$wait_ms" 2> "$errf")" || rc=$?
  case "$out$(cat "$errf")" in *consumer_fenced*) die "Run $run was taken over by another terminal (consumer_fenced)" ;; esac
  msgs="$(jq -c '.result.messages[]?' <<< "$out" 2> /dev/null)" || msgs=""
  if [ -z "$msgs" ]; then
    empties=$((empties + 1)); [ "$rc" = 0 ] || sleep "${AI_TOOLKIT_POLL:-3}"
    [ $((empties % ${COORD_SWEEP_EVERY:-10})) -ne 0 ] || sweep
    continue
  fi
  # fd 3, not stdin: a handler that reads stdin (claude -p, ssh) must not swallow the rest of the batch.
  while IFS= read -r m <&3; do handle "$m" || warn "message $(jq -r .id <<< "$m") not fully handled"; done 3<<< "$msgs"
  # Always ack, after the whole batch: a replied question is replayed until acked (03 #20).
  orca_json orchestration check --run "$run" --terminal "$H" --ack "$(jq -r '.result.deliveryId // empty' <<< "$out")" > /dev/null || warn "ack failed"
done
