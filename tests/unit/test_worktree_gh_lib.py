"""Mirror test for scripts/worktree-gh-lib.sh (issue #353 split).

`worktree-lib.sh` grew past the anti-monolith budget, so its GitHub lifecycle-label
mirror (the best-effort, time-bounded `wt_gh_*` layer, issue #236) was extracted into
this module, sourced by the thin `worktree-lib.sh` entry. The behavioural depth for
these helpers still runs through the entry in ``test_worktree_lib.py``; this file is
the module's own mirror: it pins the extraction contract — the public functions and
label constants live HERE and the entry sources them so consumers keep sourcing
``worktree-lib.sh`` unchanged — plus one behavioural smoke proving the module's
functions are reachable through the entry under a logging `gh` stub.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WT_LIB = REPO_ROOT / "scripts" / "worktree-lib.sh"
MODULE = REPO_ROOT / "scripts" / "worktree-gh-lib.sh"

# The public helpers issue #353 moved out of worktree-lib.sh into this module.
GH_FUNCTIONS = (
    "wt_gh_lifecycle_enabled",
    "wt_gh",
    "wt_gh_ensure_label",
    "wt_gh_set_status_label",
    "wt_gh_apply_dispatch_labels",
    "wt_gh_clear_lifecycle_labels",
    "wt_gh_dispatch_comment",
)

# The label taxonomy constants moved with the functions that read them.
GH_CONSTANTS = ("WT_GH_STATUS_LABELS", "WT_GH_MODE_LABELS", "WT_GH_LANE_LABELS")

_GH_STUB = '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GH_LOG"\nexit 0\n'


def _defines(text: str, fn: str) -> bool:
    return re.search(rf"^{re.escape(fn)}\(\)", text, re.MULTILINE) is not None


def test_module_file_exists_and_is_executable() -> None:
    assert MODULE.is_file(), "scripts/worktree-gh-lib.sh missing"
    assert os.access(MODULE, os.X_OK), "worktree-gh-lib.sh is not executable"


def test_module_defines_the_gh_public_functions_and_constants() -> None:
    text = MODULE.read_text()
    missing_fns = [fn for fn in GH_FUNCTIONS if not _defines(text, fn)]
    assert missing_fns == [], f"worktree-gh-lib.sh must define {missing_fns}"
    missing_consts = [c for c in GH_CONSTANTS if f"{c}=" not in text]
    assert missing_consts == [], f"worktree-gh-lib.sh must define {missing_consts}"


def test_gh_functions_are_not_duplicated_in_core() -> None:
    # A MOVE, not a copy: every gh function is defined ONLY in this module, never left
    # behind (or re-copied) in the entry. A duplicate in core would be dead code
    # shadowed by the module (sourced last), invisible to behaviour tests — this catches it.
    core = WT_LIB.read_text()
    duped = [fn for fn in GH_FUNCTIONS if _defines(core, fn)]
    assert duped == [], (
        f"{duped} are duplicated in worktree-lib.sh — they were extracted to "
        "worktree-gh-lib.sh and must not remain (or be re-copied) in the entry"
    )


def test_entry_sources_the_module_so_consumers_are_unchanged() -> None:
    # End-to-end: sourcing ONLY the entry must make every gh function available,
    # proving the 24 consumers keep sourcing worktree-lib.sh with no edit.
    query = "; ".join(f"declare -F {fn} >/dev/null || echo MISSING {fn}" for fn in GH_FUNCTIONS)
    result = subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; {query}'],
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": "UTC"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "", f"entry did not expose gh functions: {result.stdout!r}"


def test_apply_dispatch_labels_reachable_through_entry(tmp_path: Path) -> None:
    # Behavioural smoke: a moved function runs through the entry under a logging `gh`
    # stub and emits the expected label edit, proving the module is wired live.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_GH_STUB)
    gh.chmod(0o755)
    log = tmp_path / "gh-calls.log"
    env = {
        **os.environ,
        "TZ": "UTC",
        "GH_LOG": str(log),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "WT_GH_SEED_DIR": str(tmp_path / "seed"),
    }
    env.pop("AI_TOOLKIT_GH_LIFECYCLE_LABELS", None)

    result = subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; wt_gh_apply_dispatch_labels 7 afk spoke'],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    edit = [c for c in calls if c.startswith("issue edit")]
    assert len(edit) == 1, f"expected one issue-edit call, got {calls}"
    assert "--add-label status:in-progress" in edit[0]
    assert "--add-label mode:afk" in edit[0]
    assert "--add-label lane:spoke" in edit[0]
