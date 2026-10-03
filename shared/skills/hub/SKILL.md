---
name: hub
description: "Orient a fresh coordinator session and dispatch from it: survey in-flight Orca worktrees, pending questions, open issues, and branches awaiting land, then propose the next move and act only on confirmation. Use on the main checkout when the user says /hub, 'what's in flight', or 'hub status'."
argument-hint: "[optional: issue number to focus on]"
---
# Hub

The hub is the session on the **main checkout**, on the base branch, inside an Orca terminal. It decides,
dispatches, answers, and lands; it never writes task code (see `.ai-toolkit/rules/planning-hub.md`).

## Preconditions

- `git branch --show-current` is the base branch and you are not in a linked worktree. Otherwise you are a
  spoke: follow `solo-cycle` instead.
- `$ORCA_TERMINAL_HANDLE` is set (Orca terminal) and `orca status` reports ready. `gh auth status` is green.
- The lifecycle scripts are in `.ai-toolkit/scripts/` (`sync.sh` put them there).

## 1. Survey (read-only)

```bash
orca worktree ps --json                                   # every worktree: linked issue, agent state working|waiting|done
orca orchestration worker-list --terminal-state active --json   # live workers (liveness live|unverifiable|exited)
orca orchestration inbox --json                           # pending question / worker_done / escalation messages
gh issue list --state open --json number,title,labels     # backlog: priority, hold, blocked labels
.ai-toolkit/scripts/coordinator.sh --status               # is a coordinator loop running?
```

Summarize as one table, one row per issue: `#n title · state · next step`.

| State | Meaning | Next step |
|-------|---------|-----------|
| unstarted | open, not `hold`, blocked-by closed, no worktree | dispatch (`start-task`) |
| working | agent `working` | nothing; do not interrupt |
| parked | a `question` in the inbox (PLAN gate) | answer it (below) |
| done | `worker_done` received, not landed | review + land (`land`) |
| blocked | label `blocked`, worker released | read the escalation, decide |
| abandoned | worktree gone, issue open | re-dispatch or close |

## 2. Propose, then act on confirmation

State the single best next move and why, then wait. Never dispatch, answer, land, or remove a worktree
unconfirmed; those change shared state and are hard to undo.

- **Dispatch**: `.ai-toolkit/scripts/dispatch.sh <n>` or `--next` (picks the next ready issue).
- **Answer a gate** (attended): read the plan in the question, then
  `.ai-toolkit/scripts/coordinator.sh --run <run> --reply <message-id> approve` (or `'revise: <change>'`); a bare `orca orchestration reply` is refused outside the Run's bound terminal.
- **Land**: `.ai-toolkit/scripts/land.sh <n>`.
- **Unattended**: start `coordinator.sh` (`afk` skill) instead of hand-driving each step.
- **Teardown** (abandoned or finished by hand): `orca worktree rm --worktree issue:<n> --run-hooks`.

## Rules of thumb

- One recommendation, not a menu. The coordinator loop and this session are never both consumers of the Run:
  if `coordinator.sh --status` shows a loop, observe and answer through it; do not start a second one.
- A small fix you want to drive interactively: `/quick`. A tiny non-executable change: a subagent with
  `isolation: worktree`, reviewed and merged by you.
- Ambiguous scope stays here: use `brainstorming`, write the issue, then dispatch.
- Surface what the human must decide (blocked issues, `revise` loops) before what is running fine.
