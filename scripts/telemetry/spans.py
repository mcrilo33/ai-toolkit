"""The unified span — the frozen v1 contract from Issue #21.

This is the Python mirror of ``docs/telemetry-span-schema.md``. The parser emits
spans of this exact shape, in schema order; the correlation pass fills
``tokens_in`` / ``tokens_out`` / ``cost_usd``. The frozen field set is reused
verbatim, plus the additive, optional, pull-only ``summary`` field (Issue #47)
that push emitters never set.

Note: the issue text mentions a ``cache_read`` metric, but the frozen v1 span has
no such field. Cache-read tokens are tracked internally during correlation (they
feed cost math) but are not emitted as a span field; surfacing them is a v2
schema follow-up, not an in-place change to the frozen contract.

The v3 spoke-trace (Issue #50) extends the contract *additively* in three ways,
all back-compatible with the frozen v1 producers:

- three new real-span ``kind`` values — ``workflow`` / ``workflow_phase`` /
  ``approval`` — for the ``Workflow`` fan-out and approval interactions;
- three optional, pull-only **link fields** — ``emits`` / ``sidecar_session`` /
  ``agent_link`` — that the parser fills and push emitters never set (``null`` on
  push spans, exactly like ``summary``);
- the **synthetic-node field contract** (:data:`SYNTHETIC_KINDS`,
  :class:`SyntheticNode`, :func:`synthetic_node`) — display-only tree nodes that
  are *never* spans and never reach the spans table or any rollup.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, TypedDict

SPAN_KINDS: tuple[str, ...] = (
    "lifecycle",
    "step",
    "hook",
    "script",
    "tool",
    "skill",
    "agent",
    "todo",
    "human",
    "rule",
    # v3 spoke-trace (Issue #50). ``workflow`` brackets a ``Workflow`` fan-out and
    # ``workflow_phase`` its phase groups (display-only at aggregate time); cost
    # lives on their ``agent``/``turn`` leaves, never the containers. ``approval``
    # is a timed allow/ask/deny interaction — the automatability view's primary home.
    "workflow",
    "workflow_phase",
    "approval",
)

# The emitted field order — matches the schema doc's object layout exactly.
# ``summary`` (Issue #47) is an additive, optional, pull-only display field: the
# parser fills it with a few-word node label; push emitters never set it (it stays
# null on push spans). ``name`` remains the stable construct id used for grouping.
SPAN_FIELDS: tuple[str, ...] = (
    "span_id",
    "parent_id",
    "spoke_run_id",
    "session_id",
    "workflow_rev",
    "repo",
    "branch",
    "kind",
    "name",
    "phase",
    "ts_start",
    "ts_end",
    "duration_ms",
    "status",
    "human",
    # The hook's raising condition (Issue #82) — ``PreToolUse`` / ``PostToolUse`` /
    # ``SessionStart`` / ``Stop`` / … Set only on ``hook`` spans (the push emitter reads
    # it from the payload's ``hook_event_name``); ``null`` on every other span. It is the
    # trigger the dashboard shows on the hook line and the signal a Pre/PostToolUse hook
    # nests under its tool.
    "hook_event",
    "summary",
    # v3 link fields (Issue #50) — additive, optional, pull-only; ``null`` on push
    # spans, filled by the parser. ``emits``: a ``script`` span names the span_id of
    # the ``step``/``lifecycle`` marker it produced (emission link — structural, not
    # time-containment). ``sidecar_session``: a ``hook``/``script`` that shells out to
    # a separate ``claude -p`` session names that session id (the inline mirror of the
    # parser's ``agent_links`` map, keyed off this one span). ``agent_link``: an
    # ``agent`` span names its subagent ``agentId``/session — the per-span half of the
    # parser's ``agent_links`` map, so agent→agent→… recursion composes into a chain.
    "emits",
    "sidecar_session",
    "agent_link",
    "tokens_in",
    "tokens_out",
    "cost_usd",
)


@dataclass(slots=True)
class Span:
    """One unified span. Mutable so the correlation pass can fill token/cost."""

    span_id: str
    kind: str
    name: str
    parent_id: str | None = None
    spoke_run_id: str | None = None
    session_id: str | None = None
    workflow_rev: str | None = None
    repo: str = "unknown"
    branch: str | None = None
    phase: str | None = None
    ts_start: str | None = None
    ts_end: str | None = None
    duration_ms: int = 0
    status: str = "success"
    human: dict[str, object] | None = None
    summary: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    # v3 link fields (Issue #50) — declared after the v1 tail so positional
    # construction of the frozen fields is unaffected; serialized in SPAN_FIELDS
    # order (between ``summary`` and the token tail) by ``to_dict``.
    emits: str | None = None
    sidecar_session: str | None = None
    agent_link: str | None = None
    # The hook's raising condition (Issue #82) — declared after the v1/v3 tail so
    # positional construction of the frozen fields is unaffected; serialized in
    # SPAN_FIELDS order (right after ``human``) by ``to_dict``.
    hook_event: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in SPAN_KINDS:
            raise ValueError(f"unknown span kind: {self.kind!r} (expected one of {SPAN_KINDS})")

    def to_dict(self) -> dict[str, object]:
        """Serialize in frozen schema field order."""
        return {name: getattr(self, name) for name in SPAN_FIELDS}


# ── synthetic-node field contract (Issue #50) ─────────────────────────────────
# Synthetic nodes are display-only tree rows built at query time by the dashboard.
# They are NEVER spans: they carry no ``span_id``, never enter the spans table, and
# never reach an aggregate/meta rollup. The dashboard forest builders mix
# them freely with real-span nodes as children, so they mirror the real-node shape
# — with ``actor`` (the v3 ``Actor`` column: ``main``, a sub-agent name,
# ``workflow``, ``script``, ``hooks``, ``sidecar``) as the canonical owner key.

SYNTHETIC_KINDS: tuple[str, ...] = (
    "interval",  # a phase-interval bucket (the L1 spine row)
    "turn",  # one assistant inference, owning its once-per-turn cost
    "hooks",  # a collapsed ``hooks xN`` group
    "reasoning",  # an extended-thinking block
    "context",  # a loaded-context item (rule / memory / tool-schema)
    "gap",  # idle time, rendered as a divider
    "session",  # a session-resume divider (cold cache)
    "scope-band",  # a soft skill/rule ``[scope]`` band over the turns it influenced
    "unresolved",  # turns/spans off the lifecycle spine, kept so totals reconcile
)


class SyntheticNode(TypedDict):
    """The dict shape every synthetic (display-only) tree node carries.

    ``span_id``/``parent_id`` are always ``None`` — the marker that a node is
    synthetic, never a span. ``own_*`` hold only this node's directly-owned metrics
    (a container owns nothing; its turn/agent leaves do), so a subtree rollup never
    double-counts. ``children`` may hold synthetic nodes or real-span node dicts.
    """

    span_id: None
    parent_id: None
    kind: str
    name: str
    summary: str | None
    phase: str | None
    status: str
    ts_start: str | None
    ts_end: str | None
    duration_ms: int | None
    own_cost_usd: float
    own_tokens_in: int
    own_tokens_out: int
    models: list[str]
    actor: str
    human_count: int
    children: list[Any]


def synthetic_node(
    *,
    kind: str,
    name: str,
    summary: str | None = None,
    phase: str | None = None,
    status: str = "success",
    ts_start: str | None = None,
    ts_end: str | None = None,
    duration_ms: int | None = None,
    own_cost_usd: float = 0.0,
    own_tokens_in: int = 0,
    own_tokens_out: int = 0,
    models: list[str] | None = None,
    actor: str = "main",
    human_count: int = 0,
    children: list[Any] | None = None,
) -> SyntheticNode:
    """Build one display-only :class:`SyntheticNode` in the canonical shape.

    Args:
        kind: One of :data:`SYNTHETIC_KINDS`.
        name: The node's display name.

    Raises:
        ValueError: When ``kind`` is not a registered synthetic kind (a real-span
            kind here would silently smuggle a non-span into the synthetic surface).
    """
    if kind not in SYNTHETIC_KINDS:
        raise ValueError(f"not a synthetic kind: {kind!r} (expected one of {SYNTHETIC_KINDS})")
    return {
        "span_id": None,
        "parent_id": None,
        "kind": kind,
        "name": name,
        "summary": summary,
        "phase": phase,
        "status": status,
        "ts_start": ts_start,
        "ts_end": ts_end,
        "duration_ms": duration_ms,
        "own_cost_usd": own_cost_usd,
        "own_tokens_in": own_tokens_in,
        "own_tokens_out": own_tokens_out,
        "models": list(models) if models else [],
        "actor": actor,
        "human_count": human_count,
        "children": list(children) if children else [],
    }


def derive_span_id(*parts: str) -> str:
    """Deterministic 12-hex span id from stable identifiers.

    Re-parsing the same session yields the same ids, so the dataset is
    idempotent across runs (no duplicate spans when DuckDB re-reads the source).
    """
    digest = hashlib.sha1(":".join(parts).encode("utf-8")).hexdigest()
    return digest[:12]
