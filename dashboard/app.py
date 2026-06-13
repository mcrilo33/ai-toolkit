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

# `queries` is the sibling module in this directory; it resolves because
# `streamlit run dashboard/app.py` injects the script's directory onto sys.path.
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


def _step_label(row: dict) -> str:
    label = f"{row['kind']} · {row['name']}"
    if row["phase"]:
        label += f" · {row['phase']}"
    return label


def render_aggregate_view(store: queries.SpanStore) -> None:
    st.header("Aggregate view")
    st.caption(
        "Where time and tokens go: per-step rollup across all spokes in a window, "
        "normalized per invocation."
    )

    cols = st.columns(2)
    start = cols[0].text_input("Window start (ISO-8601 UTC, optional)", "")
    end = cols[1].text_input("Window end (ISO-8601 UTC, optional)", "")

    rows = store.aggregate(start.strip() or None, end.strip() or None)
    if not rows:
        st.info("No spans in this window.")
        return

    table = [
        {
            "Step": _step_label(row),
            "Freq": row["frequency"],
            "Mean time": _fmt_secs(row["mean_duration_ms"]),
            "Median time": _fmt_secs(row["median_duration_ms"]),
            "Total time": _fmt_secs(row["total_duration_ms"]),
            "Mean cost": _fmt_cost(row["mean_cost_usd"]),
            "Total cost": _fmt_cost(row["total_cost_usd"]),
            "Mean tok": f"{row['mean_tokens']:.0f}",
            "Human/inv": f"{row['human_per_invocation']:.2f}",
        }
        for row in rows
    ]
    st.dataframe(table, use_container_width=True, hide_index=True)


def _fmt_delta_secs(ms: float) -> str:
    arrow = "🔻" if ms < 0 else ("🔺" if ms > 0 else "▪️")
    return f"{arrow} {ms / 1000:+.1f}s"


def _fmt_delta_cost(usd: float) -> str:
    arrow = "🔻" if usd < 0 else ("🔺" if usd > 0 else "▪️")
    return f"{arrow} {usd:+.4f}"


def render_compare_view(store: queries.SpanStore) -> None:
    st.header("A/B compare view")
    st.caption(
        "Did a workflow change help? Per-step delta between two revisions, "
        "normalized per invocation. 🔻 = improvement, 🔺 = regression."
    )

    revs = store.workflow_revs()
    if len(revs) < 2:
        st.info("Need at least two workflow revisions to compare.")
        return

    cols = st.columns(2)
    rev_a = cols[0].selectbox("Baseline rev (A)", revs, index=0)
    rev_b = cols[1].selectbox("Candidate rev (B)", revs, index=len(revs) - 1)

    rows = store.ab_compare(rev_a, rev_b)
    low_conf = [r for r in rows if r["low_confidence"]]
    if low_conf:
        st.warning(
            f"{len(low_conf)} of {len(rows)} steps are low-confidence (small spoke "
            "counts) — deltas marked ⚠️ are noisy; don't read significance into them."
        )

    table = [
        {
            "Step": _step_label(row),
            "n (A→B)": f"{row['n_a']}→{row['n_b']}",
            "Δ time/inv": _fmt_delta_secs(row["delta_duration_ms"]),
            "Δ cost/inv": _fmt_delta_cost(row["delta_cost_usd"]),
            "Δ human/inv": f"{row['delta_human_per_invocation']:+.2f}",
            "Confidence": "⚠️ low" if row["low_confidence"] else "ok",
        }
        for row in rows
    ]
    st.dataframe(table, use_container_width=True, hide_index=True)


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
    view = st.sidebar.radio("View", ["Spoke", "Aggregate", "A/B compare"])
    if view == "Spoke":
        render_spoke_view(store)
    elif view == "Aggregate":
        render_aggregate_view(store)
    elif view == "A/B compare":
        render_compare_view(store)


if __name__ == "__main__":
    main()
