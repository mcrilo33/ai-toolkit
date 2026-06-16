"""Turn-centric Spoke-view query tests (Issues #35, #46, #47).

The spoke drill-down is reconstructed from flat spans (``parent_id`` is null for
all main/push spans). #46 builds **phase-interval buckets** (L1) from the
step/lifecycle marker spine, each labelled by the todo it advances. #47 S3 then
makes the inside **turn-centric**: the marker shows as a thin header leaf (its own
wall-clock), then come **turn nodes** (L2) — one per assistant inference, owning
its once-per-turn cost — and the skill/tool/todo/human spans an inference issued
nest *under their turn* (matched by ``ts_start``). Hooks nest under the tool whose
window contains them; an ``agent`` span holds the sub-agent's own **sub-turn**
nodes + spans (the latter bound by ``parent_id``). Cost rolls up to the same run
total as #46 — every turn is counted exactly once.
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


def _turns(node):
    """The turn-node (L2) children of a node, time-ordered."""
    return [c for c in node["children"] if c["kind"] == "turn"]


def _turn_at(node, clock):
    """The turn node under ``node`` whose ts_start clock-time is ``clock``."""
    return next(t for t in _turns(node) if (t["ts_start"] or "").split("T")[-1].startswith(clock))


def _marker(node, phase):
    """The thin marker-header leaf under ``node`` with the given phase."""
    return next(
        c for c in node["children"] if c["kind"] in ("step", "lifecycle") and c["phase"] == phase
    )


def _kinds(nodes):
    return sorted(n["kind"] for n in nodes)


def _walk(nodes):
    """Yield every node in a forest, depth-first."""
    for node in nodes:
        yield node
        yield from _walk(node["children"])


def _rolled_cost(node):
    return node["rollup"]["cost_usd"]


def _rolled_tokens(node):
    return node["rollup"]["tokens_in"] + node["rollup"]["tokens_out"]


# --- S1: phase-interval buckets + thin marker headers ---------------------------


def test_level1_roots_are_phase_buckets_in_time_order():
    forest = store_v2().spoke_steps(RUN)

    assert [n["name"] for n in forest] == ["setup", "green", "teardown"]
    assert all(n["span_id"] is None for n in forest)  # buckets are synthetic


def test_markers_are_thin_header_leaves_keeping_their_wall_clock():
    forest = store_v2().spoke_steps(RUN)
    red = _marker(_bucket(forest, "setup"), "red")

    # The phase marker is a header leaf: it keeps its own wall-clock but never
    # contains the turns/tools (those sit under turn nodes, not under the marker).
    assert red["span_id"] == "v2_red"
    assert red["duration_ms"] == 50000
    assert red["children"] == []


def test_interval_bucket_is_never_rendered_as_a_phase_duration():
    forest = store_v2().spoke_steps(RUN)
    assert all(bucket["duration_ms"] is None for bucket in forest)


def test_unknown_spoke_returns_empty_forest():
    assert store_v2().spoke_steps("does/not+exist") == []


# --- S2: turn nodes + spans nested under their turn -----------------------------


def test_turn_nodes_are_l2_children_time_ordered():
    setup = _bucket(store_v2().spoke_steps(RUN), "setup")

    # One turn node per main inference in the bucket, in time order.
    assert [t["ts_start"] for t in _turns(setup)] == [
        "2026-06-12T12:00:01Z",
        "2026-06-12T12:00:10Z",
        "2026-06-12T12:00:25Z",
        "2026-06-12T12:00:30Z",
    ]


def test_turn_node_owns_its_once_per_turn_cost():
    setup = _bucket(store_v2().spoke_steps(RUN), "setup")
    assert _turn_at(setup, "12:00:10")["own_cost_usd"] == pytest.approx(0.05)


def test_tools_nest_under_the_turn_that_issued_them():
    green = _bucket(store_v2().spoke_steps(RUN), "green")
    first = _turn_at(green, "12:01:05")
    second = _turn_at(green, "12:01:35")

    # turn 12:01:05 issued Bash + three identical TaskCreate; the TaskCreate trio
    # collapses into one ``todo x3`` group beside Bash (Issue #56). turn 12:01:35
    # issued the question + a Read — each tool sits under the inference that chose it.
    assert _kinds(first["children"]) == ["todo", "tool"]
    todo_group = next(c for c in first["children"] if c["kind"] == "todo")
    assert todo_group["collapsed_count"] == 3
    assert {m["span_id"] for m in todo_group["children"]} == {"v2_task1", "v2_task2", "v2_task3"}
    assert _kinds(second["children"]) == ["human", "tool"]


def test_same_turn_tools_are_siblings_not_nested():
    green = _bucket(store_v2().spoke_steps(RUN), "green")
    second = _turn_at(green, "12:01:35")

    # Read and the question share their inference's ts_start → siblings, even
    # though the question's wait window would otherwise enclose the Read.
    assert {c["span_id"] for c in second["children"]} == {"v2_ask", "v2_read"}
    assert all(not c["children"] for c in second["children"] if c["span_id"] == "v2_read")


def test_hook_nests_under_its_triggering_tool():
    bash = _find(store_v2().spoke_steps(RUN), "v2_bash")
    hooks = next(c for c in bash["children"] if c["kind"] == "hooks")

    assert {h["span_id"] for h in hooks["children"]} == {"v2_bash_hook"}


def test_hooks_outside_any_tool_or_turn_collapse_at_the_bucket():
    setup = _bucket(store_v2().spoke_steps(RUN), "setup")
    hooks = next(c for c in setup["children"] if c["kind"] == "hooks")

    # The red-phase hooks fall in no tool window and match no turn → they collapse
    # into one node directly under the bucket. Its status is the terminal (last-event)
    # hook (Issue #57) — here the later hook is the warn, so worst and last coincide;
    # the discriminating last-vs-worst cases live in test_dashboard_invariants_v3.
    assert hooks["collapsed_count"] == 2
    assert hooks["status"] == "warn"


def test_skill_and_agent_issued_in_one_turn_are_siblings():
    setup = _bucket(store_v2().spoke_steps(RUN), "setup")
    turn = _turn_at(setup, "12:00:10")

    # The skill renders as a soft scope-band (Issue #52, decision 5), staying a
    # sibling of the agent issued in the same turn.
    assert _kinds(turn["children"]) == ["agent", "scope-band"]


# --- S3: sub-agent drill (turns + spans under the agent) ------------------------


def test_agent_holds_its_subagent_turn_nodes():
    agent = _find(store_v2().spoke_steps(RUN), "v2_agent")

    # The agent's children are the sub-agent's own inferences, time-ordered.
    assert [t["ts_start"] for t in _turns(agent)] == [
        "2026-06-12T12:00:11Z",
        "2026-06-12T12:00:15Z",
    ]


def test_subagent_span_nests_under_its_agent_via_parent_id():
    agent = _find(store_v2().spoke_steps(RUN), "v2_agent")
    sub_read = _find(agent["children"], "v2_sub_read")

    # The sub-agent Read (parent_id = the agent) lands inside the agent subtree,
    # under its sub-turn — never at the bucket level, never under a main span.
    assert sub_read["name"] == "Read"
    assert sub_read not in store_v2().spoke_steps(RUN)  # not a bucket root


def test_parent_id_null_spans_still_time_bracket():
    # The Bash-nested hook carries no parent_id, so it nests by window — proving
    # the parent_id guard is inert for the entire pre-S3 (parent_id-null) dataset.
    bash = _find(store_v2().spoke_steps(RUN), "v2_bash")
    assert any(c["kind"] == "hooks" for c in bash["children"])


# --- S4: conservation (turn nodes own cost; total unchanged from #46) -----------


def test_run_total_reconciles_to_the_turn_costs():
    forest = store_v2().spoke_steps(RUN)

    # Every turn counted once → the forest's rolled-up cost equals the run total.
    assert sum(_rolled_cost(root) for root in forest) == pytest.approx(0.495)


def test_main_cost_lives_on_turn_nodes_not_the_bucket():
    setup = _bucket(store_v2().spoke_steps(RUN), "setup")

    # The bucket owns nothing directly; its main cost is the sum of its turn nodes.
    assert setup["own_cost_usd"] == 0.0
    assert sum(t["own_cost_usd"] for t in _turns(setup)) == pytest.approx(0.09)
    assert _rolled_cost(setup) == pytest.approx(0.44)  # 0.09 main + 0.35 nested agent


def test_subagent_cost_lives_on_sub_turn_nodes():
    agent = _find(store_v2().spoke_steps(RUN), "v2_agent")

    # The agent owns nothing in the tree; its sub-turns own the subagent pool.
    assert agent["own_cost_usd"] == 0.0
    assert [t["own_cost_usd"] for t in _turns(agent)] == [pytest.approx(0.20), pytest.approx(0.15)]
    assert _rolled_cost(agent) == pytest.approx(0.35)


def test_tool_and_marker_leaves_own_zero():
    leaves = [
        n
        for n in _walk(store_v2().spoke_steps(RUN))
        if n["kind"] in ("tool", "step", "lifecycle", "hook", "skill", "todo", "human")
    ]
    assert leaves
    assert all(n["own_cost_usd"] == 0.0 for n in leaves)


def test_no_unresolved_bucket_when_every_turn_is_placed():
    forest = store_v2().spoke_steps(RUN)
    assert not [n for n in forest if n["name"] == "(unresolved)"]


def test_same_ts_main_turns_are_both_kept_no_cost_dropped():
    queries = load_queries()
    # Two main inferences can share a timestamp (ms precision collisions happen).
    # Both must become turn nodes — neither the turn nor its cost may be dropped.
    spans = [
        _span("c_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("c_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("c_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    turns = [_turn("2026-06-12T12:00:05Z", 1000), _turn("2026-06-12T12:00:05Z", 2000)]
    forest = queries.SpanStore.from_events(spans, turns=turns).spoke_steps("ka/run+1")

    setup = _bucket(forest, "setup")
    assert len(_turns(setup)) == 2
    assert sum(_rolled_tokens(root) for root in forest) == 3000


def test_same_ts_subagent_turns_are_both_kept():
    queries = load_queries()
    # Two sub-agent inferences sharing a ts under one agent must both survive.
    spans = [
        _span("s_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span(
            "s_agent", "2026-06-12T12:00:10Z", "2026-06-12T12:00:50Z", kind="agent", name="Explore"
        ),
        _span("s_teardown", "2026-06-12T12:01:00Z", "2026-06-12T12:01:01Z", **_LIFE_DONE),
    ]
    turns = [
        _turn("2026-06-12T12:00:20Z", 300, source="subagent"),
        _turn("2026-06-12T12:00:20Z", 200, source="subagent"),
    ]
    forest = queries.SpanStore.from_events(spans, turns=turns).spoke_steps("ka/run+1")
    agent = _find(forest, "s_agent")

    assert len(_turns(agent)) == 2
    assert agent["rollup"]["tokens_in"] == 500


def test_subagent_span_follows_its_agent_across_a_phase_boundary():
    queries = load_queries()
    # A long-running agent can straddle a phase marker: the agent's ts lands in one
    # bucket while a sub-agent span's ts lands in the next. The sub-agent span must
    # still nest under its agent (by parent_id), never under a main turn elsewhere.
    spans = [
        _span("x_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span(
            "x_agent", "2026-06-12T12:00:10Z", "2026-06-12T12:00:30Z", kind="agent", name="Explore"
        ),
        _span(
            "x_sub",
            "2026-06-12T12:00:20Z",
            "2026-06-12T12:00:22Z",
            kind="tool",
            name="Read",
            parent_id="x_agent",
            summary="/x.py",
        ),
        _span("x_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("x_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    forest = queries.SpanStore.from_events(spans).spoke_steps("ka/run+1")
    agent = _find(forest, "x_agent")

    assert _find(agent["children"], "x_sub")["span_id"] == "x_sub"


def test_raw_path_without_turns_has_no_turn_nodes():
    queries = load_queries()
    store = queries.SpanStore.from_jsonl(FIXTURE_V2_SPANS)  # no turns table data

    forest = store.spoke_steps(RUN)

    # No turns relation → no turn nodes; spans fall straight under their bucket,
    # and nothing owns cost.
    assert not [n for n in _walk(forest) if n["kind"] == "turn"]
    assert sum(_rolled_cost(root) for root in forest) == 0.0


def test_spoke_steps_without_turns_table_degrades_gracefully():
    store = store_v2()
    store.con.execute("DROP TABLE turns")

    forest = store.spoke_steps(RUN)

    assert not [n for n in _walk(forest) if n["kind"] == "turn"]


# --- S4b: known-answer fixtures (turn → bucket attribution by rollup) ------------

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
        "summary": None,
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


def test_known_answer_phase_labels_with_complete_markers():
    queries = load_queries()
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

    # The turn lands as a turn node under the right bucket; the bucket rollup
    # reflects it. A label swap would put the tokens in the wrong bucket.
    assert _rolled_tokens(_bucket(forest, "red")) == 1000
    assert _rolled_tokens(_bucket(forest, "green")) == 2000


def test_missing_marker_falls_to_setup_not_green():
    queries = load_queries()
    base = [
        _span("dg_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("dg_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("dg_green", "2026-06-12T12:00:25Z", "2026-06-12T12:00:25Z", phase="green"),
        _span("dg_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    turns = [
        _turn("2026-06-12T12:00:10Z", 1000),
        _turn("2026-06-12T12:00:20Z", 2000),
    ]

    with_red = queries.SpanStore.from_events(base, turns=turns).spoke_steps("ka/run+1")
    assert _rolled_tokens(_bucket(with_red, "green")) == 2000
    assert _rolled_tokens(_bucket(with_red, "setup")) == 1000

    without_red = [s for s in base if s["span_id"] != "dg_red"]
    degenerate = queries.SpanStore.from_events(without_red, turns=turns).spoke_steps("ka/run+1")
    assert not [n for n in degenerate if n["name"] == "green"]
    assert _rolled_tokens(_bucket(degenerate, "setup")) == 3000


def test_out_of_envelope_main_turn_lands_in_unresolved():
    queries = load_queries()
    spans = [
        _span("u_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("u_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("u_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    turns = [
        _turn("2026-06-12T11:59:00Z", 500),  # before spawn → unresolved
        _turn("2026-06-12T12:00:20Z", 700),  # in-envelope
    ]
    forest = queries.SpanStore.from_events(spans, turns=turns).spoke_steps("ka/run+1")

    assert _rolled_tokens(_bucket(forest, "(unresolved)")) == 500


def test_unresolved_turn_node_ignores_malformed_timestamps():
    queries = load_queries()
    spans = queries.load_jsonl(FIXTURE_V2_SPANS)
    turns = [_turn("not-a-date", 7, session="sess-v2")]  # unparseable ts → unresolved
    store = queries.SpanStore.from_events(spans, turns=turns)

    unresolved = _bucket(store.spoke_steps(RUN), "(unresolved)")

    # The turn is still counted (as a turn node), but its garbage ts never frames
    # the bucket window.
    assert _rolled_tokens(unresolved) == 7
    assert unresolved["ts_start"] is None


# --- S5: meta-by-kind aggregation (real spans only; turn nodes excluded) --------


def _by_kind(rows):
    return {row["kind"]: row for row in rows}


def test_meta_by_kind_counts_each_span_kind():
    meta = _by_kind(store_v2().spoke_meta_by_kind(RUN))

    assert meta["agent"]["count"] == 1
    assert meta["todo"]["count"] == 4  # TodoWrite + 3 TaskCreate
    assert meta["tool"]["count"] == 4  # Bash, Read, sub-agent Read + Grep
    assert meta["hook"]["count"] == 3
    assert meta["step"]["count"] == 2
    assert meta["lifecycle"]["count"] == 2
    assert meta["human"]["count"] == 1
    assert meta["skill"]["count"] == 1


def test_turn_nodes_never_enter_meta_or_the_span_kinds():
    forest = store_v2().spoke_steps(RUN)
    meta = _by_kind(store_v2().spoke_meta_by_kind(RUN))

    # Turn nodes are synthetic, drill-down-only: never a meta-by-kind row, and the
    # tree obviously does contain them (so the absence in meta is meaningful).
    assert "turn" not in meta
    assert [n for n in _walk(forest) if n["kind"] == "turn"]


def test_meta_by_kind_time_stats_are_real_durations():
    meta = _by_kind(store_v2().spoke_meta_by_kind(RUN))

    # Hooks: 100 + 80 + 500 (the Bash-nested hook) = 680 total, median of three = 100.
    assert meta["hook"]["total_duration_ms"] == 680
    assert meta["hook"]["median_duration_ms"] == 100


def test_meta_by_kind_cost_is_subagent_only():
    meta = _by_kind(store_v2().spoke_meta_by_kind(RUN))

    # Only the agent span carries intrinsic owned cost (its subagent pool); every
    # other kind owns nothing — main cost lives on turn nodes, which meta excludes.
    assert meta["agent"]["total_cost_usd"] == pytest.approx(0.35)
    for kind in ("skill", "todo", "tool", "step", "human", "lifecycle", "hook"):
        assert meta[kind]["total_cost_usd"] == 0.0


def test_meta_by_kind_sorts_by_cost_then_count_then_kind():
    rows = store_v2().spoke_meta_by_kind(RUN)

    # agent ($0.35) first; the rest at $0 by count desc then kind asc:
    # todo(4), tool(4) → todo, tool; hook(3); lifecycle(2), step(2); human(1), skill(1).
    assert [row["kind"] for row in rows] == [
        "agent",
        "todo",
        "tool",
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
    assert sum(row["total_cost_usd"] for row in rows) == pytest.approx(0.35)


def test_meta_by_kind_unknown_spoke_is_empty():
    assert store_v2().spoke_meta_by_kind("does/not+exist") == []


# --- S6: todo-text L1 labels (Issue #47) ----------------------------------------


def test_interval_bucket_labelled_by_the_todo_it_advances():
    queries = load_queries()
    spans = [
        _span("l_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("l_anchor", "2026-06-12T12:00:05Z", "2026-06-12T12:00:05Z", phase="anchor"),
        _span(
            "l_todo",
            "2026-06-12T12:00:08Z",
            "2026-06-12T12:00:09Z",
            kind="todo",
            name="TodoWrite",
            summary="Add RED parser test",
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
    spans = [
        _span("f_spawn", "2026-06-12T12:00:00Z", "2026-06-12T12:00:01Z", **_LIFE_NEW),
        _span("f_anchor", "2026-06-12T12:00:05Z", "2026-06-12T12:00:05Z", phase="anchor"),
        _span(
            "f_todo",
            "2026-06-12T12:00:08Z",
            "2026-06-12T12:00:09Z",
            kind="todo",
            name="TodoWrite",
            summary=None,
            phase=None,
        ),
        _span("f_red", "2026-06-12T12:00:15Z", "2026-06-12T12:00:15Z", phase="red"),
        _span("f_teardown", "2026-06-12T12:00:35Z", "2026-06-12T12:00:36Z", **_LIFE_DONE),
    ]
    forest = queries.SpanStore.from_events(spans).spoke_steps("ka/run+1")

    assert _bucket(forest, "red")


# --- S7: display formatting helpers (the thin glue app.py renders) --------------


def test_format_step_label_prefers_summary_over_name():
    queries = load_queries()
    agent = {"kind": "agent", "name": "Plan", "phase": None, "summary": "review S1 changes"}
    bare = {"kind": "agent", "name": "Plan", "phase": None, "summary": None}

    assert queries.format_step_label(agent) == "review S1 changes"
    assert queries.format_step_label(bare) == "Plan"


def test_format_step_label_for_tool_shows_name_and_param():
    queries = load_queries()
    tool = {"kind": "tool", "name": "Read", "phase": None, "summary": "/repo/queries.py"}
    bare = {"kind": "tool", "name": "Bash", "phase": None, "summary": None}

    assert queries.format_step_label(tool) == "Read · /repo/queries.py"
    assert queries.format_step_label(bare) == "Bash"


def test_format_step_label_for_turn_shows_clock_and_model():
    queries = load_queries()
    forest = store_v2().spoke_steps(RUN)
    turn = _turn_at(_bucket(forest, "green"), "12:01:05")

    assert queries.format_step_label(turn) == "turn 12:01:05 · opus-4-8"


def test_format_step_label_per_kind():
    queries = load_queries()
    forest = store_v2().spoke_steps(RUN)
    setup = _bucket(forest, "setup")

    assert queries.format_step_label(setup) == "setup"  # bucket = phase (no todo summary)
    assert queries.format_step_label(_marker(setup, "red")) == "solo-cycle · red"
    assert queries.format_step_label(_find(forest, "v2_agent")) == "write the RED test"
    hooks = next(c for c in setup["children"] if c["kind"] == "hooks")
    assert queries.format_step_label(hooks) == "hooks x2"


def test_format_step_metrics_rolls_up_for_an_interval_bucket():
    queries = load_queries()
    setup = _bucket(store_v2().spoke_steps(RUN), "setup")

    metrics = queries.format_step_metrics(setup)

    assert metrics["time"] == "—"  # intervals are never rendered as durations
    assert metrics["cost"] == "$0.4400"  # 0.09 main + 0.35 nested agent
    assert metrics["tokens"] == "755"  # main 225 + subagent 530
    assert metrics["model"] == "haiku-4-5, opus-4-8"
    # last-event-wins (Issue #57): the bucket shows its terminal (closing-marker)
    # status; the nested hook's warn stays at its own leaf and does not redden it.
    assert metrics["status"] == "success"


def test_format_step_metrics_status_is_the_rolled_up_terminal_not_raw():
    # A real-span container (own status deny) that recovered — its last leaf is a
    # later success — reports the rolled-up terminal status, matching the row icon,
    # not its raw own status (Issue #57). Pins format_step_metrics to the rollup.
    queries = load_queries()
    base = {
        "human_count": 0,
        "own_cost_usd": 0.0,
        "own_tokens_in": 0,
        "own_tokens_out": 0,
        "models": [],
        "duration_ms": 0,
    }
    leaf = {
        **base,
        "kind": "tool",
        "status": "success",
        "ts_start": "2026-06-12T12:00:09Z",
        "children": [],
    }
    agent = {
        **base,
        "kind": "agent",
        "status": "deny",
        "ts_start": "2026-06-12T12:00:00Z",
        "children": [leaf],
    }
    queries._roll_up_steps(agent)

    assert agent["status"] == "deny"  # raw own status is untouched
    assert queries.format_step_metrics(agent)["status"] == "success"


def test_format_step_metrics_for_a_turn_node():
    queries = load_queries()
    turn = _turn_at(_bucket(store_v2().spoke_steps(RUN), "green"), "12:01:05")

    metrics = queries.format_step_metrics(turn)

    assert metrics["time"] == "—"  # a turn is an instant, not a span
    assert metrics["cost"] == "$0.0300"
    assert metrics["model"] == "opus-4-8"


def test_format_step_metrics_blanks_zero_values_on_a_bare_marker():
    queries = load_queries()
    done = _find(store_v2().spoke_steps(RUN), "v2_life_done")

    metrics = queries.format_step_metrics(done)

    assert metrics["cost"] == "—"
    assert metrics["tokens"] == "—"
    assert metrics["model"] == "—"
