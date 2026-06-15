# Follow-ups from #44 (dashboard: correlate-by-default + date-ordered spoke runs)

These surfaced while reviewing #44 against real telemetry. **None are in #44's scope**
(which was `dashboard/app.py` + `dashboard/queries.py` + tests, three changes). They're
captured here for the hub to triage into GitHub issues. Ordered by value.

---

## 1. fix(dashboard): turn→span attribution mis-assigns LLM cost/tokens to hooks; large untracked bucket

### Problem

The dashboard's turn→span attribution mis-assigns LLM cost/tokens, two ways.

**1a. Hooks wrongly own LLM turns.** Guard/gate shell scripts — which do no model work
— are credited with real tokens/cost/model. Measured on real spokes (ccusage skipped,
so cost shown as $0):

| Spoke | hook spans owning LLM tokens | stolen tokens |
|---|---|---|
| `feature/44-feat-dashboard-correlate-by` | 23 | 38,376 (~36% of all *attributed* tokens) |
| `fix/43-fix-worktree-done-fold` | 14 | 9,482 |

Worst offenders: `quality-gate.sh` (7,028 tok), `delegation-gate-warn.sh` (6,280),
`hub-guard.sh` (4,855), `block-no-verify.sh`, `rm-scope-guard.sh`.

**1b. Large `(untracked turns)` bucket.** 59–83% of a spoke's tokens land in the synthetic
untracked node because they fall inside no span window.

### Root cause

`_turn_owner` (`dashboard/queries.py`) attributes each turn to the *smallest* span whose
`[ts_start, ts_end]` window contains the turn's timestamp. **Push-span timestamps are
truncated to whole seconds**, so a 40 ms hook gets an *effective* ~1-second window:

```
hub-guard.sh   dur=40ms   window=[2026-06-15T06:14:58Z .. 2026-06-15T06:14:59Z]   tokens=1,035 (opus-4-8)
```

Being narrower than the enclosing multi-second `step`, the hook wins ownership. Turns
inside no window → `untracked`.

### Proposed fix

- Exclude `hook`/`hooks` (instantaneous, non-LLM) kinds from being eligible turn owners
  in `_turn_owner` — turns should only attach to `step`/`skill`/`agent`/`lifecycle` spans.
  Removes bogus hook attributions and moves those turns to their correct enclosing step
  (shrinking `untracked`).
- Consider sub-second push-span timestamps (or widening real step/lifecycle windows) to
  further reduce `untracked`.

### Scope

`dashboard/queries.py` (+ tests); timestamp-precision part may touch `scripts/telemetry/`.

### Acceptance criteria

- [ ] Hook spans never own LLM turns (cost/tokens/model).
- [ ] Stolen-token total drops to ~0; `untracked` share decreases.
- [ ] Unit tests cover hook-exclusion and the reduced untracked bucket.

---

## 2. feat(dashboard): show span start time (not just duration) in spoke drill-down

### Problem

The drill-down **Time** column shows only wall-clock *duration*
(`_format_secs(duration_ms)` → "0.5s"). No way to see *when* a step happened.

### Proposal

Show the span's actual start time alongside (or instead of) duration, e.g.
`09:07:18 · 0.5s`. Every node already carries `ts_start` / `ts_end`. Caveat: push-span
timestamps are second-precision.

### Scope

- `dashboard/queries.py` — `format_step_metrics` (add a `started`/`time_of_day` field).
- `dashboard/app.py` — `_node_row` / `_STEP_HEADERS`.
- tests.

### Acceptance criteria

- [ ] Each drill-down row shows the span's start time.
- [ ] Duration still available.
- [ ] Tests cover formatting incl. missing/malformed `ts_start`.

---

## 3. feat(dashboard+telemetry): attribute hooks to main vs sub-agent + their triggering command

### Problem

A hook fires around a command from the main agent or a sub-agent; the dashboard should
show that. Today it doesn't, two ways.

**3a. main vs sub-agent is mislabeled.** Time-nesting already places a hook that fired
inside a sub-agent's window *under* that `agent` node, but the **Agent** column is
hardcoded:

```python
# dashboard/queries.py, _step_node
"agent": "subagent" if row["kind"] == "agent" else "main",
```

So every hook reads `main`, even inside a sub-agent. The tree position knows; the label
doesn't.

**3b. "which command triggered this hook" is not captured at all.** No `tool`/`command`
span exists — kinds are `hook / todo / human / agent / skill / lifecycle / step`. A
hook's `name` is the script (`hub-guard.sh`), not the triggering tool. Needs upstream
telemetry to emit a tool/per-action span (or stamp the triggering tool onto hook spans).

### Proposal

- **3a (dashboard-only):** derive a hook's agent context from its nearest `agent`
  ancestor in the nested tree; label `subagent`/`main` accordingly. (v3 roadmap item D.)
- **3b (upstream):** emit a tool/per-action span, or add a triggering-tool field to hook
  spans, in the push schema (#21) + parser (#22), then surface it. (v3 roadmap item C.)

### Scope

- 3a: `dashboard/queries.py` (+ tests).
- 3b: `scripts/telemetry/` + span schema + dashboard display (larger; depends on schema).

### Acceptance criteria

- [ ] Hooks run inside a sub-agent display `subagent`, not `main`.
- [ ] (Stretch / 3b) each hook shows the command/tool it fired around.
- [ ] Tests cover agent-context derivation.

---

## 4. feat(dashboard): summarize human prompts in a few words (privacy decision)

### Problem

Human interactions are labeled only by type (`approval · solo-cycle · green`) — too
generic to tell *what the prompt was about*. Want a few-word summary.

### Feasibility & the privacy decision

The data exists on-machine: session logs contain the text (`user` / `last-prompt`
records), and `scripts/telemetry/session_parser.py::_human_prompt_spans` already
pinpoints the records that become human spans.

**But this crosses a deliberate invariant** — "parse on-machine, emit metrics only, never
prompt content" (stated in `scripts/telemetry/__init__.py`, `dashboard/app.py`,
`dashboard/queries.py`). This softens it to "surfaces an on-machine-derived *short
summary*." Data never leaves the machine, but it's a conscious change. **Decision needed
before implementing.**

### Two flavors

- **Heuristic (no LLM):** first ~8 words / the `AskUserQuestion` question text, truncated.
  Deterministic, but still literal (shortened) prompt content.
- **LLM topic:** a genuine few-word summary via a local/cheap model call per interaction.

### Scope

- `scripts/telemetry/session_parser.py` (derive summary where the human span is minted).
- Span schema: add e.g. `human_summary` to `_COLUMNS` in **both**
  `scripts/telemetry/queries.py` and `dashboard/queries.py`.
- `dashboard/` display (human nodes + `_interaction_label`).
- tests.

### Acceptance criteria

- [ ] Privacy stance decided and documented.
- [ ] Each human interaction shows a few-word topic summary.
- [ ] Tests cover summary derivation + the empty/missing case.
