#!/usr/bin/env bash
# bootstrap-test-suite.sh (issue #335) — stand up an ai-toolkit-conformant test
# scaffold in a host project so the pre-push gate (shared/hooks/test-select.sh)
# stops being pure friction on a fresh host.
#
# What it emits (all writes GUARD ON ABSENCE — a host-owned file is never
# clobbered, so a re-run and a repo that already has some of these are both safe):
#   - pyproject.toml         registers the `serial` marker (only when neither
#                            pyproject.toml nor pytest.ini exists; otherwise the
#                            block is printed for the host to paste, never mangled
#                            in place)
#   - tests/conftest.py      strips git's exported env (test isolation)
#   - scripts/example.sh     a starter script + its mirror test, to seed the
#   - tests/unit/test_example.py   `test_*.py`-names-its-target token convention
#   - requirements-dev.txt   pytest, pytest-xdist, pytest-testmon (missing lines
#                            appended, never duplicated)
#   - .test-select-exempt    a starter exempt list
#
# NOT its job: installing the hooks themselves (that is install-git-hooks.sh), and
# seeding a testmon DB — the gate seeds .testmondata on the first push per worktree
# (its full-suite run), so no opaque, interpreter-specific binary is shipped here.
#
# Usage: bootstrap-test-suite.sh [target-dir]   (default: current directory)
set -euo pipefail

TARGET="${1:-.}"

log() { printf 'bootstrap-test-suite: %s\n' "$*" >&2; }

# Write a file from stdin, but only when it does not already exist. Creates
# parent dirs. This is the guard-on-absence rule every emitter goes through.
emit_if_absent() {
  local dest="$1"
  if [ -e "$dest" ]; then
    log "exists, leaving as-is: ${dest#"$TARGET"/}"
    cat >/dev/null
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  cat >"$dest"
  log "created ${dest#"$TARGET"/}"
}

# Append a requirement only when no line already names that package. `grep` runs
# only on an existing file, so a missing requirements-dev.txt is created by the
# first append. The token boundary keeps `pytest` from matching `pytest-xdist`.
ensure_req() {
  local pkg="$1" file="$TARGET/requirements-dev.txt"
  if [ -e "$file" ] && grep -qiE "^${pkg}([<>=!~;[:space:]]|\$)" "$file"; then
    return 0
  fi
  printf '%s\n' "$pkg" >>"$file"
  log "requirements-dev.txt += $pkg"
}

mkdir -p "$TARGET"

# ── pytest config: register the `serial` marker ────────────────────────────────
# NO `-n auto` in addopts on purpose: the gate supplies `-n auto` itself on the
# full/selected legs and NEVER on the `pytest --testmon` leg (testmon does not
# compose with xdist). Baking it in would make the python/testmon tier error.
PYPROJECT_BLOCK="$(
  cat <<'EOF'
[tool.pytest.ini_options]
# The pre-push gate's two-phase full run splits on this marker: the parallel bulk
# runs `-m "not serial"` under xdist, then the ref-mutating tail runs `-m serial`
# single-process. Mark a test @pytest.mark.serial only when it mutates shared git
# refs or otherwise cannot run under xdist workers. Do NOT put an xdist worker
# count in addopts: the gate adds one where it is safe and must never pass it to
# the --testmon leg (testmon does not compose with xdist).
markers = [
    "serial: run single-process (never under xdist); mutates shared refs/state",
]
testpaths = ["tests"]
EOF
)"

if [ -e "$TARGET/pyproject.toml" ] || [ -e "$TARGET/pytest.ini" ]; then
  log "pytest config already present — add this block to register the serial marker:"
  printf '%s\n' "$PYPROJECT_BLOCK" >&2
else
  printf '%s\n' "$PYPROJECT_BLOCK" | emit_if_absent "$TARGET/pyproject.toml"
fi

# ── tests/conftest.py: strip git's exported env so a test spawning git cannot ──
# retarget the real repo (mirrors ai-toolkit's own isolation).
emit_if_absent "$TARGET/tests/conftest.py" <<'EOF'
"""Strip git's exported env so a test that spawns git can't retarget the real repo."""

from __future__ import annotations

import pytest

_GIT_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


@pytest.fixture(autouse=True)
def _strip_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _GIT_VARS:
        monkeypatch.delenv(var, raising=False)
EOF

# ── starter mirror pair: a NON-python target on purpose ────────────────────────
# The reverse index (SELECTED tier) maps a changed non-python file to the tests
# that name its basename as an exact token. A python target would route to the
# testmon tier instead, so the starter target is a .sh to demonstrate the tier
# this scaffold unlocks.
emit_if_absent "$TARGET/scripts/example.sh" <<'EOF'
#!/usr/bin/env bash
# Starter example script — delete or replace once you have real scripts.
# Its mirror test (tests/unit/test_example.py) names this file's basename
# (example.sh) as an exact token, so the pre-push gate's reverse index maps a
# change here to that test instead of escalating to the full suite.
echo "hello from example.sh"
EOF

emit_if_absent "$TARGET/tests/unit/test_example.py" <<'EOF'
"""Starter mirror test — names scripts/example.sh so the reverse index maps it.

The convention: a test names the file it covers (basename as an exact token) so
the pre-push gate can select it for a change to that file. Delete or replace this
pair with real tests.
"""

from __future__ import annotations

import subprocess


def test_example_script_greets() -> None:
    result = subprocess.run(
        ["bash", "scripts/example.sh"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "hello" in result.stdout
EOF

# ── requirements-dev.txt: the gate's python tier depends on these ──────────────
ensure_req "pytest"
ensure_req "pytest-xdist"
ensure_req "pytest-testmon"

# ── .test-select-exempt: paths with legitimately no test surface ───────────────
emit_if_absent "$TARGET/.test-select-exempt" <<'EOF'
# Paths the pre-push test gate (test-select.sh) ignores and the control-plane
# coverage meta-test accepts without a referencing test. One path per line; a
# trailing / marks a directory subtree. Every entry must exist on disk.
.gitignore
EOF

log "done. Next: pip install -r requirements-dev.txt, then install the hooks (install-git-hooks.sh)."
