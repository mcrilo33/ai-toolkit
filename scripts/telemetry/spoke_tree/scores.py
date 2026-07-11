"""Numeric Langfuse scores that make a spoke's metadata chartable (#100, #101, #158).

Langfuse dashboards can sum/aggregate numeric SCORES but not arbitrary observation metadata, so
signals already present as metadata are ALSO emitted as scores: :func:`build_score_events` emits
per-tool ``permission_wait_ms`` / ``tool_result_size`` and the trace-level ``gate_park_ms`` (from
:func:`~telemetry.spoke_tree.commits._gate_park_ms`); :func:`build_step_cost_scores` emits per-phase
``step_cache_write_usd`` / ``step_tokens_written`` from View B's step rollups; and
:func:`build_step_total_cost_scores` emits per-phase ``step_total_cost_usd`` — the true all-
generations cost windowed onto each step (#230). Depends on the foundation, ``ids``, ``steps``,
and ``commits``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from telemetry.spoke_tree.commits import _gate_park_ms
from telemetry.spoke_tree.cycle import _POST_STEP_KEY, _PRE_STEP_KEY
from telemetry.spoke_tree.ids import _CYCLE_STEP_PREFIX, cycle_trace_id_for, trace_id_for
from telemetry.spoke_tree.observations import (
    _POST_STEP_NAME,
    _PRE_STEP_NAME,
    _SUB_AGENT_PREFIX,
    IngestEvent,
    TraceObservations,
    _attr,
    _is_hook_event,
    _llm_requests_in_order,
    _parse_utc,
)

# Deterministic id prefix for the numeric Langfuse scores (#100 amendment: chartable time budget).
_SCORE_PREFIX = "tree-score-"
# Score names — Langfuse sums/charts numeric scores (it cannot chart arbitrary metadata).
_PERMISSION_WAIT_SCORE = "permission_wait_ms"  # per blocked tool observation
_GATE_PARK_SCORE = "gate_park_ms"  # trace-level PLAN-gate park wait
_TOOL_RESULT_SIZE_SCORE = "tool_result_size"  # bytes of a tool node's reconstructed tool_result
# Control-script success (#233): mirror each ``script:<name>`` span's ``status`` attribute into a
# ``script_success:<name>`` 0/1 score so "failure rate by script" is a one-widget Scores query.
# Observation-scoped, so a spoke that ran a script twice keeps both and the view's average IS the
# per-script success rate.
_SCRIPT_SUCCESS_SCORE = "script_success"
_STATUS_SUCCESS = "success"
# Agent verdict (#233): the outcome of an agent that ran under the spoke, as a numeric score named
# by the agent type. code-review reads the APPROVE/REJECT verdict from its signed ``.review``
# artifact; a schema-returning sub-agent reads the ``status`` its structured return carried; a
# reaper-killed sub-agent (an ``ERROR``-level container) scores the ``died`` sentinel. The three
# values are ordered so a Scores-view average reads as an approval/health rate.
_AGENT_VERDICT_SCORE = "agent_verdict"
_VERDICT_APPROVE = 1.0  # APPROVE / a success-class sub-agent status
_VERDICT_REJECT = 0.0  # REQUEST_CHANGES / a non-success sub-agent status
_VERDICT_DIED = -1.0  # a reaper-killed (ERROR-level) sub-agent container — distinct from a reject
_REVIEW_AGENT_TYPE = "code-review"
_REVIEW_APPROVE_VERDICT = "APPROVE"
_LEVEL_ERROR = "ERROR"  # the #157 failure level stamped on a failed/killed node
# Sub-agent structured-return statuses that count as a success verdict (else a reject). A closed set
# so a free-text status never mints an unexpected value; extend as new schema statuses appear.
_AGENT_SUCCESS_STATUSES = frozenset(
    {"success", "completed", "approved", "pass", "passed", "ok", "done"}
)
# Per-phase step cost/token scores (#158): the phase is the score-name suffix (a metrics
# dimension), so "what does RED cost across all spokes" is a one-widget Scores query.
_STEP_CACHE_WRITE_SCORE = (
    "step_cache_write_usd"  # per View B step observation, from rollup.written x cache-write price
)
_STEP_TOKENS_WRITTEN_SCORE = (
    "step_tokens_written"  # per View B step observation, from rollup.written
)
# True per-step cost (#230): sum EVERY generation's full Langfuse cost (main-loop +
# sub-agent:llm) into the cycle-step that contains it, so the per-phase scores reconcile to
# the trace totalCost rather than only the cache-write slice above.
_STEP_TOTAL_COST_SCORE = "step_total_cost_usd"  # per View B step observation, from costDetails
# Per-phase step latency (#230): the cycle-step window length as a numeric score, so step
# duration is a Scores-view sum/percentile dimension and not only a span the UI renders.
_STEP_DURATION_SCORE = "step_duration_ms"  # per View B step observation, from its window length
# The canonical solo-cycle phases parsed out of a step subject (e.g. "A-RED: …" → RED). Kept a
# closed set so a step subject can never mint a free-text score name (a metrics-cardinality guard).
_STEP_PHASES = ("ANCHOR", "RED", "GREEN", "REVIEW", "PUSH")
_STEP_PHASE_OTHER = "other"
_STEP_PHASE_RE = re.compile(rf"\b({'|'.join(_STEP_PHASES)})\b")

# Per-rule / per-tooldef carry-cost scores (#232): what a rule or tool schema costs EVERY request
# just by being loaded, whether it is ever invoked. A loaded prefix is cache-WRITTEN once, then
# cache-READ on every request. Anthropic prices a 5-min cache write at 1.25x base input and a cache
# read at 0.1x, so a read is 0.1/1.25 = 0.08x the cache-write price the rest of this module carries.
_CACHE_READ_RATIO = 0.08
_RULE_CARRY_COST_SCORE = "rule_carry_cost_usd"  # per rule file in the loaded-context breakdown
_RULE_INVOCATION_SCORE = "rule_invocations"  # per glob-scoped rule injected on a file-match (#232)
# Enforcement fires are scored per (event:tool) SURFACE, not per rule: per-script hook identity is
# blocked upstream (#110 AC3) — Claude Code emits one ``hook_execution_complete`` per (event x tool)
# with ``hook_name = "<event>:<tool>"`` (e.g. ``PreToolUse:Edit``), and every surface is guarded by
# many hooks spanning different rules + workflow mechanics (per ``.claude/settings.json``), so a
# block cannot be attributed to one rule. The surface itself is the honest granularity the telemetry
# supports.
_ENFORCEMENT_FIRE_SCORE = (
    "enforcement_fires"  # per (event:tool) surface where a hook blocked (#232)
)
_TOOLDEF_CARRY_COST_SCORE = "tooldef_carry_cost_usd"  # per tool / mcp schema
# The loaded instruction files that carry cost every request. The request-body path itemizes the
# auto-memory (MEMORY.md) under ``rules``; the disk fallback splits it into its own ``memory``
# category (_DISK_CATEGORY_ORDER), so both are read to keep the two paths consistent.
_RULE_CARRY_CATEGORIES = ("rules", "memory")
# Tool schemas are only itemized on the request-body loaded-context path; the disk fallback has no
# tools/mcp rows, so tooldef carry cost is naturally empty for a disk-sourced spoke.
_TOOLDEF_CARRY_CATEGORIES = ("tools", "mcp")
# Tool names (built-in + MCP + deferred) run to 100+, but a score NAME is a metrics dimension, so
# only the top-N most expensive tool defs get their own score; the cheaper tail folds into a single
# ``:other`` bucket (a visible, non-silent cap). Rule names are a small closed set and stay uncapped.
_TOOLDEF_SCORE_TOP_N = 15
_TOOLDEF_OTHER_KEY = "other"


def _score_id(spoke_run_id: str, name: str, target: str) -> str:
    """Return the deterministic id of one score for a spoke (idempotent across reruns)."""
    digest = hashlib.sha1(f"{spoke_run_id}:score:{name}:{target}".encode()).hexdigest()[:24]
    return _SCORE_PREFIX + digest


def _score_event(
    spoke_run_id: str,
    *,
    name: str,
    value: float,
    trace_id: str,
    base_ts: str,
    observation_id: str | None = None,
    id_target: str | None = None,
) -> IngestEvent:
    """Shape one numeric ``score-create`` ingestion event (trace- or observation-level).

    ``id_target`` disambiguates the deterministic id for a TRACE-level score that repeats under one
    name (e.g. one ``agent_verdict:code-review`` per ``.review`` artifact): the body stays
    trace-level but the id keys off ``id_target`` so the copies get distinct ids and both survive
    ingest instead of upserting onto one. Ignored when ``observation_id`` is given.
    """
    target = observation_id or id_target or "trace"
    score_id = _score_id(spoke_run_id, name, target)
    body: dict[str, Any] = {
        "id": score_id,
        "traceId": trace_id,
        "name": name,
        "value": value,
        "dataType": "NUMERIC",
    }
    if observation_id is not None:
        body["observationId"] = observation_id
    return {"id": score_id, "type": "score-create", "timestamp": base_ts, "body": body}


def build_score_events(
    spoke_run_id: str,
    traces: list[TraceObservations],
    batch: list[IngestEvent],
    *,
    base_ts: str,
) -> list[IngestEvent]:
    """Build the numeric Langfuse scores that make a spoke's metadata chartable (#100, #101).

    Langfuse dashboards can sum/aggregate numeric SCORES but not arbitrary observation metadata,
    so three signals already present as metadata are ALSO emitted as scores:

    - ``permission_wait_ms`` — an observation-level score on every ``tool:`` node carrying a
      folded ``blocked_on_user_ms`` (Part 2), so permission-prompt wait sums across spokes.
    - ``tool_result_size`` — an observation-level score on every ``tool:`` node carrying a
      reconstructed ``tool_result_size`` (#101 part 4), so "which tool outputs bloat context"
      is a one-click chart.
    - ``gate_park_ms`` — a trace-level score for the PLAN-gate park (:func:`_gate_park_ms`),
      emitted only when the spoke parked at a gate.

    All ids derive from the spoke run id so a rerun overwrites the same scores.

    Args:
        spoke_run_id: The spoke run identifier.
        traces: The source traces (for the gate-park gap).
        batch: The assembled events (read for the folded ``blocked_on_user_ms`` /
            ``tool_result_size`` tool metadata).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` ingestion events (empty when no signal is present).
    """
    trace_id = trace_id_for(spoke_run_id)
    events: list[IngestEvent] = []
    for event in batch:
        body = event["body"]
        metadata = body.get("metadata") or {}
        wait = metadata.get("blocked_on_user_ms")
        if wait is not None:
            events.append(
                _score_event(
                    spoke_run_id,
                    name=_PERMISSION_WAIT_SCORE,
                    value=int(wait),
                    trace_id=trace_id,
                    base_ts=base_ts,
                    observation_id=body["id"],
                )
            )
        size = metadata.get("tool_result_size")
        if size is not None:
            events.append(
                _score_event(
                    spoke_run_id,
                    name=_TOOL_RESULT_SIZE_SCORE,
                    value=int(size),
                    trace_id=trace_id,
                    base_ts=base_ts,
                    observation_id=body["id"],
                )
            )
    park = _gate_park_ms(traces)
    if park is not None:
        events.append(
            _score_event(
                spoke_run_id,
                name=_GATE_PARK_SCORE,
                value=park,
                trace_id=trace_id,
                base_ts=base_ts,
            )
        )
    return events


def _is_script_node(body: dict[str, Any]) -> bool:
    """Whether an assembled node is a control-script run node (#233).

    Matched the way :func:`~telemetry.spoke_tree.rollups._duration_class` classifies scripts: the
    ``script:<phase>`` name label OR, robustly, the ``workflow.kind == "script"`` span attribute
    (a phase-less script span keeps its raw name, so the attribute is the reliable signal).
    """
    name = body.get("name") or ""
    return name.startswith("script:") or _attr(body, "workflow.kind") == "script"


def _script_name(body: dict[str, Any]) -> str:
    """Return a script node's identity for the score-name suffix (#233).

    A phased script span is named ``<kind>:<phase>`` (e.g. ``script:gate``), so the ``script:``
    prefix is stripped to the phase; a phase-less span's whole name IS the script's identity
    (``spoke-push``, ``worktree-new``). Never free text — it is a control-script constant the emit
    layer passed as ``--name`` / ``--phase``.
    """
    name = body.get("name") or ""
    prefix = "script:"
    return name[len(prefix) :] if name.startswith(prefix) else name


def build_script_success_scores(
    spoke_run_id: str, batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-script ``script_success:<name>`` 0/1 scores from each script span's status (#233).

    A control-script span (``worktree-new``, ``spoke-push``, ``spoke-ready``, ``script:gate``, …)
    carries a ``status`` attribute already (``success`` / ``failure`` / …), but Langfuse can chart
    numeric SCORES, not arbitrary span attributes. Each script node therefore ALSO emits an
    observation-scoped NUMERIC score named by the script whose value is ``1.0`` when the status is
    ``success`` and ``0.0`` otherwise — so "failure rate by script" is a one-widget Scores query and
    a script run twice keeps both scores (the view's average is the per-script success rate). Ids
    derive from the spoke run id + observation (idempotent reruns).

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (its ``script:`` / ``workflow.kind==script`` nodes read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events, one per script node (empty when no script ran).
    """
    trace_id = trace_id_for(spoke_run_id)
    events: list[IngestEvent] = []
    for event in batch:
        body = event["body"]
        if not _is_script_node(body):
            continue
        value = 1.0 if _attr(body, "status") == _STATUS_SUCCESS else 0.0
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_SCRIPT_SUCCESS_SCORE}:{_script_name(body)}",
                value=value,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=body["id"],
            )
        )
    return events


def _review_artifact_scores(
    spoke_run_id: str, review_dir: Path, trace_id: str, base_ts: str
) -> list[IngestEvent]:
    """Build ``agent_verdict:code-review`` scores from the signed ``.review/*.json`` artifacts (#233).

    Each artifact records one review's ``verdict`` (``APPROVE`` / ``REQUEST_CHANGES``); the score is
    ``1.0`` for APPROVE else ``0.0``, so a Scores-view average over a spoke's reviews reads as its
    approve rate. Trace-level (the artifact is not a span), but the id keys off the artifact stem
    (the diff hash) via ``id_target`` so multiple reviews on one spoke keep distinct ids. A malformed
    or verdict-less artifact is skipped rather than scored 0 — an unreadable file is not a rejection.
    """
    if not review_dir.is_dir():
        return []
    events: list[IngestEvent] = []
    for artifact in sorted(review_dir.glob("*.json")):
        try:
            verdict = json.loads(artifact.read_text()).get("verdict")
        except (OSError, ValueError):
            continue
        if not verdict:
            continue
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_AGENT_VERDICT_SCORE}:{_REVIEW_AGENT_TYPE}",
                value=_VERDICT_APPROVE if verdict == _REVIEW_APPROVE_VERDICT else _VERDICT_REJECT,
                trace_id=trace_id,
                base_ts=base_ts,
                id_target=f"{_REVIEW_AGENT_TYPE}:{artifact.stem}",
            )
        )
    return events


def _sub_agent_verdict(body: dict[str, Any]) -> float | None:
    """Return a sub-agent container's verdict value, or None when it carries no verdict signal (#233).

    A reaper-killed container is stamped the #157 ``ERROR`` level, so it scores ``died``. Otherwise a
    schema-returning agent whose grafted ``output`` is a mapping with a ``status`` key scores by that
    status (:data:`_AGENT_SUCCESS_STATUSES`). A plain-text / status-less output carries no verdict
    (a free-form agent's prose is not an outcome), so it is skipped.
    """
    if body.get("level") == _LEVEL_ERROR:
        return _VERDICT_DIED
    output = body.get("output")
    if isinstance(output, dict) and "status" in output:
        status = str(output["status"]).lower()
        return _VERDICT_APPROVE if status in _AGENT_SUCCESS_STATUSES else _VERDICT_REJECT
    return None


def build_agent_verdict_scores(
    spoke_run_id: str, batch: list[IngestEvent], review_dir: Path, *, base_ts: str
) -> list[IngestEvent]:
    """Build per-agent ``agent_verdict:<type>`` scores from reviews and sub-agent outcomes (#233).

    Two sources feed one score family named by the agent type:

    - **code-review** — the signed ``.review/*.json`` artifacts under ``review_dir``, each an APPROVE
      / REQUEST_CHANGES verdict (:func:`_review_artifact_scores`). Trace-level, one per artifact.
    - **schema / killed sub-agents** — every ``sub-agent:<type>`` container in the assembled batch
      (excluding the ``sub-agent:llm`` calls, which are the sub-agent's own LLM turns, not an agent)
      scores its :func:`_sub_agent_verdict`: ``died`` for an ERROR-level (reaper-killed) container, a
      success/reject verdict from a structured ``status`` return, or nothing when it carried no
      verdict signal. Observation-scoped to the container.

    All ids derive from the spoke run id so a rerun overwrites the same scores.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (its ``sub-agent:`` containers are read).
        review_dir: The worktree's ``.review`` directory (its ``*.json`` artifacts are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events (empty when no review ran and no sub-agent carried a verdict).
    """
    trace_id = trace_id_for(spoke_run_id)
    events = _review_artifact_scores(spoke_run_id, review_dir, trace_id, base_ts)
    for event in batch:
        body = event["body"]
        name = body.get("name") or ""
        if not name.startswith(_SUB_AGENT_PREFIX):
            continue
        agent_type = name[len(_SUB_AGENT_PREFIX) :]
        if agent_type == "llm":
            continue
        value = _sub_agent_verdict(body)
        if value is None:
            continue
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_AGENT_VERDICT_SCORE}:{agent_type}",
                value=value,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=body["id"],
            )
        )
    return events


def main_loop_request_count(traces: list[TraceObservations]) -> int:
    """Count the MAIN-LOOP llm_requests over a spoke — the read multiplier for carry cost (#232).

    A rule / tool schema is loaded into the MAIN loop's cached prefix only; a ``sub-agent:llm``
    call runs its own prefix (different system prompt + tools) and does NOT carry the spoke's rules,
    so it must be excluded or every carry-cost score inflates by the sub-agent request count. The
    sub-agent generations are the ones whose name carries the :data:`_SUB_AGENT_PREFIX`.
    """
    return sum(
        1
        for _orig_trace_id, observation in _llm_requests_in_order(traces)
        if not (observation.get("name") or "").startswith(_SUB_AGENT_PREFIX)
    )


def _carry_cost_usd(tokens: int, n_requests: int, price: float) -> float:
    """Return the USD a loaded item of ``tokens`` costs across ``n_requests`` (#232).

    The item sits in the cached prefix: cache-WRITTEN once to seed it (``tokens x price``, the
    cache-creation price the module carries) and cache-READ on each request (``tokens x n_requests
    x`` the cheaper read price — a warm-started spoke reads the prefix on its first request too, so
    the read term is charged for the full count rather than n-1). Carry cost is dominated by the
    read term; the one-time write share is added once.
    """
    return tokens * (n_requests * price * _CACHE_READ_RATIO + price)


def _tokens_by_name(rows: list[dict[str, Any]], categories: tuple[str, ...]) -> dict[str, int]:
    """Sum loaded-context row tokens by ``name`` for the given categories (duplicate names summed).

    Mirrors the loaded-context breakdown, which sums duplicate ``(category, name)`` rows (e.g. a
    nested ``CLAUDE.md``) so each name is weighed once.
    """
    tokens: dict[str, int] = {}
    for row in rows:
        if row.get("category") not in categories:
            continue
        name = str(row.get("name"))
        tokens[name] = tokens.get(name, 0) + int(row.get("tokens") or 0)
    return tokens


def build_rule_carry_cost_scores(
    spoke_run_id: str,
    rows: list[dict[str, Any]],
    n_requests: int,
    *,
    base_ts: str,
    price: float,
) -> list[IngestEvent]:
    """Build per-rule ``rule_carry_cost_usd:<rule>`` scores from the loaded-context rows (#232).

    Every loaded instruction file the breakdown itemizes (``operational-gotchas.md``, ``MEMORY.md``,
    a glob-scoped rule that got injected, …) costs tokens on EVERY request whether or not it is ever
    invoked. Each one's carry cost (:func:`_carry_cost_usd`) is emitted as a trace-level NUMERIC
    score named by the file; the companion ``rule_invocations:<rule>`` score (emitted by the #232
    invocation pass, same suffix) lets a dashboard rank rules by carry cost filtered to zero
    invocations. Both on-disk instruction categories are read (:data:`_RULE_CARRY_CATEGORIES`) so
    the auto-memory (``rules`` on the request-body path, ``memory`` on the disk fallback) is not
    dropped on either source. Rule names are a small closed set, so — unlike tool defs — they are
    uncapped. Ids derive from the spoke run id (idempotent reruns).

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        rows: The loaded-context measured rows (``category`` / ``name`` / ``tokens`` read).
        n_requests: The count of main-loop LLM requests over the spoke (the read multiplier).
        base_ts: ISO timestamp stamped on every score event.
        price: Cache-creation (write) price in USD per token.

    Returns:
        The ``score-create`` events, one per rule file (empty when no rule rows are present).
    """
    trace_id = trace_id_for(spoke_run_id)
    return [
        _score_event(
            spoke_run_id,
            name=f"{_RULE_CARRY_COST_SCORE}:{rule}",
            value=_carry_cost_usd(tokens, n_requests, price),
            trace_id=trace_id,
            base_ts=base_ts,
        )
        for rule, tokens in sorted(_tokens_by_name(rows, _RULE_CARRY_CATEGORIES).items())
    ]


def build_tooldef_carry_cost_scores(
    spoke_run_id: str,
    rows: list[dict[str, Any]],
    n_requests: int,
    *,
    base_ts: str,
    price: float,
) -> list[IngestEvent]:
    """Build per-tool ``tooldef_carry_cost_usd:<tool>`` scores from the loaded-context rows (#232).

    A tool schema is loaded-context too — the ``Workflow`` def alone is ~5.3k tokens/request — so it
    carries the same cost model as a rule (:func:`_carry_cost_usd`). Tool names (built-in + MCP +
    deferred) run to 100+, so only the :data:`_TOOLDEF_SCORE_TOP_N` most expensive get their own
    score; the cheaper tail folds into one ``tooldef_carry_cost_usd:other`` bucket (a visible cap,
    not a silent drop). Ties break by name so the cut is byte-stable across reruns. Only the
    request-body loaded-context path itemizes tools, so a disk-sourced spoke (no captured request
    body) yields no tooldef scores at all. Trace-level NUMERIC scores; ids derive from the spoke
    run id.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        rows: The loaded-context measured rows (``tools`` and ``mcp`` categories read).
        n_requests: The count of main-loop LLM requests over the spoke (the read multiplier).
        base_ts: ISO timestamp stamped on every score event.
        price: Cache-creation (write) price in USD per token.

    Returns:
        The ``score-create`` events: the top-N tools plus one ``:other`` fold when any were cut
        (empty when no tool rows are present).
    """
    trace_id = trace_id_for(spoke_run_id)
    # A real tool literally named "other" is assumed not to exist (Claude Code tool names are
    # PascalCase / mcp__server__tool); if one ever did and landed in the top-N it would collide with
    # the fold bucket's score name — revisit the bucket key then.
    by_tokens = _tokens_by_name(rows, _TOOLDEF_CARRY_CATEGORIES)
    ranked = sorted(by_tokens.items(), key=lambda item: (-item[1], item[0]))
    events: list[IngestEvent] = [
        _score_event(
            spoke_run_id,
            name=f"{_TOOLDEF_CARRY_COST_SCORE}:{tool}",
            value=_carry_cost_usd(tokens, n_requests, price),
            trace_id=trace_id,
            base_ts=base_ts,
        )
        for tool, tokens in ranked[:_TOOLDEF_SCORE_TOP_N]
    ]
    folded_tokens = sum(tokens for _tool, tokens in ranked[_TOOLDEF_SCORE_TOP_N:])
    if folded_tokens:
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_TOOLDEF_CARRY_COST_SCORE}:{_TOOLDEF_OTHER_KEY}",
                value=_carry_cost_usd(folded_tokens, n_requests, price),
                trace_id=trace_id,
                base_ts=base_ts,
            )
        )
    return events


def build_rule_invocation_scores(
    spoke_run_id: str, batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-rule ``rule_invocations:<rule>`` scores from the context-delta labels (#232).

    The #160 context-delta pass stamps each llm_request copy's ``metadata.context_delta.added`` with
    the rows that entered context that turn, and :func:`~telemetry.spoke_tree.context_deltas`
    ._label_rule_injections tags each added row that injected glob-scoped rule(s) with a ``rules``
    list (one reminder can inject several). This scans the assembled batch for those labels and emits
    one trace-level NUMERIC score per rule whose value is the number of turns the rule entered play.
    Paired with ``rule_carry_cost_usd:<rule>`` (same ``<rule>`` suffix), a dashboard ranks rules by
    carry cost filtered to zero invocations — the dead-weight signal. Ids derive from the spoke run
    id (idempotent reruns).

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (their ``context_delta.added`` rows are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events, one per invoked rule (empty when no rule was injected).
    """
    trace_id = trace_id_for(spoke_run_id)
    counts: dict[str, int] = {}
    for event in batch:
        delta = (event["body"].get("metadata") or {}).get("context_delta") or {}
        for row in delta.get("added") or []:
            for rule in row.get("rules") or []:
                counts[str(rule)] = counts.get(str(rule), 0) + 1
    return [
        _score_event(
            spoke_run_id,
            name=f"{_RULE_INVOCATION_SCORE}:{rule}",
            value=count,
            trace_id=trace_id,
            base_ts=base_ts,
        )
        for rule, count in sorted(counts.items())
    ]


def _blocked_at_surface(body: dict[str, Any]) -> bool:
    """Whether a hook_execution_complete event blocked a tool call (``num_blocking >= 1``).

    ``num_blocking`` may arrive as a native int/float OR a numeric string — OTel span attributes
    are frequently flattened to strings during ingestion — so both are accepted (``float`` coerces
    all three). A ``bool`` is guarded out first: it is never a real count and ``float(True)`` would
    otherwise read as 1.0.

    UPGRADE: ``folding._level_for`` reads the same ``num_blocking`` with an ``isinstance(int, float)``
    check, so it would miss a stringified value where this counts one — align it if a stringified
    emission ever surfaces, so a scored fire always renders a WARNING node too.
    """
    num = _attr(body, "num_blocking")
    if isinstance(num, bool):
        return False
    try:
        return float(num) >= 1  # type: ignore[arg-type]  # int / float / numeric string
    except (TypeError, ValueError):
        return False


def build_enforcement_fire_scores(
    spoke_run_id: str, batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-surface ``enforcement_fires:<event>:<tool>`` scores from hook blocks (#232).

    An enforcement fire is a ``hook_execution_complete`` audit event with ``num_blocking >= 1`` (a
    hook denied a tool call). Per-script hook identity is blocked upstream (#110 AC3) — the event
    carries only ``hook_name = "<event>:<tool>"`` — and every tool surface is guarded by many hooks
    across different rules + workflow mechanics (per ``.claude/settings.json``), so a block cannot be
    attributed to one rule. The fire is therefore counted at the (event:tool) SURFACE, the honest
    granularity the telemetry supports; each blocked tool call counts once (not once per hook, so
    two hooks blocking the same call is one fire). Trace-level NUMERIC scores; ids derive from the
    spoke run id (idempotent reruns).

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (its ``hook_execution_complete`` nodes are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events, one per surface that blocked (empty when nothing blocked).
    """
    trace_id = trace_id_for(spoke_run_id)
    counts: dict[str, int] = {}
    for event in batch:
        body = event["body"]
        if not _is_hook_event(body) or not _blocked_at_surface(body):
            continue
        surface = str(_attr(body, "hook_name") or "").strip()
        if surface:
            counts[surface] = counts.get(surface, 0) + 1
    return [
        _score_event(
            spoke_run_id,
            name=f"{_ENFORCEMENT_FIRE_SCORE}:{surface}",
            value=count,
            trace_id=trace_id,
            base_ts=base_ts,
        )
        for surface, count in sorted(counts.items())
    ]


def _step_phase(subject: str) -> str:
    """Return the canonical solo-cycle phase named in a step subject, or ``other`` (#158).

    ``"A-RED: red first"`` → ``RED``; ``"ANCHOR #154 …"`` → ``ANCHOR``; a compound subject like
    ``"REVIEW + PUSH"`` takes the leftmost keyword (``REVIEW``). The result is always one of the
    closed :data:`_STEP_PHASES` set or ``other`` — never free text — so it is a safe score-name
    suffix.
    """
    match = _STEP_PHASE_RE.search(subject.upper())
    return match.group(1) if match else _STEP_PHASE_OTHER


def _step_phase_of(body: dict[str, Any]) -> str:
    """Return the phase of one View B step node: ``pre`` / ``post`` for the boundary partitions,
    else the phase parsed from its subject."""
    name = body.get("name") or ""
    if name == _PRE_STEP_NAME:
        return _PRE_STEP_KEY
    if name == _POST_STEP_NAME:
        return _POST_STEP_KEY
    subject = (body.get("metadata") or {}).get("subject") or name
    return _step_phase(subject)


def build_step_cost_scores(
    spoke_run_id: str, cycle_batch: list[IngestEvent], *, base_ts: str, price: float
) -> list[IngestEvent]:
    """Build per-phase step cost/token scores from View B's step rollups (#158).

    ``step:*`` nodes carry token rollups only in ``metadata.rollup`` (never ``usageDetails`` — the
    #114 double-count guard), so per-step cost is invisible to the Metrics API. Score NAMES are a
    metrics dimension, so each View B step emits ``step_cache_write_usd:<PHASE>`` and
    ``step_tokens_written:<PHASE>`` from its rollup's ``written`` tokens (cost = written x the
    cache-creation ``price``), observation-scoped to the step node with a deterministic id. This
    score is cache-WRITE cost only (its #230 rename made that explicit); the true
    all-generations per-step total is a separate score.

    Emitted on View B (the cycle lens) ONLY: a step lives on both views, but scoring both would
    double every phase in a Scores-view sum, so — like the other per-call enrichments — this is
    single-emit. A step with no rollup (a childless boundary partition) is skipped.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        cycle_batch: The assembled View B events (its step nodes' rollups are read).
        base_ts: ISO timestamp stamped on every score event.
        price: Cache-creation price in USD per written token.

    Returns:
        The ``score-create`` events (empty when View B has no step rollups).
    """
    trace_id = cycle_trace_id_for(spoke_run_id)
    events: list[IngestEvent] = []
    for event in cycle_batch:
        body = event["body"]
        if not body["id"].startswith(_CYCLE_STEP_PREFIX):
            continue
        written = ((body.get("metadata") or {}).get("rollup") or {}).get("written")
        if written is None:
            continue
        phase = _step_phase_of(body)
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_STEP_CACHE_WRITE_SCORE}:{phase}",
                value=written * price,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=body["id"],
            )
        )
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_STEP_TOKENS_WRITTEN_SCORE}:{phase}",
                value=written,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=body["id"],
            )
        )
    return events


def _generation_total_cost(body: dict[str, Any]) -> float:
    """Return one generation's full USD cost from its Langfuse ``costDetails`` (#230).

    Langfuse computes each generation's cost from token usage x its model price table and returns
    it as ``costDetails``. A reserved ``total`` key is the aggregate and wins when present;
    otherwise the per-usage-type components are summed. The sum over every generation therefore
    reconciles to the trace ``totalCost``. A generation with no ``costDetails`` contributes 0.
    """
    cost_details = body.get("costDetails") or {}
    total = cost_details.get("total")
    if isinstance(total, (int, float)):
        return float(total)
    return float(sum(v for v in cost_details.values() if isinstance(v, (int, float))))


def _nearest_step_ancestor(
    node_id: str, parent_of: dict[str, str | None], step_ids: set[str]
) -> str | None:
    """Walk ``parentObservationId`` up from ``node_id`` to the nearest cycle-step ancestor, or None.

    A generation may sit directly under a cycle-step node or under a ``sub-agent:<type>`` container
    that itself lands on a step, so the cost lands in the step whose window contains the work. The
    cycle-visited set guards against a malformed parent cycle.
    """
    seen: set[str] = set()
    current = parent_of.get(node_id)
    while current is not None and current not in seen:
        if current in step_ids:
            return current
        seen.add(current)
        current = parent_of.get(current)
    return None


def build_step_total_cost_scores(
    spoke_run_id: str, cycle_batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-phase ``step_total_cost_usd:<PHASE>`` scores windowing all generations (#230).

    Unlike :func:`build_step_cost_scores` (cache-WRITE cost only), this sums each generation's full
    Langfuse ``costDetails`` — main-loop ``claude_code.llm_request`` AND ``sub-agent:llm`` — into
    the cycle-step that contains it. Attribution keys off the authoritative View B spine: every
    generation copy is walked up ``parentObservationId`` to its nearest cycle-step ancestor
    (:func:`_nearest_step_ancestor`), so a sub-agent's calls land in the step its container sits
    under. The per-phase scores therefore reconcile to the trace ``totalCost``, with pre-first-step
    spend surfacing as a ``step_total_cost_usd:pre`` residual.

    Emitted on View B (the cycle lens) ONLY, observation-scoped to each step node with a
    deterministic id, mirroring :func:`build_step_cost_scores`. A step with no generation windowed
    into it emits nothing.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        cycle_batch: The assembled View B events (its generation copies' ``costDetails`` are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events (empty when View B has no generations under a step).
    """
    trace_id = cycle_trace_id_for(spoke_run_id)
    by_id = {event["body"]["id"]: event["body"] for event in cycle_batch}
    parent_of = {node_id: body.get("parentObservationId") for node_id, body in by_id.items()}
    step_ids = {node_id for node_id in by_id if node_id.startswith(_CYCLE_STEP_PREFIX)}
    cost_by_step: dict[str, float] = {}
    for event in cycle_batch:
        if event["type"] != "generation-create":
            continue
        step_id = _nearest_step_ancestor(event["body"]["id"], parent_of, step_ids)
        if step_id is None:
            continue
        cost_by_step[step_id] = cost_by_step.get(step_id, 0.0) + _generation_total_cost(
            event["body"]
        )
    events: list[IngestEvent] = []
    for step_id, cost in cost_by_step.items():
        phase = _step_phase_of(by_id[step_id])
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_STEP_TOTAL_COST_SCORE}:{phase}",
                value=cost,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=step_id,
            )
        )
    return events


def _step_duration_ms(body: dict[str, Any]) -> int | None:
    """Return a cycle-step node's window length in ms from its start/end, or None when unparseable."""
    start = _parse_utc(body.get("startTime"))
    end = _parse_utc(body.get("endTime"))
    if start is None or end is None or end < start:
        return None
    return int((end - start).total_seconds() * 1000)


def build_step_duration_scores(
    spoke_run_id: str, cycle_batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-phase ``step_duration_ms:<PHASE>`` scores from each cycle-step window (#230).

    A cycle-step node's ``[startTime, endTime]`` is its phase window, but latency is only a span the
    Langfuse UI renders, not a numeric a Scores widget can sum or percentile across spokes. Each
    View B step node (``preStep`` / ``step:*`` / ``postStep``) therefore also emits its window
    length in milliseconds as ``step_duration_ms:<phase>``, observation-scoped with a deterministic
    id — mirroring the cost scores. A step whose bounds are missing/inverted is skipped.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        cycle_batch: The assembled View B events (its step nodes' start/end are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events (empty when View B has no timestamped step nodes).
    """
    trace_id = cycle_trace_id_for(spoke_run_id)
    events: list[IngestEvent] = []
    for event in cycle_batch:
        body = event["body"]
        if not body["id"].startswith(_CYCLE_STEP_PREFIX):
            continue
        duration = _step_duration_ms(body)
        if duration is None:
            continue
        phase = _step_phase_of(body)
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_STEP_DURATION_SCORE}:{phase}",
                value=duration,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=body["id"],
            )
        )
    return events
