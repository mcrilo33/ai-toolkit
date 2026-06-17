#!/usr/bin/env bash
# batch-plan.sh — scripted parallel-batch planner for the hub (issue #70).
#
# With `Scope:` + native blocked-by on every issue, the "next concurrent batch" is a
# MECHANICAL graph computation — so it stays scripted (no LLM tokens in the control
# plane), not an LLM PARALLEL/SERIAL/MERGE judgment call.
#
# Given the open backlog it prints the next set of issues that can safely run
# CONCURRENTLY, ordered to minimize total wall-clock:
#   * READ — one `gh api graphql` round-trip for every open issue's `body` (for its
#     `Scope:` line) and its `blockedBy` connection (number + state).
#   * ELIGIBILITY — an issue is *ready* when all its blockers are closed.
#   * PRIORITY = critical-path depth — rank each ready issue by the longest blocked-by
#     chain rooted at it (one topo pass). Unblocking the longest serial tail earliest
#     minimizes makespan. Ties: direct-dependent count, then issue number.
#   * GREEDY DISJOINT-SCOPE PACK — walk ready issues in priority order; add to the
#     batch only when its `Scope:` is disjoint from every issue already in the batch
#     AND every in-flight spoke (passed in via --inflight). `Scope: *` / a missing
#     line ⇒ exclusive (runs alone, never batched). No concurrency cap.
#   * OUTPUT — issue numbers only (space-separated). It never dispatches; `/next-batch`
#     does that.
#
# Read-only. Run on the hub (main checkout). Functions are source-guarded so the unit
# tests can drive `plan_from_json` with a fixture graph without any network round-trip.
#
# Usage:
#   batch-plan.sh [--inflight "<scope tokens>"]...   # one --inflight per in-flight spoke
#
# Env:
#   BATCH_PLAN_OWNER / BATCH_PLAN_REPO   override the gh-resolved owner/repo (tests)
#   BATCH_PLAN_LIMIT                     max open issues fetched in the one round-trip
#                                        (default 100; pagination beyond is a follow-up)
set -uo pipefail
# noglob: a `Scope: *.py` glob must stay the LITERAL token the author wrote, not
# expand against the hub's cwd. The planner compares scope tokens as literals (the
# same set-intersection the scout used), so disabling globbing script-wide is safe.
set -f

# repo_slug -> "<owner> <repo>", from gh (override with BATCH_PLAN_OWNER/REPO).
repo_slug() {
  if [ -n "${BATCH_PLAN_OWNER:-}" ] && [ -n "${BATCH_PLAN_REPO:-}" ]; then
    printf '%s %s\n' "$BATCH_PLAN_OWNER" "$BATCH_PLAN_REPO"
    return 0
  fi
  gh repo view --json owner,name -q '.owner.login + " " + .name' 2>/dev/null
}

# fetch_issues -> the JSON array of open-issue nodes, each:
#   { number, body, blockedBy: { nodes: [ { number, state } ] } }
# ONE `gh api graphql` round-trip. `--jq` extracts the nodes array so plan_from_json
# reads exactly the shape the tests build by hand.
fetch_issues() {
  local owner repo limit
  read -r owner repo < <(repo_slug)
  if [ -z "${owner:-}" ] || [ -z "${repo:-}" ]; then
    echo "batch-plan: could not resolve owner/repo (is gh authenticated?)" >&2
    return 1
  fi
  limit="${BATCH_PLAN_LIMIT:-100}"
  # The $owner/$name/$limit in the query are GraphQL variables (bound via -F), NOT
  # shell variables — single quotes are deliberate so the shell leaves them intact.
  # shellcheck disable=SC2016
  gh api graphql \
    -F owner="$owner" -F name="$repo" -F limit="$limit" \
    -f query='
      query($owner:String!, $name:String!, $limit:Int!) {
        repository(owner:$owner, name:$name) {
          issues(states: OPEN, first: $limit, orderBy: {field: CREATED_AT, direction: ASC}) {
            nodes {
              number
              body
              blockedBy(first: 50) { nodes { number state } }
            }
          }
        }
      }' \
    --jq '.data.repository.issues.nodes'
}

# plan_from_json [--inflight "<tokens>"]... < <issues-json-array>
# The pure planner: reads the issue-node array on stdin, the in-flight spoke scopes
# from the repeated --inflight flags, and prints the batched issue numbers. All the
# graph work (eligibility, critical-path depth, greedy disjoint pack) is one embedded
# python3 pass; bash only marshals the in-flight scopes in via the environment.
plan_from_json() {
  local inflight=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --inflight) [ "$#" -ge 2 ] || { echo "batch-plan: --inflight needs a value" >&2; return 2; }
                  inflight+=("$2"); shift 2 ;;
      --inflight=*) inflight+=("${1#--inflight=}"); shift ;;
      *) echo "batch-plan: unknown plan_from_json arg: $1" >&2; return 2 ;;
    esac
  done
  # Marshal the in-flight scopes to python via the environment (one newline-
  # separated line per --inflight value), kept out of argv. python then splits each
  # line into scope tokens, so a multi-file spoke scope and a single one converge to
  # the same token set either way.
  local joined=""
  local s
  for s in ${inflight[@]+"${inflight[@]}"}; do
    joined+="${s}"$'\n'
  done
  command -v python3 >/dev/null 2>&1 || { echo "batch-plan: python3 required" >&2; return 1; }
  # The program is read from fd 3 (the heredoc), leaving python's stdin free to
  # carry the issue-node JSON piped in from fetch_issues / the tests.
  _BATCH_INFLIGHT="$joined" python3 /dev/fd/3 3<<'PYEOF'
import json
import os
import sys


def scope_of(body):
    """The issue's Scope: token set, or None when exclusive.

    The explicit `Scope:` line wins; its tokens are split on whitespace and commas.
    A bare `*` token OR a missing line marks the issue EXCLUSIVE (None) — it runs
    alone, never batched. Tokens are compared as literals (a plain set-intersection);
    glob-vs-path resolution is intentionally out of scope.
    """
    line = None
    for raw in (body or "").splitlines():
        stripped = raw.strip()
        if stripped.lower().startswith("scope:"):
            line = stripped[len("scope:"):]
            break
    if line is None:
        return None  # missing ⇒ exclusive
    tokens = [t for t in line.replace(",", " ").split() if t]
    if not tokens or "*" in tokens:
        return None  # `Scope: *` ⇒ exclusive
    return set(tokens)


def conflict(a, b):
    """Two scopes conflict when either is exclusive (None) or their tokens overlap."""
    if a is None or b is None:
        return True
    return bool(a & b)


def parse_inflight(blob):
    """One non-empty line per in-flight spoke ⇒ its scope (set, or None exclusive)."""
    scopes = []
    for line in (blob or "").splitlines():
        if not line.strip():
            continue
        tokens = [t for t in line.replace(",", " ").split() if t]
        scopes.append(None if (not tokens or "*" in tokens) else set(tokens))
    return scopes


def main():
    # Tolerate an empty / `null` / non-array payload (e.g. a graphql round-trip
    # that succeeded against a null `repository` — repo not found or a token-scope
    # gap): emit no batch rather than a raw traceback.
    raw = sys.stdin.read()
    nodes = json.loads(raw) if raw.strip() else []
    if not isinstance(nodes, list):
        nodes = []
    issues = {}
    for node in nodes:
        num = node.get("number")
        if num is None:
            continue
        blockers = [
            (b.get("number"), (b.get("state") or "").upper())
            for b in ((node.get("blockedBy") or {}).get("nodes") or [])
            if b.get("number") is not None
        ]
        issues[num] = {
            "number": num,
            "scope": scope_of(node.get("body")),
            "blockers": blockers,
        }

    open_nums = set(issues)

    # "blocks" edges among OPEN issues: A -> B when B is blockedBy A and A is open.
    # The downstream serial tail completing A unblocks. (Closed blockers are already
    # satisfied, so they contribute no future tail and need no edge.)
    children = {n: [] for n in open_nums}
    for num, info in issues.items():
        for blk_num, _state in info["blockers"]:
            if blk_num in open_nums:
                children[blk_num].append(num)

    # Critical-path depth: depth(A) = 1 + max(depth(child)). Memoized DFS with a
    # cycle guard (a back-edge contributes 0) so a malformed cyclic graph terminates.
    depth_cache = {}

    def depth(num, on_stack):
        if num in depth_cache:
            return depth_cache[num]
        if num in on_stack:
            return 0  # cycle — don't recurse through it
        on_stack.add(num)
        best = 0
        for child in children.get(num, ()):
            best = max(best, depth(child, on_stack))
        on_stack.discard(num)
        depth_cache[num] = 1 + best
        return depth_cache[num]

    def is_ready(info):
        # Ready when every blocker is closed (no blocker still open).
        return all(state == "CLOSED" for _num, state in info["blockers"])

    ready = [info for info in issues.values() if is_ready(info)]

    # Priority: critical-path depth desc, then direct-dependent count desc, then
    # issue number asc.
    ready.sort(
        key=lambda info: (
            -depth(info["number"], set()),
            -len(children.get(info["number"], ())),
            info["number"],
        )
    )

    # Greedy disjoint-scope pack, seeded with the in-flight spoke scopes so a ready
    # issue colliding with live work is held back. No concurrency cap.
    chosen_scopes = parse_inflight(os.environ.get("_BATCH_INFLIGHT", ""))
    batch = []
    for info in ready:
        scope = info["scope"]
        if all(not conflict(scope, existing) for existing in chosen_scopes):
            batch.append(info["number"])
            chosen_scopes.append(scope)

    if batch:
        print(" ".join(str(n) for n in batch))


main()
PYEOF
}

# main — fetch the open backlog and print the next concurrent batch. Pass through any
# --inflight flags so the hub/skill can feed in the scopes of live spokes.
main() {
  fetch_issues | plan_from_json "$@"
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
