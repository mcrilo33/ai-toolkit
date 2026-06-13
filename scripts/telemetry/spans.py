"""The unified span — the frozen v1 contract from Issue #21.

This is the Python mirror of ``docs/telemetry-span-schema.md``. The parser emits
spans of this exact shape (18 fields, in schema order); the correlation pass
fills ``tokens_in`` / ``tokens_out`` / ``cost_usd``. The shape is reused
verbatim — no divergent fields are added here.

Note: the issue text mentions a ``cache_read`` metric, but the frozen v1 span has
no such field. Cache-read tokens are tracked internally during correlation (they
feed cost math) but are not emitted as a span field; surfacing them is a v2
schema follow-up, not an in-place change to the frozen contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

SPAN_KINDS: tuple[str, ...] = (
    "lifecycle",
    "step",
    "hook",
    "script",
    "skill",
    "agent",
    "todo",
    "human",
    "rule",
)

# The emitted field order — matches the schema doc's object layout exactly.
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
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in SPAN_KINDS:
            raise ValueError(f"unknown span kind: {self.kind!r} (expected one of {SPAN_KINDS})")

    def to_dict(self) -> dict[str, object]:
        """Serialize in frozen schema field order."""
        return {name: getattr(self, name) for name in SPAN_FIELDS}


def derive_span_id(*parts: str) -> str:
    """Deterministic 12-hex span id from stable identifiers.

    Re-parsing the same session yields the same ids, so the dataset is
    idempotent across runs (no duplicate spans when DuckDB re-reads the source).
    """
    digest = hashlib.sha1(":".join(parts).encode("utf-8")).hexdigest()
    return digest[:12]
