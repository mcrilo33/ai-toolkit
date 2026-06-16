"""Golden spoke fixture coverage (Issue #50, Part 4 — RED).

Ships one representative fixture — the ``feature/47`` shape — that every v3
spoke-trace layer (Parser / Tree / App / Emission) tests against, so they develop
concurrently against the same data. These tests are the **coverage contract**: the
fixture must exercise all eight hard shapes, and each must stay present.

The eight shapes (one per acceptance-criterion bullet):

1. **overnight / resume** — spans cross a calendar-date boundary and the spoke
   spans more than one ``session_id`` (a resume).
2. **workflow fan-out** — a ``workflow`` → ``workflow_phase`` → ``agent`` chain.
3. **marker churn** — consecutive near-empty ``step`` markers.
4. **no-marker phase** — a phase started by a todo ``in_progress`` with activity
   but no ``step`` marker.
5. **deep recursion** — an ``agent`` → ``agent`` → ``agent`` parent chain.
6. **sidecar session** — a ``hook``/``script`` with ``sidecar_session`` set.
7. **deny / blocked** — an ``approval`` denied and the ``tool`` it blocked.
8. **loaded context** — ``rule`` spans covering the rule / memory / tool-schema
   sub-types.

The fixture also conforms to the Part 1 span contract (every line is a valid
:class:`telemetry.spans.Span`) and ingests cleanly into the dashboard's
``SpanStore`` (``spoke_steps`` builds without error). The loaders live inside the
test bodies so a missing fixture fails as a normal assertion (exit 1), not a
collection error.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _dashboard_helpers import store_from
from telemetry.spans import SPAN_FIELDS, Span

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPANS = _FIXTURES / "dashboard_golden_spoke.jsonl"
TURNS = _FIXTURES / "dashboard_golden_spoke_turns.jsonl"
SPOKE_RUN_ID = "feature/47+1700000000"


def _spans() -> list[dict]:
    return [json.loads(line) for line in SPANS.read_text().splitlines() if line.strip()]


def _by_id(spans: list[dict]) -> dict[str, dict]:
    return {s["span_id"]: s for s in spans}


class TestEightShapes:
    def test_overnight_and_resume(self) -> None:
        spans = _spans()
        dates = {date.fromisoformat(s["ts_start"][:10]) for s in spans if s.get("ts_start")}
        sessions = {s["session_id"] for s in spans if s.get("session_id")}
        assert len(dates) >= 2, "spans must cross a calendar-date boundary (overnight)"
        assert len(sessions) >= 2, "spoke must span >1 session_id (resume)"

    def test_workflow_fan_out_chain(self) -> None:
        spans = _spans()
        workflows = {s["span_id"] for s in spans if s["kind"] == "workflow"}
        phases = [s for s in spans if s["kind"] == "workflow_phase"]
        agents = [s for s in spans if s["kind"] == "agent"]
        assert workflows, "no workflow span"
        phase_ids = {p["span_id"] for p in phases if p["parent_id"] in workflows}
        assert phase_ids, "no workflow_phase under a workflow"
        assert any(a["parent_id"] in phase_ids for a in agents), "no agent under a workflow_phase"

    def test_marker_churn(self) -> None:
        spans = _spans()
        steps = sorted((s for s in spans if s["kind"] == "step"), key=lambda s: s["ts_start"])
        near_empty = [s for s in steps if (s["duration_ms"] or 0) <= 500]
        assert len(near_empty) >= 2, "need >=2 near-empty step markers (churn)"

    def test_no_marker_phase(self) -> None:
        spans = _spans()
        # The resume session carries todo + tool activity but emits NO step marker:
        # the phase has to be synthesized from the todo transition, never a marker.
        resume_session = "sess-47b"
        todos = [
            s
            for s in spans
            if s["kind"] == "todo" and s["session_id"] == resume_session and s.get("summary")
        ]
        steps_in_resume = [
            s for s in spans if s["kind"] == "step" and s["session_id"] == resume_session
        ]
        tools_in_resume = [
            s for s in spans if s["kind"] == "tool" and s["session_id"] == resume_session
        ]
        assert todos, "no summarised todo in the resume session"
        assert tools_in_resume, "the no-marker phase must contain real activity"
        assert not steps_in_resume, "the no-marker phase must have no step marker"

    def test_deep_agent_recursion(self) -> None:
        spans = _spans()
        by_id = _by_id(spans)

        def agent_depth(span: dict) -> int:
            depth, cur = 1, span
            while cur.get("parent_id") and cur["parent_id"] in by_id:
                parent = by_id[cur["parent_id"]]
                if parent["kind"] != "agent":
                    break
                depth, cur = depth + 1, parent
            return depth

        deepest = max((agent_depth(s) for s in spans if s["kind"] == "agent"), default=0)
        assert deepest >= 3, f"need an agent->agent->agent chain, got depth {deepest}"

    def test_sidecar_session(self) -> None:
        spans = _spans()
        sidecars = [
            s for s in spans if s.get("sidecar_session") and s["kind"] in ("hook", "script")
        ]
        assert sidecars, "no hook/script span with a sidecar_session link"

    def test_deny_blocked(self) -> None:
        spans = _spans()
        denied_approval = [s for s in spans if s["kind"] == "approval" and s["status"] == "deny"]
        blocked_tool = [s for s in spans if s["kind"] == "tool" and s["status"] == "deny"]
        assert denied_approval, "no denied approval"
        assert blocked_tool, "no blocked (never-run) tool"

    def test_loaded_context_subtypes(self) -> None:
        spans = _spans()
        ctx = defaultdict(list)
        for s in spans:
            if s["kind"] == "rule":
                ctx[s["phase"]].append(s)
        assert {"rule", "memory", "tool-schema"} <= set(ctx), (
            f"loaded context must cover rule/memory/tool-schema, got {sorted(ctx)}"
        )


class TestFixtureConformsToContract:
    def test_every_line_is_a_valid_span(self) -> None:
        for raw in _spans():
            present = {k: v for k, v in raw.items() if k in SPAN_FIELDS}
            span = Span(**present)  # raises ValueError on an unknown kind
            assert span.spoke_run_id == SPOKE_RUN_ID

    def test_new_v3_kinds_and_links_exercised(self) -> None:
        spans = _spans()
        kinds = {s["kind"] for s in spans}
        assert {"workflow", "workflow_phase", "approval"} <= kinds
        assert any(s.get("emits") for s in spans), "emission link not exercised"
        assert any(s.get("agent_link") for s in spans), "agent_link not exercised"
        assert any(s.get("sidecar_session") for s in spans), "sidecar link not exercised"

    def test_fixture_ingests_and_builds_a_forest(self) -> None:
        store = store_from(SPANS, TURNS)
        forest = store.spoke_steps(SPOKE_RUN_ID)
        assert forest, "spoke_steps built an empty forest from the fixture"


def _walk(nodes: list[dict]):
    for node in nodes:
        yield node
        yield from _walk(node.get("children", []))


class TestEmissionRendering:
    """Track E render layer (Issue #54): a control script is a first-class node in
    the v3 forest, and the ``emits`` link it carries (the marker it produced) is
    surfaced on that node so the trace can draw the script→marker chain.
    """

    def test_script_span_renders_as_a_node(self) -> None:
        store = store_from(SPANS, TURNS)
        forest = store.spoke_steps(SPOKE_RUN_ID)
        scripts = [n for n in _walk(forest) if n["kind"] == "script"]
        assert scripts, "no kind=script run-node in the v3 forest"
        assert any(n["name"] == "commit-gauntlet" for n in scripts)

    def test_script_node_carries_its_emission_link(self) -> None:
        # The golden's commit-gauntlet script span emits the red step marker
        # (g_script_red.emits == "g_red"); the rendered node must surface that link
        # so the script→marker chain is drawable.
        raw = _by_id(_spans())
        script_raw = next(s for s in _spans() if s["kind"] == "script")
        marker_id = script_raw["emits"]
        assert marker_id and raw[marker_id]["kind"] in ("step", "lifecycle")

        store = store_from(SPANS, TURNS)
        forest = store.spoke_steps(SPOKE_RUN_ID)
        script_node = next(n for n in _walk(forest) if n["kind"] == "script")
        assert script_node.get("emits") == marker_id

    def test_emits_key_absent_when_link_is_null(self) -> None:
        # Conditional surfacing (Hazard B): nodes without an emission link must NOT
        # gain an `emits` key, so the frozen v1/v2 golden forest stays byte-identical.
        store = store_from(SPANS, TURNS)
        forest = store.spoke_steps(SPOKE_RUN_ID)
        unlinked = [n for n in _walk(forest) if n["kind"] in ("turn", "interval")]
        assert unlinked, "expected synthetic nodes in the forest"
        assert all("emits" not in n for n in unlinked)
