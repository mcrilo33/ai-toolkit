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
import sys
from pathlib import Path
from typing import Any

# `queries` is the sibling module in this directory; it resolves because
# `streamlit run dashboard/app.py` injects the script's directory onto sys.path.
import queries
import streamlit as st

_DEFAULT_TELEMETRY_DIR = Path.home() / ".ai-toolkit" / "telemetry"
_DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _scripts_dir() -> Path:
    """The ai-toolkit ``scripts/`` dir (sibling of ``dashboard/``) holding #22."""
    return Path(__file__).resolve().parent.parent / "scripts"


def resolve_projects_dir() -> Path:
    """Claude session-logs root for Issue #22's pull-span parser."""
    base = os.environ.get("AI_TOOLKIT_PROJECTS_DIR")
    return Path(base) if base else _DEFAULT_PROJECTS_DIR


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


@st.cache_resource(show_spinner=False)
def _ccusage_costs() -> dict[str, float]:
    """Best-effort per-session ccusage cost map; empty on any failure.

    ccusage is an external ``npx`` tool, not a Python dep — if it is absent or
    errors, cost columns simply read blank rather than breaking the dashboard.
    """
    try:
        sys.path.insert(0, str(_scripts_dir()))
        from telemetry.cost import load_ccusage_costs

        return load_ccusage_costs()
    except Exception:
        return {}


@st.cache_resource(show_spinner=True)
def load_correlated_store(span_log: str, projects_dir: str) -> queries.SpanStore:
    """Build a store over Issue #22's correlated push+pull span dataset."""
    return queries.SpanStore.from_telemetry(
        events_path=span_log,
        projects_root=projects_dir,
        ccusage_costs=_ccusage_costs(),
        scripts_dir=_scripts_dir(),
    )


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


_STEP_COLS = [5, 1, 1, 1, 2, 1]
_STEP_HEADERS = ("Step", "Time", "Cost", "Tokens", "Model", "Agent")


def _node_row(node: dict, depth: int) -> None:
    """One drill-down row: indented label + rolled-up metric columns."""
    indent = "&nbsp;&nbsp;&nbsp;&nbsp;" * depth
    icon = _STATUS_ICON.get(node["status"], "•")
    metrics = queries.format_step_metrics(node)
    cols = st.columns(_STEP_COLS)
    cols[0].markdown(f"{indent}{icon} `{node['kind']}` **{queries.format_step_label(node)}**")
    cols[1].markdown(metrics["time"])
    cols[2].markdown(metrics["cost"])
    cols[3].markdown(metrics["tokens"])
    cols[4].markdown(metrics["model"])
    cols[5].markdown(metrics["agent"])


def _render_descendants(nodes: list[dict], depth: int) -> None:
    """Render a node's subtree as indented rows; collapsed hooks stay one line."""
    for node in nodes:
        _node_row(node, depth)
        # A hooks node is the collapsed line itself — never expand its children.
        if node["kind"] != "hooks":
            _render_descendants(node["children"], depth + 1)


def _render_step(root: dict) -> None:
    """A Level-1 step row (rolled-up metrics, collapsed) with a drill expander.

    Streamlit forbids nesting expanders, so the whole subtree drills inside one
    expander as indented rows rather than per-level expanders.
    """
    _node_row(root, 0)
    if root["children"]:
        with st.expander(f"↳ drill into {queries.format_step_label(root)}", expanded=False):
            _render_descendants(root["children"], 1)


def _render_meta(store: queries.SpanStore, spoke_id: str) -> None:
    rows = store.spoke_meta_by_kind(spoke_id)
    if not rows:
        st.info("No spans to aggregate for this spoke.")
        return
    table = [
        {
            "Kind": row["kind"],
            "Count": row["count"],
            "Total time": _fmt_secs(row["total_duration_ms"]),
            "Median time": _fmt_secs(row["median_duration_ms"]),
            "Total cost": _fmt_cost(row["total_cost_usd"]),
            "Mean cost": _fmt_cost(row["mean_cost_usd"]),
            "Models": ", ".join(m.removeprefix("claude-") for m in row["models"]) or "—",
        }
        for row in rows
    ]
    st.dataframe(table, use_container_width=True, hide_index=True)
    st.caption(
        "Cost is counted once per turn; summed across kinds it is the run total "
        "minus any untracked (non-span) turns."
    )


def render_spoke_view(store: queries.SpanStore) -> None:
    st.header("Spoke view")
    st.caption(
        "Collapse-to-steps drill-down: main steps with rolled-up metrics; expand a "
        "step to drill into sub-steps and spans. Hooks collapse into one line."
    )

    spoke_ids = store.spoke_run_ids()
    if not spoke_ids:
        st.info("No spoke runs found in the span log.")
        return

    spoke_id = st.selectbox("Spoke run", spoke_ids)
    steps_tab, meta_tab = st.tabs(["Steps", "Meta by kind"])

    with steps_tab:
        forest = store.spoke_steps(spoke_id)
        if not forest:
            st.info("No spans for this spoke run.")
        else:
            head = st.columns(_STEP_COLS)
            for col, name in zip(head, _STEP_HEADERS, strict=True):
                col.markdown(f"**{name}**")
            for root in forest:
                _render_step(root)

    with meta_tab:
        _render_meta(store, spoke_id)


def _step_label(row: dict[str, Any]) -> str:
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


def _delta_arrow(value: float) -> str:
    """Less is better across time/cost/human, so a negative delta improves."""
    return "🔻" if value < 0 else ("🔺" if value > 0 else "▪️")


def _fmt_delta_secs(ms: float) -> str:
    return f"{_delta_arrow(ms)} {ms / 1000:+.1f}s"


def _fmt_delta_cost(usd: float) -> str:
    return f"{_delta_arrow(usd)} {usd:+.4f}"


def _fmt_delta_count(value: float) -> str:
    return f"{_delta_arrow(value)} {value:+.2f}"


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
            "Δ human/inv": _fmt_delta_count(row["delta_human_per_invocation"]),
            "Confidence": "⚠️ low" if row["low_confidence"] else "ok",
        }
        for row in rows
    ]
    st.dataframe(table, use_container_width=True, hide_index=True)


def _interaction_label(row: dict[str, Any]) -> str:
    label = f"{row['human_type']} · {row['name']}"
    if row["phase"]:
        label += f" · {row['phase']}"
    return label


def render_automatability_view(store: queries.SpanStore) -> None:
    st.header("Automatability candidates")
    st.caption(
        "Human interactions ranked by frequency x low decision-variance x "
        "on-critical-path. This surfaces what's worth a closer look — it does "
        "not decide whether an interaction is actually automatable."
    )

    min_freq = st.slider("Minimum frequency", 1, 10, 1)
    rows = store.automatability_candidates(min_frequency=min_freq)
    if not rows:
        st.info("No human interactions recorded.")
        return

    table = [
        {
            "Interaction": _interaction_label(row),
            "Score": f"{row['score']:.2f}",
            "Freq": row["frequency"],
            "Consistency": f"{row['consistency']:.0%}",
            "On critical path": f"{row['on_critical_path']:.0%}",
            "Mean wait": _fmt_secs(row["mean_wait_ms"]),
        }
        for row in rows
    ]
    st.dataframe(table, use_container_width=True, hide_index=True)


def _resolve_store(span_log: Path) -> queries.SpanStore | None:
    """Pick the span source from the sidebar and build the store, or warn."""
    correlated = st.sidebar.toggle("Correlate via Issue #22 (session logs + ccusage)", value=False)
    if not correlated:
        st.sidebar.caption(f"Raw push-span log:\n`{span_log}`")
        if not span_log.exists():
            st.warning(
                f"Span log not found at `{span_log}`. Set `AI_TOOLKIT_TELEMETRY=1` "
                "to record spans, or point `AI_TOOLKIT_SPAN_LOG` at an existing log."
            )
            return None
        return load_store(span_log)

    projects_dir = resolve_projects_dir()
    st.sidebar.caption(f"Push log:\n`{span_log}`\nSession logs:\n`{projects_dir}`")
    if not projects_dir.exists():
        st.warning(
            f"Claude session logs not found at `{projects_dir}`. Set "
            "`AI_TOOLKIT_PROJECTS_DIR`, or switch off correlation."
        )
        return None
    return load_correlated_store(str(span_log), str(projects_dir))


def main() -> None:
    st.set_page_config(page_title="Workflow observability", layout="wide")
    st.title("Workflow observability dashboard")
    st.caption("100% local · metrics only, never prompt content")

    st.sidebar.subheader("Span source")
    store = _resolve_store(resolve_span_log())
    if store is None:
        return

    view = st.sidebar.radio("View", ["Spoke", "Aggregate", "A/B compare", "Automatability"])
    if view == "Spoke":
        render_spoke_view(store)
    elif view == "Aggregate":
        render_aggregate_view(store)
    elif view == "A/B compare":
        render_compare_view(store)
    elif view == "Automatability":
        render_automatability_view(store)


if __name__ == "__main__":
    main()
