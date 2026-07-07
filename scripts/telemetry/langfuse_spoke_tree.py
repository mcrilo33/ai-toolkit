#!/usr/bin/env python3
"""Assemble a spoke's existing rich Langfuse observations into one nested trace.

Natively, each turn Claude Code runs lands as its own flat Langfuse trace, and the
marker (``lifecycle:``/``spoke-push``) and hook (``*.sh``) emissions land as
yet more flat traces (cycle ``step:`` nodes are no longer emitted — they are synthesized from
the todo ledger, see :func:`build_step_windows`). Every one of those observations already carries the rich fields we
built — ``usageDetails``, ``costDetails``, ``input``/``output`` messages, ``metadata``
(including ``rollup`` and, on hooks, ``hook_event``/``tool_name``/``tool_use_id``/
``decision``/``duration_ms``), ``name``, ``type``, and ``startTime``/``endTime``. A spoke
therefore reads as dozens of disconnected traces.

This post-run script SOURCES FROM LANGFUSE — it does not rebuild from the causal store.
It fetches every trace in the session and every observation in those traces, then COPIES
each observation verbatim into TWO complementary nested traces, re-parenting across the
original trace boundaries so the whole spoke renders as a single tree with every field intact::

    LANGFUSE_HOST=http://localhost:3000 LANGFUSE_BASIC_AUTH="Basic <base64(pk:sk)>" \\
        python3 scripts/telemetry/langfuse_spoke_tree.py <spoke_run_id>

Two views over the SAME observation copies (#113), differing only in the top-level parent:

- **View A — the nested/interaction lens** (``spoketree-<spoke>``, :func:`build_batch`). Keeps
  the per-turn ``claude_code.interaction`` nesting; each ledger step wraps its contiguous run of
  same-parent siblings in a ``step:<subject>`` node inserted INSIDE the interaction
  (:func:`_apply_step_grouping`).
- **View B — the cycle/phase lens** (``spokecycle-<spoke>``, :func:`build_cycle_batch`). Flattens
  each top-level ``claude_code.interaction`` from a container to a childless leaf turn-marker and
  re-homes the copies onto a pure cycle axis — ``preStep`` + one ``step:<subject>`` per ledger task
  + ``postStep`` — placing each real span (and each turn-marker) by its timestamp and letting audit
  instants ride their tool/llm_request by causal key (:func:`_apply_cycle_axis`). The childless
  marker is stamped with its turn's ``metadata.rollup`` (the sum of its pre-flatten subtree, whose
  generations re-home onto the steps), recovering per-turn cost reading (#114).
  Its copies live in a separate id namespace, so the two traces never collide (~2x copies per
  spoke in the local store, a conscious choice).

Re-parenting rules for each source observation:

- It had a ``parentObservationId`` -> the copy points at the copy of that parent.
- It was a trace-root interaction / marker / lifecycle / script -> the synthetic root.
- It was a trace-root satellite of a tool call -> the copy of the tool whose
  ``tool_use_id`` matches the satellite's; or, when the satellite names a tool that produced no
  span (the tool was denied/cancelled), a synthesized ``blocked-tool:<Name>`` node standing in
  for that missing call (#157, :func:`_synthesize_blocked_tools`) — itself parented to the
  enclosing ``claude_code.interaction`` by ``prompt.id``, falling back to ``[start,end]`` window
  containment (#110, :func:`_enclosing_turn`), reaching the synthetic root only when no turn
  encloses it. Only a satellite naming no tool (no ``tool_use_id``) reaches the root directly. A
  satellite is a gate hook (name ends ``.sh`` or ``metadata.attributes.workflow.kind == hook``)
  or a #93 tool-scoped audit event (``tool_result``, minted on the per-spoke audit trace with its
  ``tool_use_id`` in flat metadata). (Langfuse nests OTel span attributes under
  ``metadata["attributes"]``; the audit events carry their id at the metadata top level.)

Three native 1:1 sub-spans do NOT nest — they FOLD into their tool's metadata and their nodes
are dropped (#100, :func:`_fold_tool_subspans`): ``claude_code.tool.execution`` ->
``execution_ms``/``success``/``error``, ``claude_code.tool.blocked_on_user`` ->
``blocked_on_user_ms``/``decision``/``decision_source``, and the ``tool_decision:<d>`` audit
event -> ``decision``/``decision_source``. An unmatched ``tool_decision`` (the tool was
denied/cancelled, so no span) folds onto the synthesized ``blocked-tool`` node that stands in for
the missing call (#157), supplying its ``decision`` (deny/ask); its own node is dropped.

The same session also carries the ``spoke-audit:`` trace's span-less audit/lifecycle events
(#93). They are folded in here too (#104), placed by CAUSAL id-join — never by their lagging
OTel-logs ``startTime`` (the logs signal is batched and exported after the spans it interleaves
with, so the timestamp lags the event). Tool-scoped events (``hook_execution_complete``,
``tool_result``, ``tool_decision``) nest/fold under their tool by ``tool_use_id`` as above;
``api_error``/``api_refusal`` nest under their ``llm_request`` by ``request_id``;
``skill_activated`` nests under the ``tool:Skill`` that activated it — matched by ``prompt.id``
+ ``skill.name`` (+ nearest timestamp), falling back to its enclosing turn (#110 AC2,
:func:`_match_skill_tool`); the session-startup instants (``mcp_server_connection``,
``plugin_loaded``) are DEMOTED to the synthetic root's ``session_init`` metadata list
(:func:`_collapse_startup_instants`) instead of standing as sibling nodes; and the remaining
unresolvable instants (``permission_mode_changed``, ``compaction``) fall to the root as a last
resort. Every audit instant is excluded from the cycle-step window placement
(:func:`_apply_step_grouping`), which
keys off ``startTime``. The standalone ``spoke-audit:`` trace is read-only here — additive only.

All ids derive from the spoke run id and the source ``(trace_id, observation_id)`` pair,
so a rerun overwrites the same trace/observations instead of appending. This trace
DUPLICATES the native per-turn traces by design — it is the assembled, nested view.

Tool content from the transcript: Claude Code's native OTel surfaces the full
``full_command`` for Bash, but every other tool (TaskCreate/TaskUpdate, Read, Edit, ...)
arrives with ``input=None`` — only ``tool_name``/``tool_use_id``/``duration``. The real
content lives in the session TRANSCRIPT (``*.jsonl``): each assistant ``tool_use`` block
carries ``{id, name, input}`` and the matching user ``tool_result`` block carries
``{tool_use_id, content}``. Because the copy step CREATES fresh observations (one
``*-create`` event setting every field at once), it fills that content into the create
body at build time, keyed by ``tool_use_id`` — non-destructively, so collector-provided
input (Bash) is never overwritten. (A standalone UPDATE-based patcher used to do this, but
an update body that omits ``name``/``type`` makes Langfuse CLEAR them, so it was retired.)

Beyond copying, the build adds two CREATE-only enrichments (no patches):

- **Per-container token + duration rollups.** Every container node (each interaction,
  ``tool:Agent``, sub-agent, and the synthetic root) gets ``metadata.rollup = {reused,
  written, input, output}`` summed over its subtree of the re-parented tree, reusing
  ``langfuse_rollup``'s sum logic but written into the create body — plus
  ``rollup.duration = {total_ms, components}`` (#128): the subtree wall-clock split by
  class (``llm_request`` / ``tool`` / ``hook`` / ``script`` / ``step`` / ``wait`` /
  ``turn`` / ``self`` / ``other``) via exclusive-time attribution — on serial spans the
  components sum to the observed wall-clock; concurrent siblings each book their full
  span time (class buckets are span-time, like CPU-time vs wall-time) while gap buckets
  stay true via union-based subtraction. Folded ``blocked_on_user_ms``, unmatched
  blocked-on-user spans, and the gate script count as ``wait``; a container's own
  unattributed gap is ``self`` (inter-turn idle on the root).
  View B's childless turn-markers are stamped from their pre-flatten View A subtree and
  excluded from the cycle-axis duration sums — same no-double-count rule as the #114
  token stamping.
- **A collapsed loaded-context node.** A SINGLE ``loaded-context`` node under the root —
  static startup inventory is not work, so it does not earn a ~60-leaf timeline subtree.
  Its headline ``metadata.tokens`` is the total startup context tokens (the one number
  worth aggregating across spokes); ``metadata.breakdown`` carries the full itemization
  grouped by category (``{category: {name: tokens}}``). The primary source is the spoke's
  untruncated raw request body (``OTEL_LOG_RAW_API_BODIES=file:<dir>``, located via
  ``--request-bodies`` / ``$AI_TOOLKIT_OTEL_BODY_DIR``): ``request_body`` itemizes the WHOLE
  first-call prefix — every tool and MCP tool by name + exact size, each system block, and
  each ``messages[0]`` ``<system-reminder>`` by kind — so no reconciliation is needed. When
  no request body is available, it falls back to disk measurement of rules / memory /
  skills / sub-agents / environment (via ``measure_context_cost``) plus a single reconciled
  ``remainder`` (``prefix - Σ measured disk``, clamped ≥ 0) folded into ``metadata.remainder``
  and the total — absorbing the base system prompt, tool schemas, and MCP together, the
  full prefix being ``cache_read + cache_creation`` of the first LLM call.

Import-safe: no environment is read at import time, so :func:`build_batch`,
:func:`scan_transcripts`, and :func:`build_loaded_context_events` are unit-testable with no
network. The HTTP I/O happens only in :func:`main`. Stdlib only; reuses the fetch/post
helpers, env vars, and ingestion endpoint of ``langfuse_rollup``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, cast

from telemetry.langfuse_rollup import (
    DeleteFn,
    GetFn,
    Observation,
    PostFn,
    all_observations,
    build_tree,
    make_delete,
    make_get,
    make_post,
    rollup_metadata,
    subtree_totals,
)
from telemetry.measure_context_cost import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    TokenCounter,
    assemble_items,
    make_counter,
    measure_items,
)
from telemetry.request_body import (
    ContextDelta,
    ContextItem,
    decompose_request_body,
    diff_snapshots,
    first_real_request,
    measure_request_items,
    parse_request_body,
    snapshot_items_from_path,
)
from telemetry.session_parser import project_dir_for_worktree
from telemetry.spoke_tree.ids import (
    _CYCLE_STEP_PREFIX,
    _blocked_tool_id,
    _copy_id,
    _cycle_copy_id,
    _cycle_step_id,
    _guards_id,
    cycle_copy_id_for,
    cycle_root_id_for,
    cycle_trace_id_for,
    root_id_for,
    trace_id_for,
)
from telemetry.spoke_tree.observations import (
    _BLOCKED_TOOL_NAME_PREFIX,
    _FOLD_BLOCKED_NAME,
    _FOLD_DECISION_PREFIX,
    _FOLD_EXECUTION_NAME,
    _GUARDS_NAME,
    _GUARDS_SESSION_NAME,
    _INGEST_TIMESTAMP,
    _INTERACTION_NAME,
    _POST_STEP_NAME,
    _PRE_STEP_NAME,
    _WAIT_PREFIX,
    IngestEvent,
    ToolContent,
    TraceObservations,
    _attr,
    _duration_ms,
    _elapsed_ms,
    _is_audit_instant,
    _is_blocked_tool,
    _is_fold_subspan,
    _is_gate_observation,
    _is_graftable_span,
    _is_guards_group,
    _is_hook,
    _is_hook_event,
    _is_interaction,
    _is_request_audit_event,
    _is_skill_activated,
    _is_startup_instant,
    _is_tool_span,
    _joins_under_tool,
    _obs_envelope,
    _parse_ts,
    _parse_utc,
    _prompt_id,
    _request_id,
    _skill_name,
    _tool_use_id,
)

logger = logging.getLogger("langfuse_spoke_tree")


# Deterministic id prefix for the synthetic cycle-step nodes (#100, derived from the ledger).
_STEP_PREFIX = "tree-step-"
# Synthetic timeline nodes (#162): git commits and the PLAN-gate park, keyed off the spoke run
# id (+ sha) so a rerun overwrites the same node. The ``wait:`` name prefix routes the park into
# the duration ``wait`` bucket (see _duration_class); the field separator is the byte git emits
# between --format fields in the commit dump (_parse_commits).
_COMMIT_PREFIX = "tree-commit-"
_GATE_PARK_PREFIX = "tree-gatepark-"
_GATE_PARK_NODE_NAME = "wait:gate-park"
_COMMIT_FIELD_SEP = "\x1f"
_COMMIT_LINE_MARKER = "commit"
_CYCLE_TRACE_NAME_PREFIX = "spoke-cycle:"
_CYCLE_ROOT_NAME_PREFIX = "cycle:"
# The cycle-axis bookend node names + the keys that map to their synthetic ids.
_PRE_STEP_KEY = "pre"
_POST_STEP_KEY = "post"
# Deterministic id prefix for the numeric Langfuse scores (#100 amendment: chartable time budget).
_SCORE_PREFIX = "tree-score-"
# Score names — Langfuse sums/charts numeric scores (it cannot chart arbitrary metadata).
_PERMISSION_WAIT_SCORE = "permission_wait_ms"  # per blocked tool observation
_GATE_PARK_SCORE = "gate_park_ms"  # trace-level PLAN-gate park wait
_TOOL_RESULT_SIZE_SCORE = "tool_result_size"  # bytes of a tool node's reconstructed tool_result
# Per-phase step cost/token scores (#158): the phase is the score-name suffix (a metrics
# dimension), so "what does RED cost across all spokes" is a one-widget Scores query.
_STEP_COST_SCORE = "step_cost_usd"  # per View B step observation, from rollup.written x price
_STEP_TOKENS_WRITTEN_SCORE = (
    "step_tokens_written"  # per View B step observation, from rollup.written
)
# The canonical solo-cycle phases parsed out of a step subject (e.g. "A-RED: …" → RED). Kept a
# closed set so a step subject can never mint a free-text score name (a metrics-cardinality guard).
_STEP_PHASES = ("ANCHOR", "RED", "GREEN", "REVIEW", "PUSH")
_STEP_PHASE_OTHER = "other"
_STEP_PHASE_RE = re.compile(rf"\b({'|'.join(_STEP_PHASES)})\b")
# output_config.effort handling (#101). ``ultra`` is the ultracode/harness mode, NOT an
# effort level: it is diverted to a spoke-level ``ultracode`` trace tag, never recorded as
# an ``effort:<value>`` tag or on llm_request metadata.
_ULTRA_MODE = "ultra"
_ULTRACODE_TAG = "ultracode"
_TRACE_NAME_PREFIX = "spoke-tree:"
_ROOT_NAME_PREFIX = "spoke:"


# Max page size the Langfuse traces endpoint accepts.
_PAGE_LIMIT = 100
# Max ingestion events per POST, to keep each request small.
_CHUNK_SIZE = 100

# Builder generation stamped into both trace-create bodies (#156) so a consumer can tell
# which builder produced a stored view. Bump on any change to the assembled view shape.
# rev 2 (#157): guards / guards:session group nodes, blocked-tool:* synthesis, hook endTime
# stamping, and WARNING/ERROR failure levels.
_SCHEMA_REV = 2

# --rebuild purge poll (#156): a bulk trace delete is asynchronous on the Langfuse server,
# so after issuing it we poll the session listing until both view traces are gone before
# re-posting. Give up (raise) after the budget rather than re-post over a half-deleted trace.
_PURGE_POLL_ATTEMPTS = 30
_PURGE_POLL_INTERVAL = 1.0

# Observation fields copied verbatim into the assembled trace when present.
_COPIED_FIELDS = ("input", "output", "usageDetails", "costDetails", "metadata", "model", "level")


_GUARD_NOOP_MAX_MS = 1000

_BLOCKED_TOOL_UNKNOWN = "unknown"
_TOTAL_DURATION_KEY = "total_duration_ms"
_TIME_SOURCE_KEY = "time_source"
_TIME_SOURCE_LAGGING = "lagging"

# Failure levels (#157). The native trace is all ``level=DEFAULT`` even though the failure data is
# already folded onto tools; :func:`_apply_levels` maps it: a failed/errored tool -> ERROR; a
# non-allow / failed guard (span + its group), a blocking ``hook_execution_complete``, and a
# synthesized blocked-tool -> WARNING (precedence ERROR > WARNING > DEFAULT).
_LEVEL_ERROR = "ERROR"
_LEVEL_WARNING = "WARNING"
_STATUS_SUCCESS = "success"
_GUARD_WARN_DECISIONS = ("deny", "ask", "block")
_NUM_BLOCKING_KEY = "num_blocking"
# Attribute keys naming the blocked tool, in priority order (bare tool name, then the
# ``<HookEvent>:<Tool>`` hook name whose suffix is the tool).
_TOOL_NAME_KEYS = ("tool_name", "gen_ai.tool.name")
_HOOK_NAME_KEY = "hook_name"

# Root metadata field collecting the demoted startup instants.
_SESSION_INIT_FIELD = "session_init"


# Tool content (e.g. a large file Read) can be huge; cap the serialized text past this.
_MAX_CONTENT_CHARS = 20_000
_TRUNCATION_MARKER = "...[truncated]"

# Default root holding Claude Code session transcripts.
_DEFAULT_PROJECTS = Path("~/.claude/projects").expanduser()

# Deterministic id prefix for the synthetic loaded-context node.
_LC_PREFIX = "tree-lc-"
# Default cache-creation price (USD per token), Opus tier — mirrors measure_context_cost.
_DEFAULT_PRICE = 0.00000625
# Category order for the request-body itemization (the primary, fully-itemized path). Carries
# the turn-0 combined-block router's rules / skills / environment splits (see
# request_body._route_reminder, #159) between ``system`` and the whole-kept ``context``
# reminders; empty categories are dropped by _breakdown_by_category.
_REQUEST_CATEGORY_ORDER = ("tools", "mcp", "system", "rules", "skills", "environment", "context")
# Component order for the per-llm_request cache decomposition (#99), in request order so the
# stable prefix (tools/system/rules/skills) groups ahead of the volatile messages.
_DECOMP_CATEGORY_ORDER = (
    "tools",
    "mcp",
    "system",
    "rules",
    "skills",
    "environment",
    "context",
    "messages",
)
# Category order for the disk fallback used when no request body is available.
_DISK_CATEGORY_ORDER = ("rules", "memory", "skills", "sub-agents", "environment")
# Env var naming the per-spoke dir of OTEL_LOG_RAW_API_BODIES=file:<dir> dumps.
_BODY_DIR_ENV = "AI_TOOLKIT_OTEL_BODY_DIR"
# Conventional per-spoke body dir under a worktree root (worktree-new.sh writes here).
_BODY_DIR_CONVENTION = Path(".ai-toolkit/raw-bodies")
# Execution mode + lane pointer files under a worktree root, stamped at launch by
# worktree-new.sh / worktree-quick.sh (#102). Surfaced as trace tags + metadata.
_MODE_POINTER = Path(".ai-toolkit/mode")
_LANE_POINTER = Path(".ai-toolkit/lane")
_VALID_MODES = ("afk", "attended")
_VALID_LANES = ("micro", "express", "quick", "spoke")
_DEFAULT_MODE = "attended"
_DEFAULT_LANE = "spoke"


class StepWindow(NamedTuple):
    """One solo-cycle step derived from the todo ledger (#100).

    The ``subject`` is the ``TaskCreate`` title (``S1 RED: …``); the window spans the task's
    ``in_progress`` ``TaskUpdate`` start to its ``completed`` ``TaskUpdate`` end. In View A the
    same-parent interaction siblings whose ``startTime`` falls in ``[start, end]`` re-home under a
    local step node (:func:`_apply_step_grouping`); in View B every reliably-timestamped span in
    the window re-homes under the cycle step.
    """

    task_id: str
    subject: str
    start: str
    end: str
    status: str


# Matches the numeric task id in a TaskCreate result ("Task #1 created successfully: …"); the
# matching TaskUpdate carries the same id (bare digits) in its ``taskId`` input.
_TASK_ID_RE = re.compile(r"#(\d+)")
# The visible Skill tool span and the span-less lifecycle event it activates (#110 AC2). The
# event carries no ``tool_use_id``, so it is matched to its tool by ``prompt.id`` + ``skill.name``
# (+ nearest timestamp), see :func:`_build_skill_index` / :func:`_match_skill_tool`.
_SKILL_TOOL_NAME = "tool:Skill"
# The Skill tool's input key naming the activated skill, read from the transcript content.
_SKILL_INPUT_KEY = "skill"


def _fold_attrs(observation: Observation) -> dict[str, Any]:
    """Return the metadata a fold sub-span contributes to its tool node (see :func:`_is_fold_subspan`).

    The ``*_ms`` values derive from the span's own duration and are robust; the ``success`` /
    ``error`` / ``decision`` / ``decision_source`` reads probe several candidate attribute keys
    (bare and ``claude_code.tool.*``-namespaced) since the exact native OTel names vary. When a
    tool has both a blocked-on-user and a tool_decision sub-span they both write ``decision``;
    last-writer-wins, and the two are expected to agree.

    UPGRADE: pin the success/error/decision/source attribute keys once confirmed against a real
    Claude Code OTel trace — the duration-derived ``*_ms`` already fold reliably regardless.
    """
    name = observation.get("name") or ""
    out: dict[str, Any] = {}
    if name == _FOLD_EXECUTION_NAME:
        ms = _duration_ms(observation)
        if ms is not None:
            out["execution_ms"] = ms
        success = _attr(observation, "success", "claude_code.tool.success", "gen_ai.tool.success")
        if success is not None:
            out["success"] = bool(success)
        error = _attr(observation, "error", "error.message", "claude_code.tool.error")
        if error:
            out["error"] = error
    elif name == _FOLD_BLOCKED_NAME:
        ms = _duration_ms(observation)
        if ms is not None:
            out["blocked_on_user_ms"] = ms
        out.update(_decision_attrs(observation))
    elif name.startswith(_FOLD_DECISION_PREFIX):
        suffix = name.split(":", 1)[1] if ":" in name else None
        out.update(_decision_attrs(observation, default_decision=suffix))
    return out


def _decision_attrs(
    observation: Observation, *, default_decision: str | None = None
) -> dict[str, Any]:
    """Return the ``decision`` / ``decision_source`` a blocked/decision sub-span contributes."""
    out: dict[str, Any] = {}
    decision = _attr(observation, "decision", "claude_code.tool.decision") or default_decision
    if decision:
        out["decision"] = decision
    source = _attr(observation, "decision_source", "source", "claude_code.tool.decision_source")
    if source:
        out["decision_source"] = source
    return out


def _fold_owner(
    observation: Observation,
    orig_trace_id: str,
    tool_index: dict[str, str],
    tool_span_ids: set[str],
) -> str | None:
    """Return the copy id of the tool a fold sub-span belongs to, or None.

    The audit ``tool_decision`` joins by ``tool_use_id``; the native execution / blocked spans
    are children of their tool, so they also fall back to the copy of their
    ``parentObservationId`` — but ONLY when that parent is itself a ``tool:`` span, so a sub-span
    whose parent is an interaction (or another sub-span, e.g. a resume nested under a
    ``tool.execution``) is never folded onto a non-tool node.
    """
    tuid = _tool_use_id(observation)
    if tuid and tuid in tool_index:
        return tool_index[tuid]
    parent = observation.get("parentObservationId")
    if parent:
        parent_copy = _copy_id(orig_trace_id, parent)
        if parent_copy in tool_span_ids:
            return parent_copy
    return None


def _fold_tool_subspans(
    copies: list[IngestEvent], traces: list[TraceObservations], tool_index: dict[str, str]
) -> list[IngestEvent]:
    """Fold the three 1:1 tool sub-spans into their tool's metadata, dropping their nodes (#100).

    Each execution / blocked-on-user / ``tool_decision`` sub-span's fields are merged onto the
    owning ``tool:`` node's metadata and the sub-span copy is removed. A sub-span whose tool is
    absent (an unmatched audit event) is left as-is — it keeps its node and collapses to the root.

    A folded sub-span can itself have children — a resume ``claude_code.interaction`` nests under
    the push command's ``tool.execution`` via TRACEPARENT — so any node parented on a folded
    sub-span is re-homed onto the fold owner (the tool) before the sub-span is dropped, so its
    subtree (and its tokens in the container rollups) survives rather than dangling on a deleted id.

    Args:
        copies: The source observation copies; owner tool bodies and orphaned children's parents
            are mutated in place.
        traces: The source traces (to walk every sub-span and resolve its owner).
        tool_index: Tool-call-id to tool-copy-id map from :func:`_build_tool_index`.

    Returns:
        The copies with the folded sub-spans removed.
    """
    by_id = {event["body"]["id"]: event for event in copies}
    tool_span_ids = {
        _copy_id(orig_trace_id, observation["id"])
        for orig_trace_id, observations in traces
        for observation in observations
        if _is_tool_span(observation)
    }
    reparent: dict[str, str] = {}  # folded sub-span copy id -> its fold owner (the tool)
    for orig_trace_id, observations in traces:
        for observation in observations:
            if not _is_fold_subspan(observation):
                continue
            owner = _fold_owner(observation, orig_trace_id, tool_index, tool_span_ids)
            if owner is None or owner not in by_id:
                continue  # no tool to fold into — leave the sub-span as a node
            attrs = _fold_attrs(observation)
            if attrs:
                by_id[owner]["body"].setdefault("metadata", {}).update(attrs)
            reparent[_copy_id(orig_trace_id, observation["id"])] = owner
    for event in copies:
        if event["body"]["id"] in reparent:
            continue  # this node is itself being dropped
        parent = event["body"].get("parentObservationId")
        if parent in reparent:
            while parent in reparent:  # resolve through any chain of folded ancestors
                parent = reparent[parent]
            event["body"]["parentObservationId"] = parent
    return [event for event in copies if event["body"]["id"] not in reparent]


def _guard_noop(body: Observation) -> bool:
    """Whether a guard span is a droppable no-op: ``decision=allow`` ∧ ``status=success`` ∧ <1s."""
    ms = _duration_ms(body)
    return (
        _attr(body, "decision") == "allow"
        and _attr(body, "status") == "success"
        and ms is not None
        and ms < _GUARD_NOOP_MAX_MS
    )


def _guard_group_metadata(members: list[IngestEvent]) -> dict[str, Any]:
    """Return a guards group's rollup over ALL its raw guard spans (before any are dropped).

    ``by_hook`` keys are sorted and ``decisions`` de-duplicated + sorted so the group body is
    byte-stable across reruns; ``count`` / ``total_ms`` / per-hook ``ms`` sum every member,
    including the no-op spans dropped from the tree (#157 AC1).
    """
    by_hook: dict[str, dict[str, int]] = {}
    total_ms = 0
    decisions: set[str] = set()
    for member in members:
        body = member["body"]
        name = body.get("name") or ""
        ms = _duration_ms(body) or 0
        entry = by_hook.setdefault(name, {"count": 0, "ms": 0})
        entry["count"] += 1
        entry["ms"] += ms
        total_ms += ms
        decision = _attr(body, "decision")
        if decision is not None:
            decisions.add(str(decision))
    return {
        "count": len(members),
        "total_ms": total_ms,
        "by_hook": {name: by_hook[name] for name in sorted(by_hook)},
        "decisions": sorted(decisions),
    }


def _guard_envelope(members: list[IngestEvent]) -> tuple[str | None, str | None]:
    """Return the (min start, max end) ISO bounds over the guard members, chronologically."""
    return _obs_envelope([member["body"] for member in members])


def _guard_group_event(
    parent_id: str, members: list[IngestEvent], *, trace_id: str, root_id: str
) -> IngestEvent:
    """Build the synthetic ``guards`` / ``guards:session`` group node for one parent's guards."""
    group_id = _guards_id(parent_id)
    name = _GUARDS_SESSION_NAME if parent_id == root_id else _GUARDS_NAME
    start, end = _guard_envelope(members)
    body: dict[str, Any] = {
        "id": group_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        "name": name,
        "startTime": start or _INGEST_TIMESTAMP,
        "endTime": end,
        "metadata": _guard_group_metadata(members),
    }
    if any(_guard_warns(member["body"]) for member in members):
        body["level"] = _LEVEL_WARNING  # a non-allow / failed guard flags its whole group (#157)
    return {
        "id": group_id,
        "type": "span-create",
        "timestamp": start or _INGEST_TIMESTAMP,
        "body": body,
    }


def _apply_guard_groups(
    copies: list[IngestEvent],
    *,
    trace_id: str,
    root_id: str,
    tool_owner_ids: set[str],
    keep_noop_guards: bool,
) -> list[IngestEvent]:
    """Collapse each tool's (and the session's) ``.sh`` guard copies under a ``guards`` group (#157).

    A guard copy (:func:`_is_hook`) whose resolved parent is a tool owner or the synthetic root is
    re-homed under a synthesized ``guards`` group (``guards:session`` at the root) parented where
    the guard sat. No-op guards (:func:`_guard_noop`) are dropped unless ``keep_noop_guards``; the
    survivors keep their nodes under the group. Guards resolved under anything else (e.g. an
    interaction) and non-guard satellites are left untouched. The group's ``by_hook`` rollup counts
    every raw guard including the dropped ones (:func:`_guard_group_metadata`).

    Args:
        copies: The assembled copies; guard copies are re-parented or dropped in place.
        trace_id: The assembled trace id every group node references.
        root_id: The synthetic root id (host of the ``guards:session`` group).
        tool_owner_ids: Copy ids that own a tool call (real tool spans + synthesized blocked-tools).
        keep_noop_guards: When True, no-op guards are retained under their group instead of dropped.

    Returns:
        The copies with grouped guards re-parented, no-ops dropped, and group nodes appended.
    """
    grouped: dict[str, list[IngestEvent]] = {}
    for event in copies:
        body = event["body"]
        if not _is_hook(body):
            continue
        parent = body.get("parentObservationId")
        if parent in tool_owner_ids or parent == root_id:
            grouped.setdefault(parent, []).append(event)
    if not grouped:
        return copies
    dropped: set[str] = set()
    group_events: list[IngestEvent] = []
    for parent_id, members in grouped.items():
        group = _guard_group_event(parent_id, members, trace_id=trace_id, root_id=root_id)
        group_events.append(group)
        for member in members:
            if not keep_noop_guards and _guard_noop(member["body"]):
                dropped.add(member["body"]["id"])
            else:
                member["body"]["parentObservationId"] = group["body"]["id"]
    kept = [event for event in copies if event["body"]["id"] not in dropped]
    return kept + group_events


def _stamp_hook_endtimes(copies: list[IngestEvent]) -> list[IngestEvent]:
    """Give each ``hook_execution_complete`` copy a derived endTime from ``total_duration_ms`` (#157).

    A hook event carries ``total_duration_ms`` but no ``endTime``; set ``endTime = startTime +
    total_duration_ms`` and ``metadata.time_source = "lagging"`` so the timeline can render its
    width while flagging it as derived. Events missing a start or ``total_duration_ms`` are left
    untouched. Mutates the bodies in place and returns ``copies``.
    """
    for event in copies:
        body = event["body"]
        if not _is_hook_event(body) or body.get("endTime"):
            continue
        start = body.get("startTime")
        total = _attr(body, _TOTAL_DURATION_KEY)
        # UPGRADE: accept only a native numeric counter; a numeric-string total_duration_ms would
        # be skipped (left zero-width). Coerce here if a future emission path ever stringifies it.
        if not start or not isinstance(total, (int, float)):
            continue
        parsed = _parse_utc(start)
        if parsed is None:
            continue
        end = parsed + timedelta(milliseconds=total)
        # metadata is aliased from the source observation (copied via _COPIED_FIELDS), so this
        # writes time_source back onto the source dict too — intentional and inert (time_source is
        # never read; the write is idempotent), matching the module's other in-place patterns.
        body["endTime"] = end.isoformat().replace("+00:00", "Z")
        body.setdefault("metadata", {})[_TIME_SOURCE_KEY] = _TIME_SOURCE_LAGGING
    return copies


def _hook_event_exclude(events: list[IngestEvent]) -> set[str]:
    """Return the ids of ``hook_execution_complete`` nodes to drop from duration attribution (#157).

    Their stamped width (:func:`_stamp_hook_endtimes`) duplicates the ``.sh`` guard durations
    already booked in the ``hook`` bucket, so they must contribute nothing to ``rollup.duration``.
    Name-based, so it resolves the same ids in either view's id namespace.
    """
    return {
        event["body"]["id"]
        for event in events
        if event["type"] != "trace-create" and _is_hook_event(event["body"])
    }


def _guard_warns(body: Observation) -> bool:
    """Whether a guard span is failure-worthy: a deny/ask/block decision or a non-success status."""
    decision = _attr(body, "decision")
    status = _attr(body, "status")
    return decision in _GUARD_WARN_DECISIONS or (status is not None and status != _STATUS_SUCCESS)


def _level_for(body: Observation) -> str | None:
    """Return the failure level (:data:`_LEVEL_ERROR` / :data:`_LEVEL_WARNING`) for a node, or None.

    ERROR for a tool whose folded metadata shows ``success is False`` or an ``error``; WARNING for a
    failure-worthy guard span (:func:`_guard_warns`), a synthesized blocked-tool node, or a
    ``hook_execution_complete`` with ``num_blocking > 0``. Each node matches at most one rule, so
    the ERROR > WARNING precedence needs no explicit tie-break. The guards GROUP's level is set at
    build time (:func:`_apply_guard_groups`) from its raw members, not here.
    """
    if _is_tool_span(body):
        metadata = body.get("metadata") or {}
        if metadata.get("success") is False or metadata.get("error"):
            return _LEVEL_ERROR
        return None
    if _is_blocked_tool(body):
        return _LEVEL_WARNING
    if _is_hook(body):
        return _LEVEL_WARNING if _guard_warns(body) else None
    if _is_hook_event(body):
        num = _attr(body, _NUM_BLOCKING_KEY)
        return _LEVEL_WARNING if isinstance(num, (int, float)) and num > 0 else None
    return None


def _apply_levels(copies: list[IngestEvent]) -> list[IngestEvent]:
    """Stamp WARNING/ERROR failure levels onto the assembled nodes in place (#157, :func:`_level_for`)."""
    for event in copies:
        level = _level_for(event["body"])
        if level:
            event["body"]["level"] = level
    return copies


def _guards_total_ms(body: Observation) -> int:
    """Return a guards group's summed raw guard duration from its metadata (0 if malformed)."""
    total = (body.get("metadata") or {}).get("total_ms")
    return total if isinstance(total, int) else 0


def _build_tool_index(traces: list[TraceObservations]) -> dict[str, str]:
    """Map each tool-call id to the copy id of the tool observation that owns it.

    A tool's satellites (gate hooks, tool-scoped audit events, and the three folding sub-spans)
    are skipped so none indexes its own ``tool_use_id``; the surviving owner is the tool
    observation, which is the re-parent target for the satellites and the fold target for the
    sub-spans.

    Args:
        traces: The source traces paired with their observations.

    Returns:
        A mapping of ``tool_use_id`` to the assembled-trace copy id of its tool.
    """
    index: dict[str, str] = {}
    for orig_trace_id, observations in traces:
        for observation in observations:
            if _joins_under_tool(observation) or _is_fold_subspan(observation):
                continue
            tuid = _tool_use_id(observation)
            if tuid:
                index[tuid] = _copy_id(orig_trace_id, observation["id"])
    return index


def _build_request_index(traces: list[TraceObservations]) -> dict[str, str]:
    """Map each LLM ``request_id`` to the copy id of its ``llm_request`` generation (#104).

    Only ``GENERATION`` observations (the native ``llm_request`` spans) own the index; the
    ``api_error`` / ``api_refusal`` audit events that share the id are skipped so they remain the
    re-parent satellites, never the target.

    Args:
        traces: The source traces paired with their observations.

    Returns:
        A mapping of ``request_id`` to the assembled-trace copy id of its ``llm_request``.
    """
    index: dict[str, str] = {}
    for orig_trace_id, observations in traces:
        for observation in observations:
            if (observation.get("type") or "") != "GENERATION":
                continue
            rid = _request_id(observation)
            if rid:
                index[rid] = _copy_id(orig_trace_id, observation["id"])
    return index


class InteractionIndex(NamedTuple):
    """Enclosing-turn lookup for re-homing an unmatched-tool satellite (#110 AC1).

    ``by_prompt`` maps each turn's ``prompt.id`` to its interaction copy id (the primary,
    causal join). ``windows`` lists ``(start, end, copy_id)`` for every interaction that has
    both bounds, sorted ascending, for the ``[start,end]`` containment fallback used when a
    satellite carries no ``prompt.id``.
    """

    by_prompt: dict[str, str]
    windows: list[tuple[str, str, str]]


def _build_interaction_index(traces: list[TraceObservations]) -> InteractionIndex:
    """Index every ``claude_code.interaction`` by ``prompt.id`` and by its time window.

    The first interaction seen for a ``prompt.id`` wins (a resume shares the original turn's
    id; either copy is the same turn). Only interactions carrying both bounds contribute a
    window, kept sorted so :func:`_enclosing_turn` picks the innermost on an overlap.

    Args:
        traces: Each source trace paired with all of its observations.

    Returns:
        The prompt-id map and sorted window list (see :class:`InteractionIndex`).
    """
    by_prompt: dict[str, str] = {}
    windows: list[tuple[str, str, str]] = []
    for orig_trace_id, observations in traces:
        for observation in observations:
            if not _is_interaction(observation):
                continue
            copy = _copy_id(orig_trace_id, observation["id"])
            pid = _prompt_id(observation)
            if pid:
                by_prompt.setdefault(pid, copy)
            start, end = observation.get("startTime"), observation.get("endTime")
            if start and end:
                windows.append((start, end, copy))
    windows.sort()
    return InteractionIndex(by_prompt, windows)


def _enclosing_turn(observation: Observation, index: InteractionIndex) -> str | None:
    """Return the copy id of the interaction enclosing ``observation``, or None (#110 AC1).

    Resolves by ``prompt.id`` first — the reliable causal join. Falls back to ``[start,end]``
    containment (innermost turn wins) only for an event whose ``startTime`` is its true event
    time; a lagging-timestamp audit instant (:func:`_is_audit_instant`, on the batched logs
    signal) is never window-placed, so it resolves by ``prompt.id`` alone and otherwise stays
    at the root.
    """
    pid = _prompt_id(observation)
    if pid and pid in index.by_prompt:
        return index.by_prompt[pid]
    if _is_audit_instant(observation):
        return None
    start = observation.get("startTime")
    if not start:
        return None
    chosen: tuple[str, str, str] | None = None
    for window in index.windows:
        win_start, win_end, _copy = window
        if not win_start <= start <= win_end:
            continue
        # Innermost wins: the latest-starting containing turn, and on an equal start the one
        # that ends earliest (the narrower, more-nested window).
        if (
            chosen is None
            or win_start > chosen[0]
            or (win_start == chosen[0] and win_end < chosen[1])
        ):
            chosen = window
    return chosen[2] if chosen else None


def _blocked_tool_name(satellites: list[Observation]) -> str:
    """Return the blocked tool's name: a ``tool_name`` attr, else a ``hook_name`` suffix, else unknown."""
    for satellite in satellites:
        name = _attr(satellite, *_TOOL_NAME_KEYS)
        if name:
            return str(name)
    for satellite in satellites:
        hook_name = _attr(satellite, _HOOK_NAME_KEY)
        if hook_name and ":" in str(hook_name):
            return str(hook_name).split(":", 1)[1]
    return _BLOCKED_TOOL_UNKNOWN


def _synthesize_blocked_tools(
    traces: list[TraceObservations],
    *,
    tool_index: dict[str, str],
    interaction_index: InteractionIndex,
    trace_id: str,
    root_id: str,
) -> tuple[list[IngestEvent], dict[str, str]]:
    """Synthesize a ``blocked-tool:<Name>`` node per orphaned tool-call id (#157).

    An orphaned id is one carried by a satellite (:func:`_joins_under_tool`) but owned by no
    ``tool:`` span (:func:`_build_tool_index`). Each becomes one WARNING ``blocked-tool:`` node
    parented to its enclosing turn (:func:`_enclosing_turn`, else the root), spanning its
    satellites' time envelope and carrying no usageDetails/model. The returned index maps each
    orphaned id to its node so the copy pass and fold re-home the satellites onto it.

    Args:
        traces: The source traces paired with their observations.
        tool_index: Real-tool ownership map (an id present here is NOT orphaned).
        interaction_index: Enclosing-turn lookup for parenting the synthesized node.
        trace_id: The assembled trace id every synthesized node references.
        root_id: The synthetic root id (parent when no enclosing turn resolves).

    Returns:
        ``(events, index)``: the synthesized ``span-create`` events and the orphaned-id → node-id map.
    """
    by_tuid: dict[str, list[Observation]] = {}
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if not _joins_under_tool(observation):
                continue
            tuid = _tool_use_id(observation)
            if tuid and tuid not in tool_index:
                by_tuid.setdefault(tuid, []).append(observation)
    events: list[IngestEvent] = []
    index: dict[str, str] = {}
    for tuid, satellites in by_tuid.items():
        node_id = _blocked_tool_id(tuid)
        parent = next(
            (turn for s in satellites if (turn := _enclosing_turn(s, interaction_index))), root_id
        )
        start, end = _obs_envelope(satellites)
        body: dict[str, Any] = {
            "id": node_id,
            "traceId": trace_id,
            "parentObservationId": parent,
            "name": _BLOCKED_TOOL_NAME_PREFIX + _blocked_tool_name(satellites),
            "startTime": start or _INGEST_TIMESTAMP,
            "endTime": end,
            "metadata": {"synthesized": True, "tool_use_id": tuid},
        }
        # level WARNING is stamped centrally by _apply_levels (#157), like every other node.
        events.append(
            {
                "id": node_id,
                "type": "span-create",
                "timestamp": start or _INGEST_TIMESTAMP,
                "body": body,
            }
        )
        index[tuid] = node_id
    return events, index


class SkillCandidate(NamedTuple):
    """One ``tool:Skill`` span a ``skill_activated`` event may belong to (#110 AC2).

    ``skill_name`` is the activated skill read from the tool's transcript input (None when the
    content is unavailable); ``start`` is the span's true start, used as the nearest-timestamp
    tiebreak when a turn ran the same skill more than once.
    """

    start: str | None
    copy_id: str
    skill_name: str | None


def _activated_skill_name(tuid: str | None, tool_content: dict[str, ToolContent]) -> str | None:
    """Return the skill named by a ``tool:Skill`` span's transcript input, or None."""
    content = tool_content.get(tuid or "")
    if content is None or not isinstance(content.input, dict):
        return None
    value = content.input.get(_SKILL_INPUT_KEY)
    return str(value) if value else None


def _enclosing_prompt_id(observation: Observation, by_id: dict[str, Observation]) -> str | None:
    """Return the ``prompt.id`` of the nearest ancestor interaction within a trace, or None.

    Walks ``parentObservationId`` up the trace-local node map until an ancestor carries a
    ``prompt.id`` (normally the enclosing ``claude_code.interaction``); a tool span rarely
    carries its own, so this recovers the turn id a ``tool:Skill`` belongs to.
    """
    seen: set[str] = set()
    parent = observation.get("parentObservationId")
    while parent and parent in by_id and parent not in seen:
        seen.add(parent)
        node = by_id[parent]
        pid = _prompt_id(node)
        if pid:
            return pid
        parent = node.get("parentObservationId")
    return None


def _build_skill_index(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, list[SkillCandidate]]:
    """Index every ``tool:Skill`` span by its turn's ``prompt.id`` (#110 AC2).

    A ``tool:Skill``'s ``prompt.id`` is its own when present, else the enclosing interaction's
    (:func:`_enclosing_prompt_id`); spans whose turn cannot be determined are skipped (the event
    then has no key to match and falls back to the enclosing turn / root).

    Args:
        traces: Each source trace paired with all of its observations.
        tool_content: Tool-call-id to :class:`ToolContent`, the source of each skill's name.

    Returns:
        A mapping of ``prompt.id`` to the candidate ``tool:Skill`` spans of that turn.
    """
    index: dict[str, list[SkillCandidate]] = {}
    for orig_trace_id, observations in traces:
        by_id = {observation["id"]: observation for observation in observations}
        for observation in observations:
            if (observation.get("name") or "") != _SKILL_TOOL_NAME:
                continue
            pid = _prompt_id(observation) or _enclosing_prompt_id(observation, by_id)
            if not pid:
                continue
            tuid = _tool_use_id(observation)
            candidate = SkillCandidate(
                observation.get("startTime"),
                _copy_id(orig_trace_id, observation["id"]),
                _activated_skill_name(tuid, tool_content),
            )
            index.setdefault(pid, []).append(candidate)
    return index


def _match_skill_tool(
    observation: Observation, skill_index: dict[str, list[SkillCandidate]]
) -> str | None:
    """Return the ``tool:Skill`` copy id a ``skill_activated`` event nests under, else None (#110).

    Matches within the event's turn (``prompt.id``): when the turn ran exactly one skill that is
    it; otherwise the candidates whose ``skill.name`` matches are preferred, and ties (the same
    skill activated twice in one turn) are broken by the nearest span start to the event's
    lagging time. None when the event has no ``prompt.id`` or its turn ran no skill.
    """
    pid = _prompt_id(observation)
    if not pid or pid not in skill_index:
        return None
    candidates = skill_index[pid]
    name = _skill_name(observation)
    pool = [candidate for candidate in candidates if candidate.skill_name == name] if name else []
    pool = pool or candidates
    if len(pool) == 1:
        return pool[0].copy_id
    return _nearest_skill(observation.get("startTime"), pool)


def _nearest_skill(event_start: str | None, pool: list[SkillCandidate]) -> str:
    """Return the copy id of the candidate whose start is nearest the event's time.

    Candidates with an unparseable or absent start sort last; on a full tie the first in fetch
    order wins, so the choice is deterministic across reruns.
    """
    event_ts = _parse_ts(event_start or "")

    def distance(candidate: SkillCandidate) -> tuple[int, float]:
        cand_ts = _parse_ts(candidate.start or "")
        if event_ts is None or cand_ts is None:
            return (1, 0.0)
        try:
            return (0, abs((cand_ts - event_ts).total_seconds()))
        except TypeError:  # one side tz-aware, the other naive — sort last, never crash
            return (1, 0.0)

    return min(pool, key=distance).copy_id


def _resolve_parent(
    observation: Observation,
    *,
    orig_trace_id: str,
    root_id: str,
    tool_index: dict[str, str],
    request_index: dict[str, str],
    interaction_index: InteractionIndex,
    skill_index: dict[str, list[SkillCandidate]],
) -> str:
    """Resolve the assembled-trace parent id for one source observation.

    Args:
        observation: The source observation.
        orig_trace_id: The id of the trace the observation came from.
        root_id: The synthetic root span id (the single collapsed root).
        tool_index: Tool-call-id to tool-copy-id map from :func:`_build_tool_index`.
        request_index: Request-id to llm_request-copy-id map from :func:`_build_request_index`.
        interaction_index: Enclosing-turn lookup from :func:`_build_interaction_index`.
        skill_index: Prompt-id to ``tool:Skill`` candidates from :func:`_build_skill_index`.

    Returns:
        The copy id of the intra-trace parent, the matching tool / llm_request / ``tool:Skill``,
        the enclosing turn (for an unmatched-tool satellite), or the synthetic root.
    """
    parent = observation.get("parentObservationId")
    if parent:
        return _copy_id(orig_trace_id, parent)
    if _joins_under_tool(observation):
        tuid = _tool_use_id(observation)
        if tuid and tuid in tool_index:
            return tool_index[tuid]
        # #110 AC1 / #157: a satellite naming a tool that produced no span (denied/cancelled) is
        # normally already resolved above — #157 synthesizes a blocked-tool node for every such
        # orphaned tuid and augments tool_index, so the branch above catches it. This is a
        # defensive fallback (re-home to the enclosing turn) kept for depth in case a satellite's
        # tuid ever escapes synthesis; a hook naming no tool (SessionStart/Stop) has no tuid and
        # still falls through to the root.
        if tuid:
            turn = _enclosing_turn(observation, interaction_index)
            if turn is not None:
                return turn
    if _is_skill_activated(observation):
        # #110 AC2: nest under the tool:Skill that activated it, else its enclosing turn.
        skill_tool = _match_skill_tool(observation, skill_index)
        if skill_tool is not None:
            return skill_tool
        turn = _enclosing_turn(observation, interaction_index)
        if turn is not None:
            return turn
    if _is_request_audit_event(observation):
        rid = _request_id(observation)
        if rid and rid in request_index:
            return request_index[rid]
    return root_id


def _tool_span_ids(traces: list[TraceObservations]) -> set[str]:
    """Collect the tool-call ids of every graftable ``tool:`` / ``sub-agent:`` span."""
    ids: set[str] = set()
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if not _is_graftable_span(observation):
                continue
            tuid = _tool_use_id(observation)
            if tuid:
                ids.add(tuid)
    return ids


def _capped(value: object) -> object:
    """Return ``value`` unchanged, or a truncated string when its serialized form is large.

    Small structured values are passed through so Langfuse renders them richly; only content
    whose serialized text exceeds :data:`_MAX_CONTENT_CHARS` (e.g. a large file Read) is
    flattened to a truncated string with a marker.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) > _MAX_CONTENT_CHARS:
        return text[:_MAX_CONTENT_CHARS] + _TRUNCATION_MARKER
    return value


def _tool_additions(
    observation: Observation, tool_content: dict[str, ToolContent]
) -> dict[str, Any]:
    """Return the input/output to graft onto a tool span's create body, empty when none.

    A visible ``tool:`` span or a ``sub-agent:<type>`` container with a matching transcript
    entry contributes, and only for a field the source span does not already carry — so
    collector-provided content (Bash's ``input``) is never overwritten and non-graftable spans
    are untouched. Oversized values are truncated by :func:`_capped`.

    Args:
        observation: The source observation being copied.
        tool_content: Tool-call-id to :class:`ToolContent` from :func:`scan_transcripts`.

    Returns:
        A mapping with ``input`` and/or ``output`` to merge into the body, or ``{}``.
    """
    if not _is_graftable_span(observation):
        return {}
    content = tool_content.get(_tool_use_id(observation) or "")
    if content is None:
        return {}
    additions: dict[str, Any] = {}
    if not observation.get("input") and content.input is not None:
        additions["input"] = _capped(content.input)
    if not observation.get("output") and content.output is not None:
        additions["output"] = _capped(content.output)
    return additions


def _tool_result_size(observation: Observation, tool_content: dict[str, ToolContent]) -> int | None:
    """Return the byte size of a tool span's reconstructed ``tool_result``, or None (#101).

    Measures the RAW transcript output (before :func:`_capped` truncates the display copy), so a
    large tool result reports its true size. None for a non-tool span or one with no reconstructed
    output, so the caller emits no score for it.
    """
    # UPGRADE: sizing stays tool:-only, so a sub-agent's grafted output is not sized for the #101
    # bloat chart — widen to _is_graftable_span if sub-agent report bloat needs charting (it would
    # add a tool_result_size score per sub-agent, a cardinality change worth its own test).
    if not _is_tool_span(observation):
        return None
    content = tool_content.get(_tool_use_id(observation) or "")
    if content is None or content.output is None:
        return None
    # UPGRADE: structured output is sized by its json.dumps envelope (keys/braces/quotes), a
    # slight over-count vs the rendered text — switch to summing the content blocks' text if the
    # bloat chart ever needs to compare structured and plain-string results apples-to-apples.
    text = (
        content.output
        if isinstance(content.output, str)
        else json.dumps(content.output, ensure_ascii=False)
    )
    return len(text.encode("utf-8"))


def _copy_event(
    observation: Observation,
    *,
    orig_trace_id: str,
    trace_id: str,
    parent_id: str,
    tool_content: dict[str, ToolContent],
) -> IngestEvent:
    """Shape one ingestion event copying a source observation into the assembled trace.

    The type tracks the source: a ``GENERATION`` becomes a ``generation-create``, anything
    else a ``span-create``. ``usageDetails`` and ``model`` are re-passed so Langfuse
    recomputes ``costDetails`` identically; an explicit ``costDetails`` is forwarded too.
    For a graftable (``tool:`` / ``sub-agent:``) span, transcript-sourced ``input``/``output`` is
    grafted into the create body (see :func:`_tool_additions`) so the fresh observation carries
    content the native span lacked, set in the same create event that fixes its name and type.

    Args:
        observation: The source observation to copy.
        orig_trace_id: The id of the trace the observation came from.
        trace_id: The assembled trace id every copy references.
        parent_id: The resolved parent id for this copy.
        tool_content: Tool-call-id to :class:`ToolContent` from :func:`scan_transcripts`.

    Returns:
        A Langfuse ingestion batch event recreating the observation.
    """
    new_id = _copy_id(orig_trace_id, observation["id"])
    obs_type = observation.get("type") or "SPAN"
    event_type = "generation-create" if obs_type == "GENERATION" else "span-create"
    start = observation.get("startTime") or _INGEST_TIMESTAMP
    body: dict[str, Any] = {
        "id": new_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        "name": observation.get("name"),
        "startTime": observation.get("startTime"),
        "endTime": observation.get("endTime"),
    }
    for field in _COPIED_FIELDS:
        if observation.get(field) is not None:
            body[field] = observation[field]
    body.update(_tool_additions(observation, tool_content))
    size = _tool_result_size(observation, tool_content)
    if size is not None:
        body.setdefault("metadata", {})["tool_result_size"] = size
    return {"id": new_id, "type": event_type, "timestamp": start, "body": body}


_DURATION_CLASSES: tuple[str, ...] = (
    "llm_request",
    "tool",
    "hook",
    "script",
    "step",
    "wait",
    "turn",
    "self",
    "other",
)


def _duration_class(event: IngestEvent) -> str:
    """Return the duration-attribution class of one assembled node (#128).

    The buckets mirror the issue's split — LLM calls, tool calls, hooks, scripts, cycle
    steps, human/gate wait, turns — plus ``other`` for anything unclassified. Human wait
    covers the gate script (``spoke-ready --gate``) and an UNMATCHED blocked-on-user span
    (its tool was denied/cancelled, so the #100 fold never absorbed it); the tool-side
    share of ``wait`` (folded ``blocked_on_user_ms``) is carved out in
    :func:`_duration_rollup`. ``script:`` outranks the ``.sh`` hook suffix so a
    script-labelled span never drifts into the hook bucket.
    """
    body = event["body"]
    if event["type"] == "generation-create":
        return "llm_request"
    name = body.get("name") or ""
    if _is_gate_observation(body) or name == _FOLD_BLOCKED_NAME or name.startswith(_WAIT_PREFIX):
        return "wait"
    if name.startswith("step:") or name in (_PRE_STEP_NAME, _POST_STEP_NAME):
        return "step"
    if name == _INTERACTION_NAME:
        return "turn"
    if name.startswith("script:") or _attr(body, "workflow.kind") == "script":
        return "script"
    if _is_guards_group(body) or _is_hook(body):
        return "hook"
    if _is_tool_span(body):
        return "tool"
    return "other"


_Interval = tuple[datetime, datetime]


def _interval_ms(interval: _Interval | None) -> int:
    """An interval's length in ms (0 for None)."""
    if interval is None:
        return 0
    return int((interval[1] - interval[0]).total_seconds() * 1000)


def _union_ms(intervals: list[_Interval | None], clip: _Interval) -> int:
    """Total ms covered by the union of ``intervals``, clipped to ``clip``.

    Overlapping child spans (parallel tool calls, concurrent sub-agents) are counted
    once, so a parent's uncovered gap is never over-subtracted by concurrency.
    """
    lo, hi = clip
    clipped = sorted(
        (max(start, lo), min(end, hi))
        for start, end in filter(None, intervals)
        if max(start, lo) < min(end, hi)
    )
    total = 0
    cursor: datetime | None = None
    for start, end in clipped:
        if cursor is None or start > cursor:
            total += _interval_ms((start, end))
            cursor = end
        elif end > cursor:
            total += _interval_ms((cursor, end))
            cursor = end
    return total


def _effective_intervals(
    bodies: list[Observation],
    children: dict[str | None, list[str]],
    exclude: frozenset[str] | set[str],
) -> dict[str, _Interval | None]:
    """Each node's attribution interval: its own parsed start->end, else its subtree span.

    The subtree-span fallback lets an untimed container (the synthetic root, an interaction
    the collector closed without a valid endTime) still cover its children, so the gap
    between them surfaces as that container's own time instead of vanishing. Timestamps are
    PARSED (never string-compared — mixed ``Z``/``+hh:mm`` forms misorder lexicographically)
    and excluded nodes contribute nothing.
    """
    times = {
        body["id"]: (_parse_utc(body.get("startTime")), _parse_utc(body.get("endTime")))
        for body in bodies
    }
    memo: dict[str, _Interval | None] = {}

    def visit(node_id: str) -> _Interval | None:
        if node_id in memo:
            return memo[node_id]
        kid_intervals = [visit(kid) for kid in children.get(node_id, []) if kid not in exclude]
        start, end = times[node_id]
        if start is not None and end is not None and end >= start:
            interval: _Interval | None = (start, end)
        else:
            starts = [i[0] for i in kid_intervals if i] + ([start] if start else [])
            ends = [i[1] for i in kid_intervals if i] + ([end] if end else [])
            interval = (min(starts), max(ends)) if starts and ends else None
            if interval and interval[1] < interval[0]:
                interval = None
        memo[node_id] = interval
        return interval

    for body in bodies:
        visit(body["id"])
    return memo


def _blocked_ms(body: Observation) -> int:
    """The folded ``blocked_on_user_ms`` on a tool node, 0 when absent or non-numeric."""
    raw = (body.get("metadata") or {}).get("blocked_on_user_ms")
    return max(0, int(raw)) if isinstance(raw, (int, float)) else 0


def _duration_rollup(
    root_id: str,
    *,
    by_id: dict[str, Observation],
    children: dict[str | None, list[str]],
    class_of: dict[str, str],
    intervals: dict[str, _Interval | None],
    exclude: frozenset[str] | set[str],
) -> dict[str, Any]:
    """The ``rollup.duration`` object for one container: subtree wall-clock split by class (#128).

    ``total_ms`` is the observed subtree wall-clock. Each subtree node books its exclusive
    time — its interval length minus the union of its children's intervals (clipped to its
    own) — into its class bucket; the container being rolled up books its own uncovered gap
    under ``self``, and a tool's folded ``blocked_on_user_ms`` (#100) is carved out of its
    exclusive time into ``wait``. On serial (non-overlapping) spans the components sum
    exactly to ``total_ms``; CONCURRENT siblings each book their full span time, so class
    buckets are span-time and may sum past the wall-clock (like CPU-time vs wall-time) while
    gap buckets (``self``/``turn``/``step``) stay true — union-based subtraction never
    erases them. Nodes in ``exclude`` (View B turn-markers, whose spans overlap their
    re-homed former children) contribute nothing and are not subtracted.
    """
    components = dict.fromkeys(_DURATION_CLASSES, 0)

    def visit(node_id: str) -> None:
        kids = [kid for kid in children.get(node_id, []) if kid not in exclude]
        own = intervals.get(node_id)
        # A guards-group child covers only its summed RAW guard time (``total_ms``), never its
        # min…max envelope (#157): the envelope brackets the tool's own execution, so unioning it
        # would erase that execution from the tool's exclusive time. Plain children union by
        # interval as before.
        # UPGRADE: guard_cover is summed as a scalar, so when a guard's real interval overlaps a
        # PLAIN sibling (a mid-turn Notification/Stop hook over its turn, a gate over an
        # llm_request under a sub-agent) that overlap is counted in both terms and the container's
        # own gap bucket is under-reported by the overlap — bounded (guards are short), never
        # inflating, and sum(components)==total still holds. Switch to unioning the group's real
        # member intervals into the parent if per-bucket gap exactness ever matters.
        guard_cover = sum(
            _guards_total_ms(by_id[kid]) for kid in kids if _is_guards_group(by_id.get(kid))
        )
        plain = [kid for kid in kids if not _is_guards_group(by_id.get(kid))]
        union = _union_ms([intervals.get(kid) for kid in plain], clip=own) if own else 0
        covered = min(_interval_ms(own), union + guard_cover)
        exclusive = max(0, _interval_ms(own) - covered)
        bucket = "self" if node_id == root_id else class_of.get(node_id, "other")
        if bucket == "tool":
            wait = min(exclusive, _blocked_ms(by_id[node_id]))
            components["wait"] += wait
            components["tool"] += exclusive - wait
        elif _is_guards_group(by_id.get(node_id)):
            # The group books its RAW guard time minus the slice its surviving children already
            # book, so root's ``hook`` bucket is real guard cost and dropping no-op guards leaves
            # the components unchanged.
            kept = sum(_interval_ms(intervals.get(kid)) for kid in kids)
            components["hook"] += max(0, _guards_total_ms(by_id[node_id]) - kept)
        else:
            components[bucket] += exclusive
        for kid in kids:
            visit(kid)

    visit(root_id)
    return {"total_ms": _interval_ms(intervals.get(root_id)), "components": components}


def _container_rollup(
    node_id: str,
    *,
    by_id: dict[str, Observation],
    children: dict[str | None, list[str]],
    class_of: dict[str, str],
    intervals: dict[str, _Interval | None],
    exclude: frozenset[str] | set[str],
) -> dict[str, Any]:
    """One container's full ``metadata.rollup``: the shared token sum plus the duration split.

    The single assembly point for both writers — the container stamping in
    :func:`_apply_container_rollups` and the View B turn-marker stamping in
    :func:`_apply_cycle_axis` — so the two rollup shapes cannot drift.
    """
    rollup: dict[str, Any] = dict(rollup_metadata(subtree_totals(node_id, by_id, children)))
    rollup["duration"] = _duration_rollup(
        node_id,
        by_id=by_id,
        children=children,
        class_of=class_of,
        intervals=intervals,
        exclude=exclude,
    )
    return rollup


def _apply_container_rollups(
    events: list[IngestEvent], *, duration_exclude: frozenset[str] | set[str] = frozenset()
) -> None:
    """Set ``metadata.rollup`` on every container node of the assembled tree, in place.

    A container is any node with children once the tree is re-parented (the synthetic
    root, each ``interaction`` / ``tool:Agent`` / sub-agent). Its rollup is the subtree
    sum of the four usage components over itself and all descendants, computed from the
    create-body shapes (``id`` / ``parentObservationId`` / ``usageDetails``) — the same
    sum logic as :mod:`telemetry.langfuse_rollup`, but written into the create body
    rather than patched — plus the ``duration`` wall-clock split (#128,
    :func:`_duration_rollup`). Leaves (tools, single generations) are left untouched.

    Args:
        events: The assembled ingestion events; only ``*-create`` span/generation bodies
            participate (the ``trace-create`` is skipped). Mutated in place.
        duration_exclude: Node ids that must not contribute duration (View B's flattened
            turn-markers — their spans overlap their re-homed former children).
    """
    nodes = [event for event in events if event["type"] != "trace-create"]
    bodies = [event["body"] for event in nodes]
    by_id, children = build_tree(bodies)
    class_of = {event["body"]["id"]: _duration_class(event) for event in nodes}
    intervals = _effective_intervals(bodies, children, duration_exclude)
    for body in bodies:
        if not children.get(body["id"]):
            continue  # only containers (those with children) carry a rollup
        body.setdefault("metadata", {})["rollup"] = _container_rollup(
            body["id"],
            by_id=by_id,
            children=children,
            class_of=class_of,
            intervals=intervals,
            exclude=duration_exclude,
        )


def _strip_container_usage(copies: list[IngestEvent]) -> list[IngestEvent]:
    """Drop own usage from any span copy that has a generation descendant, in place (#161).

    A container span's own ``usageDetails`` would double-count against its generation children
    in both the subtree rollup and Langfuse's trace cost, so a span (never a generation) with a
    ``generation-create`` anywhere in its subtree must carry no usage of its own. Native
    sub-agent / interaction containers already ship empty usage; this is the future-proof guard
    should the collector ever stamp usage on a container.

    Args:
        copies: The assembled observation copies. Mutated in place and returned.

    Returns:
        The same list, with container usage/cost stripped.
    """
    bodies = [event["body"] for event in copies]
    _by_id, children = build_tree(bodies)
    generation_ids = {
        event["body"]["id"] for event in copies if event["type"] == "generation-create"
    }

    def _has_generation_descendant(node_id: str) -> bool:
        stack = list(children.get(node_id, []))
        while stack:
            current = stack.pop()
            if current in generation_ids:
                return True
            stack.extend(children.get(current, []))
        return False

    for event in copies:
        body = event["body"]
        if event["type"] == "generation-create":
            continue
        if not (body.get("usageDetails") or body.get("costDetails")):
            continue
        if _has_generation_descendant(body["id"]):
            body.pop("usageDetails", None)
            body.pop("costDetails", None)
    return copies


def _earliest_start(traces: list[TraceObservations]) -> str:
    """Return the earliest ISO ``startTime`` across all observations, or the fixed base."""
    starts = [
        observation["startTime"]
        for _, observations in traces
        for observation in observations
        if observation.get("startTime")
    ]
    return min(starts) if starts else _INGEST_TIMESTAMP


def _step_id(spoke_run_id: str, task_id: str, parent_id: str) -> str:
    """Return the deterministic id of one cycle-step node for a spoke.

    Keyed by the wrap's parent as well as the task, so a cross-turn task that produces a
    partial wrap in more than one interaction gets a distinct, stable id per interaction (#113).
    """
    digest = hashlib.sha1(f"{spoke_run_id}:step:{task_id}:{parent_id}".encode()).hexdigest()[:24]
    return _STEP_PREFIX + digest


def _task_id_from_create(output: object | None) -> str | None:
    """Extract the created task id from a ``TaskCreate`` result, or None.

    The transcript ``tool_result`` content is usually the ``"Task #N created…"`` string but can
    arrive as a list of content blocks (``[{"type": "text", "text": …}]``); both are searched by
    serializing non-string output, since the only ``#N`` in the result is the task id.
    """
    if output is None:
        return None
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    match = _TASK_ID_RE.search(text)
    return match.group(1) if match else None


def _ledger_subjects(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, str]:
    """Map each created task id to its ``TaskCreate`` subject (the step title)."""
    subjects: dict[str, str] = {}
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if (observation.get("name") or "") != "tool:TaskCreate":
                continue
            content = tool_content.get(_tool_use_id(observation) or "")
            if content is None or not isinstance(content.input, dict):
                continue
            subject = content.input.get("subject")
            task_id = _task_id_from_create(content.output)
            if subject and task_id:
                subjects[task_id] = str(subject)
    return subjects


def _ledger_bounds(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, dict[str, str]]:
    """Map each task id to its window bounds from ``TaskUpdate`` status transitions.

    ``start`` is the earliest ``in_progress`` update's ``startTime``; ``end`` is the latest
    ``completed`` update's ``endTime`` (resumes can re-mark a task, so the extremes win).
    """
    bounds: dict[str, dict[str, str]] = {}
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if (observation.get("name") or "") != "tool:TaskUpdate":
                continue
            content = tool_content.get(_tool_use_id(observation) or "")
            if content is None or not isinstance(content.input, dict):
                continue
            task_id = str(content.input.get("taskId") or "")
            if not task_id:
                continue
            entry = bounds.setdefault(task_id, {})
            status = content.input.get("status")
            start = observation.get("startTime")
            if status == "in_progress" and start:
                entry["start"] = start if "start" not in entry else min(entry["start"], start)
            if status == "completed":
                end = observation.get("endTime") or observation.get("startTime") or ""
                if end:
                    entry["end"] = end if "end" not in entry else max(entry["end"], end)
                entry["status"] = "completed"
    return bounds


def _latest_time(traces: list[TraceObservations]) -> str:
    """Return the latest ISO ``endTime``/``startTime`` across all observations, or the base."""
    times = [
        observation.get("endTime") or observation.get("startTime") or ""
        for _orig_trace_id, observations in traces
        for observation in observations
        if observation.get("endTime") or observation.get("startTime")
    ]
    return max(times) if times else _INGEST_TIMESTAMP


def build_step_windows(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> list[StepWindow]:
    """Derive the solo-cycle step windows from the todo ledger (#100).

    Each ``TaskCreate`` subject is a step; its ``in_progress`` → ``completed`` ``TaskUpdate``
    timestamps bound the window. A task created but never started (no ``in_progress``) has no
    window and is skipped. An in-flight task (no ``completed``) clamps its end to the spoke's
    last observation. Non-ledger spokes (no ``TaskCreate``) yield ``[]`` — no step grouping.

    Args:
        traces: The source traces paired with their observations.
        tool_content: Tool-call-id to :class:`ToolContent` (the ledger ops' input/output).

    Returns:
        The step windows in chronological start order.
    """
    subjects = _ledger_subjects(traces, tool_content)
    if not subjects:
        return []
    bounds = _ledger_bounds(traces, tool_content)
    fallback_end = _latest_time(traces)
    windows: list[StepWindow] = []
    for task_id, subject in subjects.items():
        bound = bounds.get(task_id)
        if not bound or "start" not in bound:
            continue
        windows.append(
            StepWindow(
                task_id=task_id,
                subject=subject,
                start=bound["start"],
                end=bound.get("end") or fallback_end,
                status=bound.get("status", "in_progress"),
            )
        )
    windows.sort(key=lambda window: window.start)
    return windows


def _containing_window(start: str, windows: list[StepWindow]) -> StepWindow | None:
    """Return the innermost step window containing ``start`` (latest start wins), or None.

    ``windows`` is ordered by start, so iterating and overwriting yields the latest-starting
    window that contains the timestamp — the innermost on an overlap.
    """
    chosen: StepWindow | None = None
    for window in windows:
        if window.start <= start <= window.end:
            chosen = window
    return chosen


def _step_node_name(window: StepWindow) -> str:
    """Return the shared ``step:<subject>`` node name for a ledger window (both views)."""
    return f"step:{window.subject}"


def _step_node_metadata(window: StepWindow) -> dict[str, Any]:
    """Return the shared step-node metadata for a ledger window (both views)."""
    return {
        "subject": window.subject,
        "status": window.status,
        "started": window.start,
        "completed": window.end,
    }


def _step_event(window: StepWindow, step_id: str, parent_id: str, trace_id: str) -> IngestEvent:
    """Shape one cycle-step span-create event nested under ``parent_id`` (the local wrap parent)."""
    return {
        "id": step_id,
        "type": "span-create",
        "timestamp": window.start,
        "body": {
            "id": step_id,
            "traceId": trace_id,
            "parentObservationId": parent_id,
            "name": _step_node_name(window),
            "startTime": window.start,
            "endTime": window.end,
            "metadata": _step_node_metadata(window),
        },
    }


def _collapse_startup_instants(
    copies: list[IngestEvent], root_event: IngestEvent
) -> list[IngestEvent]:
    """Demote session-startup instants to the root's ``session_init`` metadata, dropping nodes (#104).

    Each ``mcp_server_connection`` / ``plugin_loaded`` copy is summarised as ``{"name", …metadata}``
    onto the synthetic root's ``session_init`` list (preserving fetch order) and its node is removed,
    so a spoke's startup events read as one metadata field instead of N sibling spans placed by the
    lagging log timestamp. No ``session_init`` key is written when the spoke has no startup instants.

    Args:
        copies: The source observation copies; startup-instant copies are removed.
        root_event: The synthetic root event; its metadata is mutated in place.

    Returns:
        The copies with the startup-instant nodes removed.
    """
    init: list[dict[str, Any]] = []
    kept: list[IngestEvent] = []
    for event in copies:
        body = event["body"]
        if not _is_startup_instant(body):
            kept.append(event)
            continue
        init.append({"name": body.get("name"), **(body.get("metadata") or {})})
    if init:
        root_event["body"].setdefault("metadata", {})[_SESSION_INIT_FIELD] = init
    return kept


class _LedgerMarkers(NamedTuple):
    """Copy ids of one task's ledger markers (#113).

    ``create`` is the ``TaskCreate`` copy id(s); ``anchors`` is the ``in_progress`` / ``completed``
    ``TaskUpdate`` copy ids whose parent locates the local wrap. Both are absorbed under the step.
    """

    create: set[str]
    anchors: set[str]


def _ledger_marker_ids(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, _LedgerMarkers]:
    """Map each task id to the copy ids of its ``TaskCreate`` + ``in_progress``/``completed`` markers."""
    markers: dict[str, _LedgerMarkers] = {}
    for orig_trace_id, observations in traces:
        for observation in observations:
            name = observation.get("name") or ""
            content = tool_content.get(_tool_use_id(observation) or "")
            if content is None or not isinstance(content.input, dict):
                continue
            copy_id = _copy_id(orig_trace_id, observation["id"])
            if name == "tool:TaskCreate":
                task_id = _task_id_from_create(content.output)
                if task_id:
                    markers.setdefault(task_id, _LedgerMarkers(set(), set())).create.add(copy_id)
            elif name == "tool:TaskUpdate" and content.input.get("status") in (
                "in_progress",
                "completed",
            ):
                task_id = str(content.input.get("taskId") or "")
                if task_id:
                    markers.setdefault(task_id, _LedgerMarkers(set(), set())).anchors.add(copy_id)
    return markers


def _anchor_parents(
    windows: list[StepWindow],
    markers: dict[str, _LedgerMarkers],
    by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[StepWindow]], dict[str, set[str]]]:
    """Resolve each task's anchor parents (where its anchor markers sit) and group windows by them.

    Returns ``(windows_by_parent, parents_by_task)``: the first maps a parent copy id to the
    windows anchored under it (in start order, for the innermost-wins tie-break); the second maps
    a task id to the set of parents that hold its ``in_progress`` / ``completed`` markers.
    """
    windows_by_parent: dict[str, list[StepWindow]] = {}
    parents_by_task: dict[str, set[str]] = {}
    for window in windows:
        slots = markers.get(window.task_id)
        if slots is None:
            continue
        parents = {by_id[c]["parentObservationId"] for c in slots.anchors if c in by_id}
        parents_by_task[window.task_id] = parents
        for parent in parents:
            windows_by_parent.setdefault(parent, []).append(window)
    return windows_by_parent, parents_by_task


def _wrap_members(
    parent: str,
    window: StepWindow,
    children: dict[str, list[str]],
    by_id: dict[str, dict[str, Any]],
    windows_by_parent: dict[str, list[StepWindow]],
    all_marker_ids: set[str],
) -> list[str]:
    """Return ``parent``'s non-marker children whose innermost in-window step is ``window``.

    A child qualifies when it is not a ledger marker, not a lagging-timestamped audit instant
    (#104), and its ``startTime`` falls in ``window`` — with the innermost (latest-starting)
    window among those anchored at ``parent`` deciding ties on overlap.
    """
    members: list[str] = []
    for child in children.get(parent, []):
        if child in all_marker_ids:
            continue
        body = by_id[child]
        if _is_audit_instant(body):
            continue
        start = body.get("startTime")
        if not start or not (window.start <= start <= window.end):
            continue
        if _containing_window(start, windows_by_parent[parent]) is window:
            members.append(child)
    return members


def _task_marker_ids(task_id: str, markers: dict[str, _LedgerMarkers]) -> set[str]:
    """Return the task's ledger marker copy ids (``TaskCreate`` + the two ``TaskUpdate`` anchors)."""
    slots = markers[task_id]
    return slots.create | slots.anchors


def _apply_step_grouping(
    copies: list[IngestEvent],
    traces: list[TraceObservations],
    tool_content: dict[str, ToolContent],
    *,
    spoke_run_id: str,
    trace_id: str,
) -> list[IngestEvent]:
    """Wrap each ledger step's local same-parent siblings in a ``step:`` node, in place (#113).

    For every step window, a ``step:<subject>`` node is inserted INSIDE the interaction(s) that
    hold the task's ``in_progress`` / ``completed`` markers, wrapping the contiguous run of
    same-parent siblings whose ``startTime`` falls in the window and absorbing the task's three
    ledger markers (``TaskCreate`` + the two ``TaskUpdate`` anchors). The wrap never crosses an
    interaction boundary, so a cross-turn task yields one partial wrap per anchor-holding
    interaction; a wrap with zero non-marker siblings is suppressed (no empty steps). Audit
    instants are excluded — their lagging timestamp must never window-place them (#104). The
    ``claude_code.interaction`` subtrees and their W3C-TRACEPARENT nesting are otherwise left
    untouched; root-level satellites are no longer grouped here (View B is the cycle lens).

    Args:
        copies: The re-parented source observation copies; wrapped children are mutated in place.
        traces: The source traces (for ledger windows + marker copy ids).
        tool_content: Tool-call-id to :class:`ToolContent` (the ledger ops' input/output).
        spoke_run_id: The spoke run identifier (for deterministic step ids).
        trace_id: The assembled trace id every step node references.

    Returns:
        The new step span events (empty when the spoke has no ledger windows).
    """
    windows = build_step_windows(traces, tool_content)
    if not windows:
        return []
    by_id = {event["body"]["id"]: event["body"] for event in copies}
    markers = _ledger_marker_ids(traces, tool_content)
    all_marker_ids = {c for m in markers.values() for c in (m.create | m.anchors)}
    children: dict[str, list[str]] = {}
    for body in by_id.values():
        children.setdefault(body.get("parentObservationId"), []).append(body["id"])
    windows_by_parent, parents_by_task = _anchor_parents(windows, markers, by_id)
    step_events: list[IngestEvent] = []
    for window in windows:
        for parent in sorted(parents_by_task.get(window.task_id, set())):
            members = _wrap_members(
                parent, window, children, by_id, windows_by_parent, all_marker_ids
            )
            if not members:
                continue  # suppress a wrap with zero non-marker siblings
            step_id = _step_id(spoke_run_id, window.task_id, parent)
            step_events.append(_step_event(window, step_id, parent, trace_id))
            absorbed = members + [
                c
                for c in _task_marker_ids(window.task_id, markers)
                if c in by_id and by_id[c]["parentObservationId"] == parent
            ]
            for cid in absorbed:
                by_id[cid]["parentObservationId"] = step_id
    return step_events


def build_batch(
    traces: list[TraceObservations],
    spoke_run_id: str,
    tool_content: dict[str, ToolContent] | None = None,
    *,
    keep_noop_guards: bool = False,
    commits: list[dict[str, Any]] | None = None,
) -> list[IngestEvent]:
    """Assemble one nested trace from a spoke's source traces and their observations.

    Emits a ``trace-create``, a single synthetic root span, and one copy per source
    observation re-parented across the original trace boundaries (see module docstring).
    All ids derive from the spoke run id and the source ``(trace_id, observation_id)``
    pair, so the batch is idempotent. Visible ``tool:`` spans additionally have their
    transcript-sourced ``input``/``output`` grafted into the create body (see
    :func:`_tool_additions`).

    When the spoke ran a solo cycle, the todo ledger yields per-phase step nodes
    (:func:`build_step_windows`) inserted INSIDE each interaction, wrapping the contiguous run of
    same-parent siblings between the task's ``in_progress`` / ``completed`` markers
    (:func:`_apply_step_grouping`, View A); the rest of the ``claude_code.interaction`` subtrees
    are left untouched. A non-ledger spoke emits no step nodes.

    Args:
        traces: Each source trace paired with all of its observations, as fetched from
            Langfuse with full fields.
        spoke_run_id: The spoke run identifier (becomes the trace's ``sessionId``).
        tool_content: Tool-call-id to :class:`ToolContent` from :func:`scan_transcripts`;
            defaults to empty (no tool content filled).

    Returns:
        The ingestion events: a ``trace-create``, the synthetic root, the cycle-step nodes,
        then the copies.
    """
    tool_content = tool_content or {}
    trace_id = trace_id_for(spoke_run_id)
    root_id = root_id_for(spoke_run_id)
    base_ts = _earliest_start(traces)
    trace_event: IngestEvent = {
        "id": trace_id,
        "type": "trace-create",
        "timestamp": base_ts,
        "body": {
            "id": trace_id,
            "name": _TRACE_NAME_PREFIX + spoke_run_id,
            "sessionId": spoke_run_id,
            "timestamp": base_ts,
            "metadata": {"schema_rev": _SCHEMA_REV},
        },
    }
    root_event: IngestEvent = {
        "id": root_id,
        "type": "span-create",
        "timestamp": base_ts,
        "body": {
            "id": root_id,
            "traceId": trace_id,
            "name": _ROOT_NAME_PREFIX + spoke_run_id,
            "startTime": base_ts,
        },
    }
    copies = _assemble_copies(
        traces,
        trace_id=trace_id,
        root_id=root_id,
        tool_content=tool_content,
        root_event=root_event,
        keep_noop_guards=keep_noop_guards,
    )
    step_events = _apply_step_grouping(
        copies, traces, tool_content, spoke_run_id=spoke_run_id, trace_id=trace_id
    )
    events = [trace_event, root_event, *step_events, *copies]
    commit_events = _commit_events(
        commits or [],
        spoke_run_id=spoke_run_id,
        trace_id=trace_id,
        cycle=False,
        parent_for=lambda _at: root_id,
    )
    events.extend(commit_events)
    gate_park = _gate_park_event(
        traces, spoke_run_id=spoke_run_id, trace_id=trace_id, cycle=False, parent_id=root_id
    )
    if gate_park is not None:
        events.append(gate_park)
    # Commit instants are excluded from duration: an author time outside the captured span would
    # otherwise stretch the end-time-less root's subtree interval and inflate total_ms/self.
    commit_ids = {event["id"] for event in commit_events}
    _apply_container_rollups(events, duration_exclude=_hook_event_exclude(events) | commit_ids)
    return events


def _assemble_copies(
    traces: list[TraceObservations],
    *,
    trace_id: str,
    root_id: str,
    tool_content: dict[str, ToolContent],
    root_event: IngestEvent,
    keep_noop_guards: bool = False,
) -> list[IngestEvent]:
    """Build the re-parented, folded, startup-collapsed observation copies both views share.

    Re-parents every source observation across the original trace boundaries
    (:func:`_resolve_parent`), grafts transcript content into the create body
    (:func:`_copy_event`), synthesizes a ``blocked-tool:`` node per orphaned tool-call id so its
    satellites nest under it (:func:`_synthesize_blocked_tools`), folds the three 1:1 tool
    sub-spans (:func:`_fold_tool_subspans`), collapses each tool's / the session's ``.sh`` guard
    spans under a ``guards`` group and drops the no-op ones unless ``keep_noop_guards``
    (:func:`_apply_guard_groups`), stamps a lagging ``endTime`` onto ``hook_execution_complete``
    events (:func:`_stamp_hook_endtimes`), stamps WARNING/ERROR failure levels
    (:func:`_apply_levels`), demotes session-startup instants onto ``root_event``'s metadata
    (:func:`_collapse_startup_instants`), and strips own usage from any container that has a
    generation descendant (:func:`_strip_container_usage`). View A wraps these in local step
    nodes; View B re-homes them onto the cycle axis. ``root_event`` is the view's own synthetic
    root (its metadata is mutated in place).
    """
    tool_index = _build_tool_index(traces)
    request_index = _build_request_index(traces)
    interaction_index = _build_interaction_index(traces)
    skill_index = _build_skill_index(traces, tool_content)
    blocked_events, blocked_index = _synthesize_blocked_tools(
        traces,
        tool_index=tool_index,
        interaction_index=interaction_index,
        trace_id=trace_id,
        root_id=root_id,
    )
    # A blocked-tool node owns its orphaned id like a real tool, so its satellites join it in the
    # copy pass (via _resolve_parent) and its tool_decision folds onto it (via _fold_tool_subspans).
    tool_index = {**tool_index, **blocked_index}
    copies: list[IngestEvent] = []
    for orig_trace_id, observations in traces:
        for observation in observations:
            parent_id = _resolve_parent(
                observation,
                orig_trace_id=orig_trace_id,
                root_id=root_id,
                tool_index=tool_index,
                request_index=request_index,
                interaction_index=interaction_index,
                skill_index=skill_index,
            )
            copies.append(
                _copy_event(
                    observation,
                    orig_trace_id=orig_trace_id,
                    trace_id=trace_id,
                    parent_id=parent_id,
                    tool_content=tool_content,
                )
            )
    copies.extend(blocked_events)
    copies = _fold_tool_subspans(copies, traces, tool_index)
    copies = _apply_guard_groups(
        copies,
        trace_id=trace_id,
        root_id=root_id,
        tool_owner_ids=set(tool_index.values()),
        keep_noop_guards=keep_noop_guards,
    )
    copies = _stamp_hook_endtimes(copies)
    copies = _apply_levels(copies)
    copies = _collapse_startup_instants(copies, root_event)
    return _strip_container_usage(copies)


def _cycle_step_for(start: str, windows: list[StepWindow]) -> str:
    """Return the cycle-axis key for a span starting at ``start`` (``pre`` / ``post`` / a task id).

    Before the first window's start -> ``preStep``; after the last ``completed`` -> ``postStep``;
    otherwise the latest-starting window at or before ``start`` (so an inter-step gap span attaches
    to its preceding step). ``windows`` is non-empty and ordered by start.
    """
    if start < windows[0].start:
        return _PRE_STEP_KEY
    if start > max(window.end for window in windows):
        return _POST_STEP_KEY
    chosen = windows[0]
    for window in windows:
        if window.start <= start:
            chosen = window
    return chosen.task_id


def _cycle_axis_event(
    node_id: str,
    name: str,
    start: str,
    end: str,
    parent_id: str,
    trace_id: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> IngestEvent:
    """Shape one View B cycle-axis span-create event (preStep / step:N / postStep)."""
    body: dict[str, Any] = {
        "id": node_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        "name": name,
        "startTime": start,
        "endTime": end,
    }
    if metadata:
        body["metadata"] = metadata
    return {"id": node_id, "type": "span-create", "timestamp": start, "body": body}


def _cycle_step_ids(spoke_run_id: str, windows: list[StepWindow]) -> dict[str, str]:
    """Map each cycle-axis key (``pre`` / ``post`` / a task id) to its deterministic node id."""
    ids = {
        _PRE_STEP_KEY: _cycle_step_id(spoke_run_id, _PRE_STEP_KEY),
        _POST_STEP_KEY: _cycle_step_id(spoke_run_id, _POST_STEP_KEY),
    }
    for window in windows:
        ids[window.task_id] = _cycle_step_id(spoke_run_id, window.task_id)
    return ids


def _cycle_step_events(
    windows: list[StepWindow],
    step_id_for: dict[str, str],
    *,
    root_id: str,
    trace_id: str,
    base_ts: str,
    latest: str,
) -> list[IngestEvent]:
    """Build the preStep + step:N + postStep nodes that partition the cycle timeline under the root."""
    last_completed = max(window.end for window in windows)
    events = [
        _cycle_axis_event(
            step_id_for[_PRE_STEP_KEY], _PRE_STEP_NAME, base_ts, windows[0].start, root_id, trace_id
        )
    ]
    for window in windows:
        events.append(
            _cycle_axis_event(
                step_id_for[window.task_id],
                _step_node_name(window),
                window.start,
                window.end,
                root_id,
                trace_id,
                metadata=_step_node_metadata(window),
            )
        )
    events.append(
        _cycle_axis_event(
            step_id_for[_POST_STEP_KEY], _POST_STEP_NAME, last_completed, latest, root_id, trace_id
        )
    )
    return events


def _resolve_cycle_parent(
    body: dict[str, Any],
    parent_a: str,
    *,
    flattened: set[str],
    by_id_a: dict[str, dict[str, Any]],
    a_root_id: str,
    interaction_start: dict[str, str | None],
    windows: list[StepWindow],
    step_id_for: dict[str, str],
    root_id: str,
) -> str:
    """Resolve one copy's View B parent: ride a surviving span, else land on the cycle axis.

    A copy whose View A parent is a surviving span (a tool, llm_request, sub-agent, or nested
    interaction) keeps that parent (rides along by causal key). A copy left at the synthetic root
    or under a flattened top-level interaction lands on the cycle axis: a reliably-timestamped span
    by its own ``startTime``, an audit instant OR a synthesized ``blocked-tool`` node (whose own
    start is derived from lagging audit timestamps, #157) by its turn's start, falling back to
    ``preStep``. The flattened top-level interaction marker itself is just such a reliably-
    timestamped span (parent is the root), so it lands in the step window of its own start.
    """
    if parent_a != a_root_id and parent_a not in flattened and parent_a in by_id_a:
        return _cycle_copy_id(parent_a)
    if not windows:
        return root_id  # no ledger -> no cycle axis; copies hang flat under the cycle root
    if _is_audit_instant(body) or _is_blocked_tool(body) or not body.get("startTime"):
        anchor = interaction_start.get(parent_a)
        key = _cycle_step_for(anchor, windows) if anchor else _PRE_STEP_KEY
    else:
        key = _cycle_step_for(body["startTime"], windows)
    return step_id_for[key]


def _apply_cycle_axis(
    copies: list[IngestEvent],
    traces: list[TraceObservations],
    windows: list[StepWindow],
    *,
    spoke_run_id: str,
    a_root_id: str,
    root_id: str,
    trace_id: str,
    base_ts: str,
    latest: str,
) -> tuple[list[IngestEvent], list[IngestEvent], set[str]]:
    """Re-home the copies onto the cycle axis and build its nodes (#113 View B, #114 turn markers).

    Remaps every copy into the cycle id namespace, flattens the top-level interactions from
    containers to childless leaf markers (their children land on the axis by their own time; the
    marker itself lands in the step window of its own start), and parents each copy via
    :func:`_resolve_cycle_parent`. Each flattened marker is stamped with its turn's
    ``metadata.rollup`` — the token sum AND ``duration`` split of its pre-flatten View A subtree
    (#114, #128) — so the per-turn cost/latency total stays readable even though the marker is
    now childless. Returns ``(cycle_copies, step_events, marker_ids)``; the copies are mutated
    in place, and ``marker_ids`` (the flattened markers' cycle-namespace ids) must be excluded
    from the cycle-axis duration attribution — a marker's span overlaps its former children,
    now its step siblings.
    """
    interaction_start: dict[str, str | None] = {
        _copy_id(orig_trace_id, observation["id"]): observation.get("startTime")
        for orig_trace_id, observations in traces
        for observation in observations
        if _is_interaction(observation)
    }
    by_id_a = {event["body"]["id"]: event["body"] for event in copies}
    flattened = {
        iid
        for iid in interaction_start
        if iid in by_id_a and by_id_a[iid]["parentObservationId"] == a_root_id
    }
    step_id_for = _cycle_step_ids(spoke_run_id, windows)
    step_events = (
        _cycle_step_events(
            windows, step_id_for, root_id=root_id, trace_id=trace_id, base_ts=base_ts, latest=latest
        )
        if windows
        else []
    )
    # Each flattened marker becomes childless on the cycle axis, so neither _apply_container_rollups
    # (which skips childless nodes) nor Langfuse's descendant aggregation can recover the turn's
    # token/cost total once its generations re-home onto the steps. Precompute it from the still-
    # intact View A subtree and stamp it onto the marker so per-turn cost stays readable (#114). It
    # is kept as metadata.rollup, not usageDetails, so the marker's former children — now its step
    # siblings — are not double-counted in the step/root rollups (subtree_totals sums usageDetails).
    a_bodies = [event["body"] for event in copies]
    a_by_id, a_children = build_tree(a_bodies)
    a_class = {event["body"]["id"]: _duration_class(event) for event in copies}
    # Exclude the stamped hook events from the per-turn rollup too (#157) — their derived width
    # duplicates the guard time already in the ``hook`` bucket, exactly as for the container rollups.
    hook_exclude = _hook_event_exclude(copies)
    a_intervals = _effective_intervals(a_bodies, a_children, hook_exclude)
    turn_rollup = {
        iid: _container_rollup(
            iid,
            by_id=a_by_id,
            children=a_children,
            class_of=a_class,
            intervals=a_intervals,
            exclude=hook_exclude,
        )
        for iid in flattened
    }
    kept: list[IngestEvent] = []
    for event in copies:
        body = event["body"]
        orig_id = body["id"]
        parent_a = body["parentObservationId"]
        new_id = _cycle_copy_id(orig_id)
        body["id"] = new_id
        event["id"] = new_id
        body["traceId"] = trace_id
        body["parentObservationId"] = _resolve_cycle_parent(
            body,
            parent_a,
            flattened=flattened,
            by_id_a=by_id_a,
            a_root_id=a_root_id,
            interaction_start=interaction_start,
            windows=windows,
            step_id_for=step_id_for,
            root_id=root_id,
        )
        if orig_id in flattened:
            body.setdefault("metadata", {})["rollup"] = turn_rollup[orig_id]
        kept.append(event)
    return kept, step_events, {_cycle_copy_id(iid) for iid in flattened}


def build_cycle_batch(
    traces: list[TraceObservations],
    spoke_run_id: str,
    tool_content: dict[str, ToolContent] | None = None,
    *,
    keep_noop_guards: bool = False,
    commits: list[dict[str, Any]] | None = None,
) -> list[IngestEvent]:
    """Assemble the View B (steps -> work) ``spokecycle-<spoke>`` trace (#113).

    Built from the SAME observation copies as :func:`build_batch` (same rich input/output,
    usageDetails, costDetails, metadata) but re-homed onto a pure cycle axis: the top level is
    ``preStep`` + one ``step:<subject>`` per ledger task + ``postStep``, totally partitioning the
    timeline. Real spans (``tool:*`` / ``claude_code.llm_request``) are placed under the step
    whose window contains their ``startTime`` (gap -> preceding step); audit instants ride along
    under their tool / llm_request by causal key, never their lagging timestamp
    (:func:`_apply_cycle_axis`). Each top-level ``claude_code.interaction`` is flattened from a
    container to a childless leaf turn-marker, placed in the step window of its own ``startTime`` as
    a sibling of its former children and stamped with its turn's ``metadata.rollup`` aggregate
    (#114); nested sub-agent interactions keep riding their invoking tool. The copies live in a
    separate id namespace so they never collide with View A's in the local Langfuse store. A
    non-ledger spoke emits no cycle-axis nodes.

    View B carries the copies' assembly-time rich fields (``input``/``output``, ``usageDetails``,
    ``costDetails``, ``metadata``) and per-container ``rollup`` only. The heavier per-call
    enrichments :func:`main` layers onto View A — the cache decomposition, ``output_config.effort``
    tags, numeric scores, and the collapsed ``loaded-context`` node — stay single-emitted on View A
    by design (this is the cycle/phase lens, not a duplicate of the full nested view).

    Args:
        traces: Each source trace paired with all of its observations.
        spoke_run_id: The spoke run identifier (becomes the trace's ``sessionId``).
        tool_content: Tool-call-id to :class:`ToolContent` from :func:`scan_transcripts`;
            defaults to empty.

    Returns:
        The ingestion events: a ``trace-create``, the synthetic cycle root, the cycle-axis nodes,
        then the re-homed copies.
    """
    tool_content = tool_content or {}
    a_trace_id = trace_id_for(spoke_run_id)
    a_root_id = root_id_for(spoke_run_id)
    trace_id = cycle_trace_id_for(spoke_run_id)
    root_id = cycle_root_id_for(spoke_run_id)
    base_ts = _earliest_start(traces)
    trace_event: IngestEvent = {
        "id": trace_id,
        "type": "trace-create",
        "timestamp": base_ts,
        "body": {
            "id": trace_id,
            "name": _CYCLE_TRACE_NAME_PREFIX + spoke_run_id,
            "sessionId": spoke_run_id,
            "timestamp": base_ts,
            "metadata": {"schema_rev": _SCHEMA_REV},
        },
    }
    root_event: IngestEvent = {
        "id": root_id,
        "type": "span-create",
        "timestamp": base_ts,
        "body": {
            "id": root_id,
            "traceId": trace_id,
            "name": _CYCLE_ROOT_NAME_PREFIX + spoke_run_id,
            "startTime": base_ts,
        },
    }
    copies = _assemble_copies(
        traces,
        trace_id=a_trace_id,
        root_id=a_root_id,
        tool_content=tool_content,
        root_event=root_event,
        keep_noop_guards=keep_noop_guards,
    )
    windows = build_step_windows(traces, tool_content)
    copies, step_events, marker_ids = _apply_cycle_axis(
        copies,
        traces,
        windows,
        spoke_run_id=spoke_run_id,
        a_root_id=a_root_id,
        root_id=root_id,
        trace_id=trace_id,
        base_ts=base_ts,
        latest=_latest_time(traces),
    )
    events = [trace_event, root_event, *step_events, *copies]
    step_id_for = _cycle_step_ids(spoke_run_id, windows) if windows else {}

    def _cycle_commit_parent(authored_at: str) -> str:
        if not windows:
            return root_id
        return step_id_for.get(_cycle_step_for(authored_at, windows), root_id)

    commit_events = _commit_events(
        commits or [],
        spoke_run_id=spoke_run_id,
        trace_id=trace_id,
        cycle=True,
        parent_for=_cycle_commit_parent,
    )
    events.extend(commit_events)
    # The park node is a visible block on the axis, but it overlaps the step partition it falls in,
    # so it is EXCLUDED from View B's duration attribution (the axis already partitions the time).
    # Commit instants are likewise excluded so an out-of-window author time cannot stretch the root.
    duration_exclude = marker_ids | _hook_event_exclude(events) | {e["id"] for e in commit_events}
    gate_park = _gate_park_event(
        traces, spoke_run_id=spoke_run_id, trace_id=trace_id, cycle=True, parent_id=root_id
    )
    if gate_park is not None:
        events.append(gate_park)
        duration_exclude = duration_exclude | {gate_park["id"]}
    _apply_container_rollups(events, duration_exclude=duration_exclude)
    return events


def all_traces(spoke_run_id: str, get: GetFn) -> list[dict[str, Any]]:
    """Fetch every trace in a session, walking all pages.

    Args:
        spoke_run_id: The session id (``langfuse.session.id``) to fetch.
        get: Path-to-JSON fetcher (see :data:`telemetry.langfuse_rollup.GetFn`).

    Returns:
        The session's traces across all pages, in fetch order.
    """
    session = urllib.parse.quote(spoke_run_id)
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = get(f"/traces?sessionId={session}&limit={_PAGE_LIMIT}&page={page}")
        out.extend(resp.get("data") or [])
        total_pages = (resp.get("meta") or {}).get("totalPages") or 1
        if page >= total_pages:
            break
        page += 1
    return out


def _is_own_output(trace: dict[str, Any], spoke_run_id: str) -> bool:
    """Whether a fetched session trace is this synthesizer's own assembled output.

    Both assembled views — View A (``spoketree-``, ``spoke-tree:``) and View B
    (``spokecycle-``, ``spoke-cycle:``) — carry ``sessionId == spoke_run_id``, so on a
    re-run they reappear in the session listing; sourcing either would copy its spans again
    and multiply the tree (#156). Each is recognised by its deterministic id or, defensively
    for older ids, its ``spoke-tree:`` / ``spoke-cycle:`` name prefix.

    Args:
        trace: A trace dict as returned by the Langfuse traces endpoint.
        spoke_run_id: The spoke run id whose assembled views must be excluded.

    Returns:
        True when the trace is the synthesizer's own output and must be excluded.
    """
    if trace.get("id") in {trace_id_for(spoke_run_id), cycle_trace_id_for(spoke_run_id)}:
        return True
    name = trace.get("name") or ""
    return name.startswith((_TRACE_NAME_PREFIX, _CYCLE_TRACE_NAME_PREFIX))


def fetch_session(spoke_run_id: str, get: GetFn) -> list[TraceObservations]:
    """Fetch every native trace in a session paired with all of its observations.

    The synthesizer's own prior output is excluded so re-runs stay idempotent (see
    :func:`_is_own_output`); only the real native traces are sourced, and the deterministic
    ids then overwrite the assembled trace cleanly instead of multiplying it.

    Args:
        spoke_run_id: The session id (``langfuse.session.id``) to fetch.
        get: Path-to-JSON fetcher (see :data:`telemetry.langfuse_rollup.GetFn`).

    Returns:
        Each native trace id paired with its observations (full fields), in fetch order.
    """
    traces = [
        trace for trace in all_traces(spoke_run_id, get) if not _is_own_output(trace, spoke_run_id)
    ]
    return [(trace["id"], all_observations(trace["id"], get)) for trace in traces]


def purge_own_views(
    spoke_run_id: str,
    get: GetFn,
    delete: DeleteFn,
    *,
    attempts: int = _PURGE_POLL_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Bulk-delete this spoke's two assembled view traces and wait until they are gone (#156).

    Builder output is first-write-wins on the deterministic observation ids, so a re-post
    never refreshes stale span bodies — the two view traces must be purged first. The delete
    is asynchronous on the Langfuse server, so this polls the session listing until neither
    View A (``spoketree-``) nor View B (``spokecycle-``) trace is present before returning,
    letting the caller re-post onto a clean slate.

    Args:
        spoke_run_id: The spoke run id whose two view traces are purged.
        get: Path-to-JSON fetcher (see :data:`telemetry.langfuse_rollup.GetFn`).
        delete: Bulk trace-deleter (see :data:`telemetry.langfuse_rollup.DeleteFn`).
        attempts: Max listing polls before giving up.
        sleep: Wait between polls; injectable for tests.

    Raises:
        RuntimeError: When the view traces are still listed after ``attempts`` polls — the
            caller must not re-post over a half-deleted trace.
    """
    view_ids = [trace_id_for(spoke_run_id), cycle_trace_id_for(spoke_run_id)]
    delete(view_ids)
    view_set = set(view_ids)
    present: set[str] = view_set
    for _ in range(attempts):
        present = {trace["id"] for trace in all_traces(spoke_run_id, get)}
        if not (view_set & present):
            return
        sleep(_PURGE_POLL_INTERVAL)
    raise RuntimeError(
        f"view traces for {spoke_run_id} not deleted after {attempts} polls: "
        f"{sorted(view_set & present)}"
    )


def post_in_chunks(
    batch: list[IngestEvent], post: PostFn, *, chunk_size: int = _CHUNK_SIZE
) -> None:
    """POST an ingestion batch in fixed-size chunks.

    Args:
        batch: The full ingestion batch.
        post: Ingestion batch sink (see :data:`telemetry.langfuse_rollup.PostFn`).
        chunk_size: Maximum events per request.
    """
    for start in range(0, len(batch), chunk_size):
        post(batch[start : start + chunk_size])


def _scan_blocks(content: list[Any], wanted: set[str], found: dict[str, dict[str, object]]) -> None:
    """Collect ``tool_use`` inputs and ``tool_result`` contents for the wanted ids.

    Args:
        content: A message's ``content`` block list from one transcript line.
        wanted: The tool-call ids present on this spoke's tool spans (others are skipped).
        found: Accumulator mapping a tool-call id to its ``{"input"/"output": value}``.
    """
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("id") in wanted:
            found.setdefault(block["id"], {})["input"] = block.get("input")
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in wanted:
            found.setdefault(block["tool_use_id"], {})["output"] = block.get("content")


def _scan_file(path: Path, wanted: set[str], found: dict[str, dict[str, object]]) -> None:
    """Scan one transcript file line by line, ignoring malformed lines.

    Args:
        path: The transcript ``*.jsonl`` file.
        wanted: The tool-call ids present on this spoke's tool spans.
        found: Accumulator passed through to :func:`_scan_blocks`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("cannot read transcript %s: %s", path, e)
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            _scan_blocks(content, wanted, found)


def scan_transcripts(root: Path, wanted: set[str]) -> dict[str, ToolContent]:
    """Scan every transcript under ``root`` for the wanted tool calls' input/output.

    Tool-call ids are globally unique, so no per-session transcript mapping is needed — only
    the ids on this spoke's tool spans are collected. An id is returned when the transcripts
    carry an ``input`` block for it, an ``output`` block, or both.

    Args:
        root: The Claude Code projects root holding session ``*.jsonl`` transcripts.
        wanted: The tool-call ids present on this spoke's tool spans.

    Returns:
        A mapping of ``tool_use_id`` to its :class:`ToolContent`.
    """
    found: dict[str, dict[str, object]] = {}
    if not wanted:
        return {}
    for path in sorted(root.rglob("*.jsonl")):
        _scan_file(path, wanted, found)
    return {
        tuid: ToolContent(parts.get("input"), parts.get("output")) for tuid, parts in found.items()
    }


def transcript_scan_root(projects_root: Path, worktree: Path) -> Path:
    """Scope the transcript scan to the spoke's own Claude Code project dir when present.

    The default scan rglobbed EVERY session under ``projects_root`` on each land. Matching is
    by globally-unique ``tool_use_id``, so it never cross-attached another spoke's content
    (unlike #92's reasoning backfill) — but scoping to the worktree's project dir
    (:func:`telemetry.session_parser.project_dir_for_worktree`) avoids the all-projects rglob.
    Falls back to the full root when that dir is absent (a standalone run from a non-worktree
    cwd), preserving the prior behavior.

    Args:
        projects_root: The Claude Code projects root (``--projects``).
        worktree: The spoke's worktree dir (``--root``).

    Returns:
        The worktree's project dir when it exists, else ``projects_root``.
    """
    project_dir = project_dir_for_worktree(worktree, projects_root)
    return project_dir if project_dir.is_dir() else projects_root


def filled_tool_spans(traces: list[TraceObservations], tool_content: dict[str, ToolContent]) -> int:
    """Count the tool spans whose create body would gain transcript content (see summary)."""
    return sum(
        bool(_tool_additions(observation, tool_content))
        for _orig_trace_id, observations in traces
        for observation in observations
    )


def prefix_total(traces: list[TraceObservations]) -> int:
    """Return the full session prefix size from the first LLM call's token usage.

    Claude Code writes the whole session prefix (rules, skills, tools, base system
    prompt, ...) to the prompt cache on the first call. A cold cache writes it all as
    ``cache_creation``; a warm one splits it into ``cache_read`` + ``cache_creation``.
    The prefix total is therefore their SUM on the earliest observation carrying usage
    (chosen by ``startTime``) — ``cache_creation`` alone undercounts a warm session to
    near zero. That total is the figure the loaded-context items reconcile against.

    Args:
        traces: The source traces paired with their observations.

    Returns:
        The first call's ``cache_read + cache_creation`` token total, or 0 when no usage
        is present.
    """
    best_start: str | None = None
    best_value = 0
    for _orig_trace_id, observations in traces:
        for observation in observations:
            usage = observation.get("usageDetails") or {}
            read = usage.get("cache_read_input_tokens")
            written = usage.get("cache_creation_input_tokens")
            if read is None and written is None:
                continue
            start = observation.get("startTime") or ""
            if best_start is None or start < best_start:
                best_start = start
                best_value = int(read or 0) + int(written or 0)
    return best_value


def _lc_id(spoke_run_id: str, key: str) -> str:
    """Return the deterministic id of one loaded-context node for a spoke."""
    digest = hashlib.sha1(f"{spoke_run_id}:{key}".encode()).hexdigest()[:24]
    return _LC_PREFIX + digest


def _human_tokens(tokens: int) -> str:
    """Render a token count compactly for a node label (e.g. ``3.2k``)."""
    return f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)


def _lc_node(
    *, node_id: str, parent_id: str, trace_id: str, name: str, base_ts: str, metadata: dict
) -> IngestEvent:
    """Shape one loaded-context span-create event."""
    return {
        "id": node_id,
        "type": "span-create",
        "timestamp": base_ts,
        "body": {
            "id": node_id,
            "traceId": trace_id,
            "parentObservationId": parent_id,
            "name": name,
            "startTime": base_ts,
            "metadata": metadata,
        },
    }


def build_loaded_context_events(
    spoke_run_id: str,
    item_rows: list[dict[str, object]],
    *,
    category_order: tuple[str, ...],
    base_ts: str,
    prefix_total: int | None = None,
    price: float | None = None,
) -> list[IngestEvent]:
    """Build the single collapsed loaded-context observation under the spoke root.

    The loaded-context items are static startup inventory with token weights, not work —
    they share one timestamp and carry no causal order — so they collapse into ONE
    ``loaded-context`` span under the synthetic root instead of a ~60-leaf subtree. Its
    headline ``metadata.tokens`` is the total startup context tokens (the one number worth
    aggregating across spokes); ``metadata.breakdown`` carries the full itemization grouped
    by category (``{category: {name: tokens}}``, in ``category_order``, duplicate names
    summed); ``metadata.cost_usd`` is the aggregate cost.

    The primary, request-body path itemizes the WHOLE first-call prefix — every tool / MCP
    tool / system block / reminder by name and exact size — so it needs no reconciliation;
    ``prefix_total`` is then left None. The disk fallback (no request body) can only measure
    the on-disk categories, so it passes ``prefix_total`` and ``price`` to fold a single
    reconciled ``remainder`` = ``prefix_total - Σ measured`` (clamped ≥ 0) into both
    ``metadata.remainder`` and the headline total/cost — absorbing the base system prompt,
    all tool schemas, and MCP together, without a separate node.

    The id derives from the spoke run id so a rerun overwrites the same node.

    Args:
        spoke_run_id: The spoke run identifier.
        item_rows: Per-name measured rows (from :func:`measure_request_items` or
            :func:`measure_items`), each with ``category``, ``name``, ``tokens``,
            ``cost_usd`` (other per-row fields are ignored at this rendering layer).
        category_order: The category keys to render, in display order; empties are dropped.
        base_ts: ISO timestamp stamped on the synthetic node.
        prefix_total: The first-call ``cache_read + cache_creation`` total; pass it (with
            ``price``) only on the disk fallback to fold in the reconciled remainder.
        price: Cache-creation price in USD per token, for the folded remainder's cost.

    Returns:
        A single-element list: the collapsed loaded-context ingestion event.
    """
    measured_tokens = sum(int(cast(int, row["tokens"])) for row in item_rows)
    measured_cost = sum(float(cast(float, row["cost_usd"])) for row in item_rows)

    metadata: dict[str, object] = {
        "tokens": measured_tokens,
        "cost_usd": measured_cost,
        "breakdown": _breakdown_by_category(item_rows, category_order),
    }
    if prefix_total is not None and price is not None:
        remainder = max(0, prefix_total - measured_tokens)
        metadata["remainder"] = remainder
        metadata["tokens"] = measured_tokens + remainder
        metadata["cost_usd"] = measured_cost + remainder * price

    total = cast(int, metadata["tokens"])
    return [
        _lc_node(
            node_id=_lc_id(spoke_run_id, "loaded-context"),
            parent_id=root_id_for(spoke_run_id),
            trace_id=trace_id_for(spoke_run_id),
            name=f"loaded-context: {_human_tokens(total)}",
            base_ts=base_ts,
            metadata=metadata,
        )
    ]


def _breakdown_by_category(
    item_rows: list[dict[str, object]], category_order: tuple[str, ...]
) -> dict[str, dict[str, int]]:
    """Group per-name token counts by category in ``category_order``, summing duplicate names.

    Drops empty categories. Duplicate ``(category, name)`` rows (e.g. a nested ``CLAUDE.md``)
    are summed so each name appears once with its combined weight.
    """
    breakdown: dict[str, dict[str, int]] = {}
    for category in category_order:
        names: dict[str, int] = {}
        for row in item_rows:
            if row["category"] != category:
                continue
            name = cast(str, row["name"])
            names[name] = names.get(name, 0) + int(cast(int, row["tokens"]))
        if names:
            breakdown[category] = names
    return breakdown


def loaded_context_rows(
    root: Path, *, counter: TokenCounter, price: float
) -> list[dict[str, object]]:
    """Measure the disk-sourceable loaded-context entries, one row per name.

    Only the on-disk categories (rules / memory / skills / sub-agents / environment) are
    itemized; the built-in tool and MCP schemas are NOT on disk and are not obtainable
    per-tool, so they are reconciled in aggregate by :func:`build_loaded_context_events`.

    Args:
        root: Worktree root for the disk-measurable items.
        counter: Token counter; raises ``CountTokensError`` when unreachable.
        price: Cache-creation price in USD per token.

    Returns:
        The per-name rows for rules / memory / skills / sub-agents / environment.
    """
    return measure_items(assemble_items(root), counter=counter, price=price)


def find_request_files(bodies_dir: Path) -> list[Path]:
    """Return the ``*.request.json`` dumps in ``bodies_dir``, oldest first.

    Sorted by modification time, not name: the dumps are ``<uuid>.request.json`` and random
    UUIDs are not chronological, so a name sort would not yield emission order.
    """
    if not bodies_dir.is_dir():
        return []
    return sorted(bodies_dir.glob("*.request.json"), key=lambda path: path.stat().st_mtime)


def request_context_rows(
    bodies_dir: Path, *, counter: TokenCounter, price: float
) -> list[dict[str, object]] | None:
    """Itemize the loaded context from the first real raw request body in ``bodies_dir``.

    Picks the first ``.request.json`` whose ``tools`` array is non-empty (skipping any
    degenerate aux call), parses it, and measures every tool / MCP tool / system block /
    reminder by name and exact size. This is the primary, fully-itemized path.

    Args:
        bodies_dir: The per-spoke ``OTEL_LOG_RAW_API_BODIES=file:<dir>`` dump directory.
        counter: Token counter; raises ``CountTokensError`` when unreachable.
        price: Cache-creation price in USD per token.

    Returns:
        The per-name request-body rows, or None when no real request body is found (the
        caller then falls back to disk measurement).
    """
    path = first_real_request(find_request_files(bodies_dir))
    if path is None:
        return None
    parsed = parse_request_body(path)
    return measure_request_items(parsed.items, counter=counter, price=price)


def _llm_requests_in_order(traces: list[TraceObservations]) -> list[tuple[str, Observation]]:
    """Return ``(orig_trace_id, observation)`` for each LLM call, oldest first by ``startTime``.

    An LLM call is any observation carrying ``cache_read_input_tokens`` or
    ``cache_creation_input_tokens`` usage — the same set the request-body dumps correspond to,
    so the two align positionally (the basis of the count gate in
    :func:`apply_llm_decomposition`).
    """
    calls: list[tuple[str, str, Observation]] = []
    for orig_trace_id, observations in traces:
        for observation in observations:
            usage = observation.get("usageDetails") or {}
            if (
                usage.get("cache_read_input_tokens") is None
                and usage.get("cache_creation_input_tokens") is None
            ):
                continue
            calls.append((observation.get("startTime") or "", orig_trace_id, observation))
    calls.sort(key=lambda call: call[0])
    return [(orig_trace_id, observation) for _start, orig_trace_id, observation in calls]


def _split_rows_by_cache(
    rows: list[dict[str, object]], *, cache_read: int, cache_creation: int
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Partition itemized rows into the cache_read / cache_creation budgets by cumulative fit.

    Walking the items in request order, the first ``cache_read`` tokens fall in the reused
    prefix, the next ``cache_creation`` tokens are the portion written this turn, and anything
    beyond is fresh input (not shown). Each item is assigned WHOLE by its cumulative start
    offset, so the split reconciles to the observed counters with a per-bucket remainder rather
    than the ``cached`` flag, which mislabels both cold calls (whole prefix written, not read)
    and the freshly-written delta of warm calls.
    """
    read: list[dict[str, object]] = []
    creation: list[dict[str, object]] = []
    offset = 0
    for row in rows:
        start = offset
        offset += int(cast(int, row["tokens"]))
        if start < cache_read:
            read.append(row)
        elif start < cache_read + cache_creation:
            creation.append(row)
    return read, creation


def _decomp_metadata(rows: list[dict[str, object]], observed: int) -> dict[str, Any]:
    """Shape one cache bucket's decomposition metadata: per-component -> per-item, reconciled.

    ``components`` maps each category (in :data:`_DECOMP_CATEGORY_ORDER`) to ``{name: tokens}``
    for the items the split routed into this bucket — items that share a name within a category
    are SUMMED (by :func:`_breakdown_by_category`), so ``Σ components == measured`` holds;
    ``measured`` is their sum and ``remainder`` is ``observed - measured`` so the itemization
    reconciles (≈) to the billed counter (the remainder absorbs the base system prompt / tool
    schemas not itemized per-name).
    """
    measured = sum(int(cast(int, row["tokens"])) for row in rows)
    return {
        "observed": observed,
        "measured": measured,
        "remainder": observed - measured,
        "components": _breakdown_by_category(rows, _DECOMP_CATEGORY_ORDER),
    }


def apply_llm_decomposition(
    batch: list[IngestEvent],
    traces: list[TraceObservations],
    bodies_dir: Path,
    *,
    counter: TokenCounter,
    price: float,
) -> int:
    """Fold the #99 cache_read/cache_creation decomposition onto each llm_request copy (#100).

    For each LLM call (aligned positionally with its raw request body) the body is itemized by
    :func:`telemetry.request_body.decompose_request_body` — rules per file, skills per skill,
    every message — split into the observed ``cache_read`` / ``cache_creation`` budgets by
    cumulative fit (:func:`_split_rows_by_cache`), and written as ``metadata.cache_read`` /
    ``metadata.cache_creation`` on the call's copy in ``batch`` (per-component -> per-item, with
    an ``observed`` / ``measured`` / ``remainder`` reconciliation) — NOT as nested child nodes,
    so the call reads as a single node with the decomposition on it.

    The alignment is positional (LLM calls by ``startTime`` ↔ bodies by mtime) and is only
    applied when the counts match — otherwise an aux/degenerate call has skewed the alignment and
    the decomposition is skipped entirely (the count gate).

    Args:
        batch: The assembled ingestion events; the llm_request copies are mutated in place.
        traces: The source traces paired with their observations.
        bodies_dir: The per-spoke ``OTEL_LOG_RAW_API_BODIES=file:<dir>`` dump directory.
        counter: Token counter; raises ``CountTokensError`` to trigger the char/4 fallback.
        price: Cache-creation price in USD per token (used by ``measure_request_items``).

    Returns:
        The number of llm_request copies that received a decomposition (0 when none align).
    """
    calls = _llm_requests_in_order(traces)
    bodies = find_request_files(bodies_dir)
    if not calls or len(calls) != len(bodies):
        return 0
    by_id = {event["body"]["id"]: event for event in batch}
    decomposed = 0
    for (orig_trace_id, observation), body_path in zip(calls, bodies):
        event = by_id.get(_copy_id(orig_trace_id, observation["id"]))
        if event is None:
            continue  # the call's copy is not in the batch (defensive; should not happen)
        try:
            items = decompose_request_body(body_path)
        except (OSError, json.JSONDecodeError):
            logger.warning("cannot decompose request body %s", body_path)
            continue
        rows = measure_request_items(items, counter=counter, price=price)
        usage = observation.get("usageDetails") or {}
        read_tokens = int(usage.get("cache_read_input_tokens") or 0)
        creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
        read_rows, creation_rows = _split_rows_by_cache(
            rows, cache_read=read_tokens, cache_creation=creation_tokens
        )
        metadata = event["body"].setdefault("metadata", {})
        metadata["cache_read"] = _decomp_metadata(read_rows, read_tokens)
        metadata["cache_creation"] = _decomp_metadata(creation_rows, creation_tokens)
        decomposed += 1
    return decomposed


def _memoized_counter(counter: TokenCounter) -> TokenCounter:
    """Wrap a token counter to cache counts by content hash across the whole build (#160).

    The stable prefix (tools / system / rules / skills) and every unchanged message are re-counted
    on every consecutive snapshot and on every #99 decomposition, so the same text is measured
    many times over a run; caching by sha256 collapses that to one call per distinct text. A
    counter failure (``CountTokensError``) is not cached — it propagates so the caller's char/4
    fallback still applies — so only successful counts are memoized.

    Args:
        counter: The underlying token counter.

    Returns:
        A counter with the same contract, backed by a per-build content-hash cache.
    """
    cache: dict[str, int] = {}

    def _counting(text: str) -> int:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if key not in cache:
            cache[key] = counter(text)
        return cache[key]

    return _counting


def _blob_hash(value: object) -> str:
    """Return a stable content hash of a str or JSON-able value (skill-output match identity)."""
    text = (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _skill_output_hashes(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, str]:
    """Map each ``tool:Skill`` transcript output's content hash to its skill name (#160).

    The exact identity a skill-load injects into the next request is the tool_result the
    ``tool:Skill`` returned, so its content hash keys the attribution; the name comes from the
    tool's transcript input (:func:`_activated_skill_name`).
    """
    hashes: dict[str, str] = {}
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if (observation.get("name") or "") != _SKILL_TOOL_NAME:
                continue
            tuid = _tool_use_id(observation)
            content = tool_content.get(tuid or "")
            name = _activated_skill_name(tuid, tool_content)
            if content is None or content.output is None or not name:
                continue
            hashes[_blob_hash(content.output)] = name
    return hashes


def _match_skill_output(text: str | None, skill_hashes: dict[str, str]) -> str | None:
    """Return the skill whose output an added message injected, matched by content hash, else None.

    The message text is the canonical ``{role, content}`` JSON; a skill-load rides a
    ``tool_result`` block whose ``content`` is the skill's output — that block's hash (or, for a
    plain-string message content, the content itself) is matched against :func:`_skill_output_hashes`.
    """
    if not text:
        return None
    try:
        message = json.loads(text)
    except (TypeError, ValueError):
        return None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return skill_hashes.get(_blob_hash(content))
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            skill = skill_hashes.get(_blob_hash(block.get("content")))
            if skill:
                return skill
    return None


def _label_skill_loads(
    added: list[dict[str, object]],
    curr_items: list[ContextItem],
    skill_hashes: dict[str, str],
) -> None:
    """Label each added-message row whose injected content matches a skill output, in place (#160)."""
    if not skill_hashes:
        return
    text_by_name = {item.name: item.text for item in curr_items if item.category == "messages"}
    for row in added:
        if row.get("category") != "messages":
            continue
        skill = _match_skill_output(text_by_name.get(str(row.get("name"))), skill_hashes)
        if skill:
            row["skill"] = skill


def _context_delta_summary(delta: ContextDelta) -> dict[str, int]:
    """Reduce a context delta to the token totals rolled up onto a step (net / added / removed)."""
    added = sum(int(cast(int, row["tokens"])) for row in delta.added)
    removed = sum(int(cast(int, row["tokens"])) for row in delta.removed)
    return {"net_tokens": delta.net_tokens, "added": added, "removed": removed}


def apply_context_deltas(
    batch: list[IngestEvent],
    traces: list[TraceObservations],
    bodies_dir: Path,
    *,
    counter: TokenCounter,
    price: float,
    tool_content: dict[str, ToolContent],
) -> dict[tuple[str, str], dict[str, int]]:
    """Stamp ``metadata.context_delta`` on each llm_request copy from consecutive bodies (#160).

    For every LLM call after the first (aligned positionally with its raw request body, same count
    gate as :func:`apply_llm_decomposition`), the body is diffed against its predecessor
    (:func:`telemetry.request_body.diff_snapshots`) into added / removed / size-changed rows,
    ``net_tokens`` (which reconciles ± remainder against the call's observed ``cache_creation``),
    and a compaction ``label``. An added message whose injected content matches a ``tool:Skill``
    output is tagged with the skill name (:func:`_label_skill_loads`). The delta is stamped on the
    call's View A copy only (single-emit) as metadata — it never touches billed usage.

    Args:
        batch: The assembled View A events; the llm_request copies are mutated in place.
        traces: The source traces paired with their observations.
        bodies_dir: The per-spoke raw-body dump directory.
        counter: Token counter (memoize it — the stable prefix repeats every snapshot).
        price: Cache-creation price in USD per token.
        tool_content: Tool-call-id to :class:`ToolContent`, the source of skill outputs.

    Returns:
        A map of each stamped call's ``(orig_trace_id, observation_id)`` to its
        :func:`_context_delta_summary`, so the step ``rollup.context`` can be aggregated per view.
    """
    calls = _llm_requests_in_order(traces)
    bodies = find_request_files(bodies_dir)
    if not calls or len(calls) != len(bodies):
        return {}
    by_id = {event["body"]["id"]: event for event in batch}
    skill_hashes = _skill_output_hashes(traces, tool_content)
    summaries: dict[tuple[str, str], dict[str, int]] = {}
    prev_items: list[ContextItem] | None = None
    for (orig_trace_id, observation), body_path in zip(calls, bodies):
        try:
            curr_items = snapshot_items_from_path(body_path)
        except (OSError, json.JSONDecodeError):
            logger.warning("cannot snapshot request body %s", body_path)
            prev_items = None
            continue
        predecessor, prev_items = prev_items, curr_items
        if predecessor is None:
            continue  # the first call has no prior snapshot to diff against
        event = by_id.get(_copy_id(orig_trace_id, observation["id"]))
        if event is None:
            continue  # the call's copy is not in the batch (defensive; should not happen)
        delta = diff_snapshots(predecessor, curr_items, counter=counter, price=price)
        _label_skill_loads(delta.added, curr_items, skill_hashes)
        event["body"].setdefault("metadata", {})["context_delta"] = {
            "added": delta.added,
            "removed": delta.removed,
            "changed": delta.changed,
            "net_tokens": delta.net_tokens,
            "label": delta.label,
        }
        summaries[(orig_trace_id, observation["id"])] = _context_delta_summary(delta)
    return summaries


def _apply_context_rollups(
    events: list[IngestEvent], summary_by_id: dict[str, dict[str, int]]
) -> None:
    """Aggregate ``metadata.rollup.context`` onto each step node from its llm_request deltas (#160).

    Sums the per-call context summaries of every llm_request copy in a step's subtree into
    ``{net_tokens, added, removed}`` under the step's existing ``metadata.rollup``, so per-cycle
    context cost reads without a full-trace GET. View-agnostic: the caller keys ``summary_by_id``
    by that view's copy ids (View A ``tree-…`` / View B ``cyc-…``). A step with no llm_request
    delta gets no ``context`` key.

    Args:
        events: The assembled events for one view; step-node bodies are mutated in place.
        summary_by_id: Copy id (in this view's namespace) to its context-delta summary.
    """
    bodies = [event["body"] for event in events if event["type"] != "trace-create"]
    _by_id, children = build_tree(bodies)
    for body in bodies:
        node_id = body["id"]
        if not (node_id.startswith(_STEP_PREFIX) or node_id.startswith(_CYCLE_STEP_PREFIX)):
            continue
        context = _sum_context(node_id, children, summary_by_id)
        if context is not None:
            body.setdefault("metadata", {}).setdefault("rollup", {})["context"] = context


def _sum_context(
    node_id: str, children: dict[str | None, list[str]], summary_by_id: dict[str, dict[str, int]]
) -> dict[str, int] | None:
    """Sum the context summaries of a step's descendant llm_requests, or None when it has none."""
    total = {"net_tokens": 0, "added": 0, "removed": 0}
    found = False
    stack = list(children.get(node_id, []))
    while stack:
        current = stack.pop()
        summary = summary_by_id.get(current)
        if summary is not None:
            found = True
            for key in total:
                total[key] += summary[key]
        stack.extend(children.get(current, []))
    return total if found else None


def _merge_trace_tags(batch: list[IngestEvent], tags: list[str]) -> None:
    """Merge ``tags`` into the batch's ``trace-create`` event, de-duplicated, order-stable."""
    if not tags:
        return
    trace = next((event for event in batch if event.get("type") == "trace-create"), None)
    if trace is None:
        return  # defensive: build_batch always emits one
    existing: list[str] = trace["body"].setdefault("tags", [])
    for tag in tags:
        if tag not in existing:
            existing.append(tag)


def _read_pointer(path: Path, valid: tuple[str, ...], default: str) -> str:
    """Read a one-line ``.ai-toolkit`` pointer, falling back to ``default`` safely.

    A missing file, an unreadable file, a blank value, or a value outside ``valid`` all
    resolve to ``default`` — a legacy or corrupt pointer must never crash the assembler or
    mislabel the trace.
    """
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return value if value in valid else default


def read_mode_lane(root: Path) -> tuple[str, str]:
    """Return the spoke's ``(mode, lane)`` from its launch pointer files under ``root``.

    Args:
        root: The worktree root holding ``.ai-toolkit/mode`` and ``.ai-toolkit/lane``.

    Returns:
        The validated ``(mode, lane)`` pair, defaulting to ``("attended", "spoke")`` for a
        missing, blank, or unrecognized pointer (#102).
    """
    mode = _read_pointer(root / _MODE_POINTER, _VALID_MODES, _DEFAULT_MODE)
    lane = _read_pointer(root / _LANE_POINTER, _VALID_LANES, _DEFAULT_LANE)
    return mode, lane


def apply_mode_lane_tags(batch: list[IngestEvent], mode: str, lane: str) -> None:
    """Attach the spoke's execution ``mode`` + ``lane`` to its trace (#102).

    Both surface as trace-level ``mode:<value>`` / ``lane:<value>`` tags (so a Langfuse
    dashboard can group/filter/chart spokes by how they were run) and are mirrored, bare,
    into trace metadata for direct lookup.

    Args:
        batch: The assembled ingestion events; the ``trace-create`` event is mutated in place.
        mode: The execution mode (``afk`` | ``attended``).
        lane: The spoke's lane (``micro`` | ``express`` | ``quick`` | ``spoke``).
    """
    _merge_trace_tags(batch, [f"mode:{mode}", f"lane:{lane}"])
    trace = next((event for event in batch if event.get("type") == "trace-create"), None)
    if trace is None:
        return  # defensive: build_batch always emits one
    metadata = trace["body"].setdefault("metadata", {})
    metadata["mode"] = mode
    metadata["lane"] = lane


def apply_request_body_metadata(
    batch: list[IngestEvent],
    traces: list[TraceObservations],
    bodies_dir: Path,
) -> int:
    """Fold each request body's ``output_config.effort`` + cache breakpoints onto its copy (#101).

    For each LLM call (aligned positionally with its raw request body, the same alignment and
    count gate as :func:`apply_llm_decomposition`) the body is parsed once and two request-derived
    signals are surfaced on the call's llm_request copy:

    - ``metadata.cache_breakpoints`` — the ``cache_control`` prefix boundary positions
      (``{location, index}``, in order), surfaced on EVERY aligned call as a list (empty when the
      body has no marker) so "no breakpoints" reads distinctly from "not measured". This diagnoses
      a moved breakpoint that busted the cache.
    - ``metadata.effort`` + a trace-level ``effort:<value>`` tag — the ``output_config.effort``
      reasoning level, when it is a genuine effort (Langfuse can group/filter/chart traces by tag).
      ``ultra`` is the ultracode/harness mode, not an effort: it is diverted to a single
      ``ultracode`` trace tag and never recorded as an effort.

    Args:
        batch: The assembled ingestion events; the llm_request copies + trace are mutated in place.
        traces: The source traces paired with their observations.
        bodies_dir: The per-spoke ``OTEL_LOG_RAW_API_BODIES=file:<dir>`` dump directory.

    Returns:
        The number of llm_request copies that received an ``effort`` (0 when none align or only
        ultracode was seen); cache breakpoints are surfaced independently of this count.
    """
    calls = _llm_requests_in_order(traces)
    bodies = find_request_files(bodies_dir)
    if not calls or len(calls) != len(bodies):
        return 0
    by_id = {event["body"]["id"]: event for event in batch}
    efforts: list[str] = []
    ultracode = False
    attached = 0
    for (orig_trace_id, observation), body_path in zip(calls, bodies):
        event = by_id.get(_copy_id(orig_trace_id, observation["id"]))
        if event is None:
            continue  # the call's copy is not in the batch (defensive; should not happen)
        try:
            body = parse_request_body(body_path)
        except (OSError, json.JSONDecodeError):
            logger.warning("cannot read request body %s", body_path)
            continue
        metadata = event["body"].setdefault("metadata", {})
        metadata["cache_breakpoints"] = [
            {"location": boundary.location, "index": boundary.index}
            for boundary in body.cache_boundaries
        ]
        effort = body.effort
        if effort is None:
            continue
        if effort == _ULTRA_MODE:
            ultracode = True
            continue
        metadata["effort"] = effort
        if effort not in efforts:
            efforts.append(effort)
        attached += 1
    tags = [f"effort:{effort}" for effort in efforts]
    if ultracode:
        tags.append(_ULTRACODE_TAG)
    _merge_trace_tags(batch, tags)
    return attached


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
) -> IngestEvent:
    """Shape one numeric ``score-create`` ingestion event (trace- or observation-level)."""
    target = observation_id or "trace"
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


def _is_activity_observation(observation: Observation) -> bool:
    """Whether an observation is genuine spoke activity (a turn, an LLM call, or a tool call).

    Used to find where the spoke resumed after a PLAN-gate park — the markers, hooks, and
    script spans that may fire right after the gate are NOT activity and are skipped.
    """
    if observation.get("type") == "GENERATION":
        return True
    name = observation.get("name") or ""
    return name == _INTERACTION_NAME or name.startswith("tool:")


def _earliest_after(candidates: list[str], floor: datetime) -> str | None:
    """Return the chronologically-earliest ISO ``candidates`` value parsed strictly after ``floor``.

    Compares PARSED datetimes (not raw strings), so mixed ISO forms (``Z`` vs ``+00:00``,
    fractional seconds) order correctly. A candidate whose parse fails or whose tz-awareness
    differs from ``floor`` (an uncomparable pair) is skipped rather than crashing.
    """
    best_dt: datetime | None = None
    best_str: str | None = None
    for value in candidates:
        parsed = _parse_ts(value)
        if parsed is None:
            continue
        try:
            after = parsed > floor
        except TypeError:
            continue  # naive vs aware — uncomparable, skip
        if after and (best_dt is None or parsed < best_dt):
            best_dt, best_str = parsed, value
    return best_str


def _gate_park_bounds(traces: list[TraceObservations]) -> tuple[str, str] | None:
    """Return the PLAN-gate park's ``(start, end)`` ISO bounds, or None when the spoke never parked.

    The park starts at the end of the earliest gate observation (:func:`_is_gate_observation`,
    the ``spoke-ready.sh --gate`` emission) and ends at the first genuine spoke activity
    (:func:`_is_activity_observation`) that starts after it — the resumption once the plan was
    approved. All comparisons parse the ISO timestamps. None when there is no gate observation or
    nothing resumed after it.
    """
    gate_ends: list[str] = []
    activity_starts: list[str] = []
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if _is_gate_observation(observation):
                end = observation.get("endTime") or observation.get("startTime")
                if end:
                    gate_ends.append(end)
            elif _is_activity_observation(observation) and observation.get("startTime"):
                activity_starts.append(observation["startTime"])
    parsed_gates = [(dt, end) for end in gate_ends if (dt := _parse_ts(end)) is not None]
    if not parsed_gates:
        return None
    gate_floor, gate_end = min(parsed_gates, key=lambda pair: pair[0])
    resume = _earliest_after(activity_starts, gate_floor)
    if resume is None:
        return None
    return gate_end, resume


def _gate_park_ms(traces: list[TraceObservations]) -> int | None:
    """Return the PLAN-gate park wait in ms, or None when the spoke never parked at a gate."""
    bounds = _gate_park_bounds(traces)
    return _elapsed_ms(*bounds) if bounds is not None else None


def _parse_commits(dump: str) -> list[dict[str, Any]]:
    """Parse a ``git log --numstat`` dump into per-commit records (#162).

    The dump is produced with a ``commit<US>%H<US>%aI<US>%s`` format line per commit (``<US>`` is
    :data:`_COMMIT_FIELD_SEP`), followed by the commit's ``additions<TAB>deletions<TAB>path``
    numstat lines. A binary file's counts are ``-`` and contribute 0. Records preserve dump order.

    Args:
        dump: The raw ``git log --numstat`` output.

    Returns:
        One record per commit: ``{sha, message, authored_at, files, additions, deletions}``.
    """
    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in dump.splitlines():
        if line.startswith(_COMMIT_LINE_MARKER + _COMMIT_FIELD_SEP):
            parts = line.split(_COMMIT_FIELD_SEP)
            if len(parts) < 4:
                current = None
                continue
            current = {
                "sha": parts[1],
                "message": _COMMIT_FIELD_SEP.join(parts[3:]),
                "authored_at": parts[2],
                "files": [],
                "additions": 0,
                "deletions": 0,
            }
            commits.append(current)
        elif current is not None and line.strip():
            columns = line.split("\t")
            if len(columns) == 3:
                added, deleted, path = columns
                current["additions"] += int(added) if added.isdigit() else 0
                current["deletions"] += int(deleted) if deleted.isdigit() else 0
                current["files"].append(path)
    return commits


def _load_commits(path: Path | None) -> list[dict[str, Any]]:
    """Read and parse a commit dump path, or return ``[]`` when absent/unreadable (best-effort)."""
    if path is None:
        return []
    try:
        # errors="replace": a commit subject in a non-UTF-8 locale must degrade a glyph, never
        # crash the land-time view build (the whole ingest step is best-effort).
        return _parse_commits(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        logger.warning("cannot read commits dump %s", path)
        return []


def _commit_id(spoke_run_id: str, sha: str, *, cycle: bool) -> str:
    """Return the deterministic id of a commit node (per view, idempotent across reruns)."""
    namespace = "cyccommit" if cycle else "commit"
    digest = hashlib.sha1(f"{spoke_run_id}:{namespace}:{sha}".encode()).hexdigest()[:24]
    return _COMMIT_PREFIX + digest


def _gate_park_node_id(spoke_run_id: str, *, cycle: bool) -> str:
    """Return the deterministic id of the gate-park node (per view, idempotent across reruns)."""
    namespace = "cycgatepark" if cycle else "gatepark"
    digest = hashlib.sha1(f"{spoke_run_id}:{namespace}".encode()).hexdigest()[:24]
    return _GATE_PARK_PREFIX + digest


def _commit_events(
    commits: list[dict[str, Any]],
    *,
    spoke_run_id: str,
    trace_id: str,
    cycle: bool,
    parent_for: Callable[[str], str],
) -> list[IngestEvent]:
    """Build ``commit:<sha7>`` timeline nodes, each an instant at its author time (#162).

    Each node is placed under ``parent_for(author_time)`` (the root in View A, the containing step
    in View B) and carries ``{sha, message, files, additions, deletions}`` metadata but no usage,
    so it never affects trace cost or the duration rollup.
    """
    events: list[IngestEvent] = []
    for commit in commits:
        sha = str(commit["sha"])
        authored_at = str(commit["authored_at"])
        node_id = _commit_id(spoke_run_id, sha, cycle=cycle)
        events.append(
            {
                "id": node_id,
                "type": "span-create",
                "timestamp": authored_at,
                "body": {
                    "id": node_id,
                    "traceId": trace_id,
                    "parentObservationId": parent_for(authored_at),
                    "name": f"commit:{sha[:7]}",
                    "startTime": authored_at,
                    "endTime": authored_at,
                    "metadata": {
                        "sha": sha,
                        "message": commit["message"],
                        "files": commit["files"],
                        "additions": commit["additions"],
                        "deletions": commit["deletions"],
                    },
                },
            }
        )
    return events


def _gate_park_event(
    traces: list[TraceObservations],
    *,
    spoke_run_id: str,
    trace_id: str,
    cycle: bool,
    parent_id: str,
) -> IngestEvent | None:
    """Build the ``wait:gate-park`` timeline block from the gate-park bounds, or None (#162).

    The block spans the gate's end to the resumption after approval (:func:`_gate_park_bounds`);
    its ``wait:`` name routes it into the duration ``wait`` bucket, so in View A the park time
    moves out of the root's ``self`` gap without changing ``total_ms``.

    UPGRADE: in the rare case a non-activity span (a second gate, a hook) falls inside the park
    window, both it and this node book the overlap into ``wait`` (span-time, not wall-time) — carve
    the node's interval around such spans if the wait bucket ever needs to be exact.
    """
    bounds = _gate_park_bounds(traces)
    if bounds is None:
        return None
    start, end = bounds
    node_id = _gate_park_node_id(spoke_run_id, cycle=cycle)
    return {
        "id": node_id,
        "type": "span-create",
        "timestamp": start,
        "body": {
            "id": node_id,
            "traceId": trace_id,
            "parentObservationId": parent_id,
            "name": _GATE_PARK_NODE_NAME,
            "startTime": start,
            "endTime": end,
        },
    }


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
    metrics dimension, so each View B step emits ``step_cost_usd:<PHASE>`` and
    ``step_tokens_written:<PHASE>`` from its rollup's ``written`` tokens (cost = written × the
    cache-creation ``price``), observation-scoped to the step node with a deterministic id.

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
                name=f"{_STEP_COST_SCORE}:{phase}",
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


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the CLI arguments for the spoke-tree assembler."""
    env = os.environ
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("spoke_run_id", help="The spoke run id (session id) to assemble.")
    parser.add_argument(
        "--projects",
        type=Path,
        default=_DEFAULT_PROJECTS,
        help="Root holding Claude Code session transcripts (default: ~/.claude/projects).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Worktree root for the disk-measurable loaded-context items (default: cwd).",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model id for count_tokens.")
    parser.add_argument(
        "--request-bodies",
        type=Path,
        default=Path(env[_BODY_DIR_ENV]) if env.get(_BODY_DIR_ENV) else None,
        help=(
            "Dir of OTEL_LOG_RAW_API_BODIES=file:<dir> request dumps to itemize the loaded "
            f"context from (default: ${_BODY_DIR_ENV}, else <root>/{_BODY_DIR_CONVENTION}). "
            "Falls back to disk measurement when no real request body is found."
        ),
    )
    parser.add_argument(
        "--endpoint",
        default=env.get("ANTHROPIC_BASE_URL", DEFAULT_ENDPOINT),
        help="Anthropic API base URL for count_tokens.",
    )
    parser.add_argument(
        "--api-key", default=env.get("ANTHROPIC_API_KEY"), help="Anthropic API key."
    )
    parser.add_argument(
        "--price", type=float, default=_DEFAULT_PRICE, help="Cache-creation USD per token."
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Bulk-delete this spoke's two view traces and wait until they are gone before "
            "re-posting, so a view-shape change fully replaces stale span bodies (#156)."
        ),
    )
    parser.add_argument(
        "--keep-noop-guards",
        action="store_true",
        help=(
            "Retain no-op guard spans (decision=allow, status=success, <1s) as children of their "
            "guards group instead of dropping them; the per-hook rollup is unchanged either way "
            "(#157)."
        ),
    )
    parser.add_argument(
        "--commits",
        type=Path,
        default=None,
        help=(
            "A `git log --numstat` dump of the spoke branch's origin/main..HEAD commits "
            "(commit<US>%%H<US>%%aI<US>%%s format lines); each becomes a commit:<sha7> timeline "
            "node (#162). Omitted for a non-land re-run (no worktree to read commits from)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Assemble a spoke's rich Langfuse observations into one nested trace.

    Args:
        argv: CLI arguments excluding the program name; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success.

    Raises:
        KeyError: When ``LANGFUSE_BASIC_AUTH`` is not set.
    """
    logging.basicConfig(level=logging.INFO, format="[spoke-tree] %(message)s")
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    auth = os.environ["LANGFUSE_BASIC_AUTH"]  # "Basic <base64(pk:sk)>"
    get, post = make_get(host, auth), make_post(host, auth)

    if args.rebuild:
        logger.info("--rebuild: purging prior views for %s before re-posting", args.spoke_run_id)
        purge_own_views(args.spoke_run_id, get, make_delete(host, auth))

    traces = fetch_session(args.spoke_run_id, get)
    scan_root = transcript_scan_root(args.projects, args.root.resolve())
    tool_content = scan_transcripts(scan_root, _tool_span_ids(traces))
    commits = _load_commits(args.commits)
    batch = build_batch(
        traces,
        args.spoke_run_id,
        tool_content,
        keep_noop_guards=args.keep_noop_guards,
        commits=commits,
    )
    cycle_batch = build_cycle_batch(
        traces,
        args.spoke_run_id,
        tool_content,
        keep_noop_guards=args.keep_noop_guards,
        commits=commits,
    )
    mode, lane = read_mode_lane(args.root.resolve())
    apply_mode_lane_tags(batch, mode, lane)
    apply_mode_lane_tags(cycle_batch, mode, lane)

    # One counter, memoized by content hash and shared across the loaded-context measurement, the
    # #99 decomposition, and the #160 context deltas — the stable prefix repeats on every snapshot.
    counter = _memoized_counter(
        make_counter(endpoint=args.endpoint, api_key=args.api_key, model=args.model)
    )
    base_ts = _earliest_start(traces)
    bodies_dir = args.request_bodies or (args.root.resolve() / _BODY_DIR_CONVENTION)
    request_rows = request_context_rows(bodies_dir, counter=counter, price=args.price)
    if request_rows is not None:
        rows, source = request_rows, "request body"
        context_events = build_loaded_context_events(
            args.spoke_run_id, rows, category_order=_REQUEST_CATEGORY_ORDER, base_ts=base_ts
        )
    else:
        rows, source = (
            loaded_context_rows(args.root.resolve(), counter=counter, price=args.price),
            "disk",
        )
        context_events = build_loaded_context_events(
            args.spoke_run_id,
            rows,
            category_order=_DISK_CATEGORY_ORDER,
            base_ts=base_ts,
            prefix_total=prefix_total(traces),
            price=args.price,
        )
    decomposed = apply_llm_decomposition(
        batch, traces, bodies_dir, counter=counter, price=args.price
    )
    # UPGRADE: apply_llm_decomposition + apply_request_body_metadata each re-walk
    # _llm_requests_in_order and re-stat find_request_files over the same inputs — compute the
    # calls↔bodies pairing once in main and pass it in if the body count ever makes it measurable.
    efforts = apply_request_body_metadata(batch, traces, bodies_dir)
    deltas = apply_context_deltas(
        batch, traces, bodies_dir, counter=counter, price=args.price, tool_content=tool_content
    )
    _apply_context_rollups(batch, {_copy_id(o, i): s for (o, i), s in deltas.items()})
    _apply_context_rollups(
        cycle_batch, {cycle_copy_id_for(o, i): s for (o, i), s in deltas.items()}
    )
    score_events = build_score_events(args.spoke_run_id, traces, batch, base_ts=base_ts)
    step_scores = build_step_cost_scores(
        args.spoke_run_id, cycle_batch, base_ts=base_ts, price=args.price
    )
    post_in_chunks(batch + context_events + score_events + cycle_batch + step_scores, post)

    trace_id = trace_id_for(args.spoke_run_id)
    cycle_trace_id = cycle_trace_id_for(args.spoke_run_id)
    filled = filled_tool_spans(traces, tool_content)
    print(
        f"{len(batch) - 2} observations assembled under trace {trace_id} "
        f"(roots collapsed to 1), {filled} tool spans filled from transcript, "
        f"{len(rows)} loaded-context items collapsed into 1 node (source: {source}), "
        f"{decomposed} llm_requests cache-decomposed, "
        f"{len(deltas)} llm_requests context-delta stamped, "
        f"{efforts} llm_requests effort-tagged, "
        f"{len(score_events)} numeric scores emitted, "
        f"{len(step_scores)} per-phase step cost/token scores emitted, "
        f"{len(commits)} commit nodes synthesized, "
        f"tagged mode={mode} lane={lane}; "
        f"{len(cycle_batch) - 2} observations assembled under cycle trace {cycle_trace_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
