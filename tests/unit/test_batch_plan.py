"""Unit tests for shared/skills/hub/scripts/batch-plan.sh (issue #70).

With `Scope:` + native blocked-by on every issue, computing the next set of issues
that can safely run *concurrently* is a mechanical graph computation — so it stays
SCRIPTED (no LLM in the control plane). batch-plan.sh:

  * READ — one `gh api graphql` round-trip for every open issue's `body` (the
    `Scope:` line) and its `blockedBy` connection.
  * ELIGIBILITY — an issue is *ready* when all its blockers are closed.
  * PRIORITY — critical-path depth: rank each ready issue by the longest blocked-by
    chain rooted at it, so the longest serial tail is unblocked earliest (minimizes
    makespan). Ties break on direct-dependent count, then issue number.
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

REPO_ROOT = Path(__file__).resolve().parents[2]
BATCH_PLAN = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "batch-plan.sh"
SKILLS_DIR = REPO_ROOT / "shared" / "skills"


def _node(
    number: int,
    scope: str | None,
    blocked_by: list[tuple[int, str]] | None = None,
    *,
    split: str | None = None,
) -> dict:
    """Build one graphql issue node: a Scope: body + blockedBy nodes (number, state)."""
    body = "Some description.\n"
    if scope is not None:
        body += f"Scope: {scope}\n"
    if split is not None:
        body += f"Split: {split}\n"
    nodes = [{"number": n, "state": s} for n, s in (blocked_by or [])]
    return {"number": number, "body": body, "blockedBy": {"nodes": nodes}}


def _run_plan(
    nodes: list[dict], *, inflight: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Pipe a fixture graph into plan_from_json and return the completed process."""
    env = {**os.environ}
    args = ""
    for spoke in inflight or []:
        args += f" --inflight {json_quote(spoke)}"
    return subprocess.run(
        ["bash", "-c", f'source "{BATCH_PLAN}"; plan_from_json{args}'],
        input=json.dumps(nodes),
        capture_output=True,
        text=True,
        env=env,
    )


def _plan(nodes: list[dict], *, inflight: list[str] | None = None) -> list[int]:
    """Pipe a fixture graph into plan_from_json and return the batch as issue numbers."""
    proc = _run_plan(nodes, inflight=inflight)
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


def test_split_marker_in_any_chain_member_suppresses() -> None:
    # The marker works from ANY issue of the chain, not just the head.
    nodes = [
        _node(1, "a.py"),
        _node(2, "a.py", blocked_by=[(1, "OPEN")], split="intentional — standalone value"),
    ]

    proc = _run_plan(nodes)

    assert "merge candidates" not in proc.stderr


def test_split_marker_suppresses_only_its_own_chain() -> None:
    # Two colliding chains, one marked: the other must still warn.
    nodes = [
        _node(1, "a.py", split="intentional — shelf-life"),
        _node(2, "a.py", blocked_by=[(1, "OPEN")]),
        _node(11, "b.py"),
        _node(12, "b.py", blocked_by=[(11, "OPEN")]),
    ]

    proc = _run_plan(nodes)

    warnings = [line for line in proc.stderr.splitlines() if "merge candidates" in line]
    assert len(warnings) == 1
    assert "#11 → #12" in warnings[0]
    assert "#1" not in warnings[0]


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


def test_next_batch_skill_documents_dispatch() -> None:
    skill = (SKILLS_DIR / "next-batch" / "SKILL.md").read_text().lower()

    assert "batch-plan.sh" in skill, "the skill must run batch-plan.sh"
    assert "worktree-new.sh" in skill, "the skill must dispatch via worktree-new.sh"
