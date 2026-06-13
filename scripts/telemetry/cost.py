"""Token/cost correlation — attribute tokens and cost to every span.

Fills ``tokens_in`` / ``tokens_out`` / ``cost_usd`` on parsed spans:

- ``agent`` spans take their tokens from the walked subagent transcript (matched
  by ``agent_links``); the subagent runs in its own transcript, so its usage —
  not the parent turn that spawned it — is the agent's cost.
- All other pull spans take their tokens by bracketing the main session's
  per-turn ``message.usage`` between the span's ``ts_start`` / ``ts_end``,
  joined on ``session_id``.

Cost is *not* re-derived from a pricing table. ccusage already computes an
authoritative per-session ``totalCost``; that pool is distributed across the
session's spans by their share of the session's tokens (an effective blended
rate, ``totalCost / session_tokens``, taken straight from ccusage). Tokens of
every type — input, output, cache-read, cache-creation — count toward the share
so the blended rate matches what ccusage actually billed.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime

from telemetry.session_parser import ParsedSession, UsageEvent
from telemetry.spans import Span

CCUSAGE_CMD = ("ccusage", "session", "--json", "--offline")


def attribute(parsed: ParsedSession, ccusage_costs: dict[str, float]) -> ParsedSession:
    """Fill tokens and cost on every span in ``parsed`` (in place).

    Args:
        parsed: Parser output — spans, usage events, and agent links.
        ccusage_costs: Map of ``session_id`` to ccusage ``totalCost``. A session
            absent from the map gets tokens attributed but ``cost_usd`` left
            ``None`` (no authoritative cost to distribute).

    Returns:
        The same :class:`ParsedSession`, mutated.
    """
    attribute_spans(
        parsed.spans, parsed.usage_events, ccusage_costs, agent_links=parsed.agent_links
    )
    return parsed


def attribute_spans(
    spans: list[Span],
    usage_events: list[UsageEvent],
    ccusage_costs: dict[str, float],
    *,
    agent_links: dict[str, str] | None = None,
) -> list[Span]:
    """Attribute tokens and cost to any spans — push or pull (in place).

    The unified dataset feeds push spans (cycle steps, hooks) through here too:
    a push span brackets the same session ``usage_events`` over its wider window,
    so its tokens include the narrower pull spans nested inside it.

    Args:
        spans: Spans to attribute.
        usage_events: Per-turn usage from the relevant sessions.
        ccusage_costs: Map of ``session_id`` to ccusage ``totalCost``.
        agent_links: Map of agent span_id to subagent ``agentId`` — agent spans
            take their tokens from that subagent transcript instead of bracketing.

    Returns:
        The same ``spans`` list, mutated.
    """
    links = agent_links or {}
    rate = _session_rates(usage_events, ccusage_costs)
    for span in spans:
        events = _span_events(span, usage_events, links)
        span.tokens_in = sum(e.input_tokens for e in events)
        span.tokens_out = sum(e.output_tokens for e in events)
        session_rate = rate.get(span.session_id)
        if session_rate is None:
            continue
        span.cost_usd = session_rate * sum(_event_total(e) for e in events)
    return spans


def load_ccusage_costs(runner: Callable[[], str] | None = None) -> dict[str, float]:
    """Load per-session cost from ``ccusage session --json`` (Claude rows only).

    Args:
        runner: Optional override returning the raw ccusage JSON string (for
            tests). Defaults to invoking the ccusage CLI offline.

    Returns:
        Map of session id (ccusage ``period``) to ``totalCost``.
    """
    raw = runner() if runner is not None else _run_ccusage()
    data = json.loads(raw)
    costs: dict[str, float] = {}
    for row in data.get("session", []):
        if row.get("agent") != "claude":
            continue
        period = row.get("period")
        if isinstance(period, str):
            costs[period] = float(row.get("totalCost") or 0.0)
    return costs


def _session_rates(
    events: list[UsageEvent], ccusage_costs: dict[str, float]
) -> dict[str | None, float]:
    """Effective $/token rate per session: ccusage cost ÷ session token total."""
    totals: dict[str | None, int] = defaultdict(int)
    for event in events:
        totals[event.session_id] += _event_total(event)
    rates: dict[str | None, float] = {}
    for session_id, cost in ccusage_costs.items():
        total = totals.get(session_id, 0)
        if total > 0:
            rates[session_id] = cost / total
    return rates


def _span_events(
    span: Span, usage_events: list[UsageEvent], agent_links: dict[str, str]
) -> list[UsageEvent]:
    if span.kind == "agent":
        agent_id = agent_links.get(span.span_id)
        if agent_id is None:
            return []
        return [e for e in usage_events if e.source == "subagent" and e.agent_id == agent_id]
    return [
        e
        for e in usage_events
        if e.source == "main"
        and e.session_id == span.session_id
        and _within(e.ts, span.ts_start, span.ts_end)
    ]


def _event_total(event: UsageEvent) -> int:
    return event.input_tokens + event.output_tokens + event.cache_read + event.cache_creation


def _within(ts: str | None, start: str | None, end: str | None) -> bool:
    """Half-open [start, end) membership.

    The upper bound is exclusive so an event landing exactly on the boundary
    shared by two adjacent spans (span A's ``ts_end`` == span B's ``ts_start``)
    is attributed to exactly one of them — never both — which keeps the
    "attributed cost never exceeds the ccusage session total" invariant intact.
    """
    if not ts or not start or not end:
        return False
    moment, lo, hi = _parse(ts), _parse(start), _parse(end)
    if moment is None or lo is None or hi is None:
        return False
    return lo <= moment < hi


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _run_ccusage() -> str:
    result = subprocess.run(CCUSAGE_CMD, capture_output=True, text=True, check=True)
    return result.stdout
