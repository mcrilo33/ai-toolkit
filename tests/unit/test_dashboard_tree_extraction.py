"""`dashboard/tree.py` extraction guard (Issue #50, Part 3 — RED).

Part 3 moves the forest-building functions (intervals, buckets, turn/marker/
synthetic node construction, nesting, attribution, rollups) out of
``dashboard/queries.py`` into a new ``dashboard/tree.py`` so the SQL +
aggregate/meta/A-B layer (track F) and the tree layer (track C) stop colliding in
one file. It is a **pure, behaviour-preserving move**: ``queries.py`` imports the
builders back, and the spoke forest a reader sees is byte-for-byte identical.

Two guards:

1. **Structure** — the builders are *defined in* ``tree.py`` (``__module__ ==
   "tree"``), and ``queries.py`` sources them from there rather than keeping a
   duplicate copy. Fails until the move happens.
2. **Behaviour** — ``spoke_steps`` / ``spoke_tree`` / ``spoke_meta_by_kind`` over
   the v1 and v2 fixtures match a golden snapshot captured from the pre-extraction
   code. This is the no-regression net; it stays green across the move.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from _dashboard_helpers import DASHBOARD_DIR, store, store_v2

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = _FIXTURES / "dashboard_forest_golden.json"

# The forest builders that must live in tree.py and be imported back by queries.py.
FOREST_FUNCS = (
    "_parse_ts",
    "_step_node",
    "_build_intervals",
    "_attribute_turns",
    "_turns_by_owner",
    "_interval_forest",
    "_roll_up_steps",
    "_roll_up",
)


def _load_tree() -> ModuleType:
    """Import ``dashboard/tree.py`` as a standalone module (by file path)."""
    path = DASHBOARD_DIR / "tree.py"
    spec = importlib.util.spec_from_file_location("tree", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load tree module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize(obj: object) -> object:
    """Round-trip through JSON so the comparison matches the golden's encoding."""
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


class TestStructure:
    def test_tree_module_exposes_forest_builders(self) -> None:
        tree = _load_tree()
        for fn in FOREST_FUNCS:
            assert hasattr(tree, fn), fn

    def test_queries_sources_builders_from_tree(self) -> None:
        from _dashboard_helpers import load_queries

        queries = load_queries()
        for fn in FOREST_FUNCS:
            assert getattr(queries, fn).__module__ == "tree", fn

    def test_queries_no_longer_defines_builders_itself(self) -> None:
        # A re-export, not a copy: the function object queries exposes is the one
        # tree defines — proven by identity against a freshly-loaded tree module.
        from _dashboard_helpers import load_queries

        queries = load_queries()
        tree = _load_tree()
        # Same source file, so the round-tripped source must be identical objects'
        # qualnames; identity can differ across importlib loads, so compare module.
        for fn in FOREST_FUNCS:
            assert getattr(queries, fn).__module__ == getattr(tree, fn).__module__ == "tree"


class TestBehaviourUnchanged:
    def test_spoke_steps_matches_golden_v1_and_v2(self) -> None:
        golden = json.loads(GOLDEN.read_text())
        s1, s2 = store(), store_v2()
        got = {
            "v1": {sid: s1.spoke_steps(sid) for sid in s1.spoke_run_ids()},
            "v2": {sid: s2.spoke_steps(sid) for sid in s2.spoke_run_ids()},
        }
        assert _normalize(got["v1"]) == golden["v1"]["spoke_steps"]
        assert _normalize(got["v2"]) == golden["v2"]["spoke_steps"]

    def test_spoke_tree_matches_golden(self) -> None:
        golden = json.loads(GOLDEN.read_text())
        s1, s2 = store(), store_v2()
        assert (
            _normalize({sid: s1.spoke_tree(sid) for sid in s1.spoke_run_ids()})
            == (golden["v1"]["spoke_tree"])
        )
        assert (
            _normalize({sid: s2.spoke_tree(sid) for sid in s2.spoke_run_ids()})
            == (golden["v2"]["spoke_tree"])
        )

    def test_spoke_meta_by_kind_matches_golden(self) -> None:
        golden = json.loads(GOLDEN.read_text())
        s1, s2 = store(), store_v2()
        assert (
            _normalize({sid: s1.spoke_meta_by_kind(sid) for sid in s1.spoke_run_ids()})
            == (golden["v1"]["spoke_meta_by_kind"])
        )
        assert (
            _normalize({sid: s2.spoke_meta_by_kind(sid) for sid in s2.spoke_run_ids()})
            == (golden["v2"]["spoke_meta_by_kind"])
        )
