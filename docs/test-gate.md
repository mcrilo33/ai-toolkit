# Test gate: CI is the gate

**CI runs the full suite; the local machine only runs the fast tier.** Every branch push
triggers the `CI` workflow (`.github/workflows/ci.yml`), and `spoke-ready.sh` and
`worktree-land.sh` refuse to ship a SHA until CI is green for exactly that SHA. The native
pre-push hook (`shared/hooks/test-select.sh`) therefore never runs the whole suite — it runs
the cheap, diff-aware tests that give a developer feedback in seconds.

Why (issue #378, option B of the Orca gating proposal): local full gates
took 5–33 minutes per branch, collided with the agents on the same machine (load 10–21), and
failed on main-level flakes six times in one day, while CI already ran the same suite on
`ubuntu-latest` in about seven minutes. GitHub's native merge queue is unavailable on a
personal-account repo, so lands stay scripted.

## The fast tier (pre-push)

`test-select.sh` classifies the diff a push carries and runs only what is cheap:

| Changed files | Runs |
| ------------- | ---- |
| All docs-only (`*.md`, `docs/`, `LICENSE`, `*.rst`, images) or exempt (`.test-select-exempt`) | nothing |
| A non-python file that maps to referencing tests (reverse index) | exactly those test files, under `-n auto` |
| A python change, testmon installed **and** a database present, **and** testmon would select ≤ `TEST_SELECT_TESTMON_MAX` (200) tests | `pytest --testmon` (never under xdist) |
| Any non-doc change | plus the control-plane coverage meta-test |

A `*.py` file counts as python even under `docs/` (e.g. `docs/conf.py`).

Where a diff used to escalate to the full suite — an unmapped, non-exempt change; testmon
absent; no testmon database; a diff range that cannot be resolved — the hook now runs the
mapped tests that exist and prints that **CI is the full gate**. It never starts a run whose
size it cannot bound: a first `pytest --testmon` in a tree without a database would execute
the whole suite to seed it, so that leg is skipped (the baseline `worktree-new.sh` copies in
is what keeps testmon incremental). The meta-test (`TestControlPlaneCoverage`) keeps the
invariant that an unmapped control-plane script cannot ship green: it is red until a test
references the script or `.test-select-exempt` lists it.

Lint, type-check and the anti-gutting scan are separate commit-time hooks and are unchanged.

## CI

`.github/workflows/ci.yml` runs on push to **every branch** (plus PRs), with
`concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }` so a newer push to a
branch cancels that branch's older run. The suite runs in two phases (issue #328):

1. **Parallel bulk** — `pytest tests/ -n auto -m "not serial"` (`pytest-xdist` from
   `requirements-dev.txt`).
2. **Serial tail** — `pytest tests/ -m serial`, single process; exit 5 (nothing collected) is
   green.

Tests that **escape isolation and rewrite real shared refs** (the tripwire family) carry the
`serial` marker (registered in `pyproject.toml`) and must never run under xdist workers.
`tests/conftest.py` installs a fail-loud guard for a bare, path-less `-n auto`; an explicit
`tests/` path bypasses it, which is why CI excludes the marker explicitly instead of relying on
the guard. ShellCheck and the sync-idempotency check run alongside. The macOS control-plane job
stays `continue-on-error` until its flakes are fixed (separate issue). Only pushes to `main`
and PRs file `CI red: …` backlog issues; a red spoke branch is reported to its own spoke.

## Gating ready and land

`spoke-ready.sh <N>` (the `ready/<N>` marker) refuses until CI reports **success for HEAD's
exact SHA**, after the existing clean-tree / pushed-tip / review-artifact preconditions:

```bash
gh run list --commit <sha> --workflow CI --json status,conclusion,url,databaseId
```

| CI state | Message |
| -------- | ------- |
| unfinished after the bounded wait (`WT_READY_CI_WAIT`, default 540 s) | `CI pending (<url>)` |
| completed, not successful | `CI failed (<url>, failing jobs: …)` |
| no run for the SHA after a short grace | `no CI run for this SHA (was it pushed?)` |
| `gh`/`python3` unavailable | `cannot read CI … use --local-gate` |

Several runs can share a SHA (a push run and a PR run, re-runs); any success wins. A just-pushed
SHA has no run for a few seconds, so "none" only becomes final after `WT_CI_NONE_GRACE`
(default 90 s). `--no-wait` checks once and never polls. `AI_TOOLKIT_READY_FORCE=1` still
bypasses every precondition, CI included, and stamps the bypass into the tag.

`worktree-land.sh` never runs tests itself:

1. The branch tip (the ready SHA) must be CI-green (`LAND_CI_WAIT_MAX`, default 1200 s). A red run
   exits the precondition code (5) so the drain escalates instead of retrying; pending / no run /
   no `gh` exit 1.
2. If the default branch is an ancestor of the tip, the land is a pure **fast-forward of a
   CI-green SHA** and the main push skips the local hook (`TEST_SELECT_SKIP=1`).
3. Otherwise (main moved) the default branch is merged **into the spoke branch, on the spoke**,
   pushed, and the land waits for CI on the new tip before main fast-forwards to it. A conflict
   exits 4 (the resolution lane); a main that keeps moving stops after `LAND_SYNC_ROUNDS` (3).
4. If origin still advances between the CI check and the push, the recovery re-syncs the spoke
   against the new main and re-waits for CI (`LAND_LOCK_PUSH_RETRIES`).

`--skip-tests` only skips the hook's fast tier on the main push — it does **not** waive CI, which
is what the drain's `auto_land` passes. The land lock is held across the CI waits, so queued lands
serialize behind a slow CI run (`LAND_LOCK_WAIT_MAX`).

## Offline escape hatch: `--local-gate`

When CI is unreachable (no network, `gh` missing or unauthenticated), `--local-gate` on
`spoke-ready.sh` or `worktree-land.sh` runs the **former local full suite once** — the parallel
bulk under `-n auto -m "not serial"`, then the serial tail — instead of waiting for CI. It rides
the pre-push hook as `TEST_SELECT_CMD`, so the suite runs under the repo-integrity tripwire; a
missing or non-executable hook aborts rather than shipping ungated. It is recorded in the land
log (`suite: local full suite … (--local-gate)`, also in the issue-close comment) and in the
`ready/<N>` tag message (`LOCAL_GATE: …`). `worktree-land.sh --test-cmd <cmd>` gates with a custom
command the same way. `--local` micro-spokes (never pushed, so CI has never seen them) keep the
hub-side merge and the hook's fast tier.

## Parallelism

The mapped-files leg runs under `pytest-xdist`'s `-n auto`, guarded on the runner advertising
`-n numprocesses` in `pytest --help` so a checkout without `pytest-xdist` degrades to
single-process rather than erroring the push. The `--testmon` leg stays single-process: testmon
serializes a single-writer DB and `pytest --testmon -n auto` is unsupported.

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
- **Refresh.** `gate-sweep.sh` rebuilt the baseline after a green post-land full sweep, but
  that sweep only fires for a pruned green-tree stamp and the gate no longer mints stamps
  (CI is the gate, #378), so nothing refreshes it now: a baseline ages until it is rebuilt
  by hand. An aged baseline only widens testmon's impact set; it never wrong-greens.
- **Staleness.** testmon keys its `environment` row on `system_packages` +
  `python_version`, so a copied baseline whose `.venv` dep set differs (e.g. after a
  `requirements-dev.txt` bump) is invalidated and testmon would re-select the whole suite.
  The fast tier therefore **probes first**: `pytest --testmon --collect-only -q` lists what
  testmon would run, and past `TEST_SELECT_TESTMON_MAX` tests (default 200) — or if the count
  cannot be established — the leg is skipped with a loud note and CI covers it. A collection
  error blocks the push outright. The probe runs no tests but, like any testmon invocation, may
  update the database (it can only widen later selections, never produce a false green). (A tests-only
  push once ran ~5800 tests serially for 34 minutes: testmon cannot use xdist.) Rebuild the
  baseline after a dependency bump to get incremental selection back.

## Safe fallbacks

The fast tier never starts a run it cannot bound, and never waves a push through on missing evidence:

- **No pytest resolvable → the push is blocked** for any diff that demands tests (docs-only and
  exempt diffs need no runner). `TEST_SELECT_SKIP=1` is the explicit override.
- **testmon not installed, no testmon database, or an impact set past the cap → the mapped
  tests only**, with a note that CI is the full gate.
- **A diff range that can't be resolved → the mapped tests that exist plus the meta-test**, with
  the same note.
- **Selector missing or non-executable → the push is refused** (fail-closed): a missing gate must
  not silently ship untested code. Re-run `scripts/install-git-hooks.sh` to restore it.
- **CI is the backstop for all of the above:** a SHA cannot be marked ready or landed without a
  green full-suite run (or an explicit, logged `--local-gate`).

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
installed, a push runs no fast tier; CI still gates ready and land, and the
`--local-gate` / `--test-cmd` paths refuse to run without the hook.

## Landing

See [Gating ready and land](#gating-ready-and-land) above. The land never runs the suite; a
rejected push (a remote refusal, or the local gate of a `--local-gate` / `--test-cmd` land) rolls
the merge back with `git reset --keep`, leaving a clean hub.

| Flag | Effect |
| ---- | ------ |
| `--skip-tests` | Threads `TEST_SELECT_SKIP=1` — the hook's fast tier is skipped; CI is still required |
| `--local-gate` | Runs the former local full suite once via the hook instead of waiting for CI |
| `--test-cmd <cmd>` | Threads `TEST_SELECT_CMD=<cmd>` — the hook runs `<cmd>` instead of consulting CI |

## Why CI, not the local machine

The previous flow ran a ~6-minute suite inside every spoke push, again on the hub's main push, and
sometimes twice for one branch — on the same machine the agents were using. Moving the full run to
CI removes the contention and the main-level flakes from the push path while keeping a hard gate:
the SHA that reaches `main` is always one a CI run passed.
