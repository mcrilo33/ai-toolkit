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
> exists. The view builder itself **sources from Langfuse**, not from the transcript
> parsers: it copies push-span observations and grafts only `input`/`output`. It
> touches `session_parser.py` for one thing only — `project_dir_for_worktree`, to
> resolve the loaded-context root (#87) — never its span/`summary` derivation, which
> has **no production consumer** today. Because the builder reads from Langfuse, a
> single lost land is still re-runnable from its id alone (`--spoke-run-id`) — see the
> #319 note below for why that is not a fleet backfill.

> [!NOTE]
> Issue #91 retired `ccusage` and the pull-cost layer (`telemetry/cost.py`). The
> otelcol remaps tokens to `gen_ai.usage.*`, so **Langfuse computes cost itself**
> from token usage × its model-pricing config. The pull layer now attributes only
> tokens; cost is no longer derived on-machine.

## The 2026-07-13..17 score gap: no backfill (Issue #319)

**Decision: the gap is not backfilled. It stays empty, and empty is honest.**

For ~4 days and 51 drain lands, no `step_tokens_written` / `step_total_cost_usd` /
`step_duration_ms` scores were produced. The sync never shipped the `telemetry/`
package into `.ai-toolkit/scripts/`, so the drain's self-copy (built from that dir,
and not a git checkout) failed `telemetry-ingest-spoke.sh`'s package probe and skipped
the ingest on every land. #319 fixed the cause; this records what was decided about
the data already lost.

A backfill is **technically possible**, and the honest reason not to do it is not
that no path exists:

- #140 retired the **transcript** backfill (`langfuse_backfill.py`), but the view
  builder does not need it — it **sources from Langfuse**, fetching the session's
  traces and copying their observations into the two views.
- Those 51 spokes passed the OTel and auth gates (they reached the package probe,
  which is downstream of both), so their live-pushed generations should still exist
  server-side.
- `build_step_windows` derives the cycle windows from the **trace observations** (the
  `TaskCreate`/`TaskUpdate` ledger ops), not from anything on disk. The dead score
  families are therefore rebuildable in principle.
- `telemetry-ingest-spoke.sh --spoke-run-id <id>` exists precisely as the degraded
  re-run, and the ids are recoverable from `.git/ai-toolkit-afk/land-*.log`.

It is not done because **the re-run would attribute the hub's context to 51 spokes**.
The worktrees are long torn down, so a re-run has no `--root` and no `--request-bodies`,
and `--root` defaults to the cwd — the hub. `read_mode_lane`, `read_outcome` and the
raw-bodies convention would then all read from the **hub checkout**, and the #87
loaded-context itemization would itemize the *hub's* rules/skills/memory as though they
were each spoke's. The commit (#162), lifecycle (#280) and repo-tag (#231) legs have no
source at all.

That trade is bad in the one direction that matters: a gap is visibly empty and asks a
question, whereas a wrong value silently answers it — and poisons the very cost/latency
dashboards (#128, spoke-cost) this data feeds. It is the view builder's own rule (skip a
metric rather than emit a wrong one) and #319's own lesson: the silent-but-plausible
failure is the one that costs days.

> [!TIP]
> The degraded `--spoke-run-id` re-run remains correct for its designed case: **one**
> land whose ingest was lost to a transient Langfuse outage, re-run while the worktree
> still exists. It is not a fleet backfill tool, and nothing here should be read as one.

Everything here is **read-only and 100% local**. Session logs contain prompt
content, so they are parsed on-machine and only metadata / metrics are surfaced —
never raw prompt, answer, thinking, or tool-output text.

> [!NOTE]
> **The `summary` field (Issue #47) does not reach the assembled views.** The pull
> parser (`session_parser.py`) can derive a few-word `summary` per node — the todo a
> step advances, an agent's short task `description`, the first line of a prompt or
> question, a tool's single main parameter — but that is a **pull-parser artifact**
> (`spans.py` documents `summary` as "pull-only"). The land-time view builder
> `langfuse_spoke_tree.py` sources push-span observations from Langfuse and copies
> them verbatim: `spoke_tree/assembly.py::_copy_event` grafts only transcript
> `input`/`output` plus `tool_result_size`/`skill` metadata, and its `_COPIED_FIELDS`
> tuple does not include `summary`. So short-intent-per-node is **not currently
> surfaced** on the `spoketree-`/`spokecycle-` views. Resurfacing it (grafting it in
> `_copy_event`) is a separate enhancement, out of scope here. What the builder does
> graft — the `input`/`output` content — still stays *short intent* only; long-form
> content (extended thinking, an agent's full task prompt, a tool's secondary input,
> and human answers) stays filtered.

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
| `spans.py` | The `Span` dataclass — the frozen schema plus the additive, optional, `summary` field (Issue #47). `summary` is **pull-only**: it is populated by `session_parser.py` and does **not** reach the assembled views (the builder's `_COPIED_FIELDS` omits it). |
| `session_parser.py` | Parse `~/.claude/projects/*/*.jsonl` into `skill` / `agent` / `todo` / `human` spans plus a `tool` leaf per `tool_use` (Issue #47). Walk `<session>/subagents/agent-<id>.jsonl` transcripts into `UsageEvent`s **and** the sub-agent's own step spans (#47 S3) — re-homed onto the parent session with `parent_id` = the agent span, so they nest under it. **The span/`summary` parsing has no production consumer** since #90/#140 retired the DuckDB store and transcript backfill; the view builder uses only `project_dir_for_worktree` from this module (loaded-context root resolution, #87). |
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
| `wait` | Human/gate wait: folded `blocked_on_user_ms` + the gate script (`script:gate`) + the `wait:gate-park` block. The park spans the gate onset → the drain's `answer-attempt` epoch when present (the real PLAN-gate wait, matching `gate_park_ms` / `stage_gate_answer_ms`), else the first-activity resume (#345). |
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
> to the filters. For the PLAN-gate park specifically, `gate_park_ms` is the
> per-spoke answer, and it agrees with `stage_gate_answer_ms` (both measure the park
> onset → the drain's `answer-attempt` epoch since #345). The root
> `rollup.duration.components.wait` includes that same park window, but ALSO the gate
> emission and folded `blocked_on_user_ms` permission wait — so use `gate_park_ms` when
> you want the park alone, and `wait` for total spoke wait.

### Widget 5 — per-skill cost (Issue #322)

The dashboard twin of the rule carry-cost chart. A `skill:<name>` node is a relabeled
`tool:Skill` **span** (#234) whose own `costDetails` is $0 — the real LLM spend lives in
its generation descendants — so an Observations → Total Cost widget filtered to `skill:`
returns $0. The builder therefore emits a per-skill `skill_cost_usd:<name>` numeric score
(:func:`build_skill_cost_scores`) summing the full `costDetails` of every generation in the
skill's subtree, so the $ is a queryable Scores value rather than a UI-only trace-view
rollup.

| Field | Value |
|-------|-------|
| View | Scores (numeric) |
| Filters | `name` *starts with* `skill_cost_usd:` |
| Metrics | `value` — `sum` |
| Breakdown dimension | `name` |
| Chart | Horizontal bar |

> [!NOTE]
> **Coverage caveat — only `Skill`-tool invocations mint a `skill:` span.** A `skill:<name>`
> node is minted from every `tool:Skill` span, named from the Skill tool call's input. Skills
> invoked as **slash commands / prompt expansions** (`/source-task`, `/land`, most
> drain-driven skills) do **not** create a `tool:Skill` span, so they never score. Only skills
> the model invokes through the **Skill tool** (e.g. `code-review` during a cycle) appear — so
> read the chart as "cost of Skill-tool-invoked skills", not "every skill that ran." Capturing
> slash-invoked skills is a separate, larger change. A skill node with no generation
> descendants is skipped (not charted as $0), matching the ready-but-latent `skill_success`
> idiom.

### Widget 6 — per-sub-agent cost (Issue #323)

The exact analog of Widget 5 for sub-agents — the most expensive spawned unit in a run,
and previously a cost blind spot. A `sub-agent:<type>` node is the otelcol-renamed
`tool:Agent` **container** (#161) whose own `costDetails` is $0 — the real LLM spend lives
in its `sub-agent:llm` generation descendants — so an Observations → Total Cost widget
filtered to `sub-agent:` returns $0. The builder therefore emits a per-agent
`agent_cost_usd:<type>` numeric score (:func:`build_agent_cost_scores`) summing the full
`costDetails` of every generation in the agent's subtree, reusing the same
`_cost_subtree_ancestors` rollup helper as `skill_cost_usd`. A fan-out of N same-type agents
emits N observation-scoped scores under one name, so the *Sum / breakdown Name* below folds
them into one volume-aware bar.

| Field | Value |
|-------|-------|
| View | Scores (numeric) |
| Filters | `name` *starts with* `agent_cost_usd:` |
| Metrics | `value` — `sum` (add `count` to separate "expensive per run" from "expensive because many") |
| Breakdown dimension | `name` |
| Chart | Horizontal bar |

> [!NOTE]
> **Reconciliation — `agent_cost_usd:<type>` is a *subset* of `step_total_cost_usd`, not
> additive.** The `sub-agent:llm` generations a sub-agent runs are already folded into the
> `step_total_cost_usd:<PHASE>` of the cycle-step the agent sits in (that per-step total
> windows *every* generation, main-loop and sub-agent). So the agent-cost bar re-cuts a slice
> of the step total by agent type — never read it *on top of* the step cost. A container with
> no generation descendants is skipped (not charted as $0), matching the `skill_cost_usd` /
> ready-but-latent `skill_success` discipline.

### Widget 7 — per-agent verdict + the status contract (Issues #233, #325)

Whether spawning a sub-agent *succeeded* is a per-agent `agent_verdict:<type>` score
(`1.0` success / `0.0` reject), from two sources (:func:`build_agent_verdict_scores`):

- **code-review** — the signed `.review/*.json` artifacts (APPROVE → 1.0,
  REQUEST_CHANGES → 0.0). Trace-level, authoritative; the `sub-agent:code-review`
  container is *not* also scored from its output, to avoid double-counting.
- **every other `sub-agent:<type>`** — the generic path (:func:`_sub_agent_verdict`):
  it reads a `status` off the agent's grafted `output` (:func:`_output_status`) and maps
  it through `_AGENT_SUCCESS_STATUSES` — `success, completed, approved, pass, passed, ok,
  done` score 1.0, any other status scores 0.0.

**The status contract (agent side).** `_output_status` runs `json.loads()` on the
**entire** `output` and requires a dict with a `status` key, so an agent scores a verdict
only when its **final message is a single JSON object** (no prose around it, under the
20k graft cap) carrying that key. This is a return-contract obligation on the agent brief,
not a builder feature — the builder has been generic since #233; #325 is what makes real
agents emit the status. The briefs that carry it today:

| Agent | Success status | When |
|-------|----------------|------|
| `bug-scoper` | `completed` | issue filed / drafted / deduped (a `blocked` status marks an investigation it could not complete) |
| `planner` | `completed` | a plan was produced (`blocked` when the goal was too ill-defined to decompose) |
| `code-review` | — | scored from the `.review` artifact, not a returned status |

A reaper-killed (`ERROR`-level) container instead scores a separate
`agent_verdict:<type>:died` **count** (kept off the 0/1 rate name so it never collides).

> [!NOTE]
> **Research / read-only agents carry no status — and that absence is not a failure.**
> `Explore`, `general-purpose`, `claude-code-guide`, `documentation`, and `architect`
> return prose findings/designs with no discrete pass/fail, so `_output_status` reads
> `None` and they score **nothing**. A missing `agent_verdict:<type>` for these agents
> means "not a scoreable outcome", never "the agent failed" — absence is not a firing
> basis (AFK Design Principle 6). Do not read their empty verdict bar as a 0% success
> rate; filter the widget to the status-carrying types above.

| Field | Value |
|-------|-------|
| View | Scores (numeric) |
| Filters | `name` *starts with* `agent_verdict:` (exclude `:died` for the pass rate) |
| Metrics | `value` — `avg` for the success rate; add `count` for volume |
| Breakdown dimension | `name` |
| Chart | Horizontal bar |

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
  the builder already has (a ratio is skipped when its denominator is 0). `subtasks` is always
  emitted; the three churn base counts are emitted only when a commits dump was actually parsed
  (the land threads the pre-merge tip so the range is non-empty, issue #344) — when no dump
  reaches the builder they are **skipped, not scored 0**, since an absent dump is not evidence of
  zero churn (the pull layer's skip-rather-than-emit-a-wrong-value rule). A present-but-empty dump
  (a genuinely empty spoke) still reads 0.
- **Cross-project** — a `repo:<name>` trace tag (resolved by `telemetry-ingest-spoke.sh` from
  the origin remote, else the checkout dir basename) and the Langfuse `environment` field:
  the assembled views are stamped `production`, and `dashboard/langfuse/otelcol.yaml` stamps
  `langfuse.environment` on every live spoke span (defaulting to `production`, overridable via
  `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=<env>` for a test collector).

### The repo dimension on live spans (Issue #343)

The `repo:<name>` tag above lands only on the **assembled** `spoketree-`/`spokecycle-` traces
at land time, but the spoke-latency dashboard's Observations/Scores widgets query the **live**
per-turn spans — which historically carried only `spoke_run_id`. So one shared Langfuse project
could not slice those widgets per repo. #343 closes that: the spoke launch now carries
`repo=<name>` in `OTEL_RESOURCE_ATTRIBUTES` (resolved by `wt_repo_name` in
`scripts/worktree-lib.sh` from the origin basename, matching the #231 land-time tag), and
`otelcol.yaml` lifts it onto every live span two ways —

- as the `repo:<name>` **trace tag** (`langfuse.trace.tags`), so the #231 "group by `tags`"
  Traces-view recipe now works on live data too; and
- as `metadata.repo` **observation metadata** (`langfuse.observation.metadata.repo`), a
  filterable key the Observations/Scores widgets can scope on.

Both stamps are guarded on the attribute, so an opted-out spoke (`AI_TOOLKIT_OTEL=0`) writes
neither. The unit tests pin only the config **structure**; that Langfuse actually parses the
JSON-array tag string is an operator-verify step (like the #128/#231 Langfuse behaviour), and
`error_mode: ignore` means a tag Langfuse cannot parse is dropped while the `metadata.repo`
filter still lands.

### One project, per-repo + merged dashboards (Model A)

With every tracked repo exporting to the **same** Langfuse project (identical
`telemetry.langfuse.host`/`project`/keys in each synced `settings/ai-toolkit.yml`), the
existing dashboards serve both views with no duplication:

- **Merged (all projects)** — the dashboard with no repo filter reads every repo at once.
- **Per-project** — add a `repo` dashboard **variable** (or a per-widget
  `{"column": "metadata.repo" | "tags", "operator": "=" | "any of", "value": "<repo>"}` filter)
  and switch it, rather than cloning the dashboard N times.
- **Compare** — break down by `repo:` tag (Traces view) or `metadata.repo` (Observations).

The alternative — one Langfuse **project** per repo — gives hard isolation but has no native
merged dashboard (v3.192.x has no cross-project dashboard) and recreates the dashboard per
project, so Model A is preferred whenever a merged view is wanted.

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
  stamped (the auto-answer leg). Since #345 fed the same answer epoch into `gate_park_ms`, the two
  measure the same window and agree; `gate_park_ms` falls back to the first-activity resume only
  when no answer epoch was stamped (pre-#280 lands / degraded re-runs).
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
