# Workflow observability dashboard

A 100% local Streamlit dashboard that answers, per spoke and in aggregate:
**where does the hub/spoke workflow spend time, tokens, and human interaction —
and did a change to a step actually improve things?**

It is Issue C of the workflow-observability epic. It reads the unified span
dataset (the frozen v1 schema from Issue #21,
[`docs/telemetry-span-schema.md`](../docs/telemetry-span-schema.md)) and, when
available, Issue #22's correlated push+pull dataset with ccusage token/cost
attribution.

## Local-only guarantee

Everything runs on your machine and nothing is ever exported.

- Streamlit binds to `localhost` only (see `run.sh`) and usage-stat gathering is
  off.
- The dashboard surfaces **metrics only** — timings, statuses, token/cost
  numbers, and the names of toolkit constructs. It never reads or shows prompt
  content; the span schema carries none (see the privacy contract in the schema
  doc).

## The views

1. **Spoke** — pick a `spoke_run_id` and drill down a **turn-centric** tree
   (`spoke → step → turn → trace`): Level-1 is the phase-interval bucket, named by
   the **todo it advances** (spawn · RED · GREEN · … as a fallback). Inside, the
   marker shows as a thin header, then one **turn node** per assistant inference
   (its model + token spike), with the **tools/skills it issued nested beneath** —
   each tool labelled by what it acted on (`Read · /path`, `Bash · pytest -q`),
   hooks collapsed under their triggering tool, and an agent's **sub-agent turns
   and tools** under it. A **Meta by kind** tab aggregates the spoke's spans per
   kind (counts + time/cost stats) to spot "launched too much". Cost is counted
   **once per turn** (each turn node owns it), so the rolled-up totals are
   trustworthy. Each turn carries one **`📐 context`** node — drill it to see the
   named input state (rules · `CLAUDE.md` · memory · tool-schemas) with per-item
   tokens and the history remainder, the real cached prefix in the Tokens column.
   When the selected spoke is **still running**, a **Follow live** toggle appears
   that auto-refreshes the tree (~8s) as the transcript grows — no daemon, no
   emission hook; the built tree is cached between refreshes and rebuilt only on
   real growth. Metadata only — labels are short summaries, never prompt bodies.
2. **Aggregate** — pick a time window and roll the tree up across all spokes:
   per-step frequency, totals, and per-invocation mean/median for time and cost.
   Shows where time and tokens actually go.
3. **A/B compare** — pick two `workflow_rev`s and read the per-step delta on
   time, cost, and human interaction, **normalized per invocation**. Every row
   shows its sample sizes `n_a→n_b` and flags low-confidence deltas — small
   spoke counts are noisy, and the view does not imply significance it lacks.
4. **Automatability** — ranks human-interaction points by frequency × low
   decision-variance × on-critical-path, to surface what is worth a closer look.
   It only surfaces candidates; judging true automatability is a later
   LLM-judge follow-up.

## Setup

The dashboard's dependencies are **isolated** from the repo's tooling — install
them into a dedicated virtual environment, never the repo environment.

```bash
python -m venv dashboard/.venv
source dashboard/.venv/bin/activate
pip install -r dashboard/requirements.txt
```

### Record spans

Span recording is opt-in and invisible. Nothing is written unless telemetry is
enabled:

```bash
export AI_TOOLKIT_TELEMETRY=1
```

Spans then append to
`${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl`.

### ccusage (token cost)

Token/cost numbers are reconciled from [ccusage](https://github.com/ryoppippi/ccusage),
an external Node tool invoked as `npx ccusage` by Issue #22's correlation pass.
It is **not** a Python dependency and is not in `requirements.txt`. If ccusage is
absent the dashboard still runs; cost columns simply read blank. The dashboard
reuses ccusage's numbers as-is — it never re-derives cost.

## Run

```bash
dashboard/run.sh                 # read the default span log
dashboard/run.sh path/to/events.jsonl
```

`run.sh` picks the first free port in a dedicated range (default `8600`-`8699`),
so it never lands on hex admin's Streamlit on the default `8501` — a collision
there makes the browser show the wrong app. Override the range with
`AI_TOOLKIT_DASHBOARD_PORT_MIN` / `AI_TOOLKIT_DASHBOARD_PORT_MAX`, or set
`STREAMLIT_SERVER_PORT` to pin one port (it wins over the range). If the whole
range is busy, it falls back to a random free ephemeral port and prints a note.

### Span source

The sidebar chooses the data source:

- **Raw push-span log** (default) — reads `events.jsonl` directly: lifecycle,
  cycle-step, and hook spans. No session-log pull spans, no cost correlation.
- **Correlate via Issue #22** — reads the **persisted store** (Issue #62): a
  `store.duckdb` materialized from the session logs once and updated incrementally,
  so opens are ~instant instead of re-parsing 252 MB every time. This adds
  skill/agent/todo/human spans and per-span cost.

> [!NOTE]
> **No historical backfill (Issue #62).** The persisted store
> (`~/.ai-toolkit/telemetry/store.duckdb`) is created **empty at a watermark** — its
> init time — and only ingests sessions that run **after** it. Spokes that ran before
> the store existed are **intentionally absent**; the dashboard populates as new spokes
> run. A new spoke appears after it runs and you re-open the dashboard (it ingests only
> that spoke's delta — seconds). To rebuild, delete `store.duckdb`: it re-initialises
> empty at a new watermark — it does **not** re-parse history. See
> [`docs/telemetry-pull-layer.md`](../docs/telemetry-pull-layer.md#persisted-store-issue-62-phase-a).

Environment variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `AI_TOOLKIT_TELEMETRY` | Set to `1` to record spans at all | unset (no-op) |
| `AI_TOOLKIT_SPAN_LOG` | Explicit path to the span log | — |
| `AI_TOOLKIT_TELEMETRY_DIR` | Directory holding `events.jsonl` | `~/.ai-toolkit/telemetry` |
| `AI_TOOLKIT_PROJECTS_DIR` | Claude session-logs root (correlated mode) | `~/.claude/projects` |

> [!NOTE]
> The upstream per-span cost is hierarchical: a `step` span's cost already
> includes the skill/agent spans nested inside it. The aggregate and A/B views
> group same-granularity spans, so they never double-count. The v2 Spoke view
> does **not** use that overlapping per-span cost — it attributes each turn's
> cost to exactly one **turn node** and rolls it up, so its subtree totals are an
> additive, trustworthy ledger. A main-agent turn node sits under its reconstructed
> **phase interval** (the `step`/`lifecycle` marker spine), a subagent turn node
> under its `agent` span; a main turn off the lifecycle envelope (or with an
> unparseable timestamp) surfaces in an "(unresolved)" row.

## Tests

The view and aggregation logic lives in `queries.py` and is unit-tested against
fixtures, independent of Streamlit:

```bash
pytest tests/unit/test_dashboard_*.py
```

`test_dashboard_telemetry_wiring.py` additionally drives the query layer over
Issue #22's real correlated dataset to prove the `from_telemetry` seam.
