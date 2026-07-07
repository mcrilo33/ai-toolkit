"""Synthetic timeline nodes: git commits and the PLAN-gate park (#162).

:func:`_commit_events` turns a ``git log --numstat`` dump (:func:`_parse_commits`) into one
``commit:<sha7>`` instant per commit, placed by author time; :func:`_gate_park_event` builds the
``wait:gate-park`` block spanning the gate's end to the resumption after approval
(:func:`_gate_park_bounds`). :func:`_gate_park_ms` exposes that span as the trace-level score value
consumed by :mod:`~telemetry.spoke_tree.scores`. Depends only on the foundation modules.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from telemetry.langfuse_rollup import Observation
from telemetry.spoke_tree.observations import (
    _INTERACTION_NAME,
    IngestEvent,
    TraceObservations,
    _elapsed_ms,
    _is_gate_observation,
    _parse_ts,
)

logger = logging.getLogger("langfuse_spoke_tree")

# Synthetic timeline node id prefixes (#162), keyed off the spoke run id (+ sha) so a rerun
# overwrites the same node. The ``wait:`` name prefix routes the park into the duration ``wait``
# bucket (see rollups._duration_class); the field separator is the byte git emits between
# --format fields in the commit dump (_parse_commits).
_COMMIT_PREFIX = "tree-commit-"
_GATE_PARK_PREFIX = "tree-gatepark-"
_GATE_PARK_NODE_NAME = "wait:gate-park"
_COMMIT_FIELD_SEP = "\x1f"
_COMMIT_LINE_MARKER = "commit"


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
