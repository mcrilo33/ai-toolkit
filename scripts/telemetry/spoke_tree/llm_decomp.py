"""Per-call cache decomposition: split each llm_request into cache_read / cache_creation (#99, #100).

For each LLM call (aligned positionally with its raw request body) the body is itemized, split
into the observed ``cache_read`` / ``cache_creation`` budgets by cumulative fit
(:func:`_split_rows_by_cache`), and folded onto the call's copy as ``metadata.cache_read`` /
``metadata.cache_creation`` (:func:`apply_llm_decomposition`). :func:`_memoized_counter` caches
token counts by content hash so the repeated stable prefix is measured once (#160). Depends on the
foundation, :mod:`~telemetry.spoke_tree.loaded_context`, plus ``request_body``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, cast

from telemetry.measure_context_cost import TokenCounter
from telemetry.request_body import decompose_request_body, measure_request_items
from telemetry.spoke_tree.ids import _copy_id
from telemetry.spoke_tree.loaded_context import _breakdown_by_category, find_request_files
from telemetry.spoke_tree.observations import (
    IngestEvent,
    TraceObservations,
    _llm_requests_in_order,
)

logger = logging.getLogger("langfuse_spoke_tree")

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
    for (orig_trace_id, observation), body_path in zip(calls, bodies, strict=False):
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
