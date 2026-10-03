---
name: coordinate
description: "Be the Orca coordinator from this Claude session while the user is attended: hold the Run, handle each pushed orchestration message (discuss a worker's PLAN gate with the user then reply, land finished work, explain failures), dispatch on request, and hand the Run to an unattended coordinator.sh loop and back with an explicit switch. Use on the main checkout for '/coordinate', '/coordinate auto [--until HH:MM] [--drain]', '/coordinate attended', or when a line 'You have N orchestration messages' arrives."
argument-hint: "[auto [--until HH:MM] [--drain] | attended | status]"
---
# Coordinate

One Run, one holder at a time (Orca fences the other). **Attended**: this session holds the Run; Orca pushes
`You have N orchestration message(s). Run orca orchestration check --run <run>` into it. **Auto**: `coordinator.sh --answer auto`
holds it (answers gates itself, lands, labels `blocked`). The switch is always an explicit user instruction, never inferred from presence.
The session converses, decides and calls the scripts in `.ai-toolkit/scripts/` (`dispatch.sh`, `land.sh`, `review.sh`, `answer.sh`,
`coordinator.sh`); it never writes task code (see `.ai-toolkit/rules/planning-hub.md`) and keeps no state: ids live in the conversation.

## Preconditions

Main checkout on the base branch, inside an Orca terminal (`$ORCA_TERMINAL_HANDLE`), `orca status` ready, `gh auth status` green.

## Bind

```bash
H=$ORCA_TERMINAL_HANDLE
orca orchestration run-current --from $H --json                 # already holding one? after a context reset start here
orca orchestration run-use --id <run> --from $H --json          # take an existing Run (the previous holder is fenced)
orca orchestration run-create --objective "<goal>" --from $H --json   # or a new one
```

Find the id with `orca orchestration run-list --json` or `coordinator.sh --status --run <run>` (it names the holder). Say the id back to the user.

## On each message

Triggered by the pushed `You have N orchestration message(s)` line (or the user asking you to check):

```bash
orca orchestration check --run <run> --terminal $H --json                       # one whole batch + its deliveryId
orca orchestration check --run <run> --terminal $H --ack <deliveryId> --json    # ALWAYS, after handling the whole batch
```

The ack call may return the next batch: handle it the same way. A batch of only `heartbeat` or `status` messages: ack and say nothing.
The issue of a message: its `dispatchId` (payload) in `orca orchestration worker-list --run <run> --json` gives `.resource.worktreeId`
(path after `::`), whose `.linkedIssue` is in `orca worktree list --json`. After a hand-over the Run replays the batch the loop did not
ack. Ack-and-ignore a `worker_done` whose issue is already closed (`gh issue view <n> --json state`) and a question already answered (in
`orca orchestration inbox --full --json` a message of this Run whose `thread_id` is the question's id). A `worker_done` whose issue is still
OPEN while `coordinator.sh --status` says `coordinator.sh` holds the Run means a land is in flight: wait, never land it yourself.

**`question`** (a worker's PLAN gate): show the issue (`gh issue view <n>`, its `Scope:`/`Gate:` footer), the plan, and what bears on it
(the files it touches, the acceptance criteria, conflicts with other live issues). Say what you would answer and why, then discuss. Never approve blindly: no reply before the user decided, unless they said in this conversation to approve a class of plans. Turn their answer, whatever its wording, into exactly ONE of three replies (which one: `afk-answering.md`, Choosing the reply), then comment `Gate answered by the user: <body>` on the issue:

```bash
orca orchestration reply --run <run> --from $H --id <message-id> --body "approve"
orca orchestration reply --run <run> --from $H --id <message-id> --body "approve with: <the change, one sentence>"
orca orchestration reply --run <run> --from $H --id <message-id> --body "revise: <the change, one paragraph>"
```

**`worker_done` succeeded**: run `.ai-toolkit/scripts/land.sh --review <n>` (independent review, CI on the exact tip, fast-forward, close,
release, remove the worktree; it takes minutes) and report by exit code. 0 landed (say the sha). 2 refused (a precondition: say which).
3 review rejected, 4 gate red or timed out, 5 merge conflict: show the `BLOCKER:` lines or the last line, and **offer** the re-dispatch
the loop would do: `RUN=<run> .ai-toolkit/scripts/dispatch.sh --address "address: <blockers or why>" <n>` (a fresh terminal and Task; at
most 2 rounds for a rejected review, 1 for red CI or a conflict; after that propose `blocked`). 6 landed but cleanup incomplete: run
`.ai-toolkit/scripts/land.sh --cleanup-only <n>`, never land again.

**`worker_done` failed / `escalation`**: read the body and the kept worktree (`orca worktree show --worktree issue:<n>`), explain what
happened in two sentences, then propose either `blocked` or one retry with `dispatch.sh --address "<what to do differently>" <n>`.
`blocked` = label, comment, free the slot, keep the worktree for the human:

```bash
gh issue edit <n> --add-label blocked && gh issue comment <n> -b "blocked: <why>"
orca orchestration worker-release --dispatch <dispatch-id> --json
```

## On request

- **Dispatch**: `RUN=<run> .ai-toolkit/scripts/dispatch.sh --next --dry-run` prints the next ready issue (exit 3 = nothing ready); confirm
  it with the user, then `RUN=<run> .ai-toolkit/scripts/dispatch.sh <n>`. To fill up to the cap repeat until exit 3 or
  `orca orchestration worker-list --run <run> --terminal-state active --json` shows `CONCURRENCY_CAP` live workers (default 3).
- **Status**: `.ai-toolkit/scripts/coordinator.sh --status --run <run>` (who holds the Run, live workers, unanswered questions), plus
  `orca worktree ps --json` and `gh issue list --state open --json number,title,labels`. One table: `#n title · state · next step`.

## The switch

Only on the user's explicit word. While the loop holds the Run, do not `check`, `reply` or `dispatch` as consumer: Orca fences or refuses.

### `/coordinate auto [--until HH:MM] [--drain]`

First settle the session's business: handle and ack the current batch, and tell the user that from now on gates are answered by
`answer.sh` under the `afk-answering` rule, not by them (a gate they want to discuss must be answered first). Then:

```bash
date -u +%FT%TZ        # the switch time: say it in your reply, it anchors the summary of what happened while away
orca terminal create --worktree path:$PWD --title coordinator \
  --command "bash .ai-toolkit/scripts/coordinator.sh --run <run> --answer auto [--until HH:MM] [--drain] [--cap N]"
```

The new terminal takes the Run over (`run-use`; without `--run` it creates one) and this session is fenced: stop consuming. Check with `coordinator.sh --status --run <run>`
(`held by: coordinator.sh (auto ...)`); if it never shows, read the terminal (`orca terminal read --terminal <handle>`) and rebind with `run-use`.
`--until` makes the loop exit at that time (live workers keep running; their messages wait in the Run); `--drain` exits when nothing is
ready and no worker is live.

### `/coordinate attended`

```bash
.ai-toolkit/scripts/coordinator.sh --stop --run <run>
```

This terminal takes the Run back (Orca fences the loop, which exits 0 with "taken back by") and the command waits until the loop is gone;
exit 1 = still finishing a step (a land): run it again until it exits 0. Until then you must not check, handle or land anything: the loop
may be mid-land and a second land would race it. Once `--stop` exits 0, `check` the Run (it replays what the loop left unacked) and summarize
what happened while away **from Orca, git and GitHub only** (never from files the loop keeps), since the switch time:

- Landed: `git fetch` then `git log --since=<switch time> --oneline origin/<base>`; `gh issue list --state closed --search "closed:>=<date>"`.
- Blocked: `gh issue list --label blocked --state open --json number,title` with each last `blocked:` comment; kept worktrees in `orca worktree ps --json`.
- Pending questions: `orca orchestration inbox --full --json` (this Run, a `question` with no reply in its thread).
- Still running: `orca orchestration worker-list --run <run> --terminal-state active --json`.

Present blocked issues and pending questions first, then lands, then running work, each with what you propose next.

## Rules of thumb

- A bare `orca orchestration reply` only works from the bound terminal: when the loop holds the Run, a human answers with
  `coordinator.sh --run <run> --reply <message-id> approve`; the loop sends it at its next wake.
- Surface what the user must decide (gates, blocked issues, rejected reviews) before what runs fine; one recommendation, not a menu.
