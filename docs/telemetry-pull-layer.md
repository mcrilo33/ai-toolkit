# Telemetry pull layer (Issue #22)

The pull layer reconstructs spans from Claude session logs and attributes token
cost to every span, then exposes the unified push + pull dataset to the Issue #23
dashboard. It is the counterpart to Issue #21's push layer (hooks and scripts
emitting spans at runtime) and builds against #21's frozen span schema
(`docs/telemetry-span-schema.md`) verbatim.

Everything here is **read-only and 100% local**. Session logs contain prompt
content, so they are parsed on-machine and only metadata / metrics are surfaced —
never raw prompt, answer, thinking, or tool-output text. **Exception (Issue #47):**
each node carries a few-word `summary` for display — the todo a step advances
(`TodoWrite`/`TaskCreate`/`TaskUpdate`), an agent's short task `description`, and a
trimmed first-line snippet of a human prompt or question. This widens the surface
to *short intent* metadata only; long-form content — extended thinking, an agent's
full task prompt, agent output, and human answers — stays filtered.

## Modules

All live in `scripts/telemetry/`:

| Module | Responsibility |
|--------|----------------|
| `spans.py` | The `Span` dataclass — the frozen 18-field schema, verbatim. |
| `session_parser.py` | Parse `~/.claude/projects/*/*.jsonl` into `skill` / `agent` / `todo` / `human` spans; walk `<session>/subagents/agent-<id>.jsonl` subagent transcripts into `UsageEvent`s. |
| `cost.py` | Attribute tokens and cost to every span; reuse `ccusage` for cost. |
| `spoke_runs.py` | Group spans into spoke-run lifetimes; per-invocation normalized metrics. |
| `queries.py` | Expose the unified push + pull dataset as in-memory DuckDB views. |

## How attribution works

- **`agent` spans** take their tokens from the walked subagent transcript (the
  subagent runs in its own transcript, so its usage — not the parent turn that
  spawned it — is the agent's cost).
- **All other spans** (pull `skill`/`todo`/`human` and push `step`/`hook`/…) take
  their tokens by bracketing the session's per-turn `message.usage` over the
  span's `[ts_start, ts_end)` window, joined on `session_id`. The upper bound is
  half-open so an event on a shared boundary is counted once.
- **Cost** is never re-derived from a price table. `ccusage session --json` gives
  the authoritative per-session `totalCost`; that pool is distributed across the
  session's spans by each span's share of the session's tokens (input + output +
  cache). The `ccusage` join key is `period` == `session_id`.

Spans are **hierarchical** (a `step` span encloses the skill/agent spans that ran
during it), so a wide span's tokens include the narrower spans nested inside it.
The views aggregate within one granularity; callers must not sum a step's cost
together with the nested spans it already contains.

## Spoke-run join

Pull spans parsed from session logs carry a null `spoke_run_id` (session logs do
not record it). They are backfilled from a session-peer push span — within one
session every span belongs to the same spoke run. Spans with no `spoke_run_id`
and no session match are ad-hoc and group under `None`.

## DuckDB dataset

`queries.connect(events_path=…, projects_root=…, ccusage_costs=…)` returns an
in-memory DuckDB (no separate database to run) with:

- `spans` — the unified table (the 18 schema fields, with `human` flattened to
  `human_type` / `human_wait_ms` for SQL ergonomics).
- `spoke_run_summary` — per spoke run: span count, distinct sessions, total cost,
  lifetime. `total_cost_usd` is the sum of the run's distinct sessions' ccusage
  totals — not a sum over `spans.cost_usd`, which would double-count because a
  step span's cost already includes the spans nested inside it. This makes the
  run total cross-check against ccusage directly.
- `step_metrics` — per spoke run and step key (`kind:name[:phase]`): invocation
  count, mean / median duration, total cost, human-interaction count.

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
  v1 span has no such field. Cache-read tokens are tracked internally for cost
  weighting only; surfacing them as a span field is a schema v2 follow-up, not an
  in-place change to the frozen contract.
