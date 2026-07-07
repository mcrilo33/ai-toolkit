"""Unit tests for the Langfuse per-container token rollup (Issue #83).

Langfuse rolls cost and latency up onto container spans but not the token breakdown. The
rollup script sums the four token components (input / output / cache_read / cache_creation)
over each container observation's subtree and patches them as ``metadata.rollup`` via the
ingestion API. These AAA tests exercise the pure helpers and the trace/session drivers with
stubbed get/post callables -- no network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.langfuse_rollup import (
    build_tree,
    make_delete,
    rollup_event,
    rollup_session,
    rollup_trace,
    subtree_totals,
)

# --- helpers -----------------------------------------------------------------


def _obs(
    obs_id: str,
    *,
    parent: str | None = None,
    obs_type: str = "SPAN",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a Langfuse observation dict for the tests."""
    return {
        "id": obs_id,
        "parentObservationId": parent,
        "type": obs_type,
        "usageDetails": usage or {},
    }


def _usage(
    *, inp: int = 0, out: int = 0, reused: int = 0, written: int = 0, written_1h: int = 0
) -> dict[str, int]:
    """Build a ``usageDetails`` dict. ``written`` is the 5m cache-write tier; ``written_1h``
    the 1h tier (Issue #97), added only when nonzero so the pre-#97 shape is unchanged."""
    usage = {
        "input": inp,
        "output": out,
        "cache_read_input_tokens": reused,
        "cache_creation_input_tokens": written,
    }
    if written_1h:
        usage["input_cache_creation_1h"] = written_1h
    return usage


class _GetStub:
    """Returns queued JSON responses for each path-prefix the driver requests."""

    def __init__(self, traces: list[dict[str, Any]], pages: dict[str, list[dict[str, Any]]]):
        self._traces = traces
        self._pages = pages  # trace_id -> ordered list of page response objects
        self._page_idx: dict[str, int] = {}
        self.paths: list[str] = []

    def __call__(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        if path.startswith("/traces"):
            return {"data": self._traces}
        trace_id = path.split("traceId=")[1].split("&")[0]
        idx = self._page_idx.get(trace_id, 0)
        self._page_idx[trace_id] = idx + 1
        return self._pages[trace_id][idx]


class _PostSink:
    """Records each batch the driver would POST to Langfuse, in order."""

    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    def __call__(self, batch: list[dict[str, Any]]) -> None:
        self.batches.append(batch)


# --- subtree_totals ----------------------------------------------------------


def test_subtree_sums_nested_container_descendants() -> None:
    # Arrange: interaction -> generation(a) + generation(b), each with its own usage.
    observations = [
        _obs("root", obs_type="SPAN"),
        _obs("gen-a", parent="root", obs_type="GENERATION", usage=_usage(inp=10, out=2, reused=3)),
        _obs("gen-b", parent="root", obs_type="GENERATION", usage=_usage(inp=5, out=1, written=4)),
    ]
    by_id, children = build_tree(observations)

    # Act
    totals = subtree_totals("root", by_id, children)

    # Assert
    assert totals["input"] == 15
    assert totals["output"] == 3
    assert totals["cache_read_input_tokens"] == 3
    assert totals["cache_creation_input_tokens"] == 4


def test_subtree_of_leaf_generation_is_its_own_usage() -> None:
    # Arrange
    observations = [
        _obs("gen", obs_type="GENERATION", usage=_usage(inp=7, out=2, reused=1, written=5))
    ]
    by_id, children = build_tree(observations)

    # Act
    totals = subtree_totals("gen", by_id, children)

    # Assert
    assert totals == {
        "input": 7,
        "output": 2,
        "cache_read_input_tokens": 1,
        "cache_creation_input_tokens": 5,
        "input_cache_creation_1h": 0,
    }


def test_subtree_sums_the_1h_cache_write_tier() -> None:
    # Issue #97: subtree_totals tracks the 1h cache-write tier alongside the 5m tier.
    observations = [_obs("gen", obs_type="GENERATION", usage=_usage(written=120, written_1h=180))]
    by_id, children = build_tree(observations)

    totals = subtree_totals("gen", by_id, children)

    assert totals["cache_creation_input_tokens"] == 120
    assert totals["input_cache_creation_1h"] == 180


def test_rollup_written_sums_both_cache_write_tiers() -> None:
    # Issue #97: the rollup's ``written`` is the total cache writes across both TTL tiers.
    totals = {
        "input": 0,
        "output": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 120,
        "input_cache_creation_1h": 180,
    }

    event = rollup_event(_obs("gen", obs_type="GENERATION"), totals)

    assert event["body"]["metadata"]["rollup"]["written"] == 300


def test_container_without_generation_descendants_rolls_up_to_zero() -> None:
    # Arrange: a tool container over two leaf tools, none of which call an API.
    observations = [
        _obs("tool", obs_type="SPAN"),
        _obs("bash", parent="tool", obs_type="SPAN"),
        _obs("read", parent="tool", obs_type="SPAN"),
    ]
    by_id, children = build_tree(observations)

    # Act
    totals = subtree_totals("tool", by_id, children)

    # Assert
    assert totals == dict.fromkeys(totals, 0)


# --- patch shaping (rollup_event) --------------------------------------------


def test_generation_yields_generation_update() -> None:
    # Arrange
    observation = _obs("gen", obs_type="GENERATION")
    totals = {
        "input": 11,
        "output": 22,
        "cache_read_input_tokens": 33,
        "cache_creation_input_tokens": 44,
    }

    # Act
    event = rollup_event(observation, totals)

    # Assert
    assert event["type"] == "generation-update"
    assert event["body"]["id"] == "gen"
    assert event["body"]["metadata"]["rollup"] == {
        "reused": 33,
        "written": 44,
        "input": 11,
        "output": 22,
    }


def test_span_and_other_types_yield_span_update() -> None:
    # Arrange
    totals: dict[str, int] = dict.fromkeys(
        ("input", "output", "cache_read_input_tokens", "cache_creation_input_tokens"), 0
    )

    # Act / Assert
    assert rollup_event(_obs("s", obs_type="SPAN"), totals)["type"] == "span-update"
    assert rollup_event(_obs("e", obs_type="EVENT"), totals)["type"] == "span-update"
    assert rollup_event({"id": "n"}, totals)["type"] == "span-update"  # missing type -> SPAN


# --- tree building -----------------------------------------------------------


def test_build_tree_multi_level_parentage() -> None:
    # Arrange: interaction -> tool:Workflow -> sub-agent.
    observations = [
        _obs("interaction"),
        _obs("workflow", parent="interaction"),
        _obs("subagent", parent="workflow"),
    ]

    # Act
    by_id, children = build_tree(observations)

    # Assert
    assert set(by_id) == {"interaction", "workflow", "subagent"}
    assert children[None] == ["interaction"]
    assert children["interaction"] == ["workflow"]
    assert children["workflow"] == ["subagent"]
    assert "subagent" not in children  # leaf has no entry


# --- pagination --------------------------------------------------------------


def test_pagination_consumes_all_pages() -> None:
    # Arrange: a 2-page observations response for one trace.
    page1 = {
        "data": [
            _obs("interaction"),
            _obs("gen-a", parent="interaction", obs_type="GENERATION", usage=_usage(inp=10, out=2)),
        ],
        "meta": {"totalPages": 2},
    }
    page2 = {
        "data": [
            _obs("gen-b", parent="interaction", obs_type="GENERATION", usage=_usage(inp=5, out=1))
        ],
        "meta": {"totalPages": 2},
    }
    get = _GetStub(traces=[{"id": "trace-1"}], pages={"trace-1": [page1, page2]})
    post = _PostSink()

    # Act
    patched = rollup_trace("trace-1", get, post)

    # Assert: both pages fetched, and the interaction subtree sums across both pages.
    assert get._page_idx["trace-1"] == 2
    assert patched == 1  # only the interaction container has children
    rollup = post.batches[0][0]["body"]["metadata"]["rollup"]
    assert rollup["input"] == 15
    assert rollup["output"] == 3


# --- session driver ----------------------------------------------------------


def test_rollup_session_patches_only_containers() -> None:
    # Arrange: one trace, interaction container over a single leaf generation.
    page = {
        "data": [
            _obs("interaction"),
            _obs("gen", parent="interaction", obs_type="GENERATION", usage=_usage(inp=9, out=3)),
        ],
        "meta": {"totalPages": 1},
    }
    get = _GetStub(traces=[{"id": "trace-1"}], pages={"trace-1": [page]})
    post = _PostSink()

    # Act
    patched = rollup_session("spoke-123", get, post)

    # Assert
    assert patched == 1
    assert get.paths[0].startswith("/traces?sessionId=spoke-123")
    assert [e["body"]["id"] for e in post.batches[0]] == ["interaction"]


# --- bulk trace delete (issue #156) ------------------------------------------


def test_make_delete_issues_bulk_delete_with_trace_ids(monkeypatch: Any) -> None:
    # Arrange: capture the urllib Request the delete builds instead of hitting the network.
    import json
    import urllib.request

    captured: dict[str, Any] = {}

    class _Resp:
        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    def fake_urlopen(request: urllib.request.Request, timeout: int = 0) -> _Resp:
        captured["method"] = request.get_method()
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data.decode())
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    delete = make_delete("http://localhost:3000", "Basic abc")

    # Act
    delete(["spoketree-1", "spokecycle-1"])

    # Assert: a DELETE to the bulk traces endpoint carrying the ids under "traceIds".
    assert captured["method"] == "DELETE"
    assert captured["url"] == "http://localhost:3000/api/public/traces"
    assert captured["auth"] == "Basic abc"
    assert captured["body"] == {"traceIds": ["spoketree-1", "spokecycle-1"]}
