"""v2 Spoke-view query tests (Issues #35, #46).

The v2 spoke view fixes the v1 flat dump: real spans carry ``parent_id: null``,
so the tree is reconstructed by *time-bracketing* (smallest-enclosing window).

Issue #46 then fixes attribution: ``step``/``lifecycle`` spans are point markers
that fire at phase *completion*, so there is no span representing the *interval* a
phase ran. ``spoke_steps`` Level-1 roots are now **phase-interval buckets** built
from the marker spine; each main turn attributes to the bucket whose interval
contains it (subagent turns still attribute to their ``agent`` span, unchanged).
The leading region up to the first ``step`` marker is one ``setup`` bucket; turns
outside the lifecycle envelope go to ``(unresolved)``. The real spans nest *under*
their interval bucket — so the structural drill-down (S2) is unchanged, only the
top-level grouping and where main cost lands (S3/S4/S5) move.
"""

from __future__ import annotations

import pytest
from _dashboard_helpers import FIXTURE_V2_SPANS, load_queries, store_v2

RUN = "feature/v2+1000"


def _find(nodes, span_id):
    """Depth-first lookup by span_id within a forest (raises if absent)."""
    for node in nodes:
        if node["span_id"] == span_id:
            return node
        try:
            return _find(node["children"], span_id)
        except KeyError:
            continue
    raise KeyError(span_id)


def _bucket(forest, name):
    """The Level-1 interval bucket with the given label (raises if absent)."""
    return next(n for n in forest if n["name"] == name)


def _kinds(nodes):
    return sorted(n["kind"] for n in nodes)


def _walk(nodes):
    """Yield every node in a forest, depth-first."""
    for node in nodes:
        yield node
        yield from _walk(node["children"])


# --- S2: structural drill-down (spans nest under their phase bucket) ------------


def test_level1_roots_are_phase_interval_buckets_in_time_order():
    forest = store_v2().spoke_steps(RUN)

    # The Level-1 roots are the reconstructed phase intervals, time-ordered: the
    # leading region (spawn + first step) collapses to one `setup` bucket; the
    # post-cycle lifecycle keeps its own `teardown` label. No raw marker span and
    # no hook/pull span leaks to the top level. (unresolved) appears only when a
    # turn falls outside the lifecycle envelope — none here.
    assert [n["name"] for n in forest] == ["setup", "green", "teardown"]
    assert all(n["span_id"] is None for n in forest)  # buckets are synthetic


def test_substeps_nest_under_their_step_by_time():
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")

    # red brackets a skill, a todo, and two hooks (collapsed) — by window only.
    assert _kinds(red["children"]) == ["hooks", "skill", "todo"]


def test_hooks_collapse_into_one_expandable_node():
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")
    hooks = next(c for c in red["children"] if c["kind"] == "hooks")

    assert hooks["collapsed_count"] == 2
    assert hooks["duration_ms"] == 180  # 100 + 80, summed
    # The raw hook spans remain reachable for expansion.
    assert {c["span_id"] for c in hooks["children"]} == {"v2_red_hook1", "v2_red_hook2"}


def test_collapsed_hooks_surface_worst_status():
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")
    hooks = next(c for c in red["children"] if c["kind"] == "hooks")

    # One hook warned — the collapsed line must not hide it behind "success".
    assert hooks["status"] == "warn"


def test_third_level_nests_agent_under_skill():
    forest = store_v2().spoke_steps(RUN)
    skill = _find(forest, "v2_skill")

    # The agent ran inside the skill's window → depth-3 drill-down.
    assert [c["span_id"] for c in skill["children"]] == ["v2_agent"]


def test_identical_window_siblings_do_not_nest():
    forest = store_v2().spoke_steps(RUN)
    green = _find(forest, "v2_green")

    # Three TaskCreate spans share one window; none parents another, no hooks.
    todos = [c for c in green["children"] if c["kind"] == "todo"]
    assert {c["span_id"] for c in todos} == {"v2_task1", "v2_task2", "v2_task3"}
    assert all(not c["children"] for c in todos)


def test_marker_shows_own_wallclock_duration_not_a_rolled_sum():
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")

    # The marker span keeps its own wall-clock, never a sum of overlapping children.
    assert red["duration_ms"] == 50000


def test_interval_bucket_is_never_rendered_as_a_phase_duration():
    forest = store_v2().spoke_steps(RUN)

    # Intervals are attribution-only: the bucket carries no width (the `land`
    # interval would otherwise read as 8.7h of overnight idle).
    for bucket in forest:
        assert bucket["duration_ms"] is None


def test_unknown_spoke_returns_empty_forest():
    assert store_v2().spoke_steps("does/not+exist") == []


# --- S3: source-split once-per-turn attribution to phase intervals --------------


def test_subagent_turns_attribute_to_the_agent_node():
    forest = store_v2().spoke_steps(RUN)
    agent = _find(forest, "v2_agent")

    # Subagent path is UNCHANGED: the two subagent turns (haiku) land on the agent
    # span, never on the main spine.
    assert agent["own_cost_usd"] == pytest.approx(0.35)
    assert agent["own_tokens_in"] == 350
    assert agent["own_tokens_out"] == 180
    assert agent["models"] == ["claude-haiku-4-5"]
    assert agent["agent"] == "subagent"


def test_main_turn_attributes_to_its_phase_interval_not_a_leaf_span():
    forest = store_v2().spoke_steps(RUN)
    setup = _bucket(forest, "setup")
    skill = _find(forest, "v2_skill")

    # The 12:00:10 main turn is in the leading region → owned by the `setup`
    # bucket, never by the skill span whose window happens to contain it.
    assert setup["own_cost_usd"] == pytest.approx(0.09)  # :01 + :10 + :25 + :30
    assert skill["own_cost_usd"] == 0.0
    assert setup["agent"] == "main"


def test_main_turn_counted_once_on_the_interval_not_per_overlapping_sibling():
    forest = store_v2().spoke_steps(RUN)
    green = _bucket(forest, "green")
    tasks = [c for c in _find(forest, "v2_green")["children"] if c["kind"] == "todo"]

    # The 12:01:05 turn brackets three identical-window TaskCreate spans; its cost
    # lands once on the green interval, never replicated across the siblings.
    assert green["own_cost_usd"] == pytest.approx(0.05)  # :01:05 + :01:35
    assert sum(t["own_cost_usd"] for t in tasks) == 0.0


def test_no_hook_skill_todo_or_human_owns_a_main_turn():
    forest = store_v2().spoke_steps(RUN)

    # Structural guarantee of the source split: main cost lands only on interval
    # buckets, never on a window-nested leaf span.
    leaves = [n for n in _walk(forest) if n["kind"] in ("hook", "hooks", "skill", "todo", "human")]
    assert leaves  # the fixture has such spans
    assert all(n["own_cost_usd"] == 0.0 for n in leaves)


def test_interval_rollup_sums_owned_turns_without_double_count():
    forest = store_v2().spoke_steps(RUN)

    # setup: own main 0.09 + nested agent subtree 0.35 = 0.44
    assert _bucket(forest, "setup")["rollup"]["cost_usd"] == pytest.approx(0.44)
    # green: own main 0.05 (no subagent inside) = 0.05
    assert _bucket(forest, "green")["rollup"]["cost_usd"] == pytest.approx(0.05)
    # teardown: own main 0.005 (the 12:02:05 turn, in-envelope) = 0.005
    assert _bucket(forest, "teardown")["rollup"]["cost_usd"] == pytest.approx(0.005)


def test_rollup_models_bubble_up_distinct_and_sorted():
    forest = store_v2().spoke_steps(RUN)

    # setup: own opus + nested-agent haiku. green: own opus + sonnet.
    assert _bucket(forest, "setup")["rollup"]["models"] == ["claude-haiku-4-5", "claude-opus-4-8"]
    assert _bucket(forest, "green")["rollup"]["models"] == [
        "claude-opus-4-8",
        "claude-sonnet-4-6",
    ]


def test_run_total_reconciles_to_the_turn_costs():
    forest = store_v2().spoke_steps(RUN)

    # Conservation: every turn counted exactly once → the forest's rolled-up cost
    # equals the sum of all turn costs (0.495), the trustworthy run total.
    total = sum(root["rollup"]["cost_usd"] for root in forest)
    assert total == pytest.approx(0.495)


def test_subagent_orphans_stay_zero():
    forest = store_v2().spoke_steps(RUN)

    # No (unresolved) bucket: every subagent turn found its agent span and every
    # main turn fell inside the lifecycle envelope.
    assert not [n for n in forest if n["name"] == "(unresolved)"]


def test_raw_path_without_turns_has_zero_owned_cost():
    queries = load_queries()
    store = queries.SpanStore.from_jsonl(FIXTURE_V2_SPANS)  # no turns table data
    setup = _bucket(store.spoke_steps(RUN), "setup")

    assert setup["rollup"]["cost_usd"] == 0.0
    assert setup["own_cost_usd"] == 0.0


def test_spoke_steps_without_turns_table_degrades_gracefully():
    # A connection predating the turns relation (the #22 from_connection seam)
    # has no turns table; spoke_steps must degrade, not raise.
    store = store_v2()
    store.con.execute("DROP TABLE turns")

    setup = _bucket(store.spoke_steps(RUN), "setup")

    assert setup["rollup"]["cost_usd"] == 0.0


# --- S3b: known-answer fixtures (label correctness, not just less-untracked) -----

_LIFE_NEW = {"kind": "lifecycle", "name": "worktree-new", "phase": "spawn"}
_LIFE_DONE = {"kind": "lifecycle", "name": "worktree-done", "phase": "teardown"}


def _span(span_id, ts_start, ts_end, **over):
    base = {
        "span_id": span_id,
        "parent_id": None,
        "spoke_run_id": "ka/run+1",
        "session_id": "sess-ka",
        "workflow_rev": "ka",
        "repo": "ai-toolkit",
        "branch": "ka/run",
        "kind": "step",
        "name": "solo-cycle",
        "phase": None,
        "ts_start": ts_start,
        "ts_end": ts_end,
        "duration_ms": 100,
        "status": "success",
        "human": None,
        "tokens_in": None,
        "tokens_out": None,
        "cost_usd": None,
    }
    base.update(over)
    return base


def _turn(ts, tokens, *, source="main", agent_id=None, model="claude-opus-4-8", session="sess-ka"):
    return {
        "session_id": session,
        "ts": ts,
        "model": model,
        "source": source,
        "agent_id": agent_id,
        "tokens_in": tokens,
        "tokens_out": 0,
        "tokens_total": tokens,
        "cost_usd": tokens / 1000.0,
    }


def _own_tokens(bucket):
    return bucket["own_tokens_in"] + bucket["own_tokens_out"]


def test_known_answer_phase_labels_with_complete_markers():
    queries = load_queries()
    # spawn · anchor(first step) · red · green · teardown — anchor closes setup so
    # red and green each get their own clean interval.
    spans = [
        _span("ka_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("ka_anchor", "2026-06-12T12:00:05Z", "2026-06-12T12:00:05Z", phase="anchor"),
        _span("ka_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("ka_green", "2026-06-12T12:00:25Z", "2026-06-12T12:00:25Z", phase="green"),
        _span("ka_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    turns = [
        _turn("2026-06-12T12:00:10Z", 1000),  # in red interval (anchor, red]
        _turn("2026-06-12T12:00:20Z", 2000),  # in green interval (red, green]
    ]
    forest = queries.SpanStore.from_events(spans, turns=turns).spoke_steps("ka/run+1")

    # Conservation can't catch a label swap; these known answers do.
    assert _own_tokens(_bucket(forest, "red")) == 1000
    assert _own_tokens(_bucket(forest, "green")) == 2000


def test_missing_marker_falls_to_setup_not_green():
    queries = load_queries()
    # spawn · red(first step) · green · teardown. Removing the red marker makes
    # green the first step → the leading region (setup) must absorb both turns,
    # never silently inflate green.
    base = [
        _span("dg_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("dg_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("dg_green", "2026-06-12T12:00:25Z", "2026-06-12T12:00:25Z", phase="green"),
        _span("dg_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    turns = [
        _turn("2026-06-12T12:00:10Z", 1000),  # "red" work (before red marker)
        _turn("2026-06-12T12:00:20Z", 2000),  # green work (between red and green)
    ]

    with_red = queries.SpanStore.from_events(base, turns=turns).spoke_steps("ka/run+1")
    # With the red marker, green correctly owns its own work.
    assert _own_tokens(_bucket(with_red, "green")) == 2000
    assert _own_tokens(_bucket(with_red, "setup")) == 1000

    without_red = [s for s in base if s["span_id"] != "dg_red"]
    degenerate = queries.SpanStore.from_events(without_red, turns=turns).spoke_steps("ka/run+1")
    # The missing marker surfaces as a large setup, not an inflated green.
    assert not [n for n in degenerate if n["name"] == "green"]
    assert _own_tokens(_bucket(degenerate, "setup")) == 3000


def test_non_monotonic_marker_ends_tile_by_completion_order():
    queries = load_queries()
    # A wide `red` [05, 40] overlaps a `green` [10, 26] that completes first. The
    # spine tiles by completion (ts_end) order, so neither phase's work spills into
    # setup/teardown (a ts_start tiling would invert green's interval and lose it).
    spans = [
        _span("nm_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("nm_anchor", "2026-06-12T12:00:02Z", "2026-06-12T12:00:03Z", phase="anchor"),
        _span("nm_red", "2026-06-12T12:00:05Z", "2026-06-12T12:00:40Z", phase="red"),
        _span("nm_green", "2026-06-12T12:00:10Z", "2026-06-12T12:00:26Z", phase="green"),
        _span("nm_teardown", "2026-06-12T12:00:45Z", "2026-06-12T12:00:46Z", **_LIFE_DONE),
    ]
    turns = [
        _turn("2026-06-12T12:00:20Z", 1000),  # before green completes → green
        _turn("2026-06-12T12:00:30Z", 2000),  # after green, before red completes → red
    ]
    forest = queries.SpanStore.from_events(spans, turns=turns).spoke_steps("ka/run+1")

    assert _own_tokens(_bucket(forest, "green")) == 1000
    assert _own_tokens(_bucket(forest, "red")) == 2000
    assert _own_tokens(_bucket(forest, "setup")) == 0  # nothing spilled into setup
    assert sum(r["rollup"]["tokens_in"] + r["rollup"]["tokens_out"] for r in forest) == 3000


def test_out_of_envelope_main_turn_lands_in_unresolved():
    queries = load_queries()
    spans = [
        _span("u_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("u_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("u_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    turns = [
        _turn("2026-06-12T11:59:00Z", 500),  # before spawn → unresolved
        _turn("2026-06-12T12:00:20Z", 700),  # in-envelope → setup/red region
    ]
    forest = queries.SpanStore.from_events(spans, turns=turns).spoke_steps("ka/run+1")

    unresolved = _bucket(forest, "(unresolved)")
    assert _own_tokens(unresolved) == 500


def test_unresolved_node_ignores_malformed_turn_timestamps():
    queries = load_queries()
    spans = queries.load_jsonl(FIXTURE_V2_SPANS)
    # session must match the fixture spans so the turn is fetched for this spoke.
    turns = [_turn("not-a-date", 7, session="sess-v2")]  # unparseable ts → unresolved
    store = queries.SpanStore.from_events(spans, turns=turns)

    unresolved = _bucket(store.spoke_steps(RUN), "(unresolved)")

    # The turn is still counted, but its garbage ts never frames the window.
    assert _own_tokens(unresolved) == 7
    assert unresolved["ts_start"] is None


# --- S6: todo-text L1 labels (Issue #47) ----------------------------------------


def test_interval_bucket_labelled_by_the_todo_it_advances():
    queries = load_queries()
    # A resolved todo span (its name is the in-progress item text, not the bare
    # tool name) sits in the red interval → the L1 bucket is named for that todo.
    spans = [
        _span("l_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("l_anchor", "2026-06-12T12:00:05Z", "2026-06-12T12:00:05Z", phase="anchor"),
        _span(
            "l_todo",
            "2026-06-12T12:00:08Z",
            "2026-06-12T12:00:09Z",
            kind="todo",
            name="Add RED parser test",
            phase=None,
        ),
        _span("l_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("l_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    forest = queries.SpanStore.from_events(spans).spoke_steps("ka/run+1")

    assert _bucket(forest, "Add RED parser test")
    assert not [n for n in forest if n["name"] == "red"]


def test_bucket_falls_back_to_phase_name_when_no_todo_resolves():
    queries = load_queries()
    # A bare-tool-name todo (unresolved, no in-progress item derived) must NOT
    # override the phase label — the bucket stays named for its phase.
    spans = [
        _span("f_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("f_anchor", "2026-06-12T12:00:05Z", "2026-06-12T12:00:05Z", phase="anchor"),
        _span(
            "f_todo",
            "2026-06-12T12:00:08Z",
            "2026-06-12T12:00:09Z",
            kind="todo",
            name="TodoWrite",
            phase=None,
        ),
        _span("f_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("f_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    forest = queries.SpanStore.from_events(spans).spoke_steps("ka/run+1")

    assert _bucket(forest, "red")


def test_latest_resolved_todo_in_the_interval_wins():
    queries = load_queries()
    # Two resolved todos land in the same red interval; the later one is the item
    # actually being advanced when the phase completes → it names the bucket.
    spans = [
        _span("m_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("m_anchor", "2026-06-12T12:00:05Z", "2026-06-12T12:00:05Z", phase="anchor"),
        _span(
            "m_todo1",
            "2026-06-12T12:00:08Z",
            "2026-06-12T12:00:09Z",
            kind="todo",
            name="First item",
            phase=None,
        ),
        _span(
            "m_todo2",
            "2026-06-12T12:00:12Z",
            "2026-06-12T12:00:13Z",
            kind="todo",
            name="Second item",
            phase=None,
        ),
        _span("m_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("m_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    forest = queries.SpanStore.from_events(spans).spoke_steps("ka/run+1")

    assert _bucket(forest, "Second item")


# --- S4: meta-by-kind aggregation -----------------------------------------------


def _by_kind(rows):
    return {row["kind"]: row for row in rows}


def test_meta_by_kind_counts_each_span_kind():
    meta = _by_kind(store_v2().spoke_meta_by_kind(RUN))

    assert meta["hook"]["count"] == 2  # "launched too much" signal lives here
    assert meta["todo"]["count"] == 4  # TodoWrite + 3 TaskCreate
    assert meta["step"]["count"] == 2
    assert meta["lifecycle"]["count"] == 2
    assert meta["agent"]["count"] == 1


def test_meta_by_kind_time_stats_are_real_durations():
    meta = _by_kind(store_v2().spoke_meta_by_kind(RUN))

    # Hooks: 100 + 80 = 180 total, median of the two = 90.
    assert meta["hook"]["total_duration_ms"] == 180
    assert meta["hook"]["median_duration_ms"] == 90
    assert meta["todo"]["median_duration_ms"] == 1000


def test_meta_by_kind_cost_is_subagent_only_after_source_split():
    meta = _by_kind(store_v2().spoke_meta_by_kind(RUN))

    # Main-agent cost is now a property of the PHASE (interval bucket), not of a
    # span kind — so only `agent` (subagent) spans carry intrinsic owned cost.
    assert meta["agent"]["total_cost_usd"] == pytest.approx(0.35)
    assert meta["skill"]["total_cost_usd"] == 0.0
    assert meta["todo"]["total_cost_usd"] == 0.0
    assert meta["step"]["total_cost_usd"] == 0.0
    assert meta["human"]["total_cost_usd"] == 0.0
    assert meta["lifecycle"]["total_cost_usd"] == 0.0
    assert meta["hook"]["total_cost_usd"] == 0.0


def test_meta_by_kind_sorts_by_cost_then_count_then_kind():
    rows = store_v2().spoke_meta_by_kind(RUN)

    # agent ($0.35) first; the rest at $0 by count desc then kind asc.
    assert [row["kind"] for row in rows] == [
        "agent",
        "todo",
        "hook",
        "lifecycle",
        "step",
        "human",
        "skill",
    ]


def test_meta_by_kind_surfaces_models_per_kind():
    meta = _by_kind(store_v2().spoke_meta_by_kind(RUN))

    assert meta["agent"]["models"] == ["claude-haiku-4-5"]


def test_meta_by_kind_total_is_the_subagent_cost():
    rows = store_v2().spoke_meta_by_kind(RUN)

    # All main cost moved to phase buckets; summing real-span kinds is exactly the
    # subagent total (0.35), the run total minus every bucket's owned main cost.
    total = sum(row["total_cost_usd"] for row in rows)
    assert total == pytest.approx(0.35)


def test_meta_by_kind_unknown_spoke_is_empty():
    assert store_v2().spoke_meta_by_kind("does/not+exist") == []


# --- S5: display formatting helpers (the thin glue app.py renders) --------------


def test_format_step_label_per_kind():
    queries = load_queries()
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")
    hooks = next(c for c in red["children"] if c["kind"] == "hooks")

    # Interval buckets label by phase; nested marker/agent spans keep name · phase.
    assert queries.format_step_label(_bucket(forest, "setup")) == "setup"
    assert queries.format_step_label(_bucket(forest, "green")) == "green"
    assert queries.format_step_label(red) == "solo-cycle · red"
    assert queries.format_step_label(hooks) == "hooks x2"
    assert queries.format_step_label(_find(forest, "v2_agent")) == "tdd-red"


def test_format_step_metrics_rolls_up_for_an_interval_bucket():
    queries = load_queries()
    setup = _bucket(store_v2().spoke_steps(RUN), "setup")

    metrics = queries.format_step_metrics(setup)

    assert metrics["time"] == "—"  # intervals are never rendered as durations
    assert metrics["cost"] == "$0.4400"  # 0.09 own main + 0.35 nested agent
    assert metrics["tokens"] == "755"  # (160+65) own + (350+180) agent
    assert metrics["model"] == "haiku-4-5, opus-4-8"  # claude- prefix stripped
    assert metrics["agent"] == "main"
    assert metrics["status"] == "warn"  # a nested hook warned — surfaced, not hidden


def test_format_step_metrics_marks_subagent():
    queries = load_queries()
    agent = _find(store_v2().spoke_steps(RUN), "v2_agent")

    metrics = queries.format_step_metrics(agent)

    assert metrics["agent"] == "subagent"
    assert metrics["model"] == "haiku-4-5"


def test_format_step_metrics_rolls_up_human_count():
    queries = load_queries()
    forest = store_v2().spoke_steps(RUN)

    assert queries.format_step_metrics(_bucket(forest, "green"))["humans"] == "1"
    assert queries.format_step_metrics(_bucket(forest, "setup"))["humans"] == "—"


def test_format_step_metrics_blanks_zero_values_on_a_bare_marker():
    queries = load_queries()
    done = _find(store_v2().spoke_steps(RUN), "v2_life_done")

    metrics = queries.format_step_metrics(done)

    # The teardown MARKER owns nothing (its interval cost is on the teardown
    # bucket); a node with no owned turns blanks its cost/tokens/model.
    assert metrics["cost"] == "—"
    assert metrics["tokens"] == "—"
    assert metrics["model"] == "—"
