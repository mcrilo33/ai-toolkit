# Spike: per-spoke usage via Claude Code native OTel → Langfuse (Issue #83)

An **evaluation**, not a migration. Claude Code can export its own OpenTelemetry
trace per session; this spike streams each spoke's trace into Langfuse (or any local
OTLP collector), grouped by `spoke_run_id`, and asks whether that native view could
replace the custom causal-tree dashboard.

The verdict is **hybrid** — keep both. The native trace is a near-zero-maintenance
*live drill-down* into one spoke session; it does not reproduce the dashboard's
cost-reconciliation, cross-session spoke lifetime, or conservation invariants. The two
answer different questions.

> [!NOTE]
> Historical artifact. Since this spike, #90 removed the Streamlit dashboard + DuckDB
> store and #91 retired `ccusage` and the pull-cost layer — Langfuse is now the single
> observability surface and computes cost itself from token usage. The `ccusage`
> "authoritative cost" framing below reflects the state at spike time, not today.

> [!NOTE]
> The dashboard and `telemetry.causal_strict` are deliberately untouched by this spike.
> The only code change is an **opt-in** env prefix on the spoke launch
> (`scripts/worktree-new.sh`), gated on `AI_TOOLKIT_OTEL=1`. With the gate off, the
> launch is byte-for-byte unchanged.

## What the wiring does

`worktree-new.sh` already mints a `spoke_run_id` (`<branch>+<spawn-epoch>`) into
`.ai-toolkit/spoke-run-id` for every worktree, independent of any telemetry gate. When
`AI_TOOLKIT_OTEL=1`, the script prefixes the spoke's `claude` launch — reusing the same
command-prefix lever as the existing `WT_SPOKE` / `CLAUDE_EFFORT` pins — with Claude
Code's native-OTel **trace** env:

```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1 \
CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 \
OTEL_TRACES_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=<branch>+<epoch> \
  claude --model …
```

The interactive `claude` then emits one nested trace — turns, tools, sub-agents,
workflows — and every span carries `spoke_run_id` as a resource attribute, so a single
Langfuse/collector query groups the whole run.

> [!WARNING]
> Tracing is **beta** (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`) and off by default.
> Claude Code emits **traces** here; Langfuse OTLP ingests traces, not logs/metrics.

### Secret handling

The script wires only the **non-secret** enabling flags plus the spoke identity. The
connection **target** — `OTEL_EXPORTER_OTLP_ENDPOINT` and the auth-bearing
`OTEL_EXPORTER_OTLP_HEADERS` — is *never* placed on the command line (it would be visible
in `ps`/tmux) and never printed in the manual-fallback advice. It is operator-provided
through the environment `claude` inherits. This split is what keeps a Langfuse Basic-auth
token off every process listing.

## Enabling it

### 1. A local OTLP collector (recommended for evaluation)

No credentials, nothing leaves the machine. Run any OTLP/HTTP receiver on `:4318`, e.g.
a Jaeger all-in-one or `otel-tui`:

```bash
# Jaeger all-in-one (UI on :16686, OTLP/HTTP on :4318)
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest

# Point the spoke at it, then dispatch with the gate on:
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
AI_TOOLKIT_OTEL=1 ./scripts/worktree-new.sh <issue> <slug> --prompt "/source-task <issue>"
```

> [!NOTE]
> The endpoint must reach the *interactive* `claude`. `tmux new-window` inherits the tmux
> **server** environment, not the dispatching shell, so either export the target before
> the tmux server starts, propagate it with `tmux setenv`, or run `claude` directly in a
> shell that already has it exported. The non-secret prefix the script injects always
> crosses the boundary; the operator-provided target may not.

### 2. Langfuse (documented, not exercised in this spike)

Langfuse exposes an OTLP endpoint at `/api/public/otel` (per-signal traces route
`/api/public/otel/v1/traces`). Auth is HTTP Basic from the project keys; gRPC is not
supported, so keep `http/protobuf`.

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://us.cloud.langfuse.com/api/public/otel
# base64("pk-lf-…:sk-lf-…"); keep this out of shell history and process listings
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(pk:sk)>"
```

Self-hosted Langfuse (≥ v3.22.0) is the same path on the local host
(`http://localhost:3000/api/public/otel`).

## Validation (observed)

The export path was exercised end to end against a minimal local OTLP/HTTP receiver on
`:4318` (no Langfuse, no third party). A real headless `claude --model haiku -p …` was
run with the prefix above and `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`, plus
a sentinel `OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=feature/83-otel-validation+SPIKE83PROBE`.

A single trace export (one OTLP/protobuf POST to `/v1/traces`) arrived and contained:

- **Resource**: `service.name=claude-code`, `service.version`, and the sentinel
  `spoke_run_id` — so the run is queryable by spoke.
- **Scope**: `com.anthropic.claude_code.tracing`.
- **Spans**: `claude_code.interaction` (with `interaction.duration_ms`,
  `interaction.sequence`) and `claude_code.llm_request` (with `gen_ai.request.model`,
  `gen_ai.request.attempt`, `client_request_id`, `llm_request.context`). The
  `spoke_run_id` appeared on the resource **and** each span.

This confirms acceptance: native traces export to a local collector and the whole run
groups under `spoke_run_id`. Two honest caveats on what the *minimal probe* did **not**
exercise (consistent with the comparison below, not contradicting it):

- A one-shot headless `-p` run emits one `interaction` + one `llm_request`. A real
  interactive spoke would add **tool**, **sub-agent**, and **workflow** spans nested in
  the same trace — present in the design, just not surfaced by a trivial prompt.
- The spans carry **token/latency** attributes but **no dollar cost** — reconfirming that
  cost reconciliation is not native (see below).

## LLM messages on spans (message bridge)

The native wiring above renders the span *tree* — turns, tools, sub-agents — but the
`llm_request` observations come up empty: Claude Code does **not** put the LLM request and
response bodies (the actual conversation messages) on the trace spans. It emits them on the
OTel **logs** signal instead, as the events `api_request_body` and `api_response_body`, and
those log records carry **no** `trace_id`/`span_id`. Langfuse trace ingestion never reads
the logs signal, so on its own it can't attach the messages to the matching observation.

`scripts/telemetry/langfuse_message_bridge.py` closes that gap. The collector forks both
signals to it (the `otlphttp/bridge` exporter on the `traces` and a new `logs` pipeline),
and the bridge joins them by the ids already in the data, then PATCHes each Langfuse
observation's input/output (Langfuse observation id == OTel `span_id`) via a
`generation-update` ingestion event. The join chain, derived from the real telemetry:

```text
api_request_body.prompt.id --(api_request maps prompt.id→request_id)--> request_id
request_id --(the llm_request span carries request_id)--> span_id
api_response_body.request_id ----------------------------------------> span_id
```

Both signals are buffered and re-resolved as each piece arrives, so trace/log ordering is
irrelevant — a message that lands before its span waits and flushes the moment the span
shows up.

### Running it

The bridge is stdlib-only and runs on the host beside the collector:

```bash
# the collector reaches it at ${env:BRIDGE_OTLP_ENDPOINT} (e.g. host.docker.internal:4319)
LANGFUSE_BASIC_AUTH="Basic $(printf 'pk-lf-…:sk-lf-…' | base64)" \
  python3 scripts/telemetry/langfuse_message_bridge.py
```

`LANGFUSE_HOST` (default `http://localhost:3000`) and `BRIDGE_PORT` (default `4319`) are the
other knobs. The spoke must additionally export `OTEL_LOGS_EXPORTER=otlp` and
`OTEL_LOG_RAW_API_BODIES=1` so Claude Code actually emits the bodies; the collector and the
exact `docker run` / spoke-launch lines are documented in the header of
[`dashboard/langfuse/otelcol.yaml`](../dashboard/langfuse/otelcol.yaml).

### File mode and the per-spoke body dir (issue #87)

`OTEL_LOG_RAW_API_BODIES` also accepts `file:<dir>`, which dumps each request **untruncated**
(no 60KB cap) to `<dir>/<uuid>.request.json` (and responses to `<request_id>.response.json`)
and emits a `body_ref` path attr on the log event instead of the inline `body`. The heavy
conversation content therefore stays on local disk; only the path rides the logs signal.
This is what the loaded-context itemization consumes.

`worktree-new.sh` wires this under the `AI_TOOLKIT_OTEL=1` gate: it creates
`<worktree>/.ai-toolkit/raw-bodies/` (gitignored, per-spoke), sets
`OTEL_LOG_RAW_API_BODIES=file:<that dir>`, and exports `AI_TOOLKIT_OTEL_BODY_DIR=<that dir>`.
The post-run spoke-tree builder finds the dumps via `--request-bodies`, defaulting to
`$AI_TOOLKIT_OTEL_BODY_DIR` and then to `<root>/.ai-toolkit/raw-bodies`. The FIRST
`llm_request` can be a degenerate aux call (tiny prefix, empty `tools`); the builder skips it
and itemizes the first request whose `tools` array is non-empty.

### Caveats

- **60KB truncation (inline mode only).** In the legacy inline mode (`=1`), request bodies
  over ~60KB hit Claude Code's body cap and arrive as truncated, invalid JSON; the bridge
  falls back to storing the raw partial text so the request start is still visible. File mode
  (`=file:<dir>`, above) has no such cap — prefer it when itemization needs the full body.
- **Redacted thinking.** Extended-thinking blocks come through as `<REDACTED>`; the bridge
  forwards them verbatim — it does not (and cannot) reconstruct them.
- **Content leaves the box.** Like the opt-in `OTEL_LOG_*` content flags, raw API bodies
  ship the full conversation to the collector and Langfuse, so this stays operator opt-in,
  not part of the `AI_TOOLKIT_OTEL` gate.

## Native view vs the custom dashboard

| Capability | Native OTel trace | Custom dashboard |
|------------|-------------------|------------------|
| Per-turn / tool / sub-agent / workflow tree | ✅ live, automatic | ✅ rebuilt from logs |
| Group one run by `spoke_run_id` | ✅ resource attribute | ✅ minted + backfilled |
| Latency + token counts per span | ✅ from the SDK | ✅ bracketed per turn |
| Setup / zero maintenance | ✅ env vars only | ❌ parser + store to maintain |
| Authoritative **cost** (`ccusage`) | ❌ token-only, no `$` reconcile | ✅ per-session `totalCost` |
| Cross-session spoke **lifetime** (resume) | ⚠️ many traces, one id | ✅ one spoke-run rollup |
| **Conservation** invariants (Σ owned == Σ turns, time tiling) | ❌ | ✅ asserted |
| Strict **causal** rebuild (ids, not timestamps) | ⚠️ SDK span tree only | ✅ `causal_strict` |
| Cold-context / loaded-but-unused lens | ❌ | ✅ |
| Marker-driven step spine (RED/GREEN/gate) | ❌ not modeled | ✅ push spans |
| Offline / no third party | ⚠️ needs a collector | ✅ 100% local |

### What is lost and must be reattached

- **Cost.** Native traces carry token counts but no dollar figure. The dashboard's
  authority is `ccusage session --json` `totalCost`, distributed across spans by token
  share (see [telemetry-pull-layer.md](./telemetry-pull-layer.md)). A native-only world
  would still need that offline reconciliation.
- **Conservation + spoke lifetime.** The dashboard guarantees `Σ owned == Σ turns` with
  no double-count under re-parenting, plus time tiling and a single per-spoke rollup
  across resumes (see [dashboard-spoke-trace-scope.md](./dashboard-spoke-trace-scope.md)).
  The native view is per-session and unconstrained — fine for eyeballing one run, not for
  invariant-checked accounting.
- **Strict causal rebuild + the marker spine.** `causal_strict` reconstructs causality
  from ids and threads the RED/GREEN/REVIEW/gate markers (push spans) as the step spine.
  Native traces give the SDK's own span parent/child tree only — no marker steps, no
  cross-session emission links.

## Recommendation: hybrid

Keep the dashboard as the system of record (cost, conservation, causal spine,
offline-only) and keep the native trace as an **opt-in live lens** for deep-drilling a
single in-flight spoke without waiting for a log re-parse. The wiring is cheap, fully
gated, and leaks no secrets, so it can live alongside the existing telemetry with no
entanglement: `AI_TOOLKIT_OTEL` (this spike) and `AI_TOOLKIT_TELEMETRY` (the push layer)
are independent gates.

Do **not** retire the dashboard on the strength of native traces alone — the cost
reconciliation and conservation guarantees have no native equivalent.

## Per-container token rollup (post-run)

Langfuse rolls *cost* and *latency* up onto container spans at render time, but it does
**not** roll up the token breakdown. `scripts/telemetry/langfuse_rollup.py` fills that gap.
For one session it walks each trace, rebuilds the observation tree from
`parentObservationId`, and for every container observation (one that *has* children) sums
the four token components — `input`, `output`, `cache_read_input_tokens`,
`cache_creation_input_tokens` — over its whole subtree (itself plus all descendants). The
sum is patched back as `metadata.rollup = {reused, written, input, output}` (`reused` is
the cache-read total, `written` the cache-creation total) via the same ingestion endpoint
and `LANGFUSE_HOST` / `LANGFUSE_BASIC_AUTH` env vars as the message bridge.

```bash
LANGFUSE_HOST=http://localhost:3000 LANGFUSE_BASIC_AUTH="Basic <base64(pk:sk)>" \
    python3 scripts/telemetry/langfuse_rollup.py <spoke_run_id>
```

Notes and limits:

- **Run it after the trace is fully ingested.** It reads the observations already in
  Langfuse; a partially-ingested trace yields partial sums. Re-running is idempotent (the
  ingestion event id derives from the observation id, so a rerun overwrites
  `metadata.rollup` rather than appending).
- **It shows in the span's metadata panel, not a native ∑ column.** Langfuse has no UI
  surface for a custom per-span token total, so the rollup lives under `metadata.rollup`.
- **Leaf tools roll up to zero.** `Bash`, `Read`, and other leaf tools make no API call,
  so their subtree sums to zero — correct. Containers (`interaction`, `tool:Workflow`,
  sub-agent) get their real subtree totals.
- **Skills are not covered.** A skill's work is not nested under the `Skill` span, so it is
  not attributed to that span's subtree. Covering skills needs a scope rule; deferred.

## Single nested spoke-tree (`langfuse_spoke_tree.py`)

Natively, each turn Claude Code runs lands as its own flat Langfuse trace, and the marker
(`step:`/`lifecycle:`/`spoke-push`) and hook (`*.sh`) emissions land as yet more flat
traces, so one spoke reads as dozens of disconnected traces. But every one of those
observations *already* carries the rich fields we built — `usageDetails`, `costDetails`,
`input`/`output` messages, `metadata` (including `rollup` and, on hooks,
`hook_event`/`tool_name`/`tool_use_id`/`decision`/`duration_ms`), `name`, `type`, and
`startTime`/`endTime`.

`scripts/telemetry/langfuse_spoke_tree.py` **assembles those existing rich observations
into one nested trace, preserving every field**. It sources from Langfuse, not from the
causal store: it fetches every trace in the session and every observation in those traces,
then copies each observation verbatim into one new trace, re-parenting across the original
trace boundaries. It ships via the same ingestion endpoint and `LANGFUSE_HOST` /
`LANGFUSE_BASIC_AUTH` env vars as the rollup and message bridge:

```bash
LANGFUSE_HOST=http://localhost:3000 LANGFUSE_BASIC_AUTH="Basic <base64(pk:sk)>" \
    python3 scripts/telemetry/langfuse_spoke_tree.py <spoke_run_id>
```

The assembly shape:

- One `trace-create` (`sessionId = spoke_run_id`, name `spoke-tree:<spoke_run_id>`) and one
  synthetic root span `spoke:<spoke_run_id>` — the single collapsed root.
- One copy per source observation, fields intact: a `GENERATION` becomes a
  `generation-create`, anything else a `span-create`; `name`, `startTime`/`endTime`,
  `input`, `output`, `usageDetails`, `metadata`, `model`, and `level` are copied verbatim.
  `usageDetails` + `model` are re-passed so Langfuse recomputes `costDetails` identically
  (an explicit `costDetails` is forwarded too).
- Re-parenting: an observation with a `parentObservationId` keeps that link (remapped to the
  copy); a trace-root interaction / marker / lifecycle / script collapses to the synthetic
  root; a trace-root hook re-parents under the tool whose `tool_use_id` matches the hook's
  `metadata.tool_use_id`, or the synthetic root when there is no id or no match.
- Tool content: for a visible `tool:<Name>` span the builder grafts transcript-sourced
  `input`/`output` into the same create body (see the next section), so the fresh
  observation carries content the native span lacked.
- Per-container token rollups and a fully-itemized loaded-context subtree, both emitted as
  CREATE bodies in the same build (see the two sections below).

Notes and limits:

- **Run it after the per-turn native traces are ingested** — it reads the observations
  already in Langfuse; a partially-ingested session yields a partial tree.
- **It duplicates the native per-turn traces by design.** The native traces stay; this is
  the additional, single-tree view of the whole spoke. Filter to `name = spoke-tree:*` (or
  the `spoketree-` trace-id prefix) to see only the assembled trees.
- **Idempotent.** The trace id, the synthetic root id, and every copy id derive from the
  spoke run id and the source `(trace_id, observation_id)` pair, so a rerun overwrites the
  same trace/observations rather than appending duplicates.

## Transcript → Langfuse backfill (`langfuse_backfill.py`)

`langfuse_spoke_tree.py` assembles a tree from observations the live push **already**
wrote, so it only works for fully-instrumented spokes. The session transcript uniquely
owns three things the live push cannot reconstruct: **extended-thinking bodies** (redacted
in every raw API body), **true causal edges** (`uuid`/`parentUuid`), and **coverage of
sessions that ran un-instrumented, while the collector was down, or historically**.

`scripts/telemetry/langfuse_backfill.py` is the keystone that makes Langfuse the single
complete source. It **sources from the transcript**: it reuses `session_parser.py` plus
`causal_tree.py` to assemble one spoke's causal forest from the local `~/.claude`
transcript, then translates that forest into Langfuse ingestion events — the second sink
for the same forest the dashboard renders.

```bash
LANGFUSE_HOST=http://localhost:3000 LANGFUSE_BASIC_AUTH="Basic <base64(pk:sk)>" \
    python3 scripts/telemetry/langfuse_backfill.py <spoke_run_id> [--thinking]
```

The translation:

- One `trace-create` (`sessionId = spoke_run_id`, name `spoke-backfill:<spoke_run_id>`) and
  a synthetic root, in a **backfill-owned id namespace** (`spokefill-`/`fillroot-`/`fill-`)
  distinct from the spoke-tree's (`spoketree-`/`spoke-tree:`), so the two views never
  collide.
- One event per causal node: a cost-bearing `turn`/`agent` leaf becomes a
  `generation-create` with four-component `usageDetails` (the ccusage cost join is a
  deferred upgrade); every other node a `span-create`; containers get `metadata.rollup`
  summed over the subtree.
- A `reasoning` node carries the **extended-thinking body** as its `output`, read only when
  the opt-in is set (`--thinking` or `AI_TOOLKIT_BACKFILL_THINKING=1`) — never otherwise
  (volume / privacy).

Idempotency and dedup are two separate mechanisms:

- **Re-running the backfill** is idempotent: every id derives from `(spoke_run_id,
  node_id)` — and a causal `node_id` is the transcript `uuid` — so a rerun overwrites the
  same observations rather than appending.
- **Backfill vs the live push** is deduped by a coverage query (`/traces?sessionId=...`):
  when the live push already covered the session (a native, non-synthetic trace exists),
  the backfill emits only the thinking bodies (covered + opt-in) or **nothing** (covered,
  no opt-in) — never a competing full tree. An un-instrumented session (no native trace)
  gets the full forest.

## Tool content from the transcript (in the tree builder)

Claude Code's native OTel surfaces the full `full_command` for Bash, but for every *other*
tool it emits only `tool_name`, `tool_use_id`, and `duration` — the span arrives with
`input=None`, so TaskCreate/TaskUpdate, Read, Edit, and friends are contentless. The actual
content never reaches the span; it lives in the session **transcript** (`*.jsonl`): each
assistant `tool_use` block carries `{id, name, input}` and the matching user `tool_result`
block carries `{tool_use_id, content}`.

`langfuse_spoke_tree.py` **fills that content as it creates each `tool:` observation**,
joining by `tool_use_id`. Because the copy step emits a `*-create` event that sets every
field at once, the content lands in the same event that fixes the observation's `name` and
`type` — nothing is overwritten or cleared. The work happens during assembly; no extra
command is needed:

- **Scan the transcripts.** Walk every `*.jsonl` under `--projects` (default
  `~/.claude/projects`) for `tool_use` (id → input, including the todo-ledger
  TaskCreate/TaskUpdate subjects + status, Read paths, Edit strings, ...) and `tool_result`
  (id → content) blocks, keeping only the ids carried by this spoke's `tool:` spans.
  Tool-call ids are globally unique, so no per-session transcript mapping is needed.
- **Graft at create time.** For each visible `tool:<Name>` span, if the copied `input` is
  empty/`None` it is set from the transcript input map keyed by the span's `tool_use_id`
  (`metadata.attributes.tool_use_id` / `gen_ai.tool.call.id`); likewise `output`. Content the
  source span already carries — Bash's collector-provided `input` — is **never** overwritten,
  and non-`tool:` spans are untouched. Input/output larger than ~20 KB is truncated with a
  marker.

The run prints `… , N tool spans filled from transcript` alongside the assembly summary.

> [!NOTE]
> A standalone `langfuse_tool_content.py` previously PATCHed content onto the already-ingested
> spans via the ingestion API's `span-update`/`generation-update`. That was **destructive**:
> an update body that omits `name`/`type` makes Langfuse *clear* the observation's name and
> type (a `tool:TaskCreate` became `name="" type=SPAN`), and it fought eventual-consistency
> timestamp merges. The tree builder's create-time approach has neither problem, so the
> patcher was retired.

## Per-container token rollups (in the tree builder)

The standalone `langfuse_rollup.py` patches `metadata.rollup` onto the *native* per-turn
traces after the fact. The assembled tree re-parents observations across trace boundaries,
so `langfuse_spoke_tree.py` recomputes the rollup over the **assembled** structure and
writes it into each container's CREATE body — no destructive patch, and it reflects the
single-tree parentage (sub-agents nested under their `tool:Agent`, every turn under the
spoke root).

For every container node — any node that has children once re-parented: each
`interaction`, each `tool:Agent`, each sub-agent, and the synthetic spoke root —
`metadata.rollup = {reused, written, input, output}` is the subtree sum of the four usage
components (`cache_read_input_tokens` = reused, `cache_creation_input_tokens` = written,
`input`, `output`) over itself and all descendants, using the same sum logic as
`langfuse_rollup.subtree_totals`. Leaves (a single `llm_request`, a `Bash`/`Read` tool)
are not containers and keep their metadata verbatim.

## Itemized loaded-context subtree (in the tree builder)

Under the spoke root the builder also emits a `loaded-context` subtree that itemizes the
session's full first-call prefix. Its **primary source is the untruncated raw request body**
(`OTEL_LOG_RAW_API_BODIES=file:<dir>`, see below): `request_body` parses the first real
`.request.json` and every loaded-context section becomes a named item with its exact size.
The shape:

- A `loaded-context` parent under the synthetic root carrying the itemized total.
- One **category** node per group (`tools`, `mcp`, `system`, `context`) with its rolled-up
  total.
- One **item** node per name under its category, `metadata = {tokens, cost_usd, source,
  cached, estimated?}`:

| Category | Items |
|----------|-------|
| `tools` | one node per resident tool schema, by name (`Workflow: 20.9k`, `Bash: 11.9k`, …) |
| `mcp` | one node per connected `mcp__server__tool` schema, by name |
| `system` | one node per system block (billing header / identity preamble / base system prompt / tool-use + output prompt) |
| `context` | one node per `messages[0]` `<system-reminder>` by kind (session-start-hook / deferred-tools / agent-types / skills / rules+memory+env) plus the residual prompt |

Because the request body IS the complete prefix, the primary path needs **no
reconciliation** — there is no floor and no derived `mcp` aggregate. Deferred tools are
named-only in a reminder (their schemas are not in `tools`), so they are counted, not sized.
The real `cache_control` prefix boundaries are read directly from the body.

```bash
LANGFUSE_HOST=http://localhost:3000 LANGFUSE_BASIC_AUTH="Basic <base64(pk:sk)>" \
ANTHROPIC_API_KEY=sk-… \
    python3 scripts/telemetry/langfuse_spoke_tree.py <spoke_run_id> \
        --request-bodies "$AI_TOOLKIT_OTEL_BODY_DIR"   # the spoke's file-mode dump dir
```

#### Disk fallback (no request body)

When no request body is available (`--request-bodies` / `$AI_TOOLKIT_OTEL_BODY_DIR` unset or
the dir holds no real request), the builder falls back to disk measurement of the
source-measurable categories (`rules`, `memory`, `skills`, `sub-agents`, `environment` — via
`measure_context_cost.assemble_items` / `measure_items`) plus a **single** reconciled
`remainder` node = `prefix_total − Σ(measured disk)`, clamped at zero, absorbing the base
system prompt, all tool schemas, and MCP together. The prefix it reconciles against is the
**full** first-call prefix — `cache_read_input_tokens + cache_creation_input_tokens` of the
first `llm_request`, not `cache_creation` alone (a warm cache splits the prefix across both,
so `cache_creation` alone collapses the remainder to near zero).

| Category | Source | Per-item granularity |
|----------|--------|----------------------|
| `rules` | `CLAUDE.md` + `.claude/rules/*.md` | one node per file (exact, from disk) |
| `memory` | `MEMORY.md` + a `memory/` dir's `*.md` | one node per file (exact) |
| `skills` | each `.claude/skills/*/SKILL.md` frontmatter | one node per skill name |
| `sub-agents` | each `.claude/agents/*.md` frontmatter | one node per agent name |
| `environment` | reconstructed block | single node, always `estimated` |

## Loaded-context cost baseline (`measure_context_cost.py`)

Claude Code writes a session prefix to the prompt cache on the first call of a session;
that prefix shows up as `cache_creation` tokens. The prefix is assembled from several
categories, and we want to know how much each one costs so the dashboard can attribute the
cold-cache write rather than treating it as one opaque number.

`scripts/telemetry/measure_context_cost.py` measures the categories that are
*source-measurable* — reconstructable from on-disk files in the worktree — and stores their
token count and cost in a reusable manifest. A later decomposition step reads the manifest
instead of re-deriving the numbers.

### What it measures vs the reconciled remainder

Measured from the worktree (`--root`, default cwd):

| Category | Source |
|----------|--------|
| `rules` | `CLAUDE.md` + `.claude/rules/*.md` (concatenated text) |
| `memory` | `MEMORY.md` + a `memory/` dir's `*.md` if present |
| `skills` | the injected available-skills list — `name` + `description` parsed from each `.claude/skills/*/SKILL.md` frontmatter, NOT the SKILL bodies |
| `sub-agents` | the agent-types list — `name` + `description` from each `.claude/agents/*.md` frontmatter |
| `environment` | a reconstructed block (platform, cwd, a date placeholder, git user email); always flagged `estimated` since it never matches the runtime byte-for-byte |

Three categories are **not** measured from disk here because they are not sourceable from a
standalone script: MCP connector / OAuth schemas, built-in tool schemas, and the base system
prompt. This disk path is the **fallback** the spoke tree uses only when no raw request body
is available; when one is (the primary path), `request_body` itemizes all three directly
from the body. In the disk fallback they are reconciled together against the real first-call
prefix (`cache_read + cache_creation`, not `cache_creation` alone) as a single remainder:

```text
remainder ≈ prefix_total − measured_disk_total
```

Token counting uses the Anthropic `count_tokens` endpoint when reachable
(`ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`, or `--endpoint` / `--api-key`); when
unreachable or uncredentialed it falls back to a `len(text) // 4` estimate and marks that
category `estimated`. It never hard-fails on missing creds. Cost is `tokens × price`, where
`--price` defaults to the Opus cache-creation rate (`0.00000625` USD/token).

### Where the manifest lives

`<root>/.ai-toolkit/context-cost.json` — gitignored, since it is a generated per-checkout
artifact. Each category records `tokens`, `cost_usd`, `source_files`, a `content_hash`
(sha256 of the concatenated sources), and `estimated`. The manifest also carries an `items`
array — one entry per file / skill / agent (`category`, `name`, `tokens`, `cost_usd`,
`source`, `estimated`) — which is what the spoke tree's itemized loaded-context subtree
reads. The manifest is idempotent: the same sources (same per-category `content_hash`) yield
the same token/cost numbers.

```bash
# count_tokens path — counts match what the runtime caches:
ANTHROPIC_API_KEY=sk-… python3 scripts/telemetry/measure_context_cost.py --root .

# offline estimate path — no creds needed; categories flagged estimated:
python3 scripts/telemetry/measure_context_cost.py --root .
```

### Validation runbook (operator-run against a live session)

These checks are **not** unit tests — the unit suite stubs `count_tokens` and never touches
the network. They are run manually by the operator against a real Claude Code session's
telemetry.

1. **Reconciliation.** Run the script in the spoke's worktree, then read the first LLM
   call's `cache_creation` from that session's telemetry. `Sum(measured) + remainder`
   should approximate the first call's `cache_creation`, where `remainder` is the
   system-prompt + built-in-tool + MCP portion from check 3. A large unexplained gap means
   a measured category drifted from what the runtime actually loaded. (This validates the
   disk fallback; when a raw request body is present the spoke tree itemizes all of it
   directly and no remainder is needed.)
2. **Differential.** Add one rule (or one skill) to the worktree, re-run the script, and
   start a fresh session. The measured category's tokens and the real first-call
   `cache_creation` should both rise by ~the same amount. Divergence means the script is
   measuring something the runtime does not load, or vice versa.
3. **Remainder.** Start a bare `claude -p` session in a directory with no rules / skills /
   agents / memory. Its first-call `cache_creation` is the system-prompt + built-in-tool
   portion — the unmeasured remainder to subtract in check 1.
