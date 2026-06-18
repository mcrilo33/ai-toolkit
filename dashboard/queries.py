"""Query layer for the workflow-observability dashboard (Issue #23).

This is the thin layer the Streamlit UI (``app.py``) sits on. It loads the
unified span dataset — the frozen v1 schema from Issue #21
(``docs/telemetry-span-schema.md``) — into DuckDB and answers the dashboard's
three views plus the automatability panel.

Data source: until Issue #22 (the session-log parser + token/cost correlation)
lands its DuckDB views, this module ingests the append-only ``events.jsonl``
span log directly via :meth:`SpanStore.from_jsonl`. When #22 lands, point
:meth:`SpanStore.from_connection` at its views — every query here is plain SQL
over a ``spans``-shaped relation, so the wiring is a one-line swap.

Privacy: spans carry metadata only (timings, statuses, toolkit construct names).
This layer never reads or surfaces prompt content — there is none in the schema.
Token/cost numbers originate from #22's ccusage correlation and are read as-is;
nothing is re-derived here.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

# ``step_nodes.py`` is the sibling module holding the span->node / interval / turn
# attribution + rollup primitives the SQL/aggregate layer reuses. Make it importable
# regardless of how this module was loaded — ``streamlit run`` injects this directory
# onto sys.path, but the unit harness loads queries by file path and does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# The ``telemetry`` package lives in the sibling ``scripts/`` dir, NOT in ``dashboard/``.
# ``streamlit run dashboard/app.py`` only adds ``dashboard/`` to sys.path, so without
# this the ``telemetry.*`` imports below crash a real launch (Issue #75, Bug 1). Insert
# it here — in the module doing the import — so every entrypoint (streamlit, the
# importlib unit harness) resolves the package, not just one launcher.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
# The v3 causal trace (Issue #65): the per-spoke builder + the parser it re-parses with.
from telemetry.causal_tree import causal_forest_from_parsed
from telemetry.session_parser import ParsedSession, parse_session_file
from step_nodes import (
    _attribute_turns,
    _parse_ts,
    _roll_up,
    _roll_up_steps,
    _step_node,
)

# Push-span kinds the parser does NOT reconstruct from a transcript — the spine markers,
# hooks and control scripts the causal builder takes from the push side (everything else
# comes fresh from the per-spoke parse, so passing these alone avoids double-counting).
_PUSH_KINDS: tuple[str, ...] = ("step", "lifecycle", "hook", "script")

# Column order for the in-memory ``spans`` table. ``human`` is flattened into
# ``human_type`` / ``human_wait_ms`` so the table is purely scalar (no nested
# structs to wrangle in SQL). Timestamps stay as ISO-8601 UTC strings: they sort
# lexicographically, so window filtering and child ordering need no parsing.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("span_id", "VARCHAR"),
    ("parent_id", "VARCHAR"),
    ("spoke_run_id", "VARCHAR"),
    ("session_id", "VARCHAR"),
    ("workflow_rev", "VARCHAR"),
    ("repo", "VARCHAR"),
    ("branch", "VARCHAR"),
    ("kind", "VARCHAR"),
    ("name", "VARCHAR"),
    ("phase", "VARCHAR"),
    ("ts_start", "VARCHAR"),
    ("ts_end", "VARCHAR"),
    ("duration_ms", "BIGINT"),
    ("status", "VARCHAR"),
    ("human_type", "VARCHAR"),
    ("human_wait_ms", "BIGINT"),
    ("summary", "VARCHAR"),
    # v3 pull-only link fields (Issues #50/#54): a ``script``'s ``emits`` names the
    # step/lifecycle marker it produced (the script→marker chain, #54); a
    # ``hook``/``script``'s ``sidecar_session`` and an ``agent``'s ``agent_link`` feed
    # the tree's sidecar/actor attribution (#50). Null on push spans, filled by the
    # parser; ingested so the forest can surface all three links.
    ("emits", "VARCHAR"),
    ("sidecar_session", "VARCHAR"),
    ("agent_link", "VARCHAR"),
    ("tokens_in", "BIGINT"),
    ("tokens_out", "BIGINT"),
    ("cost_usd", "DOUBLE"),
)
_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in _COLUMNS)

# Per-turn relation (mirrors Issue #22's ``turns`` table). One row per assistant
# usage event, carrying model and a per-turn cost counted exactly once — the
# source the v2 spoke view uses for model attribution and once-per-turn cost.
_TURN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("session_id", "VARCHAR"),
    ("ts", "VARCHAR"),
    ("model", "VARCHAR"),
    ("source", "VARCHAR"),
    ("agent_id", "VARCHAR"),
    ("tokens_in", "BIGINT"),
    ("tokens_out", "BIGINT"),
    ("tokens_total", "BIGINT"),
    # Issue #59 budget panel: the per-turn cache breakdown — cheap reuse
    # (``cache_read``) vs cold writes (``cache_creation``, the resume re-read).
    ("cache_read", "BIGINT"),
    ("cache_creation", "BIGINT"),
    ("cost_usd", "DOUBLE"),
    # Issue #59: the turn's privacy-safe reasoning gist, surfaced as a ``reasoning``
    # node and as a phase step's content-derived label when no todo summary resolves.
    ("reasoning", "VARCHAR"),
)
_TURN_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in _TURN_COLUMNS)


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Parse an append-only span log: one JSON object per non-blank line."""
    spans: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            spans.append(json.loads(line))
    return spans


def _row_tuple(span: dict[str, Any]) -> tuple[Any, ...]:
    """Flatten one span dict into a row matching ``_COLUMNS`` order."""
    human = span.get("human") or {}
    values: dict[str, Any] = {
        **span,
        "human_type": human.get("type"),
        "human_wait_ms": human.get("wait_ms"),
    }
    return tuple(values.get(name) for name in _COLUMN_NAMES)


# Approval derivation (Issue #60). A tool-permission decision is never its own
# span: it lives only in the push-layer PreToolUse ``hook`` span as a ``status`` of
# ``allow`` / ``deny`` / ``warn``. ``_derive_approvals`` turns each such gating hook
# into a first-class ``approval`` node and links it to the tool it gated.
_GATING_STATUSES: tuple[str, ...] = ("allow", "deny", "warn")
_APPROVAL_NAME = "tool-permission"
# A gate fires at PreToolUse, immediately before its tool; the gated tool is the
# nearest tool in the same spoke run that *starts at or after* the gate. The window
# guards against a gate with no real tool matching an unrelated far-future one.
_GATE_WINDOW_S = 120.0
_DECISION_WORD: dict[str, str] = {"allow": "allowed", "deny": "denied", "warn": "flagged"}
# Fold a gate's raw status into the canonical allow / ask / deny breakdown surfaced
# in the Automatability view (Issue #60): a ``warn`` is an advisory the human had to
# act on, so it counts as an ``ask``.
_DECISION_BUCKET: dict[str, str] = {"allow": "allow", "warn": "ask", "deny": "deny"}


def _fetch_dicts(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]]:
    """Run SQL and return rows as column-keyed dicts (module-level twin of ``_query``)."""
    cursor = con.execute(sql, params or [])
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _gated_tool(
    hook: dict[str, Any], tools_by_run: dict[Any, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """The tool a gate hook gated: nearest tool starting at/after it, same run.

    A gate with a ``session_id`` prefers a same-session tool; the nearest match by
    start time wins, tie-broken by ``span_id``. ``None`` when no tool follows the
    gate within :data:`_GATE_WINDOW_S` (a deny that blocked nothing parsable, or a
    gate at the very tail of a run).
    """
    hook_ts = _parse_ts(hook["ts_start"])
    if hook_ts is None:
        return None
    options: list[tuple[float, dict[str, Any]]] = []
    for tool in tools_by_run.get(hook["spoke_run_id"], []):
        tool_ts = _parse_ts(tool["ts_start"])
        if tool_ts is None or tool_ts < hook_ts or tool_ts - hook_ts > _GATE_WINDOW_S:
            continue
        options.append((tool_ts, tool))
    if hook["session_id"]:
        same = [o for o in options if o[1]["session_id"] == hook["session_id"]]
        if same:
            options = same
    if not options:
        return None
    options.sort(key=lambda o: (o[0], o[1]["span_id"]))
    return options[0][1]


def _approval_dict(hook: dict[str, Any], gated: dict[str, Any] | None, parent_id: Any) -> dict:
    """Build the ``approval`` span dict derived from one gating ``hook``."""
    decision = hook["status"]
    target = gated["name"] if gated else "tool"
    summary = f"{_DECISION_WORD.get(decision, decision)}: {target}"
    if gated and gated.get("summary"):
        summary = f"{summary} {gated['summary']}"
    return {
        "span_id": f"approval:{hook['span_id']}",
        "parent_id": parent_id,
        "spoke_run_id": hook["spoke_run_id"],
        "session_id": hook["session_id"],
        "workflow_rev": hook["workflow_rev"],
        "repo": hook["repo"],
        "branch": hook["branch"],
        "kind": "approval",
        "name": _APPROVAL_NAME,
        "phase": hook["phase"],
        "ts_start": hook["ts_start"],
        "ts_end": hook["ts_end"],
        "duration_ms": hook["duration_ms"] or 0,
        "status": decision,
        "human": {"type": "approval", "wait_ms": hook["duration_ms"] or 0},
        "summary": summary,
    }


def _blocked_tool_dict(hook: dict[str, Any], approval_id: str) -> dict:
    """A synthetic never-run tool under a deny approval (no parsable tool to reparent)."""
    return {
        "span_id": f"blocked:{hook['span_id']}",
        "parent_id": approval_id,
        "spoke_run_id": hook["spoke_run_id"],
        "session_id": hook["session_id"],
        "workflow_rev": hook["workflow_rev"],
        "repo": hook["repo"],
        "branch": hook["branch"],
        "kind": "tool",
        "name": "(blocked)",
        "phase": hook["phase"],
        "ts_start": hook["ts_start"],
        "ts_end": hook["ts_end"],
        "duration_ms": 0,
        "status": "deny",
        "summary": f"blocked by {hook['name']}",
    }


def _derive_approvals(con: duckdb.DuckDBPyConnection) -> None:
    """Materialize ``approval`` spans from gating hooks, linked to the tool they gated.

    A PreToolUse hook whose ``status`` is ``allow`` / ``deny`` / ``warn`` is a
    tool-permission gate. Each becomes one ``approval`` span carrying
    ``human={type:'approval', wait_ms}`` (the wait derives from the gate's own
    duration) so it routes into the Automatability view and rolls up at ``$0`` cost:

    - **allow / warn** — the gate let the tool through, so the approval nests *under*
      that tool (it gains an approval child; the tool keeps its place in the tree).
    - **deny** — the gate blocked the tool, which reparents *under* the approval and
      is marked never-run (``status='deny'`` + a ``never ran`` summary); when no
      parsable tool follows, a synthetic never-run tool stands in.

    Idempotent and curated-data-safe: the guard is all-or-nothing by design — a
    dataset that already carries *any* ``approval`` span (the golden fixture, a
    re-wrapped connection) is left wholly untouched. The live #22 correlation never
    emits approvals, so on real data this always derives. A deny approval's own
    ``parent_id`` is the gate hook's parent (an ancestor turn/step — a PreToolUse
    hook always fires before its tool), never the gated tool, so reparenting the
    blocked tool under the approval cannot form a cycle.
    """
    if _fetch_dicts(con, "SELECT 1 FROM spans WHERE kind = 'approval' LIMIT 1"):
        return
    hooks = _fetch_dicts(
        con,
        f"SELECT * FROM spans WHERE kind = 'hook' AND status IN ({_sql_in_list(_GATING_STATUSES)}) "
        "AND spoke_run_id IS NOT NULL ORDER BY ts_start, span_id",
    )
    if not hooks:
        return
    tools_by_run: dict[Any, list[dict[str, Any]]] = {}
    for tool in _fetch_dicts(con, "SELECT * FROM spans WHERE kind = 'tool'"):
        tools_by_run.setdefault(tool["spoke_run_id"], []).append(tool)

    new_spans: list[dict[str, Any]] = []
    tool_updates: list[tuple[Any, ...]] = []
    claimed: set[Any] = set()
    for hook in hooks:
        gated = _gated_tool(hook, tools_by_run)
        approval_id = f"approval:{hook['span_id']}"
        if hook["status"] == "deny":
            approval = _approval_dict(hook, gated, hook["parent_id"])
            # Only a tool that did NOT succeed can be the one this gate blocked: a hook
            # deny makes the tool_use return an error (parsed as status 'failure'), so a
            # 'success' tool belongs to a *later* gate and must never be relabeled
            # never-run. When the real blocked tool isn't matchable (it succeeded, was
            # already claimed by another deny, or no tool follows), a synthetic stands in.
            if gated and gated["status"] != "success" and gated["span_id"] not in claimed:
                claimed.add(gated["span_id"])
                # Reparent + mark deny only; the original command stays the summary.
                # The never-run marker is the render-layer badge (status='deny'), not
                # mangled text, so it shows regardless of what the summary says.
                tool_updates.append((approval_id, "deny", gated["span_id"]))
            else:
                new_spans.append(_blocked_tool_dict(hook, approval_id))
        else:
            parent_id = gated["span_id"] if gated else hook["parent_id"]
            approval = _approval_dict(hook, gated, parent_id)
        new_spans.append(approval)

    if new_spans:
        placeholders = ", ".join("?" for _ in _COLUMN_NAMES)
        con.executemany(
            f"INSERT INTO spans VALUES ({placeholders})",
            [_row_tuple(span) for span in new_spans],
        )
    for parent_id, status, span_id in tool_updates:
        con.execute(
            "UPDATE spans SET parent_id = ?, status = ? WHERE span_id = ?",
            [parent_id, status, span_id],
        )


_SPOKE_ISSUE_RE = re.compile(r"^(\d+)-")

# A real spoke run's ``repo`` is the toolkit checkout basename (``ai-toolkit`` or a
# worktree like ``ai-toolkit-55``). Test-fixture leaks carry sandbox hub basenames
# (``proj``, ``hub-8``, ``test_gauntlet_*`` …); #55 filters those off the read path
# so a stray pre-#49 leak never renders as a fake spoke in the spoke-listing views.
REAL_REPO_PREFIX = "ai-toolkit"


def _is_real_spoke_repo(repo: str | None, prefix: str = REAL_REPO_PREFIX) -> bool:
    """True when a span's ``repo`` names the real toolkit checkout, not a sandbox."""
    return bool(repo) and repo.startswith(prefix)


# Per-view handling of the v3 span kinds (Issue #61,
# ``docs/dashboard-spoke-trace-scope.md`` §Per-view behaviour of new kinds).
#
# ``ZERO_COST_ROLLUP_KINDS`` own no cost in any rollup: a ``script`` bills no LLM,
# and a ``workflow``'s cost lives on its ``agent`` children — summing it on the
# workflow row too would double-count, breaking the conservation invariant
# (Σ owned == Σ turns; cost lives only on turn/agent leaves). Their time and
# frequency still roll up; only cost and tokens are forced to zero.
ZERO_COST_ROLLUP_KINDS: tuple[str, ...] = ("script", "workflow")

# ``ROLLUP_EXCLUDED_KINDS`` never appear as a rollup row at all: a
# ``workflow_phase`` is a display-only grouping span (it brackets a fan-out's
# phase in the Spoke tree) with no own metrics.
ROLLUP_EXCLUDED_KINDS: tuple[str, ...] = ("workflow_phase",)


def _parse_spoke_sessions(projects_dir: Path, session_ids: list[str]) -> ParsedSession:
    """Parse ONLY the given sessions' transcripts under ``projects_dir`` and merge them.

    The per-spoke lazy parse (Issue #65): each session id is resolved to its
    ``<slug>/<session>.jsonl`` transcript by glob — sub-agent transcripts live deeper and
    are reached through the parent walk, so the ``*/<id>.jsonl`` glob never picks them up
    as top-level sessions. A session with no transcript on disk is silently skipped.
    """
    merged = ParsedSession()
    for session_id in session_ids:
        for path in sorted(Path(projects_dir).glob(f"*/{session_id}.jsonl")):
            parsed = parse_session_file(path)
            merged.spans.extend(parsed.spans)
            merged.usage_events.extend(parsed.usage_events)
            merged.agent_links.update(parsed.agent_links)
            merged.reasoning_refs.extend(parsed.reasoning_refs)
            merged.tool_parents.update(parsed.tool_parents)
    return merged


def _latest_transcript_mtime(projects_dir: Path, session_ids: list[str]) -> float:
    """The newest mtime across the given sessions' transcripts under ``projects_dir``.

    Walks each session's ``<slug>/<session>.jsonl`` plus its ``<session>/subagents``
    subtree (sub-agent turns grow there during a Workflow), so a write anywhere in the
    spoke's live transcript advances the signal. Returns ``0.0`` when nothing is on disk.
    """
    latest = 0.0
    for session_id in session_ids:
        for path in sorted(Path(projects_dir).glob(f"*/{session_id}.jsonl")):
            latest = max(latest, path.stat().st_mtime)
            subagents = path.parent / path.stem / "subagents"
            if subagents.is_dir():
                for agent_file in subagents.rglob("*.jsonl"):
                    latest = max(latest, agent_file.stat().st_mtime)
    return latest


def _sql_in_list(kinds: tuple[str, ...]) -> str:
    """Render a fixed tuple of kind literals as a SQL ``IN`` list.

    Inputs are module constants, never user data, so inlining the quoted
    literals is safe and keeps the SQL readable; the assert pins that contract
    so a future caller can't smuggle arbitrary text into the SQL.
    """
    assert all(kind.isidentifier() for kind in kinds), "kind literals must be identifiers"
    return ", ".join(f"'{kind}'" for kind in kinds)


def _issue_from_spoke_run_id(spoke_run_id: str | None) -> str | None:
    """Parse the issue number out of a ``<type>/<issue>-<slug>+<epoch>`` run id.

    Returns the leading-digits issue of the branch slug, or ``None`` for an
    ad-hoc/express run whose slug has no leading number (e.g. ``feature/a+1000``).
    """
    if not spoke_run_id:
        return None
    branch = spoke_run_id.rsplit("+", 1)[0]
    slug = branch.rsplit("/", 1)[-1]
    match = _SPOKE_ISSUE_RE.match(slug)
    return match.group(1) if match else None


class SpanStore:
    """A DuckDB-backed view over the span dataset.

    Construct with :meth:`from_jsonl` (ingest the raw log) or
    :meth:`from_connection` (reuse Issue #22's prepared views). Query methods
    return plain Python structures so the Streamlit layer — and the tests — stay
    free of DuckDB types.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        # Derive approval nodes from the gating hooks once, at construction, so every
        # path (raw JSONL fixtures, the live #22 correlation) surfaces them uniformly
        # (Issue #60). No-op when the dataset already carries approvals.
        _derive_approvals(con)

    @classmethod
    def from_events(
        cls, events: list[dict[str, Any]], turns: list[dict[str, Any]] | None = None
    ) -> SpanStore:
        """Build an in-memory store from already-parsed span dicts.

        ``turns`` (optional per-turn rows) seeds a ``turns`` table so the v2
        spoke view's once-per-turn cost/model attribution has data; the raw
        push-span log has none, so it defaults to an empty table.
        """
        con = duckdb.connect(":memory:")
        ddl = ", ".join(f"{name} {sqltype}" for name, sqltype in _COLUMNS)
        con.execute(f"CREATE TABLE spans ({ddl})")
        if events:
            placeholders = ", ".join("?" for _ in _COLUMN_NAMES)
            con.executemany(
                f"INSERT INTO spans VALUES ({placeholders})",
                [_row_tuple(span) for span in events],
            )
        _create_turns_table(con, turns or [])
        return cls(con)

    @classmethod
    def from_jsonl(cls, path: str | Path, turns: list[dict[str, Any]] | None = None) -> SpanStore:
        """Build an in-memory store from a raw ``events.jsonl`` span log."""
        return cls.from_events(load_jsonl(path), turns=turns)

    @classmethod
    def from_connection(cls, con: duckdb.DuckDBPyConnection) -> SpanStore:
        """Wrap an existing connection that already exposes a ``spans`` relation.

        This is the seam for Issue #22: once its parser publishes correlated
        ``spans`` views, hand that connection here instead of re-ingesting JSONL.
        """
        return cls(con)

    @classmethod
    def from_persisted_store(cls, store_path: str | Path) -> SpanStore:
        """Read the persisted incremental store (Issue #62), scoped to its content.

        Attaches the ``store.duckdb`` materialized by ``telemetry.store.ingest_store``
        read-only and copies its relations into an in-memory connection — cheap,
        because the parse already happened at ingest time, so no session log is read
        here. The copy is then mutable, so :meth:`__init__`'s approval derivation runs
        exactly as on every other path; every query method is unchanged. The store
        holds only post-watermark spokes, by design (no historical backfill).
        """
        con = duckdb.connect(":memory:")
        con.execute(f"ATTACH '{store_path}' AS store (READ_ONLY)")
        # ``spoke_run_summary`` is a view in the store (its cost cross-checks ccusage);
        # copying ``SELECT *`` materializes its current rows into the working copy.
        for table in ("spans", "turns", "session_costs", "spoke_run_summary"):
            con.execute(f"CREATE TABLE {table} AS SELECT * FROM store.{table}")
        con.execute("DETACH store")
        return cls(con)

    @classmethod
    def from_telemetry(
        cls,
        *,
        events_path: str | Path,
        projects_root: str | Path,
        ccusage_costs: dict[str, float] | None = None,
        scripts_dir: str | Path | None = None,
    ) -> SpanStore:
        """Build a store from Issue #22's correlated unified-span dataset.

        Issue #22's ``telemetry.queries.connect`` parses session logs, joins the
        push ``events.jsonl`` spans, and ccusage-attributes tokens/cost — then
        exposes a ``spans`` table whose columns match this module's exactly, so
        every query here runs against it unchanged. This is the live path; the
        JSONL constructors are for fixtures and the raw log.

        The import is lazy and ``scripts_dir`` (the ai-toolkit ``scripts/``
        directory) is added to ``sys.path`` only here, so the fixture path and
        the unit suite never depend on the ``telemetry`` package being importable.
        ``ccusage_costs`` is reused verbatim — costs are never re-derived here.
        """
        if scripts_dir is not None:
            sys.path.insert(0, str(scripts_dir))
        from telemetry.queries import connect

        con = connect(
            events_path=Path(events_path),
            projects_root=Path(projects_root),
            ccusage_costs=ccusage_costs or {},
        )
        return cls.from_connection(con)

    def close(self) -> None:
        self.con.close()

    def _query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run SQL and return rows as column-keyed dicts."""
        cursor = self.con.execute(sql, params or [])
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def spoke_run_ids(self, real_repo_prefix: str | None = None) -> list[str]:
        """All known ``spoke_run_id``s, newest-first by latest activity.

        Ordered by ``max(ts_end)`` descending, tie-broken by ``min(ts_start)``
        descending — not by the ``<branch>+<epoch>`` id string, which sorts
        alphabetically by branch. Ordering happens in Python via :func:`_parse_ts`
        because push spans carry second precision and pull spans millisecond, so a
        lexical sort on the raw timestamps would misorder them.

        ``real_repo_prefix`` is the #55 defense-in-depth filter: when given, runs
        whose ``repo`` is not a real toolkit checkout (a fixture-leak sandbox name)
        are dropped. It defaults to ``None`` — the bare primitive stays unfiltered
        so other callers and minimal-span fixtures keep listing every run.
        """
        rows = self._query(
            "SELECT spoke_run_id, MAX(ts_end) AS last_end, MIN(ts_start) AS first_start, "
            "array_agg(DISTINCT repo) AS repos "
            "FROM spans WHERE spoke_run_id IS NOT NULL GROUP BY spoke_run_id"
        )
        if real_repo_prefix is not None:
            # Keep a run when ANY of its spans names a real repo: a real run's push
            # span carries the toolkit basename even when its session spans fall back
            # to ``repo='unknown'`` (no cwd), whereas a fixture-leak run has none.
            rows = [
                r
                for r in rows
                if any(_is_real_spoke_repo(repo, real_repo_prefix) for repo in r["repos"])
            ]
        rows.sort(
            key=lambda r: (
                _parse_ts(r["last_end"]) or 0.0,
                _parse_ts(r["first_start"]) or 0.0,
                r["spoke_run_id"],
            ),
            reverse=True,
        )
        return [row["spoke_run_id"] for row in rows]

    def morning_rows(
        self,
        triage: dict[str, str] | None = None,
        *,
        real_repo_prefix: str | None = REAL_REPO_PREFIX,
    ) -> list[dict[str, Any]]:
        """The night-mode 'morning' lens: one row per spoke run, newest-first.

        A filtered view over the SAME #35 data (AC#6 "morning view shared with
        #35"): each spoke run with its issue (parsed from the ``spoke_run_id``),
        its authoritative per-run cost (reused from the ``spoke_run_summary`` view
        when this store is the correlated/telemetry one — ``None`` over a raw-JSONL
        fixture that has no such view), and a land-readiness annotation from the
        night's land-triage cache. ``triage`` maps issue -> ``clean``/``conflict``
        (the verdict ``hub-morning.sh --triage`` wrote); it complements the shell
        worklist rather than re-deriving the tiers here.

        ``real_repo_prefix`` (the #55 fixture-spoke filter) defaults to
        :data:`REAL_REPO_PREFIX` so the live morning view only ever shows runs from
        the real toolkit checkout; pass another prefix (or ``None``) to widen it.
        """
        triage = triage or {}
        cost_by_run: dict[str, Any] = {}
        if self._has_table("spoke_run_summary"):
            for row in self._query("SELECT spoke_run_id, total_cost_usd FROM spoke_run_summary"):
                cost_by_run[row["spoke_run_id"]] = row["total_cost_usd"]
        out: list[dict[str, Any]] = []
        for spoke_run_id in self.spoke_run_ids(real_repo_prefix):
            issue = _issue_from_spoke_run_id(spoke_run_id)
            out.append(
                {
                    "spoke_run_id": spoke_run_id,
                    "issue": issue,
                    "total_cost_usd": cost_by_run.get(spoke_run_id),
                    "merge": triage.get(issue) if issue else None,
                }
            )
        return out

    def workflow_revs(self) -> list[str]:
        """All known ``workflow_rev``s, sorted — the A/B view's pick list."""
        rows = self._query(
            "SELECT DISTINCT workflow_rev FROM spans "
            "WHERE workflow_rev IS NOT NULL ORDER BY workflow_rev"
        )
        return [row["workflow_rev"] for row in rows]

    def spoke_tree(self, spoke_run_id: str) -> list[dict[str, Any]]:
        """The step/sub-step tree for one spoke.

        Returns a forest of root nodes (spans with no parent inside this spoke),
        each child list ordered by ``ts_start``. Every node carries its own
        metrics plus a ``subtree`` rollup summing the node and all descendants
        (null cost/token values count as zero).
        """
        rows = self._query(
            "SELECT * FROM spans WHERE spoke_run_id = ? ORDER BY ts_start, span_id",
            [spoke_run_id],
        )
        nodes: dict[str, dict[str, Any]] = {}
        for row in rows:
            nodes[row["span_id"]] = {
                "span_id": row["span_id"],
                "parent_id": row["parent_id"],
                "kind": row["kind"],
                "name": row["name"],
                "phase": row["phase"],
                "status": row["status"],
                "ts_start": row["ts_start"],
                "duration_ms": row["duration_ms"],
                "cost_usd": row["cost_usd"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "human_type": row["human_type"],
                "human_wait_ms": row["human_wait_ms"],
                "human_count": 1 if row["human_type"] else 0,
                "children": [],
            }

        roots: list[dict[str, Any]] = []
        for node in nodes.values():
            parent = nodes.get(node["parent_id"])
            # A node with no in-spoke parent — or a malformed self-reference —
            # is a root, so a bad span can never vanish or recurse forever.
            if parent is None or parent is node:
                roots.append(node)
            else:
                parent["children"].append(node)

        for root in roots:
            _roll_up(root)
        return roots

    def spoke_causal_forest(
        self,
        spoke_run_id: str,
        projects_dir: Path,
        ccusage_costs: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        """The v3 **causal** trace for one spoke, parsed per-spoke (Issue #65).

        The sole spoke-tree builder since #80 removed the timestamp-correlated path:
        the trace is built from the real causal ids. Only this spoke's own session
        transcripts are parsed (located from its push spans), never the whole projects
        root — that per-spoke parse is what keeps cold open fast. The fresh pull spans +
        per-turn rows + ``tool_parents`` feed
        the builder; the spoke's push markers/hooks/scripts supply the spine; idle/resume
        dividers are appended.

        Returns the causal forest (empty when the spoke has no parseable transcript).
        """
        session_ids = self._spoke_session_ids(spoke_run_id)
        if not session_ids:
            return []
        parsed = _parse_spoke_sessions(projects_dir, session_ids)
        if not parsed.usage_events and not parsed.spans:
            return []
        push = self._query(
            f"SELECT * FROM spans WHERE spoke_run_id = ? AND kind IN ({_sql_in_list(_PUSH_KINDS)})",
            [spoke_run_id],
        )
        forest = causal_forest_from_parsed(parsed, push, ccusage_costs or {})
        # Attach the additive subtree ``rollup`` the renderer's composition/step metrics
        # read — without it the Composition tab is zero.
        for root in forest:
            _roll_up_steps(root)
        return forest

    def _spoke_session_ids(self, spoke_run_id: str) -> list[str]:
        """The sorted session ids attributed to a spoke by its push spans."""
        rows = self._query(
            "SELECT DISTINCT session_id FROM spans "
            "WHERE spoke_run_id = ? AND session_id IS NOT NULL",
            [spoke_run_id],
        )
        return sorted(row["session_id"] for row in rows)

    def spoke_transcript_mtime(self, spoke_run_id: str, projects_dir: Path) -> float:
        """The newest mtime across ONLY this spoke's own session transcripts (Issue #67).

        The live-follow "still running" signal: a spoke whose transcript was written
        recently is still producing turns, so the view can auto-refresh to follow it as
        the transcript grows — with no live-emission hook and no daemon. Resolves the
        spoke's sessions from its push spans (the same per-spoke locality as
        :meth:`spoke_causal_forest`) and reads only those files; ``0.0`` when the spoke
        has no session or no transcript on disk (a push-only or fixture spoke).
        """
        session_ids = self._spoke_session_ids(spoke_run_id)
        if not session_ids:
            return 0.0
        return _latest_transcript_mtime(projects_dir, session_ids)

    def spoke_meta_by_kind(self, spoke_run_id: str) -> list[dict[str, Any]]:
        """Aggregate one spoke's spans by ``kind`` to spot "launched too much".

        Per span kind: invocation ``count``, total/mean/median ``duration_ms``,
        total/mean ``cost_usd``, and the distinct ``models`` seen. Since main-agent
        cost lives on turn nodes (the drill-down) rather than spans, only ``agent``
        spans carry owned cost here — so summing across kinds equals the *resolved*
        subagent total; a subagent turn with no enclosing agent (or a malformed
        timestamp) is off-node and contributes to neither. The "launched too much"
        signal lives in the ``count``/``duration`` columns. Rows sort by total cost
        then count, descending. Unknown → ``[]``.
        """
        nodes = self._meta_nodes(spoke_run_id)
        return _aggregate_by_kind(nodes)

    def cold_context(self, spoke_run_id: str) -> list[dict[str, Any]]:
        """The cold-context lens: context loaded for one spoke, by subtype (Issue #52).

        A rollup of the loaded-context spans — the trimming/automation candidates the
        spec calls out: context paid for at startup whose payoff a reader should be
        able to weigh. Every subtype is a ``kind='rule'`` span keyed by ``phase``
        (``rule`` / ``memory`` / ``tool-schema``); rows carry that ``phase`` and its
        load ``count``, ordered by count descending then phase ascending.

        UPGRADE: surface only context never *exercised* once span→turn reference
        edges exist; today there is no usage signal, so every load is listed.
        """
        rows = self._query(
            "SELECT phase, COUNT(*) AS count FROM spans "
            "WHERE spoke_run_id = ? AND kind = 'rule' GROUP BY phase "
            "ORDER BY count DESC, phase",
            [spoke_run_id],
        )
        return rows

    def _meta_nodes(self, spoke_run_id: str) -> list[dict[str, Any]]:
        """One spoke's spans as flat nodes with once-per-turn cost attributed.

        Each ``agent`` node carries its subagent ``own_cost_usd`` / ``own_tokens_*`` /
        ``models`` for the meta-by-kind view; every other node owns nothing. Cost is
        attributed per turn so the per-kind aggregate counts each inference exactly once.
        """
        rows = self._query(
            "SELECT * FROM spans WHERE spoke_run_id = ? ORDER BY ts_start, span_id",
            [spoke_run_id],
        )
        nodes = [_step_node(row) for row in rows]
        session_ids = sorted({row["session_id"] for row in rows if row["session_id"]})
        turns = self._turns_for_sessions(session_ids)
        # Fill node own_cost (agent nodes get their subagent pool) for meta-by-kind.
        _attribute_turns(nodes, turns)
        return nodes

    def _turns_for_sessions(self, session_ids: list[str]) -> list[dict[str, Any]]:
        """Per-turn rows for the spoke's sessions (empty on the raw path).

        A connection handed to :meth:`from_connection` that predates the ``turns``
        relation has no such table; rather than crash, degrade to no owned cost.
        """
        if not session_ids or not self._has_table("turns"):
            return []
        placeholders = ", ".join("?" for _ in session_ids)
        return self._query(
            f"SELECT * FROM turns WHERE session_id IN ({placeholders})", list(session_ids)
        )

    def _has_table(self, name: str) -> bool:
        rows = self._query("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name])
        return bool(rows)

    def aggregate(
        self,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> list[dict[str, Any]]:
        """Roll spans up across all spokes in a time window.

        Groups by ``(kind, name, phase)`` and reports, per group: frequency
        (invocation count), totals, and per-invocation mean/median for time and
        cost, plus human-interaction count normalized per invocation. The window
        is a half-open ``[window_start, window_end)`` interval on ``ts_start``
        (ISO-8601 strings compare lexicographically); ``None`` drops that bound.
        Null cost/token values count as zero. Rows are sorted by total time
        spent, descending — the dashboard's "where does time go" ordering.
        """
        # ``script``/``workflow`` own no cost or tokens in the rollup (see
        # ZERO_COST_ROLLUP_KINDS); ``workflow_phase`` is dropped entirely.
        zero_cost = _sql_in_list(ZERO_COST_ROLLUP_KINDS)
        excluded = _sql_in_list(ROLLUP_EXCLUDED_KINDS)
        cost = f"CASE WHEN kind IN ({zero_cost}) THEN 0 ELSE COALESCE(cost_usd, 0) END"
        tokens = (
            f"CASE WHEN kind IN ({zero_cost}) THEN 0 "
            "ELSE COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0) END"
        )
        rows = self._query(
            f"""
            SELECT
                kind, name, phase,
                COUNT(*) AS invocations,
                SUM(duration_ms) AS total_duration_ms,
                AVG(duration_ms) AS mean_duration_ms,
                MEDIAN(duration_ms) AS median_duration_ms,
                SUM({cost}) AS total_cost_usd,
                AVG({cost}) AS mean_cost_usd,
                MEDIAN({cost}) AS median_cost_usd,
                SUM({tokens}) AS total_tokens,
                AVG({tokens}) AS mean_tokens,
                SUM(CASE WHEN human_type IS NOT NULL THEN 1 ELSE 0 END) AS human_count
            FROM spans
            WHERE (? IS NULL OR ts_start >= ?)
              AND (? IS NULL OR ts_start < ?)
              AND (kind IS NULL OR kind NOT IN ({excluded}))
            GROUP BY kind, name, phase
            ORDER BY total_duration_ms DESC, kind, name, phase
            """,
            [window_start, window_start, window_end, window_end],
        )
        for row in rows:
            row["frequency"] = row["invocations"]
            row["human_per_invocation"] = row["human_count"] / row["invocations"]
        return rows

    def ab_compare(
        self,
        rev_a: str,
        rev_b: str,
        *,
        low_confidence_n: int = 5,
    ) -> list[dict[str, Any]]:
        """Per-step delta between two ``workflow_rev``s, normalized per invocation.

        For each ``(kind, name, phase)`` present in either rev, reports the
        per-invocation mean time, cost, and human-interaction rate on each side
        and their B-minus-A deltas. Normalizing per invocation makes revs with
        different spoke counts comparable; a negative delta is an improvement
        (less time/cost/human). Each row carries sample sizes ``n_a``/``n_b``
        and a ``low_confidence`` flag set when ``min(n_a, n_b) < low_confidence_n``
        — small spoke counts are noisy and must not imply significance. Rows are
        sorted by the magnitude of the time delta, descending.
        """
        # Same new-kind handling as ``aggregate``: ``script``/``workflow`` carry no
        # own cost (so the delta stays honest), ``workflow_phase`` is excluded. The
        # time deltas for ``script``/``workflow`` still surface — a gate getting
        # slower is a real regression signal.
        zero_cost = _sql_in_list(ZERO_COST_ROLLUP_KINDS)
        excluded = _sql_in_list(ROLLUP_EXCLUDED_KINDS)
        cost = f"CASE WHEN kind IN ({zero_cost}) THEN 0 ELSE COALESCE(cost_usd, 0) END"
        rows = self._query(
            f"""
            SELECT
                kind, name, phase, workflow_rev,
                COUNT(*) AS n,
                AVG(duration_ms) AS mean_duration,
                AVG({cost}) AS mean_cost,
                SUM(CASE WHEN human_type IS NOT NULL THEN 1 ELSE 0 END) * 1.0
                    / COUNT(*) AS human_per_invocation
            FROM spans
            WHERE workflow_rev IN (?, ?)
              AND (kind IS NULL OR kind NOT IN ({excluded}))
            GROUP BY kind, name, phase, workflow_rev
            """,
            [rev_a, rev_b],
        )

        groups: dict[tuple[Any, Any, Any], dict[str, dict[str, Any]]] = {}
        for row in rows:
            key = (row["kind"], row["name"], row["phase"])
            groups.setdefault(key, {})[row["workflow_rev"]] = row

        result = [
            _ab_row(kind, name, phase, per_rev.get(rev_a), per_rev.get(rev_b), low_confidence_n)
            for (kind, name, phase), per_rev in groups.items()
        ]
        result.sort(
            key=lambda r: (-abs(r["delta_duration_ms"]), r["kind"], r["name"], r["phase"] or "")
        )
        return result

    def automatability_candidates(self, *, min_frequency: int = 1) -> list[dict[str, Any]]:
        """Rank human-interaction points by how worth automating they look.

        Groups spans that waited on a human (``human_type`` set) by
        ``(name, phase, human_type)`` and scores each group by
        ``frequency * consistency * on_critical_path``:

        - ``frequency`` — how often the interaction occurs.
        - ``consistency`` — modal-status fraction (the share of the most common
          outcome); high means low decision variance, so a rule could likely
          replace the human.
        - ``on_critical_path`` — fraction of the spans that are ``step`` or
          ``lifecycle`` (blocking the workflow's spine) rather than incidental.

        Reports ``mean_wait_ms`` too, plus a canonical ``decisions`` breakdown
        (allow / ask / deny — ``warn`` folds into ask, Issue #60) so an approval
        group shows how the gate resolved. This only SURFACES candidates; judging
        whether one is truly automatable is a later LLM-judge step. Groups below
        ``min_frequency`` are dropped; rows sort by score descending.
        """
        groups = self._query(
            """
            SELECT
                name, phase, human_type,
                COUNT(*) AS frequency,
                AVG(human_wait_ms) AS mean_wait_ms,
                AVG(CASE WHEN kind IN ('step', 'lifecycle') THEN 1.0 ELSE 0.0 END)
                    AS on_critical_path
            FROM spans
            WHERE human_type IS NOT NULL
            GROUP BY name, phase, human_type
            """
        )
        # Per-status counts per group, computed in Python: a SQL self-join on
        # (name, phase, human_type) would drop null-phase groups (NULL != NULL).
        # These feed both the modal-status consistency and the decision breakdown.
        status_rows = self._query(
            """
            SELECT name, phase, human_type, status, COUNT(*) AS cnt
            FROM spans
            WHERE human_type IS NOT NULL
            GROUP BY name, phase, human_type, status
            """
        )
        modal: dict[tuple[Any, Any, Any], int] = {}
        decisions: dict[tuple[Any, Any, Any], dict[str, int]] = {}
        for row in status_rows:
            key = (row["name"], row["phase"], row["human_type"])
            modal[key] = max(modal.get(key, 0), row["cnt"])
            bucket = decisions.setdefault(key, {"allow": 0, "ask": 0, "deny": 0})
            slot = _DECISION_BUCKET.get(row["status"])
            if slot is not None:
                bucket[slot] += row["cnt"]

        result: list[dict[str, Any]] = []
        for group in groups:
            if group["frequency"] < min_frequency:
                continue
            key = (group["name"], group["phase"], group["human_type"])
            consistency = modal[key] / group["frequency"]
            group["consistency"] = consistency
            group["score"] = group["frequency"] * consistency * group["on_critical_path"]
            # The allow/ask/deny breakdown is meaningful only for a gate decision; a
            # prompt/question interaction carries None so the view shows an em dash.
            group["decisions"] = decisions[key] if group["human_type"] == "approval" else None
            result.append(group)
        result.sort(key=lambda r: (-r["score"], r["name"], r["phase"] or "", r["human_type"]))
        return result


def _ab_row(
    kind: Any,
    name: Any,
    phase: Any,
    side_a: dict[str, Any] | None,
    side_b: dict[str, Any] | None,
    low_confidence_n: int,
) -> dict[str, Any]:
    """Build one A/B comparison row from each side's per-rev aggregate.

    A missing side (the step never ran under that rev) reads as zero metrics
    with ``n = 0``, which always trips ``low_confidence``.
    """
    n_a = side_a["n"] if side_a else 0
    n_b = side_b["n"] if side_b else 0
    dur_a = side_a["mean_duration"] if side_a else 0.0
    dur_b = side_b["mean_duration"] if side_b else 0.0
    cost_a = side_a["mean_cost"] if side_a else 0.0
    cost_b = side_b["mean_cost"] if side_b else 0.0
    human_a = side_a["human_per_invocation"] if side_a else 0.0
    human_b = side_b["human_per_invocation"] if side_b else 0.0
    return {
        "kind": kind,
        "name": name,
        "phase": phase,
        "n_a": n_a,
        "n_b": n_b,
        "mean_duration_a": dur_a,
        "mean_duration_b": dur_b,
        "delta_duration_ms": dur_b - dur_a,
        "mean_cost_a": cost_a,
        "mean_cost_b": cost_b,
        "delta_cost_usd": cost_b - cost_a,
        "human_per_invocation_a": human_a,
        "human_per_invocation_b": human_b,
        "delta_human_per_invocation": human_b - human_a,
        "low_confidence": min(n_a, n_b) < low_confidence_n,
    }


def _create_turns_table(con: duckdb.DuckDBPyConnection, turns: list[dict[str, Any]]) -> None:
    """Create the ``turns`` table and seed it (empty when no turns are given)."""
    ddl = ", ".join(f"{name} {sqltype}" for name, sqltype in _TURN_COLUMNS)
    con.execute(f"CREATE TABLE turns ({ddl})")
    if not turns:
        return
    placeholders = ", ".join("?" for _ in _TURN_COLUMN_NAMES)
    con.executemany(
        f"INSERT INTO turns VALUES ({placeholders})",
        [tuple(turn.get(name) for name in _TURN_COLUMN_NAMES) for turn in turns],
    )


def format_spoke_label(spoke_run_id: str) -> str:
    """Human dropdown label for a spoke run: ``<branch> · <YYYY-MM-DD>``.

    The raw id is ``<branch>+<spawn-epoch>``; the trailing epoch renders as a UTC
    spawn date while the id stays the lookup key. A malformed id — no ``+`` epoch,
    a non-numeric one, or an epoch outside the platform's timestamp range — falls
    back to the raw id unchanged, so this never raises in a selectbox format_func.
    """
    branch, sep, epoch = spoke_run_id.rpartition("+")
    if not sep or not epoch.isdigit():
        return spoke_run_id
    try:
        date = datetime.fromtimestamp(int(epoch), tz=UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return spoke_run_id
    return f"{branch} · {date}"


def format_step_label(node: dict[str, Any]) -> str:
    """Human label for a v2 spoke node.

    Prefers the node's few-word ``summary`` (Issue #47: the todo it advances, the
    agent's task, a prompt snippet); else ``name · phase``, or ``hooks xN``.
    """
    if node["kind"] == "hooks":
        return f"hooks x{node['collapsed_count']}"
    # An approval reads as the gate it was: "🔐 ask→allow/deny/warn" (Issue #60),
    # the decision carried on its own ``status``. The tool it gated is shown by the
    # tree nesting (the approval sits under an allowed tool; a blocked tool sits
    # under the approval), so the label stays the decision alone.
    if node["kind"] == "approval":
        return f"🔐 ask→{node.get('status') or 'ask'}"
    # A context node has two shapes. The v3 *causal* per-turn node carries
    # ``input_context`` (the named input state) and reads "context · N loaded" — the
    # real cached-prefix total rides the Tokens column, the names show on drill. The
    # v2 collapsed group (``collapsed_count`` + ``phase``, no ``input_context``) keeps
    # its "rule x3" / "memory x1" / "tool-schema x2" label.
    if node["kind"] == "context":
        ctx = node.get("input_context")
        if ctx is not None:
            loaded = (
                len(ctx["rules"])
                + (1 if ctx["claude_md"] else 0)
                + len(ctx["memory"])
                + ctx["schemas"]["count"]
            )
            return f"context · {loaded} loaded" if loaded else "context"
        return f"{node['phase']} x{node['collapsed_count']}"
    # A turn node (one inference) is labelled by its clock time + model, e.g.
    # "turn 12:01:05 · opus-4-8" — the per-turn token spike shows in the metrics.
    if node["kind"] == "turn":
        clock = _clock(node.get("ts_start"))
        model = node.get("model")
        return f"turn {clock} · {_short_model(model)}" if model else f"turn {clock}"
    summary = node.get("summary")
    # A tool leaf keeps its name visible alongside the parameter it acted on, so
    # the trace reads e.g. "Read · /path"; other kinds show the summary alone.
    if node["kind"] == "tool" and summary:
        return f"{node['name']} · {summary}"
    if summary:
        return summary
    if node.get("phase"):
        return f"{node['name']} · {node['phase']}"
    return node["name"]


def format_step_metrics(node: dict[str, Any]) -> dict[str, str]:
    """Display-ready metrics for a v2 spoke node.

    Time is the node's own wall-clock; cost/tokens/models/humans/status come from the
    rolled-up subtree totals that the forest builder attaches to every node (each falls
    back to the node's own only for a node built without a rollup). Status is the
    rolled-up terminal (last-event) outcome (Issue #57), matching the row icon. Zero
    values render as an em dash.
    """
    rollup = node.get("rollup") or {}
    cost = rollup.get("cost_usd", node.get("own_cost_usd", 0.0))
    # A causal context node owns no turn cost, so its rollup tokens are zero; the
    # Tokens column instead shows its real cached-prefix total (the named items + the
    # history remainder), the number the per-turn input state is worth (Issue #67).
    ctx = node.get("input_context")
    tokens = (
        ctx["total_tokens"] if ctx else rollup.get("tokens_in", 0) + rollup.get("tokens_out", 0)
    )
    models = rollup.get("models") or node.get("models") or []
    humans = rollup.get("human_count", node.get("human_count", 0))
    return {
        "time": _format_secs(node.get("duration_ms")),
        "cost": _format_cost(cost),
        "tokens": f"{tokens:,}" if tokens else "—",
        "model": ", ".join(_short_model(m) for m in models) if models else "—",
        "actor": node.get("actor", "main"),
        "humans": str(humans) if humans else "—",
        "status": rollup.get("status") or node.get("status", ""),
    }


def _format_secs(ms: int | float | None) -> str:
    return "—" if not ms else f"{ms / 1000:.1f}s"


def _format_cost(usd: float | None) -> str:
    return "—" if not usd else f"${usd:.4f}"


def _short_model(model: str) -> str:
    """Drop the ``claude-`` vendor prefix for compact display."""
    return model.removeprefix("claude-")


def _clock(ts: str | None) -> str:
    """``2026-06-12T12:01:05Z`` → ``12:01:05`` for a compact turn-node label."""
    if not ts or "T" not in ts:
        return ts or "—"
    return ts.split("T", 1)[1].rstrip("Z")[:8]


def _aggregate_by_kind(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group attributed span nodes by ``kind`` into meta-view rows.

    ``ROLLUP_EXCLUDED_KINDS`` (``workflow_phase``) are display-only grouping
    spans and never form a meta row. ``script``/``workflow`` nodes already carry
    ``own_cost_usd == 0`` here — only ``agent`` nodes receive attributed cost —
    so they surface at $0 without special handling.
    """
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        if node["kind"] in ROLLUP_EXCLUDED_KINDS:
            continue
        by_kind.setdefault(node["kind"], []).append(node)
    rows = [_kind_row(kind, group) for kind, group in by_kind.items()]
    rows.sort(key=lambda r: (-r["total_cost_usd"], -r["count"], r["kind"]))
    return rows


def _kind_row(kind: str, group: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [node["duration_ms"] for node in group]
    costs = [node["own_cost_usd"] for node in group]
    models = sorted({model for node in group for model in node["models"]})
    # Mean human wait across the group's timed interactions (Issue #60): the
    # approval row reports the gate's mean wait; a kind with no human wait reports
    # None (never a bogus 0) so the meta view shows an em dash for it.
    waits = [w for node in group if (w := node.get("human_wait_ms")) is not None]
    return {
        "kind": kind,
        "count": len(group),
        "total_duration_ms": sum(durations),
        "mean_duration_ms": statistics.mean(durations),
        "median_duration_ms": statistics.median(durations),
        "total_cost_usd": sum(costs),
        "mean_cost_usd": sum(costs) / len(group),
        "mean_wait_ms": statistics.mean(waits) if waits else None,
        "models": models,
    }
