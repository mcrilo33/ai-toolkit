"""Loaded-context enrichment: one collapsed startup-inventory node under the root (#87).

Static startup context (rules / memory / skills / sub-agents / tools / MCP) is inventory, not
work, so it collapses into a SINGLE ``loaded-context`` node whose ``metadata.tokens`` is the total
and ``metadata.breakdown`` the per-category itemization (:func:`build_loaded_context_events`). The
primary source is the spoke's raw request body (:func:`request_context_rows`), fully itemized; the
fallback is disk measurement (:func:`loaded_context_rows`) plus a reconciled remainder against the
first call's :func:`prefix_total`. Depends on the foundation modules plus ``request_body`` /
``measure_context_cost``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

from telemetry.measure_context_cost import TokenCounter, assemble_items, measure_items
from telemetry.request_body import (
    first_real_request,
    measure_request_items,
    parse_request_body,
)
from telemetry.spoke_tree.ids import root_id_for, trace_id_for
from telemetry.spoke_tree.observations import IngestEvent, TraceObservations

# Deterministic id prefix for the synthetic loaded-context node.
_LC_PREFIX = "tree-lc-"
# Category order for the request-body itemization (the primary, fully-itemized path). Carries
# the turn-0 combined-block router's rules / skills / environment splits (see
# request_body._route_reminder, #159) between ``system`` and the whole-kept ``context``
# reminders; empty categories are dropped by _breakdown_by_category.
_REQUEST_CATEGORY_ORDER = ("tools", "mcp", "system", "rules", "skills", "environment", "context")
# Category order for the disk fallback used when no request body is available.
_DISK_CATEGORY_ORDER = ("rules", "memory", "skills", "sub-agents", "environment")


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
