#!/usr/bin/env bash
# ensure-test-venv.sh — provision the pre-push gate's testmon/xdist deps into a project .venv.
#
# Why (issue #342): detect_pytest (shared/hooks/lib/utils.sh) resolves the bare `pytest` on
# PATH when no `.venv/bin/pytest` exists. On a host whose bare pytest lacks pytest-testmon /
# pytest-xdist, test-select.sh silently degrades the gate to the FULL suite, SINGLE-process —
# the worst case, run quietly. detect_pytest ALREADY prefers `.venv/bin/pytest`, so once a
# provisioned .venv exists the gate uses it automatically; this helper creates that .venv so
# the gate self-heals instead of depending on a human `pip install -r requirements-dev.txt`
# into the right interpreter.
#
# BEST-EFFORT, always-exit-0 (AFK Design Principle #6): a telemetry/provisioning step must
# never fail the operation it supports. Idempotent — a second run over a provisioned .venv is
# a clean no-op. FAIL-LOUD (AFK Design Principle #2): every provisioning ACTION (and every
# failure) prints a visible line, so a silent degrade never hides again.
#
# Usage: ensure-test-venv.sh [project-dir]   # default: the git toplevel, else the cwd
#   install-git-hooks.sh calls the sibling scripts/ensure-test-venv.sh (gate-adoption moment).
#   worktree-new.sh calls the shipped .ai-toolkit/scripts/ensure-test-venv.sh (each spawn).
set -uo pipefail   # NOT -e: this helper must never abort its caller.

DIR="${1:-}"
if [ -z "$DIR" ]; then
  DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi

loud() { printf '%s\n' "-> ensure-test-venv: $1"; }

# Exclude .venv from the target repo's LOCAL git exclude (per-clone, never committed), matching
# the toolkit-state entries install-git-hooks.sh / worktree-new.sh already append. Resolve the
# path via `git -C "$DIR"` and ABSOLUTIZE it (install-git-hooks.sh's guard): rev-parse prints a
# RELATIVE path, and a caller running from a different cwd (worktree-new.sh runs from REPO_ROOT
# and passes $WT_DIR) would otherwise append it to the WRONG repo, leaving .venv visible in the
# target's `git status`. Best-effort and appended at most once.
exclude_venv() {
  local exclude
  exclude="$(git -C "$DIR" rev-parse --git-path info/exclude 2>/dev/null || true)"
  [ -n "$exclude" ] || return 0
  case "$exclude" in /*) ;; *) exclude="$DIR/$exclude" ;; esac
  mkdir -p "$(dirname "$exclude")" 2>/dev/null || return 0
  grep -qxF '.venv/' "$exclude" 2>/dev/null || printf '%s\n' '.venv/' >> "$exclude"
}

# Skip cleanly if the project ships no dev-deps manifest: nothing to provision, no action taken.
[ -f "$DIR/requirements-dev.txt" ] || exit 0

VENV="$DIR/.venv"
PIP="$VENV/bin/pip"
PY="$VENV/bin/python"

if [ ! -x "$VENV/bin/pytest" ]; then
  # No .venv (or a partial one without pytest): create it and install the full dev deps.
  # --system-site-packages is deliberate — it inherits the host interpreter's packages, so only
  # the wheels the host is MISSING (typically just testmon/xdist) download, keeping a spawn fast.
  loud "provisioning $VENV (--system-site-packages) with the pre-push gate's testmon/xdist"
  if ! python3 -m venv --system-site-packages "$VENV"; then
    loud "python3 -m venv failed -- gate falls back to the full suite (install requirements-dev.txt manually)"
    exit 0
  fi
  if ! "$PIP" install -q -r "$DIR/requirements-dev.txt"; then
    loud "pip install -r requirements-dev.txt failed (network?) -- gate falls back to the full suite"
  fi
elif ! "$PY" -c 'import testmon, xdist' 2>/dev/null; then
  # .venv exists but the gate's plugins do not import: install ONLY those (cheap repair).
  loud "installing missing pytest-testmon/pytest-xdist into $VENV"
  if ! "$PIP" install -q 'pytest-testmon>=2,<3' 'pytest-xdist>=3,<4'; then
    loud "pip install of testmon/xdist failed (network?) -- gate falls back to the full suite"
  fi
fi

exclude_venv
exit 0
