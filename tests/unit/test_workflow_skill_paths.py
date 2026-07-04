"""The workflow skills must reference the workflow scripts canonically.

The decision for issue #18 is a single canonical ``.ai-toolkit/scripts/``
location used in both the ai-toolkit checkout and a synced target — no
sync-time path rewrite. So the source SKILL.md files must already point every
worktree-script and hub-script invocation at ``.ai-toolkit/scripts/<name>``;
the repo-root ``scripts/worktree-*.sh`` and ``shared/skills/hub/scripts/``
forms (which only resolve in this checkout) must be gone. Covers every skill
that invokes a workflow script: hub/start-task/land plus quick, next-batch
and afk (whose path drift shipped a /next-batch that resolved nowhere).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "shared" / "skills"

WORKFLOW_SKILLS = ("hub", "start-task", "land", "quick", "next-batch", "afk")

# Markdown files that drive the hub/spoke/land workflow and must invoke the
# worktree scripts via the canonical .ai-toolkit/scripts/ path: the skills
# plus the planning-hub rule, which the hub skill loads.
WORKFLOW_DOCS = (
    *(SKILLS_DIR / name / "SKILL.md" for name in WORKFLOW_SKILLS),
    REPO_ROOT / "shared" / "rules" / "planning-hub.md",
)

# A repo-root scripts/worktree-*.sh reference NOT prefixed by .ai-toolkit/.
ROOT_WORKTREE_REF = re.compile(r"(?<!\.ai-toolkit/)scripts/worktree-\w+\.sh")


def _skill_text(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text()


@pytest.mark.parametrize("skill", WORKFLOW_SKILLS)
def test_no_repo_root_worktree_script_paths(skill: str) -> None:
    """No worktree script is referenced via the repo-root scripts/ path."""
    text = _skill_text(skill)
    leaked = ROOT_WORKTREE_REF.findall(text)
    assert not leaked, f"{skill}: repo-root worktree paths leaked: {leaked}"


@pytest.mark.parametrize("skill", WORKFLOW_SKILLS)
def test_no_shared_hub_status_invocation(skill: str) -> None:
    """hub-status.sh is never invoked via the shared/skills/ source path."""
    text = _skill_text(skill)
    assert "shared/skills/hub/scripts/" not in text, (
        f"{skill}: hub-status.sh invoked via shared/ path that won't resolve in a target"
    )


@pytest.mark.parametrize("skill", WORKFLOW_SKILLS)
def test_uses_canonical_scripts_dir(skill: str) -> None:
    """Each workflow skill references the canonical .ai-toolkit/scripts/ dir."""
    assert ".ai-toolkit/scripts/" in _skill_text(skill), (
        f"{skill}: no canonical .ai-toolkit/scripts/ reference"
    )


@pytest.mark.parametrize("doc", WORKFLOW_DOCS, ids=lambda p: p.stem)
def test_no_repo_root_worktree_paths_in_any_workflow_doc(doc: Path) -> None:
    """No workflow doc (skills + planning-hub rule) leaks a repo-root worktree path."""
    leaked = ROOT_WORKTREE_REF.findall(doc.read_text())
    assert not leaked, f"{doc.name}: repo-root worktree paths leaked: {leaked}"
