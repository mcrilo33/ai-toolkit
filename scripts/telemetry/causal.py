"""The causal-node contract for the v3 spoke trace (Issue #65, Phase 1).

Phase 1 replaces the dashboard's timestamp-correlation tree with the real causal
ids already in the data (``uuid``/``parentUuid``, ``tool_use.id``,
``tool_result.tool_use_id``, ``isSidechain``, ``agent_links``). Every node the
causal builder emits — whether it wraps a real span or is a display-only synthetic
— carries the single shape frozen here, so the parser (S2), the builder (S3), and
the query layer (S5) all develop against one contract.

The shape is a superset of the older :class:`telemetry.spans.SyntheticNode`: it
keeps ``own_*`` directly-owned metrics (a container owns nothing; cost lives only on
``turn``/``agent`` leaves) and recursive ``children``, and adds the causal keys —
``node_id`` (stable id), ``parent_id`` (causal parent), ``synthetic`` (display-only
flag), and the optional ``input_context`` a ``turn`` carries to render loaded context
as ONE named, real-token node instead of N bare ``rule`` leaves.
"""

from __future__ import annotations

from typing import TypedDict

from telemetry.spans import SPAN_KINDS, SYNTHETIC_KINDS

# Every legal node kind: a real span (lifecycle/step/tool/agent/…) or a display-only
# synthetic (interval/turn/context/gap/…). One union so a node built from either
# source validates against one contract.
CAUSAL_KINDS: tuple[str, ...] = tuple(dict.fromkeys((*SPAN_KINDS, *SYNTHETIC_KINDS)))

# Kinds permitted to own cost/tokens. Conservation rule: a container owns nothing;
# the per-inference cost lives on the ``turn`` it was spent in (or the ``agent`` leaf
# for a sub-agent whose sub-turns are not expanded), never on a wrapping bucket.
_COST_OWNING_KINDS: frozenset[str] = frozenset({"turn", "agent"})


class ContextItem(TypedDict):
    """One named loaded-context element with its real token weight."""

    name: str
    tokens: int


class SchemaSummary(TypedDict):
    """The tool-schema block of a turn's input state (count + total tokens)."""

    count: int
    tokens: int


class InputContext(TypedDict):
    """A turn's input state: what was loaded into the prompt, named, with real tokens.

    Rendered as the single ``context`` child of a ``turn`` node. Tokens are real
    (composed from the turn's ``cache_read``/``cache_creation`` usage), so the view
    shows ``code-quality 1.8k`` rather than ``rule ~2 tokens`` repeated N times.
    """

    rules: list[ContextItem]
    claude_md: ContextItem | None
    memory: list[ContextItem]
    schemas: SchemaSummary
    history_tokens: int
    total_tokens: int


class _CausalNodeRequired(TypedDict):
    node_id: str
    parent_id: str | None
    kind: str
    name: str
    summary: str | None
    actor: str
    phase: str | None
    status: str
    ts_start: str | None
    ts_end: str | None
    duration_ms: int
    own_cost_usd: float
    own_tokens_in: int
    own_tokens_out: int
    human_count: int
    synthetic: bool
    children: list[CausalNode]


class _CausalNodeOptional(TypedDict, total=False):
    # The loaded-context input state, carried on the single ``context`` render node a
    # ``turn`` owns (the "ONE node" the loaded context collapses to).
    input_context: InputContext
    # Causal link fields, surfaced only where they apply (mirrors the span schema):
    # a ``script`` names the marker it ``emits``; a ``hook``/``script`` names its
    # ``sidecar_session``; an ``agent`` names its ``agent_link`` (the sub-agent id).
    emits: str | None
    sidecar_session: str | None
    agent_link: str | None
    # Present on a ``human``/``approval`` node — the interaction's type + wait.
    human_type: str | None
    human_wait_ms: int | None
    # Per-turn cache breakdown (on a ``turn`` node) and the resume cold-cache magnitude
    # (on a ``session`` divider) — display-only, read by the composition/divider renderers;
    # they never fold into the once-per-turn cost rollup.
    cache_read: int
    cache_creation: int
    resume_cache_creation: int


class CausalNode(_CausalNodeRequired, _CausalNodeOptional):
    """One node in the causal spoke tree (real span or synthetic), recursive."""


# The required keys every node must carry — the view columns (Node/Time/Dur/Cost/
# Tokens/H/Actor) plus the causal parentage and recursion keys.
REQUIRED_NODE_KEYS: frozenset[str] = frozenset(_CausalNodeRequired.__annotations__)


class CausalContractError(ValueError):
    """A node violates the frozen causal-node contract."""


# Sentinel for optional keys the factory should add only when the caller passes them,
# so a node never carries a key it has no value for (``emits=None`` means a present
# null link; an omitted ``emits`` means no link at all — the contract distinguishes them).
_UNSET: object = object()

# The optional link keys, paired with the sentinel-guarded factory params below.
_OPTIONAL_LINK_KEYS: tuple[str, ...] = (
    "emits",
    "sidecar_session",
    "agent_link",
    "human_type",
    "human_wait_ms",
)


def causal_node(
    *,
    node_id: str,
    kind: str,
    name: str,
    parent_id: str | None = None,
    summary: str | None = None,
    actor: str = "main",
    phase: str | None = None,
    status: str = "success",
    ts_start: str | None = None,
    ts_end: str | None = None,
    duration_ms: int = 0,
    own_cost_usd: float = 0.0,
    own_tokens_in: int = 0,
    own_tokens_out: int = 0,
    human_count: int = 0,
    children: list[CausalNode] | None = None,
    input_context: InputContext | None = None,
    emits: object = _UNSET,
    sidecar_session: object = _UNSET,
    agent_link: object = _UNSET,
    human_type: object = _UNSET,
    human_wait_ms: object = _UNSET,
) -> CausalNode:
    """Build one :class:`CausalNode` in the canonical shape (``synthetic`` inferred).

    The required keys are always set; ``input_context`` and the link keys are added
    only when supplied, keeping a node free of keys it has no value for.

    Raises:
        CausalContractError: When ``kind`` is not a registered causal kind.
    """
    if kind not in CAUSAL_KINDS:
        raise CausalContractError(f"not a causal kind: {kind!r} (expected one of {CAUSAL_KINDS})")
    node: CausalNode = {
        "node_id": node_id,
        "parent_id": parent_id,
        "kind": kind,
        "name": name,
        "summary": summary,
        "actor": actor,
        "phase": phase,
        "status": status,
        "ts_start": ts_start,
        "ts_end": ts_end,
        "duration_ms": duration_ms,
        "own_cost_usd": own_cost_usd,
        "own_tokens_in": own_tokens_in,
        "own_tokens_out": own_tokens_out,
        "human_count": human_count,
        "synthetic": kind in SYNTHETIC_KINDS,
        "children": children if children is not None else [],
    }
    if input_context is not None:
        node["input_context"] = input_context
    for key, value in zip(
        _OPTIONAL_LINK_KEYS,
        (emits, sidecar_session, agent_link, human_type, human_wait_ms),
        strict=True,
    ):
        if value is not _UNSET:
            node[key] = value  # type: ignore[literal-required]
    return node


def validate_causal_tree(forest: list[object]) -> None:
    """Validate a causal forest against the contract, raising on the first defect.

    Args:
        forest: The top-level causal nodes (the L1 spine + dividers).

    Raises:
        CausalContractError: When any node is missing a required key, carries an
            unknown ``kind``, has an inconsistent ``synthetic`` flag, owns cost while
            not a turn/agent leaf, reuses a ``node_id``, points ``parent_id`` at
            anything but its structural parent, or holds a malformed ``input_context``.
            The message is qualified with the offending node's id path.
    """
    seen: set[str] = set()
    for index, node in enumerate(forest):
        _validate_node(node, path=f"[{index}]", expected_parent=None, seen=seen)


def _validate_node(node: object, *, path: str, expected_parent: str | None, seen: set[str]) -> None:
    if not isinstance(node, dict):
        raise CausalContractError(f"{path}: node is {type(node).__name__}, not a dict")

    missing = REQUIRED_NODE_KEYS - node.keys()
    if missing:
        raise CausalContractError(f"{path}: missing required keys {sorted(missing)}")

    node_id = node["node_id"]
    here = f"{path}({node_id}/{node['kind']})"
    if node_id in seen:
        raise CausalContractError(f"{here}: duplicate node_id {node_id!r}")
    seen.add(node_id)

    if node["kind"] not in CAUSAL_KINDS:
        raise CausalContractError(f"{here}: unknown kind (expected one of {CAUSAL_KINDS})")

    if node["synthetic"] is not (node["kind"] in SYNTHETIC_KINDS):
        raise CausalContractError(f"{here}: synthetic flag disagrees with kind")

    # Referential integrity: a nested node names its structural parent; a top-level
    # node has no in-tree parent. A dangling/mismatched parent_id is the defect the
    # causal builder (S3) is most likely to emit, so the contract catches it here.
    if node["parent_id"] != expected_parent:
        raise CausalContractError(
            f"{here}: parent_id {node['parent_id']!r} != structural parent {expected_parent!r}"
        )

    if node["own_cost_usd"] and node["kind"] not in _COST_OWNING_KINDS:
        raise CausalContractError(f"{here}: owns cost but is not a turn/agent leaf")

    if "input_context" in node:
        if node["kind"] != "context":
            raise CausalContractError(f"{here}: input_context only allowed on a context node")
        _validate_input_context(node["input_context"], path=here)

    children = node["children"]
    if not isinstance(children, list):
        raise CausalContractError(f"{here}: children is not a list")
    for index, child in enumerate(children):
        _validate_node(child, path=f"{here}.children[{index}]", expected_parent=node_id, seen=seen)


def _validate_input_context(ctx: dict, *, path: str) -> None:
    required = {"rules", "claude_md", "memory", "schemas", "history_tokens", "total_tokens"}
    missing = required - (ctx.keys() if isinstance(ctx, dict) else set())
    if missing:
        raise CausalContractError(f"{path}: input_context missing {sorted(missing)}")

    schemas = ctx["schemas"]
    if not (
        isinstance(schemas, dict)
        and isinstance(schemas.get("count"), int)
        and isinstance(schemas.get("tokens"), int)
    ):
        raise CausalContractError(f"{path}: input_context.schemas must hold int count + tokens")
    if not (isinstance(ctx["history_tokens"], int) and isinstance(ctx["total_tokens"], int)):
        raise CausalContractError(f"{path}: input_context history/total tokens must be ints")

    items = [*ctx["rules"], *ctx["memory"]]
    if ctx["claude_md"] is not None:
        items.append(ctx["claude_md"])
    for item in items:
        if not item.get("name") or not isinstance(item.get("tokens"), int):
            raise CausalContractError(f"{path}: context item must have a name and int tokens")
