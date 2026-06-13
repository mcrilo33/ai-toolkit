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

1. **Spoke** — pick a `spoke_run_id` and drill down the step/sub-step tree
   (lifecycle → cycle phase → hook), each node showing time, token cost, status,
   and human interactions, with per-subtree rollups.
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

### Span source

The sidebar chooses the data source:

- **Raw push-span log** (default) — reads `events.jsonl` directly: lifecycle,
  cycle-step, and hook spans. No session-log pull spans, no cost correlation.
- **Correlate via Issue #22** — calls `telemetry.queries.connect` to parse
  Claude session logs, join the push spans, and ccusage-attribute tokens/cost.
  This adds skill/agent/todo/human spans and per-span cost.

Environment variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `AI_TOOLKIT_TELEMETRY` | Set to `1` to record spans at all | unset (no-op) |
| `AI_TOOLKIT_SPAN_LOG` | Explicit path to the span log | — |
| `AI_TOOLKIT_TELEMETRY_DIR` | Directory holding `events.jsonl` | `~/.ai-toolkit/telemetry` |
| `AI_TOOLKIT_PROJECTS_DIR` | Claude session-logs root (correlated mode) | `~/.claude/projects` |

> [!NOTE]
> Spans are hierarchical: a `step` span's cost already includes the skill/agent
> spans nested inside it. The aggregate and A/B views group same-granularity
> spans, so they never double-count. The Spoke view's per-subtree rollup does
> sum a node with its descendants — read it as a rough subtree total, not an
> additive cost ledger.

## Tests

The view and aggregation logic lives in `queries.py` and is unit-tested against
fixtures, independent of Streamlit:

```bash
pytest tests/unit/test_dashboard_*.py
```

`test_dashboard_telemetry_wiring.py` additionally drives the query layer over
Issue #22's real correlated dataset to prove the `from_telemetry` seam.
