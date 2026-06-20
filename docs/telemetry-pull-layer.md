# Telemetry pull layer (Issue #22)

The pull layer reconstructs spans from Claude session logs and attributes tokens
to every span. It is the counterpart to Issue #21's push layer (hooks and
scripts emitting spans at runtime) and builds against #21's frozen span schema
(`docs/telemetry-span-schema.md`) verbatim.

> [!NOTE]
> Issue #90 retired the Streamlit dashboard and its pull-only DuckDB store
> (`telemetry/store.py` + the DuckDB query layer `telemetry/queries.py`).
> Observability now lives on **Langfuse** (the push path under
> `dashboard/langfuse/otelcol.yaml` + `langfuse_*.py`). The transcript parsers
> documented here are retained as the always-on, on-machine **backfill source**
> for that pipeline — they are no longer wired to a renderer.

> [!NOTE]
> Issue #91 retired `ccusage` and the pull-cost layer (`telemetry/cost.py`). The
> otelcol remaps tokens to `gen_ai.usage.*`, so **Langfuse computes cost itself**
> from token usage × its model-pricing config. The pull layer now attributes only
> tokens; cost is no longer derived on-machine.

Everything here is **read-only and 100% local**. Session logs contain prompt
content, so they are parsed on-machine and only metadata / metrics are surfaced —
never raw prompt, answer, thinking, or tool-output text. **Exception (Issue #47):**
each node carries a few-word `summary` for display — the todo a step advances
(`TodoWrite`/`TaskCreate`/`TaskUpdate`), an agent's short task `description`, the
first line of a human prompt or question, and a tool's single main parameter (the
`Bash` command, the file path a `Read`/`Edit`/`Write` acted on, a `Grep` pattern).
This widens the surface to *short intent* metadata only; long-form content —
extended thinking, an agent's full task prompt, a tool's secondary input
(replacement text, file content) and its output, and human answers — stays
filtered.

## Modules

All live in `scripts/telemetry/`:

| Module | Responsibility |
|--------|----------------|
| `spans.py` | The `Span` dataclass — the frozen schema plus the additive, optional, pull-only `summary` field (Issue #47). |
| `session_parser.py` | Parse `~/.claude/projects/*/*.jsonl` into `skill` / `agent` / `todo` / `human` spans plus a `tool` leaf per `tool_use` (Issue #47). Walk `<session>/subagents/agent-<id>.jsonl` transcripts into `UsageEvent`s **and** the sub-agent's own step spans (#47 S3) — re-homed onto the parent session with `parent_id` = the agent span, so they nest under it. |
| `spoke_runs.py` | Group spans into spoke-run lifetimes; per-invocation normalized metrics. |
| `causal.py` / `causal_tree.py` | Build the strict, id-based causal forest over a parsed session (Issue #65). |

## How attribution works

- **`agent` spans** take their tokens from the walked subagent transcript (the
  subagent runs in its own transcript, so its usage — not the parent turn that
  spawned it — is the agent's usage).
- **All other spans** (pull `skill`/`todo`/`human` and push `step`/`hook`/…) take
  their tokens by bracketing the session's per-turn `message.usage` over the
  span's `[ts_start, ts_end)` window, joined on `session_id`. The upper bound is
  half-open so an event on a shared boundary is counted once.
- **Cost** is not derived here. The otelcol remaps the four token types to
  `gen_ai.usage.*` and Langfuse computes cost from its model-pricing config
  (Issue #91).

Spans are **hierarchical** (a `step` span encloses the skill/agent spans that ran
during it), so a wide span's tokens include the narrower spans nested inside it.
Consumers aggregate within one granularity; callers must not sum a step's tokens
together with the nested spans it already contains.

## Spoke-run join

Pull spans parsed from session logs carry a null `spoke_run_id` (session logs do
not record it). They are backfilled from a session-peer push span — within one
session every span belongs to the same spoke run. Spans with no `spoke_run_id`
and no session match are ad-hoc and group under `None`.

## Verification notes and follow-ups

- **Timestamps** — every `user` and `assistant` record (the ones bracketed)
  carries a `timestamp`, so time-bracketing is sound.
- **Tool-permission approvals are NOT in the session JSONL.** Records carry a
  `permissionMode` state (`default` / `acceptEdits` / `auto`) but there is no
  per-tool approve/deny event. Capturing approvals is a PreToolUse-hook job in
  the push layer (Issue #21), not reconstructable here — a follow-up, not
  implemented in this issue.
- **`~/.claude/analytics/skill-usage.jsonl`** was absent on the reference
  machine; skill spans are reconstructed from `Skill` tool_use blocks alone. The
  analytics file remains an optional enrichment if present.
- **`cache_read`** — the issue text mentions a cache-read metric, but the frozen
  v1 span has no such field. Cache-read tokens are carried on the per-turn rows
  (the cache breakdown the causal tree renders); surfacing them as a span field is
  a schema v2 follow-up, not an in-place change to the frozen contract.
