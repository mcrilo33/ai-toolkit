"""Unit tests for the spoke-cost dashboard definitions.

Companion to the spoke-latency dashboard (#128), same contract and same reason it
exists: Langfuse v3.192.x exposes NO public dashboards API, so
``dashboard/langfuse/spoke-cost-dashboard.json`` is the reproducible source of truth
— the dashboard is saved once via the UI from these definitions, and each widget
carries the exact ``/api/public/metrics`` query that backs it. All six queries were
verified live (HTTP 200 with rows) when added.

These tests pin the file to the metrics-API schema — views, measures, aggregations,
filter operators, the high-cardinality dimension ban — so a typo cannot silently
produce an empty widget, and to the score FAMILIES the land-time view builder
actually emits (``langfuse_spoke_tree.py`` + ``telemetry/spoke_tree/scores.py``): a
renamed score must fail here rather than blank a widget in the UI.
"""

import json
from pathlib import Path

import pytest

_DEFINITIONS_PATH = (
    Path(__file__).resolve().parents[2] / "dashboard" / "langfuse" / "spoke-cost-dashboard.json"
)

_WIDGET_COUNT = 6

# The metrics-API query contract — mirrors test_spoke_latency_dashboard.py. Kept as a
# local copy rather than imported: the two dashboards must be able to drift apart (a
# schema change should fail loudly per-dashboard), and the latency file is the sibling
# precedent, not a library.
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
# Only usable as filters, never as grouping dimensions (400 from the metrics API).
_HIGH_CARDINALITY = {"id", "traceId", "userId", "sessionId", "parentObservationId", "observationId"}

# The score families this dashboard reads. Each is emitted by the land-time view
# builder; renaming one there without updating the widget silently blanks it.
_EMITTED_SCORE_FAMILIES = {
    "step_total_cost_usd:",
    "rule_carry_cost_usd:",
    "tooldef_carry_cost_usd:",
    "step_tokens_written:",
    "step_cache_write_usd:",
}


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(_DEFINITIONS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def widgets(dashboard: dict) -> list[dict]:
    return dashboard["widgets"]


class TestDashboardShape:
    def test_names_the_saved_dashboard(self, dashboard: dict) -> None:
        assert dashboard["name"] == "spoke cost"
        assert dashboard["description"]

    def test_has_the_expected_widget_count(self, widgets: list[dict]) -> None:
        assert len(widgets) == _WIDGET_COUNT

    @pytest.mark.parametrize("index", range(_WIDGET_COUNT))
    def test_widget_carries_title_chart_and_query(self, index: int, widgets: list[dict]) -> None:
        widget = widgets[index]

        assert widget["title"]
        assert widget["chart"]
        assert isinstance(widget["metricsQuery"], dict)

    @pytest.mark.parametrize("index", range(_WIDGET_COUNT))
    def test_widget_view_is_supported(self, index: int, widgets: list[dict]) -> None:
        assert widgets[index]["metricsQuery"]["view"] in _VIEWS

    @pytest.mark.parametrize("index", range(_WIDGET_COUNT))
    def test_widget_measures_match_its_view(self, index: int, widgets: list[dict]) -> None:
        query = widgets[index]["metricsQuery"]

        allowed = _MEASURES_BY_VIEW[query["view"]]
        assert {metric["measure"] for metric in query["metrics"]} <= allowed
        assert {metric["aggregation"] for metric in query["metrics"]} <= _AGGREGATIONS

    @pytest.mark.parametrize("index", range(_WIDGET_COUNT))
    def test_widget_filter_operators_match_their_types(
        self, index: int, widgets: list[dict]
    ) -> None:
        filters = widgets[index]["metricsQuery"].get("filters", [])

        assert all(
            f["operator"] in _OPERATORS_BY_TYPE[f["type"]] and f["column"] and "value" in f
            for f in filters
        )

    @pytest.mark.parametrize("index", range(_WIDGET_COUNT))
    def test_widget_dimensions_avoid_high_cardinality(
        self, index: int, widgets: list[dict]
    ) -> None:
        dimensions = widgets[index]["metricsQuery"].get("dimensions", [])

        assert not {d["field"] for d in dimensions} & _HIGH_CARDINALITY

    @pytest.mark.parametrize("index", range(_WIDGET_COUNT))
    def test_widget_query_leaves_the_time_range_to_the_caller(
        self, index: int, widgets: list[dict]
    ) -> None:
        # fromTimestamp/toTimestamp are per-run values; baking one in would pin every
        # verification query to a stale window.
        query = widgets[index]["metricsQuery"]

        assert "fromTimestamp" not in query
        assert "toTimestamp" not in query


class TestReadsTheScoresWeEmit:
    def test_every_widget_filters_on_an_emitted_score_family(self, widgets: list[dict]) -> None:
        # Each widget must target a family the land-time builder emits — otherwise it
        # renders empty in the UI and nothing else catches it.
        for widget in widgets:
            values = [f["value"] for f in widget["metricsQuery"].get("filters", [])]
            assert any(isinstance(v, str) and v in _EMITTED_SCORE_FAMILIES for v in values), (
                f"widget {widget['title']!r} filters on no known score family"
            )

    def test_covers_the_cost_families_that_motivated_the_dashboard(
        self, widgets: list[dict]
    ) -> None:
        # The point of this dashboard: per-step cost AND the carry cost of rules and
        # tool definitions (paid on every call). Losing either defeats it.
        targeted = {
            f["value"]
            for widget in widgets
            for f in widget["metricsQuery"].get("filters", [])
            if isinstance(f.get("value"), str)
        }

        assert "step_total_cost_usd:" in targeted
        assert "rule_carry_cost_usd:" in targeted
        assert "tooldef_carry_cost_usd:" in targeted

    def test_scores_widgets_group_by_score_name(self, widgets: list[dict]) -> None:
        # `name` is the only dimension that separates the per-step / per-rule / per-tool
        # series; without it every family collapses to one bar.
        for widget in widgets:
            if widget["metricsQuery"]["view"].startswith("scores"):
                fields = {d["field"] for d in widget["metricsQuery"].get("dimensions", [])}
                assert "name" in fields, f"{widget['title']!r} must group by score name"
