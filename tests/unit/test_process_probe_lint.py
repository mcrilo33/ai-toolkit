"""Convention lint: control-plane scripts must not probe processes bare.

Issue #189 routes every locale-fragile process probe through the shared helpers
``wt_pgrep`` / ``wt_ps_start_epoch`` (in ``scripts/worktree-lib.sh``). This guard
fails the moment a new *bare* ``pgrep -f`` or ``ps -o lstart`` re-appears anywhere
in the control-plane shell scripts, so the class fix cannot silently regress one
call site at a time (the exact way the bug kept coming back). It mirrors the other
grep-the-sources convention tests in this suite.

Only ``worktree-lib.sh`` — where the hardened helpers legitimately wrap the raw
tools under ``LC_ALL=C`` — is exempt. Everyone else goes through the helpers.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories holding control-plane shell scripts (scripts, hook scripts, hub scripts).
SCRIPT_DIRS = [
    REPO_ROOT / "scripts",
    REPO_ROOT / "shared" / "hooks",
    REPO_ROOT / "shared" / "skills" / "hub" / "scripts",
]

# The sanctioned home of the hardened helpers: it alone may call the raw tools.
EXEMPT = {REPO_ROOT / "scripts" / "worktree-lib.sh"}

# A bare `pgrep`/`pkill` (NOT `wt_pgrep`): argv matching dies "illegal byte
# sequence" on non-ASCII argv under a non-C locale and self-matches a monitor
# loop's own argv. Every such probe now has a wt_pgrep path (kill = wt_pgrep then
# `kill`), so none survive outside the lib.
RAW_PGREP = re.compile(r"(?<![\w-])p(?:grep|kill)\b")
# A bare `ps` (NOT `wt_ps_start_epoch`) reading the locale-formatted start time.
# Tolerates a glued flag (`-olstart`) and extra columns (`-o pid,lstart`).
RAW_PS_LSTART = re.compile(r"(?<![\w-])ps\b[^\n]*?-o\s*[\w,]*lstart")

# Characters that (with whitespace and line start) begin a new shell word, so a
# following `#` opens a comment: `;` `|` `&` `(`.
_WORD_BOUNDARY = " \t;|&("


def _strip_comment(line: str) -> str:
    """Drop a shell comment: a ``#`` at start-of-word (line start or after a word
    boundary such as whitespace, ``;``, ``|``, ``&``, ``(``).

    Leaves ``${v#pat}``, ``$#``, and any ``#`` mid-word intact so parameter
    expansions are not mistaken for comments.
    """
    prev_boundary = True  # line start counts as start-of-word
    for i, ch in enumerate(line):
        if ch == "#" and prev_boundary:
            return line[:i]
        prev_boundary = ch in _WORD_BOUNDARY
    return line


def _control_plane_scripts() -> list[Path]:
    files = [p for d in SCRIPT_DIRS for p in d.rglob("*.sh") if p not in EXEMPT]
    return sorted(files)


def test_found_the_control_plane_scripts() -> None:
    # Guard against a stale glob silently scanning nothing (a vacuous green).
    assert len(_control_plane_scripts()) > 10


def test_no_bare_process_probes_outside_the_helper_lib() -> None:
    violations: list[str] = []
    for path in _control_plane_scripts():
        for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
            code = _strip_comment(raw)
            if RAW_PGREP.search(code):
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: bare `pgrep`/`pkill` — use wt_pgrep"
                )
            if RAW_PS_LSTART.search(code):
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: bare `ps -o lstart` — use wt_ps_start_epoch"
                )
    assert not violations, "bare, locale-fragile process probes found:\n" + "\n".join(violations)


def test_helpers_are_defined_in_the_lib() -> None:
    # The exempt lib must actually define both helpers, so the guard above is not
    # exempting a file that no longer carries the sanctioned implementation.
    lib = (REPO_ROOT / "scripts" / "worktree-lib.sh").read_text()
    assert re.search(r"^wt_pgrep\s*\(\)", lib, re.MULTILINE), (
        "wt_pgrep not defined in worktree-lib.sh"
    )
    assert re.search(r"^wt_ps_start_epoch\s*\(\)", lib, re.MULTILINE), (
        "wt_ps_start_epoch not defined in worktree-lib.sh"
    )
