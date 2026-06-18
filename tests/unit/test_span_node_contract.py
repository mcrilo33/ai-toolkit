"""v3 spoke-trace span/node contract (Issue #50, Part 1 — RED).

This pins the *contract* the four v3 layers (Parser/Tree/App/Emission) build
against, frozen here so they can then develop concurrently against fixtures:

- three new real-span ``kind`` values — ``workflow``, ``workflow_phase``,
  ``approval`` — added to :data:`telemetry.spans.SPAN_KINDS`;
- three additive, optional, pull-only link fields on the :class:`Span` — ``emits``
  (a ``script`` span names the ``step``/``lifecycle`` marker it emitted),
  ``sidecar_session`` (a ``hook``/``script`` that shells out to a separate
  ``claude -p`` session names that session), and ``agent_link`` (the per-span half
  of the parser's chained ``agent_links``, so agent→agent→… recursion composes);
- the **synthetic-node field contract** — display-only nodes that are *never*
  spans (``interval``, ``turn``, ``hooks``, ``reasoning``, ``context``, ``gap``,
  ``session``, ``scope-band``, ``unresolved``) — expressed as a shared
  :func:`synthetic_node` factory + :class:`SyntheticNode` TypedDict.

Everything is additive: the existing frozen v1 spans (``test_telemetry_span.py``)
and the parser/cost/queries suites stay green.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spans import (
    SPAN_FIELDS,
    SPAN_KINDS,
    SYNTHETIC_KINDS,
    Span,
    synthetic_node,
)

_NEW_KINDS = ("workflow", "workflow_phase", "approval")
_LINK_FIELDS = ("emits", "sidecar_session", "agent_link")
_SYNTHETIC_KINDS = (
    "interval",
    "turn",
    "hooks",
    "reasoning",
    "context",
    "gap",
    "session",
    "scope-band",
    "unresolved",
)


class TestNewSpanKinds:
    @pytest.mark.parametrize("kind", _NEW_KINDS)
    def test_kind_registered(self, kind: str) -> None:
        assert kind in SPAN_KINDS

    @pytest.mark.parametrize("kind", _NEW_KINDS)
    def test_span_constructs_with_new_kind(self, kind: str) -> None:
        span = Span(span_id="s1", kind=kind, name="demo")
        assert span.kind == kind

    def test_legacy_kinds_still_present(self) -> None:
        # Additive only — the frozen v1 kinds must all survive.
        for kind in (
            "lifecycle",
            "step",
            "hook",
            "script",
            "tool",
            "skill",
            "agent",
            "todo",
            "human",
            "rule",
        ):
            assert kind in SPAN_KINDS


class TestLinkFields:
    @pytest.mark.parametrize("field", _LINK_FIELDS)
    def test_field_in_schema(self, field: str) -> None:
        assert field in SPAN_FIELDS

    @pytest.mark.parametrize("field", _LINK_FIELDS)
    def test_defaults_to_none(self, field: str) -> None:
        span = Span(span_id="s1", kind="script", name="demo")
        assert getattr(span, field) is None

    def test_emits_round_trips_in_to_dict(self) -> None:
        span = Span(span_id="s1", kind="script", name="commit-gauntlet", emits="m1")
        assert span.to_dict()["emits"] == "m1"

    def test_sidecar_session_round_trips(self) -> None:
        span = Span(span_id="h1", kind="hook", name="llm-judge", sidecar_session="sess-9")
        assert span.to_dict()["sidecar_session"] == "sess-9"

    def test_agent_link_round_trips(self) -> None:
        span = Span(span_id="a1", kind="agent", name="Explore", agent_link="agent-xyz")
        assert span.to_dict()["agent_link"] == "agent-xyz"

    def test_tokens_and_cost_stay_last(self) -> None:
        # Link fields are inserted before the correlation-filled token/cost tail.
        fields = list(SPAN_FIELDS)
        assert fields[-3:] == ["tokens_in", "tokens_out", "cost_usd"]
        for field in _LINK_FIELDS:
            assert fields.index(field) < fields.index("tokens_in")


class TestHookEventField:
    """Issue #82 — the hook's raising condition is a first-class span field."""

    def test_hook_event_in_schema(self) -> None:
        assert "hook_event" in SPAN_FIELDS

    def test_defaults_to_none(self) -> None:
        span = Span(span_id="h1", kind="hook", name="secrets-scan.sh")
        assert span.hook_event is None

    def test_round_trips_in_to_dict(self) -> None:
        span = Span(span_id="h1", kind="hook", name="secrets-scan.sh", hook_event="PreToolUse")
        assert span.to_dict()["hook_event"] == "PreToolUse"

    def test_stays_before_token_tail(self) -> None:
        fields = list(SPAN_FIELDS)
        assert fields.index("hook_event") < fields.index("tokens_in")


class TestSyntheticNodeContract:
    def test_all_synthetic_kinds_registered(self) -> None:
        assert set(_SYNTHETIC_KINDS) == set(SYNTHETIC_KINDS)

    def test_synthetic_kinds_disjoint_from_spans(self) -> None:
        # A synthetic node is never a span — the two kind namespaces must not collide.
        assert set(SYNTHETIC_KINDS).isdisjoint(SPAN_KINDS)

    def test_factory_marks_node_as_non_span(self) -> None:
        node = synthetic_node(kind="interval", name="S1·RED")
        assert node["span_id"] is None
        assert node["parent_id"] is None

    def test_factory_carries_contract_fields(self) -> None:
        node = synthetic_node(kind="gap", name="idle")
        for key in (
            "kind",
            "name",
            "summary",
            "status",
            "ts_start",
            "ts_end",
            "duration_ms",
            "own_cost_usd",
            "own_tokens_in",
            "own_tokens_out",
            "models",
            "actor",
            "human_count",
            "children",
        ):
            assert key in node

    def test_factory_defaults(self) -> None:
        node = synthetic_node(kind="turn", name="turn")
        assert node["own_cost_usd"] == 0.0
        assert node["own_tokens_in"] == 0
        assert node["own_tokens_out"] == 0
        assert node["models"] == []
        assert node["actor"] == "main"
        assert node["human_count"] == 0
        assert node["status"] == "success"
        assert node["children"] == []

    def test_factory_passes_through_values(self) -> None:
        child = synthetic_node(kind="turn", name="turn")
        node = synthetic_node(
            kind="scope-band",
            name="[scope] generate-tests",
            summary="generate-tests",
            ts_start="2026-06-12T12:00:00Z",
            ts_end="2026-06-12T12:05:00Z",
            actor="main",
            children=[child],
        )
        assert node["name"] == "[scope] generate-tests"
        assert node["summary"] == "generate-tests"
        assert node["ts_start"] == "2026-06-12T12:00:00Z"
        assert node["children"] == [child]

    def test_factory_rejects_non_synthetic_kind(self) -> None:
        with pytest.raises(ValueError, match="synthetic"):
            synthetic_node(kind="tool", name="not synthetic")
