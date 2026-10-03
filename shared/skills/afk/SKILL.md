---
name: afk
description: "Step away from the backlog: hand the Run to the unattended coordinator loop. Alias of '/coordinate auto'. Use on the main checkout when the user says '/afk <duration>', '/afk until HH:MM', '/afk drain', '/afk off' or '/afk status'."
argument-hint: "[duration | until HH:MM | drain | off | status]"
---
# AFK

`/afk` is `/coordinate auto`: follow the `coordinate` skill (bind the Run, settle the open gates, start `coordinator.sh --answer auto` in an
Orca terminal, stop consuming). State lives in Orca, git and issue labels; nothing is lost when the loop stops.

| Command | Same as |
|---------|---------|
| `/afk <duration>` | `/coordinate auto --until HH:MM` (the clock time that far ahead; `--until` takes `HH:MM` only) |
| `/afk until HH:MM` | `/coordinate auto --until HH:MM` |
| `/afk drain` | `/coordinate auto --drain` (stop when the backlog is empty and no worker is live) |
| `/afk status` | `coordinator.sh --status --run <run>`, plus the survey from `hub` |
| `/afk off` | `/coordinate attended`: take the Run back and summarize what happened while away |

Gates are answered by `answer.sh` under the `afk-answering` rule; blocked issues are labelled, commented, and notified for you to decide
when you are back. `--cap N` (default `CONCURRENCY_CAP`, 3) bounds the concurrent spokes: the shared usage window saturates past ~4.
Never edit the coordinator's scripts while it runs.
