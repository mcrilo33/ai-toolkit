#!/usr/bin/env bash
# dispatch.sh [--run R] (<issue> | --next [--dry-run]): one issue -> one supervised worker (06 section 4 steps 2, 3, 5, 6).
# The Run is explicit (--run or $RUN, never inferred: a coordinator's Run must not be picked up by accident).
# --retry-of D --task T <issue>: relaunch the issue's worker (a new claude-spoke terminal in its worktree) on the same task; Orca re-seeds the spec.
# --address "<spec>" <issue>: same fresh terminal, but a NEW task whose spec is the text (a review/CI round: worker-start refuses --task with --spec,
# and Orca does not accept a new task on an idle two-step terminal: agent_unconfigured).
# Prints one JSON line {issue,dispatch,worktree,terminal} (--dry-run: just the picked number). Exit: 0 dispatched, 1 error, 2 usage, 3 --next: nothing ready.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
load_env
run="${RUN:-}"; n=""; next=0; dry=0; retry_of=""; task=""; address=""; tries="${DISPATCH_TRIES:-60}"; poll="${AI_TOOLKIT_POLL:-3}"
while [ $# -gt 0 ]; do
  case "$1" in
    --run) run="${2:-}"; shift ;;
    --next) next=1 ;;
    --dry-run) dry=1 ;;
    --retry-of) retry_of="${2:-}"; shift ;;
    --task) task="${2:-}"; shift ;;
    --address) address="${2:-}"; shift ;;
    [0-9]*) n="$1" ;;
    *) usage_exit "usage: dispatch.sh [--run R] <issue> | --next" ;;
  esac; shift
done
[ -n "$run" ] || usage_exit "no Run: pass --run or set RUN"
[ "$next" = 1 ] || [ -n "$n" ] || usage_exit "usage: dispatch.sh [--run R] <issue> | --next | --retry-of D --task T <issue>"
[ -z "$retry_of" ] || [ -n "$task" ] || usage_exit "--retry-of needs --task"
: "${ORCA_TERMINAL_HANDLE:?run me from the coordinator Orca terminal}"
main="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
base="origin/$BASE_BRANCH"

# Ready = not hold/blocked, blockers closed, not in flight, Scope disjoint from every in-flight Scope
# (a missing or `*` scope is exclusive and collides with everything); `priority` first, then number.
# shellcheck disable=SC2016
PICK='def scope: [(.body // "") | split("\n")[] | select(test("^\\s*[Ss]cope:"))] | .[-1]
    | if . == null then null else (sub("^\\s*[Ss]cope:"; "") | gsub(","; " ") | [splits(" +")] | map(select(. != "")))
      | if length == 0 or index("*") != null then null else . end end;
  def clash($a; $b): $a == null or $b == null or (($a - ($a - $b)) | length > 0);
  def names: [.labels.nodes[].name];
  (map(select(.number as $x | ($busy | index($x)) != null) | scope)) as $held
  | [.[] | . as $i | scope as $s
      | select(($busy | index($i.number)) == null)
      | select(names | any(. == "hold" or . == "blocked") | not)
      | select([$i.blockedBy.nodes[] | select(.state != "CLOSED")] | length == 0)
      | select($held | all(clash($s; .) | not))
      | {n: $i.number, p: (if (names | index("priority")) == null then 1 else 0 end)}]
  | sort_by([.p, .n]) | (.[0].n // empty)'
pick_next() {
  local nodes busy
  # shellcheck disable=SC2016
  nodes="$(gh api graphql -F owner='{owner}' -F name='{repo}' -F limit=100 --jq '.data.repository.issues.nodes' -f query='
    query($owner:String!, $name:String!, $limit:Int!) { repository(owner:$owner, name:$name) { issues(states: OPEN, first: $limit, orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes { number body labels(first: 20) { nodes { name } } blockedBy(first: 50) { nodes { number state } } } } } }')" || die "gh api graphql failed"
  busy="$(orca_json worktree list --repo "path:$main" | jq -c '[.result.worktrees[].linkedIssue | select(. != null)]')" || die "worktree list failed"
  printf '%s' "$nodes" | jq -r --argjson busy "$busy" "$PICK"
}

seed() {   # $1 = gate: plan = full cycle; none = light lane (no PLAN gate, no in-worker review)
  local s="Read .ai-toolkit/task.md." review=" -> in-spoke code-review"
  [ "$1" = none ] || s="$s Gate: plan. Explore, write the plan, then run your preamble's ask command with the plan as the question and options approve,revise; do not edit code before approve (on revise, amend the plan and ask again)."
  [ "$1" != none ] || review=""
  printf '%s' "$s Cycle per subtask: RED -> GREEN -> REFACTOR${review} -> git push -u origin HEAD. When the acceptance criteria hold: push, then send worker_done --outcome succeeded with a 3-sentence summary. Never merge or push the base branch."
}

start_worker() {   # worker-start with the common flags; $@ = placement flags. Sets $disp.
  local out what=(--task-title "#$n $title" --spec "$(seed "$gate")")
  [ -z "$retry_of" ] || what=(--task "$task" --retry-of "$retry_of")
  [ -z "$address" ] || what=(--task-title "#$n address" --spec "$address")
  out="$(orca_mutate orchestration worker-start --run "$run" --from "$ORCA_TERMINAL_HANDLE" "$@" "${what[@]}" --timeout-ms 120000)" \
    || die "worker-start failed: $(printf '%s' "$out" | jq -c '{state: .result.state, stage: .result.failedStage}' 2> /dev/null)"
  disp="$(printf '%s' "$out" | jq -r '.result.dispatchId // empty')"
}
# --- launch path (06 Q1): DISPATCH_LAUNCH=twostep. After Orca's agentCmdOverrides.claude
# points at bin/claude-spoke, flip the default below to `override` (a single worker-start, no terminal step).
launch_twostep() {
  local o
  o="$(orca_json worktree create --repo "path:$main" --name "$name" --base-branch "$base" --issue "$n" --setup run)" || die "worktree create failed: $o"
  wt="$(printf '%s' "$o" | jq -r '.result.worktree.path // empty')"; [ -n "$wt" ] || die "worktree create returned no path: $o"
  wait_until "$tries" "$poll" test -f "$wt/.ai-toolkit/setup-done" || die "setup did not finish in $wt (see its Orca setup terminal)"
  term="$(spawn_claude "$wt" "$name-agent" "$model" "$effort")"
  start_worker --worktree "path:$wt" --terminal "$term"; sel="path:$wt"
}
launch_retry() {
  wt="$(orca_json worktree show --worktree "issue:$n")" || die "retry: no worktree for issue $n"
  wt="$(printf '%s' "$wt" | jq -r '.result.worktree.path // empty')"; [ -n "$wt" ] || die "retry: no worktree path for issue $n"
  term="$(spawn_claude "$wt" "$name-agent" "$model" "$effort")"
  start_worker --worktree "path:$wt" --terminal "$term"; sel="path:$wt"
}
launch_override() {
  start_worker --worktree new-top-level --repo "path:$main" --name "$name" --base-branch "$base" --agent claude --model "$model" --effort "$effort"
  sel="name:$name"; term=""
  wt="$(orca_json worktree show --worktree "$sel" | jq -r '.result.worktree.path // empty')"
}
# ---

dispatch() {
  local issue body slug
  n="$1"
  issue="$(gh_issue "$n")" || die "cannot read issue $n"
  title="$(printf '%s' "$issue" | jq -r .title)"; body="$(printf '%s' "$issue" | jq -r '.body // ""')"
  gate="$(issue_footer "$body" Gate | tr '[:upper:]' '[:lower:]')"; gate="${gate:-$GATE_DEFAULT}"
  read -r model effort _ <<< "$(issue_footer "$body" Model)"
  model="${model:-$SPOKE_MODEL}"; effort="${effort:-$SPOKE_EFFORT}"
  # The model/effort end up in a shell command typed into a terminal: allow nothing but id characters.
  case "$model$effort" in *[!A-Za-z0-9._-]*) die "issue $n: bad Model footer '$model $effort'" ;; esac
  slug="$(printf '%s' "$title" | LC_ALL=C tr '[:upper:]' '[:lower:]' | LC_ALL=C tr -cs 'a-z0-9' '-' | cut -c1-40 | sed 's/^-*//; s/-*$//')"
  name="$n-${slug:-issue}"
  if [ -n "$retry_of$address" ]; then launch_retry; else "launch_${DISPATCH_LAUNCH:-twostep}"; fi
  orca_json worktree set --worktree "$sel" --issue "$n" --workspace-status in-progress > /dev/null || die "worktree set failed for $sel"
  jq -nc --argjson issue "$n" --arg dispatch "$disp" --arg worktree "$wt" --arg terminal "$term" \
    '{issue: $issue, dispatch: $dispatch, worktree: $worktree, terminal: $terminal}'
}

if [ "$next" = 1 ]; then
  n="$(pick_next)"
  [ -n "$n" ] || { echo "dispatch: nothing ready" >&2; exit 3; }
  [ "$dry" = 0 ] || { echo "$n"; exit 0; }
fi
dispatch "$n"
