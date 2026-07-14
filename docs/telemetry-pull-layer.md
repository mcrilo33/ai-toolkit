# Telemetry pull layer (Issue #22)

The pull layer is the **land-time view builder**: `langfuse_spoke_tree.py`, run
once per land by `scripts/telemetry-ingest-spoke.sh`, parses the spoke's session
logs and raw request bodies on-machine and assembles the `spoketree-` nested
view and the `spokecycle-` todo view in Langfuse. It is the counterpart to
Issue #21's push layer (hooks and scripts emitting spans at runtime) and builds
against #21's frozen span schema (`docs/telemetry-span-schema.md`) verbatim.

> [!NOTE]
> Issue #90 retired the Streamlit dashboard and its pull-only DuckDB store
> (`telemetry/store.py` + the DuckDB query layer `telemetry/queries.py`).
> Observability now lives on **Langfuse** (the push path under
> `dashboard/langfuse/otelcol.yaml` + `langfuse_*.py`). Issue #140 then retired
> the transcript backfill (`langfuse_backfill.py` + `causal_tree.py`, #92) —
> live capture is complete by construction, so no after-the-fact healing path
> exists. The transcript parsers documented here survive solely as the view
> builder's input layer.

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

## Client-side telemetry config (Issue #228)

The **client-side** telemetry settings — whether capture is on and which Langfuse
it targets — live in the `telemetry:` section of `settings/ai-toolkit.yml`, the
declarative behavior config (see also model routing and `base_branch` there):

```yaml
telemetry:
  enabled: true                    # replaces the AI_TOOLKIT_OTEL toggle
  langfuse:
    host: http://localhost:3000
    project: proj-quicktest
    public_key: pk-lf-quicktest    # public — safe to commit
    otlp_endpoint: http://localhost:4318
```

`scripts/ai_toolkit_config.py` exposes these as accessors (`telemetry_enabled`,
`langfuse_host`/`project`/`public_key`/`otlp_endpoint`) and a `telemetry-env` CLI
seam. The bash consumers read them through `wt_resolve_telemetry_config`
(`scripts/worktree-lib.sh`), layered **env → config → hardcoded default**: a live
env var (`AI_TOOLKIT_OTEL`, `LANGFUSE_HOST`, …) still **wins**, so existing
env-driven setups keep working; the config supplies the default where the operator
set nothing; a telemetry-less config is a no-op that keeps each consumer's own
literal. Consumers wired this way: the `AI_TOOLKIT_OTEL` toggle + span endpoint in
`worktree-new.sh`, `LANGFUSE_HOST` in `telemetry-ingest-spoke.sh`, the toggle in
`hub-otel-watch.sh`, and the host/project/public-key side of
`wt_resolve_langfuse_auth`.

> [!WARNING]
> **The Langfuse SECRET never enters `ai-toolkit.yml`.** The config is committed
> and synced into downstream projects, so only the *public* settings above belong
> there. The secret key / `LANGFUSE_BASIC_AUTH` stays in `~/.afk-telemetry`
> (gitignored, per-machine), resolved by `wt_resolve_langfuse_auth`; `secrets-scan`
> and the security rule guard the boundary. A fresh **downstream** project should
> ship `enabled: false` (opt-in) and point `host`/`otlp_endpoint` at its own
> Langfuse rather than inherit `localhost`.

The collector's own pipeline config (`dashboard/langfuse/otelcol.yaml`) is separate
infrastructure and is **not** generated from this section — the config holds only the
client-facing "where/whether to send," never the collector's exporters or ports.

## Modules

All live in `scripts/telemetry/`:

| Module | Responsibility |
|--------|----------------|
| `spans.py` | The `Span` dataclass — the frozen schema plus the additive, optional, pull-only `summary` field (Issue #47). |
| `session_parser.py` | Parse `~/.claude/projects/*/*.jsonl` into `skill` / `agent` / `todo` / `human` spans plus a `tool` leaf per `tool_use` (Issue #47). Walk `<session>/subagents/agent-<id>.jsonl` transcripts into `UsageEvent`s **and** the sub-agent's own step spans (#47 S3) — re-homed onto the parent session with `parent_id` = the agent span, so they nest under it. |
| `spoke_runs.py` | Group spans into spoke-run lifetimes; per-invocation normalized metrics. |
| `langfuse_spoke_tree.py` | The land-time view builder — assemble the `spoketree-` nested view and `spokecycle-` todo view for one spoke (Issues #87/#100/#114/#128). Since #166 this is the **orchestrator**: it holds `build_batch`/`build_cycle_batch` and drives the post-build enrichments through an ordered `_ENRICHMENTS` registry; the families live in the `telemetry/spoke_tree/` package (see below). |
| `spoke_tree/` | The view-builder families, split out of the monolith (#166) so view-builder features touch disjoint files. Foundation (`ids`, `observations`); core span-copy plumbing (`indices`, `folding`, `assembly`); `rollups`; view lenses (`steps` = View A, `cycle` = View B); enrichments (`loaded_context`, `llm_decomp`, `context_deltas`, `metadata`, `commits`, `scores`). Each has a `tests/unit/test_spoke_tree_<family>.py`. |
| `langfuse_rollup.py` | Shared token-rollup sum logic + the standalone `rollup` patcher the view builder reuses. |

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
  is absent). In Langfuse the 5m tier is the `cache_creation_input_tokens`
  usage type (1.25×) and the 1h tier is `input_cache_creation_1h` (2×);
  `langfuse_rollup`'s totals count both when a span carries them. The live
  **push** path stays single-rate: Claude Code's
  native OTel span carries only the flat `cache_creation_tokens` aggregate
  (no per-TTL attribute), so the otelcol cannot recover the ratio — that side is
  blocked on an upstream Claude Code change, not fixable in the collector.

Spans are **hierarchical** (a `step` span encloses the skill/agent spans that ran
during it), so a wide span's tokens include the narrower spans nested inside it.
Consumers aggregate within one granularity; callers must not sum a step's tokens
together with the nested spans it already contains.

## Spoke-run join

Pull spans parsed from session logs carry a null `spoke_run_id` (session logs do
not record it). They inherit it from a session-peer push span — within one
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
> `duration` is written only by the spoke-tree assembly, and historical spokes
> (ingested before #128) lack `rollup.duration` — treat the key as optional in
> consumers. The standalone `langfuse_rollup.py` patcher still emits the
> token-only shape, and its `span-update` replaces the `rollup` metadata key
> wholesale — running it over a session that already holds assembled
> `spoketree-`/`spokecycle-` traces strips their `duration`. Re-run
> `langfuse_spoke_tree.py` to restore it; aligning the two writers is a follow-up.

## Spoke-latency dashboard (Issue #128)

One saved Langfuse dashboard — **"spoke latency"** — answers, at a glance: which
cycle step dominates a spoke, what the gate costs, and how LLM latency compares
across models. Its reproducible source of truth is
**`dashboard/langfuse/spoke-latency-dashboard.json`**: four widget definitions, each
carrying the exact `/api/public/metrics` query that backs it (the deployed v3.192.2
routes this query shape at the v1 path — `/api/public/v2/metrics` 404s there, newer
builds add it; all four queries verified live, HTTP 200 with data), pinned to the
metrics contract by `tests/unit/test_spoke_latency_dashboard.py` (views, measures,
aggregations, filter operators, the high-cardinality dimension ban, and the emitted
span/score names).

Langfuse v3.192.x has **no public dashboards API** — verified against the local
instance's OpenAPI spec and `langfuse-cli api __schema`, neither of which lists a
`dashboards` resource — so the dashboard is saved once via the UI (*Dashboards → New
dashboard → "spoke latency"*, one *New widget* form per entry in the JSON file). The
widget numbers are verifiable headlessly by running each `metricsQuery` (plus a
`fromTimestamp`/`toTimestamp` window) through the Metrics API. Auth resolves like the
rest of the push stack (see `wt_bridge_launch` in `scripts/worktree-lib.sh`): the
operator-exported `LANGFUSE_BASIC_AUTH` (`Basic <base64(pk:sk)>`), sent verbatim as
the `Authorization` header against `LANGFUSE_HOST` (default
`http://localhost:3000`).

```bash
curl -s -H "Authorization: $LANGFUSE_BASIC_AUTH" \
  "$LANGFUSE_HOST/api/public/metrics" --get \
  --data-urlencode 'query=<widget metricsQuery + time range>'
```

Span-name inventory the widgets key on (all emitted by this repo): cycle-step nodes
`step:<subject>` / `preStep` / `postStep` (assembled views) and `step:<phase>`
markers; script spans `worktree-new` / `worktree-land` / `worktree-done` /
`spoke-push` (the push span's window covers the pre-push test gate) and
`script:ready` / `script:gate` / `script:accept` / `script:blocked`; native
`claude_code.llm_request` generations; numeric scores `gate_park_ms` (trace-level
PLAN-gate park), `permission_wait_ms` (per blocked tool), and the per-phase
cycle-step scores `step_cache_write_usd:<PHASE>` / `step_tokens_written:<PHASE>` /
`step_total_cost_usd:<PHASE>` / `step_duration_ms:<PHASE>` (View B step nodes).
`step_total_cost_usd` is the true per-step cost — every generation's Langfuse
`costDetails`, main-loop and sub-agent, windowed onto its step so the per-phase
scores sum to the trace `totalCost` (pre-first-step spend lands in a `:pre`
residual), whereas `step_cache_write_usd` is the cache-write slice only.
`step_duration_ms` is each step's window length, so phase latency is a Scores-view
dimension and not only a rendered span.

> [!WARNING]
> **Dashboard-breaking rename (#230):** the per-phase score formerly named
> `step_cost_usd:<PHASE>` is now `step_cache_write_usd:<PHASE>`. Its value is
> unchanged — cache-WRITE cost only (`rollup.written x cache-write price`) — but the
> old name was misleading, since it omitted cache reads, output tokens, and all
> sub-agent spend. Any Scores widget filtering on `step_cost_usd:*` must be repointed
> to `step_cache_write_usd:*`.

### Widget 1 — cycle-step duration by step name

| Field | Value |
|-------|-------|
| View | Observations |
| Filters | `name` *starts with* `step:` |
| Metrics | `latency` — `p50` and `p95` |
| Breakdown dimension | `name` |
| Chart | Horizontal bar (or table) |

### Widget 2 — script spans p50/p95 (land, push, gate)

| Field | Value |
|-------|-------|
| View | Observations |
| Filters | `name` *any of* `worktree-land`, `worktree-new`, `worktree-done`, `spoke-push`, `script:ready`, `script:gate` |
| Metrics | `latency` — `p50` and `p95` |
| Breakdown dimension | `name` |
| Chart | Horizontal bar |

The `spoke-push` span's window covers the pre-push test gate; `script:gate` is the
PLAN-gate park emission.

### Widget 3 — LLM request latency by model

| Field | Value |
|-------|-------|
| View | Observations |
| Filters | `type` = `GENERATION` |
| Metrics | `latency` — `p50` and `p95` |
| Breakdown dimension | `providedModelName` |
| Chart | Horizontal bar (add a second time-series copy with `timeDimension` for trends) |

### Widget 4 — gate park + permission wait totals

| Field | Value |
|-------|-------|
| View | Scores (numeric) |
| Filters | `name` *any of* `gate_park_ms`, `permission_wait_ms` |
| Metrics | `value` — `sum` (add `count` for how often) |
| Breakdown dimension | `name`, over time (`timeDimension` day) |
| Chart | Stacked bar over time |

> [!NOTE]
> Per-**session** wait totals are not groupable here: `sessionId` is a
> high-cardinality dimension the Metrics API only accepts as a *filter*. To read
> one spoke's wait, add
> `{"column": "sessionId", "operator": "=", "value": "<spoke_run_id>", "type": "string"}`
> to the filters — or read `rollup.duration.components.wait` off the spoke's
> assembled root span, which carries the same answer per spoke.

## Outcome, normalization + cross-project stamping (Issue #231)

A blocked/reaped disaster spoke and a clean landed spoke used to carry identical trace
tags (`mode:`, `lane:`), so failure economics and cross-repo comparison were not queryable.
The land-time view build now stamps three families onto every assembled trace:

- **Outcome** — an `outcome:<landed|blocked|reaped|abandoned>` trace tag (+ bare
  `metadata.outcome`), plus the `gate_park_count` / `blocked_count` / `relaunch_count`
  numeric scores. `outcome:landed` is stamped by the land script; `outcome:blocked` (and the
  block/relaunch counts) by the `/afk` supervisor, which also rebuilds the view on a terminal
  block so a spoke that never lands still carries an outcome. `gate_park_count` is derived
  from the PLAN-gate spans; the other two are read from `.ai-toolkit` pointers (default 0).
- **Normalization** — `files_changed`, `lines_changed`, `commits`, `subtasks` numeric scores
  plus the derived `cost_per_changed_line` and `wall_per_subtask`, so a cheap one-line fix
  and an expensive refactor are comparable. Computed from the commit numstat + cycle windows
  the builder already has (a ratio is skipped when its denominator is 0).
- **Cross-project** — a `repo:<name>` trace tag (resolved by `telemetry-ingest-spoke.sh` from
  the origin remote, else the checkout dir basename) and the Langfuse `environment` field:
  the assembled views are stamped `production`, and `dashboard/langfuse/otelcol.yaml` stamps
  `langfuse.environment` on every live spoke span (defaulting to `production`, overridable via
  `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=<env>` for a test collector).

### Excluding test traffic from a dashboard

The store holds ~10k synthetic fixture sessions from earlier test runs. They predate the
`environment` field (or a test run stamps a non-`production` env), so a widget scoped to the
production environment drops them without a fragile name-based filter:

- **Dashboard scope** — set the dashboard's **Environment** selector to `production` (it
  scopes every widget at once). This is the one-switch recipe.
- **Per-widget filter** — where a single widget needs it, add
  `{"column": "environment", "operator": "=", "value": "production", "type": "string"}` to
  its filters. Legacy/fixture traces lacking the field (`default`) are excluded.
- **Compare repos** — break down by the `repo:` tag (Traces view, group by `tags`) to read
  cost/latency per repository once multiple projects export to the same store.

## Per-issue cycle-time metrics (Issue #280)

The #128 spoke-latency dashboard charts *within-spoke* step/script/gate latency, but nothing
recorded a per-issue **lifecycle timeline** or decomposed a spoke's wall-clock into **work vs
overhead** — so the #275–#278 throughput work had no on-record baseline to verify against. The
land-time view build now enriches every assembled `spoketree-`/`spokecycle-` trace with three
families sourced entirely from data already on disk / in the traces (a **backfill** — no
`gate-broker.sh` change, no live epochs). `telemetry-ingest-spoke.sh` gathers the off-trace
sources into `.ai-toolkit/lifecycle.json` and passes `--lifecycle`; every source is best-effort,
so an absent one skips its metric rather than emitting a wrong value.

### Lifecycle timeline

A `metadata.lifecycle` map on both views' trace records the five instants
`filed → dispatched → first-commit → ready → landed` (ISO), each sourced independently:

- **filed** — `gh issue view <N> --json createdAt` (the shell derives `<N>` from the branch).
- **dispatched** — `dispatch-<N>.epoch` in the drain's afk state dir (`git-common-dir`/`ai-toolkit-afk`).
- **first-commit** — the earliest commit's author time in the `--commits` numstat dump.
- **ready** — the completion `spoke-ready --phase ready` span already in the traces. Its OTLP span
  name is the `script:ready` label (like the sibling gate span is `script:gate`), so it is matched
  by the `workflow.kind == "script"` / `workflow.phase == "ready"` attributes, not the `--name`.
- **landed** — the instant the land-time ingest ran.

Any leg whose source is absent is omitted, so a partial timeline (e.g. `first-commit` + `ready`
from the traces alone, on a spoke with no lifecycle file) reads distinctly from a guessed value.

### Per-stage overhead scores

Five trace-level numeric scores decompose the wall-clock, chartable exactly like `gate_park_ms`:

- **`stage_spawn_seed_ms`** — dispatch epoch → first-commit author time.
- **`stage_gate_answer_ms`** — PLAN-gate park onset → the `answer-attempt-<N>.epoch` the drain
  stamped (the auto-answer leg, extending the existing `gate_park_ms` park window).
- **`stage_review_ms`** — Σ the review span windows: the `sub-agent:code-review` containers (the
  real-duration signal) plus any `agent:review` broker span (zero-duration today, so it contributes
  0 and the score skips when only those match).
- **`stage_push_gate_ms`** — Σ the `spoke-push` span windows (each brackets a pre-push test gate).
- **`stage_land_ms`** — the `worktree-land` span window. Usually absent at land time (the land
  span has not closed yet), so it is skipped then and captured on a later `--rebuild` backfill.

A stage whose source is absent is skipped (never emitted as 0). A relaunched spoke reuses its
`spoke_run_id`, so its dispatch epoch is re-stamped to the *last* dispatch; a dead-run commit
earlier than that yields a negative spawn+seed delta and is dropped, cross-checked against
`relaunch_count` — no double-count of the dead run.

### Per-drain-window rollups

The per-spoke builder never sees sibling spokes, so the window rollups are read off the afk state
dir the shell snapshotted (dispatch-epoch count + intervention-ledger line count) and stamped as
trace-level scores on **each spoke's own trace**; a dashboard filtered to `mode:afk` reads the
latest (fullest) snapshot per window:

- **`issues_per_hour`** — `spokes_serviced ÷ window_hours` (window = earliest dispatch epoch →
  landed). Skipped when the window is non-positive.
- **`overhead_work_ratio`** — Σ the five overhead stage scores ÷ Σ the root duration rollup's WORK
  components (`llm_request` + `tool` + `step` + `turn`). Skipped when the work sum is 0.
- **`autonomy_score`** — `max(0, 1 - interventions ÷ spokes_serviced)` (#251). An absent ledger
  counts as 0 firings (matching `hub-watchdog`'s `_wd_intervention_count`); skipped only when no
  spoke was serviced.

All scores derive their ids from the spoke run id, so re-running the builder overwrites the same
scores (idempotent), matching the #128 `fetch_session` self-exclusion.

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
  v1 span has no such field. Cache-read tokens are carried on the parsed
  per-turn `UsageEvent`s; surfacing them as a span field is a schema v2
  follow-up, not an in-place change to the frozen contract.
