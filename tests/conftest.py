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

The list also covers ``GIT_NAMESPACE`` and the ``GIT_CONFIG`` / ``GIT_CONFIG_*``
family (issue #30): ``GIT_CONFIG_*`` redirects git's config resolution, so a
leaked value could still steer a child git process. ``GIT_CONFIG_*`` is a family
(``COUNT``, ``KEY``/``VALUE`` pairs, ``GLOBAL``, ``SYSTEM``) handled by the prefix
sweep below. The regression guard is ``tests/unit/test_git_env_isolation.py`` —
do not drop this strip without removing that test's reason to exist.
"""

from __future__ import annotations

import os

_LEAKED_GIT_HOOK_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CONFIG",
    "GIT_REFLOG_ACTION",
)

for _var in _LEAKED_GIT_HOOK_VARS:
    os.environ.pop(_var, None)

# GIT_CONFIG_* is an open-ended family (GIT_CONFIG_COUNT, GIT_CONFIG_KEY_n /
# GIT_CONFIG_VALUE_n, GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM) — sweep by prefix.
for _var in [_k for _k in os.environ if _k.startswith("GIT_CONFIG_")]:
    os.environ.pop(_var, None)
