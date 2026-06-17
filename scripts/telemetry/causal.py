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


class CausalNode(_CausalNodeRequired, _CausalNodeOptional):
    """One node in the causal spoke tree (real span or synthetic), recursive."""


# The required keys every node must carry — the view columns (Node/Time/Dur/Cost/
# Tokens/H/Actor) plus the causal parentage and recursion keys.
REQUIRED_NODE_KEYS: frozenset[str] = frozenset(_CausalNodeRequired.__annotations__)


class CausalContractError(ValueError):
    """A node violates the frozen causal-node contract."""


def validate_causal_tree(forest: list[object]) -> None:
    """Validate a causal forest against the contract, raising on the first defect.

    Args:
        forest: The top-level causal nodes (the L1 spine + dividers).

    Raises:
        CausalContractError: When any node is missing a required key, carries an
            unknown ``kind``, has an inconsistent ``synthetic`` flag, owns cost while
            not a turn/agent leaf, or holds a malformed ``input_context``. The message
            is qualified with the offending node's id path.
    """
    for index, node in enumerate(forest):
        _validate_node(node, path=f"[{index}]")


def _validate_node(node: object, *, path: str) -> None:
    if not isinstance(node, dict):
        raise CausalContractError(f"{path}: node is {type(node).__name__}, not a dict")

    missing = REQUIRED_NODE_KEYS - node.keys()
    if missing:
        raise CausalContractError(f"{path}: missing required keys {sorted(missing)}")

    here = f"{path}({node['node_id']}/{node['kind']})"
    if node["kind"] not in CAUSAL_KINDS:
        raise CausalContractError(f"{here}: unknown kind (expected one of {CAUSAL_KINDS})")

    if node["synthetic"] is not (node["kind"] in SYNTHETIC_KINDS):
        raise CausalContractError(f"{here}: synthetic flag disagrees with kind")

    if node["own_cost_usd"] and node["kind"] not in _COST_OWNING_KINDS:
        raise CausalContractError(f"{here}: owns cost but is not a turn/agent leaf")

    if "input_context" in node:
        _validate_input_context(node["input_context"], path=here)

    children = node["children"]
    if not isinstance(children, list):
        raise CausalContractError(f"{here}: children is not a list")
    for index, child in enumerate(children):
        _validate_node(child, path=f"{here}.children[{index}]")


def _validate_input_context(ctx: dict, *, path: str) -> None:
    missing = {"rules", "claude_md", "memory", "schemas", "history_tokens", "total_tokens"} - (
        ctx.keys() if isinstance(ctx, dict) else set()
    )
    if missing:
        raise CausalContractError(f"{path}: input_context missing {sorted(missing)}")
    for item in (*ctx["rules"], *ctx["memory"]):
        if not item.get("name") or not isinstance(item.get("tokens"), int):
            raise CausalContractError(f"{path}: context item must have a name and int tokens")
