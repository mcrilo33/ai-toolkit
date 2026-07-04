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

Telemetry isolation works the same way and for the same reason (issue #49). The
span recorder in ``shared/hooks/lib/telemetry.sh`` writes to
``${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}`` whenever
``AI_TOOLKIT_TELEMETRY=1``. A dev shell (or the pre-push gate) commonly exports
``=1``, so a test that shells out to a hook — or snapshots ``os.environ`` at
import, as ``test_worktree_new`` does — leaked fixture spans into the REAL log,
where they surfaced as fake spokes in the observability dashboard. Neutralize for
the whole session: drop the opt-in (the recorder no-ops without ``=1``), and
redirect the dir to a sandbox so even a test that re-enables telemetry without its
own dir can never hit the real default. The regression guard is
``tests/unit/test_telemetry_env_isolation.py``.

That recorder has a SECOND, INDEPENDENT sink — the OTLP/Langfuse fan-out (issue #83) —
gated solely on ``AI_TOOLKIT_OTEL_SPAN_ENDPOINT`` (NOT on ``AI_TOOLKIT_TELEMETRY``), which
``curl``-POSTs the span to a live collector. The ``=1`` strip never covered it, so a test
inheriting a spoke's exported endpoint leaked fixture spans straight to Langfuse. A network
endpoint has no sandbox to redirect to, so the cure is to strip the whole export family —
done below, same import-time mechanism, same regression guard.
"""

from __future__ import annotations

import os
import tempfile

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

# Telemetry isolation (issue #49) — see the module docstring. Drop the opt-in so the
# recorder no-ops, and redirect the dir to a throwaway sandbox as belt-and-suspenders
# against any test that re-enables telemetry without supplying its own dir.
for _var in ("AI_TOOLKIT_TELEMETRY", "AI_TOOLKIT_SPAN_LOG"):
    os.environ.pop(_var, None)
os.environ["AI_TOOLKIT_TELEMETRY_DIR"] = tempfile.mkdtemp(prefix="ai-toolkit-test-telemetry-")

# The OTLP/Langfuse fan-out sink (issue #83) is a SECOND, INDEPENDENT sink:
# telemetry.sh curl-POSTs a span whenever AI_TOOLKIT_OTEL_SPAN_ENDPOINT is set — gated on
# that var ALONE, NOT on AI_TOOLKIT_TELEMETRY. The strip above never covered it, so a test
# that inherits a spoke/dev shell's exported endpoint and shells out to telemetry.sh POSTed
# fixture spans straight to the live collector -> Langfuse (recurring fake spokes). There is
# no sandbox to redirect a network endpoint to, so stripping the whole export family IS the
# cure: drop AI_TOOLKIT_OTEL_SPAN_ENDPOINT (the shell sink's gate), the AI_TOOLKIT_OTEL
# native-OTel opt-in family, BRIDGE_OTLP_ENDPOINT, and the OTEL_EXPORTER_OTLP_* family (swept
# by prefix). The regression guard is tests/unit/test_telemetry_env_isolation.py.
for _var in (
    "AI_TOOLKIT_OTEL_SPAN_ENDPOINT",
    "AI_TOOLKIT_OTEL",
    "AI_TOOLKIT_OTEL_BODY_DIR",
    "BRIDGE_OTLP_ENDPOINT",
):
    os.environ.pop(_var, None)
for _var in [_k for _k in os.environ if _k.startswith("OTEL_EXPORTER_OTLP_")]:
    os.environ.pop(_var, None)

# Hub-side Langfuse auth resolution (issue #127): wt_resolve_langfuse_auth
# (worktree-lib.sh, called by worktree-land.sh / worktree-quick.sh) resolves auth from
# the env OR ${AFK_TELEMETRY_CONF:-~/.afk-telemetry} and then RE-DEFAULTS the OTLP span
# endpoint — so the endpoint strip above is not enough: a test shelling out to a land
# would read the operator's REAL conf and re-open the export channel to a live
# collector. Strip the auth pair and pin the conf var at a nonexistent sandbox path so
# the resolver can never resolve inside the suite; a test that wants auth opts in with
# its own tmp conf. Same regression guard file as the strips above.
for _var in ("LANGFUSE_BASIC_AUTH", "LANGFUSE_HOST"):
    os.environ.pop(_var, None)
os.environ["AFK_TELEMETRY_CONF"] = os.path.join(
    tempfile.mkdtemp(prefix="ai-toolkit-test-afk-"), "no-such-conf"
)
