"""Spine construction for the v3 spoke trace (Issue #52 Track C — RED).

The spine is the L1 row of phase-interval buckets. Track C subtask 1 fixes four
spine defects against the ``feature/47`` golden fixture:

1. **No coalescing** — every marker is its own step; the near-empty ``green`` and
   ``review`` markers stay separate buckets.
2. **Spawn / first-RED split** — ``setup``/spawn holds only the pre-cycle region
   (lifecycle + loaded context); the first real phase splits off at the first
   ``in_progress`` todo transition, taking the RED-phase work with it.
3. **No-marker synthesis** — a phase with todo activity but no ``step`` marker (the
   resume session) is synthesized from the todo transition and badged
   ``⟨from todo — no marker⟩``; its spans never fall to ``(unresolved)``.
4. **Setup label not overridden** — the spawn bucket keeps its honest ``setup``
   label; a todo summary never renames it (the phantom-first-step defect).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _dashboard_helpers import store_from

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
V3_SPANS = _FIXTURES / "dashboard_golden_spoke.jsonl"
V3_TURNS = _FIXTURES / "dashboard_golden_spoke_turns.jsonl"
SPOKE_RUN_ID = "feature/47+1700000000"

_NO_MARKER_BADGE = "⟨from todo — no marker⟩"
_RED_TODO = "write failing test for emission link"
_NOMARKER_TODO = "wire emission link into push gate"


def _forest() -> list[dict]:
    return store_from(V3_SPANS, V3_TURNS).spoke_steps(SPOKE_RUN_ID)


def _span_ids(node: dict) -> set[str]:
    """Every real-span id in a node's subtree."""
    ids: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur["span_id"] is not None:
            ids.add(cur["span_id"])
        stack.extend(cur["children"])
    return ids


def _root_containing(forest: list[dict], span_id: str) -> dict | None:
    return next((r for r in forest if span_id in _span_ids(r)), None)


def test_spawn_holds_only_precycle_not_red_work() -> None:
    forest = _forest()
    spawn = _root_containing(forest, "g_life_new")
    assert spawn is not None, "no bucket holds the spawn lifecycle"
    ids = _span_ids(spawn)
    # Loaded context belongs to spawn; the RED-phase work does not.
    assert "g_ctx_q" in ids
    for red_work in ("g_skill_tddred", "g_tool_edit1", "g_tool_read"):
        assert red_work not in ids, f"{red_work} leaked into the spawn bucket"


def test_red_phase_splits_off_at_first_in_progress_todo() -> None:
    forest = _forest()
    red = _root_containing(forest, "g_skill_tddred")
    spawn = _root_containing(forest, "g_life_new")
    assert red is not None and spawn is not None
    assert red is not spawn, "the RED phase must split off from spawn"
    assert "g_tool_edit1" in _span_ids(red)
    # The split-off phase is named for the todo it advances (#47 naming, now on the
    # phase bucket rather than hijacking setup).
    assert red["name"] == _RED_TODO
    # The RED step marker heads its own phase bucket (markers place by completion),
    # never orphaning back into setup.
    assert "g_red" in _span_ids(red)
    assert "g_red" not in _span_ids(spawn)


def test_setup_label_is_not_overridden_by_a_todo() -> None:
    forest = _forest()
    spawn = _root_containing(forest, "g_life_new")
    assert spawn is not None
    assert spawn["name"] in {"setup", "spawn"}, f"spawn mislabeled {spawn['name']!r}"
    assert spawn["name"] != _RED_TODO


def test_teardown_region_keeps_its_label_not_the_no_marker_todo() -> None:
    # The no-marker todo sits on the teardown/no-marker boundary; its summary must
    # not leak onto the coarse teardown region (the phantom defect, on teardown).
    forest = _forest()
    teardown = _root_containing(forest, "g_approval")
    assert teardown is not None
    assert teardown["name"] == "teardown", f"teardown mislabeled {teardown['name']!r}"


def test_green_and_review_are_separate_buckets() -> None:
    forest = _forest()
    green = _root_containing(forest, "g_green")
    review = _root_containing(forest, "g_review")
    assert green is not None and review is not None
    assert green is not review, "near-empty green/review markers must not coalesce"


def test_no_marker_phase_is_synthesized_and_badged() -> None:
    forest = _forest()
    nomarker = _root_containing(forest, "g_tool_b1")
    assert nomarker is not None, "the resume-session activity vanished"
    assert _NO_MARKER_BADGE in nomarker["name"], f"missing badge: {nomarker['name']!r}"
    assert _NOMARKER_TODO in nomarker["name"]
    ids = _span_ids(nomarker)
    assert {"g_tool_b1", "g_hook_b", "g_tool_b2"} <= ids


def test_no_marker_activity_never_falls_to_unresolved() -> None:
    forest = _forest()
    unresolved = next((r for r in forest if r["kind"] == "unresolved"), None)
    if unresolved is None:
        return
    orphaned = _span_ids(unresolved)
    assert not ({"g_tool_b1", "g_hook_b", "g_tool_b2"} & orphaned)


def _load_tree():
    import importlib.util

    from _dashboard_helpers import DASHBOARD_DIR

    spec = importlib.util.spec_from_file_location("tree", DASHBOARD_DIR / "tree.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _marker(span_id: str, kind: str, start: str, end: str, **extra) -> dict:
    return {"span_id": span_id, "kind": kind, "ts_start": start, "ts_end": end, **extra}


def test_each_no_marker_transition_gets_its_own_badged_bucket() -> None:
    # Two summarised todos inside one teardown region are two distinct phases — each
    # must synthesize its own badged interval, never collapse into the first.
    tree = _load_tree()
    nodes = [
        _marker(
            "life0", "lifecycle", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", phase="spawn"
        ),
        _marker("step0", "step", "2026-01-01T00:00:02Z", "2026-01-01T00:00:03Z", phase="red"),
        _marker(
            "life1", "lifecycle", "2026-01-01T00:00:04Z", "2026-01-01T01:00:00Z", phase="teardown"
        ),
        _marker("todoA", "todo", "2026-01-01T00:10:00Z", "2026-01-01T00:10:00Z", summary="phase A"),
        _marker("todoB", "todo", "2026-01-01T00:20:00Z", "2026-01-01T00:20:00Z", summary="phase B"),
    ]
    intervals = tree._build_intervals(nodes)
    badged = {iv["key"]: iv["label"] for iv in intervals if _NO_MARKER_BADGE in iv["label"]}
    assert set(badged) == {"todoA", "todoB"}, f"each transition needs its own bucket: {badged}"
    assert badged["todoA"].startswith("phase A")
    assert badged["todoB"].startswith("phase B")
