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
- **Cache-write TTL split (Issue #97).** Anthropic bills a 1-hour cache write at
  2× input and a 5-minute write at 1.25×. The transcript's `message.usage`
  carries the per-TTL breakdown (`cache_creation.ephemeral_5m_input_tokens` /
  `ephemeral_1h_input_tokens`); `session_parser` reads it onto `UsageEvent`
  (`cache_creation_5m` / `cache_creation_1h`, summing to the flat
  `cache_creation`; the flat total falls into the 5m tier when the nested object
  is absent). The backfill maps the 5m tier to Langfuse's
  `cache_creation_input_tokens` (1.25×) and the 1h tier to its
  `input_cache_creation_1h` usage type (2×) so backfilled cost matches ccusage
  on 1h-cache workloads. The live **push** path stays single-rate: Claude Code's
  native OTel span carries only the flat `cache_creation_tokens` aggregate
  (no per-TTL attribute), so the otelcol cannot recover the ratio — that side is
  blocked on an upstream Claude Code change, not fixable in the collector.

Spans are **hierarchical** (a `step` span encloses the skill/agent spans that ran
during it), so a wide span's tokens include the narrower spans nested inside it.
Consumers aggregate within one granularity; callers must not sum a step's tokens
together with the nested spans it already contains.

## Spoke-run join

Pull spans parsed from session logs carry a null `spoke_run_id` (session logs do
not record it). They are backfilled from a session-peer push span — within one
session every span belongs to the same spoke run. Spans with no `spoke_run_id`
and no session match are ad-hoc and group under `None`.

## Duration rollup in the assembled spoke tree (Issue #128)

`langfuse_spoke_tree.py` stamps every container node of both assembled views with
`metadata.rollup.duration = {total_ms, components}` alongside the token rollup.
`total_ms` is the observed subtree wall-clock (the container's own `start → end`, or
its subtree span when untimed — which is how the synthetic root covers the whole
spoke; timestamps are parsed, never string-compared). `components` books each node's
exclusive time — its duration minus the *union* of its children's intervals, clamped
≥ 0 — into one class bucket. On serial spans the components sum to `total_ms`;
concurrent siblings (parallel tool calls, background sub-agents) each book their full
span time, so class buckets are span-time and may sum past the wall-clock (CPU-time
vs wall-time), while the gap buckets (`self`/`turn`/`step`) stay true — union-based
subtraction never erases them:

| Bucket | What lands in it |
|--------|------------------|
| `llm_request` | Generations (`claude_code.llm_request`). |
| `tool` | `tool:*` spans, minus their folded `blocked_on_user_ms`. |
| `hook` | `*.sh` spans / `workflow.kind == hook`. |
| `script` | `workflow.kind == script` spans (`script:worktree-land`, `script:spoke-push`, …). |
| `step` | Cycle-step nodes' own gap time (step overhead not covered by real spans). |
| `wait` | Human/gate wait: folded `blocked_on_user_ms` + the gate script (`script:gate`). |
| `turn` | Interactions' own gap (thinking/streaming outside child spans). |
| `self` | The container's own unattributed gap — inter-turn idle on the root. |
| `other` | Anything unclassified; untimed nodes contribute 0. |

Same leaf-marker rule as the #114 token stamping: View B's childless turn-markers
carry a `duration` computed from their pre-flatten View A subtree and are excluded
from the cycle-axis sums (their span overlaps their re-homed former children).
Rebuilds are idempotent — `fetch_session` excludes the synthesizer's own prior
output, so durations never double-count.

> [!WARNING]
> `duration` is written only by the spoke-tree assembly. The other two rollup
> writers still emit the token-only shape: `langfuse_backfill.py` (historical
> spokes lack `rollup.duration` — treat the key as optional in consumers), and the
> standalone `langfuse_rollup.py` patcher, whose `span-update` replaces the
> `rollup` metadata key wholesale — running it over a session that already holds
> assembled `spoketree-`/`spokecycle-` traces strips their `duration`. Re-run
> `langfuse_spoke_tree.py` to restore it; aligning the two writers is a follow-up.

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
