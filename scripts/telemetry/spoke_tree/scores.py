"""Numeric Langfuse scores that make a spoke's metadata chartable (#100, #101, #158).

Langfuse dashboards can sum/aggregate numeric SCORES but not arbitrary observation metadata, so
signals already present as metadata are ALSO emitted as scores: :func:`build_score_events` emits
per-tool ``permission_wait_ms`` / ``tool_result_size`` and the trace-level ``gate_park_ms`` (from
:func:`~telemetry.spoke_tree.commits._gate_park_ms`); :func:`build_step_cost_scores` emits per-phase
``step_cache_write_usd`` / ``step_tokens_written`` from View B's step rollups; and
:func:`build_step_total_cost_scores` emits per-phase ``step_total_cost_usd`` — the true all-
generations cost windowed onto each step (#230). :func:`build_outcome_count_scores` emits the
trace-level failure-economics counts (``gate_park_count`` / ``blocked_count`` /
``relaunch_count``) and :func:`build_normalization_scores` the size/effort normalizers
(``files_changed`` / ``lines_changed`` / ``commits`` / ``subtasks`` + the derived
``cost_per_changed_line`` / ``wall_per_subtask``) so a spoke's cost is comparable across
spokes/repos (#231). Depends on the foundation, ``ids``, ``steps``, and ``commits``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telemetry.spoke_tree.commits import _first_commit_at, _gate_park_bounds, _gate_park_ms
from telemetry.spoke_tree.cycle import _POST_STEP_KEY, _PRE_STEP_KEY
from telemetry.spoke_tree.ids import (
    _CYCLE_STEP_PREFIX,
    cycle_trace_id_for,
    root_id_for,
    trace_id_for,
)
from telemetry.spoke_tree.observations import (
    _MCP_GROUP_PREFIX,
    _POST_STEP_NAME,
    _PRE_STEP_NAME,
    _SKILL_SPAN_PREFIX,
    _SUB_AGENT_PREFIX,
    IngestEvent,
    Lifecycle,
    Observation,
    TraceObservations,
    _attr,
    _duration_ms,
    _is_gate_observation,
    _is_hook_event,
    _is_mcp_group,
    _is_script_node,
    _is_skill_span,
    _iso_to_epoch,
    _llm_requests_in_order,
    _mcp_server,
    _parse_utc,
)

# Deterministic id prefix for the numeric Langfuse scores (#100 amendment: chartable time budget).
_SCORE_PREFIX = "tree-score-"
# Failure-economics counts (#231): a blocked/reaped disaster spoke and a clean landing carried
# identical tags, so these trace-level counts make the difference queryable. ``gate_park_count`` is
# DERIVED from the traces (the PLAN-gate park spans); ``blocked_count`` / ``relaunch_count`` are
# supervisor state, read from ``.ai-toolkit`` integer pointers (0 emitted so "clean" reads distinct
# from "not measured").
_GATE_PARK_COUNT_SCORE = "gate_park_count"
_BLOCKED_COUNT_SCORE = "blocked_count"
_RELAUNCH_COUNT_SCORE = "relaunch_count"
_BLOCKED_COUNT_POINTER = ".ai-toolkit/blocked-count"
_RELAUNCH_COUNT_POINTER = ".ai-toolkit/relaunch-count"
# Normalization scores (#231): the size + effort a spoke's cost/latency should be read against,
# so a cheap one-line fix and an expensive refactor are comparable across spokes/repos. The four
# base counts come from the commit numstat + the cycle windows the builder already has; the two
# derived ratios normalize the trace's cost/wall by size. A ratio is skipped (not 0) when its
# denominator is 0 so an empty spoke never divides by zero.
_FILES_CHANGED_SCORE = "files_changed"
_LINES_CHANGED_SCORE = "lines_changed"
_COMMITS_SCORE = "commits"
_SUBTASKS_SCORE = "subtasks"
_COST_PER_CHANGED_LINE_SCORE = "cost_per_changed_line"
_WALL_PER_SUBTASK_SCORE = "wall_per_subtask"
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
# Skill success (#234): mirror a skill span's SCRIPTED exit-status (stamped by the SKILL.md/hook
# contract, never LLM-self-reported) into a ``skill_success:<name>`` 0/1 score. Ready-but-latent
# like ``script_success`` above: emitted ONLY when a skill carries a scripted status attribute, so a
# statusless skill is not scored 0 (absence is not a failure) and the widget stays empty until the
# contract stamps a status. Read from the skill span attributes, in priority order.
_SKILL_SUCCESS_SCORE = "skill_success"
_SKILL_STATUS_KEYS = ("skill.status", "skill_exit_status")
# Per-skill cost (#322): a ``skill:<name>`` span is a relabeled ``tool:Skill`` span whose OWN
# ``costDetails`` is $0 — the real LLM spend lives in its generation DESCENDANTS. Langfuse's
# metrics/dashboard API sums each observation's own cost, so a ``skill:`` filter returns $0; mirror
# the rule/step carry-cost builders and emit the summed descendant-generation cost as a numeric
# ``skill_cost_usd:<name>`` score. A skill with no generation descendants is SKIPPED (never scored 0)
# — absence of spend is not a cost (AFK Design Principle 1; the ready-but-latent skill_success idiom).
_SKILL_COST_SCORE = "skill_cost_usd"
# Per-sub-agent cost (#323): the exact analog for a ``sub-agent:<type>`` container — the otelcol-
# renamed ``tool:Agent`` span whose OWN ``costDetails`` is $0 while the real LLM spend lives in its
# ``sub-agent:llm`` generation DESCENDANTS. Langfuse's own-cost-summing metrics API returns $0 for a
# ``sub-agent:`` filter, so the summed descendant-generation cost is emitted as a numeric
# ``agent_cost_usd:<type>`` score, reusing the #322 subtree-rollup helper (:func:`_cost_subtree_ancestors`).
# A container with no generation descendants is SKIPPED (never scored 0). ``agent_cost_usd:<type>`` is
# a SUBSET of the ``step_total_cost_usd`` of the step the agent sits in (that step already folds
# ``sub-agent:llm``), so the two must not be read as additive.
_AGENT_COST_SCORE = "agent_cost_usd"
_SUB_AGENT_LLM_TYPE = "llm"  # the sub-agent's own generation leaves, never a rollup boundary
# MCP call scores (#234): per ``mcp:<server>`` group, mirror its call count and success into
# chartable numeric scores — ``mcp_success:<server>`` is 1.0 when the server's group had zero
# error-shaped calls else 0.0 (so a Scores-view average reads as its success rate), and
# ``mcp_calls:<server>`` is how many calls that group folded (a volume dimension). Observation-scoped
# to the group so a spoke that hit one server under two turns keeps both.
_MCP_SUCCESS_SCORE = "mcp_success"
_MCP_CALLS_SCORE = "mcp_calls"
# MCP def scores (#234): the carrying-vs-using split for MCP schemas, mirroring rules (#232).
# ``mcp_carry_cost_usd:<server>`` is what a server's loaded tool schemas cost every request whether
# used or not; ``mcp_def_loads:<server>`` counts on-demand ToolSearch schema loads mid-session.
_MCP_CARRY_COST_SCORE = "mcp_carry_cost_usd"
_MCP_DEF_LOADS_SCORE = "mcp_def_loads"
_MCP_CARRY_CATEGORY = "mcp"
# Agent verdict (#233): the outcome of an agent that ran under the spoke, as a numeric score named
# by the agent type. code-review reads the APPROVE/REJECT verdict from its signed ``.review``
# artifact; a schema-returning sub-agent reads the ``status`` its structured return carried. The
# verdict score is strictly 0/1 so a Scores-view average reads directly as an approval/health rate.
# A reaper-killed (``ERROR``-level) sub-agent scores a SEPARATE ``agent_verdict:<type>:died`` flag
# (value 1.0, a count) — the died class is kept off the 0/1 rate name so a death never drags the
# averaged approval rate below zero (a distinct failure mode from a returned reject).
_AGENT_VERDICT_SCORE = "agent_verdict"
_VERDICT_APPROVE = 1.0  # APPROVE / a success-class sub-agent status
_VERDICT_REJECT = 0.0  # REQUEST_CHANGES / a non-success sub-agent status
_DIED_SCORE_SUFFIX = "died"  # the ``agent_verdict:<type>:died`` count score name suffix
_DIED_FLAG = 1.0  # one death — a count, not a point on the 0/1 verdict scale
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

# Per-stage overhead decomposition (#280): the wall-clock a spoke spends in each lifecycle overhead
# stage, as trace-level NUMERIC scores chartable exactly like gate_park_ms. Each is derived from
# already-on-disk / in-trace sources at land time (backfill), and skipped (never 0) when its source
# is absent so "not measured" reads distinctly from a real zero.
_STAGE_SPAWN_SEED_SCORE = "stage_spawn_seed_ms"  # dispatch epoch -> first-commit author time
_STAGE_GATE_ANSWER_SCORE = "stage_gate_answer_ms"  # PLAN-gate park onset -> auto-answer attempt
_STAGE_REVIEW_SCORE = "stage_review_ms"  # Σ phase:review agent + code-review span windows
_STAGE_PUSH_GATE_SCORE = "stage_push_gate_ms"  # Σ spoke-push span windows (the pre-push test gate)
_STAGE_LAND_SCORE = "stage_land_ms"  # the worktree-land span window (absent until the land closes)
# The review / control-script span identities the stage windows read off the source traces.
_REVIEW_PHASE = "review"
_CODE_REVIEW_AGENT_PREFIX = "sub-agent:code-review"
_PUSH_SCRIPT_NAME = "spoke-push"
_LAND_SCRIPT_NAME = "worktree-land"

# Per-drain-window rollups (#280): a snapshot of the whole drain window, read off the afk state dir
# at THIS spoke's land time and stamped as trace-level scores on its own trace (the per-spoke
# builder never sees sibling spokes, so a dashboard filtered to mode:afk reads the latest snapshot).
_ISSUES_PER_HOUR_SCORE = "issues_per_hour"  # spokes serviced ÷ window hours (throughput)
_OVERHEAD_WORK_RATIO_SCORE = "overhead_work_ratio"  # Σ overhead stages ÷ Σ work duration buckets
_AUTONOMY_SCORE = "autonomy_score"  # 1 - interventions ÷ spokes serviced (#251 autonomy)
_SECONDS_PER_HOUR = 3600
# The duration-rollup component classes that count as productive WORK (the denominator of the
# overhead-vs-work ratio): model calls, tool calls, the cycle-step spine, and turn containers. The
# wait / script / hook / self / other buckets are overhead and excluded.
_WORK_DURATION_CLASSES = ("llm_request", "tool", "step", "turn")

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


def _root_total_ms(spoke_run_id: str, batch: list[IngestEvent]) -> int:
    """Return the View A root's subtree wall-clock in ms from its duration rollup, or 0 (#231).

    ``_apply_container_rollups`` stamps ``metadata.rollup.duration.total_ms`` on the synthetic
    root; a batch built before that pass (or a malformed one) yields 0 rather than crashing.
    """
    root_id = root_id_for(spoke_run_id)
    for event in batch:
        if event["body"].get("id") != root_id:
            continue
        duration = ((event["body"].get("metadata") or {}).get("rollup") or {}).get("duration") or {}
        total = duration.get("total_ms")
        return int(total) if isinstance(total, (int, float)) else 0
    return 0


def build_normalization_scores(
    spoke_run_id: str,
    commits: list[dict[str, Any]],
    batch: list[IngestEvent],
    subtasks: int,
    *,
    base_ts: str,
    commits_dump_present: bool,
) -> list[IngestEvent]:
    """Build the trace-level normalization scores that size a spoke's cost + latency (#231).

    A spoke's raw cost/wall-clock is only comparable across spokes/repos once normalized by how
    much it changed and how many subtasks it ran. Four base counts come from data the builder
    already has — the commit numstat (:func:`_parse_commits`) and the cycle windows — plus two
    derived ratios:

    - ``files_changed`` — distinct paths touched across all commits (de-duplicated).
    - ``lines_changed`` — total additions + deletions summed over the per-commit numstat (the
      churn the issue scopes to — the data ``commits.py`` already parses — NOT the net
      ``merge-base..HEAD`` diff, so a spoke that rewrites the same lines across several commits
      reads higher here than its net diff).
    - ``commits`` — number of commits on the branch.
    - ``subtasks`` — number of cycle windows (the ledger subtask count), passed in.
    - ``cost_per_changed_line`` — the trace's total generation cost (Σ ``costDetails``, the same
      figure :func:`build_step_total_cost_scores` reconciles to ``totalCost``) ÷ ``lines_changed``.
    - ``wall_per_subtask`` — the root subtree wall-clock (ms) ÷ ``subtasks``.

    The four base counts are always emitted (0 included); a ratio is SKIPPED when its denominator
    is 0 so an empty spoke never divides by zero. All are trace-level NUMERIC scores; ids derive
    from the spoke run id so a rerun overwrites the same scores.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids + the root id).
        commits: The parsed ``git log --numstat`` records (``files`` / ``additions`` / ``deletions``).
        batch: The assembled View A events (its generations' ``costDetails`` + root duration read).
        subtasks: The cycle-window count (the ledger subtask count).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The four base-count ``score-create`` events plus each derived ratio whose denominator is
        non-zero.
    """
    trace_id = trace_id_for(spoke_run_id)
    files_changed = len({path for commit in commits for path in commit.get("files") or []})
    lines_changed = sum(
        int(commit.get("additions") or 0) + int(commit.get("deletions") or 0) for commit in commits
    )
    total_cost = sum(
        _generation_total_cost(event["body"])
        for event in batch
        if event.get("type") == "generation-create"
    )
    values: dict[str, float] = {
        _FILES_CHANGED_SCORE: files_changed,
        _LINES_CHANGED_SCORE: lines_changed,
        _COMMITS_SCORE: len(commits),
        _SUBTASKS_SCORE: subtasks,
    }
    if lines_changed:
        values[_COST_PER_CHANGED_LINE_SCORE] = total_cost / lines_changed
    if subtasks:
        values[_WALL_PER_SUBTASK_SCORE] = _root_total_ms(spoke_run_id, batch) / subtasks
    return [
        _score_event(spoke_run_id, name=name, value=value, trace_id=trace_id, base_ts=base_ts)
        for name, value in values.items()
    ]


def _read_count_pointer(root: Path, pointer: str) -> int:
    """Return a non-negative integer from a ``.ai-toolkit`` count pointer, or 0 (#231).

    A missing, unreadable, blank, or non-integer pointer resolves to 0 — supervisor state
    that was never written reads as "no blocks/relaunches", never crashes the land-time build.
    """
    try:
        value = (root / pointer).read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    return int(value) if value.isdigit() else 0


def build_outcome_count_scores(
    spoke_run_id: str, traces: list[TraceObservations], root: Path, *, base_ts: str
) -> list[IngestEvent]:
    """Build the trace-level failure-economics count scores (#231).

    A blocked/reaped disaster spoke and a clean landing carried identical trace tags, so these
    three numeric scores make the difference queryable:

    - ``gate_park_count`` — DERIVED from the traces: how many PLAN-gate park spans the spoke
      emitted (:func:`_is_gate_observation`), so no shell input is needed.
    - ``blocked_count`` / ``relaunch_count`` — supervisor state, read from the worktree's
      ``.ai-toolkit`` integer pointers (:func:`_read_count_pointer`), defaulting to 0 so a
      clean landing (0 blocks, 0 relaunches) reads distinctly from "not measured".

    All three are emitted unconditionally (0 included) and trace-level, so each is a one-widget
    Scores query; ids derive from the spoke run id so a rerun overwrites the same scores.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        traces: The source traces (scanned for the gate-park spans).
        root: The worktree root holding the ``.ai-toolkit`` count pointers.
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The three ``score-create`` events (gate_park_count, blocked_count, relaunch_count).
    """
    trace_id = trace_id_for(spoke_run_id)
    gate_parks = sum(
        1
        for _orig_trace_id, observations in traces
        for observation in observations
        if _is_gate_observation(observation)
    )
    values = {
        _GATE_PARK_COUNT_SCORE: gate_parks,
        _BLOCKED_COUNT_SCORE: _read_count_pointer(root, _BLOCKED_COUNT_POINTER),
        _RELAUNCH_COUNT_SCORE: _read_count_pointer(root, _RELAUNCH_COUNT_POINTER),
    }
    return [
        _score_event(spoke_run_id, name=name, value=value, trace_id=trace_id, base_ts=base_ts)
        for name, value in values.items()
    ]


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

    A control-script span (``worktree-new``, ``spoke-push``, ``spoke-ready``, …) carries a
    ``status`` attribute already (``success`` / ``failure`` / …), but Langfuse can chart numeric
    SCORES, not arbitrary span attributes. Each real script node therefore ALSO emits an
    observation-scoped NUMERIC score named by the script whose value is ``1.0`` when the status is
    ``success`` and ``0.0`` otherwise — so "failure rate by script" is a one-widget Scores query and
    a script run twice keeps both scores (the view's average is the per-script success rate). Ids
    derive from the spoke run id + observation (idempotent reruns).

    The PLAN-gate park span is a ``script:gate`` node too but is a human-WAIT node (``_duration_class``
    buckets it as ``wait``, not ``script``) whose status is always ``success`` — scoring it would add
    a bogus 100%-successful ``gate`` series — so it is excluded via :func:`_is_gate_observation`.

    UPGRADE: the ``0.0`` (failure) branch is currently latent — every emit site passes a literal
    ``success`` and a failing script exits before reaching its emit line (see the ``spoke-push.sh``
    "emit a status=failure span" UPGRADE), so the widget reads a constant 100% until the emit layer
    stamps failure spans. The scoring side is ready for it — no change needed here when it lands.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (its ``script:`` / ``workflow.kind==script`` nodes read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events, one per real (non-gate) script node (empty when none ran).
    """
    trace_id = trace_id_for(spoke_run_id)
    events: list[IngestEvent] = []
    for event in batch:
        body = event["body"]
        if not _is_script_node(body) or _is_gate_observation(body):
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


def _skill_span_name(body: dict[str, Any]) -> str:
    """Return a ``skill:<name>`` span's skill identity for the score-name suffix (#234)."""
    name = body.get("name") or ""
    return name[len(_SKILL_SPAN_PREFIX) :]


def build_skill_success_scores(
    spoke_run_id: str, batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-skill ``skill_success:<name>`` 0/1 scores from each skill span's status (#234).

    A first-class ``skill:<name>`` span (relabeled from ``tool:Skill``, :func:`_skill_relabel`)
    carries a cost rollup already, but success must be SCRIPTED, never LLM-self-reported: this reads
    a scripted exit-status attribute (:data:`_SKILL_STATUS_KEYS`) that the SKILL.md/hook contract
    stamps, mirroring :func:`build_script_success_scores`. The value is ``1.0`` when the status is
    ``success`` and ``0.0`` otherwise, observation-scoped to the skill node with a deterministic id
    (a skill run twice keeps both, so the view's average is the per-skill success rate).

    Ready-but-latent: a score is emitted ONLY when the skill carries a scripted status attribute — a
    statusless skill is skipped rather than scored 0 (absence of a scripted status is not a failure),
    so no skill self-reports and the widget stays empty until the contract (a separate cross-cutting
    surface) stamps a status.

    UPGRADE: a gate-shaped skill (``/afk``, ``/hub``) has no scripted exit-status but a mechanical
    gate outcome (subtask landed) — derive its success from the existing gate signal once that join
    is wired; today only the status-attribute path emits.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (its ``skill:`` nodes are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events, one per skill node carrying a scripted status (empty when none).
    """
    trace_id = trace_id_for(spoke_run_id)
    events: list[IngestEvent] = []
    for event in batch:
        body = event["body"]
        if not _is_skill_span(body):
            continue
        status = _attr(body, *_SKILL_STATUS_KEYS)
        if status is None:
            continue
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_SKILL_SUCCESS_SCORE}:{_skill_span_name(body)}",
                value=1.0 if status == _STATUS_SUCCESS else 0.0,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=body["id"],
            )
        )
    return events


def build_mcp_call_scores(
    spoke_run_id: str, batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-server ``mcp_success`` / ``mcp_calls`` scores from the MCP group nodes (#234).

    Each ``mcp:<server>`` group (:func:`~telemetry.spoke_tree.folding._apply_mcp_groups`) carries a
    ``metadata`` rollup of its ``server`` / ``calls`` / ``failures``, but Langfuse charts numeric
    SCORES not span metadata. Each group therefore emits ``mcp_success:<server>`` — ``1.0`` when it
    had zero error-shaped calls else ``0.0`` — and ``mcp_calls:<server>`` — its folded call count —
    observation-scoped to the group with deterministic ids (a server hit under two turns keeps both,
    so the view's average is the per-server success rate). A group with no ``calls`` metadata is
    skipped (defensive; a real group always carries it).

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (its ``mcp:`` group nodes are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events, two per MCP group (empty when the spoke made no MCP call).
    """
    trace_id = trace_id_for(spoke_run_id)
    events: list[IngestEvent] = []
    for event in batch:
        body = event["body"]
        if not _is_mcp_group(body):
            continue
        metadata = body.get("metadata") or {}
        calls = metadata.get("calls")
        if calls is None:
            continue
        server = metadata.get("server") or (body.get("name") or "")[len(_MCP_GROUP_PREFIX) :]
        success = 1.0 if not metadata.get("failures") else 0.0
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_MCP_SUCCESS_SCORE}:{server}",
                value=success,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=body["id"],
            )
        )
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_MCP_CALLS_SCORE}:{server}",
                value=int(calls),
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=body["id"],
            )
        )
    return events


def _tokens_by_server(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Sum the ``mcp``-category loaded-context row tokens by server (#234)."""
    servers: dict[str, int] = {}
    for row in rows:
        if row.get("category") != _MCP_CARRY_CATEGORY:
            continue
        server = _mcp_server(str(row.get("name") or ""))
        if server:
            servers[server] = servers.get(server, 0) + int(row.get("tokens") or 0)
    return servers


def build_mcp_carry_cost_scores(
    spoke_run_id: str,
    rows: list[dict[str, Any]],
    n_requests: int,
    *,
    base_ts: str,
    price: float,
) -> list[IngestEvent]:
    """Build per-server ``mcp_carry_cost_usd:<server>`` scores from the loaded-context mcp rows (#234).

    An MCP server's tool schemas sit in the cached prefix and cost tokens on EVERY request whether
    or not any of its tools are called — the same carry-cost model as a rule (:func:`_carry_cost_usd`),
    but rolled up per SERVER (the ``mcp`` breakdown keys per tool; a server exposes many). Paired with
    ``mcp_def_loads:<server>`` (same suffix), a dashboard ranks servers by carry cost filtered to zero
    on-demand loads. Only the request-body loaded-context path itemizes MCP, so a disk-sourced spoke
    yields none. Trace-level NUMERIC scores; ids derive from the spoke run id (idempotent reruns).

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        rows: The loaded-context measured rows (the ``mcp`` category is read).
        n_requests: The count of main-loop LLM requests over the spoke (the read multiplier).
        base_ts: ISO timestamp stamped on every score event.
        price: Cache-creation (write) price in USD per token.

    Returns:
        The ``score-create`` events, one per MCP server (empty when no MCP def was loaded).
    """
    trace_id = trace_id_for(spoke_run_id)
    return [
        _score_event(
            spoke_run_id,
            name=f"{_MCP_CARRY_COST_SCORE}:{server}",
            value=_carry_cost_usd(tokens, n_requests, price),
            trace_id=trace_id,
            base_ts=base_ts,
        )
        for server, tokens in sorted(_tokens_by_server(rows).items())
    ]


def build_mcp_def_load_scores(
    spoke_run_id: str, batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-server ``mcp_def_loads:<server>`` scores from the context-delta labels (#234).

    The #160 context-delta pass tags each added ``mcp``-category def row (a ToolSearch schema load)
    with ``mcp_def_load=<server>`` (:func:`~telemetry.spoke_tree.context_deltas._label_mcp_def_loads`).
    This scans the assembled batch for those labels and emits one trace-level NUMERIC score per
    server whose value is how many times that server's schemas were loaded on demand — the "using"
    half of the carrying-vs-using split, paired with ``mcp_carry_cost_usd:<server>``. Mirrors
    :func:`build_rule_invocation_scores`. Ids derive from the spoke run id (idempotent reruns).

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (their ``context_delta.added`` rows are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events, one per server loaded on demand (empty when none were).
    """
    trace_id = trace_id_for(spoke_run_id)
    counts: dict[str, int] = {}
    for event in batch:
        delta = (event["body"].get("metadata") or {}).get("context_delta") or {}
        for row in delta.get("added") or []:
            server = row.get("mcp_def_load")
            if server:
                counts[str(server)] = counts.get(str(server), 0) + 1
    return [
        _score_event(
            spoke_run_id,
            name=f"{_MCP_DEF_LOADS_SCORE}:{server}",
            value=count,
            trace_id=trace_id,
            base_ts=base_ts,
        )
        for server, count in sorted(counts.items())
    ]


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
            parsed = json.loads(artifact.read_text())
        except (OSError, ValueError):
            continue
        # A valid-but-non-object artifact (a JSON array / scalar / null) is skipped, not fatal —
        # ``.get`` on a non-dict would raise and abort the whole land-time build.
        if not isinstance(parsed, dict):
            continue
        verdict = parsed.get("verdict")
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


def _output_status(output: object) -> str | None:
    """Return the lowercased ``status`` a sub-agent's grafted ``output`` carried, or None (#233).

    The transcript grafts a sub-agent's structured return either as an already-parsed mapping OR as
    a raw JSON string (the tool_result content is frequently a string), so a string is decoded first.
    A non-JSON string (a free-form agent's prose) or a JSON value without a ``status`` key carries no
    outcome and yields None.
    """
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return None
    if isinstance(output, dict) and "status" in output:
        return str(output["status"]).lower()
    return None


def _sub_agent_verdict(body: dict[str, Any]) -> float | None:
    """Return a sub-agent's 0/1 verdict from its returned status, or None when it carried none (#233).

    A schema-returning agent's ``output`` status (:func:`_output_status`) maps to
    :data:`_VERDICT_APPROVE` / :data:`_VERDICT_REJECT` via :data:`_AGENT_SUCCESS_STATUSES`. A
    status-less / plain-text output carries no verdict, so it is skipped. A reaper-killed container
    is handled separately by the caller (a died-class count score, not a point on this 0/1 scale).
    """
    status = _output_status(body.get("output"))
    if status is None:
        return None
    return _VERDICT_APPROVE if status in _AGENT_SUCCESS_STATUSES else _VERDICT_REJECT


def build_agent_verdict_scores(
    spoke_run_id: str, batch: list[IngestEvent], review_dir: Path, *, base_ts: str
) -> list[IngestEvent]:
    """Build per-agent ``agent_verdict:<type>`` scores from reviews and sub-agent outcomes (#233).

    Two sources feed the score family named by the agent type:

    - **code-review** — the signed ``.review/*.json`` artifacts under ``review_dir``, each an APPROVE
      / REQUEST_CHANGES verdict (:func:`_review_artifact_scores`). Trace-level, one per artifact. This
      is the AUTHORITATIVE source for a code-review VERDICT, so a ``sub-agent:code-review`` container
      does not emit an approve/reject score below — but a KILLED reviewer still scores a died count
      (under the distinct ``:died`` name, which cannot collide with the artifact scores).
    - **schema / killed sub-agents** — every non-``llm`` ``sub-agent:<type>`` container in the batch
      (``sub-agent:llm`` is the sub-agent's own LLM turns, not an agent). A reaper-killed (ERROR-level)
      container scores a SEPARATE ``agent_verdict:<type>:died`` count (kept off the 0/1 rate name);
      otherwise, and only for a non-code-review type, a structured ``status`` return scores the 0/1
      :func:`_sub_agent_verdict`. A container with neither signal scores nothing. Observation-scoped.

    All ids derive from the spoke run id so a rerun overwrites the same scores.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (its ``sub-agent:`` containers are read).
        review_dir: The worktree's ``.review`` directory (its ``*.json`` artifacts are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events (empty when no review ran and no sub-agent carried a signal).
    """
    trace_id = trace_id_for(spoke_run_id)
    events = _review_artifact_scores(spoke_run_id, review_dir, trace_id, base_ts)
    for event in batch:
        body = event["body"]
        name = body.get("name") or ""
        if not name.startswith(_SUB_AGENT_PREFIX):
            continue
        agent_type = name[len(_SUB_AGENT_PREFIX) :]
        if agent_type == "llm":  # the sub-agent's own LLM turns, not an agent
            continue
        # A reaper-killed container scores a died count for EVERY type, code-review included — the
        # distinct :died name never collides with code-review's artifact-based verdict scores.
        if body.get("level") == _LEVEL_ERROR:
            events.append(
                _score_event(
                    spoke_run_id,
                    name=f"{_AGENT_VERDICT_SCORE}:{agent_type}:{_DIED_SCORE_SUFFIX}",
                    value=_DIED_FLAG,
                    trace_id=trace_id,
                    base_ts=base_ts,
                    observation_id=body["id"],
                )
            )
            continue
        # code-review's non-died verdict is owned by the authoritative .review artifacts above.
        if agent_type == _REVIEW_AGENT_TYPE:
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


def _cost_subtree_ancestors(
    node_id: str, parent_of: dict[str, str | None], boundary_ids: set[str]
) -> set[str]:
    """Walk ``parentObservationId`` up from ``node_id``, collecting EVERY boundary-span ancestor.

    The shared subtree-cost rollup helper (#322 skill cost, #323 agent cost): a generation is a
    descendant of every boundary span on its ancestor chain, so for nested boundaries (a
    ``skill:brainstorming`` under a ``skill:code-review``, or a ``sub-agent:planner`` under a
    ``sub-agent:general-purpose``) an inner generation credits BOTH — the subtree-rollup boundary.
    The visited set guards against a malformed parent cycle.
    """
    ancestors: set[str] = set()
    seen: set[str] = set()
    current = parent_of.get(node_id)
    while current is not None and current not in seen:
        if current in boundary_ids:
            ancestors.add(current)
        seen.add(current)
        current = parent_of.get(current)
    return ancestors


def build_skill_cost_scores(
    spoke_run_id: str, batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-skill ``skill_cost_usd:<name>`` scores summing each skill's descendant cost (#322).

    A first-class ``skill:<name>`` span (relabeled from ``tool:Skill``, :func:`_skill_relabel`) is a
    span whose OWN ``costDetails`` is $0 — the real LLM spend lives in its generation descendants — so
    Langfuse's own-cost-summing metrics API returns $0 for a ``skill:`` filter. Mirroring
    :func:`build_step_total_cost_scores`, every ``generation-create`` in the assembled batch is walked
    up ``parentObservationId`` (:func:`_cost_subtree_ancestors`) and its full Langfuse
    ``costDetails`` (:func:`_generation_total_cost`) is summed into each skill span on its chain, so a
    skill node's score is the total cost of its SUBTREE. Only actual generation leaves are summed —
    never a nested skill span's own field — so there is no double counting within one skill's score.
    Observation-scoped to the skill node with a deterministic id (a skill run twice keeps both scores).

    A skill span with no generation descendants never enters the accumulator, so it is SKIPPED rather
    than scored 0 — absence of spend is not a cost (mirrors the ready-but-latent ``skill_success``
    idiom; AFK Design Principle 1). Read off View A (the same batch :func:`build_skill_success_scores`
    reads), so it is trace-level like the other skill scores.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (its ``skill:`` spans and generation copies are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events, one per skill node with generation descendants (empty when none).
    """
    trace_id = trace_id_for(spoke_run_id)
    by_id = {event["body"]["id"]: event["body"] for event in batch}
    parent_of = {node_id: body.get("parentObservationId") for node_id, body in by_id.items()}
    skill_ids = {node_id for node_id, body in by_id.items() if _is_skill_span(body)}
    cost_by_skill: dict[str, float] = {}
    for event in batch:
        if event["type"] != "generation-create":
            continue
        cost = _generation_total_cost(event["body"])
        for skill_id in _cost_subtree_ancestors(event["body"]["id"], parent_of, skill_ids):
            cost_by_skill[skill_id] = cost_by_skill.get(skill_id, 0.0) + cost
    return [
        _score_event(
            spoke_run_id,
            name=f"{_SKILL_COST_SCORE}:{_skill_span_name(by_id[skill_id])}",
            value=cost,
            trace_id=trace_id,
            base_ts=base_ts,
            observation_id=skill_id,
        )
        for skill_id, cost in cost_by_skill.items()
    ]


def _is_sub_agent_container(body: dict[str, Any]) -> bool:
    """Whether a node is a ``sub-agent:<type>`` container, not the ``sub-agent:llm`` leaves (#323).

    The ``sub-agent:llm`` generations are the sub-agent's own LLM turns (the cost leaves), never a
    rollup boundary — the same split :func:`build_agent_verdict_scores` / :func:`main_loop_request_count`
    already use.
    """
    name = body.get("name") or ""
    return (
        name.startswith(_SUB_AGENT_PREFIX) and name[len(_SUB_AGENT_PREFIX) :] != _SUB_AGENT_LLM_TYPE
    )


def _sub_agent_type(body: dict[str, Any]) -> str:
    """Return a ``sub-agent:<type>`` container's agent type for the score-name suffix (#323)."""
    return (body.get("name") or "")[len(_SUB_AGENT_PREFIX) :]


def build_agent_cost_scores(
    spoke_run_id: str, batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-sub-agent ``agent_cost_usd:<type>`` scores summing each agent's descendant cost (#323).

    A ``sub-agent:<type>`` span is the otelcol-renamed ``tool:Agent`` container (:data:`_SUB_AGENT_PREFIX`)
    whose OWN ``costDetails`` is $0 — the real LLM spend lives in its ``sub-agent:llm`` generation
    descendants — so Langfuse's own-cost-summing metrics API returns $0 for a ``sub-agent:`` filter.
    Mirroring :func:`build_skill_cost_scores`, every ``generation-create`` in the assembled batch is
    walked up ``parentObservationId`` (:func:`_cost_subtree_ancestors`, the shared #322 rollup helper)
    and its full Langfuse ``costDetails`` (:func:`_generation_total_cost`) is summed into each sub-agent
    container on its chain, so a container's score is the total cost of its SUBTREE. Only actual
    generation leaves are summed — never a nested container's own field — so there is no double counting
    within one agent's score; a nested ``sub-agent:planner`` under a ``sub-agent:general-purpose``
    credits BOTH (the subtree boundary). Observation-scoped to the container with a deterministic id, so
    a fan-out of N same-type agents keeps N distinct scores that the dashboard's Sum-by-Name folds into
    one volume-aware ``agent_cost_usd:<type>`` bar.

    A container with no generation descendants never enters the accumulator, so it is SKIPPED rather
    than scored 0 — absence of spend is not a cost (mirrors the #322 skill-cost / ``skill_success``
    idiom; AFK Design Principle 1). Read off the View A batch (the same one
    :func:`build_agent_verdict_scores` reads), observation-scoped to the container.

    ``agent_cost_usd:<type>`` is a SUBSET of the ``step_total_cost_usd`` of the step the agent sits in
    (that step already folds ``sub-agent:llm`` spend), so the two must not be read as additive.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (its ``sub-agent:`` containers and generation copies read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events, one per sub-agent container with generation descendants (empty
        when no sub-agent ran or none carried spend).
    """
    trace_id = trace_id_for(spoke_run_id)
    by_id = {event["body"]["id"]: event["body"] for event in batch}
    parent_of = {node_id: body.get("parentObservationId") for node_id, body in by_id.items()}
    agent_ids = {node_id for node_id, body in by_id.items() if _is_sub_agent_container(body)}
    cost_by_agent: dict[str, float] = {}
    for event in batch:
        if event["type"] != "generation-create":
            continue
        cost = _generation_total_cost(event["body"])
        for agent_id in _cost_subtree_ancestors(event["body"]["id"], parent_of, agent_ids):
            cost_by_agent[agent_id] = cost_by_agent.get(agent_id, 0.0) + cost
    return [
        _score_event(
            spoke_run_id,
            name=f"{_AGENT_COST_SCORE}:{_sub_agent_type(by_id[agent_id])}",
            value=cost,
            trace_id=trace_id,
            base_ts=base_ts,
            observation_id=agent_id,
        )
        for agent_id, cost in cost_by_agent.items()
    ]


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


def _is_review_span(observation: Observation) -> bool:
    """Whether a source observation is a review span the #280 review-stage window sums.

    Two shapes, per the issue's "afk-answer / code-review agent spans ... plus the spoke-side
    code-review spans":

    - the spoke-side code-review Agent container, otelcol-renamed to ``sub-agent:code-review`` —
      the real-duration review signal today;
    - an ``_afk_emit_span --phase review`` agent span (``agent:review``, ``workflow.phase ==
      "review"``). These broker drain-review spans carry no ``--start-ms`` so they are zero-duration
      instants today (contributing 0, and :func:`_sum_span_ms` skips the score when only they match),
      but the branch keeps the window correct if that emitter ever gains a duration.

    A ``workflow.kind == "step"`` cycle-step REVIEW marker is an instant, not a review window, so it
    is excluded.
    """
    if (observation.get("name") or "").startswith(_CODE_REVIEW_AGENT_PREFIX):
        return True
    return (
        _attr(observation, "workflow.phase") == _REVIEW_PHASE
        and _attr(observation, "workflow.kind", "kind") != "step"
    )


def _sum_span_ms(
    traces: list[TraceObservations], predicate: Callable[[Observation], bool]
) -> int | None:
    """Sum the wall-clock ms of every source observation matching ``predicate``, or None (#280).

    None (not 0) when nothing with a measurable duration matched, so a stage skips its score rather
    than reading a misleading zero. This covers two absence shapes uniformly: no span matched at all,
    AND only zero-duration instants matched — e.g. an afk drain-intervention ``agent:review`` span
    (``_afk_emit_span`` emits it with no ``--start-ms``), so a spoke that had an auto-answer but no
    real code review skips ``stage_review_ms`` instead of emitting 0. Concurrent matching spans each
    book their full window (span-time, matching the duration-rollup buckets).
    """
    total = sum(
        _duration_ms(observation) or 0
        for _orig_trace_id, observations in traces
        for observation in observations
        if predicate(observation)
    )
    return total if total > 0 else None


def _spawn_seed_ms(commits: list[dict[str, Any]], lifecycle: Lifecycle) -> int | None:
    """Return the spawn+seed stage ms: dispatch epoch -> first-commit author time, or None (#280).

    None when either instant is absent, or when the first commit predates the dispatch epoch (a
    relaunch reusing the ``spoke_run_id`` re-stamps the dispatch epoch to the LAST dispatch, so an
    earlier dead-run commit yields a negative delta — dropped, not double-counted; the wall-clock is
    then read from the last dispatch, reconciled against ``relaunch_count``).
    """
    dispatched = lifecycle.dispatched
    first_commit = _iso_to_epoch(_first_commit_at(commits))
    if dispatched is None or first_commit is None or first_commit < dispatched:
        return None
    return (first_commit - dispatched) * 1000


def _gate_answer_ms(traces: list[TraceObservations], lifecycle: Lifecycle) -> int | None:
    """Return the PLAN-gate answer-latency ms: park onset -> auto-answer attempt, or None (#280).

    Extends the trace-level ``gate_park_ms`` window (:func:`_gate_park_bounds`, the park onset) with
    the ``answer-attempt-<N>.epoch`` the drain stamps when it delivers a PLAN-gate answer. None when
    the spoke never parked at a gate, no answer was attempted, or the attempt predates the park
    onset (an unrelated stale epoch).
    """
    bounds = _gate_park_bounds(traces)
    answer = lifecycle.answer_attempt
    if bounds is None or answer is None:
        return None
    onset = _iso_to_epoch(bounds[0])
    if onset is None or answer < onset:
        return None
    return (answer - onset) * 1000


def build_lifecycle_stage_scores(
    spoke_run_id: str,
    traces: list[TraceObservations],
    commits: list[dict[str, Any]],
    lifecycle: Lifecycle,
    *,
    base_ts: str,
) -> list[IngestEvent]:
    """Build the five per-stage overhead scores decomposing a spoke's wall-clock (#280).

    Each stage is a trace-level NUMERIC score chartable like ``gate_park_ms``, derived at land time
    from already-on-disk / in-trace sources (backfill — no broker change):

    - ``stage_spawn_seed_ms`` — dispatch epoch -> first-commit author time (:func:`_spawn_seed_ms`).
    - ``stage_gate_answer_ms`` — PLAN-gate park onset -> auto-answer attempt (:func:`_gate_answer_ms`).
    - ``stage_review_ms`` — Σ the review span windows (:func:`_is_review_span`).
    - ``stage_push_gate_ms`` — Σ the ``spoke-push`` span windows (the pre-push test gate).
    - ``stage_land_ms`` — the ``worktree-land`` span window (absent until the land span closes, so
      usually skipped at land time and captured on a later backfill re-run).

    A stage whose source is absent is SKIPPED (never emitted as 0) so "not measured" reads
    distinctly from a real zero, matching the #231 graceful-skip idiom. All ids derive from the
    spoke run id, so a rerun overwrites the same scores (idempotent).

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        traces: The source traces (review / push / land / gate-park windows read here).
        commits: The parsed commit records (for the first-commit author time).
        lifecycle: The gathered per-issue sources (dispatch / answer-attempt epochs).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events, one per stage whose source was present (empty when none).
    """
    trace_id = trace_id_for(spoke_run_id)
    stages: dict[str, int | None] = {
        _STAGE_SPAWN_SEED_SCORE: _spawn_seed_ms(commits, lifecycle),
        _STAGE_GATE_ANSWER_SCORE: _gate_answer_ms(traces, lifecycle),
        _STAGE_REVIEW_SCORE: _sum_span_ms(traces, _is_review_span),
        _STAGE_PUSH_GATE_SCORE: _sum_span_ms(
            traces, lambda o: (o.get("name") or "") == _PUSH_SCRIPT_NAME
        ),
        _STAGE_LAND_SCORE: _sum_span_ms(
            traces, lambda o: (o.get("name") or "") == _LAND_SCRIPT_NAME
        ),
    }
    return [
        _score_event(spoke_run_id, name=name, value=value, trace_id=trace_id, base_ts=base_ts)
        for name, value in stages.items()
        if value is not None
    ]


def _root_duration_components(spoke_run_id: str, batch: list[IngestEvent]) -> dict[str, float]:
    """Return the View A root's ``rollup.duration.components`` map, or ``{}`` (#280).

    ``_apply_container_rollups`` stamps the per-class exclusive-time split on the synthetic root; a
    batch built before that pass (or a malformed one) yields ``{}`` rather than crashing.
    """
    root_id = root_id_for(spoke_run_id)
    for event in batch:
        if event["body"].get("id") != root_id:
            continue
        duration = ((event["body"].get("metadata") or {}).get("rollup") or {}).get("duration") or {}
        components = duration.get("components") or {}
        return components if isinstance(components, dict) else {}
    return {}


def build_window_rollup_scores(
    spoke_run_id: str,
    batch: list[IngestEvent],
    stage_scores: list[IngestEvent],
    lifecycle: Lifecycle,
    *,
    base_ts: str,
) -> list[IngestEvent]:
    """Build the per-drain-window rollup scores, snapshotted at this spoke's land time (#280).

    The per-spoke builder never sees sibling spokes, so the window rollups are read off the afk
    state dir the shell snapshotted (dispatch-epoch count + intervention-ledger line count) and
    stamped as trace-level scores on THIS spoke's own trace; a dashboard filtered to ``mode:afk``
    reads the latest (fullest) snapshot per window:

    - ``issues_per_hour`` — ``spokes_serviced ÷ window_hours`` (window = earliest dispatch epoch ->
      landed). Skipped when the window is non-positive or the count is absent.
    - ``overhead_work_ratio`` — Σ the five overhead stage scores ÷ Σ the root's WORK duration
      components (:data:`_WORK_DURATION_CLASSES`). Skipped when the work sum is 0 (no divide-by-zero).
    - ``autonomy_score`` — ``max(0, 1 - interventions ÷ spokes_serviced)`` (#251). Absent ledger
      counts as 0 firings (an unwritten ledger is "no interventions", matching ``_wd_intervention_count``);
      skipped only when no spoke was serviced.

    All ids derive from the spoke run id (idempotent reruns).

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        batch: The assembled View A events (the root's work duration components read here).
        stage_scores: The stage score events from :func:`build_lifecycle_stage_scores` (the ratio
            numerator).
        lifecycle: The gathered window snapshot (spokes serviced / interventions / window bounds).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events for each rollup whose inputs resolved (empty when none).
    """
    trace_id = trace_id_for(spoke_run_id)
    values: dict[str, float] = {}
    spokes = lifecycle.spokes_serviced
    if spokes and lifecycle.landed is not None and lifecycle.window_start is not None:
        window_s = lifecycle.landed - lifecycle.window_start
        if window_s > 0:
            values[_ISSUES_PER_HOUR_SCORE] = spokes / (window_s / _SECONDS_PER_HOUR)
    overhead_ms = sum(event["body"]["value"] for event in stage_scores)
    work_ms = sum(
        float(v)
        for cls, v in _root_duration_components(spoke_run_id, batch).items()
        if cls in _WORK_DURATION_CLASSES and isinstance(v, (int, float))
    )
    # Skip when NO overhead stage was measured (``not stage_scores``) as well as on a zero work
    # denominator, so a ratio is never a misleading 0.0 that reads as "0% overhead" when it is really
    # "overhead not measured" — the same skip-vs-real-zero care the stage scores take.
    if work_ms > 0 and stage_scores:
        values[_OVERHEAD_WORK_RATIO_SCORE] = overhead_ms / work_ms
    if spokes:
        interventions = lifecycle.interventions or 0
        values[_AUTONOMY_SCORE] = max(0.0, 1 - interventions / spokes)
    return [
        _score_event(spoke_run_id, name=name, value=value, trace_id=trace_id, base_ts=base_ts)
        for name, value in values.items()
    ]
