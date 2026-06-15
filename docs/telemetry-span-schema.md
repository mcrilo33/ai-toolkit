# Telemetry span schema (v1)

The workflow-observability dashboard is built on **one append-only event type: a
span**. Every meaningful unit of the hub/spoke workflow — a spoke's whole life, a
solo-cycle step, a hook firing, a worktree script, and (reconstructed later by the
parser) skills, agents, todos, and human waits — is emitted as a span to a single
JSONL log. This document is the **frozen v1 contract**. Issue B (session-log
parser + token/cost correlation) and Issue C (the Streamlit dashboard) build
against it; change it only by versioning, never in place. The one exception is
*additive, optional, pull-only* fields that push emitters never write and that
default to `null` — `summary` (Issue #47) is the first; these extend the contract
without breaking any push producer or existing consumer.

## Where spans live

```
${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl
```

One JSON object per line, append-only. Writing is **opt-in and invisible**:

- Nothing is written unless `AI_TOOLKIT_TELEMETRY=1`. When unset, the emit layer
  is a pure no-op — it creates no file and no directory.
- Emitting never writes to stdout/stderr and never changes the caller's exit
  code. A telemetry failure can never break a hook or a script.

## Privacy contract

**Metadata only.** A span may carry identifiers, names of toolkit constructs,
timings, and statuses — never user content. Concretely:

- `repo` is a **basename**, never a path.
- We never log commands, commit messages, file paths, diffs, or any payload
  content.
- The only field read out of a hook's stdin payload is `session_id`. Nothing
  else from the payload is ever copied into a span.
- `name` / `phase` are toolkit-defined constants supplied by the caller (e.g.
  `worktree-new`, `red`), not values derived from user input.

`tests/unit/test_telemetry_span.py` asserts these guarantees (no path / command /
payload leakage, basename-only repo, opt-in no-op, invisibility).

## The span object

```jsonc
{
  "span_id": "a1b2c3d4e5f6",      // opaque random id for this span
  "parent_id": "…",               // enclosing span; null at the root
                                  // nesting: session -> turn -> skill -> step -> hook
  "spoke_run_id": "feature/21-foo+1700000000",
                                  // ties ALL spans of one spoke across sessions/resumes
  "session_id": "…",              // Claude session id (join key to CC token cost)
  "workflow_rev": "abc1234",      // ai-toolkit short SHA active at emit time (A/B anchor)
  "repo": "ai-toolkit",           // project-root BASENAME, never a path
  "branch": "feature/21-…",       // current branch (null if unavailable)
  "kind": "lifecycle",            // span category — see below
  "name": "worktree-new",         // toolkit construct that emitted it
  "phase": "spawn",               // sub-phase within the kind (null if N/A)
  "ts_start": "2026-06-13T12:00:00Z",
  "ts_end":   "2026-06-13T12:00:03Z",
  "duration_ms": 3000,            // non-negative integer
  "status": "success",            // success|failure|deny|warn|skipped
  "human": null,                  // {type, wait_ms} when a human was waited on
  "summary": null,                // pull-only few-word node label (Issue #47); null on push
  "tokens_in": null,              // \
  "tokens_out": null,             //  } null at emit — filled by Issue B's
  "cost_usd": null                // /  token/cost correlation pass, not here
}
```

### Field reference

| Field | Type | Notes |
|-------|------|-------|
| `span_id` | string | Opaque random id. Always present and unique per span. |
| `parent_id` | string \| null | Enclosing span id; `null` at the root. From `--parent-id` or `$TELEMETRY_PARENT_ID`. |
| `spoke_run_id` | string \| null | Minted at `worktree-new`, written to `<root>/.ai-toolkit/spoke-run-id`. Stable across sessions/resumes. `null` outside a spoke. |
| `session_id` | string \| null | From the Claude hook payload (`$INPUT.session_id`). The join key Issue B uses to attribute per-turn token cost. |
| `workflow_rev` | string \| null | ai-toolkit short SHA at emit time. Resolution order: `$AI_TOOLKIT_WORKFLOW_REV` → synced-target `.ai-toolkit-manifest.json` `toolkit_rev` → ai-toolkit checkout git SHA → `VERSION`. |
| `repo` | string | Project-root **basename**. `unknown` if unresolved. |
| `branch` | string \| null | Current git branch of the project root. |
| `kind` | string | One of `lifecycle, step, hook, script, skill, agent, todo, human, rule`. |
| `name` | string | The toolkit construct: `worktree-new`, `commit-gauntlet`, `solo-cycle`, `tdd-red`, … |
| `phase` | string \| null | Sub-phase: `spawn, land, teardown` (lifecycle); `red, green, review, push` (step); else `null`. |
| `ts_start` / `ts_end` | string | ISO-8601 UTC, second precision. |
| `duration_ms` | integer | `ts_end - ts_start` in ms (≥ 0). `0` when no start clock was supplied. |
| `status` | string | `success, failure, deny, warn, skipped`. |
| `human` | object \| null | `{ "type": "prompt\|question\|approval", "wait_ms": <int> }` when a human interaction was timed; else `null`. |
| `summary` | string \| null | **Additive, pull-only (Issue #47).** A few-word node label the parser derives for display: the todo a step advances, an agent's task `description`, a trimmed prompt/question snippet. `null` on push spans and whenever none resolves; `name` stays the stable grouping key. |
| `tokens_in` / `tokens_out` / `cost_usd` | null | Always `null` at emit. Issue B's correlation pass fills these by joining on `session_id`. |

### `kind` values

| kind | Emitted by (this issue) | Later (parser) |
|------|--------------------------|----------------|
| `lifecycle` | `worktree-new/land/done` (`spawn/land/teardown`) | — |
| `step` | cycle gate scripts (`red/green/review/push`) | — |
| `hook` | every hook invocation (auto, via the hook lib) | — |
| `script` | reserved for other instrumented scripts | — |
| `skill`, `agent`, `todo`, `human`, `rule` | — | reconstructed from CC session logs |

## The three key mechanisms

- **`spoke_run_id`** — minted once at `worktree-new` as `<branch>+<spawn-epoch>`
  and written to `.ai-toolkit/spoke-run-id` in the new worktree. Every hook and
  script emitting inside that worktree reads it, so all spans of one spoke share
  an id even across multiple Claude sessions and resumes.
- **`session_id`** — read from the Claude hook payload. A single spoke run spans
  many sessions; this is the per-session join key the parser uses to attribute
  token cost to whichever span was open.
- **`workflow_rev`** — the ai-toolkit revision active when the span was emitted.
  It anchors the dashboard's A/B "did my workflow change help?" comparison.

## Emitting a span

Source the helper and call `telemetry_emit_span` (defined in
`shared/hooks/lib/telemetry.sh`):

```bash
source "<lib>/telemetry.sh"

# A lifecycle span timed from a captured start clock:
_t0=$(_telemetry_now_ms)
# … do the work …
telemetry_emit_span --kind lifecycle --name worktree-new --phase spawn \
  --status success --start-ms "$_t0"
```

Flags: `--kind` and `--name` are required; `--phase` (null), `--status`
(`success`), `--start-ms` (→ `ts_start`/`duration_ms`), `--parent-id`,
`--span-id`, `--human-type`, `--human-wait-ms` are optional. Unknown flags are
ignored rather than erroring, so a caller can never be broken by a flag the
running lib version does not yet understand.
