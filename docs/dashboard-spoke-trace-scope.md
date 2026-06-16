# Dashboard spoke-trace scope (v3)

The spec for the v3 spoke view: a single, sequential, fully-drillable trace of a spoke
run — every run-script, agent, sub-agent, todo, hook, and loaded-context item — so a
reader can follow what happened end to end and spot ways to improve it. Builds on the
4-level IA from #46 (`spoke → step → cycle → trace`) and the pull layer from #47.

Guiding principle: **nothing is filtered.** Anything that took time, cost tokens, or
triggered something (or was loaded but did nothing) appears as a row. The only
compromises are *grouping* (reversible drill, never omission) and rendering idle time as
a divider rather than a phase.

## Motivating defects (found auditing `feature/47`)

1. **Phantom first step.** The `setup`/spawn bucket is renamed by the latest in-progress
   todo, so it masquerades as `S1·RED` and the spawn step is invisible
   (`_bucket_todo_label` overrides the `setup` label in `queries.py`). Regresses #46's
   honest-coarse `setup` guarantee.
2. **Todo shown twice, one role wrong.** The same todo span names its L1 bucket *and*
   renders as an L3 leaf. The leaf is correct (a real ledger event); the relabel is not.
   A pure ledger-*creation* write has no summary, so it renders as a bare `todo`.
3. **`(unresolved)` subagent turns.** The S3·DESIGN fan-out ran a **Workflow**; its
   agents live at `subagents/workflows/wf_*/agent-*.jsonl` (one level deeper than the
   parser's `subagents/agent-*.jsonl`), so no `agent` span brackets them, and subagent
   turns — unlike main turns — have no phase-interval fallback, so they orphan even though
   their timestamps sit inside the S3·DESIGN interval.

## The model

### Levels

- **L0 `spoke`** — the lifecycle envelope (`spawn … teardown`).
- **L1 step** — one marker = one step, in chronological order (the spine).
- **L2 action** — turns (one inference each), run-scripts, agents, workflows, human
  interactions, approvals, todos.
- **L3 trace** — tools, skills, hooks, reasoning, sub-turns, sidecar sessions.

Depth is unbounded: containment recurses (agent→agent→agent, workflow→phase→agent→turn→tool).

### Node kinds

Real spans: `lifecycle`, `step`, `hook`, `script`, `tool`, `skill`, `agent`, `todo`,
`human`, `rule`, plus new `workflow`, `workflow_phase`, `approval`.

Synthetic display nodes (built at query time, never spans): `interval` (phase bucket),
`turn`, `hooks` (collapsed group), `reasoning`, `context`, `gap` (idle), `session`
(resume), scope-band (a soft skill/rule), `unresolved`.

### Nesting semantics

- **Hard** — the parent's `[ts_start, ts_end]` truly contains the child (agent→sub-agent,
  workflow→phase→agent, tool→hook, approval→tool). Attribute by window / `parent_id` /
  chained `agent_links`.
- **Soft** — causal "active scope," no containing window (a skill loads instructions then
  guides later turns; a rule). Rendered as a `[scope]`-tagged band holding the turns it
  influenced; the band carries only its **load** cost, and its scope ends at the next
  skill-load or step-end (inferred, tagged).
- **Emission** — the child is *produced by* the parent (a gate script emits a `step`
  marker). Structural link, not time-containment.
- **Sidecar** — the parent shells out to a separate Claude session (`claude -p`,
  LLM-judge hook); a cross-session subtree linked via `sidecar_session`.

## View spec

### Columns

`Node` · `Time` (start clock, HH:MM:SS) · `Dur` · `Cost` · `Tokens` · `H` (human count) ·
`Actor`. No Date column; a thin date-divider row marks day rollover. `Actor` is `main`,
the **sub-agent name** (`Explore`, `code-review`, …), `workflow`, `script`, `hooks`, or
`sidecar`.

### Tree behaviour

- **Every marker is its own step**, including near-empty churn (no coalescing).
- **Spawn / first-RED split**: setup + planning + ledger creation stay in `spawn`; the
  first real phase becomes its own step, split at the first `in_progress` todo transition.
- **No-marker step synthesis**: a phase that emitted no marker is synthesized from its
  todo `in_progress` transition and badged `⟨from todo — no marker⟩`.
- **`×N` groups** (`hooks ×9`, `turns ×7`, `agent ×3 parallel`) collapse high-cardinality
  *leaf* siblings; drilling shows each member with its own metrics. Collapse never omits.
- **Status rollup**: a container's status is the worst status among its children.
- **Idle** renders as a divider, not a phase. **Session resume** renders as a divider with
  the cold-cache (`cache_creation`) note. **Cache-bust** (e.g. rules edited + re-synced)
  renders as an inline event.
- **Deny/blocked**: a denied approval or hook-blocked tool shows the tool as never-run.
- **Uninstrumented subprocess** (xdist workers, git subprocesses under `Bash(pytest)`) is
  noted as a traced boundary, so absence reads as "not traced," not "nothing happened."

### Drill panels

- **`×N` → per-item rows** (each rule/memory/hook/agent with its own metrics).
- **`turn` → context-composition** bar: exact `usage` totals (in/out/cache_read/
  cache_creation) with a **modeled** prefix/skills/memory/history split (labelled estimate).

### Loaded context (two surfaces)

- **Flow**: context loads once under `spawn` (`rule ×N`, `CLAUDE.md`, `memory ×N`,
  `tool-schemas ×N`), plus `ctx-bust` and `session-resume` events where they occur.
- **Composition**: the per-turn panel above.
- **Cold-context lens**: a rollup of context loaded but never exercised (rules, tool
  schemas, memory recalls) — the trimming/automation candidates.

## Per-view behaviour of new kinds

Only `script`, `workflow`, `approval` are new spans (synthetics never reach rollups).

| Kind | Meta / Aggregate / A-B | Automatability | Cost |
|------|------------------------|----------------|------|
| `script` | own time+freq row | — | $0 |
| `workflow` | own count+time row | — | $0 (cost on its `agent` children) |
| `workflow_phase` | excluded (display-only) | — | — |
| `approval` | count + mean-wait row | **primary home** (deny/ask/allow + wait) | $0 |

## Cross-cutting requirements

- **Per-spoke lazy build**: startup loads only the spoke index for the selectbox; the
  selected spoke's tree is correlated + built on demand, memoized on
  `(spoke_id, log-mtime)`. Interaction toggles cached rows only — never rebuilds.
- **Conservation**: `Σ owned == Σ turns`; no double-count under any re-parenting
  (`workflow`/`phase`/scope-band/`sidecar`). Cost lives only on `turn`/`agent` leaves.
- **Status rollup**: worst-child propagation, defined once.

## Decisions log

1. Every marker = its own step (no coalescing).
2. Setup/creation stays in `spawn`; the DoD phase is its own step (split at first
   `in_progress` todo); no-marker phases synthesized + badged.
3. Idle = divider row.
4. Render all depth eagerly; collapse only wide leaf groups; build only the selected
   spoke; cache the built tree.
5. Skills render as soft `[scope]`-bands (option A) holding the turns they influenced.

## Consciously out of scope (approximations, labelled in the UI)

- Uninstrumented subprocess tree under a `Bash` (would need per-process instrumentation).
- The context composition split is modeled from artifact byte-sizes; only in/out/cache
  totals are exact (exactness needs cache breakpoints + token-counting API).
- A soft skill's `[scope]` end is inferred (next skill-load / step-end), not measured.
