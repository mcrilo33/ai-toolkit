"""Structural checks on the Langfuse OpenTelemetry collector config.

These do not run a collector; they assert that ``dashboard/langfuse/otelcol.yaml``
declares the receivers, pipeline wiring, and OTTL attribute maps the spoke
telemetry depends on (issue #88). A real collector run is an integration concern;
these unit checks guard the config from silent drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
OTELCOL_CONFIG = REPO_ROOT / "dashboard" / "langfuse" / "otelcol.yaml"


def _load_config() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(OTELCOL_CONFIG.read_text(encoding="utf-8")))


def _span_statements(config: dict[str, Any]) -> list[str]:
    """Collect every OTTL statement that runs in span context for transform/langfuse."""
    processors = config["processors"]
    blocks = processors["transform/langfuse"]["trace_statements"]
    statements: list[str] = []
    for block in blocks:
        if block.get("context") == "span":
            statements.extend(block["statements"])
    return statements


def _port(endpoint: str) -> str:
    return endpoint.rsplit(":", 1)[-1]


def test_normal_receiver_offers_grpc_and_http() -> None:
    # The spoke exports its normal stream over gRPC (the beta detailed exporter is
    # HTTP-only, so normal takes gRPC — the arrangement proven to land in Langfuse).
    # The http listener is retained for backward compat + the message-bridge fork.
    config = _load_config()

    protocols = config["receivers"]["otlp"]["protocols"]

    assert "grpc" in protocols, "normal otlp must accept gRPC (the spoke's normal export)"
    assert "http" in protocols, "normal otlp must keep http for backward compat + bridge"


def test_beta_receiver_is_http_on_a_distinct_port() -> None:
    # The beta detailed exporter speaks HTTP, so its receiver must be HTTP — and on a
    # different port than EVERY normal endpoint, or it silently kills all trace+log
    # export (the probe footgun). A gRPC beta receiver would never receive the stream.
    config = _load_config()

    receivers = config["receivers"]

    assert "otlp/beta" in receivers, "expected a dedicated otlp/beta receiver"
    beta_endpoint = receivers["otlp/beta"]["protocols"]["http"]["endpoint"]
    normal = receivers["otlp"]["protocols"]
    normal_ports = {_port(normal["grpc"]["endpoint"]), _port(normal["http"]["endpoint"])}
    assert _port(beta_endpoint) not in normal_ports, "beta port must differ from every normal port"


def test_traces_pipeline_consumes_both_receivers() -> None:
    # Both the normal and beta receivers feed the same Langfuse traces pipeline, so
    # the detailed spans get the same token-remap / span-rename / content transforms.
    config = _load_config()

    receivers = config["service"]["pipelines"]["traces"]["receivers"]

    assert "otlp" in receivers
    assert "otlp/beta" in receivers


def test_model_output_maps_to_observation_output() -> None:
    # Detailed tracing's response.model_output (the assistant output) is mapped onto
    # langfuse.observation.output so Langfuse renders it as the span's output.
    config = _load_config()

    statements = _span_statements(config)

    assert any(
        'set(attributes["langfuse.observation.output"], attributes["response.model_output"])'
        in stmt
        for stmt in statements
    ), "response.model_output must map to langfuse.observation.output"


def test_detailed_context_attrs_map_to_metadata() -> None:
    # new_context (per-turn delta) and system_reminders ride detailed-tracing spans;
    # they map under langfuse.observation.metadata.* so they surface as filterable
    # top-level metadata keys (a bare attribute would nest under metadata.attributes).
    config = _load_config()

    statements = _span_statements(config)

    assert any(
        'set(attributes["langfuse.observation.metadata.new_context"], attributes["new_context"])'
        in stmt
        for stmt in statements
    ), "new_context must map to langfuse.observation.metadata.new_context"
    assert any(
        'set(attributes["langfuse.observation.metadata.system_reminders"], '
        'attributes["system_reminders"])' in stmt
        for stmt in statements
    ), "system_reminders must map to langfuse.observation.metadata.system_reminders"


def test_beta_tools_and_system_prompt_preview_map_to_metadata() -> None:
    # The beta detailed stream carries `tools` (name + hash per available tool, for
    # schema-drift detection) and `system_prompt_preview` (first ~500 chars). The
    # span-name rewrite keeps only the tool name, so the hash must be preserved onto a
    # metadata attribute; both map under langfuse.observation.metadata.* so they surface
    # as filterable top-level keys (a bare attribute would nest under metadata.attributes).
    config = _load_config()

    statements = _span_statements(config)

    assert any(
        'set(attributes["langfuse.observation.metadata.tools"], attributes["tools"])' in stmt
        for stmt in statements
    ), "beta tools (name+hash) must map to langfuse.observation.metadata.tools"
    assert any(
        'set(attributes["langfuse.observation.metadata.system_prompt_preview"], '
        'attributes["system_prompt_preview"])' in stmt
        for stmt in statements
    ), "system_prompt_preview must map to langfuse.observation.metadata.system_prompt_preview"


def test_metrics_pipeline_routes_to_a_non_langfuse_sink() -> None:
    # Claude Code's claude_code.token.usage / cost.usage metrics flush to the
    # collector, but Langfuse is NOT a metrics store — they must route to a
    # dedicated sink (Prometheus/console), never to the Langfuse exporter, or
    # they would be silently dropped (no metrics pipeline) or misrouted.
    config = _load_config()

    metrics = config["service"]["pipelines"]["metrics"]

    assert metrics["receivers"] == ["otlp"], "metrics arrive on the otlp receiver"
    assert "otlphttp/langfuse" not in metrics["exporters"], "Langfuse is not a metrics store"
    assert "otlphttp/bridge" not in metrics["exporters"], (
        "metrics must not reach the message bridge"
    )
    assert "prometheus" in metrics["exporters"], "metrics route to the Prometheus sink"


def test_logs_pipeline_routes_to_the_message_bridge() -> None:
    # The audit/lifecycle event layer (tool_decision, mcp_server_connection, compaction, ...)
    # rides the logs signal, and the bridge is its only consumer (issue #93): it maps each
    # event onto a per-spoke Langfuse trace. Guard the logs->bridge route from drift — if it
    # broke, the whole audit layer would silently stop reaching Langfuse.
    config = _load_config()

    logs = config["service"]["pipelines"]["logs"]

    assert logs["receivers"] == ["otlp"], "audit/body log events arrive on the otlp receiver"
    assert "otlphttp/bridge" in logs["exporters"], "the bridge is the logs signal's consumer"
    assert "otlphttp/langfuse" not in logs["exporters"], (
        "Langfuse trace ingestion does not read the logs signal; the bridge bridges it"
    )


def test_prometheus_sink_listens_on_a_port_distinct_from_the_receivers() -> None:
    # The metrics sink's scrape endpoint must not collide with any OTLP receiver port,
    # or the collector fails to bind — exactly the kind of drift these checks guard.
    config = _load_config()

    prom_port = _port(config["exporters"]["prometheus"]["endpoint"])
    receivers = config["receivers"]
    receiver_ports = {
        _port(receivers["otlp"]["protocols"]["grpc"]["endpoint"]),
        _port(receivers["otlp"]["protocols"]["http"]["endpoint"]),
        _port(receivers["otlp/beta"]["protocols"]["http"]["endpoint"]),
    }
    assert prom_port not in receiver_ports, "Prometheus port must not collide with a receiver"


def test_environment_stamped_on_spans() -> None:
    # #231: every real spoke span is stamped langfuse.environment so a Langfuse dashboard can
    # scope to environment=production and exclude test/fixture traffic. A deployment.environment
    # resource attr (a test collector) overrides; otherwise it defaults to production.
    statements = _span_statements(_load_config())

    env_stmts = [s for s in statements if "langfuse.environment" in s]
    assert env_stmts, "a transform must set langfuse.environment on spoke spans"
    assert any('"production"' in s for s in env_stmts), (
        "real spoke spans must default to the production environment"
    )
