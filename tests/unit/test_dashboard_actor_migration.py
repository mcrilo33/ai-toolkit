"""Actor/factory node-shape contract for the spoke forest (Issue #52 Track C — RED).

Track C adopts the #50 frozen synthetic-node contract: every forest node keys its
owner on ``actor`` (the v3 Actor column), never the legacy ``agent`` key, and every
*synthetic* (display-only, ``span_id is None``) node is built through the canonical
:func:`telemetry.spans.synthetic_node` factory — so it carries a registered
``SYNTHETIC_KINDS`` kind and the factory's field shape, never an ad-hoc dict.

These guard the migration that precedes the spine/nesting/synthetics/invariants
work; they fail against the pre-migration ``agent``-keyed tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _dashboard_helpers import store_v2
from telemetry.spans import SYNTHETIC_KINDS

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
V3_SPANS = _FIXTURES / "dashboard_golden_spoke.jsonl"
V3_TURNS = _FIXTURES / "dashboard_golden_spoke_turns.jsonl"
V3_SPOKE_RUN_ID = "feature/47+1700000000"

# The factory's canonical key set (telemetry.spans.SyntheticNode). A synthetic node
# may additionally carry display extras the tree attaches (``model`` on a turn,
# ``collapsed``/``collapsed_count`` on a hooks group, and the ``rollup`` added by the
# post-order pass) — but never the legacy ``agent`` owner key.
_FACTORY_KEYS = {
    "span_id",
    "parent_id",
    "kind",
    "name",
    "summary",
    "phase",
    "status",
    "ts_start",
    "ts_end",
    "duration_ms",
    "own_cost_usd",
    "own_tokens_in",
    "own_tokens_out",
    "models",
    "actor",
    "human_count",
    "children",
}
_ALLOWED_EXTRAS = {"model", "collapsed", "collapsed_count", "rollup", "subtree", "source_span_id"}


def _walk(nodes: list[dict]):
    for node in nodes:
        yield node
        yield from _walk(node["children"])


def _v3_forest() -> list[dict]:
    from _dashboard_helpers import store_from

    return store_from(V3_SPANS, V3_TURNS).spoke_steps(V3_SPOKE_RUN_ID)


def test_every_node_keys_owner_on_actor_not_agent() -> None:
    for forest in (_v3_forest(), _v2_forest()):
        for node in _walk(forest):
            assert "actor" in node, f"{node['kind']} node missing actor key"
            assert "agent" not in node, f"{node['kind']} node still carries legacy agent key"


def test_synthetic_nodes_use_factory_shape_and_kinds() -> None:
    for forest in (_v3_forest(), _v2_forest()):
        for node in _walk(forest):
            if node["span_id"] is not None:
                continue
            assert node["kind"] in SYNTHETIC_KINDS, f"synthetic kind {node['kind']!r} unregistered"
            extra = set(node) - _FACTORY_KEYS - _ALLOWED_EXTRAS
            assert not extra, f"synthetic {node['kind']} carries non-factory keys: {sorted(extra)}"


def _v2_forest() -> list[dict]:
    s = store_v2()
    return [n for sid in s.spoke_run_ids() for n in s.spoke_steps(sid)]
