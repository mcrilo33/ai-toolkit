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
#   * GREEDY DISJOINT-SCOPE PACK — walk ready issues in priority order; open a new
#     dispatch UNIT only when its `Scope:` is disjoint from every unit already in the
#     batch AND every in-flight spoke (passed in via --inflight). `Scope: *` / a missing
#     line ⇒ exclusive (runs alone, never batched).
#   * SAME-SCOPE SUBTASK PACK (issue #278) — a unit is a GROUP of issues, not one issue.
#     When a ready issue wins a slot, the remaining ranked ready set is swept for peers
#     CONTAINED in its scope (`packable`), and they are folded in as ordered subtasks on
#     ONE branch. Such peers could never run concurrently anyway (they collide), so
#     serializing them into separate dispatches paid the whole spoke lifecycle tax —
#     worktree, first-push suite seed, PLAN gate, review, land — twice for nothing
#     (measured: #263/#265 shipped byte-identical `Scope:` footers and ran as two
#     back-to-back lifecycles).
#     Containment (not mere overlap) makes the pack STRICTLY ADDITIVE: a unit's footprint
#     stays exactly its leader's declared scope, so which units form — and every other
#     issue's slot — is bit-for-bit what it would have been without packing. A PARTIAL
#     overlap never packs, and neither does a SUPERSET peer (absorbing it would widen the
#     unit past the scope its slot was granted for and can de-parallelize disjoint work);
#     both merely wait, exactly as they do today. `Split: intentional` opts an issue out.
#     --pack-max bounds a unit's size (a spoke's context window is finite); the overflow
#     falls back to a later dispatch.
#   * OUTPUT SHAPE — members are comma-joined within a unit, units space-separated
#     (`263,265 270`), so a one-issue unit is byte-identical to the pre-#278 line.
#   * ROUTE (issue #278, --route) — the pack above can only group issues ready in the
#     SAME tick. An issue filed while a matching spoke is already LIVE is instead named
#     on a `route:<issue> <spoke>` line, so the drain can hand it to that running spoke as
#     a subtask rather than wait out its whole lifecycle. Advisory: the drain owns the
#     decision (only it knows the spoke's queue depth). Omitted ⇒ stdout is unchanged.
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
#   * UNCHAINED MERGE-CANDIDATE LINT (issue #167) — the #125 lint above only catches
#     blocked-by CHAINS. A cluster of READY, mutually-unblocked issues sharing a
#     concrete scope token with NO dependency edge between them is serialized by the
#     greedy pack yet never surfaced (the #158/#160/#161/#162 case). This lint groups
#     the ready set by scope-token overlap and prints one `⚠ merge candidates … ready
#     & unchained` proposal per cluster of ≥2 on STDERR. A cluster fully contained in
#     one #125 chain component is skipped (already on a #125 line), but a collision
#     ACROSS two separate chains — which #125 never pairs — is still surfaced, so
#     nothing is double-reported yet nothing real is dropped. Same detection-only /
#     `Split: intentional` semantics.
#   * SCOPELESS-READY WARNING (issue #217) — a MISSING `Scope:` line marks an issue
#     exclusive (runs alone), correct fail-closed semantics but silent, so a batch of
#     scope-less issues can serialize a whole drain unnoticed. This names each READY
#     issue with no `Scope:` line on STDERR (`#N has no Scope: line — treated
#     exclusive`) so the serialization is diagnosable from the plan log. `Scope: *` is
#     a deliberate exclusive and stays silent — only the accidental missing line is
#     flagged. Detection-only: the batch, exit code, and dispatch are untouched.
#
# Read-only. Run on the hub (main checkout). Functions are source-guarded so the unit
# tests can drive `plan_from_json` with a fixture graph without any network round-trip.
#
# Usage:
#   batch-plan.sh [--inflight "<scope tokens>"]... [--cap N] [--pack-max N] [--route]
#     one --inflight per in-flight spoke; --cap N bounds total concurrent spokes (0=off);
#     --pack-max N bounds issues per dispatch unit (0=off); --route adds route: lines
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
  local inflight_nums=()
  local explain=""
  local cap=0
  local pack_max=0
  local route=0
  local repo_root=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --inflight) [ "$#" -ge 2 ] || { echo "batch-plan: --inflight needs a value" >&2; return 2; }
                  inflight+=("$2"); shift 2 ;;
      --inflight=*) inflight+=("${1#--inflight=}"); shift ;;
      # --explain (#223) annotates every open dispatchable issue with its scheduling
      # disposition + reason instead of printing the winning batch; --explain-labels emits
      # the same dispositions as a machine-readable `<num>\t<afk:label|->` TSV. --inflight-issue
      # N (repeatable) names the in-flight issue NUMBERS so the view can attribute a
      # blocked-by-scope collision to a specific live spoke (the scope is looked up from the
      # backlog). All three are inert on the normal batch path (byte-identical output).
      --explain) explain="view"; shift ;;
      --explain-labels) explain="labels"; shift ;;
      --inflight-issue) [ "$#" -ge 2 ] || { echo "batch-plan: --inflight-issue needs a value" >&2; return 2; }
                        inflight_nums+=("$2"); shift 2 ;;
      --inflight-issue=*) inflight_nums+=("${1#--inflight-issue=}"); shift ;;
      --cap) [ "$#" -ge 2 ] || { echo "batch-plan: --cap needs a value" >&2; return 2; }
             cap="$2"; shift 2 ;;
      --cap=*) cap="${1#--cap=}"; shift ;;
      # --pack-max N (#278) bounds how many issues one dispatch UNIT may carry (the
      # chain-length cap: a spoke's context window is finite). 0/omitted ⇒ unbounded.
      # --route (#278) ADDS `route:<issue> <spoke>` lines after the batch line, naming the
      # live spoke a ready issue is packable into. Omitted ⇒ stdout is byte-identical.
      --pack-max) [ "$#" -ge 2 ] || { echo "batch-plan: --pack-max needs a value" >&2; return 2; }
                  pack_max="$2"; shift 2 ;;
      --pack-max=*) pack_max="${1#--pack-max=}"; shift ;;
      --route) route=1; shift ;;
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
  case "$pack_max" in ''|*[!0-9]*) echo "batch-plan: --pack-max needs a non-negative integer (got '$pack_max')" >&2; return 2 ;; esac
  # Marshal the in-flight scopes to python via the environment (one newline-
  # separated line per --inflight value), kept out of argv. python then splits each
  # line into scope tokens, so a multi-file spoke scope and a single one converge to
  # the same token set either way.
  local joined=""
  local s
  for s in ${inflight[@]+"${inflight[@]}"}; do
    joined+="${s}"$'\n'
  done
  # The in-flight issue NUMBERS (--inflight-issue), newline-separated, for the explain view.
  local joined_nums=""
  for s in ${inflight_nums[@]+"${inflight_nums[@]}"}; do
    joined_nums+="${s}"$'\n'
  done
  command -v python3 >/dev/null 2>&1 || { echo "batch-plan: python3 required" >&2; return 1; }
  # The program is read from fd 3 (the heredoc), leaving python's stdin free to
  # carry the issue-node JSON piped in from fetch_issues / the tests.
  _BATCH_INFLIGHT="$joined" _BATCH_INFLIGHT_NUMS="$joined_nums" _BATCH_EXPLAIN="$explain" \
    _BATCH_CAP="$cap" _BATCH_PACK_MAX="$pack_max" _BATCH_ROUTE="$route" \
    _BATCH_REPO_ROOT="$repo_root" python3 /dev/fd/3 3<<'PYEOF'
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


def scope_undeclared(body):
    """True when the body declares no USABLE scope: no `Scope:` line at all, or a bare
    `Scope:` label with no tokens.

    Both leave the issue exclusive-by-accident (scope_of ⇒ None) — the silent case
    issue #217 flags. A deliberate `Scope: *` DECLARES exclusivity (a token is
    present), so it is not treated as undeclared and stays silent. Uses the same
    `scope:`-prefix / comma+whitespace token split as scope_of, so the two can never
    disagree on which issues are exclusive.
    """
    for raw in (body or "").splitlines():
        stripped = raw.strip()
        if stripped.lower().startswith("scope:"):
            tokens = [t for t in stripped[len("scope:"):].replace(",", " ").split() if t]
            return not tokens  # bare label ⇒ nothing declared; any token (incl `*`) ⇒ declared
    return True  # no `Scope:` line at all


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


def packable(host, guest):
    """True when a spoke owning `host` can also carry `guest` as an ordered subtask (#278).

    CONTAINMENT, not mere overlap: `guest <= host`. The guest's whole footprint already
    sits inside the scope the host's dispatch reserved, so absorbing it does not widen the
    unit by a single token. Two issues in this relation could never run concurrently
    anyway (they conflict), so the planner used to SERIALIZE them into separate
    dispatches — paying the full spoke lifecycle tax (worktree, first-push suite seed,
    PLAN-gate round-trip, review, land) twice for work one branch could carry. The
    measured motivator: #263/#265 shipped byte-identical `Scope:` footers (containment
    holds both ways) and ran as two back-to-back lifecycles.

    Containment is what makes packing STRICTLY ADDITIVE, and that property is the whole
    reason this is safe: the unit's footprint stays exactly the host's declared scope, so
    the set of units the planner forms — and every other issue's slot — is bit-for-bit
    what it would have been without packing. Packing only fills issues that were already
    doomed to wait into a spoke that was already running.

    Deliberately NARROWER than `not conflict(host, guest)` in BOTH directions:

      * A PARTIAL overlap (`{a,m}` vs `{a,z}`) never packs: neither spoke owns the other's
        full footprint, so a subtask would edit files outside the scope its dispatch
        reserved — the disjointness `conflict()` guarantees BETWEEN units would silently
        not hold WITHIN one.
      * A SUPERSET guest never packs either, even though it is "identical-or-subset" in
        the loose sense. Absorbing it would widen the unit past the scope the host won its
        slot with, and a widened unit can collide with live work or steal a token a
        genuinely disjoint peer needed — de-parallelizing issues that had no reason to be
        ordered. (Concretely: `{w}`, `{w,b}`, `{b}` — the first and last are disjoint and
        dispatch as two concurrent spokes today. Letting `{w,b}` be absorbed by `{w}`
        widens that unit onto b.py and collapses all three into ONE serial spoke: a real
        parallelism LOSS versus the pre-#278 planner.) Such a pair is not dropped, merely
        deferred: rank decides which of the two leads, and when the superset leads, the
        subset packs into IT next round.

    Exclusive (None — `Scope: *` or a missing line) NEVER packs, in either position: an
    unknown footprint cannot be proven to sit inside another. That fails CLOSED, matching
    how `conflict()` treats None (collides with everything) rather than inverting it.
    """
    if host is None or guest is None:
        return False
    return guest <= host


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


def connected_components(adj):
    """Connected components of an undirected adjacency map, each as a set of nodes.

    Shared by both merge-candidate lints (#125 chains, #167 unchained clusters) so
    their clustering can never silently diverge. Isolated nodes absent from `adj`
    are not walked — a caller wanting singletons must seed `adj` with them.
    """
    seen = set()
    comps = []
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
        comps.append(comp)
    return comps


def print_routes(issues, ready_nums, inflight_nums):
    """Print `route:<issue> <spoke>` for each ready issue packable into ONE live spoke.

    Trigger B of issue #278 (`--route`). The dispatch-time pack above can only group
    issues that are ready in the SAME tick; an issue filed while a matching spoke is
    already running is merely held back by the in-flight scope collision and waits out
    that spoke's whole lifecycle before starting its own. This names the running spoke it
    could instead join as a subtask, so the drain can hand it over — no second worktree,
    no second first-push suite seed.

    Each live spoke's scope is looked up from the BACKLOG by issue number, exactly as
    `_explain_dispositions` does, rather than positionally zipping `--inflight` scopes to
    `--inflight-issue` numbers: the two flags are populated from separate walks, so
    trusting their order would silently mis-attribute a route to the wrong spoke. An
    in-flight issue absent from the fetched backlog contributes no scope and simply never
    matches — it can only miss a route, never invent one.

    The live spoke is the HOST and the new issue the GUEST, so `packable` requires the
    issue to sit INSIDE the running spoke's declared scope. A superset issue is refused:
    routing it would silently widen a live spoke's footprint past what its dispatch
    reserved, onto files another spoke may hold.

    Fails CLOSED on ambiguity: an issue packable into TWO live spokes has no single
    correct target, so it is left to the normal collision hold-back. The chain-length cap
    is NOT applied here — how full a spoke's queue already is, is state only the drain
    holds (the queued-subtask marker); this names candidates and the drain decides.
    """
    live = sorted(n for n in inflight_nums if n in issues and not issues[n]["split"])
    for num in sorted(ready_nums):
        if num in inflight_nums or issues[num]["split"]:
            continue  # a deliberate split is never auto-merged into a shared spoke
        scope = issues[num]["scope"]
        targets = [m for m in live if packable(issues[m]["scope"], scope)]
        if len(targets) != 1:
            continue  # unmatched, or ambiguous (2+ candidate spokes) — fail closed
        # Joining the target spoke must not collide with a DIFFERENT live spoke: being
        # packable into one says nothing about the others. (Two live spokes should never
        # overlap in the first place, but a hand-dispatched pair can, and routing must not
        # compound it.)
        if any(conflict(issues[m]["scope"], scope) for m in live if m != targets[0]):
            continue
        print(f"route:{num} {targets[0]}")


def print_merge_candidates(issues, children, packed_clusters=()):
    """Warn on stderr about blocked-by chains of open issues with colliding scopes.

    Such a chain is strictly serialized AND scope-colliding — the planner can never
    batch its members, so filing them separately bought zero throughput (issue #125).
    Detection-only: prints proposals on stderr, never touches the batch on stdout,
    the exit code, or which issues get dispatched. A `Split: intentional` marker in
    any member suppresses the proposal for that chain only.

    Returns the list of reported components (each a set of issue numbers), so the
    unchained lint (#167) can skip a cluster this chain already covers and never
    double-report the same collision.
    """
    reported = []
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
    for comp in connected_components(adj):
        if any(issues[n]["split"] for n in comp):
            continue  # deliberate split — the chain opted out of the proposal
        if any(comp <= cluster for cluster in packed_clusters):
            continue  # #278 auto-packed it into one spoke — nothing left to propose
        reported.append(comp)
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
    return reported


def print_unchained_merge_candidates(issues, ready_nums, flagged_components, packed_clusters=()):
    """Warn on stderr about a cluster of ready issues sharing scope with no deps.

    The #125 lint fires only on blocked-by CHAINS. An UNCHAINED cluster — ready,
    mutually-unblocked issues (all blockers closed) that share a concrete scope
    token but have no dependency edge between them — is serialized by the greedy
    disjoint-scope pack yet never surfaced, so splitting bought zero throughput
    (issue #167, the #158/#160/#161/#162 case merged by hand as #165).

    Groups the ready set by concrete-token overlap (the same literal set-intersection
    the packer uses) and prints one umbrella-candidate hint per connected cluster of
    ≥2. `flagged_components` is what #125 already reported: a cluster fully contained
    in one such component is skipped (that collision is already on the #125 line), but
    two ready heads colliding ACROSS separate chains — which #125 never pairs — are
    still surfaced. Exclusive (`Scope: *`/missing) issues carry no named token to
    report and are left to #125's "exclusive scope" path.

    Detection-only: prints to stderr, never touches the batch, the exit code, or
    which issues dispatch. A `Split: intentional` marker opts an issue out of its own
    candidacy, so a deliberately-separate issue no longer silences its unmarked peers.
    """
    # Candidates: ready, with a concrete (non-exclusive) scope, and not themselves
    # opted out via `Split: intentional`. Two share an edge when their tokens overlap.
    candidates = sorted(
        n
        for n in ready_nums
        if issues[n]["scope"] is not None and not issues[n]["split"]
    )
    adj = {n: set() for n in candidates}
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            if issues[a]["scope"] & issues[b]["scope"]:
                adj[a].add(b)
                adj[b].add(a)

    for comp in connected_components(adj):
        if len(comp) < 2:
            continue
        if any(comp <= fc for fc in flagged_components):
            continue  # already reported on a #125 chain line — don't double-report
        # #278: this lint's whole proposal ("consider one umbrella issue") is what the
        # planner now performs automatically for a packable cluster. Warning about work
        # already batched into one spoke would train the operator to tune out a lint that
        # fires on solved problems. A cluster only PARTLY packed (a subset-vs-partial-
        # overlap mix) is not covered by any single group and still warrants the hint.
        if any(comp <= cluster for cluster in packed_clusters):
            continue
        members = sorted(comp)
        shared = set()
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                shared |= issues[a]["scope"] & issues[b]["scope"]
        names = " ".join(f"#{n}" for n in members)
        print(
            f"⚠ merge candidates: {names} "
            f"(scope collides on {', '.join(sorted(shared))}, ready & unchained)"
            " — consider one umbrella issue",
            file=sys.stderr,
        )


def print_scopeless_ready(issues, ready_nums):
    """Warn on stderr for each ready issue with NO `Scope:` line (issue #217).

    A missing `Scope:` line marks the issue exclusive (runs alone, never batched) —
    correct fail-closed semantics, but silent, so a whole drain can serialize on a
    batch of scope-less issues before a human notices (the two 2026-07-08 batches that
    motivated this). Naming each ready scope-less issue makes that serialization
    diagnosable from the plan log (the no-silent-caps rule). `Scope: *` is a DELIBERATE
    exclusive and stays silent — only the accidental missing-line case is flagged.

    Detection-only: prints to stderr, never touches the batch, the exit code, or which
    issues dispatch.
    """
    for num in sorted(ready_nums):
        if issues[num]["scope_undeclared"]:
            print(
                f"⚠ #{num} has no Scope: line — treated exclusive (runs alone) — "
                "add a concrete Scope: line so it can batch",
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


def _inflight_issue_nums():
    """The in-flight issue NUMBERS passed via --inflight-issue (env-marshaled), as a set."""
    nums = set()
    for line in (os.environ.get("_BATCH_INFLIGHT_NUMS", "")).splitlines():
        tok = line.strip()
        if tok.isdigit():
            nums.add(int(tok))
    return nums


def _explain_dispositions(issues, children, depth, is_ready):
    """Classify every open issue by its scheduling disposition (issue #223).

    Runs the SAME greedy disjoint-scope walk the packer uses, seeded with the in-flight
    issue numbers (their scopes looked up from the backlog), so the surfaced dispositions
    track what the scheduler actually does. (An in-flight issue absent from the fetched
    backlog is dropped from the seed here, whereas dispatch fails it closed to `*`; that
    rare not-in-fetch case can only under-report a collision, never invent one.) Returns
    (ranked, inflight_present, inflight_nums, collider_of, holds_back, packed_with) where:
      * ranked          — dispatchable issues (all blockers closed, incl. held) in priority order
      * inflight_present— the in-flight issue numbers that are in the backlog, ascending
      * collider_of[n]  — the already-chosen issue whose scope blocks n (in-flight or a
                          higher-priority batched peer), when n is held back
      * holds_back[m]   — the issues m blocks, in priority order (m names what it holds back)
      * packed_with[n]  — the leader whose spoke carries n as an ordered subtask (#278),
                          when n is absorbed rather than held back; disjoint from collider_of
    """
    inflight_nums = _inflight_issue_nums()
    pack_max = int(os.environ.get("_BATCH_PACK_MAX", "0") or "0")
    dispatchable = [n for n in issues if is_ready(issues[n])]
    ranked = sorted(
        dispatchable,
        key=lambda n: (
            0 if issues[n]["priority"] else 1,
            -depth(n, set()),
            -len(children.get(n, ())),
            n,
        ),
    )
    inflight_present = sorted(n for n in inflight_nums if n in issues)
    chosen_order = list(inflight_present)
    chosen_scope = {n: issues[n]["scope"] for n in inflight_present}
    collider_of = {}
    packed_with = {}
    group_size = {}
    for n in ranked:
        if n in inflight_nums or issues[n]["hold"]:
            continue  # in-flight is already running; held is staged out — neither is packed
        scope = issues[n]["scope"]
        collider = next((m for m in chosen_order if conflict(chosen_scope[m], scope)), None)
        if collider is None:
            chosen_order.append(n)
            chosen_scope[n] = scope
            group_size[n] = 1
            continue
        # #278: a collider the issue is PACKABLE into is not holding it back at all — it
        # ships in that spoke as a subtask. Reporting the old `blocked-by-scope` here
        # would be a lie about work that is scheduled to run.
        #
        # Mirrors main()'s pack exactly: same ranked order, same containment test against
        # the LEADER's (never-widening) scope, same chain cap, same split opt-out. An
        # in-flight collider is excluded: that issue is ROUTED (the `--route` channel), a
        # decision only the drain can make, since the spoke's queue depth lives there.
        if (
            collider not in inflight_nums
            and not issues[n]["split"]
            and not issues[collider]["split"]
            and packable(chosen_scope[collider], scope)
            and (pack_max <= 0 or group_size.get(collider, 1) < pack_max)
        ):
            packed_with[n] = collider
            group_size[collider] = group_size.get(collider, 1) + 1
        else:
            collider_of[n] = collider
    holds_back = {}
    for n, m in collider_of.items():  # collider_of was built in ranked (priority) order
        holds_back.setdefault(m, []).append(n)
    return ranked, inflight_present, inflight_nums, collider_of, holds_back, packed_with


def _explain_label(issues, ranked_set, inflight_nums, collider_of, n):
    """The single afk:* label for issue n, or '-' when it carries none (#223).

    The label collapses the disposition to the four-label set — deliberately WITHOUT the
    cross-issue `#N` detail (that stays in the human view). A held or dep-blocked issue
    carries no afk:* label so the GitHub issue list stays a clean live-scheduling glance.
    """
    if n in inflight_nums:
        return "afk:in-flight"
    if n not in ranked_set or issues[n]["hold"]:
        return "-"  # dep-blocked (open native blocker) or held — not a scheduling candidate
    if n in collider_of:
        return "afk:blocked-by-scope"  # blocked reads first, even for an exclusive issue waiting
    if issues[n]["scope"] is None:
        return "afk:exclusive"
    return "afk:queued"


def _print_explain_view(
    issues, ranked, inflight_present, inflight_nums, collider_of, holds_back, packed_with
):
    """Print the human `--explain` view: one `#N  <disposition>  <reason>` line per issue."""

    def excl(n):
        return "Scope: *" if not issues[n]["scope_undeclared"] else "no Scope: line"

    def tokens(scope):
        return "(" + " ".join(sorted(scope)) + ")"

    def held_back_suffix(n):
        blocked = holds_back.get(n, [])
        return " — holds back " + ", ".join(f"#{k}" for k in blocked) if blocked else ""

    def scope_detail(n):
        scope = issues[n]["scope"]
        return tokens(scope) if scope is not None else f"({excl(n)})"

    def line(n):
        scope = issues[n]["scope"]
        if n in inflight_nums:
            primary = "in-flight"
            if scope is None:
                detail = f"exclusive ({excl(n)})" + (held_back_suffix(n) or " — runs alone")
            else:
                detail = scope_detail(n) + held_back_suffix(n)
        elif issues[n]["hold"]:
            primary, detail = "held", "(hold label)"
        elif n in packed_with:
            # #278: not held back — it rides #M's spoke as an ordered subtask.
            primary, detail = f"packed-with:#{packed_with[n]}", scope_detail(n)
        elif n in collider_of:
            # A held-back issue reads as blocked FIRST — even an exclusive one waiting behind
            # another chosen spoke — so its reason is the collider, not a false "runs alone".
            primary, detail = f"blocked-by-scope:#{collider_of[n]}", scope_detail(n)
        elif scope is None:
            primary = "exclusive"
            detail = f"({excl(n)})" + (held_back_suffix(n) or " — runs alone")
        else:
            primary, detail = "queued", scope_detail(n)
        return f"#{n:<5} {primary:<22} {detail}".rstrip()

    for n in inflight_present:
        print(line(n))
    for n in ranked:
        if n not in inflight_nums:
            print(line(n))


def render_explain(issues, children, depth, is_ready, mode):
    """Render the disposition of every open issue (issue #223): the human `--explain` view
    (mode 'view') or the machine-readable `<num>\\t<afk:label|->` TSV (mode 'labels')."""
    (
        ranked,
        inflight_present,
        inflight_nums,
        collider_of,
        holds_back,
        packed_with,
    ) = _explain_dispositions(issues, children, depth, is_ready)
    if mode == "labels":
        ranked_set = set(ranked)
        for n in sorted(issues):
            print(f"{n}\t{_explain_label(issues, ranked_set, inflight_nums, collider_of, n)}")
    else:
        _print_explain_view(
            issues, ranked, inflight_present, inflight_nums, collider_of, holds_back, packed_with
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
            "scope_undeclared": scope_undeclared(node.get("body")),
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

    # `--explain` / `--explain-labels` (#223): a read-only disposition view over the SAME
    # graph, rendered instead of the batch line + lints. Returns early so the normal path's
    # output is untouched when neither flag is set.
    explain = os.environ.get("_BATCH_EXPLAIN", "")
    if explain:
        render_explain(issues, children, depth, is_ready, explain)
        return

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
    #
    # Since #278 the unit of dispatch is a GROUP (one spoke), not one issue: when a ready
    # issue wins a slot, sweep the remaining ranked ready set for `packable` peers and
    # fold them in as ordered subtasks on one branch. The group's FIRST member is the
    # primary — it names the branch slug and is what `inflight_worktrees` / worktree-land
    # parse — so the group leads with the RANKED winner and peers follow in ranked order.
    #
    # (The issue proposed ordering groups with `order_chain()`. That topo-sort is vacuous
    # here: only READY issues pack, and `is_ready` means every blocker is CLOSED, so a
    # blocked-by edge between two group members is impossible. It would degenerate to
    # sort-by-number and silently demote a `priority`-labelled issue to a subtask under a
    # lower-numbered peer — discarding the rank the block above just computed.)
    inflight_scopes = parse_inflight(os.environ.get("_BATCH_INFLIGHT", ""))
    pack_max = int(os.environ.get("_BATCH_PACK_MAX", "0") or "0")
    chosen_scopes = list(inflight_scopes)
    batch = []
    packed = set()
    for info in ready:
        n = info["number"]
        if n in packed:
            continue  # already folded into an earlier group as a subtask
        scope = info["scope"]
        if any(conflict(scope, existing) for existing in chosen_scopes):
            continue
        group = [n]
        packed.add(n)
        # `Split: intentional` opts an issue out of PACKING, not just out of the lint. The
        # marker records that this issue's separation from its scope-peers is deliberate —
        # auto-folding it into a shared spoke is precisely the merge the operator declined.
        # Honoured on both sides: a marked leader absorbs nobody, a marked peer joins nobody.
        if scope is not None and not info["split"]:
            for peer in ready:
                if pack_max > 0 and len(group) >= pack_max:
                    break  # chain-length cap: the rest overflow to a later dispatch
                m = peer["number"]
                if m in packed or peer["split"] or not packable(scope, peer["scope"]):
                    continue
                group.append(m)
                packed.add(m)
        batch.append(group)
        # The unit's footprint is the LEADER's scope, unchanged: `packable` only absorbs
        # peers already contained in it (see its docstring), so no peer can widen the unit
        # past the scope this slot was granted for. That is what keeps the pack strictly
        # additive — every other issue's disposition is exactly what it would have been
        # without packing — and it is why no re-validation is needed here.
        chosen_scopes.append(scope)

    # The clusters packing ABSORBED, for the merge-candidate lints below. Captured before
    # the cap truncates: a group the cap defers is still packed (it dispatches whole next
    # window), so the lint must stay silent about it either way.
    packed_clusters = [set(group) for group in batch if len(group) > 1]

    # Concurrency cap (issue #151): bound the total live spokes so a wide batch can't
    # starve the box / the co-located Langfuse. `_BATCH_CAP` (>0) is the ceiling on
    # (in-flight + newly-dispatched); the free slots are what remain after the spokes
    # already running. Cap 0 / unset ⇒ the historical unbounded batch. Pure tail
    # truncation of the priority-ordered pack — never reorders, never adds.
    # `batch` is a list of GROUPS since #278, so this slices dispatch UNITS (spokes) —
    # which is what the cap has always meant. Counting issues would starve a packed batch
    # to a fraction of the live spokes the operator asked for.
    cap = int(os.environ.get("_BATCH_CAP", "0") or "0")
    if cap > 0:
        slots = max(0, cap - len(inflight_scopes))
        batch = batch[:slots]

    # The wire format: members comma-joined within a unit, units space-separated. A
    # one-issue unit renders exactly as it did pre-#278, so every consumer that never
    # meets a pack — and afk_done's bare planner call — sees byte-identical output.
    if batch:
        print(" ".join(",".join(str(n) for n in group) for group in batch))

    ready_nums = {info["number"] for info in ready}
    if os.environ.get("_BATCH_ROUTE", "0") == "1":
        print_routes(issues, ready_nums, _inflight_issue_nums())

    flagged_components = print_merge_candidates(issues, children, packed_clusters)
    print_unchained_merge_candidates(issues, ready_nums, flagged_components, packed_clusters)
    print_undeclared_dependencies(issues, children, os.environ.get("_BATCH_REPO_ROOT", ""))
    print_scopeless_ready(issues, ready_nums)


main()
PYEOF
}

# _batch_inflight_issue_nums — the issue number leading each task worktree's branch slug
# (e.g. feature/223-slug → 223), one per line. Used to seed the --explain view with the
# live in-flight set so it can attribute blocked-by-scope collisions to a running spoke.
# The main checkout (branch `main`) and detached worktrees carry no leading digits and
# are skipped. A best-effort standalone parse (no worktree-lib dependency). LC_ALL=C forces
# a byte-stable locale for the system-tool parse, matching this repo's locale-hardening
# discipline (#189/#194) even though worktree branch refs are ASCII.
_batch_inflight_issue_nums() {
  LC_ALL=C git worktree list --porcelain 2>/dev/null | LC_ALL=C awk '
    /^branch /{ slug = $2; sub(/.*\//, "", slug); if (match(slug, /^[0-9]+/)) print substr(slug, RSTART, RLENGTH) }'
}

# main — fetch the open backlog and print the next concurrent batch. Pass through any
# --inflight flags so the hub/skill can feed in the scopes of live spokes, and seed
# --repo-root with the hub checkout so the undeclared-dependency lint can tell a
# to-be-created scope path from an existing file. An explicit --repo-root in "$@"
# (last-wins in the arg loop) still overrides this default.
#
# In an explain mode (#223) — and under --route (#278), which likewise has to attribute a
# pack to a specific LIVE spoke — the in-flight issue NUMBERS are derived from the worktree
# list and fed in as --inflight-issue flags, so `batch-plan --explain` / `--route` reflects
# live work standalone; the pure plan_from_json stays network-free for the tests.
main() {
  local root
  root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  case " $* " in
    *" --explain "* | *" --explain-labels "* | *" --route "*)
      local ifargs=() num
      while IFS= read -r num; do
        [ -n "$num" ] && ifargs+=("--inflight-issue" "$num")
      done < <(_batch_inflight_issue_nums)
      fetch_issues | plan_from_json --repo-root "$root" ${ifargs[@]+"${ifargs[@]}"} "$@"
      ;;
    *)
      fetch_issues | plan_from_json --repo-root "$root" "$@"
      ;;
  esac
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
