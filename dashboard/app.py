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


# v3 columns (docs/dashboard-spoke-trace-scope.md): Time is the start clock and a
# separate Dur holds the wall-clock; there is no Date column (day rollover renders
# as a divider) and no Model column (model folds into the per-turn panel). Actor is
# the owner — main, a sub-agent name, workflow, script, hooks, or sidecar.
_STEP_COLS = [5, 1, 1, 1, 1, 1, 2]
_STEP_HEADERS = ("Node", "Time", "Dur", "Cost", "Tokens", "H", "Actor")

# Synthetic divider kinds render as a thin full-width row, never a metric row.
_DIVIDER_KINDS = {"gap", "session"}

# A kind whose Actor is fixed regardless of the v2 ``agent`` field.
_ACTOR_BY_KIND = {
    "workflow": "workflow",
    "workflow_phase": "workflow",
    "script": "script",
    "hook": "hooks",
    "hooks": "hooks",
}


def _actor_label(node: dict) -> str:
    """The Actor column value: explicit ``actor`` wins, else derived from kind.

    A fixed kind (``hooks``/``workflow``/``script``) is structurally never ``main``,
    so its kind is authoritative over an unfilled contract default. Otherwise an
    explicit #50 ``actor`` wins, then a sub-agent span reads as its own name
    (``Explore``, ``code-review``), and everything else falls back to the v2
    ``agent`` field (``main`` / ``subagent``).
    """
    kind = node["kind"]
    if kind in _ACTOR_BY_KIND:
        return _ACTOR_BY_KIND[kind]
    actor = node.get("actor")
    if actor:
        return actor
    if kind == "agent":
        return node.get("name") or "subagent"
    return node.get("agent", "main")


def _node_label(node: dict) -> str:
    """The Node-cell label: an ``xN`` line for a collapsed group, else the step label.

    A collapsed group (``collapsed_count``) reads ``<kind> xN`` for any kind — the
    query-layer ``format_step_label`` only special-cases ``hooks``. Any ``badges``
    the tree attached (``ctx-bust``, ``⟨from todo — no marker⟩``) trail as tags.
    """
    count = node.get("collapsed_count")
    label = f"{node['kind']} x{count}" if count else queries.format_step_label(node)
    badges = node.get("badges")
    if badges:
        label += " " + " ".join(f"`{badge}`" for badge in badges)
    return label


def _render_divider(node: dict) -> None:
    """A gap (idle) or session-resume node as a thin divider, not a metric row."""
    if node["kind"] == "session":
        cache = node.get("own_tokens_in") or 0
        note = f" · cold cache (+{cache:,})" if cache else " · cold cache"
        st.markdown(f"··· session resume{note} ···")
        return
    st.markdown(f"··· idle · {node['name']} ···")


def _node_row(node: dict, depth: int) -> None:
    """One trace row: indented label + Time(start clock)·Dur·Cost·Tokens·H·Actor."""
    indent = "&nbsp;&nbsp;&nbsp;&nbsp;" * depth
    icon = _STATUS_ICON.get(node["status"], "•")
    metrics = queries.format_step_metrics(node)
    cols = st.columns(_STEP_COLS)
    cols[0].markdown(f"{indent}{icon} `{node['kind']}` **{_node_label(node)}**")
    cols[1].markdown(queries._clock(node.get("ts_start")))
    cols[2].markdown(metrics["time"])
    cols[3].markdown(metrics["cost"])
    cols[4].markdown(metrics["tokens"])
    cols[5].markdown(metrics["humans"])
    cols[6].markdown(_actor_label(node))


def _render_descendants(nodes: list[dict], depth: int) -> None:
    """Render a subtree as indented rows.

    A divider kind renders inline; a collapsed ``xN`` group gates its members behind
    a checkbox so drilling toggles already-built rows (never a rebuild). A v2 hooks
    node carries ``collapsed_count`` and so drills through that same gate.
    """
    for node in nodes:
        if node["kind"] in _DIVIDER_KINDS:
            _render_divider(node)
            continue
        _node_row(node, depth)
        if node.get("collapsed_count"):
            if st.checkbox(f"show {_node_label(node)} members", key=f"drill-{id(node)}"):
                _render_descendants(node["children"], depth + 1)
            continue
        _render_descendants(node["children"], depth + 1)


def _render_step(root: dict) -> None:
    """A Level-1 spine row with a drill expander (or a thin divider for idle/resume).

    Streamlit forbids nesting expanders, so the whole subtree drills inside one
    expander as indentation depth (Issue #47 S3): marker headers, then the turn
    nodes, with the tools/skills each turn issued nested beneath — and a sub-agent's
    own turns under its agent node. Wide leaf groups drill through a checkbox toggle.
    """
    if root["kind"] in _DIVIDER_KINDS:
        _render_divider(root)
        return
    _node_row(root, 0)
    if root["children"]:
        with st.expander(f"↳ drill into {_node_label(root)}", expanded=False):
            _render_descendants(root["children"], 1)


def _date_of(ts: str | None) -> str | None:
    """The ``YYYY-MM-DD`` of an ISO timestamp, or None when absent/malformed."""
    return ts.split("T", 1)[0] if ts and "T" in ts else None


def _render_spine(forest: list[dict]) -> None:
    """Render the L1 trace spine: a date-divider on day rollover, then each step.

    No Date column — a thin date-divider row marks the day rollover (the first day
    gets none). Idle/session-resume roots render as dividers, not metric rows.
    """
    prev_date: str | None = None
    for root in forest:
        date = _date_of(root.get("ts_start"))
        if date and prev_date and date != prev_date:
            st.markdown(f"**📅 {date}**")
        prev_date = date or prev_date
        _render_step(root)


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
            "Models": ", ".join(queries._short_model(m) for m in row["models"]) or "—",
        }
        for row in rows
    ]
    st.dataframe(table, use_container_width=True, hide_index=True)
    st.caption(
        "Cost is counted once per turn. Main-agent cost belongs to its phase "
        "interval, so summed across span kinds this is the subagent total — the "
        "run total minus the setup / phase / unresolved (non-span) buckets."
    )


def render_spoke_view(store: queries.SpanStore) -> None:
    st.header("Spoke view")
    st.caption(
        "Collapse-to-steps drill-down: main steps with rolled-up metrics; expand a "
        "step to drill into sub-steps and spans. Hooks collapse into one line."
    )

    spoke_ids = store.spoke_run_ids(queries.REAL_REPO_PREFIX)  # #55: hide fixture-leak spokes
    if not spoke_ids:
        st.info("No spoke runs found in the span log.")
        return

    spoke_id = st.selectbox("Spoke run", spoke_ids, format_func=queries.format_spoke_label)
    steps_tab, meta_tab = st.tabs(["Steps", "Meta by kind"])

    with steps_tab:
        forest = store.spoke_steps(spoke_id)
        if not forest:
            st.info("No spans for this spoke run.")
        else:
            head = st.columns(_STEP_COLS)
            for col, name in zip(head, _STEP_HEADERS, strict=True):
                col.markdown(f"**{name}**")
            _render_spine(forest)

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


def resolve_mode(correlated_requested: bool, projects_dir: Path) -> str:
    """Pick the span source: ``"correlated"`` or ``"raw"``.

    Correlation needs Issue #22's Claude session logs; when that dir is absent we
    fall back to the raw push-span log rather than blanking the dashboard.
    """
    return "correlated" if correlated_requested and projects_dir.exists() else "raw"


def _resolve_store(span_log: Path) -> queries.SpanStore | None:
    """Pick the span source from the sidebar and build the store, or warn.

    Correlation is on by default; when it's requested but the session-logs dir is
    missing, the mode falls back to raw (with a note) instead of returning None.
    """
    correlated = st.sidebar.toggle("Correlate via Issue #22 (session logs + ccusage)", value=True)
    projects_dir = resolve_projects_dir()

    if resolve_mode(correlated, projects_dir) == "correlated":
        st.sidebar.caption(f"Push log:\n`{span_log}`\nSession logs:\n`{projects_dir}`")
        return load_correlated_store(str(span_log), str(projects_dir))

    if correlated:
        st.sidebar.info(
            f"Claude session logs not found at `{projects_dir}`; showing the raw "
            "push-span log. Set `AI_TOOLKIT_PROJECTS_DIR` to correlate."
        )
    st.sidebar.caption(f"Raw push-span log:\n`{span_log}`")
    if not span_log.exists():
        st.warning(
            f"Span log not found at `{span_log}`. Set `AI_TOOLKIT_TELEMETRY=1` "
            "to record spans, or point `AI_TOOLKIT_SPAN_LOG` at an existing log."
        )
        return None
    return load_store(span_log)


def render_morning_view(store: queries.SpanStore) -> None:
    st.header("Morning")
    st.caption(
        "Last night's spoke runs — the cost lens that complements the shell worklist "
        "(`hub-morning.sh`). Land readiness is the night's land-triage verdict."
    )
    rows = store.morning_rows()
    if not rows:
        st.info("No spoke runs found.")
        return
    table = [
        {
            "Issue": f"#{row['issue']}" if row["issue"] else "—",
            "Spoke run": row["spoke_run_id"],
            "Cost": _fmt_cost(row["total_cost_usd"]),
            "Land": row["merge"] or "—",
        }
        for row in rows
    ]
    st.dataframe(table, use_container_width=True, hide_index=True)


def main() -> None:
    st.set_page_config(page_title="Workflow observability", layout="wide")
    st.title("Workflow observability dashboard")
    st.caption("100% local · metrics only, never prompt content")

    st.sidebar.subheader("Span source")
    store = _resolve_store(resolve_span_log())
    if store is None:
        return

    view = st.sidebar.radio(
        "View", ["Spoke", "Morning", "Aggregate", "A/B compare", "Automatability"]
    )
    if view == "Spoke":
        render_spoke_view(store)
    elif view == "Morning":
        render_morning_view(store)
    elif view == "Aggregate":
        render_aggregate_view(store)
    elif view == "A/B compare":
        render_compare_view(store)
    elif view == "Automatability":
        render_automatability_view(store)


if __name__ == "__main__":
    main()
