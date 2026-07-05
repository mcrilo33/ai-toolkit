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
#   * ELIGIBILITY — an issue is *ready* when all its blockers are closed. A
#     `hold`-labelled issue is never ready: it is staged out of every batch until
#     the label is removed (it still blocks its dependents while open).
#   * PRIORITY — a `priority`-labelled issue sorts ahead of non-priority ready issues
#     (a manual "first among independents" override), then by critical-path depth: the
#     longest blocked-by chain rooted at the issue (one topo pass). Unblocking the
#     longest serial tail earliest minimizes makespan. Ties: direct-dependent count,
#     then issue number.
#   * GREEDY DISJOINT-SCOPE PACK — walk ready issues in priority order; add to the
#     batch only when its `Scope:` is disjoint from every issue already in the batch
#     AND every in-flight spoke (passed in via --inflight). `Scope: *` / a missing
#     line ⇒ exclusive (runs alone, never batched).
#   * CONCURRENCY CAP (issue #151) — `--cap N` bounds the TOTAL live spokes: the batch
#     is truncated so (in-flight count + batched count) never exceeds N, so a wide
#     batch can't starve the box or the co-located Langfuse. Pure tail truncation of
#     the priority-ordered pack. `--cap 0` / omitted ⇒ the historical unbounded batch.
#   * OUTPUT — issue numbers only (space-separated). It never dispatches; `/next-batch`
#     does that.
#   * MERGE-CANDIDATE LINT (issue #125) — when a blocked-by chain of OPEN issues has
#     colliding scopes it can never parallelize, so splitting bought nothing; print a
#     `⚠ merge candidates` proposal per chain on STDERR. Detection-only: the batch on
#     stdout, the exit code, and dispatch behavior are untouched. A
#     `Split: intentional — <why>` body line in ANY issue of the chain records the
#     deliberate split and silences the lint for that chain only.
#
# Read-only. Run on the hub (main checkout). Functions are source-guarded so the unit
# tests can drive `plan_from_json` with a fixture graph without any network round-trip.
#
# Usage:
#   batch-plan.sh [--inflight "<scope tokens>"]... [--cap N]
#     one --inflight per in-flight spoke; --cap N bounds total concurrent spokes (0=off)
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
#   { number, body, labels: { nodes: [ { name } ] },
#     blockedBy: { nodes: [ { number, state } ] } }
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
              labels(first: 20) { nodes { name } }
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
  local cap=0
  local repo_root=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --inflight) [ "$#" -ge 2 ] || { echo "batch-plan: --inflight needs a value" >&2; return 2; }
                  inflight+=("$2"); shift 2 ;;
      --inflight=*) inflight+=("${1#--inflight=}"); shift ;;
      --cap) [ "$#" -ge 2 ] || { echo "batch-plan: --cap needs a value" >&2; return 2; }
             cap="$2"; shift 2 ;;
      --cap=*) cap="${1#--cap=}"; shift ;;
      # --repo-root DIR enables the undeclared-dependency lint's create-detection
      # (a scope path absent under DIR is a to-be-created file). OMITTED ⇒ the lint's
      # filesystem probe is off and the planner's output stays byte-identical.
      --repo-root) [ "$#" -ge 2 ] || { echo "batch-plan: --repo-root needs a value" >&2; return 2; }
                   repo_root="$2"; shift 2 ;;
      --repo-root=*) repo_root="${1#--repo-root=}"; shift ;;
      *) echo "batch-plan: unknown plan_from_json arg: $1" >&2; return 2 ;;
    esac
  done
  case "$cap" in ''|*[!0-9]*) echo "batch-plan: --cap needs a non-negative integer (got '$cap')" >&2; return 2 ;; esac
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
  _BATCH_INFLIGHT="$joined" _BATCH_CAP="$cap" _BATCH_REPO_ROOT="$repo_root" python3 /dev/fd/3 3<<'PYEOF'
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


def has_split_marker(body):
    """True when the body carries a `Split: intentional — <why>` line.

    Mirrors the `Scope:`/`Gate:` line conventions (issue #125): the first
    `split:` line (case-insensitive) wins, and suppression applies only when
    ITS value begins with `intentional` — recording that the chain's
    serialization is deliberate and silencing the merge-candidate lint for the
    chain containing this issue. Any other value (`Split: maybe`) does NOT
    suppress, even if a later line says `intentional`.
    """
    for raw in (body or "").splitlines():
        stripped = raw.strip()
        if stripped.lower().startswith("split:"):
            return stripped[len("split:"):].strip().lower().startswith("intentional")
    return False


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


def order_chain(comp, comp_edges):
    """Members of one merge-candidate component in topological order.

    Kahn over the component's blocked-by edges, ties broken by issue number; a
    malformed cycle falls back to appending the leftovers by number so the lint
    still terminates and names every member.
    """
    indeg = {n: 0 for n in comp}
    out = {n: [] for n in comp}
    for a, b in comp_edges:
        indeg[b] += 1
        out[a].append(b)
    frontier = sorted(n for n in comp if indeg[n] == 0)
    order = []
    while frontier:
        n = frontier.pop(0)
        order.append(n)
        for m in out[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                frontier.append(m)
        frontier.sort()
    order.extend(sorted(n for n in comp if n not in set(order)))
    return order


def print_merge_candidates(issues, children):
    """Warn on stderr about blocked-by chains of open issues with colliding scopes.

    Such a chain is strictly serialized AND scope-colliding — the planner can never
    batch its members, so filing them separately bought zero throughput (issue #125).
    Detection-only: prints proposals on stderr, never touches the batch on stdout,
    the exit code, or which issues get dispatched. A `Split: intentional` marker in
    any member suppresses the proposal for that chain only.
    """
    edges = [
        (parent, child)
        for parent, kids in children.items()
        for child in kids
        if conflict(issues[parent]["scope"], issues[child]["scope"])
    ]
    adj = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen = set()
    for start in sorted(adj):
        if start in seen:
            continue
        comp = set()
        stack = [start]
        while stack:
            n = stack.pop()
            if n in comp:
                continue
            comp.add(n)
            stack.extend(adj[n] - comp)
        seen |= comp
        if any(issues[n]["split"] for n in comp):
            continue  # deliberate split — the chain opted out of the proposal
        comp_edges = [(a, b) for a, b in edges if a in comp and b in comp]
        shared = set()
        for a, b in comp_edges:
            sa, sb = issues[a]["scope"], issues[b]["scope"]
            if sa is not None and sb is not None:
                shared |= sa & sb
        detail = (
            "scope collides on " + ", ".join(sorted(shared)) if shared else "exclusive scope"
        )
        chain = " → ".join(f"#{n}" for n in order_chain(comp, comp_edges))
        print(
            f"⚠ merge candidates: {chain} ({detail}, strictly serialized)"
            " — consider one issue with subtasks",
            file=sys.stderr,
        )


def is_glob(token):
    """True when a scope token carries glob metacharacters (not a concrete path)."""
    return any(ch in token for ch in "*?[")


def print_undeclared_dependencies(issues, children, repo_root):
    """Warn on stderr about a probable undeclared blocked-by edge between open issues.

    Signal (issue #148): two open issues both list a scope path that does NOT yet
    exist under `repo_root` — a file one of them will CREATE — with no blocked-by
    edge (direct or transitive) ordering them. That is a producer→consumer pair
    whose ordering is undeclared, so the scheduler would run them in arbitrary order.

    Scope: this fires on the CREATE direction only — the "reads a file another
    creates" half of the issue's "creates/deletes" wording. A shared path that
    already EXISTS is mere file overlap the planner serializes on its own (and
    flagging it would nudge authors toward the fake edges `issue-hygiene` forbids),
    so it stays silent. The DELETE direction (a shared path a sibling removes) is
    deliberately out of scope: a to-be-deleted file still exists at plan time, so
    existence alone cannot detect it.

    Detection-only: prints to stderr, never touches the batch, the exit code, or
    which issues dispatch. Disabled entirely when `repo_root` is falsy (the pure
    planner default, `--repo-root` omitted) so existing output stays byte-identical.
    A `Split: intentional` marker on either issue suppresses that pair.
    """
    if not repo_root:
        return

    # Transitive blocked-by reachability over open issues: `b in reach[a]` ⇒ a must
    # close before b, so the pair is already ordered (directly or through a chain).
    reach = {n: set() for n in issues}
    for n in issues:
        stack = list(children.get(n, ()))
        while stack:
            m = stack.pop()
            if m in reach[n]:
                continue
            reach[n].add(m)
            stack.extend(children.get(m, ()))

    def ordered(a, b):
        return b in reach.get(a, ()) or a in reach.get(b, ())

    # Group open issues by each concrete, not-yet-created scope path they list.
    # UPGRADE: covers CREATE only (an absent path). To also catch a shared to-be-
    # DELETED file, teach `Scope:` an explicit create/delete annotation (e.g.
    # `+new.py` / `-old.py`) and key this lint off it — worth it once delete-ordering
    # bugs actually surface in practice.
    producers = {}
    for num, info in issues.items():
        scope = info["scope"]
        if scope is None:
            continue
        for token in scope:
            if is_glob(token) or os.path.exists(os.path.join(repo_root, token)):
                continue  # a glob, or an existing file (mere overlap) — not a create
            producers.setdefault(token, set()).add(num)

    # Collect the shared not-yet-created paths per issue pair, then warn once each.
    pair_tokens = {}
    for token, members in producers.items():
        ordered_members = sorted(members)
        for i in range(len(ordered_members)):
            for j in range(i + 1, len(ordered_members)):
                pair_tokens.setdefault((ordered_members[i], ordered_members[j]), []).append(token)

    for (a, b) in sorted(pair_tokens):
        if ordered(a, b):
            continue  # ordering already declared (directly or transitively)
        if issues[a]["split"] or issues[b]["split"]:
            continue  # a deliberate split opted out
        toks = ", ".join(f"`{t}`" for t in sorted(pair_tokens[(a, b)]))
        print(
            f"⚠ possible undeclared dependency: #{a} / #{b} both list {toks} "
            "(not yet in tree — one creates it) — declare blocked-by if one is "
            "based on the other",
            file=sys.stderr,
        )


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
        labels = {
            (lbl.get("name") or "").lower()
            for lbl in ((node.get("labels") or {}).get("nodes") or [])
            if lbl.get("name")
        }
        issues[num] = {
            "number": num,
            "scope": scope_of(node.get("body")),
            "split": has_split_marker(node.get("body")),
            "blockers": blockers,
            "hold": "hold" in labels,
            "priority": "priority" in labels,
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

    # A `hold`-labelled issue is staged, not dispatchable: excluded from every
    # batch until the label is removed. It stays open, so it still blocks its
    # dependents and still contributes to their critical-path depth.
    ready = [info for info in issues.values() if is_ready(info) and not info["hold"]]

    # Rank: `priority`-labelled first, then critical-path depth desc, then
    # direct-dependent count desc, then issue number asc. The `priority` label is a
    # manual "first among independents" override sorted ahead of the depth tiebreak;
    # it reorders only — the greedy disjoint-scope pack below is unchanged.
    ready.sort(
        key=lambda info: (
            0 if info["priority"] else 1,
            -depth(info["number"], set()),
            -len(children.get(info["number"], ())),
            info["number"],
        )
    )

    # Greedy disjoint-scope pack, seeded with the in-flight spoke scopes so a ready
    # issue colliding with live work is held back.
    inflight_scopes = parse_inflight(os.environ.get("_BATCH_INFLIGHT", ""))
    chosen_scopes = list(inflight_scopes)
    batch = []
    for info in ready:
        scope = info["scope"]
        if all(not conflict(scope, existing) for existing in chosen_scopes):
            batch.append(info["number"])
            chosen_scopes.append(scope)

    # Concurrency cap (issue #151): bound the total live spokes so a wide batch can't
    # starve the box / the co-located Langfuse. `_BATCH_CAP` (>0) is the ceiling on
    # (in-flight + newly-dispatched); the free slots are what remain after the spokes
    # already running. Cap 0 / unset ⇒ the historical unbounded batch. Pure tail
    # truncation of the priority-ordered pack — never reorders, never adds.
    cap = int(os.environ.get("_BATCH_CAP", "0") or "0")
    if cap > 0:
        slots = max(0, cap - len(inflight_scopes))
        batch = batch[:slots]

    if batch:
        print(" ".join(str(n) for n in batch))

    print_merge_candidates(issues, children)
    print_undeclared_dependencies(issues, children, os.environ.get("_BATCH_REPO_ROOT", ""))


main()
PYEOF
}

# main — fetch the open backlog and print the next concurrent batch. Pass through any
# --inflight flags so the hub/skill can feed in the scopes of live spokes, and seed
# --repo-root with the hub checkout so the undeclared-dependency lint can tell a
# to-be-created scope path from an existing file. An explicit --repo-root in "$@"
# (last-wins in the arg loop) still overrides this default.
main() {
  local root
  root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  fetch_issues | plan_from_json --repo-root "$root" "$@"
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
