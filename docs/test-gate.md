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
