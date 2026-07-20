# Pre-push test gate

The native pre-push hook is the **single owner of test execution** in this
toolkit's worktree workflow: every `git push` runs a tiered, diff-aware test
selector exactly once — **one push = one run**. There is no separate land-side
suite run; landing merges and pushes, and the push is what gets gated.

## Tiers

`shared/hooks/test-select.sh` classifies the diff a push carries and runs the
cheapest sufficient suite, with **default-to-full safety** — anything not
provably docs-only or python-only runs the full suite.

| Changed files | Runs |
| ------------- | ---- |
| All docs-only (`*.md`, `docs/`, `LICENSE`, `*.rst`, images) | nothing |
| Every non-doc file is `*.py` | `pytest --testmon` (coverage-based test-impact) |
| Anything else (`.sh`, `Dockerfile`, `*.yml`, `Makefile`, unrecognized) | the full suite |

A `*.py` file counts as python even under `docs/` (e.g. `docs/conf.py`): testmon
judges its impact rather than the path skipping it as a doc.

## Parallelism (pytest-xdist)

The **non-testmon** legs — the full suite and the SELECTED mapped-files leg — run
under `pytest-xdist`'s `-n auto` (one worker per core), since the suite is
I/O-bound and embarrassingly parallel. The post-land full sweep
(`gate-sweep.sh`) parallelizes the same way. The `--testmon` legs stay
single-process: testmon serializes a single-writer DB and does not compose with
xdist (`pytest --testmon -n auto` is unsupported). This is guarded on the runner
advertising `-n numprocesses` in `pytest --help`, so a checkout without
`pytest-xdist` installed degrades to single-process rather than erroring the push.

## The serial tail: two-phase full runs (`serial` marker)

A minority of tests **escape isolation and rewrite real shared refs** (the tripwire
family) and so cannot run under xdist workers safely; run bare under `-n auto` they
would corrupt a worker or trip the tripwire, capping how well the parallel majority
scales (issue #328). These carry the `serial` marker (registered in `pyproject.toml`),
and every unavoidable full run is **two-phase**:

1. **Parallel bulk** — `-n auto -m "not serial"`: the xdist-safe majority.
2. **Serial tail** — `-m serial`, single-process (no `-n`): the ref-mutating minority.

Both phases run under the same tripwire; the combined exit code is the verdict. The
serial leg's exit 5 ("no tests collected" — nothing marked `serial`, e.g. a fresh
checkout or a synced repo with none) is normalized to green. This applies to the FULL
leg and the testmon-absent full fallback in `test-select.sh`, and to `gate-sweep.sh`'s
post-land sweep; the `--testmon` and SELECTED legs are unchanged.

`tests/conftest.py` installs a fail-loud guard: if a `serial`-marked test is collected
under a **bare, path-less `-n auto`** — the whole-suite invocation this split replaces —
the run refuses rather than let a ref-mutating test corrupt a worker. It deliberately does
NOT fire when the caller names test files explicitly (the SELECTED gate leg, or a dev
running specific files), since that is a deliberate selection, not the whole-suite run.
Under-marking degrades safely: an unmarked ref-mutator that reaches the parallel phase
still trips the tripwire (a loud, blocked push), never silent corruption.
`tests/unit/test_serial_marker.py` guards the marker registration and the guard's
fire/no-fire cases.

## Pre-warmed `.testmondata` baseline

A fresh worktree has no testmon DB, so its **first push** would run the whole suite
just to build `.testmondata` (12–47 min observed) before any later push can prune.
To skip that seed, a maintained baseline `.testmondata` lives at
`<git-common-dir>/.testmondata-baseline` on the hub:

- **Spawn copy.** `worktree-new.sh` copies the baseline into each new worktree's
  root, so its first push runs a testmon **incremental** (only the branch diff's
  affected tests) instead of the full seed. No path rewrite is needed — testmon
  stores rootdir-relative paths, so a DB built at one absolute path is reused at
  another.
- **Refresh.** `gate-sweep.sh` rebuilds the baseline after a **green** post-land full
  sweep, running `pytest --testmon` with `TESTMON_DATAFILE` pointed at the baseline
  (single-process — testmon does not compose with xdist) so the hub's own
  `.testmondata` is untouched.
- **Staleness.** testmon keys its `environment` row on `system_packages` +
  `python_version`, so a copied baseline whose `.venv` dep set differs (e.g. after a
  `requirements-dev.txt` bump) is invalidated automatically — testmon re-runs the
  full suite. A missing or unreadable baseline likewise degrades to today's
  full-suite seed. Both paths fail toward *more* testing, never a wrong-green.

## Safe fallbacks

The gate is built to fail toward *more* testing, never toward silently skipping:

- **testmon not installed → the full suite.** The python tier degrades to the
  whole suite rather than skipping python tests. Install it with `pip install
  pytest-testmon` (declared in `requirements-dev.txt`) to get test-impact
  selection.
- **No pytest resolvable → nothing to run.** The gate degrades rather than
  erroring the push.
- **A diff range that can't be resolved → the full suite** (it cannot be proven
  safe).
- **Selector missing or non-executable → the push is refused** (fail-closed): a
  missing gate must not silently ship untested code. Re-run
  `scripts/install-git-hooks.sh` to restore it.

## How the range is resolved

Git feeds the pre-push hook the pushed refs on stdin, one per line as
`<local ref> <local sha> <remote ref> <remote sha>`. The range classified per
ref is `remote_sha..local_sha`; a new branch (all-zero remote sha) falls back to
the merge-base with the default branch; a deletion (all-zero local sha)
contributes nothing.

## Test isolation: the git-hook env strip

Git runs the pre-push hook with `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` and
related vars **exported** into its environment. Because the gate launches `pytest`
from inside that hook, those vars are live for the whole test run — and a leaked
`GIT_DIR` overrides a subprocess's working directory. Tests here shell out to
`git` against throwaway tmpdirs; without protection they would instead operate on
the **real repository**. This is not hypothetical: issue #24's push committed a
bogus `chore: seed` onto the hub's `main` and flipped `core.bare` to `true`
through exactly this leak (issue #30).

Two layers close it, and **both must stay**:

- **`tests/conftest.py` strips the git-hook env** (`GIT_DIR`, `GIT_WORK_TREE`,
  `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_COMMON_DIR`, `GIT_NAMESPACE`,
  `GIT_PREFIX`, `GIT_CONFIG`, `GIT_CONFIG_*`) both at module import — before any
  test module snapshots `os.environ` — and via an autouse fixture per test. This
  protects every run regardless of how `pytest` was launched (hook, CI, or local).
- **The hook scripts also drop those vars for the pytest child** (`env -u …` in
  `test-select.sh` and the `run_pytest_node` backstop), scoped so the scripts'
  own `git` classification calls keep their context. This is defense-in-depth for
  a repo whose tests lack the conftest strip.

`tests/unit/test_git_env_isolation.py` is the regression guard: it runs a child
`pytest` under a leaked `GIT_DIR` pointing at a **decoy** repo and asserts the
decoy is untouched. Do not remove the strip without removing that test's reason
to exist.

## The repo-integrity tripwire

The env strip closes the **known** leak vector. The tripwire (issue #31) is the
safety net for the whole **class** of isolation breaches: the #29/#30 incident
corrupted the real repo *silently* — a ref moved and `core.bare` flipped, and
nothing noticed until a bogus commit was found on `main` by hand. Any future
breach (a different env var, a fixture that `cd`s wrong, a plain bug) could do
the same. The tripwire turns silent corruption into a loud, blocked push.

It brackets every pytest run the gate launches with a snapshot/verify:

1. **Before** the run, snapshot the real repo's integrity markers —
   `HEAD` + every local ref tip (`git show-ref --head`), `core.bare`, and
   `core.worktree`.
2. Run the tests (still with the git-hook env stripped).
3. **After**, re-read the markers. If any changed, a test escaped isolation and
   mutated **this** repo: **abort** the push (exit `97`) naming the marker that
   moved, and **restore** the snapshot — reset the ref, drop a ref that appeared,
   set `core.bare`/`core.worktree` back — so the checkout is left clean.

It is cheap (one `git show-ref` + two `git config` reads per side) and wraps both
pytest entry points: every `test-select.sh` tier (`run_under_tripwire`) and the
`run_pytest_node` red-proof backstop (a mutation there yields a `BREACH` verdict
that `red-proof-verify` and `red-proof-warn` block on).

**No false positives.** Because the pytest child still runs with `GIT_*` stripped,
a hermetic test that creates and deletes its **own** tmpdir repo never touches
these markers — only a real escape into this repo trips it. The already-fixed
`GIT_DIR` scenario passes cleanly through.

**Only `TEST_SELECT_SKIP` bypasses it** — the same `--skip-tests` escape hatch
that skips the gate skips its tripwire; there is no separate opt-out.

> [!NOTE]
> The tripwire **detects** a HEAD symbolic-target re-point (a stray
> `git checkout`/detach) and aborts on it, but restores branch refs and config
> rather than rewinding HEAD's symref — the abort, not the rewind, is the
> protection. The incident's actual vectors (a branch-ref move and a `core.bare`
> flip) are fully restored.

`tests/unit/test_tripwire.py` is the regression guard: clean run (no trip),
ref-moved and `core.bare`-flip (trip + restore), plus the hermetic-tmpdir and
`GIT_DIR` no-false-positive cases.

## Installation

The gate fires only where the native hooks are installed:

```bash
scripts/install-git-hooks.sh [target-repo]   # wires commit-msg + pre-push
```

This copies `test-select.sh` (and the other cage scripts) into the repo's
`.git/hooks/ai-toolkit-scripts/` and wires the pre-push hook to run it as a
**blocking** gate — a non-zero exit aborts the push. `shared/hooks/*.sh` also
sync into target repos via `scripts/sync-to-repo.sh`. Without the native hook
installed, a push runs no gate; `worktree-land.sh` warns when that is the case so
a green land is never mistaken for a tested one.

## Landing

`worktree-land.sh` does not run the suite itself. It merges the spoke branch and
pushes the default branch; that push's pre-push hook is the test gate. A rejected
push — the gate failing, or a remote refusal — rolls the merge back with
`git reset --keep`, leaving a clean hub. The escape hatches are threaded into the
hook rather than run land-side:

| Flag | Effect |
| ---- | ------ |
| `--skip-tests` | Threads `TEST_SELECT_SKIP=1` — the gate runs nothing |
| `--test-cmd <cmd>` | Threads `TEST_SELECT_CMD=<cmd>` — the gate runs `<cmd>` instead of the tiered selection |

## Why one push = one run

The previous flow tested the same merged state twice at land time: a blanket
land-side `pytest` *and* the native pre-push hook firing on the main push. Making
the hook the single owner removes the redundancy and adds diff-awareness — a
docs-only change pays nothing, a python-only change pays only its test impact.

There is deliberately no fast-forward dedup and no commit pass-cache: a
fast-forward land re-tests already-green commits once, which is still one push,
one run.
