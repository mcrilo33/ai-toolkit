"""Unit tests for the spoke-latency dashboard definitions (Issue #128).

Langfuse v3.192.x exposes NO public dashboards API (no ``dashboards`` resource in the
local instance's OpenAPI spec nor in langfuse-cli's schema), so the checked-in
definitions file ``dashboard/langfuse/spoke-latency-dashboard.json`` is the
reproducible source of truth: the dashboard is saved once via the UI from these
definitions, and each widget carries the exact ``/api/public/v2/metrics`` query that
backs it. These tests pin the file to the v2 metrics schema — views, measures,
aggregations, filter operators, the high-cardinality dimension ban — so a typo cannot
silently produce an empty widget, and to the span/score names this repo actually
emits.
"""

import json
from pathlib import Path

import pytest

_DEFINITIONS_PATH = (
    Path(__file__).resolve().parents[2] / "dashboard" / "langfuse" / "spoke-latency-dashboard.json"
)

# The /api/public/v2/metrics contract (from the endpoint's OpenAPI description).
_VIEWS = {"observations", "scores-numeric", "scores-categorical"}
_AGGREGATIONS = {
    "sum",
    "avg",
    "count",
    "max",
    "min",
    "p50",
    "p75",
    "p90",
    "p95",
    "p99",
    "histogram",
}
_MEASURES_BY_VIEW = {
    "observations": {
        "count",
        "latency",
        "streamingLatency",
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "outputTokensPerSecond",
        "tokensPerSecond",
        "inputCost",
        "outputCost",
        "totalCost",
        "timeToFirstToken",
        "countScores",
    },
    "scores-numeric": {"count", "value"},
    "scores-categorical": {"count"},
}
_OPERATORS_BY_TYPE = {
    "string": {"=", "contains", "does not contain", "starts with", "ends with"},
    "stringOptions": {"any of", "none of"},
    "number": {"=", ">", "<", ">=", "<="},
    "boolean": {"=", "<>"},
}
# Only usable as filters, never as grouping dimensions (400 from the v2 API).
_HIGH_CARDINALITY = {"id", "traceId", "userId", "sessionId", "parentObservationId", "observationId"}


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(_DEFINITIONS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def widgets(dashboard: dict) -> list[dict]:
    return dashboard["widgets"]


class TestDashboardShape:
    def test_names_the_saved_dashboard(self, dashboard: dict) -> None:
        assert dashboard["name"] == "spoke latency"
        assert dashboard["description"]

    def test_has_exactly_four_widgets(self, widgets: list[dict]) -> None:
        assert len(widgets) == 4

    @pytest.mark.parametrize("index", range(4))
    def test_widget_carries_title_chart_and_query(self, index: int, widgets: list[dict]) -> None:
        widget = widgets[index]

        assert widget["title"]
        assert widget["chart"]
        assert isinstance(widget["metricsQuery"], dict)

    @pytest.mark.parametrize("index", range(4))
    def test_widget_view_is_supported(self, index: int, widgets: list[dict]) -> None:
        assert widgets[index]["metricsQuery"]["view"] in _VIEWS

    @pytest.mark.parametrize("index", range(4))
    def test_widget_measures_match_its_view(self, index: int, widgets: list[dict]) -> None:
        query = widgets[index]["metricsQuery"]

        allowed = _MEASURES_BY_VIEW[query["view"]]
        assert {metric["measure"] for metric in query["metrics"]} <= allowed
        assert {metric["aggregation"] for metric in query["metrics"]} <= _AGGREGATIONS

    @pytest.mark.parametrize("index", range(4))
    def test_widget_filter_operators_match_their_types(
        self, index: int, widgets: list[dict]
    ) -> None:
        filters = widgets[index]["metricsQuery"].get("filters", [])

        assert all(
            f["operator"] in _OPERATORS_BY_TYPE[f["type"]] and f["column"] and "value" in f
            for f in filters
        )

    @pytest.mark.parametrize("index", range(4))
    def test_widget_dimensions_avoid_high_cardinality(
        self, index: int, widgets: list[dict]
    ) -> None:
        dimensions = widgets[index]["metricsQuery"].get("dimensions", [])

        assert not {d["field"] for d in dimensions} & _HIGH_CARDINALITY

    @pytest.mark.parametrize("index", range(4))
    def test_widget_query_leaves_the_time_range_to_the_caller(
        self, index: int, widgets: list[dict]
    ) -> None:
        # fromTimestamp/toTimestamp are per-run values; baking one into the definitions
        # would silently pin every verification query to a stale window.
        query = widgets[index]["metricsQuery"]

        assert "fromTimestamp" not in query
        assert "toTimestamp" not in query


class TestWidgetSemantics:
    """Each widget targets the span/score names this repo actually emits."""

    def test_step_widget_targets_cycle_step_spans(self, widgets: list[dict]) -> None:
        query = widgets[0]["metricsQuery"]

        assert query["view"] == "observations"
        assert {d["field"] for d in query["dimensions"]} == {"name"}
        assert {(f["column"], f["operator"], f["value"]) for f in query["filters"]} == {
            ("name", "starts with", "step:")
        }
        assert {m["aggregation"] for m in query["metrics"]} == {"p50", "p95"}

    def test_script_widget_covers_the_gate_bearing_scripts(self, widgets: list[dict]) -> None:
        query = widgets[1]["metricsQuery"]
        name_filter = next(f for f in query["filters"] if f["column"] == "name")

        # The emitted labels: wt_emit_script names (no phase -> bare name) and the
        # spoke-ready marker labels (kind:phase). spoke-push's window covers the
        # pre-push test gate.
        assert {"worktree-land", "spoke-push", "script:gate"} <= set(name_filter["value"])
        assert name_filter["operator"] == "any of"
        assert {m["measure"] for m in query["metrics"]} == {"latency"}

    def test_llm_widget_breaks_down_latency_by_model(self, widgets: list[dict]) -> None:
        query = widgets[2]["metricsQuery"]

        assert {d["field"] for d in query["dimensions"]} == {"providedModelName"}
        assert ("type", "=", "GENERATION") in {
            (f["column"], f["operator"], f["value"]) for f in query["filters"]
        }
        assert {m["measure"] for m in query["metrics"]} == {"latency"}

    def test_wait_widget_sums_the_wait_scores(self, widgets: list[dict]) -> None:
        query = widgets[3]["metricsQuery"]
        name_filter = next(f for f in query["filters"] if f["column"] == "name")

        assert query["view"] == "scores-numeric"
        assert set(name_filter["value"]) == {"gate_park_ms", "permission_wait_ms"}
        assert ("value", "sum") in {(m["measure"], m["aggregation"]) for m in query["metrics"]}
