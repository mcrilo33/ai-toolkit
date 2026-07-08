# Enforcement gate fail-open audit

An inventory pass (issue #187) over every enforcement gate, guard, and hook in the
toolkit, asking three questions of each:

1. **Malformed input** — on an unparseable payload, missing artifact, or unexpected
   encoding, does it **block** or **silently pass**?
2. **Crash** — when the gate itself exits nonzero or is killed mid-run, does the
   wrapping flow treat that as **deny** or **allow**?
3. **Witness** — is there a positive-confirmation signal that the gate actually ran
   (a stamp, log line, artifact), or is silence indistinguishable from success?

No fixes are made here — this is inventory only. Confirmed fail-opens are filed as
separate issues (linked in the last section); each carries a minimal repro that was
run read-only in a worktree, or code-traced to an exact failure path.

## Platform semantics (why "crash" has two answers)

Every gate runs in one or both of two contexts, and the *same nonzero exit* means
opposite things in each:

- **Claude Code hooks** (`PreToolUse`/`PostToolUse`, wired in `.claude/settings.json`
  from `shared/hooks/metadata.yml`): exit **2** blocks the tool call; exit **0** with
  a JSON decision allows/denies; **any other nonzero exit is a non-blocking error and
  the tool call PROCEEDS**. So a guard that dies with exit 1 or 5 *before* reaching its
  deny path is fail-open by platform design.
- **Native git hooks** (`commit-msg`, `pre-push`, emitted by
  `scripts/install-git-hooks.sh`): any nonzero exit blocks — *if* the wrapper
  propagates it. This is fail-closed by platform design, but only when the invocation
  actually forwards the exit code.

Two cross-cutting mechanics drive most findings:

- **jq-swallow vs jq-crash.** Extraction helpers that end in `2>/dev/null` (e.g.
  `get_shell_command`, `utils.sh:143-154`) return empty on malformed JSON, and errexit
  does not propagate out of `$(…)` without `inherit_errexit` — so the gate reaches its
  `[ -z "$COMMAND" ] && exit 0` and *silently passes*. But a helper whose assignment is
  **jq-terminal** (`EVENT=$(get_hook_event "$INPUT")`) propagates jq's parse-failure
  **exit 5** under `set -e`, and 5 ≠ 2 → the CC tool call proceeds. Same malformed
  input, two different fail-open shapes.
- **No default witness.** Only the push test gate mints a durable witness (the
  green-tree stamp, `lib/gate-stamp.sh`). Every commit-path gate and guard emits a
  positive signal *only* via the opt-in telemetry span (`AI_TOOLKIT_TELEMETRY=1` or an
  OTel endpoint). By default, "hook never invoked" is byte-identical to "hook passed".

## Summary matrix

Verdict legend: **FAIL-OPEN** = confirmed silent pass / crash-allow (filed);
*suspected* = a plausible fail-open needing one live probe to confirm;
*advisory* = warn-only by design (records intent, not a defect);
**closed** = fails in the safe direction.

### Commit path

| Gate | Malformed input | Crash | Witness | Verdict |
|------|-----------------|-------|---------|---------|
| `install-git-hooks.sh` commit-msg stage | bad msg-file → empty msg → passes | crashing cage script blocks (`\|\| exit $?`) | none | **FAIL-OPEN** — missing/non-exec cage script silently skipped (no `else`) |
| `commit-gauntlet.sh` | empty → exit 0 | native blocks; CC exit≠2 → proceeds | none (opt-in span) | closed natively; *suspected* in CC (crash/timeout/chained cmd) |
| `commit-quality.sh` | malformed → exit 0; `-F`/editor msg → exit 0 in CC | native blocks; CC proceeds | none | closed natively; *suspected* in CC |
| `red-proof-verify.sh` | malformed → exit 0 | CC exit≠2 → commit proceeds; **no native backstop** | none — unexecuted RED trailer looks verified | **FAIL-OPEN** |
| `block-no-verify.sh` | no-command payload → exit 0 | exit≠2 → `--no-verify` commit proceeds (disarms whole native cage) | none | **FAIL-OPEN** — sole `--no-verify` defense, CC-only prefix match |
| `secrets-scan.sh` | malformed JSON → **exit 5** → Write proceeds; missing jq → empty → exit 0 | crash → proceeds | none | **FAIL-OPEN** |
| `red-proof-warn.sh` | malformed → exit 0 | CC proceed / native `\|\| true` | none | advisory (Cursor-only block) |
| `reviewer-sep-warn.sh` | malformed/unresolvable → exit 0 | CC proceed / native swallowed | `.review` artifact = review witness, not gate witness | advisory (Cursor-only block) |
| `secrets-scan-revert.sh` | malformed → exit 5 (harmless, PostToolUse) | no containment on crash | backup file + log when it acts | advisory (containment) |
| `quality-gate.sh` | malformed → exit 0 | never blocks (exit 0 always) | stderr only | advisory |
| `lib/gate-stamp.sh` | bad stamp → re-run; dirty tree → no key | mint failure non-fatal | **yes** — stamp minted only post-green | closed (scope: push gate only) |
| `lib/enabled.sh` | unreadable config → ENABLED | cannot crash realistically | none when OFF | closed resolver; see off-switch below |

### Push path

| Gate | Malformed input | Crash | Witness | Verdict |
|------|-----------------|-------|---------|---------|
| `test-select.sh` | empty/malformed stdin → exit 0 ("empty diff"); bad sha → FULL | crash → nonzero → push aborts | green-tree stamp on pass only | **FAIL-OPEN** — empty stdin passes; **no pytest → exit 0** |
| native `pre-push` hook | `cat \|\| true` drains read-fail to empty | test-select `\|\| exit $?`; missing test-select → exit 1 (blocks) | none of its own | closed wrapper; off-switch bypass (below) |
| `anti-gutting-scan.sh` | no ranges → exit 0 | crash blocks; findings always exit 0 | stderr only | advisory-by-design (#143 moved enforcement) |
| `review-stamp-guard.sh` | unparseable → exit 0 (approve proceeds) | CC crash (exit 1) → allows | step:review span (opt-in) | *suspected* (Cursor-path only; Claude allows by design) |
| `review-window-open.sh` | no identity/no jq → substring fallback opens window | crash → window not opened | `.review/.window` file | *suspected* (open over-grants via substring) |
| `review-window-close.sh` | substring-only match | crash → window stays open | window file | closed by 1800s TTL |
| `git-push-review.sh` | empty/unparseable → exit 0 | crash → CC allows | stderr only | advisory (Cursor-only block) |
| `spoke-push.sh` | detached/main → refuse | branch push via `wt_git_push` keepalive; `set -e` aborts | `✓ complete` on success only | closed |
| `spoke-ready.sh` | dirty/no-upstream/no-APPROVE → exit 1 | tag push is a **raw unwrapped** `git push` (fails loud) | pushed tag; `READY_FORCE` leaves no durable trace | closed on preconditions; see #184, #206 |
| `gate-sweep.sh` | missing lib/sha → no sweep | `trap 'exit 0' EXIT` — every failure → exit 0 | `sweep.log` + stamp upgrade | fail-open **by contract** (safety net); inherits gate skip blind spots |
| `worktree-land.sh` | stale/missing `ready/<N>` → die; no pre-push hook → warn+proceed | gate red → rollback+die; **exit-141 → one `TEST_SELECT_SKIP=1` retry** | issue-close `suite:` line (best witness in system) | closed on primary path; **FAIL-OPEN** via 141-retry; see #196 |
| `worktree-lib.sh` `wt_git_push` | — | keepalive, exit passthrough | — | closed; gap is the one uncovered call site (spoke-ready tag) |

### Guards + hub control plane

| Component | Malformed input | Crash | Witness | Verdict |
|-----------|-----------------|-------|---------|---------|
| `lib/enabled.sh` off-switch (via `utils.sh:21-25`) | n/a | resolver fails to ENABLED | **none** — OFF also silences telemetry | **FAIL-OPEN** (by-design switch, witness-less, agent-reachable) |
| registration `if:` gating | n/a | n/a | n/a | *suspected* — env-prefix/`-C`/compound cmds may bypass whole commit/push family (#15292) |
| `rm-scope-guard.sh` / `chmod-scope-guard.sh` | malformed/newline/redirect → exit 0 | exit 0 always | allow-decision only | closed (silent = the ask-rule still prompts) |
| `lib/scope-guard.sh` | splits on `;&&\|\|\|` **not newlines** | degrades to not-provable | — | closed for allow-guards; hole surfaces in spoke-main-guard |
| `spoke-main-guard.sh` | malformed → exit 0; **`true\ngit checkout main` → exit 0** | git calls `\|\| true`; deny = exit 2 | deny only | **FAIL-OPEN** (newline-separated compound bypasses deny) |
| `hub-guard.sh` | malformed → exit 0; bare-string `tool_input` → exit 5 | jq-terminal file-path branch crash-open | deny loud; bypass silent | *suspected* (abnormal payload; stale `hub-guard-allow` marker) |
| `push-scope-guard.sh` | malformed → exit 0 | git calls guarded | allow JSON | advisory on Claude (warn-only) |
| `config-protection.sh` | **malformed/shape-mismatch → exit 5 → Write proceeds** | crash → proceeds | deny loud; crash silent (empty stderr) | **FAIL-OPEN** |
| `plan-gate-guard.sh` | malformed → exit 0 | git calls `\|\| true` | deny loud | closed for stated scope (deliberate deny-or-silent) |
| `todo-ledger-warn.sh` | no transcript → exit 0 | guarded | warn when fires | advisory |
| `delegation-gate-warn.sh` | malformed → exit 0 | guarded | warn-once markers | advisory |
| `todo-ledger-nudge.sh` | drains stdin | fixed literal only | additionalContext | advisory, robust |
| `console-log-warn.sh` / `post-edit-format.sh` | malformed → exit 0 | formatter crash silent | log on action | advisory |
| `afk-notify-wake.sh` | malformed → exit 5 (dies) | later steps `\|\| exit 0` | spool + SIGUSR1 | advisory; latent `set -e` re-enable |
| `gate-broker.sh` | no decision → **escalate blocked**; unknown segment → deny | timeout-bound; crash → escalate | logs, blocked tags, durable records | **closed** (best-engineered; default-deny/escalate) |
| `hub-afk.sh` supervisor | corrupt state → never time-expires (but drain/off still end) | heartbeat + cross-check + watchdog respawn | heartbeat, `--status`, tags | closed, with two known exceptions (watchdog unwatched; review gate default-off) |
| `hub-ready-watch.sh` | malformed tags filtered; torn-down wt skipped | fetch/notify `\|\| true` | proposals printed | advisory |
| `hub-notify.sh` | numeric filter; degraded tags handled | notifier `\|\| true` | notification only | advisory; **confirmed** suppress-forever under stale `.afk-state` |

## Confirmed fail-opens (with repros)

Each was verified read-only. Repros feed malformed payloads on stdin or code-trace an
exact branch — none mutate state.

### 1. jq-terminal extraction crashes guards open (exit 5) — #208

`secrets-scan.sh:23`, `config-protection.sh:61,90`, `hub-guard.sh:114` extract via a
jq-terminal assignment (`EVENT=$(get_hook_event "$INPUT")` /
`FILE_PATH=$(get_edit_file_path "$INPUT")`). Under `set -e`, a jq parse failure
propagates **exit 5** — not 2 — so Claude Code treats it as a non-blocking error and
the guarded Write/tool call **proceeds**, with empty stderr (jq's error is
`2>/dev/null`'d).

```bash
printf 'garbage' | bash shared/hooks/secrets-scan.sh; echo $?          # → 5 (Write proceeds)
printf '{"tool_name":"Write","tool_input":"garbage"}' | \
  bash shared/hooks/config-protection.sh; echo $?                      # → 5 (Write proceeds)
# control: a well-formed protected write still denies →
printf '{"tool_name":"Write","tool_input":{"file_path":"pyproject.toml","content":"x"}}' | \
  bash shared/hooks/config-protection.sh; echo $?                      # → 2 (deny)
```

`secrets-scan.sh` additionally silent-passes when `jq` is absent
(`get_edit_new_content` can only extract via jq → empty content → exit 0 → nothing
scanned).

### 2. spoke-main-guard newline bypass — #209

`spoke-main-guard.sh:288` normalizes `(){}` and backticks to `;` before tokenizing but
never newlines, and (unlike rm/chmod guards) does not bail on newlines. A
newline-separated compound whose first token isn't `git` tokenizes past the deny.

```bash
# in a spoke worktree (WT_SPOKE=1):
printf '{"tool_name":"Bash","tool_input":{"command":"git checkout main"},"cwd":"'"$PWD"'"}' | \
  WT_SPOKE=1 bash shared/hooks/spoke-main-guard.sh; echo $?            # → 2 (deny, works)
printf '{"tool_name":"Bash","tool_input":{"command":"true\ngit checkout main"},"cwd":"'"$PWD"'"}' | \
  WT_SPOKE=1 bash shared/hooks/spoke-main-guard.sh; echo $?            # → 0 (bypass)
```

Multi-line Bash blocks are routine agent output, so the #32 "no checkout main in a
spoke" invariant is practically reachable.

### 3. red-proof-verify has no native backstop — #210

`red-proof-verify.sh` is wired **only** as a CC PreToolUse hook (`if: Bash(git commit *)`,
`settings.json:41-45`) — it is not in the native commit-msg hook
(`install-git-hooks.sh:12`). So a chained/prefixed commit the `if` filter excludes
(`true; git commit -m '… Tested-RED: fake'`) never invokes it, a CC crash (exit≠2)
lets the commit through, and its only output is the very trailer it was meant to prove.

```bash
printf 'garbage' | bash shared/hooks/red-proof-verify.sh; echo $?      # → 0 (silent pass)
```

### 4. block-no-verify is a crash-open CC-only prefix match — #211

`block-no-verify.sh` (`if: Bash(git * --no-verify *)`, `settings.json:26-29`) is the
sole defense against the one flag that disarms **both** the commit-msg and pre-push
native cages. It cannot be installed natively (`--no-verify` skips the hooks it would
live in). Any chained/env-prefixed spelling the `if` filter misses, or a crash
(exit≠2), lets `--no-verify` through — after which no gate on either surface runs.

```bash
printf '{"tool_name":"Bash"}' | bash shared/hooks/block-no-verify.sh; echo $?   # → 0 (no command → pass)
```

### 5. install-git-hooks commit-msg silently skips missing cage scripts — #212

`install-git-hooks.sh:139-143` guards each cage script with `if [ -x "$SCRIPTS/$s.sh" ]`
and **no `else`** — a deleted or `chmod -x`'d `commit-quality.sh`/`commit-gauntlet.sh`
is silently skipped and the commit sails through. The pre-push stage closed exactly this
hole for `test-select.sh` with an explicit `exit 1` (lines 197-200); the commit-msg
stage has no equivalent.

```bash
SCRIPTS=/nonexistent; for s in commit-quality commit-gauntlet; do
  [ -x "$SCRIPTS/$s.sh" ] && echo run || echo silently-skipped; done   # both: silently-skipped
```

### 6. test-select: no pytest → exit 0 — #213

`test-select.sh:285-289`: when no pytest runner is found, `note "no pytest available —
nothing to run"; exit 0`. A python-touching diff on a fresh checkout (no `.venv`,
pytest not on PATH) passes the gate untested with only a transient stderr note and
**no stamp** — and because the post-land sweep launches only off a pruned stamp, the
skip also disarms that safety net. Empty/malformed pre-push stdin passes the same way
("docs-only or empty diff"), and the native hook's `cat || true` drain can manufacture
exactly that empty input.

```bash
printf '' | bash shared/hooks/test-select.sh; echo $?                  # → 0 ("empty diff")
```

### 7. worktree-land 141-retry lane auto-degrades to TEST_SELECT_SKIP=1 — #214

`worktree-lib.sh:154` treats **any** exit 141 as post-green SSH transport death, and
`worktree-land.sh:488-492` recognizes a real pytest failure only by its printed summary
("N failed", "Interrupted"). A gate whose pytest is **killed** mid-run (SIGPIPE, OOM)
prints no summary and can exit 141 — the land then retries once with
`TEST_SELECT_SKIP=1`, shipping a tree whose suite never finished. Loud (a warn line),
but automatic. Code-traced; live repro would kill a pytest mid-land and is not run here.

### 8. hub-notify suppresses gate/ready pings forever under stale `.afk-state` — #215

`hub-notify.sh:58-63` gates notifications on `.afk-state` being *non-empty*, not on
supervisor liveness. A crashed drain whose watchdog is also gone keeps suppressing every
gate/ready ping until `--off` or respawn; worse, suppressed markers are still recorded
as *seen* (159-177), so those transitions never ping even after recovery. A corrupt
state file compounds it — `window_expired` treats a non-numeric state as never-expiring.
Advisory surface (lost notifications, not a blocked gate), but a confirmed
degrade-to-silence.

## Already-filed fail-opens (anchors, not re-filed)

The audit re-derived these known instances; they are tracked and excluded from new
filing:

- **#184** — `spoke-ready.sh:271` ready/N tag push is a raw unwrapped `git push` (no
  keepalive/retry); the residual of the #119 keepalive work.
- **#196** — `worktree-land.sh:440-446` lands untested (warn + honest report line) when
  no pre-push hook is installed.
- **#206** — `AI_TOOLKIT_READY_FORCE=1` emits a `ready/N` tag byte-identical to an
  earned one (stderr-only witness); tracked under the READY_FORCE audit.
- **#188** — push-gate tripwire snapshots/restores refs without a reflog entry; the
  killed-mid-gate ref-rollback class.
- **#202** — afk supervisor hardening (heartbeat/watchdog/drain-completion), which
  covers the "watchdog itself is unwatched" residual.

## Suspected (not filed — need one live probe)

- **Registration `if:` gating** — if Claude Code honors the emitted
  `if: "Bash(git commit *)"` / `"Bash(git push *)"` with permission-rule semantics, it
  inherits the engine's env-prefix/`git -C`/compound-command matching gaps
  (anthropics/claude-code#15292): `cd x && git commit …`, `VAR=1 git push …`,
  `git -C . commit …` would bypass the *entire* commit/push hook family before the
  hardened scripts run. Confirm with a live CC probe (the scripts self-filter if unknown
  keys are ignored — then it is fail-closed).
- **review-window-open substring fallback** — `review-window-open.sh:33-35` opens the
  window on any payload merely containing `code-review` when no identity field resolves;
  over-grant direction (distinct from the close-substring issue #197).
- **hub-guard abnormal-payload crash** — same jq-terminal mechanism as finding 1 at
  `hub-guard.sh:114`, reachable only with a bare-string `tool_input`.

## Structural observations

- **No positive witness across the CC gate surface.** Every commit-path gate and every
  PreToolUse guard emits a "ran and passed" signal only through the opt-in telemetry
  span. By default, a hook that never fired (unregistered, non-executable, `if`-filtered,
  crashed at source) is indistinguishable from one that passed. The only durable witness
  in the system is the push gate's green-tree stamp and `worktree-land`'s `suite:` line
  in the issue-close comment. A general witness mechanism would turn most of the
  suspected/crash-open findings from silent into observable.
- **The commit path is fail-closed only on the native surface.** Everything routed
  purely through CC PreToolUse hooks is crash-open by platform design; the native
  commit-msg/pre-push cage is the real backstop — which is why the gaps in it (finding 5,
  the `--no-verify` escape hatch) matter most.
