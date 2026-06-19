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

### Caveats

- **60KB truncation.** Request bodies over ~60KB hit Claude Code's body cap and arrive as
  truncated, invalid JSON. The bridge falls back to storing the raw partial text as the
  observation input, so the request start is still visible rather than dropped.
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
