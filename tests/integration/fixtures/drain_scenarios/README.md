# Drain-simulation scenarios (issue #314)

Each `*.yaml` here is ONE scenario the fixed harness in
`tests/integration/test_drain_simulation.py` interprets. Adding coverage means
adding a file — never editing the harness or the invariants (AC4). The harness
auto-discovers every `*.yaml` in this directory and parametrizes two tests over it:
the REAL run must satisfy every invariant, and the `mutation` must redden exactly its
one named invariant.

## Why this exists

Every afk false-fire was found by watching production for hours. This harness
exercises the drain LOOP (the real `hub-afk.sh --once` tick) against a fake clock and
scripted mock spokes, and asserts PRINCIPLES — not steps — over one explicit,
append-only contract surface: the #300 transition log (spoke lifecycle). It never
reads epochs, pane text, private functions, or log lines. A scenario changes only when a
principle changes; a refactor of how state is COMPUTED never touches it.

## Schema

```yaml
id: <unique-id>                 # test id
stresses:                       # documentation only
  incident: <number>            # the historical incident this pins
  invariant: <I2|I4|I5>         # the invariant it stresses
  principle: "<n[,m]>"          # AFK Design Principle number(s)
spokes:
  - issue: <number>             # a mock spoke = a git worktree on feature/<issue>-<slug>
    slug: <name>
    truth:                      # scenario-declared ground truth the invariants read
      agent: dead               # I5: the agent (claude) is gone
      recovers: true            # I5: a dead agent must be recovered
      advances: true            # I4: an answered park must advance
    initial:
      agent_alive: true         # ps shows a claude descendant (default true)
timeline:                       # steps, sorted by fake-clock `t` (seconds from t0)
  - { t: 0,   spoke: <n>, do: <verb> }
  - { t: 700, run: [drain] }    # run a REAL hub-afk.sh --once at AFK_NOW=t0+t
expect:
  violations: []                # invariant ids the REAL run should report (normally none)
mutation:                       # AC5 negative control (optional)
  drop: [<transition/event>]    # withhold a scripted record (reintroduce inference)
  env: { KEY: value }           # a seam override (e.g. AFK_SIM_PS_FORCE_ALIVE: "1")
  expect_violation: <I2|I4|I5>  # the one invariant the mutation must redden
```

## Timeline verbs (`do`)

| verb | effect |
|------|--------|
| `park` | gate/<issue> tag + pane dialog + `parked` transition + park-onset epoch |
| `answer` | the drain's service: a journal entry + `answer_delivered` |
| `push` | `pushing` then `pushed` transitions |
| `ready` | `ready/<issue>` tag + `ready` transition |
| `commit` | advance the spoke's branch tip |
| `kill_agent` | the agent dies; its launcher zsh keeps the pane alive (#301) |

## Scenario-authoring rules

- **The fake clock (`AFK_NOW = t0 + t`) governs READS, not writes.** Scripted records
  (`world.tlog`/`event`, the `park`/`push`/... verbs) are stamped with a
  fake `ts = t0 + t`. But records the REAL drain writes use real `date +%s`.
  So do not assert a **time-sensitive** property (an age, an ordering by `ts`) over a
  transition the drain WROTE during a `run: [drain]` step — only over scripted
  transitions. Presence/membership assertions over drain-written records are fine
  (I2/I5 do exactly that). This holds because scenario `t` values (hundreds of
  seconds) dwarf a test's real wall-clock (a few seconds), so the fake clock
  dominates every age the drain computes.

## Invariants (declared once in the harness, mirror the AFK Design Principles)

- **I2** a `pushed`+`ready` spoke eventually reaches `landed`/`reaped` (#299)
- **I4** an answered park advances (a forward transition) (#312)
- **I5** a dead agent is never injected — it is recovered (#301)
