---
name: afk
description: "Drain the backlog unattended: run the coordinator loop in an Orca terminal on the main worktree for a bounded window or until the backlog is empty, answering gates automatically or leaving them to the human. Use on the main checkout when stepping away: '/afk <duration>', '/afk until HH:MM', '/afk drain', '/afk off', '/afk status'."
argument-hint: "[duration | until HH:MM | drain | off | status]"
---
# AFK

Unattended work is one foreground process, `coordinator.sh`, bound to one Orca Run. It fills slots with
`dispatch.sh --next`, waits on the inbox, answers gates, lands finished work after the independent review
and green CI, and labels failures `blocked`. State lives in Orca, git, and issue labels; a restart re-binds the Run.

## Start

Run it in a dedicated Orca terminal on the main worktree, never in a spoke:

```bash
orca terminal create --worktree path:$PWD --title coordinator \
  --command ".ai-toolkit/scripts/coordinator.sh --answer auto --until 23:00 --cap 3"
```

| Flag | Meaning |
|------|---------|
| `--answer auto` | `answer.sh` services PLAN gates by the `afk-answering` rule; `--answer human` notifies and waits for `orca orchestration reply` |
| `--until HH:MM` / `+40m` | stop dispatching at that time and finish what is live |
| `--drain` | stop when the backlog is empty and no worker is live |
| `--cap N` | max concurrent spokes (`CONCURRENCY_CAP`, default 3; the shared usage window saturates past ~4) |

`/afk <duration>` means `--until +<duration>`; `/afk drain` means `--drain`. Check preconditions first: Orca
ready, `gh` authed, no other coordinator running (`coordinator.sh --status`), a clean main worktree.

## Status and stop

- `/afk status`: `coordinator.sh --status` plus the survey from `hub`.
- `/afk off`: Ctrl-C the coordinator terminal. Live spokes keep running and their messages wait in the
  inbox; nothing is lost. Restart or answer by hand.

## Rules of thumb

- Blocked issues need a human: the coordinator labels them `blocked`, comments, and sends a desktop notification.
- Never edit the coordinator's scripts while it runs.
