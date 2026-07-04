"""afk/SKILL.md must launch hub-afk.sh via the canonical path (issues #74, #18).

The maiden run exposed a packaging/doc mismatch (issue #74, defect 6): the skill
launched ``.ai-toolkit/scripts/hub/hub-afk.sh`` — a ``hub/`` subdir form that exists
in NEITHER the ai-toolkit checkout nor a synced target. The lasting contract is the
issue #18 convention the other workflow skills follow: every launch example points at
``.ai-toolkit/scripts/hub-afk.sh``, the canonical location ``sync-to-repo.sh``
installs from the skill source ``shared/skills/hub/scripts/hub-afk.sh``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "shared" / "skills" / "afk" / "SKILL.md"

CANONICAL = ".ai-toolkit/scripts/hub-afk.sh"
SOURCE = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-afk.sh"

# A hub-afk.sh reference WITH a path (a leading directory) — bare `hub-afk.sh` mentions
# in prose are not launch commands and are excluded.
_HUB_AFK_PATH = re.compile(r"[\w./-]*/hub-afk\.sh")


def test_no_broken_hub_subdir_launch_path() -> None:
    text = SKILL.read_text()
    assert ".ai-toolkit/scripts/hub/hub-afk.sh" not in text, (
        "afk/SKILL.md references a hub-afk.sh path that exists in neither the checkout "
        "nor a synced target"
    )


def test_every_hub_afk_path_is_canonical() -> None:
    """Every pathed hub-afk.sh reference is the one canonical installed location."""
    text = SKILL.read_text()
    paths = set(_HUB_AFK_PATH.findall(text))
    assert paths, "afk/SKILL.md must reference the hub-afk.sh launch path"
    assert paths == {CANONICAL}, (
        f"afk/SKILL.md must launch hub-afk.sh only via {CANONICAL}, found: {sorted(paths)}"
    )


def test_canonical_path_has_a_sync_source() -> None:
    """The canonical path is backed by a real skill-source file sync installs from."""
    assert SOURCE.is_file(), (
        "shared/skills/hub/scripts/hub-afk.sh missing — the canonical "
        f"{CANONICAL} would sync from nowhere"
    )
