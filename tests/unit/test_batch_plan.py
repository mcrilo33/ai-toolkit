"""Unit tests for shared/skills/hub/scripts/batch-plan.sh (issue #70).

With `Scope:` + native blocked-by on every issue, computing the next set of issues
that can safely run *concurrently* is a mechanical graph computation — so it stays
SCRIPTED (no LLM in the control plane). batch-plan.sh:

  * READ — one `gh api graphql` round-trip for every open issue's `body` (the
    `Scope:` line), its `labels`, and its `blockedBy` connection.
  * ELIGIBILITY — an issue is *ready* when all its blockers are closed and it does
    not carry the `hold` label (a held issue is staged out of every batch).
  * PRIORITY — a `priority`-labelled ready issue sorts ahead of non-priority ones,
    then by critical-path depth: the longest blocked-by chain rooted at the issue, so
    the longest serial tail is unblocked earliest (minimizes makespan). Ties break on
    direct-dependent count, then issue number.
  * GREEDY DISJOINT-SCOPE PACK — walk ready issues in priority order; add to the
    batch only when its `Scope:` is disjoint from every batch member AND every
    in-flight spoke. `Scope: *` / a missing line ⇒ exclusive (never batched).

These tests drive the pure planner by sourcing the script and piping a hand-built
fixture graph into `plan_from_json` (a source-guard keeps `main` from fetching on
import), plus one end-to-end test that mocks `gh` through `main`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BATCH_PLAN = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "batch-plan.sh"
SKILLS_DIR = REPO_ROOT / "shared" / "skills"


def _node(
    number: int,
    scope: str | None,
    blocked_by: list[tuple[int, str]] | None = None,
    *,
    split: str | None = None,
    labels: list[str] | None = None,
) -> dict:
    """Build one graphql issue node: a Scope: body + blockedBy nodes (number, state).

    When `labels` is given, a `labels.nodes` array of `{name}` objects is attached,
    mirroring the graphql shape. Omitting it leaves the key absent, exactly as a
    node with no labels arrives — so unlabelled fixtures stay byte-identical.
    """
    body = "Some description.\n"
    if scope is not None:
        body += f"Scope: {scope}\n"
    if split is not None:
        body += f"Split: {split}\n"
    nodes = [{"number": n, "state": s} for n, s in (blocked_by or [])]
    node = {"number": number, "body": body, "blockedBy": {"nodes": nodes}}
    if labels is not None:
        node["labels"] = {"nodes": [{"name": name} for name in labels]}
    return node


def _run_plan(
    nodes: list[dict],
    *,
    inflight: list[str] | None = None,
    cap: int | None = None,
    repo_root: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Pipe a fixture graph into plan_from_json and return the completed process."""
    env = {**os.environ}
    args = ""
    for spoke in inflight or []:
        args += f" --inflight {json_quote(spoke)}"
    if cap is not None:
        args += f" --cap {cap}"
    if repo_root is not None:
        args += f" --repo-root {json_quote(str(repo_root))}"
    return subprocess.run(
        ["bash", "-c", f'source "{BATCH_PLAN}"; plan_from_json{args}'],
        input=json.dumps(nodes),
        capture_output=True,
        text=True,
        env=env,
    )


def _plan(
    nodes: list[dict],
    *,
    inflight: list[str] | None = None,
    cap: int | None = None,
    repo_root: Path | str | None = None,
) -> list[int]:
    """Pipe a fixture graph into plan_from_json and return the batch as issue numbers."""
    proc = _run_plan(nodes, inflight=inflight, cap=cap, repo_root=repo_root)
    assert proc.returncode == 0, proc.stderr
    return [int(tok) for tok in proc.stdout.split()]


def json_quote(s: str) -> str:
    # Shell-safe single-quote wrap for the --inflight value.
    return "'" + s.replace("'", "'\\''") + "'"


# ── the acceptance fixture: deps + scope overlap + exclusivity ────────────────


def _acceptance_graph() -> list[dict]:
    # #1 -> #2 -> #3 is a serial chain (each blockedBy the previous, all OPEN), so
    # only #1 is ready and its critical-path depth is 3 (the longest tail).
    # #4 is ready but its scope (a.py) collides with #1 -> excluded (lower priority).
    # #5 is ready and disjoint (d.py) -> joins the batch.
    # #6 is ready but Scope: * is exclusive -> never batched alongside others.
    return [
        _node(1, "a.py"),
        _node(2, "b.py", blocked_by=[(1, "OPEN")]),
        _node(3, "c.py", blocked_by=[(2, "OPEN")]),
        _node(4, "a.py"),
        _node(5, "d.py"),
        _node(6, "*"),
    ]


def test_acceptance_batch_honors_criticalpath_scope_and_exclusivity() -> None:
    batch = _plan(_acceptance_graph())

    # Critical-path depth puts #1 first; #5 is the only disjoint ready peer.
    # #2/#3 are blocked, #4 collides with #1, #6 is exclusive.
    assert batch == [1, 5]


def test_blocked_issues_are_not_ready() -> None:
    batch = _plan(_acceptance_graph())

    assert 2 not in batch and 3 not in batch


def test_scope_collision_excludes_lower_priority_issue() -> None:
    batch = _plan(_acceptance_graph())

    assert 4 not in batch, "#4 shares scope a.py with the higher-priority #1"


def test_star_scope_is_exclusive() -> None:
    batch = _plan(_acceptance_graph())

    assert 6 not in batch, "Scope: * is exclusive and must never batch with others"


def test_exclusive_issue_runs_alone_when_it_is_the_only_ready_one() -> None:
    batch = _plan([_node(9, "*")])

    assert batch == [9], "an exclusive issue is still dispatched, just by itself"


# ── eligibility: a closed blocker unblocks the issue ──────────────────────────


def test_closed_blocker_makes_issue_ready() -> None:
    nodes = [_node(7, "x.py", blocked_by=[(100, "CLOSED")])]

    batch = _plan(nodes)

    assert batch == [7], "all blockers closed ⇒ ready"


def test_mixed_blockers_block_when_any_is_open() -> None:
    nodes = [_node(8, "x.py", blocked_by=[(100, "CLOSED"), (101, "OPEN")])]

    batch = _plan(nodes)

    assert batch == [], "an issue with any open blocker is not ready"


# ── in-flight scopes are honored ──────────────────────────────────────────────


def test_inflight_scope_excludes_colliding_ready_issue() -> None:
    # An in-flight spoke already owns d.py, so #5 can no longer join.
    batch = _plan(_acceptance_graph(), inflight=["d.py"])

    assert 5 not in batch
    assert batch == [1]


def test_inflight_exclusive_spoke_blocks_the_whole_batch() -> None:
    # An exclusive in-flight spoke (Scope: *) collides with everything.
    batch = _plan([_node(1, "a.py"), _node(5, "d.py")], inflight=["*"])

    assert batch == []


# ── hold label: staged issues are never dispatched (issue #147) ───────────────


def test_hold_label_excludes_issue_from_batch() -> None:
    # A ready, disjoint issue carrying `hold` is staged, never dispatched — even
    # though nothing blocks it and its scope collides with no live work.
    nodes = [_node(1, "a.py", labels=["hold"]), _node(2, "b.py")]

    batch = _plan(nodes)

    assert 1 not in batch, "a hold-labelled issue must never be dispatched"
    assert batch == [2]


def test_hold_excluded_but_disjoint_peers_still_batch() -> None:
    # The held issue drops out; its disjoint non-held peers still pack together,
    # proving hold removes only the held issue, not the concurrency around it.
    nodes = [_node(1, "a.py", labels=["hold"]), _node(2, "b.py"), _node(3, "c.py")]

    batch = _plan(nodes)

    assert 1 not in batch
    assert batch == [2, 3]


def test_unrelated_label_does_not_change_dispatch() -> None:
    # A label other than hold/priority leaves eligibility and ordering untouched.
    nodes = [_node(1, "a.py", labels=["enhancement"])]

    batch = _plan(nodes)

    assert batch == [1]


def test_hold_label_is_case_insensitive_among_other_labels() -> None:
    # The label match is case-folded and holds regardless of sibling labels.
    nodes = [_node(1, "a.py", labels=["enhancement", "HOLD"]), _node(2, "b.py")]

    batch = _plan(nodes)

    assert batch == [2], "a HOLD-labelled issue is excluded even alongside other labels"


# ── priority label: dispatched ahead of equally-eligible peers (issue #147) ───


def test_priority_label_outranks_higher_criticalpath_depth() -> None:
    # #20 has the deeper critical-path tail (it blocks #21), so by depth alone it
    # ranks first — but #10 carries `priority` and they collide on shared.py, so
    # only one is picked. Priority must win the seat.
    nodes = [
        _node(10, "shared.py", labels=["priority"]),
        _node(20, "shared.py"),
        _node(21, "a.py", blocked_by=[(20, "OPEN")]),
    ]

    batch = _plan(nodes)

    assert batch[0] == 10, "a priority issue outranks a deeper-critical-path peer"
    assert 20 not in batch, "#20 collides on shared.py with the higher-priority #10"


def test_priority_issue_still_batches_with_disjoint_peer() -> None:
    # Priority reorders but does not serialize: a priority issue and a disjoint
    # non-priority peer are BOTH dispatched, the priority one ordered first even
    # though its higher number would otherwise put it last.
    nodes = [_node(2, "a.py"), _node(9, "b.py", labels=["priority"])]

    batch = _plan(nodes)

    assert set(batch) == {2, 9}, "disjoint concurrency is preserved"
    assert batch[0] == 9, "the priority issue is ordered first despite the higher number"


def test_priority_does_not_resurrect_a_held_issue() -> None:
    # hold is a pre-sort eligibility filter; priority only reorders the survivors,
    # so a held+priority issue stays excluded rather than jumping to the front.
    nodes = [_node(1, "a.py", labels=["hold", "priority"]), _node(2, "b.py")]

    batch = _plan(nodes)

    assert batch == [2], "a held issue is not dispatched even when also labelled priority"


# ── merge-candidate lint: colliding-scope serialized chains warn (issue #125) ─


def test_merge_candidates_warn_for_colliding_open_chain() -> None:
    # #2 is blockedBy #1 and both touch a.py — strictly serialized, zero parallelism.
    nodes = [_node(1, "a.py"), _node(2, "a.py", blocked_by=[(1, "OPEN")])]

    proc = _run_plan(nodes)

    assert proc.returncode == 0, proc.stderr
    assert "merge candidates: #1 → #2" in proc.stderr
    assert "a.py" in proc.stderr


def test_merge_candidates_silent_for_disjoint_chain() -> None:
    # The acceptance chain #1 → #2 → #3 has disjoint scopes (a.py, b.py, c.py), so the
    # #125 CHAIN lint (`strictly serialized`) stays silent. (The ready pair #1/#4
    # collides on a.py — the unchained #167 lint covers that, not this assertion.)
    proc = _run_plan(_acceptance_graph())

    assert proc.returncode == 0, proc.stderr
    assert "strictly serialized" not in proc.stderr


def test_merge_candidates_chain_coalesces_into_one_line() -> None:
    # A three-issue colliding chain prints ONE proposal line, not one per edge.
    nodes = [
        _node(1, "a.py"),
        _node(2, "a.py", blocked_by=[(1, "OPEN")]),
        _node(3, "a.py", blocked_by=[(2, "OPEN")]),
    ]

    proc = _run_plan(nodes)

    warnings = [line for line in proc.stderr.splitlines() if "merge candidates" in line]
    assert len(warnings) == 1
    assert "#1 → #2 → #3" in warnings[0]


def test_merge_candidates_never_alter_batch_or_exit_code() -> None:
    # The lint is detection-only: same batch and exit code as the silent case.
    nodes = [_node(1, "a.py"), _node(2, "a.py", blocked_by=[(1, "OPEN")])]

    proc = _run_plan(nodes)

    assert proc.returncode == 0
    assert [int(tok) for tok in proc.stdout.split()] == [1]


def test_merge_candidates_ignore_closed_blockers() -> None:
    # A closed blocker is already satisfied — no serialized chain left to merge.
    # (The OPEN-only fetch never carries closed issues, so the blocker is absent
    # from the payload by construction — exactly the shape production sees.)
    nodes = [_node(7, "x.py", blocked_by=[(100, "CLOSED")])]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


def test_merge_candidates_report_disjoint_chains_separately_and_ordered() -> None:
    # Two unrelated colliding chains ⇒ two lines, lowest component member first
    # regardless of input order (deterministic content-based ordering).
    nodes = [
        _node(11, "b.py"),
        _node(12, "b.py", blocked_by=[(11, "OPEN")]),
        _node(2, "a.py"),
        _node(3, "a.py", blocked_by=[(2, "OPEN")]),
    ]

    proc = _run_plan(nodes)

    warnings = [line for line in proc.stderr.splitlines() if "merge candidates" in line]
    assert len(warnings) == 2
    assert "#2 → #3" in warnings[0]
    assert "#11 → #12" in warnings[1]


def test_merge_candidates_terminate_on_blockedby_cycle() -> None:
    # A malformed mutual blocked-by must not hang or crash the lint.
    nodes = [
        _node(1, "a.py", blocked_by=[(2, "OPEN")]),
        _node(2, "a.py", blocked_by=[(1, "OPEN")]),
    ]

    proc = _run_plan(nodes)

    assert proc.returncode == 0, proc.stderr
    warnings = [line for line in proc.stderr.splitlines() if "merge candidates" in line]
    assert len(warnings) == 1


def test_merge_candidates_label_exclusive_scope_chains() -> None:
    # `Scope: *` collides with everything but has no named tokens to report.
    nodes = [_node(1, "*"), _node(2, "*", blocked_by=[(1, "OPEN")])]

    proc = _run_plan(nodes)

    assert "merge candidates: #1 → #2" in proc.stderr
    assert "exclusive scope" in proc.stderr


# ── Split: intentional — the reviewable escape hatch silences the lint ────────


def test_split_marker_suppresses_the_chain_warning() -> None:
    # A deliberate split records its reasoning in the issue body and is not nagged.
    nodes = [
        _node(1, "a.py", split="intentional — mid-chain rollback line"),
        _node(2, "a.py", blocked_by=[(1, "OPEN")]),
    ]

    proc = _run_plan(nodes)

    assert proc.returncode == 0, proc.stderr
    assert "merge candidates" not in proc.stderr
    assert [int(tok) for tok in proc.stdout.split()] == [1], "suppression must not touch the batch"


def test_split_marker_in_any_chain_member_suppresses() -> None:
    # The marker works from ANY issue of the chain, not just the head.
    nodes = [
        _node(1, "a.py"),
        _node(2, "a.py", blocked_by=[(1, "OPEN")], split="intentional — standalone value"),
    ]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


def test_split_marker_suppresses_only_its_own_chain() -> None:
    # Two colliding chains, one marked: the other must still warn. (Chain numbers
    # share no prefix so the absence asserts can't substring-match, e.g. #1 in #11.)
    nodes = [
        _node(1, "a.py", split="intentional — shelf-life"),
        _node(2, "a.py", blocked_by=[(1, "OPEN")]),
        _node(31, "b.py"),
        _node(32, "b.py", blocked_by=[(31, "OPEN")]),
    ]

    proc = _run_plan(nodes)

    warnings = [line for line in proc.stderr.splitlines() if "merge candidates" in line]
    assert len(warnings) == 1
    assert "#31 → #32" in warnings[0]
    assert "#1" not in warnings[0]
    assert "#2" not in warnings[0]


@pytest.mark.parametrize("marker", ["INTENTIONAL — caps", "intentional"])
def test_split_marker_parses_leniently(marker: str) -> None:
    # Casing is normalized and the `— <why>` tail is optional; both forms suppress.
    nodes = [
        _node(1, "a.py", split=marker),
        _node(2, "a.py", blocked_by=[(1, "OPEN")]),
    ]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


def test_split_marker_without_space_after_colon_suppresses() -> None:
    # `Split:intentional` (no space) still parses — the value is stripped first.
    node = _node(1, "a.py")
    node["body"] += "Split:intentional\n"
    nodes = [node, _node(2, "a.py", blocked_by=[(1, "OPEN")])]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


def test_split_marker_requires_intentional_value() -> None:
    # Only the documented `Split: intentional` form suppresses — not any Split: line.
    nodes = [
        _node(1, "a.py", split="maybe"),
        _node(2, "a.py", blocked_by=[(1, "OPEN")]),
    ]

    proc = _run_plan(nodes)

    assert "merge candidates: #1 → #2" in proc.stderr


# ── unchained merge-candidate lint: ready colliding cluster, no deps (issue #167) ─
# The #125 lint fires only on blocked-by CHAINS. A cluster of ready, mutually-
# unblocked issues sharing a scope (e.g. #158/#160/#161/#162) is serialized by the
# greedy pack but never flagged. This lint groups the ready set by concrete-token
# overlap and prints one umbrella-candidate hint per cluster of ≥2 — report-only,
# stderr, and structurally disjoint from #125 (a #125 component always contains a
# non-ready child, so no cluster is reported by both).


def test_unchained_merge_candidates_warn_for_ready_colliding_cluster() -> None:
    # Four ready, dependency-free issues on an identical scope — the #165 case.
    nodes = [_node(n, "langfuse_spoke_tree.py") for n in (158, 160, 161, 162)]

    proc = _run_plan(nodes)

    assert proc.returncode == 0, proc.stderr
    warnings = [line for line in proc.stderr.splitlines() if "merge candidates" in line]
    assert len(warnings) == 1
    assert "#158 #160 #161 #162" in warnings[0]
    assert "ready & unchained" in warnings[0]
    assert "langfuse_spoke_tree.py" in warnings[0]


def test_unchained_merge_candidates_silent_for_disjoint_ready() -> None:
    # Independent, disjoint-scope ready issues parallelize — nothing to merge.
    nodes = [_node(1, "a.py"), _node(2, "b.py"), _node(3, "c.py")]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


def test_unchained_single_ready_issue_no_hint() -> None:
    # A lone ready issue is not a cluster.
    nodes = [_node(1, "a.py")]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


def test_unchained_not_double_reported_with_chain() -> None:
    # A colliding chain is the #125 case: it prints the chain line and NOT an
    # unchained line, even though its head (#1) is ready.
    nodes = [_node(1, "a.py"), _node(2, "a.py", blocked_by=[(1, "OPEN")])]

    proc = _run_plan(nodes)

    warnings = [line for line in proc.stderr.splitlines() if "merge candidates" in line]
    assert len(warnings) == 1
    assert "#1 → #2" in warnings[0]
    assert "ready & unchained" not in proc.stderr


def test_unchained_and_chain_reported_separately() -> None:
    # A #125 chain (a.py) and an unchained ready cluster (b.py) each print one line.
    nodes = [
        _node(1, "a.py"),
        _node(2, "a.py", blocked_by=[(1, "OPEN")]),
        _node(31, "b.py"),
        _node(32, "b.py"),
    ]

    proc = _run_plan(nodes)

    chain = [line for line in proc.stderr.splitlines() if "strictly serialized" in line]
    unchained = [line for line in proc.stderr.splitlines() if "ready & unchained" in line]
    assert len(chain) == 1 and "#1 → #2" in chain[0]
    assert len(unchained) == 1
    assert "#31 #32" in unchained[0]


def test_unchained_lint_is_detection_only() -> None:
    # The lint fires but leaves the greedy-pack batch and exit code untouched: two
    # ready issues colliding on a.py still dispatch exactly one.
    nodes = [_node(1, "a.py"), _node(2, "a.py")]

    proc = _run_plan(nodes)

    assert proc.returncode == 0
    assert [int(tok) for tok in proc.stdout.split()] == [1]
    assert "ready & unchained" in proc.stderr


def test_unchained_reports_collision_across_separate_chains() -> None:
    # #1 and #5 each head their own colliding chain (#125 reports each pair), but they
    # also collide with EACH OTHER on a.py — a cross-chain ready collision #125 never
    # pairs. The unchained lint must still surface #1/#5 rather than drop both as
    # already-flagged.
    nodes = [
        _node(1, "a.py"),
        _node(2, "a.py", blocked_by=[(1, "OPEN")]),
        _node(5, "a.py"),
        _node(6, "a.py", blocked_by=[(5, "OPEN")]),
    ]

    proc = _run_plan(nodes)

    unchained = [line for line in proc.stderr.splitlines() if "ready & unchained" in line]
    assert len(unchained) == 1
    assert "#1 #5" in unchained[0]
    assert "a.py" in unchained[0]


def test_unchained_cluster_within_one_chain_is_not_double_reported() -> None:
    # #1 and #2 are both ready and both block the same non-ready #3, all on a.py, so
    # #125 already reports the whole {1,2,3} component. The ready pair {1,2} is a
    # subset of that component ⇒ the unchained lint stays silent (no double-report).
    nodes = [
        _node(1, "a.py"),
        _node(2, "a.py"),
        _node(3, "a.py", blocked_by=[(1, "OPEN"), (2, "OPEN")]),
    ]

    proc = _run_plan(nodes)

    assert "ready & unchained" not in proc.stderr
    assert "strictly serialized" in proc.stderr, "#125 still reports the chain component"


def test_unchained_split_intentional_opts_out_only_the_marked_issue() -> None:
    # #1 records a deliberate split, but its unmarked peers #2/#3 are still redundant
    # duplicates on a.py — the marker opts #1 out of candidacy without silencing #2/#3.
    nodes = [
        _node(1, "a.py", split="intentional — kept apart on purpose"),
        _node(2, "a.py"),
        _node(3, "a.py"),
    ]

    proc = _run_plan(nodes)

    unchained = [line for line in proc.stderr.splitlines() if "ready & unchained" in line]
    assert len(unchained) == 1
    assert "#2 #3" in unchained[0]
    assert "#1" not in unchained[0]


def test_unchained_split_intentional_suppresses_lone_pair() -> None:
    # With only the marked issue and one peer, opting #1 out leaves a cluster of one,
    # so the deliberate split still silences the hint.
    nodes = [_node(1, "a.py", split="intentional — kept apart on purpose"), _node(2, "a.py")]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


def test_unchained_excludes_held_issue() -> None:
    # A held issue is staged out of dispatch, so it is not a current merge candidate:
    # only the non-held peer remains, leaving a cluster of one.
    nodes = [_node(1, "a.py", labels=["hold"]), _node(2, "a.py")]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


def test_unchained_excludes_blocked_issue() -> None:
    # #2 has an OPEN blocker (#99, absent from the OPEN payload but still open), so it
    # is not ready and cannot join the ready cluster with #1.
    nodes = [_node(1, "a.py"), _node(2, "a.py", blocked_by=[(99, "OPEN")])]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


def test_unchained_pair_reports_only_shared_token() -> None:
    # Partial overlap: the hint names the shared token, not each issue's private files.
    nodes = [_node(1, "a.py shared.py"), _node(2, "shared.py b.py")]

    proc = _run_plan(nodes)

    warnings = [line for line in proc.stderr.splitlines() if "merge candidates" in line]
    assert len(warnings) == 1
    assert "shared.py" in warnings[0]
    assert "a.py" not in warnings[0]
    assert "b.py" not in warnings[0]


def test_unchained_exclusive_star_not_clustered() -> None:
    # Two `Scope: *` ready issues share no named token, so the unchained lint (which
    # names the shared token) leaves them alone.
    nodes = [_node(1, "*"), _node(2, "*")]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


# ── undeclared-dependency lint: shared not-yet-created scope path (issue #148) ─
# A scope token naming a path ABSENT under --repo-root is a file an issue will
# CREATE; two open issues both listing it, with no blocked-by edge, is a probable
# undeclared producer→consumer dependency (one creates, the other consumes). The
# lint fires ONLY on the create direction (an absent path); a shared EXISTING file
# is mere overlap the scheduler already serializes and must stay silent, else the
# lint would nudge authors toward the fake edges issue-hygiene forbids. Detection-
# only, stderr, and OFF (byte-identical) whenever --repo-root is omitted.


def _UNDECLARED() -> str:
    return "possible undeclared dependency"


def test_undeclared_dependency_warns_for_shared_missing_scope(tmp_path: Path) -> None:
    # newmod.py exists nowhere under root ⇒ #1 creates it, #2 also lists it, no edge.
    nodes = [_node(1, "newmod.py"), _node(2, "newmod.py a.py")]

    proc = _run_plan(nodes, repo_root=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert f"⚠ {_UNDECLARED()}: #1 / #2" in proc.stderr
    assert "newmod.py" in proc.stderr


def test_undeclared_dependency_silent_when_file_exists(tmp_path: Path) -> None:
    # Same shape, but the shared path EXISTS under root ⇒ mere overlap of an existing
    # file, not a producer→consumer edge. Must stay silent (overlap ≠ dependency).
    (tmp_path / "newmod.py").write_text("x")
    nodes = [_node(1, "newmod.py"), _node(2, "newmod.py")]

    proc = _run_plan(nodes, repo_root=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert _UNDECLARED() not in proc.stderr


def test_undeclared_dependency_silent_when_edge_declared(tmp_path: Path) -> None:
    # #2 is blocked-by #1 on the shared missing path — the ordering is already declared.
    nodes = [_node(1, "newmod.py"), _node(2, "newmod.py", blocked_by=[(1, "OPEN")])]

    proc = _run_plan(nodes, repo_root=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert _UNDECLARED() not in proc.stderr


def test_undeclared_dependency_silent_for_transitive_edge(tmp_path: Path) -> None:
    # #3 is transitively blocked by #1 (#3→#2→#1); the create/consume pair #1/#3 is
    # already ordered, so no warning despite sharing the missing z.py.
    nodes = [
        _node(1, "z.py"),
        _node(2, "b.py", blocked_by=[(1, "OPEN")]),
        _node(3, "z.py", blocked_by=[(2, "OPEN")]),
    ]

    proc = _run_plan(nodes, repo_root=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert _UNDECLARED() not in proc.stderr


def test_undeclared_dependency_suppressed_by_split_intentional(tmp_path: Path) -> None:
    nodes = [
        _node(1, "newmod.py", split="intentional — staged rollout"),
        _node(2, "newmod.py"),
    ]

    proc = _run_plan(nodes, repo_root=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert _UNDECLARED() not in proc.stderr


def test_undeclared_dependency_off_without_repo_root() -> None:
    # No --repo-root ⇒ create-detection disabled; the pure planner path is unchanged.
    nodes = [_node(1, "newmod.py"), _node(2, "newmod.py")]

    proc = _run_plan(nodes)

    assert _UNDECLARED() not in proc.stderr


def test_undeclared_dependency_is_detection_only(tmp_path: Path) -> None:
    # A warning fires, but the batch on stdout and the exit code are byte-identical to
    # the --repo-root-omitted run — the lint never touches dispatch.
    nodes = [_node(1, "newmod.py"), _node(2, "newmod.py")]

    warned = _run_plan(nodes, repo_root=tmp_path)
    silent = _run_plan(nodes)

    assert f"⚠ {_UNDECLARED()}" in warned.stderr
    assert _UNDECLARED() not in silent.stderr
    assert warned.returncode == silent.returncode == 0
    assert warned.stdout == silent.stdout


def test_undeclared_dependency_ignores_glob_tokens(tmp_path: Path) -> None:
    # A glob scope token is not a concrete path — never treated as a to-be-created file.
    nodes = [_node(1, "src/*.py"), _node(2, "src/*.py")]

    proc = _run_plan(nodes, repo_root=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert _UNDECLARED() not in proc.stderr


def test_undeclared_dependency_needs_two_issues(tmp_path: Path) -> None:
    # A single issue creating a new file is normal — no consumer, no warning.
    nodes = [_node(1, "newmod.py"), _node(2, "other.py")]

    proc = _run_plan(nodes, repo_root=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert _UNDECLARED() not in proc.stderr


# ── scopeless-ready warning: a missing Scope: line is flagged (issue #217) ────
# A MISSING `Scope:` line marks the issue exclusive (runs alone, never batched) —
# correct fail-closed semantics, but silent, so a whole drain can serialize on a batch
# of scope-less issues unnoticed until a human spots it. This warning names each ready
# issue with no Scope: line so the serialization is diagnosable from the plan log (the
# no-silent-caps rule). `Scope: *` is a DELIBERATE exclusive and stays silent — only the
# accidental missing-line case is flagged. Detection-only, stderr.


def _SCOPELESS() -> str:
    return "has no Scope: line — treated exclusive"


def test_scopeless_ready_issue_warns() -> None:
    # A ready issue with no Scope: line at all is flagged by number.
    proc = _run_plan([_node(1, None)])

    assert proc.returncode == 0, proc.stderr
    assert f"#1 {_SCOPELESS()}" in proc.stderr


def test_star_scope_is_not_a_scopeless_warning() -> None:
    # `Scope: *` is a deliberate exclusive — it must NOT trip the missing-line warning.
    proc = _run_plan([_node(1, "*")])

    assert proc.returncode == 0, proc.stderr
    assert _SCOPELESS() not in proc.stderr


def test_scoped_issue_does_not_warn_scopeless() -> None:
    # A concrete Scope: line is the normal case — silent.
    proc = _run_plan([_node(1, "a.py")])

    assert proc.returncode == 0, proc.stderr
    assert _SCOPELESS() not in proc.stderr


def test_scopeless_warning_only_for_ready_issues() -> None:
    # #2 is blocked (open blocker) and scope-less; it is not ready, so it is not warned
    # about — only the ready-but-scope-less #1 is.
    nodes = [_node(1, None), _node(2, None, blocked_by=[(99, "OPEN")])]

    proc = _run_plan(nodes)

    assert f"#1 {_SCOPELESS()}" in proc.stderr
    assert f"#2 {_SCOPELESS()}" not in proc.stderr


def test_scopeless_warning_names_each_ready_issue() -> None:
    # Two ready scope-less issues ⇒ one warning line each, by number.
    proc = _run_plan([_node(1, None), _node(2, None)])

    warned = [line for line in proc.stderr.splitlines() if _SCOPELESS() in line]
    assert len(warned) == 2
    assert any("#1" in line for line in warned)
    assert any("#2" in line for line in warned)


def test_scopeless_warning_is_detection_only() -> None:
    # The warning fires but the scope-less issue is still dispatched (alone) and the
    # exit code is unchanged — detection-only, like the other lints.
    proc = _run_plan([_node(1, None)])

    assert proc.returncode == 0
    assert [int(t) for t in proc.stdout.split()] == [1]
    assert _SCOPELESS() in proc.stderr


def test_empty_scope_label_warns_like_a_missing_line() -> None:
    # A bare `Scope:` label with no tokens declares nothing usable — the same
    # accidental-exclusive silent serialization as a missing line, so it is flagged.
    node = _node(1, None)
    node["body"] += "Scope:  \n"

    proc = _run_plan([node])

    assert proc.returncode == 0, proc.stderr
    assert f"#1 {_SCOPELESS()}" in proc.stderr


def test_scopeless_warning_excludes_held_issue() -> None:
    # A `hold`-labelled scope-less issue is staged out of the ready set, so it is not
    # a current merge candidate and is not warned about — only the ready peer is.
    nodes = [_node(1, None, labels=["hold"]), _node(2, None)]

    proc = _run_plan(nodes)

    assert f"#1 {_SCOPELESS()}" not in proc.stderr
    assert f"#2 {_SCOPELESS()}" in proc.stderr


# ── tie-break: equal depth ⇒ more direct dependents wins ──────────────────────


def test_tiebreak_prefers_more_direct_dependents() -> None:
    # #10 and #20 are both ready with depth 2, but #10 directly unblocks two issues
    # while #20 unblocks one. They share scope, so only the higher-priority one is
    # picked — proving #10 (more direct dependents) outranks #20.
    nodes = [
        _node(10, "shared.py"),
        _node(20, "shared.py"),
        _node(11, "a.py", blocked_by=[(10, "OPEN")]),
        _node(12, "b.py", blocked_by=[(10, "OPEN")]),
        _node(21, "c.py", blocked_by=[(20, "OPEN")]),
    ]

    batch = _plan(nodes)

    assert batch[0] == 10, "equal depth ⇒ the issue with more direct dependents ranks first"
    assert 20 not in batch, "#20 collides on shared.py with the higher-priority #10"


# ── concurrency cap: bound live spokes across dispatch (issue #151) ───────────
# The hub batch/dispatch path had no ceiling, so a wide batch could starve the box
# and the co-located Langfuse. `--cap N` truncates the batch so the total concurrent
# spokes (already in-flight + newly batched) never exceeds N. It is a pure output
# truncation layered on top of the greedy pack — priority order is preserved and no
# extra issues are ever added. Absent / `--cap 0` keeps the historical unlimited batch.


def _disjoint_ready(*nums: int) -> list[dict]:
    # One ready, no-dependency issue per number, each on its own file so they pack
    # together — the cap, not scope-disjointness, is what bounds the batch here.
    return [_node(n, f"f{n}.py") for n in nums]


def test_cap_truncates_batch_to_the_configured_ceiling() -> None:
    # Three disjoint ready issues would all batch; --cap 2 keeps only the top two
    # in priority order (equal depth ⇒ issue-number asc).
    batch = _plan(_disjoint_ready(1, 2, 3), cap=2)

    assert batch == [1, 2], "the cap keeps the two highest-priority ready issues"


def test_cap_accounts_for_inflight_spokes() -> None:
    # One spoke already running (f9.py) consumes a slot: with --cap 2 only ONE more
    # of the two disjoint ready issues may dispatch, not two.
    batch = _plan(_disjoint_ready(1, 2), inflight=["f9.py"], cap=2)

    assert batch == [1], "cap counts in-flight spokes, leaving a single free slot"


def test_cap_full_from_inflight_yields_empty_batch() -> None:
    # The cap is already saturated by live spokes ⇒ dispatch nothing this round.
    batch = _plan(_disjoint_ready(1), inflight=["f8.py", "f9.py"], cap=2)

    assert batch == [], "no free slots ⇒ empty batch"


def test_cap_larger_than_batch_is_a_noop() -> None:
    batch = _plan(_disjoint_ready(1, 2), cap=10)

    assert batch == [1, 2], "a cap above the batch size changes nothing"


def test_cap_zero_is_unlimited() -> None:
    # `--cap 0` is the explicit "no ceiling" value (the dispatch default when unset).
    batch = _plan(_disjoint_ready(1, 2, 3), cap=0)

    assert batch == [1, 2, 3]


def test_absent_cap_preserves_unlimited_batch() -> None:
    batch = _plan(_disjoint_ready(1, 2, 3))

    assert batch == [1, 2, 3], "no --cap flag keeps the historical unbounded batch"


def test_cap_does_not_reorder_or_add_issues() -> None:
    # The cap only removes from the tail of the priority-ordered batch; a scope
    # collision still excludes #4 and exclusivity still bars #6 even under a cap.
    batch = _plan(_acceptance_graph(), cap=5)

    assert batch == [1, 5], "cap ≥ batch leaves the greedy pack untouched"


# ── end-to-end: mock gh through main (fetch → plan) ───────────────────────────


def test_main_fetches_via_gh_and_prints_batch(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    nodes = _acceptance_graph()
    payload = json.dumps(nodes)
    gh = bindir / "gh"
    # `gh repo view` → owner/name; `gh api graphql` → the issues node array (what
    # fetch_issues extracts with --jq). The fake ignores --jq and just emits it.
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "repo" ] && [ "$2" = "view" ]; then echo "octo ai-toolkit"; exit 0; fi\n'
        'if [ "$1" = "api" ] && [ "$2" = "graphql" ]; then cat "$PAYLOAD"; exit 0; fi\n'
        "exit 1\n"
    )
    gh.chmod(0o755)
    (tmp_path / "payload.json").write_text(payload)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PAYLOAD": str(tmp_path / "payload.json"),
    }
    proc = subprocess.run(["bash", str(BATCH_PLAN)], capture_output=True, text=True, env=env)

    assert proc.returncode == 0, proc.stderr
    assert [int(t) for t in proc.stdout.split()] == [1, 5]


# ── doc guards: the skill is registered and dispatches ────────────────────────


def test_next_batch_skill_registered_in_metadata() -> None:
    meta = (SKILLS_DIR / "metadata.yml").read_text()

    assert "next-batch:" in meta, "/next-batch must be registered in metadata.yml"


def test_start_task_skill_documents_merge_heuristic() -> None:
    # Filing time is the decision point: start-task must carry the heuristic + escape hatch.
    skill = (SKILLS_DIR / "start-task" / "SKILL.md").read_text()

    assert "one issue with subtasks" in skill, "start-task must teach the merge heuristic"
    assert "Split: intentional" in skill, "start-task must document the escape hatch"


def test_workflow_rule_documents_merge_heuristic() -> None:
    # The workflow rule feeds /afk's answerer and DEFINE sessions the same convention.
    rule = (REPO_ROOT / "shared" / "rules" / "workflow.md").read_text()

    assert "one issue with subtasks" in rule, "workflow.md must carry the filing heuristic"
    assert "Split: intentional" in rule, "workflow.md must document the escape hatch"


def test_next_batch_skill_documents_dispatch() -> None:
    skill = (SKILLS_DIR / "next-batch" / "SKILL.md").read_text().lower()

    assert "batch-plan.sh" in skill, "the skill must run batch-plan.sh"
    assert "worktree-new.sh" in skill, "the skill must dispatch via worktree-new.sh"


# ── doc guards: every filed issue carries Scope:/Gate: (issue #217) ────────────
# Both the footer convention (author-facing skill docs) and its rule home
# (issue-hygiene) must teach `Scope:` + `Gate:` with their semantics, so the
# missing-scope silent-serialization that motivated #217 stops being tribal knowledge.


def test_github_issues_templates_show_scope_and_gate_footer() -> None:
    # The issue templates are where bodies get authored — they must show BOTH footer
    # lines with semantics, not just Scope:.
    templates = (SKILLS_DIR / "github-issues" / "references" / "templates.md").read_text()

    assert "Scope:" in templates, "templates must show the Scope: footer line"
    assert "Gate:" in templates, "templates must show the Gate: footer line"
    assert "none" in templates and "plan" in templates, (
        "the Gate: semantics (none | plan) must be spelled out"
    )
    assert "exclusive" in templates, "the Scope: exclusivity semantics must be spelled out"


def test_start_task_shows_scope_and_gate_footer() -> None:
    # start-task's issue-drafting step must carry both footer lines with semantics.
    skill = (SKILLS_DIR / "start-task" / "SKILL.md").read_text()

    assert "Gate:" in skill and "Scope:" in skill, "start-task must show both footer lines"
    assert "exclusive" in skill, "start-task must spell out the missing ⇒ exclusive semantics"


def test_issue_hygiene_documents_gate_line() -> None:
    # The issue-filing conventions rule must document the Gate: line, not only Scope:.
    rule = (REPO_ROOT / "shared" / "rules" / "issue-hygiene.md").read_text()

    assert "Gate:" in rule, "issue-hygiene must document the Gate: line"


def test_issue_hygiene_requires_programmatic_filers_to_emit_scope_gate() -> None:
    # Audits / workflows that file issues in bulk are exactly the paths that shipped
    # scope-less issues (#183-#200, #208-#215); the rule must require them to emit
    # Scope:/Gate: too.
    rule = (REPO_ROOT / "shared" / "rules" / "issue-hygiene.md").read_text()

    assert "programmatically" in rule, (
        "the rule must require programmatic filers to emit Scope:/Gate:"
    )


# ── doc guards: blocked-by is a checked step at creation (issue #148) ──────────


def test_start_task_requires_blocked_by_question() -> None:
    # Filing must ASK the ordering question, the same status as the mandatory Scope:.
    skill = (SKILLS_DIR / "start-task" / "SKILL.md").read_text()

    assert "Does this depend on open work" in skill, (
        "start-task must pose the blocked-by question as a required step"
    )
    assert "required step" in skill, "the blocked-by step must be labelled required"


def test_issue_hygiene_documents_blocked_by_creation_step() -> None:
    # The rule must record blocked-by as a checked step and state the planner guarantee.
    rule = (REPO_ROOT / "shared" / "rules" / "issue-hygiene.md").read_text()

    assert "checked step" in rule, "blocked-by must be documented as a checked step"
    assert "What the planner guarantees" in rule, "the ordering guarantee must be documented"
    assert "never dispatches an issue while" in rule, (
        "the rule must state the never-before-a-blocker guarantee"
    )


# ── ordering guarantee: AC #3 — both directions, explicitly (issue #148) ──────
# The scheduler already enforces ordering off native blocked-by; these lock the
# acceptance criterion in place: (1) a declared OPEN blocker holds its dependent
# out of every batch, and closing it releases the dependent; (2) independent
# disjoint-scope issues still batch and run concurrently.


def test_never_dispatches_before_a_declared_blocker_closes() -> None:
    # #2 declares blocked-by #1 (OPEN) — #2 must never be dispatched while #1 is open.
    held = _plan([_node(1, "a.py"), _node(2, "b.py", blocked_by=[(1, "OPEN")])])

    assert 2 not in held, "a dependent must not dispatch while its blocker is open"
    assert held == [1], "only the unblocked blocker is dispatched"

    # When #1 closes it leaves the OPEN backlog; #2 now sees only a CLOSED blocker
    # and is released into the batch — the close is what unblocks it.
    released = _plan([_node(2, "b.py", blocked_by=[(1, "CLOSED")])])

    assert released == [2], "closing the declared blocker releases the dependent"


def test_independent_disjoint_issues_batch_concurrently() -> None:
    # Three ready issues, no blocked-by edges, disjoint scopes ⇒ all run at once.
    batch = _plan([_node(1, "a.py"), _node(2, "b.py"), _node(3, "c.py")])

    assert batch == [1, 2, 3], "independent disjoint-scope issues batch concurrently"


def test_declared_blocker_holds_dependent_while_disjoint_peers_still_batch() -> None:
    # Both guarantees at once: #2 (blocked-by #1 OPEN) is held, while the blocker #1
    # and two disjoint independent peers #3/#4 all batch together — ordering serializes
    # only the declared pair, never the independent work around it.
    nodes = [
        _node(1, "a.py"),
        _node(2, "b.py", blocked_by=[(1, "OPEN")]),
        _node(3, "c.py"),
        _node(4, "d.py"),
    ]

    batch = _plan(nodes)

    assert batch == [1, 3, 4], "the blocked dependent is held; disjoint peers still batch"
    assert 2 not in batch


# ── `--explain` / `--explain-labels`: surface the scheduler's disposition (#223) ──


def _explain(
    nodes: list[dict],
    *,
    inflight_issues: list[int] | None = None,
    labels: bool = False,
) -> str:
    """Drive plan_from_json in explain (human) or explain-labels (TSV) mode.

    The in-flight set is passed as explicit `--inflight-issue N` flags (the pure
    planner never touches the worktree list — main() derives those for the CLI).
    """
    args = ""
    for n in inflight_issues or []:
        args += f" --inflight-issue {n}"
    args += " --explain-labels" if labels else " --explain"
    proc = subprocess.run(
        ["bash", "-c", f'source "{BATCH_PLAN}"; plan_from_json{args}'],
        input=json.dumps(nodes),
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _label_map(nodes: list[dict], *, inflight_issues: list[int] | None = None) -> dict[str, str]:
    out = _explain(nodes, inflight_issues=inflight_issues, labels=True)
    return dict(line.split("\t") for line in out.splitlines() if line.strip())


def _scheduling_graph() -> list[dict]:
    # #189 is exclusive (Scope: *); the rest are ready with concrete scopes; #300 is held.
    return [
        _node(189, "*"),
        _node(212, "batch-plan.sh"),
        _node(214, "worktree-lib.sh worktree-land.sh"),
        _node(222, "hub-afk.sh"),
        _node(300, "held.py", labels=["hold"]),
        _node(400, "solo.py"),
    ]


def test_explain_inflight_exclusive_names_what_it_holds_back() -> None:
    out = _explain(_scheduling_graph(), inflight_issues=[189])

    line = next(ln for ln in out.splitlines() if ln.startswith("#189"))
    assert "in-flight" in line
    assert "exclusive" in line
    # An in-flight exclusive conflicts with every dispatchable peer (held #300 excluded),
    # named in priority order (equal depth ⇒ ascending issue number).
    assert "holds back #212, #214, #222, #400" in line


def test_explain_marks_scope_collider_blocked_by_that_issue() -> None:
    out = _explain(_scheduling_graph(), inflight_issues=[189])

    # Every dispatchable peer collides with the exclusive in-flight #189.
    assert re.search(r"^#222\b.*\bblocked-by-scope:#189\b.*\(hub-afk\.sh\)", out, re.M)
    assert re.search(
        r"^#214\b.*\bblocked-by-scope:#189\b.*\(worktree-lib\.sh worktree-land\.sh\)", out, re.M
    )


def test_explain_marks_held_issue() -> None:
    out = _explain(_scheduling_graph(), inflight_issues=[189])

    assert re.search(r"^#300\b.*\bheld\b", out, re.M)


def test_explain_marks_disjoint_ready_issue_queued() -> None:
    out = _explain([_node(10, "a.py"), _node(11, "b.py")])

    assert re.search(r"^#10\b.*\bqueued\b.*\(a\.py\)", out, re.M)
    assert re.search(r"^#11\b.*\bqueued\b.*\(b\.py\)", out, re.M)


def test_explain_ready_scope_collision_between_peers_blocks_lower_priority() -> None:
    # Two ready issues share a.py, nothing in-flight: the lower-numbered wins the slot
    # (queued) and the other is blocked-by-scope on it — mirrors the greedy pack.
    out = _explain([_node(10, "a.py"), _node(11, "a.py")])

    assert re.search(r"^#10\b.*\bqueued\b", out, re.M)
    assert re.search(r"^#11\b.*\bblocked-by-scope:#10\b", out, re.M)


def test_explain_labels_collapse_to_the_four_label_set() -> None:
    labels = _label_map(_scheduling_graph(), inflight_issues=[189])

    assert labels["189"] == "afk:in-flight"
    assert labels["222"] == "afk:blocked-by-scope"
    assert labels["400"] == "afk:blocked-by-scope"
    assert labels["300"] == "-", "a held issue carries no afk:* label"


def test_explain_labels_mark_queued_and_exclusive() -> None:
    labels = _label_map([_node(10, "a.py"), _node(11, "b.py"), _node(12, "a.py")])

    assert labels["10"] == "afk:queued"
    assert labels["11"] == "afk:queued"
    assert labels["12"] == "afk:blocked-by-scope"

    exclusive = _label_map([_node(9, "*")])
    assert exclusive["9"] == "afk:exclusive"


def test_explain_labels_strip_dep_blocked_issue() -> None:
    # An issue with an OPEN native blocker is not dispatchable ⇒ no afk:* label ('-').
    labels = _label_map([_node(1, "a.py"), _node(2, "b.py", blocked_by=[(1, "OPEN")])])

    assert labels["2"] == "-"
