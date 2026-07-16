# AFK Design Principles

The `/afk` control plane (hub-afk, the gate-broker modules, the watchdog, the
worktree/spoke scripts) is a distributed, unattended system. A week of incidents
(the #263/#265/#283/#288/#290 watchdog false-fire family, #299's silent 10-hour
jam, the #291/#305/#306 model-config leaks, #301's dead-agent-live-pane) all trace
to a small set of violated principles. Honor these when writing or reviewing any
control-plane change; a reviewer should cite the number a diff breaks.

## 1. Explicit state over inferred state

Record a transition where it happens, by the actor that causes it. Do NOT
reconstruct lifecycle state from side-effects — file mtimes, pane text, epoch
staleness, transcript tails. Inference is why detectors false-fire: the signal
they read (a progress epoch that pre-ages during a park, a pane that survives a
dead agent) does not mean what they assume. If a detector must know "is this spoke
landing / pushing / being serviced", something must have RECORDED that, not left
it to be guessed. (The #300 transition log is this principle made concrete.)

## 2. Fail loud; never silently fall back to the worst case

A fallback that fires silently hides the failure it papers over — and if it falls
back to the most expensive / most dangerous option, it is the worst possible
default. The model resolver silently defaulting to `claude-opus-4-8[1m]/max` cost
real money three times before anyone saw it (#291/#305/#306). A `pushed-but-unmarked`
spoke that only *warns* stalled a whole overnight window and every issue behind it
(#299). Rule: a fallback path emits a loud, visible signal (a `wt_warn`, a `blocked`
marker, a notification), and its default is the SAFE/CHEAP option, never the
priciest or most destructive.

## 3. Act when unattended; escalate only the irreversible

Under `/afk` the human is not there, so ACTING on ambiguity beats warn-and-wait.
A wrong-but-reversible action costs one cycle to undo; a stall costs the entire
window and silently jams every scope-dependent issue behind it. Run the ladder —
nudge → relaunch → decide — and escalate to `blocked` only for the genuinely
irreversible and outward-facing (force-push, history rewrite, `main`, deletions
outside the worktree). Every unattended decision is journaled so it can be audited
after the fact — that auditability is what makes "act, don't wait" safe.

## 4. Liveness = probe the real process, never a proxy

To decide whether a spoke's agent is alive, walk the pane pid's DESCENDANTS for a
live agent process. Do NOT use `pane_current_command` (the launcher is a `zsh -c`
wrapper, so tmux reports `zsh` whether the agent lives or died) and do NOT grep for
a `WT_SPOKE=<n>` argv (that matches only the wrapper, which survives the agent's
exit, and a live agent does not carry it). Both proxies are wrong in BOTH
directions — false-dead and false-alive (#301). Getting this wrong means injecting
prose into a shell, where it executes as commands.

## 5. Cross-lane shared state carries explicit, single-writer semantics

A per-issue state file read by more than one lane must mean exactly one thing, set
by exactly one writer. `answer-attempt-<n>.epoch` had four writers across three
lanes and two readers with different meanings — the reaper read it as "delivery in
progress", the watchdog as "an answer was delivered" — and that ambiguity produced
false-fires (#241/#274/#288/#294). Never reinterpret another lane's marker. If a
new consumer needs a different meaning, it needs its own record, or the writer must
expose the meaning explicitly.

## 6. Best-effort writes never fail the caller; "unknown" is never a firing basis

A telemetry/log/marker write must never fail the operation it observes — it degrades
to a no-op (the hooks' always-exit-0 discipline). Correspondingly, a reader that
finds a record absent reads "unknown", and "unknown" alone must never be the basis
for a destructive or escalating action (a reap, a `blocked`, a fire). Absence is not
evidence; it is the lack of evidence. Fall back to a conservative path when the
record you need is missing, and say so.

## 7. Structure for parallelism: no monolith becomes everyone's `Scope:`

Concurrency in the drain is scope-graph-bound: two issues run in parallel only if
their `Scope:` files are disjoint. So a control-plane file that every change must
touch (once `gate-broker.sh` at 3,700 lines, then `hub-afk.sh` at 4,400) silently
serializes the whole backlog — the cost is invisible until you notice nothing runs
concurrently. Split by single responsibility (dispatch, recover, land, the tick
loop) behind a thin entry lib, each with its own mirror test, so independent work
has disjoint scopes. A file that becomes the common `Scope:` token is a scheduling
bottleneck, not merely a large file — treat crossing the line budget as a signal to
split (enforced by the control-plane-size governance test).
