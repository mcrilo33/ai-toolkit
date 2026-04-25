"""Integration tests for scripts/sync-to-repo.sh.

Runs the sync script against a temporary git repo and verifies that
the correct files are generated with the right frontmatter and content.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-to-repo.sh"
SHARED_DIR = REPO_ROOT / "shared"


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def target_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo to sync into."""
    subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


def _run_sync(
    target: Path, tool: str = "all"
) -> subprocess.CompletedProcess[str]:
    """Run sync-to-repo.sh and return the result."""
    return subprocess.run(
        ["bash", str(SYNC_SCRIPT), str(target), tool],
        capture_output=True,
        text=True,
        check=True,
    )


# ── Copilot ───────────────────────────────────────────────


class TestSyncCopilot:
    """Verify Copilot file generation."""

    def test_copilot_instructions_created(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "copilot")

        dst = target_repo / ".github" / "copilot-instructions.md"
        assert dst.exists()

    def test_copilot_instructions_matches_guidelines(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "copilot")

        src = SHARED_DIR / "rules" / "guidelines.md"
        dst = target_repo / ".github" / "copilot-instructions.md"
        assert dst.read_text() == src.read_text()

    def test_copilot_instruction_files_have_frontmatter(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "copilot")

        instructions = target_repo / ".github" / "instructions"
        md_files = list(instructions.glob("*.instructions.md"))
        assert len(md_files) > 0

        for f in md_files:
            content = f.read_text()
            assert content.startswith("---"), (
                f"{f.name} missing frontmatter"
            )
            assert "applyTo:" in content, (
                f"{f.name} missing applyTo field"
            )

    def test_copilot_skills_created(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "copilot")

        skills_dir = target_repo / ".github" / "skills"
        skill_dirs = [
            d for d in skills_dir.iterdir() if d.is_dir()
        ]
        assert len(skill_dirs) > 0

        for d in skill_dirs:
            assert (d / "SKILL.md").exists(), (
                f"{d.name} missing SKILL.md"
            )

    def test_copilot_agents_copied(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "copilot")

        agents_src = SHARED_DIR / "agents"
        agent_files = list(agents_src.glob("*.agent.md"))
        if not agent_files:
            pytest.skip("No agent files in shared/agents/")

        agents_dir = target_repo / ".github" / "agents"
        for f in agent_files:
            assert (agents_dir / f.name).exists()

    def test_copilot_prompts_have_frontmatter(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "copilot")

        prompts = target_repo / ".github" / "prompts"
        prompt_files = list(prompts.glob("*.prompt.md"))
        if not prompt_files:
            pytest.skip("No prompt metadata entries")

        for f in prompt_files:
            content = f.read_text()
            assert content.startswith("---")


# ── Cursor ────────────────────────────────────────────────


class TestSyncCursor:
    """Verify Cursor file generation."""

    def test_cursor_rules_created(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "cursor")

        rules_dir = target_repo / ".cursor" / "rules"
        md_files = list(rules_dir.glob("*.md"))
        assert len(md_files) > 0

    def test_cursor_rules_have_frontmatter(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "cursor")

        rules_dir = target_repo / ".cursor" / "rules"
        for f in rules_dir.glob("*.md"):
            content = f.read_text()
            assert content.startswith("---"), (
                f"{f.name} missing frontmatter"
            )
            assert "description:" in content, (
                f"{f.name} missing description"
            )

    def test_cursor_skills_created(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "cursor")

        skills_dir = target_repo / ".cursor" / "skills"
        skill_dirs = [
            d for d in skills_dir.iterdir() if d.is_dir()
        ]
        assert len(skill_dirs) > 0


# ── Claude ────────────────────────────────────────────────


class TestSyncClaude:
    """Verify Claude file generation."""

    def test_claude_md_created(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "claude")

        assert (target_repo / "CLAUDE.md").exists()

    def test_claude_md_matches_guidelines(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "claude")

        src = SHARED_DIR / "rules" / "guidelines.md"
        dst = target_repo / "CLAUDE.md"
        assert dst.read_text() == src.read_text()

    def test_claude_rules_have_frontmatter(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        md_files = list(rules_dir.glob("*.md"))
        assert len(md_files) > 0

        for f in md_files:
            content = f.read_text()
            assert content.startswith("---"), (
                f"{f.name} missing frontmatter"
            )

    def test_claude_skills_created(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "claude")

        skills_dir = target_repo / ".claude" / "skills"
        skill_dirs = [
            d for d in skills_dir.iterdir() if d.is_dir()
        ]
        assert len(skill_dirs) > 0


# ── Cross-tool ────────────────────────────────────────────


class TestSyncAll:
    """Verify syncing all tools at once."""

    def test_sync_all_creates_all_tool_dirs(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "all")

        assert (target_repo / ".github").is_dir()
        assert (target_repo / ".cursor").is_dir()
        assert (target_repo / ".claude").is_dir()

    def test_idempotent_second_run(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "all")

        # Collect file contents after first run
        first: dict[str, str] = {}
        for f in target_repo.rglob("*.md"):
            first[str(f.relative_to(target_repo))] = (
                f.read_text()
            )

        _run_sync(target_repo, "all")

        # Contents should be identical after second run
        second: dict[str, str] = {}
        for f in target_repo.rglob("*.md"):
            second[str(f.relative_to(target_repo))] = (
                f.read_text()
            )

        assert first == second

    def test_content_preserved_after_frontmatter(
        self, target_repo: Path
    ) -> None:
        """Original rule body is intact after frontmatter injection."""
        _run_sync(target_repo, "copilot")

        src = SHARED_DIR / "rules" / "security.md"
        dst = (
            target_repo
            / ".github"
            / "instructions"
            / "security.instructions.md"
        )
        if not dst.exists():
            pytest.skip("security rule not generated")

        src_body = src.read_text()
        dst_content = dst.read_text()

        # Strip frontmatter (between --- markers) from dst
        parts = dst_content.split("---", 2)
        dst_body = parts[2].lstrip("\n") if len(parts) >= 3 else ""

        assert dst_body == src_body
