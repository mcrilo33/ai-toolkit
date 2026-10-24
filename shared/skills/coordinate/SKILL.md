---
name: coordinate
description: "Run the Orca coordinator from this Claude session, in one of two modes: 'attended' (the coordinator.sh loop does the routine work out of sight and queues every real decision for the user, this session presents them one at a time and rings only for a decision it has just written) or 'auto' (the loop answers everything itself, for when the user is away). Use on the main checkout for '/coordinate', '/coordinate attended', '/coordinate auto [--until HH:MM] [--drain]', or when the user asks what needs them."
argument-hint: "[attended | auto [--until HH:MM] [--drain] | status]"
---
# Coordinate

One Run, and the loop (`coordinator.sh`, in its own Orca terminal) always holds it and does the routine work: dispatch to the cap, land, retry, block, route leftovers. Two modes, switched only on the user's explicit word, never inferred:

- **attended**: the loop answers only what is routine (approves a routine plan; `afk-answering`) and **queues everything else for the user**: a plan it handed over, every permission request (the loop never approves one), a blocked issue. This session only presents that queue.
- **auto**: the loop answers every plan itself, denies every permission request and labels `blocked`; the user decides when back.

This session never binds the Run, `check`s or reads the inbox, never writes task code (`.ai-toolkit/rules/planning-hub.md`), keeps no state: ids live in the conversation. Scripts are in `.ai-toolkit/scripts/` (`coordinator.sh`, `dispatch.sh`, `land.sh`).

## Preconditions

Main checkout on the base branch, `orca status` ready, `gh auth status` green, and either an Orca terminal (`$ORCA_TERMINAL_HANDLE` set) or an Orca chat session (unset: no terminal handle, no tty).

This session has the toolkit guards: `[ -f "${AI_TOOLKIT_DIR:-/nonexistent}/claude-settings.json" ]` in Bash (`bin/claude-spoke` exports `AI_TOOLKIT_DIR`). If not, say plainly "no toolkit guards in this session: it was not started through bin/claude-spoke", give the fix (Orca > Settings > Agents > Claude command = `<toolkit>/bin/claude-spoke`, then restart this session) and stop.

## Start

```bash
# attended: the loop types one line into THIS session's terminal when a decision arrives
orca terminal create --worktree path:$PWD --title coordinator \
  --command "bash .ai-toolkit/scripts/coordinator.sh [--run <run>] --answer attended --session $ORCA_TERMINAL_HANDLE [--cap N]"
# attended, chat session: no --session (its warning is expected), then arm the watcher: ONE Bash call with run_in_background and timeout: 7200000 (the background maximum)
orca terminal create --worktree path:$PWD --title coordinator --command "bash .ai-toolkit/scripts/coordinator.sh [--run <run>] --answer attended [--cap N]"
.ai-toolkit/scripts/coordinator.sh --run <run> --wait [--seen '<ids still waiting>']   # exits 0 printing the id of a new decision; polls every COORD_WATCH_SEC (5) s
# auto
date -u +%FT%TZ        # the switch time: say it, it anchors the summary of what happened while away
orca terminal create --worktree path:$PWD --title coordinator \
  --command "bash .ai-toolkit/scripts/coordinator.sh --run <run> --answer auto [--until HH:MM] [--drain] [--cap N]"
```

Without `--run` the loop creates a Run: find it with `orca orchestration run-list --json`, say it back. It takes the Run over (`run-use`), so no other terminal consumes: check with `coordinator.sh --status --run <run>`
(`held by: coordinator.sh (attended ...)`); if it never shows, read the terminal (`orca terminal read --terminal <handle>`). `--until` stops the loop at that time (live workers keep running, their messages wait), `--drain` when nothing is ready and no worker is live.
Before switching to auto tell the user that gates are then answered by `answer.sh` under `afk-answering`, not by them.

## The queue (attended)

**The bell, the one rule** (terminal session; a chat session: see below). The loop never rings. It types one line into this session (`[coordinator loop] new decision waiting: <plan | permission request | blocked issue | finished issue held> <id>`) when a decision arrives on an empty queue, and for every permission request. On that line: read the item whole
(a plan or permission request: `coordinator.sh --run <run> --show <id>`; a blocked or held issue: `--status` and `gh issue view`), present it with one recommendation (below), and only once that is on screen ring: `printf '\a' > /dev/$(ps -o tty= -p $PPID | tr -d ' ')` (the agent has no `/dev/tty`: ask ps). Right after the bell send one `PushNotification` (when the tool is available) naming the item, always, since you cannot tell whether the user is away: `<kind> <id> on #<n>: <one-line ask>`, a permission request adding `expires in 9 minutes`. Ring and notify at no other time.
If you are in the middle of another decision with the user, hold the new item, no bell, no notification, until the current one is answered; a permission request is the exception (it expires in 9 minutes, and an expired one can come back, up to 3 requests in total): write it at once, ahead of the current one, and ring and notify. After relaying any answer run `--status` and present the next item: the loop sends no line for what waited behind another.
**Chat session.** The background `--wait` exiting replaces the typed line (it wakes for a plan the loop handed over, a permission request, a blocked issue or a refused dispatch request, by the same rule): read the printed id as above, then present it in two turns, because Orca folds the text written before a tool call into a collapsed block and shows only a turn's final text. Turn 1: every tool call first (re-arm `--wait`, the `PushNotification` below, a background `sleep 3` whose completion starts turn 2), then the decision as the turn's final text, no tool call after it, as structured markdown in two levels. Turn 2: always an `AskUserQuestion` card alone, no text before it, with options matching that decision's replies (plan: approve / approve with / revise; permission request: allow / deny; blocked issue: guidance / hold / close; finished issue held: land now / keep held; refused dispatch request: guidance for another try / cancel). The two levels: a **quick decision** at the top (2-3 lines: what it is about, the exact choice, the recommendation and its reason; enough to answer without reading further), then compact **technical detail** (files and mechanism, cost and growth, risks, why it reached the user), as bullets or a small table or diagram through Orca's inline chat visuals when the decision is about a structure or a flow (a one-line choice needs none), readable in about a minute, more depth only on request. The card holds only the question, short but identifiable on its own: `#<n>: <choice>? (advice: <answer>)`; each option's description says what that answer does; set no `preview` (Orca does not render it). The card is the alert (Orca raises its sound when the card appears): no sound command of its own. The `PushNotification` is the same line as above. For a permission request keep turn 1 short, so the card shows well inside its 9 minutes. A permission request that wakes the watcher while a card is open waits for that card (it holds the turn): present it first once the card is answered; if it expired meanwhile it can come back (up to 3 requests, above). A watcher that ends with no printed id (stopped at its timeout) is no decision: re-arm it with the same `--seen`, present nothing, no `PushNotification`. Re-arm `--wait --seen '<ids still waiting>'` in turn 1, after relaying each decision and in any turn in which it is not running, always before that turn's final text.

When the user arrives, or asks what needs them, `coordinator.sh --status --run <run>` is the queue in the order to present it: open questions oldest first, then blocked issues, then dispatch requests. Present **one decision at a time**, stop for the answer, relay it,
then the next; when it is empty, say so. A `being answered by the loop: <id>` line after the queue is not a decision and is not presented (the loop is still answering that plan; if it stays there, the answerer is stuck: say so, nothing more). Never show a heartbeat, a land, a retry or any other routine event; report what landed only when asked (below). Each item: the issue (`gh issue view <n>`, its `Scope:`/`Gate:` footer), what the
loop shows, what bears on it (the files, the acceptance criteria, conflicts with other live issues), and **one recommendation with its reason**, not a menu. The user's answer is relayed with `--reply`, which queues it for the loop's next wake (the loop sends it and comments the issue):

```bash
.ai-toolkit/scripts/coordinator.sh --run <run> --reply <message-id> approve
.ai-toolkit/scripts/coordinator.sh --run <run> --reply <message-id> 'approve with: <the change, one sentence>'
.ai-toolkit/scripts/coordinator.sh --run <run> --reply <message-id> 'revise: <the change, one paragraph>'
```

- **A plan the answerer handed over** (the issue comment says `human: <reason>`): show the plan and that reason, then say what you would answer. Never approve blindly: no reply before the user decided, unless they said in this conversation to approve a class of plans.
  Turn their answer, whatever its wording, into exactly ONE of the three replies (which one: `afk-answering`, Choosing the reply). An answer that changes what the issue asks goes into the issue body (or a comment) first: the issue is the contract.
- **`PERMISSION REQUEST`** (not a plan gate: a worker's permission prompt; it waits at most 9 minutes and then its relay denies itself, so ignore a stale one): show the tool, the whole command, the cwd and the `reason`; reply `--reply <id> allow` or `deny` only on the user's decision
  in this conversation (a standing "allow this class" counts only if they said it). The issue comment names the tool and the answer only, never the command. A `deny` refuses one command, not the land: for a `worker_done` the relay tells the worker not to resend it, but the loop lands the next one it gets. To hold a finished issue, add `hold` (`gh issue edit <n> --add-label hold`) before the reply.
- **A finished issue held** (`finished issue held <n>`: the `hold` label stopped its land): propose land now (remove `hold`, then `--dispatch <n> 'address: send worker_done again'`) or keep it held.
- **A blocked issue** (`blocked: <why>`, worktree kept: `orca worktree show --worktree issue:<n>`): explain in two sentences, then propose one of: guidance for another try (`--dispatch <n> '<message>'`), `hold` (parked on purpose, leaves the queue), or close it.

## Requests

The loop holds the Run, so this session neither dispatches nor lands itself: it asks the loop.

- **What is running / status**: `coordinator.sh --status --run <run>` (holder, live workers and how long each is silent, the queue, dispatch requests with their reason), plus `gh issue list --state open --json number,title,labels`.
  A worker silent past 15 minutes (`COORD_IDLE_MIN`, no output or heartbeat, no open question) is relaunched once, then blocked by the loop; `observation.agentWait` in `orca orchestration worker-show --dispatch <id>` set means a prompt only the user can answer: tell them.
- **Dispatch this issue**: `coordinator.sh --run <run> --dispatch <n> ['<message>']` queues it; the loop starts it at its next wake ahead of its own pick, within the cap (`CONCURRENCY_CAP`, 3) and the Scope rule. A kept worktree is re-dispatched
  with the message (`--address`; it loses its `blocked` label once started), a new one is dispatched plainly (a fresh worker reads only the issue body: put guidance there, the request is refused with a message). A request that cannot start (on hold, blocked with no message, a Scope overlap, no free slot,
  an unreadable issue, a failed dispatch) stays in `--status` with its reason; `--dispatch <n> --cancel` withdraws it. A plain request for a closed or already running issue is dropped as met. A request *with* a message behind a live worker is kept, its reason naming the dispatch (`a worker is still live on the worktree (dispatch <id>)`), and starts once that worker ends.
  To replace a live worker (another model, new guidance): queue `--dispatch <n> '<message>'`, then `orca orchestration worker-stop --dispatch <id>`; when its result says `stop_unknown`, or `worker-list` still shows the dispatch as `dispatched`, run `orca orchestration worker-abandon --dispatch <id>` (a stop alone never settles it). The loop then re-dispatches the kept worktree and closes the old agent terminal; a reply still queued for that worker's permission request is dropped. Free slots are otherwise filled with the next ready issue by the loop.
- **Land this**: the loop lands each finished worker itself (`land.sh --review`: independent review, CI on the exact tip, fast-forward, close). One that cannot be landed (rejected review after 2 rounds, red CI, a conflict) arrives as a blocked item. The `hold` label is the user's "do not land yet": a finished issue carrying it is not landed (the loop logs it, comments the way back and keeps the worktree). To land it later, remove `hold`, then `--dispatch <n> 'address: send worker_done again'`.
- **What landed**: from Orca, git and GitHub only, never from files the loop keeps: `git fetch` then `git log --since=<switch time> --oneline origin/<base>`; `gh issue list --state closed --search "closed:>=<date>"`;
  `gh issue list --label blocked --state open --json number,title`; `orca orchestration worker-list --run <run> --terminal-state active --json`.

## Switching

Only on the user's explicit word. Stop the running loop, then start the other one (see Start):

```bash
.ai-toolkit/scripts/coordinator.sh --stop --run <run>
```

This terminal (from a chat session, a `coordinator-stop` terminal it opens) takes the Run (Orca fences the loop, which exits 0 with "taken back by") and the command waits until the loop is gone; exit 1 = still finishing a step (a land): run it again until it exits 0. A land takes minutes, longer than a foreground Bash call lives: from either kind of session run `--stop` as ONE Bash call with `run_in_background` and `timeout: 7200000` (as Start arms `--wait`), and read its exit when the call returns. Never start the other loop before it exits 0: the first may be mid-land and a second land would race it.
Nothing is lost: state lives in Orca, git and the issue labels, live workers keep running, and the new loop replays what the old one left unacked. Going back to attended after auto, summarize from "What landed" since the switch time, blocked issues first.
