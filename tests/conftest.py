"""Suite-wide pytest fixtures.

WHY THE GIT-HOOK ENV STRIP (issue #30 — DO NOT REMOVE):

Git exports ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_INDEX_FILE`` and friends into
the environment of its *native hooks*. The pre-push test gate runs ``pytest``
from inside such a hook (``test-select.sh`` / the ``red-proof-warn`` backstop), so
those vars are live in ``os.environ`` for the whole run. Many tests here shell out
to ``git`` against a throwaway tmpdir; a leaked ``GIT_DIR`` overrides their cwd, so
without this strip they operate on the REAL repository instead — which is how
issue #24's push moved the hub's ``main`` to a bogus ``chore: seed`` commit and
flipped ``core.bare``.

The strip runs in TWO places, on purpose:

  * at MODULE IMPORT (below) — conftest is imported before any test module is
    collected, so this guarantees module-level ``{**os.environ}`` snapshots (e.g.
    ``test_install_git_hooks._GIT_ENV``) cannot capture a leaked pointer; and
  * via an AUTOUSE fixture — re-strips per test in case anything re-sets the vars
    mid-session.

See docs/test-gate.md for the rationale. The regression guard is
tests/unit/test_git_env_isolation.py.
"""

from __future__ import annotations

import os

import pytest

# Vars git injects into native-hook environments. Any one of them can redirect a
# child git process away from the repo its cwd names. ``GIT_CONFIG_*`` is a family
# (COUNT, KEY/VALUE pairs, GLOBAL, SYSTEM) handled by the prefix sweep below.
_GIT_HOOK_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CONFIG",
)


def _strip_git_hook_env(environ: os._Environ[str] | dict[str, str]) -> None:
    """Remove every git-hook env var from *environ* in place (idempotent)."""
    for var in _GIT_HOOK_ENV_VARS:
        environ.pop(var, None)
    for key in [k for k in environ if k.startswith("GIT_CONFIG_")]:
        environ.pop(key, None)


# Strip at import time — before any test MODULE is imported during collection —
# so module-level os.environ snapshots start clean.
_strip_git_hook_env(os.environ)


@pytest.fixture(autouse=True)
def _isolate_git_hook_env() -> None:
    """Re-strip the git-hook env before every test (defends mid-session re-sets)."""
    _strip_git_hook_env(os.environ)
