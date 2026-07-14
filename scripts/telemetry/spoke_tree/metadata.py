"""Trace-level metadata: mode/lane tags and per-request effort / cache-breakpoint signals (#101, #102).

:func:`read_mode_lane` + :func:`apply_mode_lane_tags` surface how a spoke was run (afk/attended,
lane) as trace tags + metadata; :func:`apply_request_body_metadata` folds each request body's
``output_config.effort`` and ``cache_control`` breakpoints onto its llm_request copy (the
``ultra`` mode diverts to an ``ultracode`` trace tag). Depends on the foundation,
:mod:`~telemetry.spoke_tree.loaded_context`, and ``request_body``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from telemetry.request_body import parse_request_body
from telemetry.spoke_tree.commits import _first_commit_at
from telemetry.spoke_tree.ids import _copy_id
from telemetry.spoke_tree.loaded_context import find_request_files
from telemetry.spoke_tree.observations import (
    IngestEvent,
    Lifecycle,
    TraceObservations,
    _epoch_to_iso,
    _llm_requests_in_order,
    _normalize_iso,
    _ready_at,
)

logger = logging.getLogger("langfuse_spoke_tree")

# output_config.effort handling (#101). ``ultra`` is the ultracode/harness mode, NOT an
# effort level: it is diverted to a spoke-level ``ultracode`` trace tag, never recorded as
# an ``effort:<value>`` tag or on llm_request metadata.
_ULTRA_MODE = "ultra"
_ULTRACODE_TAG = "ultracode"
# Execution mode + lane pointer files under a worktree root, stamped at launch by
# worktree-new.sh / worktree-quick.sh (#102). Surfaced as trace tags + metadata.
_MODE_POINTER = Path(".ai-toolkit/mode")
_LANE_POINTER = Path(".ai-toolkit/lane")
_VALID_MODES = ("afk", "attended")
_VALID_LANES = ("micro", "express", "quick", "spoke")
_DEFAULT_MODE = "attended"
_DEFAULT_LANE = "spoke"
# Terminal-outcome pointer file under a worktree root (#231), stamped at land / reap time by
# worktree-land.sh / hub-afk.sh — the state each already knows. Surfaced as an ``outcome:<v>``
# trace tag + bare metadata so failure economics are queryable across spokes.
_OUTCOME_POINTER = Path(".ai-toolkit/outcome")
_VALID_OUTCOMES = ("landed", "blocked", "reaped", "abandoned")


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


def apply_repo_tag(batch: list[IngestEvent], repo: str | None) -> None:
    """Attach the originating repo to a spoke's trace so cross-project comparison is queryable (#231).

    Surfaces as a ``repo:<name>`` trace tag (a dashboard can then group/filter/chart spokes by
    repository) and is mirrored, bare, into trace metadata for direct lookup. The name is resolved
    by the shell wrapper (git remote, else the checkout dir basename); a None/empty name (an ad-hoc
    or non-git checkout) leaves the trace untouched rather than emitting a bare ``repo:`` tag.

    Args:
        batch: The assembled ingestion events; the ``trace-create`` event is mutated in place.
        repo: The originating repository name, or None to skip.
    """
    if not repo:
        return
    _merge_trace_tags(batch, [f"repo:{repo}"])
    trace = next((event for event in batch if event.get("type") == "trace-create"), None)
    if trace is None:
        return  # defensive: build_batch always emits one
    trace["body"].setdefault("metadata", {})["repo"] = repo


def read_outcome(root: Path) -> str | None:
    """Return the spoke's terminal outcome from its ``.ai-toolkit/outcome`` pointer, or None (#231).

    Written at land / reap time by ``worktree-land.sh`` / ``hub-afk.sh`` — the state each
    already knows. A missing, unreadable, blank, or unrecognized pointer resolves to None (no
    outcome tag) rather than a mislabel, so a legacy spoke stays honestly untagged.

    Args:
        root: The worktree root holding ``.ai-toolkit/outcome``.

    Returns:
        One of :data:`_VALID_OUTCOMES`, or None when the pointer is absent/blank/unknown.
    """
    try:
        value = (root / _OUTCOME_POINTER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if value in _VALID_OUTCOMES else None


def apply_outcome_tag(batch: list[IngestEvent], outcome: str | None) -> None:
    """Attach the spoke's terminal ``outcome`` to its trace (#231).

    Surfaces as an ``outcome:<value>`` trace tag (so a dashboard can group/filter/chart spokes
    by how they ended — landed vs blocked/reaped/abandoned) and is mirrored, bare, into trace
    metadata for direct lookup. A None outcome (no pointer) leaves the trace untouched.

    Args:
        batch: The assembled ingestion events; the ``trace-create`` event is mutated in place.
        outcome: The terminal outcome (one of :data:`_VALID_OUTCOMES`), or None to skip.
    """
    if outcome is None:
        return
    _merge_trace_tags(batch, [f"outcome:{outcome}"])
    trace = next((event for event in batch if event.get("type") == "trace-create"), None)
    if trace is None:
        return  # defensive: build_batch always emits one
    trace["body"].setdefault("metadata", {})["outcome"] = outcome


# The per-issue lifecycle timeline legs, in chronological order (#280). Stamped as an ordered
# ``metadata.lifecycle`` mapping on both views' trace so a dashboard reads the five instants
# ``filed → dispatched → first-commit → ready → landed`` as one row. Any leg whose source was
# absent is omitted (never a wrong/guessed value), so "no dispatch epoch" reads distinctly from a
# zero.
_LIFECYCLE_LEGS = ("filed", "dispatched", "first_commit", "ready", "landed")


def build_lifecycle_timeline(
    lifecycle: Lifecycle,
    commits: list[dict[str, Any]],
    traces: list[TraceObservations],
) -> dict[str, str]:
    """Assemble the ``filed → dispatched → first-commit → ready → landed`` timeline (ISO) (#280).

    Each leg is sourced independently and an absent one is omitted (graceful skip, never guessed):
    ``filed`` from the ``gh createdAt`` the shell gathered, ``dispatched`` / ``landed`` from the
    on-disk epochs (unix seconds -> ISO), ``first_commit`` from the earliest commit's author time
    (:func:`~telemetry.spoke_tree.commits._first_commit_at`), and ``ready`` from the completion
    ``spoke-ready`` span already in the traces (:func:`~telemetry.spoke_tree.observations._ready_at`).

    Args:
        lifecycle: The gathered per-issue sources (any field may be None).
        commits: The parsed ``git log --numstat`` records (for the first-commit leg).
        traces: The source traces (for the ready leg).

    Returns:
        An ordered mapping of the present legs to their ISO instants (empty when none resolved).
    """
    resolved: dict[str, str | None] = {
        "filed": _normalize_iso(lifecycle.filed),
        "dispatched": _epoch_to_iso(lifecycle.dispatched),
        "first_commit": _normalize_iso(_first_commit_at(commits)),
        "ready": _normalize_iso(_ready_at(traces)),
        "landed": _epoch_to_iso(lifecycle.landed),
    }
    return {leg: value for leg in _LIFECYCLE_LEGS if (value := resolved[leg])}


def apply_lifecycle_metadata(
    batch: list[IngestEvent], cycle_batch: list[IngestEvent], timeline: dict[str, str]
) -> None:
    """Stamp the per-issue lifecycle ``timeline`` onto both views' trace metadata (#280).

    Surfaces as ``metadata.lifecycle`` on each view's ``trace-create`` event so the five-instant
    timeline (:func:`build_lifecycle_timeline`) is a direct trace-metadata lookup, the same idiom
    :func:`apply_outcome_tag` uses. A None/empty timeline (a pre-#280 spoke, or every source absent)
    leaves both traces untouched rather than stamping an empty map.

    Args:
        batch: The assembled View A events; its ``trace-create`` is mutated in place.
        cycle_batch: The assembled View B events; its ``trace-create`` is mutated in place.
        timeline: The resolved lifecycle legs (present-only), from :func:`build_lifecycle_timeline`.
    """
    if not timeline:
        return
    for events in (batch, cycle_batch):
        trace = next((event for event in events if event.get("type") == "trace-create"), None)
        if trace is None:
            continue  # defensive: build_batch always emits one
        trace["body"].setdefault("metadata", {})["lifecycle"] = dict(timeline)


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
    for (orig_trace_id, observation), body_path in zip(calls, bodies, strict=False):
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
