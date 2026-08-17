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
  re-homes the copies onto a pure cycle axis — ``preStep`` + one ``step:<subject>`` per cycle window
  (:func:`build_cycle_windows`, marker-preferred, #235) + ``postStep`` — placing each real span
  (and each turn-marker) by its timestamp and letting audit
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

Module layout (#166): this file is the ORCHESTRATOR — it holds the two view assemblers
(:func:`build_batch` / :func:`build_cycle_batch` / :func:`_assemble_copies`), the Langfuse
fetch/post I/O, the transcript scan, and :func:`main`. Everything else lives in the
``telemetry.spoke_tree`` package, one module per family (foundation ``ids`` / ``observations``;
core plumbing ``indices`` / ``folding`` / ``assembly``; ``rollups``; view lenses ``steps`` /
``cycle``; enrichments ``loaded_context`` / ``llm_decomp`` / ``context_deltas`` / ``metadata`` /
``commits`` / ``scores``). :func:`main` drives the post-build enrichments through the ordered
:data:`_ENRICHMENTS` registry over a shared :class:`EnrichmentContext`, so a future enrichment is
one new module + one registry line.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telemetry.langfuse_rollup import (
    DeleteFn,
    GetFn,
    PostFn,
    all_observations,
    make_delete,
    make_get,
    make_post,
)
from telemetry.measure_context_cost import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    TokenCounter,
    make_counter,
)
from telemetry.session_parser import project_dir_for_worktree
from telemetry.spoke_tree.assembly import (
    _copy_event,
    _resolve_parent,
    _tool_additions,
    _tool_span_ids,
)
from telemetry.spoke_tree.commits import _commit_events, _gate_park_event, _load_commits
from telemetry.spoke_tree.context_deltas import (
    _apply_context_rollups,
    apply_context_deltas,
    load_scoped_rules,
)
from telemetry.spoke_tree.cycle import (
    _apply_cycle_axis,
    _cycle_step_for,
    _cycle_step_ids,
)
from telemetry.spoke_tree.folding import (
    _apply_guard_groups,
    _apply_levels,
    _apply_mcp_groups,
    _fold_tool_subspans,
    _hook_event_exclude,
    _stamp_hook_endtimes,
)
from telemetry.spoke_tree.ids import (
    _copy_id,
    cycle_copy_id_for,
    cycle_root_id_for,
    cycle_trace_id_for,
    root_id_for,
    trace_id_for,
)
from telemetry.spoke_tree.indices import (
    _build_interaction_index,
    _build_request_index,
    _build_skill_index,
    _build_tool_index,
    _synthesize_blocked_tools,
)
from telemetry.spoke_tree.llm_decomp import _memoized_counter, apply_llm_decomposition
from telemetry.spoke_tree.loaded_context import (
    _DISK_CATEGORY_ORDER,
    _REQUEST_CATEGORY_ORDER,
    build_loaded_context_events,
    loaded_context_rows,
    prefix_total,
    request_context_rows,
)
from telemetry.spoke_tree.metadata import (
    apply_lifecycle_metadata,
    apply_mode_lane_tags,
    apply_outcome_tag,
    apply_repo_tag,
    apply_request_body_metadata,
    build_lifecycle_timeline,
    read_mode_lane,
    read_outcome,
)
from telemetry.spoke_tree.observations import (
    IngestEvent,
    Lifecycle,
    ToolContent,
    TraceObservations,
    _earliest_start,
    _is_cycle_step_marker,
    _latest_time,
)
from telemetry.spoke_tree.rollups import (
    _apply_container_rollups,
    _strip_container_usage,
)
from telemetry.spoke_tree.scores import (
    build_agent_cost_scores,
    build_agent_verdict_scores,
    build_enforcement_fire_scores,
    build_lifecycle_stage_scores,
    build_mcp_call_scores,
    build_mcp_carry_cost_scores,
    build_mcp_def_load_scores,
    build_normalization_scores,
    build_outcome_count_scores,
    build_rule_carry_cost_scores,
    build_rule_invocation_scores,
    build_score_events,
    build_script_success_scores,
    build_skill_cost_scores,
    build_skill_success_scores,
    build_step_cost_scores,
    build_step_duration_scores,
    build_step_total_cost_scores,
    build_tooldef_carry_cost_scores,
    build_window_rollup_scores,
    main_loop_request_count,
)
from telemetry.spoke_tree.steps import (
    _apply_step_grouping,
    _collapse_startup_instants,
    build_cycle_windows,
)

logger = logging.getLogger("langfuse_spoke_tree")


_CYCLE_TRACE_NAME_PREFIX = "spoke-cycle:"
_CYCLE_ROOT_NAME_PREFIX = "cycle:"
_TRACE_NAME_PREFIX = "spoke-tree:"
_ROOT_NAME_PREFIX = "spoke:"

# Langfuse environment (#231): the assembled views are always real prod spoke data, so both view
# trace-create bodies are stamped ``production``. A dashboard scoped to environment=production then
# excludes test/fixture traffic (which lacks the field), the same signal the otelcol stamps on live
# spans. Kept a constant here — the builder never assembles a view for anything but a real spoke.
_ENVIRONMENT = "production"


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

# Default cache-creation price (USD per token), Opus tier — mirrors measure_context_cost.
_DEFAULT_PRICE = 0.00000625
# Env var naming the per-spoke dir of OTEL_LOG_RAW_API_BODIES=file:<dir> dumps.
_BODY_DIR_ENV = "AI_TOOLKIT_OTEL_BODY_DIR"
# Conventional per-spoke body dir under a worktree root (worktree-new.sh writes here).
_BODY_DIR_CONVENTION = Path(".ai-toolkit/raw-bodies")


def build_batch(
    traces: list[TraceObservations],
    spoke_run_id: str,
    tool_content: dict[str, ToolContent] | None = None,
    *,
    keep_noop_guards: bool = False,
    commits: list[dict[str, Any]] | None = None,
    answer_epoch: int | None = None,
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
            "environment": _ENVIRONMENT,
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
    # The solo-cycle marker spans (step:<phase>) are telemetry plumbing consumed to build the
    # cycle spine (#235); drop them here so a marker never renders as an orphan ``step:green``
    # sibling of the ledger-labelled ``step:GREEN`` grouping node.
    copies = [event for event in copies if not _is_cycle_step_marker(event["body"])]
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
        traces,
        spoke_run_id=spoke_run_id,
        trace_id=trace_id,
        cycle=False,
        parent_id=root_id,
        answer_epoch=answer_epoch,
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
    # After guard grouping / level stamping so an MCP tool's guards already nest under it and ride it
    # under the server group, and the group can read each member's folded success/error (#234).
    copies = _apply_mcp_groups(copies, trace_id=trace_id)
    copies = _collapse_startup_instants(copies, root_event)
    return _strip_container_usage(copies)


def build_cycle_batch(
    traces: list[TraceObservations],
    spoke_run_id: str,
    tool_content: dict[str, ToolContent] | None = None,
    *,
    keep_noop_guards: bool = False,
    commits: list[dict[str, Any]] | None = None,
    answer_epoch: int | None = None,
) -> list[IngestEvent]:
    """Assemble the View B (steps -> work) ``spokecycle-<spoke>`` trace (#113).

    Built from the SAME observation copies as :func:`build_batch` (same rich input/output,
    usageDetails, costDetails, metadata) but re-homed onto a pure cycle axis: the top level is
    ``preStep`` + one ``step:<subject>`` per cycle window (marker-preferred, #235) + ``postStep``,
    totally partitioning the
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
            "environment": _ENVIRONMENT,
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
    # Drop the marker spans before re-homing (#235): their startTimes seed the cycle windows
    # (read from ``traces`` below), but the raw nodes must not land on the axis as leaf duplicates
    # of the step they define.
    copies = [event for event in copies if not _is_cycle_step_marker(event["body"])]
    windows = build_cycle_windows(traces, tool_content)
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
        traces,
        spoke_run_id=spoke_run_id,
        trace_id=trace_id,
        cycle=True,
        parent_id=root_id,
        answer_epoch=answer_epoch,
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


def _coerce_int(value: object) -> int | None:
    """Coerce a lifecycle-JSON epoch/count field to int, or None when absent/unparseable.

    A ``bool`` is guarded out (a stray ``true`` is never a real epoch/count, and ``int(True)`` would
    read as 1); anything that is not already an int / float / numeric string yields None.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _load_lifecycle(path: Path | None) -> Lifecycle:
    """Read the ingest shell's lifecycle-sources JSON into a :class:`Lifecycle`, best-effort (#280).

    The shell gathers the on-disk / ``gh`` sources (dispatch + answer-attempt epochs, ``filed`` ISO,
    the land instant, and the drain-window snapshot) into ``.ai-toolkit/lifecycle.json`` and passes
    ``--lifecycle`` at land time. A missing path (a pre-#280 land, or the degraded id-only re-run), an
    unreadable / malformed file, or a non-object body all yield an empty :class:`Lifecycle` so every
    dependent metric skips rather than crashing the best-effort land-time build. Epoch / count fields
    are coerced to int; ``issue`` / ``filed`` stay strings.

    Args:
        path: The ``--lifecycle`` JSON path, or None when the flag was not passed.

    Returns:
        The parsed :class:`Lifecycle` (all-None when the source is absent or unusable).
    """
    if path is None:
        return Lifecycle()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("cannot read lifecycle sources %s", path)
        return Lifecycle()
    if not isinstance(parsed, dict):
        return Lifecycle()
    issue = parsed.get("issue")
    filed = parsed.get("filed")
    return Lifecycle(
        issue=str(issue) if issue else None,
        filed=str(filed) if filed else None,
        dispatched=_coerce_int(parsed.get("dispatched")),
        answer_attempt=_coerce_int(parsed.get("answer_attempt")),
        landed=_coerce_int(parsed.get("landed")),
        window_start=_coerce_int(parsed.get("window_start")),
        spokes_serviced=_coerce_int(parsed.get("spokes_serviced")),
        interventions=_coerce_int(parsed.get("interventions")),
    )


def filled_tool_spans(traces: list[TraceObservations], tool_content: dict[str, ToolContent]) -> int:
    """Count the tool spans whose create body would gain transcript content (see summary)."""
    return sum(
        bool(_tool_additions(observation, tool_content))
        for _orig_trace_id, observations in traces
        for observation in observations
    )


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
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "The originating repository name, stamped as a repo:<name> trace tag so cross-project "
            "cost/latency is comparable (#231). Resolved by the shell wrapper (git remote, else the "
            "checkout dir basename); omitted for an ad-hoc run leaves the trace untagged."
        ),
    )
    parser.add_argument(
        "--lifecycle",
        type=Path,
        default=None,
        help=(
            "A JSON file of the per-issue cycle-time sources the ingest shell gathered off disk "
            "(dispatch/answer-attempt epochs, filed ISO, land instant, drain-window snapshot) — "
            "stamped as the lifecycle timeline + per-stage / window scores (#280). Omitted for a "
            "pre-#280 land or the degraded id-only re-run leaves those metrics unstamped."
        ),
    )
    return parser.parse_args(argv)


@dataclass
class EnrichmentContext:
    """The shared inputs + accumulated outputs threaded through the enrichment passes.

    The read-only inputs (``batch`` / ``cycle_batch`` are mutated in place by the passes) plus the
    per-pass result fields the summary line reports. One object is built once in :func:`main` and
    handed to each entry in :data:`_ENRICHMENTS` in order.
    """

    spoke_run_id: str
    traces: list[TraceObservations]
    batch: list[IngestEvent]
    cycle_batch: list[IngestEvent]
    tool_content: dict[str, ToolContent]
    bodies_dir: Path
    counter: TokenCounter
    price: float
    base_ts: str
    root: Path
    n_requests: int = 0
    commits: list[dict[str, Any]] = field(default_factory=list)
    # Whether a commits dump was actually handed to the builder (#344). Distinct from an empty
    # `commits` list: absence of a dump (the empty-range bug, or a bare-branch/--local checkout the
    # ingest resolve-or-skips) is NOT evidence of zero churn, so the normalization base counts are
    # skipped rather than emitted as misleading zeros.
    commits_dump_present: bool = False
    lifecycle: Lifecycle = field(default_factory=Lifecycle)
    # Accumulated outputs (populated by the passes, read by the summary line).
    context_events: list[IngestEvent] = field(default_factory=list)
    rows: list[dict[str, object]] = field(default_factory=list)
    source: str = ""
    decomposed: int = 0
    efforts: int = 0
    deltas: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)
    score_events: list[IngestEvent] = field(default_factory=list)
    # #280 per-issue cycle-time scores (stage overhead + drain-window rollups). Accumulated on their
    # OWN field, NOT appended to ``score_events``, so ``score_events`` stays exactly
    # ``build_score_events(...)`` for any input — keeping the out-of-scope registry equality assertion
    # (test_spoke_tree_registry.py) transparently true rather than true only for a source-less fixture.
    lifecycle_scores: list[IngestEvent] = field(default_factory=list)
    step_scores: list[IngestEvent] = field(default_factory=list)
    carry_scores: list[IngestEvent] = field(default_factory=list)
    invocation_scores: list[IngestEvent] = field(default_factory=list)
    enforcement_scores: list[IngestEvent] = field(default_factory=list)
    script_success_scores: list[IngestEvent] = field(default_factory=list)
    skill_success_scores: list[IngestEvent] = field(default_factory=list)
    skill_cost_scores: list[IngestEvent] = field(default_factory=list)
    mcp_call_scores: list[IngestEvent] = field(default_factory=list)
    mcp_def_load_scores: list[IngestEvent] = field(default_factory=list)
    agent_verdict_scores: list[IngestEvent] = field(default_factory=list)
    agent_cost_scores: list[IngestEvent] = field(default_factory=list)
    outcome_count_scores: list[IngestEvent] = field(default_factory=list)
    normalization_scores: list[IngestEvent] = field(default_factory=list)


def _enrich_loaded_context(ctx: EnrichmentContext) -> None:
    """Collapse the startup inventory into one ``loaded-context`` node (#87), request body else disk."""
    request_rows = request_context_rows(ctx.bodies_dir, counter=ctx.counter, price=ctx.price)
    if request_rows is not None:
        ctx.rows, ctx.source = request_rows, "request body"
        ctx.context_events = build_loaded_context_events(
            ctx.spoke_run_id, ctx.rows, category_order=_REQUEST_CATEGORY_ORDER, base_ts=ctx.base_ts
        )
    else:
        ctx.rows, ctx.source = (
            loaded_context_rows(ctx.root, counter=ctx.counter, price=ctx.price),
            "disk",
        )
        ctx.context_events = build_loaded_context_events(
            ctx.spoke_run_id,
            ctx.rows,
            category_order=_DISK_CATEGORY_ORDER,
            base_ts=ctx.base_ts,
            prefix_total=prefix_total(ctx.traces),
            price=ctx.price,
        )


def _enrich_llm_decomposition(ctx: EnrichmentContext) -> None:
    """Fold each llm_request's #99 cache_read/cache_creation decomposition onto its View A copy."""
    ctx.decomposed = apply_llm_decomposition(
        ctx.batch, ctx.traces, ctx.bodies_dir, counter=ctx.counter, price=ctx.price
    )


def _enrich_request_body_metadata(ctx: EnrichmentContext) -> None:
    """Surface each request body's #101 effort + cache-breakpoint signals on its llm_request copy."""
    ctx.efforts = apply_request_body_metadata(ctx.batch, ctx.traces, ctx.bodies_dir)


def _enrich_context_deltas(ctx: EnrichmentContext) -> None:
    """Stamp #160 per-request context deltas and aggregate them onto both views' step rollups.

    The delta pass also labels each added message that injected a #232 glob-scoped rule, so the
    later invocation-scores pass can count them; the scoped-rule set is read once from the worktree.
    """
    ctx.deltas = apply_context_deltas(
        ctx.batch,
        ctx.traces,
        ctx.bodies_dir,
        counter=ctx.counter,
        price=ctx.price,
        tool_content=ctx.tool_content,
        scoped_rules=load_scoped_rules(ctx.root),
    )
    _apply_context_rollups(ctx.batch, {_copy_id(o, i): s for (o, i), s in ctx.deltas.items()})
    _apply_context_rollups(
        ctx.cycle_batch, {cycle_copy_id_for(o, i): s for (o, i), s in ctx.deltas.items()}
    )


def _enrich_scores(ctx: EnrichmentContext) -> None:
    """Emit the trace/observation numeric scores (#100, #101) plus the #280 cycle-time scores.

    The base signals (``permission_wait_ms`` / ``tool_result_size`` / ``gate_park_ms``) are assigned
    to ``score_events``, and the #280 per-stage overhead + drain-window rollup scores accumulate on
    the SEPARATE ``lifecycle_scores`` field (the window ratio reads the stage scores it just built).
    Folded into this existing pass — not a new ``_ENRICHMENTS`` entry, so the registry order is
    unchanged — and kept off ``score_events`` so that field stays exactly ``build_score_events(...)``,
    leaving the out-of-scope registry equality assertion transparently true.
    """
    ctx.score_events = build_score_events(
        ctx.spoke_run_id,
        ctx.traces,
        ctx.batch,
        base_ts=ctx.base_ts,
        answer_epoch=ctx.lifecycle.answer_attempt,
    )
    stage_scores = build_lifecycle_stage_scores(
        ctx.spoke_run_id, ctx.traces, ctx.commits, ctx.lifecycle, base_ts=ctx.base_ts
    )
    window_scores = build_window_rollup_scores(
        ctx.spoke_run_id, ctx.batch, stage_scores, ctx.lifecycle, base_ts=ctx.base_ts
    )
    ctx.lifecycle_scores = stage_scores + window_scores


def _enrich_step_scores(ctx: EnrichmentContext) -> None:
    """Emit per-phase step cache-write/token (#158) + true total cost + duration (#230) scores."""
    ctx.step_scores = (
        build_step_cost_scores(
            ctx.spoke_run_id, ctx.cycle_batch, base_ts=ctx.base_ts, price=ctx.price
        )
        + build_step_total_cost_scores(ctx.spoke_run_id, ctx.cycle_batch, base_ts=ctx.base_ts)
        + build_step_duration_scores(ctx.spoke_run_id, ctx.cycle_batch, base_ts=ctx.base_ts)
    )


def _enrich_carry_cost(ctx: EnrichmentContext) -> None:
    """Emit per-rule + per-tooldef + per-MCP-server carry-cost scores from loaded-context rows (#232/#234).

    Reads the same measured rows the loaded-context node collapsed (populated by
    :func:`_enrich_loaded_context`, so this pass runs after it) and the once-computed request count.
    """
    ctx.carry_scores = (
        build_rule_carry_cost_scores(
            ctx.spoke_run_id, ctx.rows, ctx.n_requests, base_ts=ctx.base_ts, price=ctx.price
        )
        + build_tooldef_carry_cost_scores(
            ctx.spoke_run_id, ctx.rows, ctx.n_requests, base_ts=ctx.base_ts, price=ctx.price
        )
        + build_mcp_carry_cost_scores(
            ctx.spoke_run_id, ctx.rows, ctx.n_requests, base_ts=ctx.base_ts, price=ctx.price
        )
    )


def _enrich_invocation_scores(ctx: EnrichmentContext) -> None:
    """Emit per-rule ``rule_invocations:<rule>`` scores from the #232 context-delta rule labels.

    Reads the rule labels the context-deltas pass stamped on the batch, so it runs after it.
    """
    ctx.invocation_scores = build_rule_invocation_scores(
        ctx.spoke_run_id, ctx.batch, base_ts=ctx.base_ts
    )


def _enrich_enforcement_scores(ctx: EnrichmentContext) -> None:
    """Emit per-surface ``enforcement_fires:<event>:<tool>`` scores from the #232 hook-block events."""
    ctx.enforcement_scores = build_enforcement_fire_scores(
        ctx.spoke_run_id, ctx.batch, base_ts=ctx.base_ts
    )


def _enrich_script_success(ctx: EnrichmentContext) -> None:
    """Emit per-script ``script_success:<name>`` 0/1 scores from each script span's status (#233)."""
    ctx.script_success_scores = build_script_success_scores(
        ctx.spoke_run_id, ctx.batch, base_ts=ctx.base_ts
    )


def _enrich_skill_success(ctx: EnrichmentContext) -> None:
    """Emit per-skill ``skill_success:<name>`` (#234) + ``skill_cost_usd:<name>`` (#322) scores.

    Both read the same assembled View A batch. The #322 per-skill cost score is FOLDED into this
    existing pass — not a new ``_ENRICHMENTS`` entry — so the registry order stays the documented
    sequence and the out-of-scope registry equality assertion is untouched (mirroring how the #280
    lifecycle scores fold into the ``scores`` pass).
    """
    ctx.skill_success_scores = build_skill_success_scores(
        ctx.spoke_run_id, ctx.batch, base_ts=ctx.base_ts
    )
    ctx.skill_cost_scores = build_skill_cost_scores(
        ctx.spoke_run_id, ctx.batch, base_ts=ctx.base_ts
    )


def _enrich_mcp_calls(ctx: EnrichmentContext) -> None:
    """Emit per-server ``mcp_success`` / ``mcp_calls`` scores from the assembled MCP group nodes (#234)."""
    ctx.mcp_call_scores = build_mcp_call_scores(ctx.spoke_run_id, ctx.batch, base_ts=ctx.base_ts)


def _enrich_mcp_def_loads(ctx: EnrichmentContext) -> None:
    """Emit per-server ``mcp_def_loads`` scores from the #234 context-delta mcp def-load labels.

    Reads the labels the context-deltas pass stamped on the batch, so it runs after it.
    """
    ctx.mcp_def_load_scores = build_mcp_def_load_scores(
        ctx.spoke_run_id, ctx.batch, base_ts=ctx.base_ts
    )


def _enrich_outcome_counts(ctx: EnrichmentContext) -> None:
    """Emit the #231 trace-level gate_park_count / blocked_count / relaunch_count scores."""
    ctx.outcome_count_scores = build_outcome_count_scores(
        ctx.spoke_run_id, ctx.traces, ctx.root, base_ts=ctx.base_ts
    )


def _enrich_normalization(ctx: EnrichmentContext) -> None:
    """Emit the #231 files/lines/commits/subtasks + derived cost-per-line / wall-per-subtask scores.

    ``subtasks`` is the cycle-window count (the ledger subtask count); the batch's duration rollup
    and generation costs are read here, so this runs after the batch is assembled.
    """
    subtasks = len(build_cycle_windows(ctx.traces, ctx.tool_content))
    ctx.normalization_scores = build_normalization_scores(
        ctx.spoke_run_id,
        ctx.commits,
        ctx.batch,
        subtasks,
        base_ts=ctx.base_ts,
        commits_dump_present=ctx.commits_dump_present,
    )


def _enrich_agent_verdict(ctx: EnrichmentContext) -> None:
    """Emit per-agent ``agent_verdict:<type>`` (#233) + ``agent_cost_usd:<type>`` (#323) scores.

    Both read the assembled View A batch's ``sub-agent:`` containers (the verdict pass also reads the
    worktree's ``.review`` artifacts for code-review verdicts). The #323 per-agent cost score is
    FOLDED into this existing pass — not a new ``_ENRICHMENTS`` entry — so the registry order stays
    the documented sequence and the out-of-scope registry equality assertion is untouched (mirroring
    how the #322 skill-cost score folds into the ``skill-success`` pass).
    """
    ctx.agent_verdict_scores = build_agent_verdict_scores(
        ctx.spoke_run_id, ctx.batch, ctx.root / ".review", base_ts=ctx.base_ts
    )
    ctx.agent_cost_scores = build_agent_cost_scores(
        ctx.spoke_run_id, ctx.batch, base_ts=ctx.base_ts
    )


# The enrichment passes, in the exact order main applies them. Adding a future enrichment is one
# module + one line here; the passes mutate the batches / accumulate onto the shared context above
# (a plain ordered list, deliberately not a self-registering registry — the cross-pass data flow
# through EnrichmentContext makes explicit ordering the honest shape).
_ENRICHMENTS: tuple[tuple[str, Callable[[EnrichmentContext], None]], ...] = (
    ("loaded-context", _enrich_loaded_context),
    ("llm-decomposition", _enrich_llm_decomposition),
    ("request-body-metadata", _enrich_request_body_metadata),
    ("context-deltas", _enrich_context_deltas),
    ("scores", _enrich_scores),
    ("step-scores", _enrich_step_scores),
    ("carry-cost", _enrich_carry_cost),
    ("invocation-scores", _enrich_invocation_scores),
    ("enforcement-scores", _enrich_enforcement_scores),
    ("script-success", _enrich_script_success),
    ("skill-success", _enrich_skill_success),
    ("mcp-calls", _enrich_mcp_calls),
    ("mcp-def-loads", _enrich_mcp_def_loads),
    ("agent-verdict", _enrich_agent_verdict),
    ("outcome-counts", _enrich_outcome_counts),
    ("normalization", _enrich_normalization),
)


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
    # Loaded before the batch builds (moved up from the timeline step) so the drain's PLAN-gate
    # answer epoch widens the wait:gate-park node to the real park window (#345).
    lifecycle = _load_lifecycle(args.lifecycle)
    batch = build_batch(
        traces,
        args.spoke_run_id,
        tool_content,
        keep_noop_guards=args.keep_noop_guards,
        commits=commits,
        answer_epoch=lifecycle.answer_attempt,
    )
    cycle_batch = build_cycle_batch(
        traces,
        args.spoke_run_id,
        tool_content,
        keep_noop_guards=args.keep_noop_guards,
        commits=commits,
        answer_epoch=lifecycle.answer_attempt,
    )
    mode, lane = read_mode_lane(args.root.resolve())
    apply_mode_lane_tags(batch, mode, lane)
    apply_mode_lane_tags(cycle_batch, mode, lane)
    outcome = read_outcome(args.root.resolve())
    apply_outcome_tag(batch, outcome)
    apply_outcome_tag(cycle_batch, outcome)
    apply_repo_tag(batch, args.repo)
    apply_repo_tag(cycle_batch, args.repo)
    timeline = build_lifecycle_timeline(lifecycle, commits, traces)
    apply_lifecycle_metadata(batch, cycle_batch, timeline)

    # One counter, memoized by content hash and shared across the loaded-context measurement, the
    # #99 decomposition, and the #160 context deltas — the stable prefix repeats on every snapshot.
    counter = _memoized_counter(
        make_counter(endpoint=args.endpoint, api_key=args.api_key, model=args.model)
    )
    base_ts = _earliest_start(traces)
    bodies_dir = args.request_bodies or (args.root.resolve() / _BODY_DIR_CONVENTION)
    # UPGRADE: apply_llm_decomposition + apply_request_body_metadata each re-walk
    # _llm_requests_in_order and re-stat find_request_files over the same inputs — compute the
    # calls↔bodies pairing once and pass it in if the body count ever makes it measurable.
    ctx = EnrichmentContext(
        spoke_run_id=args.spoke_run_id,
        traces=traces,
        batch=batch,
        cycle_batch=cycle_batch,
        tool_content=tool_content,
        bodies_dir=bodies_dir,
        counter=counter,
        price=args.price,
        base_ts=base_ts,
        root=args.root.resolve(),
        n_requests=main_loop_request_count(traces),
        commits=commits,
        commits_dump_present=args.commits is not None,
        lifecycle=lifecycle,
    )
    for _name, enrich in _ENRICHMENTS:
        enrich(ctx)
    post_in_chunks(
        batch
        + ctx.context_events
        + ctx.score_events
        + ctx.lifecycle_scores
        + cycle_batch
        + ctx.step_scores
        + ctx.carry_scores
        + ctx.invocation_scores
        + ctx.enforcement_scores
        + ctx.script_success_scores
        + ctx.skill_success_scores
        + ctx.skill_cost_scores
        + ctx.mcp_call_scores
        + ctx.mcp_def_load_scores
        + ctx.agent_verdict_scores
        + ctx.agent_cost_scores
        + ctx.outcome_count_scores
        + ctx.normalization_scores,
        post,
    )

    trace_id = trace_id_for(args.spoke_run_id)
    cycle_trace_id = cycle_trace_id_for(args.spoke_run_id)
    filled = filled_tool_spans(traces, tool_content)
    print(
        f"{len(batch) - 2} observations assembled under trace {trace_id} "
        f"(roots collapsed to 1), {filled} tool spans filled from transcript, "
        f"{len(ctx.rows)} loaded-context items collapsed into 1 node (source: {ctx.source}), "
        f"{ctx.decomposed} llm_requests cache-decomposed, "
        f"{len(ctx.deltas)} llm_requests context-delta stamped, "
        f"{ctx.efforts} llm_requests effort-tagged, "
        f"{len(ctx.score_events)} numeric scores emitted, "
        f"{len(ctx.step_scores)} per-phase step cost/token/duration scores emitted, "
        f"{len(ctx.carry_scores)} rule/tooldef carry-cost scores emitted "
        f"(n_requests={ctx.n_requests}), "
        f"{len(ctx.invocation_scores)} rule-invocation scores emitted, "
        f"{len(ctx.enforcement_scores)} enforcement-fire scores emitted, "
        f"{len(ctx.script_success_scores)} script-success scores emitted, "
        f"{len(ctx.skill_success_scores)} skill-success scores emitted, "
        f"{len(ctx.skill_cost_scores)} skill-cost scores emitted, "
        f"{len(ctx.mcp_call_scores)} mcp-call scores emitted, "
        f"{len(ctx.mcp_def_load_scores)} mcp-def-load scores emitted, "
        f"{len(ctx.agent_verdict_scores)} agent-verdict scores emitted, "
        f"{len(ctx.agent_cost_scores)} agent-cost scores emitted, "
        f"{len(ctx.outcome_count_scores)} outcome-count scores emitted, "
        f"{len(ctx.normalization_scores)} normalization scores emitted, "
        f"{len(commits)} commit nodes synthesized, "
        f"{len(timeline)} lifecycle timeline legs stamped, "
        f"{len(ctx.lifecycle_scores)} lifecycle stage/window scores emitted, "
        f"tagged mode={mode} lane={lane} outcome={outcome} repo={args.repo}; "
        f"{len(cycle_batch) - 2} observations assembled under cycle trace {cycle_trace_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
