#!/usr/bin/env python3
"""Roll up the per-``llm_request`` token decomposition onto Langfuse container spans.

Langfuse rolls *cost* and *latency* up onto container spans at render time, but it does NOT
roll up the token breakdown. This standalone post-run script fills that gap: for one session
(spoke run id) it walks every trace, builds the observation tree from ``parentObservationId``,
and for each container observation (one that HAS children) sums the token components over
its whole subtree (itself + all descendants). The sum is patched back as
``metadata.rollup = {reused, written, input, output}`` via the Langfuse ingestion API, where
``reused`` is ``cache_read_input_tokens`` and ``written`` is the total cache writes across
both ephemeral TTL tiers (``cache_creation_input_tokens`` 5m + ``input_cache_creation_1h`` 1h,
Issue #97).

Leaf tools (Bash, Read, ...) make no API call, so their subtree sums to zero -- correct.
Containers (``interaction`` / ``tool:Workflow`` / sub-agent) get their subtree totals. The
result shows in the span's metadata panel, not a native ∑ column (a Langfuse UI limit).

Run AFTER the trace is fully ingested::

    LANGFUSE_HOST=http://localhost:3000 LANGFUSE_BASIC_AUTH="Basic <base64(pk:sk)>" \\
        python3 scripts/telemetry/langfuse_rollup.py <spoke_run_id>

The patch is idempotent: re-running overwrites ``metadata.rollup`` (the ingestion event id is
derived from the observation id, so a rerun updates the same observation rather than appending).

Import-safe: no environment is read at import time, so the pure helpers can be unit-tested
without any Langfuse credentials. Configuration is read in :func:`main`. Stdlib only; reuses the
same env vars and ingestion endpoint as ``langfuse_message_bridge``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("langfuse_rollup")

# Langfuse ingestion requires a timestamp on every event; for an update event it only patches an
# existing observation, so the value is not meaningful and a fixed stamp is fine.
_INGEST_TIMESTAMP = "2026-01-01T00:00:00Z"

# Max page size the Langfuse observations endpoint accepts.
_PAGE_LIMIT = 100

# The token components summed bottom-up per ``llm_request`` generation. Cache writes split
# by ephemeral TTL (Issue #97): ``cache_creation_input_tokens`` is the 5m tier (1.25x input)
# and ``input_cache_creation_1h`` the 1h tier (2x); ``written`` below totals both.
_COMPONENTS = (
    "input",
    "output",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "input_cache_creation_1h",
)

Observation = dict[str, Any]
TokenTotals = dict[str, int]
# Fetch a Langfuse public-API path (e.g. ``/traces?...``) and return the decoded JSON object.
GetFn = Callable[[str], dict[str, Any]]
# Post a list of ingestion batch events to Langfuse.
PostFn = Callable[[list[dict[str, Any]]], None]
# Bulk-delete Langfuse traces by id (async on the server; poll the listing to confirm gone).
DeleteFn = Callable[[list[str]], None]


def build_tree(
    observations: list[Observation],
) -> tuple[dict[str, Observation], dict[str | None, list[str]]]:
    """Index observations by id and group child ids under their parent id.

    Args:
        observations: All observations of a single trace.

    Returns:
        A ``(by_id, children)`` pair: ``by_id`` maps observation id to the observation, and
        ``children`` maps a parent observation id (or None for roots) to its child ids.
    """
    by_id = {o["id"]: o for o in observations}
    children: dict[str | None, list[str]] = {}
    for o in observations:
        children.setdefault(o.get("parentObservationId"), []).append(o["id"])
    return by_id, children


def subtree_totals(
    observation_id: str,
    by_id: dict[str, Observation],
    children: dict[str | None, list[str]],
) -> TokenTotals:
    """Sum the four token components over an observation's subtree (itself + descendants).

    Args:
        observation_id: The id of the subtree root.
        by_id: Observation index from :func:`build_tree`.
        children: Parent-id to child-ids map from :func:`build_tree`.

    Returns:
        A mapping of each component name in ``_COMPONENTS`` to its summed token count.
    """
    totals: TokenTotals = dict.fromkeys(_COMPONENTS, 0)
    usage = by_id[observation_id].get("usageDetails") or {}
    for c in _COMPONENTS:
        totals[c] += int(usage.get(c) or 0)
    for child_id in children.get(observation_id, []):
        sub = subtree_totals(child_id, by_id, children)
        for c in _COMPONENTS:
            totals[c] += sub[c]
    return totals


def rollup_metadata(totals: TokenTotals) -> dict[str, int]:
    """The ``{reused, written, input, output}`` rollup summary for a subtree.

    ``written`` totals cache writes across both ephemeral TTL tiers — the 5m
    ``cache_creation_input_tokens`` plus the 1h ``input_cache_creation_1h`` (Issue #97).
    The single source of truth for both rollup writers (this module's update events
    plus the spoke-tree create-body rollups) so they cannot drift.

    Args:
        totals: Subtree token totals from :func:`subtree_totals`.
    """
    return {
        "reused": totals["cache_read_input_tokens"],
        "written": totals["cache_creation_input_tokens"] + totals.get("input_cache_creation_1h", 0),
        "input": totals["input"],
        "output": totals["output"],
    }


def rollup_event(observation: Observation, totals: TokenTotals) -> dict[str, Any]:
    """Shape a single ingestion event patching ``metadata.rollup`` onto an observation.

    The event type tracks the observation type: ``generation-update`` for a GENERATION,
    ``span-update`` for a SPAN (or any other / missing type). The event id is derived from
    the observation id so a rerun updates the same observation instead of appending.

    Args:
        observation: The container observation being patched.
        totals: Subtree token totals from :func:`subtree_totals`.

    Returns:
        A Langfuse ingestion batch event.
    """
    obs_type = observation.get("type") or "SPAN"
    event_type = "generation-update" if obs_type == "GENERATION" else "span-update"
    return {
        "id": f"rollup-{observation['id']}",
        "type": event_type,
        "timestamp": _INGEST_TIMESTAMP,
        "body": {
            "id": observation["id"],
            "metadata": {"rollup": rollup_metadata(totals)},
        },
    }


def all_observations(trace_id: str, get: GetFn) -> list[Observation]:
    """Fetch every observation of a trace, walking all pages.

    Args:
        trace_id: The Langfuse trace id.
        get: Path-to-JSON fetcher (see :data:`GetFn`).

    Returns:
        The trace's observations across all pages, in fetch order.
    """
    out: list[Observation] = []
    page = 1
    while True:
        resp = get(f"/observations?traceId={trace_id}&limit={_PAGE_LIMIT}&page={page}")
        out.extend(resp.get("data") or [])
        total_pages = (resp.get("meta") or {}).get("totalPages") or 1
        if page >= total_pages:
            break
        page += 1
    return out


def rollup_trace(trace_id: str, get: GetFn, post: PostFn) -> int:
    """Patch ``metadata.rollup`` onto every container observation of one trace.

    Args:
        trace_id: The Langfuse trace id.
        get: Path-to-JSON fetcher (see :data:`GetFn`).
        post: Ingestion batch sink (see :data:`PostFn`).

    Returns:
        The number of container observations patched.
    """
    observations = all_observations(trace_id, get)
    by_id, children = build_tree(observations)
    batch = [
        rollup_event(o, subtree_totals(o["id"], by_id, children))
        for o in observations
        if children.get(o["id"])  # only containers (those with children) get a rollup
    ]
    if batch:
        post(batch)
    return len(batch)


def rollup_session(spoke_run_id: str, get: GetFn, post: PostFn) -> int:
    """Roll up token decompositions across every trace of a session.

    Args:
        spoke_run_id: The session id (``langfuse.session.id``) to roll up.
        get: Path-to-JSON fetcher (see :data:`GetFn`).
        post: Ingestion batch sink (see :data:`PostFn`).

    Returns:
        The total number of container observations patched across all traces.
    """
    session = urllib.parse.quote(spoke_run_id)
    traces = get(f"/traces?sessionId={session}&limit={_PAGE_LIMIT}").get("data") or []
    return sum(rollup_trace(t["id"], get, post) for t in traces)


def make_get(host: str, auth: str) -> GetFn:
    """Build a fetcher for the Langfuse public API bound to ``host`` and ``auth``.

    Args:
        host: Base Langfuse URL, e.g. ``http://localhost:3000``.
        auth: The ``Authorization`` header value, ``Basic <base64(pk:sk)>``.

    Returns:
        A callable mapping a public-API path to its decoded JSON object.
    """

    def get(path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{host}/api/public{path}", headers={"Authorization": auth}
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            return json.load(resp)

    return get


def make_post(host: str, auth: str) -> PostFn:
    """Build an ingestion sink that POSTs a batch to the Langfuse ingestion endpoint.

    Args:
        host: Base Langfuse URL, e.g. ``http://localhost:3000``.
        auth: The ``Authorization`` header value, ``Basic <base64(pk:sk)>``.

    Returns:
        A callable posting a list of batch events as one ingestion request.
    """

    def post(batch: list[dict[str, Any]]) -> None:
        data = json.dumps({"batch": batch}).encode()
        request = urllib.request.Request(
            f"{host}/api/public/ingestion",
            data=data,
            headers={"Authorization": auth, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            resp.read()

    return post


def make_delete(host: str, auth: str) -> DeleteFn:
    """Build a bulk trace-deleter bound to ``host`` and ``auth``.

    Args:
        host: Base Langfuse URL, e.g. ``http://localhost:3000``.
        auth: The ``Authorization`` header value, ``Basic <base64(pk:sk)>``.

    Returns:
        A callable that issues one ``DELETE /api/public/traces`` for the given trace ids.
        Deletion is asynchronous on the server, so callers must poll the listing to confirm
        the traces are gone before re-posting (see ``purge_own_views``).
    """

    def delete(trace_ids: list[str]) -> None:
        data = json.dumps({"traceIds": trace_ids}).encode()
        request = urllib.request.Request(
            f"{host}/api/public/traces",
            data=data,
            headers={"Authorization": auth, "Content-Type": "application/json"},
            method="DELETE",
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            resp.read()

    return delete


def main(argv: list[str] | None = None) -> int:
    """Read configuration from the environment and roll up one session's traces.

    Args:
        argv: CLI arguments excluding the program name; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success, 2 on a usage error.

    Raises:
        KeyError: When ``LANGFUSE_BASIC_AUTH`` is not set.
    """
    logging.basicConfig(level=logging.INFO, format="[rollup] %(message)s")
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        logger.error("usage: langfuse_rollup.py <spoke_run_id>")
        return 2
    spoke_run_id = args[0]
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    auth = os.environ["LANGFUSE_BASIC_AUTH"]  # "Basic <base64(pk:sk)>"

    count = rollup_session(spoke_run_id, make_get(host, auth), make_post(host, auth))
    logger.info("patched rollup onto %d container span(s) for session %s", count, spoke_run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
