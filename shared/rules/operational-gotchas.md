# Operational Gotchas

Recurring mechanical traps a spoke hits while committing and pushing through this
repo's commit/push cage. Each entry teaches how to **satisfy** an existing gate,
not how to bypass it — the hooks stay authoritative. If a hook and this rule ever
disagree, the hook wins and this rule is stale.

## RED-commit mechanics

The RED commit of a `/cycle` subtask (tests only, implementation withheld) passes
three separate checks that each read a different surface. Get all three right or
the commit blocks:

- **`Tested-RED:` is a machine-read trailer, not prose.** `red-proof-verify`
  extracts the pytest node ID (`tests/unit/test_x.py::test_name`) that follows the
  keyword and requires it to FAIL. The extractor takes the **first token after
  each `Tested-RED:` occurrence** — not every token on the line. So
  `Tested-RED: tests/x.py::test_a tests/x.py::test_b` proves only `test_a`;
  `test_b` rides along unverified. To prove several RED nodes, **repeat the
  keyword** — `Tested-RED: tests/x.py::test_a Tested-RED: tests/x.py::test_b` — so
  each node is a first-token. Prose like `Tested-RED: layout resolution fails`
  makes it run `layout` as a node and block with "no pytest runner". Put only
  full node IDs after each keyword; explain the failure in the body above.
- **Commit inline with `-m`, never `-F <file>`.** `commit-gauntlet` skips its
  whole-file typecheck only when it sees `Tested-RED:` in the git-commit *command
  string* the PreToolUse hook inspects. `-F` hides the trailer in a file, so the
  typecheck runs and a RED test referencing a not-yet-existing API fails pyright.
  Keep each `Tested-RED:` **space-preceded inside a line** (e.g. `... Refs #NNN.
  Tested-RED: <node>`) so it matches both the raw-command and the jq-escaped
  commit-msg forms.
- **Stash the implementation before committing RED.** `red-proof-verify` runs the
  listed nodes against the *working tree*. If the implementation is already
  present the nodes PASS and it blocks ("these Tested-RED nodes PASS"). Run
  `git stash push --keep-index` (staged tests stay, unstaged impl leaves),
  confirm the nodes fail, commit, then `git stash pop`. List only nodes that
  currently fail — omit determinism or negative-invariant pins that pass
  pre-implementation.

## The gauntlet lints only changed lines

`commit-gauntlet` runs ruff on the **changed** python lines of your diff. Two
things that pass local pytest but block the commit:

- **RUF003 ambiguous unicode in comments.** The multiplication sign `×` trips it
  (other exotic math glyphs may too); the em-dash `—` and `⇒` used throughout this
  repo do not. Write ASCII in new or edited python comments — `x` for "times",
  `<<` for "much less than". Pre-existing unicode elsewhere in the file is ignored;
  only your diff's lines matter.
- **A `×` adjacent to a shell `$var` is also a bash bug.** Under a non-UTF-8
  locale the `0xC3` byte is absorbed into the variable name (`$fails×` →
  `unbound variable`).

## The push gate reads the working tree, not the commit

The pre-push test gate runs pytest against the **live working tree**. Never let a
spoke push a tree that contains failing tests — including RED-by-design ones.

- **Don't interleave the next subtask's RED with an in-flight push.** If you start
  subtask A's push and then add subtask B's failing RED tests while A's gate is
  still running, the gate collects B's tests and rejects the push. Let each
  subtask's push finish before authoring the next RED, or keep the next RED
  stashed until GREEN and push only an all-green tree. Recovery: implement B's
  GREEN so the whole tree is green, then re-push (lands both). Do not kill the
  running gate.

## Never run the full suite bare in a spoke

Some tests in the full suite escape isolation and rewrite **real** refs shared
across worktrees (branch tips, `main`, sibling tags). Run only targeted test
files, and never concurrently with commits.

- After every commit, verify `git log --oneline -2` shows the new commit's parent
  is the previous tip — a silent ref rollback looks exactly like "my commits
  vanished".
- **The first push per worktree legitimately runs the full suite once** to seed
  `.testmondata`; subsequent pushes select only affected tests. Ensure
  `pytest-testmon` is installed (`pip install -r requirements-dev.txt`) or every
  push degrades to the full multi-thousand-test suite.

## Merge `origin/main` before pushing when behind

When a push is blocked or the branch is behind, `git fetch origin` then
`git merge origin/main`, resolve conflicts, and push normally. Do not invent
`TEST_SELECT_*` env hacks to force a push through a failing gate: the bug you are
hitting may already be fixed on main, and a sibling task may have landed changes
to the same files your work touches. Never self-land — the hub lands.

## Review-artifact hash bases on `@{upstream}`

The review-stamp diff hash and the `reviewer-sep` push gate resolve their base as
`@{upstream}` -> `origin/main` -> `origin/HEAD`. Once the branch has an upstream
(after the first `git push -u`), every subsequent subtask bases on `@{upstream}`
(the previous subtask's pushed tip), not `origin/main`. Each artifact binds only
to that subtask's new commits — that is correct. When hand-checking parity, use
`git diff -M $(git merge-base @{upstream} HEAD)..HEAD`; basing on `origin/main`
reports a false mismatch.

## Locale traps in system-tool output

This dev host runs a non-C, non-English locale. Any helper that parses system-tool
output by English keyword must force `LC_ALL=C`:

- **Never conclude "process not running" from a bare `pgrep -f`.** On non-ASCII
  argv it dies with "illegal byte sequence" and exits non-zero, so
  `pgrep -f foo || echo "(not running)"` prints "(not running)" for a live
  process. Use `LC_ALL=C pgrep -fl`, or check the port with
  `lsof -nP -iTCP:<port> -sTCP:LISTEN`.
- **`ps -o lstart=` is locale-formatted.** Parsing it without `LC_ALL=C` on a
  non-English locale strands the epoch empty (no error), so any staleness check
  silently never fires. Force `LC_ALL=C` on both the `ps` read and the `date -j
  -f` conversion.

Mechanical `LC_ALL=C` process-probe helpers are tracked in #189, and a
non-C-locale CI job in #194 — prefer those helpers once they land over hand-rolled
wrappers.

## Ship discipline

Push every subtask's branch without asking; emit the ready marker
(`spoke-ready.sh <issue>`) when the acceptance criteria are all met. Ask first
before genuinely irreversible operations — force-push, history rewrites, anything
touching `main`, or deletions outside the worktree. The hub lands the issue; a
spoke never self-lands.
