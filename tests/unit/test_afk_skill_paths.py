"""afk/SKILL.md must launch hub-afk.sh via a path that resolves (issue #74, defect 6).

The maiden run exposed a packaging/doc mismatch: the skill launched
``.ai-toolkit/scripts/hub/hub-afk.sh`` — a path that exists in NEITHER the ai-toolkit
checkout (where the supervisor lives at ``shared/skills/hub/scripts/hub-afk.sh``) nor a
synced target. The launch examples must point at the real script location, so every
``…/hub-afk.sh`` path the doc shows resolves to a file in the repo, and the broken
``hub/`` subdir form is gone.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "shared" / "skills" / "afk" / "SKILL.md"

# A hub-afk.sh reference WITH a path (a leading directory) — bare `hub-afk.sh` mentions
# in prose are not launch commands and are excluded.
_HUB_AFK_PATH = re.compile(r"[\w./-]*/hub-afk\.sh")


def test_no_broken_hub_subdir_launch_path() -> None:
    text = SKILL.read_text()
    assert ".ai-toolkit/scripts/hub/hub-afk.sh" not in text, (
        "afk/SKILL.md references a hub-afk.sh path that exists in neither the checkout "
        "nor a synced target"
    )


def test_every_hub_afk_path_resolves() -> None:
    text = SKILL.read_text()
    paths = set(_HUB_AFK_PATH.findall(text))
    assert paths, "afk/SKILL.md must reference the hub-afk.sh launch path"
    for p in paths:
        assert (REPO_ROOT / p).is_file(), f"afk/SKILL.md references non-existent path: {p}"
