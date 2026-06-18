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
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# `queries` is the sibling module in this directory; it resolves because
# `streamlit run dashboard/app.py` injects the script's directory onto sys.path.
import duckdb
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


def resolve_store_path() -> Path:
    """The persisted DuckDB store path (Issue #62), beside the span-log WAL."""
    base = os.environ.get("AI_TOOLKIT_TELEMETRY_DIR")
    directory = Path(base) if base else _DEFAULT_TELEMETRY_DIR
    return directory / "store.duckdb"


# Loaded-context items resolve their bodies from the synced toolkit configs (Issue #68).
_DEFAULT_RULES_DIR = Path(__file__).resolve().parent.parent / ".claude" / "rules"
_DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "skills"


def resolve_summary_cache_path() -> Path:
    """The JSON sidecar caching one-line context summaries (Issue #68)."""
    base = os.environ.get("AI_TOOLKIT_TELEMETRY_DIR")
    directory = Path(base) if base else _DEFAULT_TELEMETRY_DIR
    return directory / "summaries.json"


@st.cache_resource(show_spinner=False)
def _summary_cache() -> Any:
    """Process-wide content-hash summary cache, loaded once and reused across reruns."""
    sys.path.insert(0, str(_scripts_dir()))
    from telemetry.summarizer import SummaryCache

    return SummaryCache(resolve_summary_cache_path())


def _context_summary(kind: str, identifier: str) -> str:
    """One-line cached LLM "what this is" for a rule/skill/reasoning item.

    Fail-soft and cached by content hash: an unconfigured backend or a missing body
    yields a blank, and identical content is summarized once across reruns and spokes.
    """
    if not identifier:
        return ""
    sys.path.insert(0, str(_scripts_dir()))
    from telemetry.summarizer import context_summary

    return context_summary(
        kind,
        identifier,
        cache=_summary_cache(),
        rules_dir=_DEFAULT_RULES_DIR,
        skills_dir=_DEFAULT_SKILLS_DIR,
    )


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


def _materialize_lock_safe(store_path: str) -> queries.SpanStore:
    """Read the persisted store, falling back to a snapshot copy if a writer holds it.

    DuckDB is single-writer (Issue #75): while another dashboard instance has the store
    open read-write, even a read-only ATTACH of the live file raises ``IOException``. On
    that lock we read a lock-free snapshot copy of the store instead of crashing — the
    copy carries the committed data and is discarded once materialized.
    """
    try:
        return queries.SpanStore.from_persisted_store(store_path)
    except duckdb.IOException:
        sys.path.insert(0, str(_scripts_dir()))
        from telemetry.store import snapshot_store

        snap = snapshot_store(store_path)
        try:
            return queries.SpanStore.from_persisted_store(snap)
        finally:
            shutil.rmtree(snap.parent, ignore_errors=True)


@st.cache_resource(show_spinner=False)
def _materialize_store(store_path: str, version: str) -> queries.SpanStore:
    """Copy the persisted store into an in-memory read model (Issue #62).

    Cached on the store's content ``version`` so a Streamlit rerun reuses the model
    and only a real delta rebuilds it; ``version`` keys the cache and is intentionally
    unused in the body. The read is lock-safe (#75): a concurrent writer's lock routes
    through a snapshot copy rather than crashing the instance.
    """
    _ = version
    return _materialize_lock_safe(store_path)


def load_correlated_store(
    span_log: str, projects_dir: str, store_path: str
) -> tuple[queries.SpanStore, str]:
    """Delta-ingest the persisted store on open, then materialize the read model.

    The ingest runs on every open but only parses new/changed *post-watermark*
    sessions (the historical backlog is never read), so it is fast; it returns a
    content version on which the in-memory read model is cached. The store is created
    empty at a watermark on first run, so a cold open is ~instant and spokes appear as
    they run — pre-watermark spokes are intentionally absent.
    """
    sys.path.insert(0, str(_scripts_dir()))
    from telemetry.store import ingest_store

    version = ingest_store(
        store_path,
        events_path=Path(span_log),
        projects_root=Path(projects_dir),
        ccusage_costs=_ccusage_costs(),
    )
    return _materialize_store(store_path, version), version


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


def _actor_label(node: dict, inherited_actor: str = "main") -> str:
    """The Actor column value: explicit ``actor`` wins, else derived from kind/context.

    A fixed kind (``hooks``/``workflow``/``script``) is structurally never ``main``,
    so its kind is authoritative over an unfilled contract default. Otherwise an
    explicit #50 ``actor`` wins, then a sub-agent span reads as its own name
    (``Explore``, ``code-review``). For anything else, ``inherited_actor`` — the
    enclosing sub-agent passed down by the renderer — owns it (a sub-agent's tools
    and turns are the sub-agent's, even though the tree tags their own ``agent`` as
    ``main`` by kind); at the top level that inheritance is ``main``, so we fall back
    to the node's own ``agent`` field to keep an orphaned ``subagent`` honest.
    """
    kind = node["kind"]
    if kind in _ACTOR_BY_KIND:
        return _ACTOR_BY_KIND[kind]
    actor = node.get("actor")
    if actor:
        return actor
    if kind == "agent":
        return node.get("name") or "subagent"
    return inherited_actor if inherited_actor != "main" else node.get("agent", "main")


def _child_actor(node: dict, inherited_actor: str) -> str:
    """The actor an ``node``'s children inherit: the sub-agent name, else passed through."""
    if node["kind"] == "agent":
        return node.get("name") or inherited_actor
    return node.get("actor") or inherited_actor


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
    # A gate-blocked tool reads as never-run regardless of its summary text — the
    # tool carries status='deny' only when a deny approval blocked it (Issue #60).
    if node.get("kind") == "tool" and node.get("status") == "deny":
        label += " `never-run`"
    return label


def _row_glyph(node: dict) -> str:
    """The row's leading glyph: 📐 for a per-turn context node, else the status icon.

    A causal context node (it carries ``input_context``) is input *state*, not an event
    with an outcome, so it reads as the spec's ``📐 context`` rather than a ✅ tick. Every
    other node shows its rolled-up terminal status icon (a leaf's rollup is its own).
    """
    if node["kind"] == "context" and node.get("input_context") is not None:
        return "📐"
    status = (node.get("rollup") or {}).get("status") or node["status"]
    return _STATUS_ICON.get(status, "•")


def _context_item_rows(ctx: dict) -> list[dict]:
    """The per-item drill rows of a context node's input state, each with its tokens.

    One row per named load — each rule, ``CLAUDE.md``, each memory, then the tool-schemas
    as one ``tool-schemas xN`` group — followed by the history remainder, in
    load-then-history order. Each row carries a ``$0`` cost slot (loaded context bears no
    inference cost) and the row tokens reconcile to the context total (Issue #67).
    """
    rows: list[dict] = []
    for rule in ctx["rules"]:
        rows.append(
            {
                "label": f"rule · {rule['name']}",
                "tokens": rule["tokens"],
                "cost": 0.0,
                "summary_kind": "rule",
                "summary_id": rule["name"],
            }
        )
    if ctx["claude_md"]:
        rows.append({"label": "CLAUDE.md", "tokens": ctx["claude_md"]["tokens"], "cost": 0.0})
    for memory in ctx["memory"]:
        rows.append(
            {"label": f"memory · {memory['name']}", "tokens": memory["tokens"], "cost": 0.0}
        )
    schemas = ctx["schemas"]
    if schemas["count"]:
        rows.append(
            {"label": f"tool-schemas x{schemas['count']}", "tokens": schemas["tokens"], "cost": 0.0}
        )
    rows.append({"label": "history", "tokens": ctx["history_tokens"], "cost": 0.0})
    return rows


def _render_context_item(item: dict, depth: int) -> None:
    """One drilled context-item row: indented name + its tokens in the Tokens column.

    A named load has no clock/duration/actor of its own; the Cost column is its ``$0``
    slot and the trailing column holds the cached one-line "what this is" summary
    (Issue #68) for a rule, blank for loads with no resolvable body.
    """
    indent = "&nbsp;&nbsp;&nbsp;&nbsp;" * depth
    cols = st.columns(_STEP_COLS)
    cols[0].markdown(f"{indent}↳ {item['label']}")
    cols[1].markdown("")
    cols[2].markdown("")
    cols[3].markdown(_fmt_cost(item["cost"]))
    cols[4].markdown(f"{item['tokens']:,}")
    cols[5].markdown("")
    summary = (
        _context_summary(item["summary_kind"], item["summary_id"])
        if item.get("summary_kind")
        else ""
    )
    cols[6].markdown(f"_{summary}_" if summary else "")


def _render_divider(node: dict) -> None:
    """A gap (idle) or session-resume node as a thin divider, not a metric row."""
    if node["kind"] == "session":
        # The resume cold-cache magnitude rides on resume_cache_creation, never
        # own_tokens_in — it must not fold into the exact once-per-turn rollup (#59).
        cache = node.get("resume_cache_creation") or 0
        note = f" · cold cache (+{cache:,})" if cache else " · cold cache"
        st.markdown(f"··· session resume{note} ···")
        return
    st.markdown(f"··· idle · {node['name']} ···")


def _inline_node_summary(node: dict) -> str:
    """The cached one-line "what this is" trailing a skill or reasoning leaf (Issue #68).

    A skill resolves from its ``SKILL.md`` body; a reasoning leaf summarizes its own
    privacy-safe gist. Any other kind, or an unconfigured backend, yields a blank.
    """
    kind = node.get("kind")
    if kind == "skill":
        return _context_summary("skill", node.get("name") or "")
    if kind == "reasoning":
        return _context_summary("reasoning", node.get("summary") or "")
    return ""


def _node_row(node: dict, depth: int, inherited_actor: str = "main") -> None:
    """One trace row: indented label + Time(start clock)·Dur·Cost·Tokens·H·Actor."""
    indent = "&nbsp;&nbsp;&nbsp;&nbsp;" * depth
    # The leading glyph is the rolled-up (terminal) status — a container reads as its
    # last-event outcome, not a worst-child reddening (Issue #57), a leaf's rollup
    # equals its own status — except a per-turn context node, which reads as 📐 (#67).
    icon = _row_glyph(node)
    metrics = queries.format_step_metrics(node)
    cols = st.columns(_STEP_COLS)
    label = f"**{_node_label(node)}**"
    inline = _inline_node_summary(node)
    if inline:
        label += f" — _{inline}_"
    cols[0].markdown(f"{indent}{icon} `{node['kind']}` {label}")
    cols[1].markdown(queries._clock(node.get("ts_start")))
    cols[2].markdown(metrics["time"])
    cols[3].markdown(metrics["cost"])
    cols[4].markdown(metrics["tokens"])
    cols[5].markdown(metrics["humans"])
    cols[6].markdown(_actor_label(node, inherited_actor))


def _render_node(node: dict, depth: int, path: str, inherited_actor: str = "main") -> None:
    """Render one node and, when drilled open, its children — uniformly at any depth.

    Streamlit forbids nested expanders, so drilling uses a ``st.toggle`` per node
    instead: one consistent control for steps, agents, and collapsed ``xN`` groups
    alike. The toggle's state persists across reruns by its ``path`` key (no
    re-collapse trap), and children render only while it is open (lazy). A divider
    kind (idle gap / session resume) renders inline with no drill. ``inherited_actor``
    flows the enclosing sub-agent down so its tools/turns read as the sub-agent.
    """
    if node["kind"] in _DIVIDER_KINDS:
        _render_divider(node)
        return
    _node_row(node, depth, inherited_actor)
    # A per-turn context node holds its named items in ``input_context`` (not children),
    # so it drills into those rows — each named load + the history remainder (Issue #67).
    ctx = node.get("input_context")
    if ctx is not None:
        if st.toggle(f"↳ drill into {_node_label(node)}", key=f"drill::{path}", value=False):
            for item in _context_item_rows(ctx):
                _render_context_item(item, depth + 1)
        return
    children = node.get("children") or []
    if not children:
        return
    if st.toggle(f"↳ drill into {_node_label(node)}", key=f"drill::{path}", value=False):
        child_actor = _child_actor(node, inherited_actor)
        for index, child in enumerate(children):
            _render_node(child, depth + 1, f"{path}.{index}", child_actor)


def _date_of(ts: str | None) -> str | None:
    """The ``YYYY-MM-DD`` of an ISO timestamp, or None when absent/malformed."""
    return ts.split("T", 1)[0] if ts and "T" in ts else None


def _render_spine(forest: list[dict]) -> None:
    """Render the L1 trace spine: a date-divider on day rollover, then each step.

    No Date column — a thin date-divider row marks the day rollover (the first day
    gets none). Idle/session-resume roots render as dividers, not metric rows. Each
    step drills through the uniform per-node toggle in :func:`_render_node`.
    """
    prev_date: str | None = None
    for index, root in enumerate(forest):
        date = _date_of(root.get("ts_start"))
        if date and prev_date and date != prev_date:
            st.markdown(f"**📅 {date}**")
        prev_date = date or prev_date
        _render_node(root, 0, str(index))


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
            # Mean human wait — set only for timed interactions (approvals); an
            # em dash for kinds that never waited on a human (Issue #60).
            "Mean wait": _fmt_secs(row.get("mean_wait_ms")),
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


def _composition_totals(forest: list[dict]) -> dict[str, float]:
    """Exact usage totals reconciled over the whole forest (each turn counted once).

    Sums the additive root ``rollup`` so the total is the spoke's true usage —
    main-agent turns (under interval buckets) included, unlike the per-kind meta
    view which only sees span-bearing kinds.
    """
    cost = 0.0
    tokens_in = 0
    tokens_out = 0
    for root in forest:
        rollup = root.get("rollup") or {}
        cost += rollup.get("cost_usd", 0.0)
        tokens_in += rollup.get("tokens_in", 0)
        tokens_out += rollup.get("tokens_out", 0)
    return {"cost_usd": cost, "tokens_in": tokens_in, "tokens_out": tokens_out}


def _composition_cache_totals(forest: list[dict]) -> dict[str, int]:
    """Spoke cache breakdown — cheap reuse (``cache_read``) vs cold writes
    (``cache_creation``) — summed across every turn node in the forest (Issue #59)."""
    read = 0
    creation = 0

    def _walk(nodes: list[dict]) -> None:
        nonlocal read, creation
        for node in nodes:
            read += node.get("cache_read") or 0
            creation += node.get("cache_creation") or 0
            _walk(node.get("children", []))

    _walk(forest)
    return {"cache_read": read, "cache_creation": creation}


def _render_composition(forest: list[dict]) -> None:
    """The context-composition bar: exact usage totals + cache framing + a modeled split.

    The totals are exact (reconciled to the once-per-turn rollup); the cache breakdown
    frames cheap reuse (``cache_read``) against cold writes (``cache_creation``); the
    prefix/skills/memory/history split is modeled from artifact sizes and labelled an
    estimate (scope doc: only in/out/cost totals are exact).
    """
    totals = _composition_totals(forest)
    cols = st.columns(3)
    cols[0].metric("Tokens in (exact)", f"{totals['tokens_in']:,}")
    cols[1].metric("Tokens out (exact)", f"{totals['tokens_out']:,}")
    cols[2].metric("Cost (exact)", _fmt_cost(totals["cost_usd"]))
    cache = _composition_cache_totals(forest)
    cache_cols = st.columns(2)
    cache_cols[0].metric("Cache read (reuse)", f"{cache['cache_read']:,}")
    cache_cols[1].metric("Cache creation (cold)", f"{cache['cache_creation']:,}")
    st.caption(
        "Usage totals are exact — reconciled to the run's once-per-turn rollup, so "
        "they include the main-agent cost the per-kind view omits. Cache read is cheap "
        "prompt reuse; cache creation is the expensive cold write (largest on session "
        "resume). The prefix / skills / memory / history split is a modeled estimate "
        "from artifact sizes."
    )


def _subtree_tokens(node: dict) -> int:
    """Total tokens consumed anywhere in ``node``'s subtree (the 'exercised' signal)."""
    total = (node.get("own_tokens_in") or 0) + (node.get("own_tokens_out") or 0)
    for child in node.get("children", []):
        total += _subtree_tokens(child)
    return total


def _cold_context(forest: list[dict]) -> list[dict]:
    """Loaded-context items never exercised: ``context`` nodes with zero subtree usage.

    A rule / tool-schema / memory recall loaded but never used (no tokens consumed
    under it) is a trimming / automation candidate.
    """
    cold: list[dict] = []

    def _walk(nodes: list[dict]) -> None:
        for node in nodes:
            if node["kind"] == "context" and _subtree_tokens(node) == 0:
                cold.append(node)
            _walk(node.get("children", []))

    _walk(forest)
    return cold


def _render_cold_context(forest: list[dict]) -> None:
    cold = _cold_context(forest)
    if not cold:
        st.info("No cold (unexercised) context loaded.")
        return
    st.caption("Context loaded but never exercised — trimming / automation candidates.")
    for node in cold:
        st.markdown(f"• `{node['kind']}` {node['name']}")


# Per-spoke built-forest cache, keyed on (spoke_id, source_key). Module-level so it
# survives Streamlit reruns within a session; a fresh import (a new session) starts
# empty. ``source_key`` encodes the data source — the correlation mode AND the log
# mtime — so toggling correlation or a changed log rebuilds against the right store
# (the store caches are keyed on the same mtime). The forest is plain dicts, so a
# cached entry is reused by reference: a drill toggle re-renders the same objects.
_FOREST_CACHE: dict[tuple[str, str], list[dict]] = {}


def _spoke_forest(store: queries.SpanStore, spoke_id: str, source_key: str) -> list[dict]:
    """The selected spoke's drill-down tree, built on demand and memoized.

    Builds only the requested spoke, keyed on ``(spoke_id, source_key)`` so a re-select
    or rerun returns the cached forest instantly while a new data source (mode toggle or
    changed log) rebuilds.
    """
    key = (spoke_id, source_key)
    if key not in _FOREST_CACHE:
        # Live-follow folds the transcript mtime into ``source_key``, so a growing spoke
        # would otherwise leave one entry per refresh. The UI only ever requests the
        # current build, so a fresh build supersedes any prior same-spoke entry — evict
        # them to keep the cache bounded (#67). A static (same-key) refresh hits the
        # branch below and reuses the cached object by reference, so a drill never rebuilds.
        for stale in [cached for cached in _FOREST_CACHE if cached[0] == spoke_id]:
            del _FOREST_CACHE[stale]
        _FOREST_CACHE[key] = _build_spoke_forest(store, spoke_id)
    return _FOREST_CACHE[key]


def _build_spoke_forest(store: queries.SpanStore, spoke_id: str) -> list[dict]:
    """The v3 **causal** trace for one spoke — the sole builder (Issues #65/#80).

    Parses ONLY this spoke's own sessions (located from its push spans); a spoke with no
    transcript on disk (a push-only dataset or a fixture) yields an empty forest, which
    the caller renders as an empty state. The legacy timestamp-bucketed ``spoke_steps``
    builder and its silent fallback are gone (#80): a genuine build failure propagates so
    the view surfaces it via :func:`_build_or_error`, never silently renders the old
    broken model.
    """
    return store.spoke_causal_forest(spoke_id, resolve_projects_dir(), _ccusage_costs())


def _build_or_error(store: queries.SpanStore, spoke_id: str, source_key: str) -> list[dict] | None:
    """The spoke's causal forest, or ``None`` after surfacing an explicit ``st.error``.

    The causal forest is the only builder (#80); a build failure is shown to the user
    rather than silently falling back to the removed legacy tree. ``None`` (build failed,
    error shown) is distinct from ``[]`` (a legitimately empty forest).
    """
    try:
        return _spoke_forest(store, spoke_id, source_key)
    except Exception as exc:  # noqa: BLE001 — any build failure must reach the user
        st.error(f"Could not build the causal trace for this spoke: {exc}")
        return None


# Live-follow (Issue #67). A spoke whose transcript changed within this window is taken
# to be still running, so the view offers to auto-refresh the causal tree as the
# transcript grows — no live-emission hook, no daemon, just a re-read of the file.
_LIVE_WINDOW_SECS = 120.0
_LIVE_REFRESH = "8s"  # auto-refresh cadence for a followed spoke (the spec's ~5–10s)


def _spoke_is_running(
    transcript_mtime: float, now: float, window: float = _LIVE_WINDOW_SECS
) -> bool:
    """True when the spoke's transcript was written within ``window`` seconds of ``now``.

    The hook-free / daemon-free live signal: recent transcript writes mean the spoke is
    still producing turns, so the view can auto-refresh to follow it. A ``0.0`` mtime (no
    transcript on disk — a push-only or fixture spoke) is never running.
    """
    return transcript_mtime > 0.0 and (now - transcript_mtime) <= window


def _forest_cache_key(source_key: str, live_mtime: float) -> str:
    """The per-spoke forest cache key: the data source plus the transcript mtime.

    Folding the transcript mtime in means a grown transcript (mtime advanced) rebuilds the
    cached tree on the next refresh, while a static transcript — an expand/collapse toggle
    with no new turns — reuses it instantly, so a drill never triggers a rebuild (#67).
    """
    return f"{source_key}@{live_mtime}"


def _spoke_live_mtime(store: queries.SpanStore, spoke_id: str, projects_dir: Path) -> float:
    """The spoke's newest transcript mtime, or ``0.0`` when unavailable.

    Guards the live signal: a raw push-only store has no transcript method and a missing
    session-logs dir has nothing to stat, so both read as 'not running'.
    """
    read = getattr(store, "spoke_transcript_mtime", None)
    if read is None or not projects_dir.exists():
        return 0.0
    return read(spoke_id, projects_dir)


def _render_steps_body(forest: list[dict] | None) -> None:
    """Render the Steps tab from an already-built forest: header row + the L1 trace spine.

    ``None`` means the causal build failed and the error is already surfaced; ``[]`` is a
    legitimately empty spoke (a note). The caller owns the build so a failure renders one
    error, not one per tab.
    """
    if forest is None:
        return  # build failed; the error is already surfaced
    if not forest:
        st.info("No spans for this spoke run.")
        return
    head = st.columns(_STEP_COLS)
    for col, name in zip(head, _STEP_HEADERS, strict=True):
        col.markdown(f"**{name}**")
    _render_spine(forest)


def render_spoke_view(store: queries.SpanStore, source_key: str = "") -> None:
    st.header("Spoke view")
    st.caption(
        "Causal spoke trace: main steps with rolled-up metrics; expand a step to drill "
        "into its turns, tools, hooks, and sub-agents."
    )

    spoke_ids = store.spoke_run_ids(queries.REAL_REPO_PREFIX)  # #55: hide fixture-leak spokes
    if not spoke_ids:
        st.info("No spoke runs found in the span log.")
        return

    spoke_id = st.selectbox("Spoke run", spoke_ids, format_func=queries.format_spoke_label)
    projects_dir = resolve_projects_dir()
    live_mtime = _spoke_live_mtime(store, spoke_id, projects_dir)

    follow = False
    if _spoke_is_running(live_mtime, time.time()):
        follow = st.toggle(
            f"🔴 Follow live (auto-refresh ~{_LIVE_REFRESH})",
            value=False,
            key=f"live_follow::{spoke_id}",
        )

    # One build feeds both the Steps and Composition tabs (same mtime key → a single
    # build, and at most one error banner on failure). A followed spoke re-reads and
    # rebuilds inside the fragment so the live view tracks transcript growth.
    forest = _build_or_error(store, spoke_id, _forest_cache_key(source_key, live_mtime))
    steps_tab, meta_tab, comp_tab = st.tabs(["Steps", "Meta by kind", "Composition"])

    with steps_tab:
        # Only the selected running spoke auto-refreshes, and only its Steps tab — the
        # st.fragment re-runs just this body on the interval, leaving the rest static.
        if follow:

            def _steps() -> None:
                live = _spoke_live_mtime(store, spoke_id, projects_dir)
                followed = _build_or_error(store, spoke_id, _forest_cache_key(source_key, live))
                _render_steps_body(followed)

            st.fragment(run_every=_LIVE_REFRESH)(_steps)()
        else:
            _render_steps_body(forest)

    with meta_tab:
        _render_meta(store, spoke_id)

    with comp_tab:
        if forest is None:
            pass  # build failed; the error is already surfaced above
        elif not forest:
            st.info("No spans for this spoke run.")
        else:
            _render_composition(forest)
            st.subheader("Cold-context lens")
            _render_cold_context(forest)


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


def _decisions_label(decisions: dict[str, int] | None) -> str:
    """The allow/ask/deny breakdown for an approval candidate (Issue #60).

    An em dash for a non-approval interaction (a human prompt/question), which the
    query layer leaves ``decisions=None`` because a decision breakdown is meaningless
    for it.
    """
    if not decisions:
        return "—"
    return " · ".join(f"{slot} {decisions.get(slot, 0)}" for slot in ("allow", "ask", "deny"))


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
            "Decisions": _decisions_label(row.get("decisions")),
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


def _resolve_store(span_log: Path, log_mtime: float) -> tuple[queries.SpanStore | None, str]:
    """Pick the span source from the sidebar and build the store, with its cache key.

    Correlation is on by default; when it's requested but the session-logs dir is
    missing, the mode falls back to raw (with a note). Returns ``(store, source_key)``
    — ``source_key`` keys the per-spoke forest cache so toggling correlation never
    serves the other store's tree. In correlated mode it is ``correlated:<version>``
    (the persisted store's content version, so a new spoke's delta rebuilds the
    forest); in raw mode ``raw:<log_mtime>``. The store is ``None`` only when the raw
    log is absent.
    """
    correlated = st.sidebar.toggle("Correlate via Issue #22 (session logs + ccusage)", value=True)
    projects_dir = resolve_projects_dir()

    if resolve_mode(correlated, projects_dir) == "correlated":
        store_path = resolve_store_path()
        st.sidebar.caption(
            f"Push log:\n`{span_log}`\nSession logs:\n`{projects_dir}`\nStore:\n`{store_path}`"
        )
        store, version = load_correlated_store(str(span_log), str(projects_dir), str(store_path))
        return store, f"correlated:{version}"

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
        return None, "raw"
    return load_store(span_log), f"raw:{log_mtime}"


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
    span_log = resolve_span_log()
    # The log mtime keys the raw store + forest cache; the correlated source_key is the
    # persisted store's content version, so a new spoke's delta rebuilds the tree.
    log_mtime = span_log.stat().st_mtime if span_log.exists() else 0.0
    store, source_key = _resolve_store(span_log, log_mtime)
    if store is None:
        return

    view = st.sidebar.radio(
        "View", ["Spoke", "Morning", "Aggregate", "A/B compare", "Automatability"]
    )
    if view == "Spoke":
        render_spoke_view(store, source_key)
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
