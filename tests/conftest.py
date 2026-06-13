"""Project-wide pytest configuration.

Git exports a set of environment variables to the hooks it runs (``GIT_DIR``,
``GIT_INDEX_FILE``, …). When this suite is executed FROM a git hook — the
pre-push test gate (``test-select.sh``) is the live case — those variables are
inherited by pytest and, in turn, by any test that shells out to ``git``. A test
that builds a throwaway repo in ``tmp_path`` and runs ``git init`` / ``git
worktree add`` there would then have its commands silently retargeted at the
REAL repository (``GIT_DIR`` overrides the subprocess cwd), erroring the test and
corrupting the working repo.

Strip those variables from ``os.environ`` at import time — this conftest is
imported before any test module, so the cleanup lands before a test module's
top-level ``_GIT_ENV = {**os.environ, …}`` capture runs. Every test is then
hermetic regardless of whether the suite was launched from a shell or a git hook.
``GIT_EXEC_PATH`` is deliberately preserved (it locates git's own helpers, not a
target repo).
"""

from __future__ import annotations

import os

_LEAKED_GIT_HOOK_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REFLOG_ACTION",
)

for _var in _LEAKED_GIT_HOOK_VARS:
    os.environ.pop(_var, None)
