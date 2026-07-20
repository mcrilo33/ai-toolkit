# Bootstrap Test Suite

Stand up an ai-toolkit-conformant test scaffold in a **host** project so the
pre-push test gate (`shared/hooks/test-select.sh`, the single owner of test
execution) stops being pure friction. Until a fresh host has this scaffold, every
push either escalates to the full suite or fails closed — the gate assumes a
pytest runner, a `tests/**/test_*.py` layout, a mirror-test naming convention, a
`serial` marker, and testmon, none of which a bare repo has.

**Strictly opt-in.** Nothing runs this for you: there is no always-on rule and no
sync-time mutation of host trees. A host that never invokes it is completely
unaffected.

## When to use

Run it once, by hand, on a project that has adopted ai-toolkit's pre-push gate but
does not yet have a conformant test suite. Trigger phrases: "bootstrap the test
suite", "make this repo conformant with the gate", `/bootstrap-test-suite`.

## What it does

`scripts/bootstrap-test-suite.sh [target-dir]` (default: the current directory)
generates the scaffold. Every write **guards on absence** — a host-owned file is
never overwritten — so a re-run, or a repo that already has some of these files,
is safe: only the gaps are filled.

| Generated | Purpose |
|-----------|---------|
| `pyproject.toml` | Registers the `serial` marker and `testpaths`. Written only when neither `pyproject.toml` nor `pytest.ini` exists; otherwise the block is **printed** for you to paste (no in-place TOML mangling). |
| `tests/conftest.py` | Strips git's exported env (`GIT_DIR` &co) so a test spawning git can't retarget the real repo. |
| `scripts/example.sh` + `tests/unit/test_example.py` | A starter mirror pair seeding the naming convention. Delete or replace with real code. |
| `requirements-dev.txt` | `pytest`, `pytest-xdist`, `pytest-testmon` (missing lines appended, never duplicated). |
| `.test-select-exempt` | A starter list of paths with legitimately no test surface. |

Then: `pip install -r requirements-dev.txt`, and install the hooks with
`install-git-hooks.sh` (a separate step — this helper only builds the test
scaffold, it does not install the gate).

## The three contracts it encodes

### The `test_*.py`-names-its-target token convention

The gate's reverse index maps a changed **non-python** file to a test only when a
`test_*.py` names that file's basename as an exact token (in a docstring, a path
build, or a `subprocess` call). The starter `tests/unit/test_example.py` names
`example.sh`, so a change to `scripts/example.sh` selects that one test instead of
escalating to the full suite. Python files route to the testmon tier instead, which
is why the starter target is a shell script — it is the case the reverse index
exists for.

### The `serial` marker and the two-phase full run

The gate's full run is two-phase: the parallel bulk under `-m "not serial"` (xdist),
then a single-process tail under `-m serial`. Mark a test `@pytest.mark.serial`
**only** when it mutates shared git refs or otherwise cannot run under xdist workers.
On a fresh scaffold nothing is marked serial, so the serial leg collects nothing and
exits 5 ("no tests collected") — which the gate normalizes to green, so an empty
phase never blocks a clean push.

Do **not** put an xdist worker count (`-n auto`) in `addopts`. The gate adds one
itself where it is safe and must never pass it to the `pytest --testmon` leg —
testmon does not compose with xdist, so a global `addopts` worker count would break
the python-only tier.

### The default-to-full contract and testmon seeding

Anything the gate cannot prove is docs-only or maps to a test runs the full suite;
a missing testmon falls back to the full suite rather than silently skipping. This
helper does **not** seed a `.testmondata` baseline: that binary is interpreter- and
environment-specific, and the gate already seeds it on the first push per worktree
(its one full-suite run). Shipping a prebuilt DB would be stale the moment the
host's environment differs — so seeding is left to that first push.

## Assumptions

The host is a python (or python-containing) project. No attempt is made at
language-agnostic scaffolding; the scaffold targets the pytest-based gate the
toolkit ships.
