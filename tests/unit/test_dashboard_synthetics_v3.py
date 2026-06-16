"""Synthetic display nodes for the v3 spoke trace (Issue #52 Track C — RED).

Subtask 3 (synthetics) renders the non-span display rows the spec calls for, against
the ``feature/47`` golden fixture:

- **Context load nodes** — the spawn-time ``rule`` / ``memory`` / ``tool-schema``
  loads collapse into per-subtype ``context`` groups instead of bare ``rule`` rows.
- **Idle gap divider** — a long stretch with no activity (the overnight gap) renders
  as a ``gap`` divider row, not a phase.
- **Session-resume divider** — a spoke that spans more than one ``session_id`` gets
  a ``session`` divider at the resume, carrying the cold-cache note.
- **Context bust** — a context load that arrives mid-run (not in the spawn batch)
  renders as a ``context`` event in the phase it lands in (the inline re-load).
- **Cold-context rollup** — a lens over context loaded for the spoke, the
  trimming/automation candidates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _dashboard_helpers import store_from

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
V3_SPANS = _FIXTURES / "dashboard_golden_spoke.jsonl"
V3_TURNS = _FIXTURES / "dashboard_golden_spoke_turns.jsonl"
SPOKE_RUN_ID = "feature/47+1700000000"
_IDLE_GAP_SECONDS = 600  # 10 min; the fixture's overnight gap is ~57 min


def _store():
    return store_from(V3_SPANS, V3_TURNS)


def _forest() -> list[dict]:
    return _store().spoke_steps(SPOKE_RUN_ID)


def _walk(forest: list[dict]):
    stack = list(forest)
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n["children"])


def test_context_loads_group_by_subtype_under_spawn() -> None:
    forest = _forest()
    spawn = next(r for r in forest if r["name"] in ("setup", "spawn"))
    context_nodes = [n for n in _walk([spawn]) if n["kind"] == "context"]
    subtypes = {n["phase"] for n in context_nodes}
    assert {"rule", "memory", "tool-schema"} <= subtypes, f"got context subtypes {subtypes}"
    # The individual rule spans drill under a context group, not bare under spawn.
    grouped = {cid for n in context_nodes for cid in _ids(n)}
    assert {"g_ctx_q", "g_ctx_mem", "g_ctx_tb"} <= grouped
    assert not any((c.get("span_id") or "").startswith("g_ctx_") for c in spawn["children"])


def test_overnight_idle_renders_as_a_gap_divider() -> None:
    forest = _forest()
    gaps = [r for r in forest if r["kind"] == "gap"]
    assert gaps, "the overnight idle did not render as a gap divider"
    assert any((g["duration_ms"] or 0) >= _IDLE_GAP_SECONDS * 1000 for g in gaps)


def test_session_resume_renders_as_a_divider() -> None:
    forest = _forest()
    sessions = [r for r in forest if r["kind"] == "session"]
    assert sessions, "the resume did not render as a session divider"
    # Carries a cold-cache note (the resume reloads the prompt cache).
    assert any("cache" in (s["summary"] or "").lower() for s in sessions)


def test_cold_context_rollup_lists_loaded_context() -> None:
    rollup = _store().cold_context(SPOKE_RUN_ID)
    by_subtype = {row["phase"]: row for row in rollup}
    assert {"rule", "memory", "tool-schema"} <= set(by_subtype)
    assert by_subtype["rule"]["count"] == 3
    assert by_subtype["tool-schema"]["count"] == 2


def test_mid_run_context_reload_renders_as_a_context_event(tmp_path: Path) -> None:
    # A rule re-loaded after spawn (rules edited + re-synced) lands as a context
    # event in the phase it arrives in — not folded into the spawn batch.
    spans = [json.loads(line) for line in V3_SPANS.read_text().splitlines() if line.strip()]
    spans.append(
        {
            "span_id": "g_ctx_reload",
            "parent_id": None,
            "spoke_run_id": SPOKE_RUN_ID,
            "session_id": "sess-47a",
            "kind": "rule",
            "name": "code-quality",
            "phase": "rule",
            "ts_start": "2026-06-12T23:03:00Z",  # mid-design, long after spawn
            "ts_end": "2026-06-12T23:03:00Z",
            "status": "success",
            "summary": "re-synced rule",
        }
    )
    crafted = tmp_path / "spans.jsonl"
    crafted.write_text("\n".join(json.dumps(s) for s in spans) + "\n")
    forest = store_from(crafted, V3_TURNS).spoke_steps(SPOKE_RUN_ID)
    design = next(r for r in forest if r["name"] == "design")
    assert any("g_ctx_reload" in _ids(n) for n in _walk([design]) if n["kind"] == "context")


def _ids(node: dict) -> set[str]:
    out: set[str] = set()
    stack = list(node["children"])
    while stack:
        n = stack.pop()
        if n.get("span_id"):
            out.add(n["span_id"])
        stack.extend(n["children"])
    return out
