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
from datetime import datetime
from pathlib import Path
from typing import Any, cast

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
from telemetry.spoke_tree.assembly import (
    _copy_event,
    _resolve_parent,
    _tool_additions,
    _tool_span_ids,
)
from telemetry.spoke_tree.cycle import (
    _POST_STEP_KEY,
    _PRE_STEP_KEY,
    _apply_cycle_axis,
    _cycle_step_for,
    _cycle_step_ids,
)
from telemetry.spoke_tree.folding import (
    _apply_guard_groups,
    _apply_levels,
    _fold_tool_subspans,
    _hook_event_exclude,
    _stamp_hook_endtimes,
)
from telemetry.spoke_tree.ids import (
    _CYCLE_STEP_PREFIX,
    _copy_id,
    cycle_copy_id_for,
    cycle_root_id_for,
    cycle_trace_id_for,
    root_id_for,
    trace_id_for,
)
from telemetry.spoke_tree.indices import (
    _SKILL_TOOL_NAME,
    _activated_skill_name,
    _build_interaction_index,
    _build_request_index,
    _build_skill_index,
    _build_tool_index,
    _synthesize_blocked_tools,
)
from telemetry.spoke_tree.observations import (
    _INTERACTION_NAME,
    _POST_STEP_NAME,
    _PRE_STEP_NAME,
    IngestEvent,
    ToolContent,
    TraceObservations,
    _earliest_start,
    _elapsed_ms,
    _is_gate_observation,
    _latest_time,
    _parse_ts,
    _tool_use_id,
)
from telemetry.spoke_tree.rollups import (
    _apply_container_rollups,
    _strip_container_usage,
)
from telemetry.spoke_tree.steps import (
    _STEP_PREFIX,
    _apply_step_grouping,
    _collapse_startup_instants,
    build_step_windows,
)

logger = logging.getLogger("langfuse_spoke_tree")


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
