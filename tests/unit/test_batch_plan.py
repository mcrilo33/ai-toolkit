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
    nodes: list[dict], *, inflight: list[str] | None = None, cap: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Pipe a fixture graph into plan_from_json and return the completed process."""
    env = {**os.environ}
    args = ""
    for spoke in inflight or []:
        args += f" --inflight {json_quote(spoke)}"
    if cap is not None:
        args += f" --cap {cap}"
    return subprocess.run(
        ["bash", "-c", f'source "{BATCH_PLAN}"; plan_from_json{args}'],
        input=json.dumps(nodes),
        capture_output=True,
        text=True,
        env=env,
    )


def _plan(
    nodes: list[dict], *, inflight: list[str] | None = None, cap: int | None = None
) -> list[int]:
    """Pipe a fixture graph into plan_from_json and return the batch as issue numbers."""
    proc = _run_plan(nodes, inflight=inflight, cap=cap)
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
    # The acceptance chain #1 → #2 → #3 has disjoint scopes (a.py, b.py, c.py).
    proc = _run_plan(_acceptance_graph())

    assert proc.returncode == 0, proc.stderr
    assert "merge candidates" not in proc.stderr


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
