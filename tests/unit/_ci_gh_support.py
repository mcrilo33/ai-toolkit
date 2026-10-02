"""A `gh` stub that answers the CI-status queries the ready/land scripts make (#378).

ready/<N> and the land script wait for a green CI run on a SHA, so any test that drives
them needs a `gh` on PATH — the suite must never reach the real GitHub. The stub prints
`runs_json` (default: one green run) for `gh run list` and ``$GH_JOBS`` for
`gh run view`, and appends every call's argv to ``$GH_LOG`` when set.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

CI_URL = "https://ci.example/runs/1"

GREEN_RUNS = f'[{{"status":"completed","conclusion":"success","url":"{CI_URL}","databaseId":1}}]'
PENDING_RUNS = f'[{{"status":"in_progress","conclusion":"","url":"{CI_URL}","databaseId":1}}]'
FAILED_RUNS = f'[{{"status":"completed","conclusion":"failure","url":"{CI_URL}","databaseId":1}}]'


def make_ci_gh(runs_json: str = GREEN_RUNS) -> Path:
    """Create a temp dir holding the stub `gh`; it is removed at interpreter exit."""
    d = Path(tempfile.mkdtemp(prefix="ghci-"))
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    gh = d / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "${GH_LOG:-/dev/null}"\n'
        'case "$1 $2" in\n'
        f"  \"run list\") printf '%s' '{runs_json}' ;;\n"
        '  "run view") printf "%s" "${GH_JOBS:-}" ;;\n'
        "esac\n"
    )
    gh.chmod(0o755)
    return d
