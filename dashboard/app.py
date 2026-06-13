# pyright: reportMissingImports=false
"""Streamlit UI for the workflow-observability dashboard (Issue #23).

100% local. Reads a local span log (the frozen v1 schema from Issue #21) and
renders three views — Spoke, Aggregate, A/B compare — plus an automatability
panel. It surfaces metrics only; it never reads or shows prompt content (the
span schema carries none).

Run via ``dashboard/run.sh`` (which calls ``streamlit run dashboard/app.py``).

The view-logic lives in ``queries.py`` and is unit-tested against fixtures; this
file is the thin presentation layer.
"""

from __future__ import annotations

import os
from pathlib import Path

import queries
import streamlit as st

_DEFAULT_TELEMETRY_DIR = Path.home() / ".ai-toolkit" / "telemetry"


def resolve_span_log() -> Path:
    """Resolve the span-log path from env, mirroring the schema's contract.

    Precedence: explicit ``AI_TOOLKIT_SPAN_LOG`` -> ``AI_TOOLKIT_TELEMETRY_DIR``
    -> ``~/.ai-toolkit/telemetry``, with ``events.jsonl`` inside the directory.
    """
    explicit = os.environ.get("AI_TOOLKIT_SPAN_LOG")
    if explicit:
        return Path(explicit)
    base = os.environ.get("AI_TOOLKIT_TELEMETRY_DIR")
    directory = Path(base) if base else _DEFAULT_TELEMETRY_DIR
    return directory / "events.jsonl"


@st.cache_data(show_spinner=False)
def _load_events(path: str, mtime: float) -> list[dict]:
    """Parse the span log. ``mtime`` is part of the cache key so a changed log
    invalidates the cache; it is intentionally unused in the body."""
    _ = mtime
    return queries.load_jsonl(path)


def load_store(path: Path) -> queries.SpanStore:
    mtime = path.stat().st_mtime
    return queries.SpanStore.from_events(_load_events(str(path), mtime))


def _fmt_secs(ms: int | float | None) -> str:
    return "—" if not ms else f"{ms / 1000:.1f}s"


def _fmt_cost(usd: float | None) -> str:
    return "—" if not usd else f"${usd:.4f}"


_STATUS_ICON = {
    "success": "✅",
    "failure": "❌",
    "deny": "🚫",
    "warn": "⚠️",
    "skipped": "⏭️",
}


def _render_node(node: dict, depth: int = 0) -> None:
    indent = "&nbsp;&nbsp;&nbsp;&nbsp;" * depth  # markdown-rendered indent per depth
    icon = _STATUS_ICON.get(node["status"], "•")
    label = node["name"]
    if node["phase"]:
        label += f" · {node['phase']}"
    human = ""
    if node["human_count"]:
        human = f"  👤 {node['human_type']} ({_fmt_secs(node['human_wait_ms'])})"

    cols = st.columns([5, 1, 1, 1])
    cols[0].markdown(f"{indent}{icon} `{node['kind']}` **{label}**{human}")
    cols[1].markdown(_fmt_secs(node["duration_ms"]))
    cols[2].markdown(_fmt_cost(node["cost_usd"]))
    tokens = (node["tokens_in"] or 0) + (node["tokens_out"] or 0)
    cols[3].markdown(f"{tokens:,} tok" if tokens else "—")

    for child in node["children"]:
        _render_node(child, depth + 1)


def render_spoke_view(store: queries.SpanStore) -> None:
    st.header("Spoke view")
    st.caption("One spoke run, drilled down: step → sub-step → hook.")

    spoke_ids = store.spoke_run_ids()
    if not spoke_ids:
        st.info("No spoke runs found in the span log.")
        return

    spoke_id = st.selectbox("Spoke run", spoke_ids)
    tree = store.spoke_tree(spoke_id)

    head = st.columns([5, 1, 1, 1])
    head[0].markdown("**Step**")
    head[1].markdown("**Time**")
    head[2].markdown("**Cost**")
    head[3].markdown("**Tokens**")
    for root in tree:
        _render_node(root)


def main() -> None:
    st.set_page_config(page_title="Workflow observability", layout="wide")
    st.title("Workflow observability dashboard")
    st.caption("100% local · metrics only, never prompt content")

    span_log = resolve_span_log()
    st.sidebar.subheader("Span log")
    st.sidebar.code(str(span_log))
    if not span_log.exists():
        st.warning(
            f"Span log not found at `{span_log}`. Set `AI_TOOLKIT_TELEMETRY=1` to "
            "record spans, or point `AI_TOOLKIT_SPAN_LOG` at an existing log."
        )
        return

    store = load_store(span_log)
    view = st.sidebar.radio("View", ["Spoke"])
    if view == "Spoke":
        render_spoke_view(store)


if __name__ == "__main__":
    main()
