"""Unit tests for the spoke-outcomes dashboard definitions (Issue #348).

Fourth in the #128 family (spoke-latency / spoke-cost / entity-tracking), same
contract and same reason it exists: Langfuse v3.192.x exposes NO public dashboards
API, so ``dashboard/langfuse/spoke-outcomes-dashboard.json`` is the reproducible
source of truth — the dashboard is saved once via the UI from these definitions, and
each widget carries the exact ``/api/public/metrics`` query that backs it. All widget
queries were verified live (HTTP 200 with rows) against the local instance 2026-08-17.

This dashboard closes the gap where the #231 outcome family and the #280 throughput
family had scores/tags but ZERO widgets: cost-by-model (the governance widget that
would have caught the #291/#305/#306 model-config leaks at a glance), terminal outcomes
(landed/blocked/reaped/abandoned), per-stage overhead, drain KPIs, and per-script
success rate + volume.

These tests pin the file to the metrics-API schema — views, measures, aggregations,
filter operators, the high-cardinality dimension ban — so a typo cannot silently
produce an empty widget, and to the score/tag FAMILIES the land-time view builder
actually emits (``langfuse_spoke_tree.py`` + ``telemetry/spoke_tree/scores.py`` +
``telemetry/spoke_tree/metadata.py``): a renamed family must fail here rather than blank
a widget in the UI. It also pins the deliberate EXCLUSION of the normalization family
(#344 documents its false zeros), so a well-meaning add cannot poison the view before
#344 lands.
"""

import json
from pathlib import Path

import pytest

_DEFINITIONS_PATH = (
    Path(__file__).resolve().parents[2] / "dashboard" / "langfuse" / "spoke-outcomes-dashboard.json"
)

_WIDGET_COUNT = 6

# The metrics-API query contract — mirrors test_entity_tracking_dashboard.py. Kept as a
# local copy rather than imported: each dashboard must be able to drift apart (a schema
# change should fail loudly per-dashboard), and the sibling files are precedent, not a
# library. Extended here with the ``traces`` view (the outcomes widget groups by the
# trace ``tags`` dimension) and the ``arrayOptions`` filter type it uses.
_VIEWS = {"observations", "scores-numeric", "scores-categorical", "traces"}
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
    "traces": {
        "count",
        "latency",
        "totalCost",
        "totalTokens",
        "observationsCount",
        "scoresCount",
    },
}
_OPERATORS_BY_TYPE = {
    "string": {"=", "contains", "does not contain", "starts with", "ends with"},
    "stringOptions": {"any of", "none of"},
    "arrayOptions": {"any of", "none of"},
    "number": {"=", ">", "<", ">=", "<="},
    "boolean": {"=", "<>"},
}
# Only usable as filters, never as grouping dimensions (400 from the metrics API).
_HIGH_CARDINALITY = {"id", "traceId", "userId", "sessionId", "parentObservationId", "observationId"}

# The outcome tags this dashboard groups on — ``apply_outcome_tag`` in metadata.py surfaces
# each terminal state as an ``outcome:<value>`` trace tag (#231).
_OUTCOME_TAGS = {"outcome:landed", "outcome:blocked", "outcome:reaped", "outcome:abandoned"}
# The drain KPI score names the throughput family emits (#280 / scores.py).
_DRAIN_KPIS = {"issues_per_hour", "overhead_work_ratio", "autonomy_score", "wall_per_subtask"}
# The per-stage overhead score family and per-script success family (starts-with filters).
_STAGE_FAMILY = "stage_"
_SCRIPT_SUCCESS_FAMILY = "script_success:"
# The normalization family EXCLUDED on purpose: #344 documents these emit false zeros on
# every land; charting them before #344 lands poisons the view. A widget filtering on any
# of these must fail here.
_NORMALIZATION_EXCLUDED = {"files_changed", "lines_changed", "commits", "cost_per_changed_line"}


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(_DEFINITIONS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def widgets(dashboard: dict) -> list[dict]:
    return dashboard["widgets"]


def _filter_values(widget: dict) -> list:
    """Return every ``value`` across a widget's filters (str or list)."""
    return [f.get("value") for f in widget["metricsQuery"].get("filters", [])]


def _flat_filter_values(widget: dict) -> set[str]:
    """Return every string filter value a widget carries, flattening list-valued filters."""
    flat: set[str] = set()
    for value in _filter_values(widget):
        if isinstance(value, str):
            flat.add(value)
        elif isinstance(value, list):
            flat.update(v for v in value if isinstance(v, str))
    return flat


class TestDashboardShape:
    def test_names_the_saved_dashboard(self, dashboard: dict) -> None:
        assert dashboard["name"] == "spoke outcomes"
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


class TestGovernanceWidget:
    """Cost-by-model is the widget that would have caught the model-config leaks (#291/#305/#306)."""

    def test_charts_cost_by_provided_model(self, widgets: list[dict]) -> None:
        model_widgets = [
            w
            for w in widgets
            if {d["field"] for d in w["metricsQuery"].get("dimensions", [])}
            == {"providedModelName"}
        ]

        assert model_widgets, "no widget breaks cost down by providedModelName"
        for widget in model_widgets:
            query = widget["metricsQuery"]
            assert query["view"] == "observations"
            assert ("totalCost", "sum") in {
                (m["measure"], m["aggregation"]) for m in query["metrics"]
            }
            assert ("type", "=", "GENERATION") in {
                (f["column"], f["operator"], f["value"]) for f in query["filters"]
            }

    def test_has_a_snapshot_and_a_drift_over_time_copy(self, widgets: list[dict]) -> None:
        # The issue asks for both a snapshot bar AND a daily time-series copy so a tier
        # leak shows up as a rising band, not just a totalled bar.
        model_widgets = [
            w
            for w in widgets
            if {d["field"] for d in w["metricsQuery"].get("dimensions", [])}
            == {"providedModelName"}
        ]
        has_time = [w for w in model_widgets if "timeDimension" in w["metricsQuery"]]
        no_time = [w for w in model_widgets if "timeDimension" not in w["metricsQuery"]]

        assert has_time, "cost-by-model needs a daily time-series copy for drift-over-time"
        assert no_time, "cost-by-model needs a snapshot copy"


class TestReadsWhatWeEmit:
    def test_charts_the_terminal_outcome_tags(self, widgets: list[dict]) -> None:
        # The #231 outcome family: group traces by the outcome:<state> tag.
        outcome_widget = next(
            (w for w in widgets if "tags" in {d["field"] for d in w["metricsQuery"]["dimensions"]}),
            None,
        )
        assert outcome_widget is not None, "no widget groups by the trace tags dimension"
        assert outcome_widget["metricsQuery"]["view"] == "traces"
        assert _flat_filter_values(outcome_widget) >= _OUTCOME_TAGS

    def test_charts_the_per_stage_overhead_family(self, widgets: list[dict]) -> None:
        stage_widget = next(
            (
                w
                for w in widgets
                if _STAGE_FAMILY in _flat_filter_values(w)
                or any(
                    f.get("value") == _STAGE_FAMILY for f in w["metricsQuery"].get("filters", [])
                )
            ),
            None,
        )
        assert stage_widget is not None, "no widget filters on the stage_ overhead family"
        query = stage_widget["metricsQuery"]
        assert query["view"] == "scores-numeric"
        assert {d["field"] for d in query["dimensions"]} == {"name"}
        assert ("value", "sum") in {(m["measure"], m["aggregation"]) for m in query["metrics"]}

    def test_charts_the_drain_kpis(self, widgets: list[dict]) -> None:
        kpi_widget = next((w for w in widgets if _flat_filter_values(w) & _DRAIN_KPIS), None)
        assert kpi_widget is not None, "no widget charts the drain KPI throughput family"
        query = kpi_widget["metricsQuery"]
        assert query["view"] == "scores-numeric"
        assert _flat_filter_values(kpi_widget) >= _DRAIN_KPIS
        # KPIs are per-window rates; averaging (not summing) keeps them comparable.
        assert ("value", "avg") in {(m["measure"], m["aggregation"]) for m in query["metrics"]}

    def test_charts_per_script_success_rate_and_volume(self, widgets: list[dict]) -> None:
        script_widget = next(
            (w for w in widgets if _SCRIPT_SUCCESS_FAMILY in _flat_filter_values(w)), None
        )
        assert script_widget is not None, "no widget filters on the script_success: family"
        query = script_widget["metricsQuery"]
        assert query["view"] == "scores-numeric"
        assert {d["field"] for d in query["dimensions"]} == {"name"}
        pairs = {(m["measure"], m["aggregation"]) for m in query["metrics"]}
        # avg -> success rate; count -> volume. Both, so a low-volume script is not read
        # as an equal peer of a high-volume one.
        assert ("value", "avg") in pairs
        assert ("count", "count") in pairs

    def test_scores_widgets_group_by_score_name(self, widgets: list[dict]) -> None:
        # `name` is the only dimension separating the per-stage / per-KPI / per-script series;
        # without it every family collapses to one bar.
        for widget in widgets:
            if widget["metricsQuery"]["view"] == "scores-numeric":
                fields = {d["field"] for d in widget["metricsQuery"].get("dimensions", [])}
                assert "name" in fields, f"{widget['title']!r} must group by score name"


class TestExcludesTheNormalizationFamily:
    def test_no_widget_charts_a_normalization_score(self, widgets: list[dict]) -> None:
        # #344: files_changed / lines_changed / commits / cost_per_changed_line emit false
        # zeros on every land. Charting one before #344 lands poisons the view.
        for widget in widgets:
            charted = _flat_filter_values(widget)
            assert not (charted & _NORMALIZATION_EXCLUDED), (
                f"widget {widget['title']!r} charts an excluded normalization score"
            )

    def test_header_notes_the_normalization_exclusion(self, dashboard: dict) -> None:
        # The caveat must be visible in the file the operator reads, the way the sibling
        # dashboards carry their caveats in the description.
        assert "#344" in dashboard["description"]


class TestScopedToProduction:
    def test_every_widget_carries_the_production_scope_note(self, widgets: list[dict]) -> None:
        # The store holds ~10k fixture sessions; without the environment=production scope
        # every widget silently mixes fixtures into the numbers. The one-switch recipe is
        # the dashboard-level Environment selector, surfaced per widget so the operator
        # cannot miss it. Pinned so a title edit cannot drop the note silently.
        for widget in widgets:
            assert "environment=production" in widget["title"], (
                f"widget {widget['title']!r} is missing the production scope note"
            )
