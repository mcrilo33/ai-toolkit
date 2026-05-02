"""Integration tests for scripts/sync-to-repo.sh.

Runs the sync script against a temporary git repo and verifies that
the correct files are generated with the right frontmatter and content.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-to-repo.sh"
SHARED_DIR = REPO_ROOT / "shared"

# ── Expected rules derived from metadata ─────────────────

# Rules that have the tool-relevant field in metadata.yml:
# Copilot: emits all rules that have at least one of (name, description, applyTo)
# Cursor:  emits all rules that have at least one of (description, globs, alwaysApply)
# Claude:  emits only rules that have the 'paths' field

# All rules in metadata — guidelines is ALSO generated as an instruction file
ALL_RULE_NAMES = {
    "guidelines",
    "security",
    "code-quality",
    "python-style",
    "gitignore-template",
    "markdown-style",
    "mermaid-conventions",
    "pytest-conventions",
    "workflow",
    "github-actions",
    "library-research",
    "agent-orchestration",
}

# Rules that have no applyTo/globs in metadata
RULES_WITHOUT_GLOB = {"library-research"}

# Rules that define 'paths' in metadata → generated as Claude rules
CLAUDE_RULES_WITH_PATHS = {
    "code-quality",
    "python-style",
    "gitignore-template",
    "markdown-style",
    "mermaid-conventions",
    "pytest-conventions",
    "github-actions",
}

# All skill directories in shared/skills/ that contain SKILL.md
ALL_SKILL_NAMES = {
    d.name
    for d in (SHARED_DIR / "skills").iterdir()
    if d.is_dir() and (d / "SKILL.md").exists()
}

# Skills that have metadata entries (parsed from metadata.yml)
SKILLS_WITH_METADATA: set[str] = set()
_skills_meta = SHARED_DIR / "skills" / "metadata.yml"
if _skills_meta.exists():
    SKILLS_WITH_METADATA = set(yaml.safe_load(_skills_meta.read_text()).keys())

# Skills that define allowed-tools in metadata
SKILLS_WITH_ALLOWED_TOOLS = {
    name
    for name, data in (
        yaml.safe_load(_skills_meta.read_text()).items()
        if _skills_meta.exists()
        else []
    )
    if "allowed-tools" in data
}

# All agent .md files in shared/agents/ (excluding metadata.yml)
ALL_AGENT_NAMES = {
    f.stem
    for f in (SHARED_DIR / "agents").iterdir()
    if f.is_file() and f.suffix == ".md" and f.name != "metadata.yml"
}

# Agents that have metadata entries (parsed from metadata.yml)
AGENTS_WITH_METADATA: set[str] = set()
_agents_meta = SHARED_DIR / "agents" / "metadata.yml"
if _agents_meta.exists():
    AGENTS_WITH_METADATA = set(yaml.safe_load(_agents_meta.read_text()).keys())

# Agents that define disallowedTools in metadata
AGENTS_WITH_DISALLOWED_TOOLS = {
    name
    for name, data in (
        yaml.safe_load(_agents_meta.read_text()).items()
        if _agents_meta.exists()
        else []
    )
    if "disallowedTools" in data
}

# Skills with subdirectories (references, scripts, templates, assets)
SKILL_SUBDIRS = ("references", "scripts", "templates", "assets")
SKILLS_WITH_SUBDIRS = {
    d.name: [sub for sub in SKILL_SUBDIRS if (d / sub).is_dir()]
    for d in (SHARED_DIR / "skills").iterdir()
    if d.is_dir()
    and (d / "SKILL.md").exists()
    and any((d / sub).is_dir() for sub in SKILL_SUBDIRS)
}


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (between --- markers) and leading blank line."""
    parts = text.split("---", 2)
    return parts[2].lstrip("\n") if len(parts) >= 3 else text


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract frontmatter fields as a flat dict."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    fm_block = parts[1].strip()
    result: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


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


def _run_sync(target: Path, tool: str = "all") -> subprocess.CompletedProcess[str]:
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

    def test_copilot_instruction_files_have_frontmatter(
        self, target_repo: Path
    ) -> None:
        _run_sync(target_repo, "copilot")

        instructions = target_repo / ".github" / "instructions"
        md_files = list(instructions.glob("*.instructions.md"))
        assert len(md_files) > 0

        for f in md_files:
            content = f.read_text()
            assert content.startswith("---"), f"{f.name} missing frontmatter"
            rule_name = f.stem.removesuffix(".instructions")
            if rule_name not in RULES_WITHOUT_GLOB:
                assert "applyTo:" in content, f"{f.name} missing applyTo field"

    def test_copilot_skills_created(self, target_repo: Path) -> None:
        _run_sync(target_repo, "copilot")

        skills_dir = target_repo / ".github" / "skills"
        skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
        assert len(skill_dirs) > 0

        for d in skill_dirs:
            assert (d / "SKILL.md").exists(), f"{d.name} missing SKILL.md"

    def test_copilot_agents_created(self, target_repo: Path) -> None:
        _run_sync(target_repo, "copilot")

        agents_dir = target_repo / ".github" / "agents"
        agent_files = list(agents_dir.glob("*.agent.md"))
        assert len(agent_files) > 0

    def test_copilot_prompts_have_frontmatter(self, target_repo: Path) -> None:
        _run_sync(target_repo, "copilot")

        prompts = target_repo / ".github" / "prompts"
        prompt_files = list(prompts.glob("*.prompt.md"))
        if not prompt_files:
            pytest.skip("No prompt metadata entries")

        for f in prompt_files:
            content = f.read_text()
            assert content.startswith("---")


# ── Copilot Rules ─────────────────────────────────────────


class TestCopilotRules:
    """Verify Copilot instruction rules: exact set, fields, and body."""

    def test_exact_rule_set_generated(self, target_repo: Path) -> None:
        """Every metadata rule appears as an instruction file."""
        _run_sync(target_repo, "copilot")

        instructions = target_repo / ".github" / "instructions"
        generated = {
            f.stem.removesuffix(".instructions")
            for f in instructions.glob("*.instructions.md")
        }
        assert generated == ALL_RULE_NAMES

    def test_frontmatter_has_name_and_applyTo(self, target_repo: Path) -> None:
        """Each instruction file has name and applyTo (unless it has no glob)."""
        _run_sync(target_repo, "copilot")

        instructions = target_repo / ".github" / "instructions"
        for f in instructions.glob("*.instructions.md"):
            rule_name = f.stem.removesuffix(".instructions")
            fm = _parse_frontmatter(f.read_text())
            assert "name" in fm, f"{f.name} missing 'name'"
            if rule_name not in RULES_WITHOUT_GLOB:
                assert "applyTo" in fm, f"{f.name} missing 'applyTo'"

    def test_frontmatter_has_description(self, target_repo: Path) -> None:
        _run_sync(target_repo, "copilot")

        instructions = target_repo / ".github" / "instructions"
        for f in instructions.glob("*.instructions.md"):
            fm = _parse_frontmatter(f.read_text())
            assert "description" in fm, f"{f.name} missing 'description'"
            assert fm["description"], f"{f.name} has empty description"

    def test_body_matches_source_for_every_rule(self, target_repo: Path) -> None:
        """Body after frontmatter matches the original shared rule."""
        _run_sync(target_repo, "copilot")

        instructions = target_repo / ".github" / "instructions"
        for f in instructions.glob("*.instructions.md"):
            rule_name = f.stem.removesuffix(".instructions")
            src = SHARED_DIR / "rules" / f"{rule_name}.md"
            assert src.exists(), f"Source missing: {src}"

            dst_body = _strip_frontmatter(f.read_text())
            assert dst_body == src.read_text(), f"{f.name} body differs from source"

    def test_applyTo_value_matches_metadata(self, target_repo: Path) -> None:
        """applyTo value in generated file matches metadata.yml."""
        _run_sync(target_repo, "copilot")

        meta = yaml.safe_load((SHARED_DIR / "rules" / "metadata.yml").read_text())
        instructions = target_repo / ".github" / "instructions"
        for f in instructions.glob("*.instructions.md"):
            rule_name = f.stem.removesuffix(".instructions")
            expected = meta.get(rule_name, {}).get("applyTo", "")
            fm = _parse_frontmatter(f.read_text())
            assert fm.get("applyTo", "") == str(expected), f"{f.name}: applyTo mismatch"


# ── Cursor ────────────────────────────────────────────────


class TestSyncCursor:
    """Verify Cursor file generation."""

    def test_cursor_rules_created(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        rules_dir = target_repo / ".cursor" / "rules"
        mdc_files = list(rules_dir.glob("*.mdc"))
        assert len(mdc_files) > 0

    def test_cursor_rules_have_frontmatter(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        rules_dir = target_repo / ".cursor" / "rules"
        for f in rules_dir.glob("*.mdc"):
            content = f.read_text()
            assert content.startswith("---"), f"{f.name} missing frontmatter"
            assert "description:" in content, f"{f.name} missing description"

    def test_cursor_skills_created(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        skills_dir = target_repo / ".cursor" / "skills"
        skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
        assert len(skill_dirs) > 0


# ── Cursor Rules ──────────────────────────────────────────


class TestCursorRules:
    """Verify Cursor rules: exact set, fields, and body."""

    def test_exact_rule_set_generated(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        rules_dir = target_repo / ".cursor" / "rules"
        generated = {f.stem for f in rules_dir.glob("*.mdc")}
        assert generated == ALL_RULE_NAMES

    def test_frontmatter_has_description_and_alwaysApply(
        self, target_repo: Path
    ) -> None:
        """Each Cursor rule has description and alwaysApply."""
        _run_sync(target_repo, "cursor")

        rules_dir = target_repo / ".cursor" / "rules"
        for f in rules_dir.glob("*.mdc"):
            fm = _parse_frontmatter(f.read_text())
            assert "description" in fm, f"{f.name} missing 'description'"
            assert "alwaysApply" in fm, f"{f.name} missing 'alwaysApply'"

    def test_globs_field_present_when_expected(self, target_repo: Path) -> None:
        """Rules with globs in metadata have globs in frontmatter."""
        _run_sync(target_repo, "cursor")

        meta = yaml.safe_load((SHARED_DIR / "rules" / "metadata.yml").read_text())
        rules_dir = target_repo / ".cursor" / "rules"
        for f in rules_dir.glob("*.mdc"):
            rule_name = f.stem
            expected_globs = meta.get(rule_name, {}).get("globs")
            fm = _parse_frontmatter(f.read_text())
            if expected_globs:
                assert fm.get("globs") == str(
                    expected_globs
                ), f"{f.name}: globs mismatch"

    def test_alwaysApply_value_matches_metadata(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        meta = yaml.safe_load((SHARED_DIR / "rules" / "metadata.yml").read_text())
        rules_dir = target_repo / ".cursor" / "rules"
        for f in rules_dir.glob("*.mdc"):
            rule_name = f.stem
            expected = str(meta.get(rule_name, {}).get("alwaysApply", "")).lower()
            fm = _parse_frontmatter(f.read_text())
            assert (
                fm.get("alwaysApply", "").lower() == expected
            ), f"{f.name}: alwaysApply mismatch"

    def test_body_matches_source_for_every_rule(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        rules_dir = target_repo / ".cursor" / "rules"
        for f in rules_dir.glob("*.mdc"):
            src = SHARED_DIR / "rules" / f"{f.stem}.md"
            assert src.exists(), f"Source missing: {src}"

            dst_body = _strip_frontmatter(f.read_text())
            assert dst_body == src.read_text(), f"{f.name} body differs from source"


# ── Claude ────────────────────────────────────────────────


class TestSyncClaude:
    """Verify Claude file generation."""

    def test_claude_rules_have_frontmatter(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        md_files = list(rules_dir.glob("*.md"))
        assert len(md_files) > 0

        for f in md_files:
            content = f.read_text()
            assert content.startswith("---"), f"{f.name} missing frontmatter"

    def test_claude_skills_created(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        skills_dir = target_repo / ".claude" / "skills"
        skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
        assert len(skill_dirs) > 0


# ── Claude Rules ──────────────────────────────────────────


class TestClaudeRules:
    """Verify Claude rules: exact set, fields, and body."""

    def test_exact_rule_set_generated(self, target_repo: Path) -> None:
        """Only rules with 'paths' in metadata are generated."""
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        generated = {f.stem for f in rules_dir.glob("*.md")}
        assert generated == CLAUDE_RULES_WITH_PATHS

    def test_frontmatter_has_paths_field(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        for f in rules_dir.glob("*.md"):
            fm = _parse_frontmatter(f.read_text())
            assert "paths" in fm, f"{f.name} missing 'paths' field"
            assert fm["paths"], f"{f.name} has empty paths"

    def test_paths_value_matches_metadata(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        meta = yaml.safe_load((SHARED_DIR / "rules" / "metadata.yml").read_text())
        rules_dir = target_repo / ".claude" / "rules"
        for f in rules_dir.glob("*.md"):
            rule_name = f.stem
            expected = str(meta.get(rule_name, {}).get("paths", ""))
            fm = _parse_frontmatter(f.read_text())
            assert fm.get("paths", "") == expected, f"{f.name}: paths mismatch"

    def test_rules_without_paths_excluded(self, target_repo: Path) -> None:
        """guidelines, security, workflow, library-research have no paths → not generated."""
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        generated = {f.stem for f in rules_dir.glob("*.md")}
        excluded = {"guidelines", "security", "workflow", "library-research"}
        assert generated.isdisjoint(
            excluded
        ), f"Unexpected rules in Claude: {generated & excluded}"

    def test_body_matches_source_for_every_rule(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        for f in rules_dir.glob("*.md"):
            src = SHARED_DIR / "rules" / f"{f.stem}.md"
            assert src.exists(), f"Source missing: {src}"

            dst_body = _strip_frontmatter(f.read_text())
            assert dst_body == src.read_text(), f"{f.name} body differs from source"


# ── Cross-tool ────────────────────────────────────────────


class TestSyncAll:
    """Verify syncing all tools at once."""

    def test_sync_all_creates_all_tool_dirs(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")

        assert (target_repo / ".github").is_dir()
        assert (target_repo / ".cursor").is_dir()
        assert (target_repo / ".claude").is_dir()

    def test_idempotent_second_run(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")

        # Collect file contents after first run
        first: dict[str, str] = {}
        for f in target_repo.rglob("*.md"):
            first[str(f.relative_to(target_repo))] = f.read_text()

        _run_sync(target_repo, "all")

        # Contents should be identical after second run
        second: dict[str, str] = {}
        for f in target_repo.rglob("*.md"):
            second[str(f.relative_to(target_repo))] = f.read_text()

        assert first == second

    def test_content_preserved_after_frontmatter(self, target_repo: Path) -> None:
        """Original rule body is intact after frontmatter injection."""
        _run_sync(target_repo, "copilot")

        src = SHARED_DIR / "rules" / "security.md"
        dst = target_repo / ".github" / "instructions" / "security.instructions.md"
        if not dst.exists():
            pytest.skip("security rule not generated")

        src_body = src.read_text()
        dst_content = dst.read_text()

        # Strip frontmatter (between --- markers) from dst
        parts = dst_content.split("---", 2)
        dst_body = parts[2].lstrip("\n") if len(parts) >= 3 else ""

        assert dst_body == src_body


# ── Copilot Skills ────────────────────────────────────────


class TestCopilotSkills:
    """Verify Copilot skill generation: set, frontmatter, body, and subdirs."""

    def test_exact_skill_set_generated(self, target_repo: Path) -> None:
        """Every skill directory appears under .github/skills/."""
        _run_sync(target_repo, "copilot")

        skills_dir = target_repo / ".github" / "skills"
        generated = {
            d.name
            for d in skills_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        }
        assert generated == ALL_SKILL_NAMES

    def test_skill_files_have_frontmatter(self, target_repo: Path) -> None:
        """Skills with metadata entries have YAML frontmatter."""
        _run_sync(target_repo, "copilot")

        skills_dir = target_repo / ".github" / "skills"
        for name in SKILLS_WITH_METADATA:
            skill_md = skills_dir / name / "SKILL.md"
            if not skill_md.exists():
                continue
            content = skill_md.read_text()
            assert content.startswith(
                "---"
            ), f"skills/{name}/SKILL.md missing frontmatter"

    def test_frontmatter_has_name_and_description(self, target_repo: Path) -> None:
        _run_sync(target_repo, "copilot")

        skills_dir = target_repo / ".github" / "skills"
        for name in SKILLS_WITH_METADATA:
            skill_md = skills_dir / name / "SKILL.md"
            if not skill_md.exists():
                continue
            fm = _parse_frontmatter(skill_md.read_text())
            assert "name" in fm, f"skills/{name}/SKILL.md missing 'name'"
            assert "description" in fm, f"skills/{name}/SKILL.md missing 'description'"

    def test_allowed_tools_present_when_defined(self, target_repo: Path) -> None:
        """Skills with allowed-tools in metadata have it in frontmatter."""
        _run_sync(target_repo, "copilot")

        skills_dir = target_repo / ".github" / "skills"
        for name in SKILLS_WITH_ALLOWED_TOOLS:
            skill_md = skills_dir / name / "SKILL.md"
            if not skill_md.exists():
                continue
            fm = _parse_frontmatter(skill_md.read_text())
            assert (
                "allowed-tools" in fm
            ), f"skills/{name}/SKILL.md missing 'allowed-tools'"
            assert fm[
                "allowed-tools"
            ], f"skills/{name}/SKILL.md has empty 'allowed-tools'"

    def test_body_matches_source_for_every_skill(self, target_repo: Path) -> None:
        """Body after frontmatter matches the original shared skill."""
        _run_sync(target_repo, "copilot")

        skills_dir = target_repo / ".github" / "skills"
        for d in skills_dir.iterdir():
            if not d.is_dir():
                continue
            skill_md = d / "SKILL.md"
            if not skill_md.exists():
                continue
            src = SHARED_DIR / "skills" / d.name / "SKILL.md"
            assert src.exists(), f"Source missing: {src}"

            dst_body = _strip_frontmatter(skill_md.read_text())
            assert (
                dst_body == src.read_text()
            ), f"skills/{d.name}/SKILL.md body differs from source"

    def test_skill_subdirs_copied(self, target_repo: Path) -> None:
        """Skill subdirectories (references, scripts, etc.) are copied."""
        _run_sync(target_repo, "copilot")

        skills_dir = target_repo / ".github" / "skills"
        for skill_name, subdirs in SKILLS_WITH_SUBDIRS.items():
            for sub in subdirs:
                dst_sub = skills_dir / skill_name / sub
                assert dst_sub.is_dir(), f"skills/{skill_name}/{sub}/ not copied"
                # Verify at least one file is inside
                files = list(dst_sub.rglob("*"))
                assert len(files) > 0, f"skills/{skill_name}/{sub}/ is empty"


# ── Cursor Skills ─────────────────────────────────────────


class TestCursorSkills:
    """Verify Cursor skill generation: set, frontmatter, body, and subdirs."""

    def test_exact_skill_set_generated(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        skills_dir = target_repo / ".cursor" / "skills"
        generated = {
            d.name
            for d in skills_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        }
        assert generated == ALL_SKILL_NAMES

    def test_skill_files_have_frontmatter(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        skills_dir = target_repo / ".cursor" / "skills"
        for name in SKILLS_WITH_METADATA:
            skill_md = skills_dir / name / "SKILL.md"
            if not skill_md.exists():
                continue
            content = skill_md.read_text()
            assert content.startswith(
                "---"
            ), f"skills/{name}/SKILL.md missing frontmatter"

    def test_frontmatter_has_name_and_description(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        skills_dir = target_repo / ".cursor" / "skills"
        for name in SKILLS_WITH_METADATA:
            skill_md = skills_dir / name / "SKILL.md"
            if not skill_md.exists():
                continue
            fm = _parse_frontmatter(skill_md.read_text())
            assert "name" in fm, f"skills/{name}/SKILL.md missing 'name'"
            assert "description" in fm, f"skills/{name}/SKILL.md missing 'description'"

    def test_body_matches_source_for_every_skill(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        skills_dir = target_repo / ".cursor" / "skills"
        for d in skills_dir.iterdir():
            if not d.is_dir():
                continue
            skill_md = d / "SKILL.md"
            if not skill_md.exists():
                continue
            src = SHARED_DIR / "skills" / d.name / "SKILL.md"
            assert src.exists(), f"Source missing: {src}"

            dst_body = _strip_frontmatter(skill_md.read_text())
            assert (
                dst_body == src.read_text()
            ), f"skills/{d.name}/SKILL.md body differs from source"

    def test_skill_subdirs_copied(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        skills_dir = target_repo / ".cursor" / "skills"
        for skill_name, subdirs in SKILLS_WITH_SUBDIRS.items():
            for sub in subdirs:
                dst_sub = skills_dir / skill_name / sub
                assert dst_sub.is_dir(), f"skills/{skill_name}/{sub}/ not copied"


# ── Claude Skills ─────────────────────────────────────────


class TestClaudeSkills:
    """Verify Claude skill generation: set, frontmatter, body, and subdirs."""

    def test_exact_skill_set_generated(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        skills_dir = target_repo / ".claude" / "skills"
        generated = {
            d.name
            for d in skills_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        }
        assert generated == ALL_SKILL_NAMES

    def test_skill_files_have_frontmatter(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        skills_dir = target_repo / ".claude" / "skills"
        for name in SKILLS_WITH_METADATA:
            skill_md = skills_dir / name / "SKILL.md"
            if not skill_md.exists():
                continue
            content = skill_md.read_text()
            assert content.startswith(
                "---"
            ), f"skills/{name}/SKILL.md missing frontmatter"

    def test_frontmatter_has_name_and_description(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        skills_dir = target_repo / ".claude" / "skills"
        for name in SKILLS_WITH_METADATA:
            skill_md = skills_dir / name / "SKILL.md"
            if not skill_md.exists():
                continue
            fm = _parse_frontmatter(skill_md.read_text())
            assert "name" in fm, f"skills/{name}/SKILL.md missing 'name'"
            assert "description" in fm, f"skills/{name}/SKILL.md missing 'description'"

    def test_body_matches_source_for_every_skill(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        skills_dir = target_repo / ".claude" / "skills"
        for d in skills_dir.iterdir():
            if not d.is_dir():
                continue
            skill_md = d / "SKILL.md"
            if not skill_md.exists():
                continue
            src = SHARED_DIR / "skills" / d.name / "SKILL.md"
            assert src.exists(), f"Source missing: {src}"

            dst_body = _strip_frontmatter(skill_md.read_text())
            assert (
                dst_body == src.read_text()
            ), f"skills/{d.name}/SKILL.md body differs from source"

    def test_skill_subdirs_copied(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        skills_dir = target_repo / ".claude" / "skills"
        for skill_name, subdirs in SKILLS_WITH_SUBDIRS.items():
            for sub in subdirs:
                dst_sub = skills_dir / skill_name / sub
                assert dst_sub.is_dir(), f"skills/{skill_name}/{sub}/ not copied"


# ── Copilot Agents ────────────────────────────────────────


class TestCopilotAgents:
    """Verify Copilot agent generation: exact set, frontmatter, body."""

    def test_exact_agent_set_generated(self, target_repo: Path) -> None:
        """Every agent .md appears as a .agent.md under .github/agents/."""
        _run_sync(target_repo, "copilot")

        agents_dir = target_repo / ".github" / "agents"
        generated = {
            f.stem.removesuffix(".agent") for f in agents_dir.glob("*.agent.md")
        }
        assert generated == ALL_AGENT_NAMES

    def test_agents_with_metadata_have_frontmatter(self, target_repo: Path) -> None:
        _run_sync(target_repo, "copilot")

        agents_dir = target_repo / ".github" / "agents"
        for name in AGENTS_WITH_METADATA:
            agent_md = agents_dir / f"{name}.agent.md"
            if not agent_md.exists():
                continue
            content = agent_md.read_text()
            assert content.startswith(
                "---"
            ), f"agents/{name}.agent.md missing frontmatter"

    def test_frontmatter_has_name_and_description(self, target_repo: Path) -> None:
        _run_sync(target_repo, "copilot")

        agents_dir = target_repo / ".github" / "agents"
        for name in AGENTS_WITH_METADATA:
            agent_md = agents_dir / f"{name}.agent.md"
            if not agent_md.exists():
                continue
            fm = _parse_frontmatter(agent_md.read_text())
            assert "name" in fm, f"agents/{name}.agent.md missing 'name'"
            assert "description" in fm, f"agents/{name}.agent.md missing 'description'"

    def test_disallowed_tools_present_when_defined(self, target_repo: Path) -> None:
        """Agents with disallowedTools in metadata have it in frontmatter."""
        _run_sync(target_repo, "copilot")

        agents_dir = target_repo / ".github" / "agents"
        for name in AGENTS_WITH_DISALLOWED_TOOLS:
            agent_md = agents_dir / f"{name}.agent.md"
            if not agent_md.exists():
                continue
            fm = _parse_frontmatter(agent_md.read_text())
            assert (
                "disallowedTools" in fm
            ), f"agents/{name}.agent.md missing 'disallowedTools'"

    def test_body_matches_source_for_every_agent(self, target_repo: Path) -> None:
        """Body after frontmatter matches the original shared agent."""
        _run_sync(target_repo, "copilot")

        agents_dir = target_repo / ".github" / "agents"
        for f in agents_dir.glob("*.agent.md"):
            agent_name = f.stem.removesuffix(".agent")
            src = SHARED_DIR / "agents" / f"{agent_name}.md"
            assert src.exists(), f"Source missing: {src}"

            dst_body = _strip_frontmatter(f.read_text())
            assert (
                dst_body == src.read_text()
            ), f"agents/{f.name} body differs from source"


# ── Cursor Agents ─────────────────────────────────────────


class TestCursorAgents:
    """Verify Cursor agent generation: exact set, frontmatter, body."""

    def test_exact_agent_set_generated(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        agents_dir = target_repo / ".cursor" / "agents"
        generated = {f.stem for f in agents_dir.glob("*.md")}
        assert generated == ALL_AGENT_NAMES

    def test_agents_with_metadata_have_frontmatter(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        agents_dir = target_repo / ".cursor" / "agents"
        for name in AGENTS_WITH_METADATA:
            agent_md = agents_dir / f"{name}.md"
            if not agent_md.exists():
                continue
            content = agent_md.read_text()
            assert content.startswith("---"), f"agents/{name}.md missing frontmatter"

    def test_frontmatter_has_description(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        agents_dir = target_repo / ".cursor" / "agents"
        for name in AGENTS_WITH_METADATA:
            agent_md = agents_dir / f"{name}.md"
            if not agent_md.exists():
                continue
            fm = _parse_frontmatter(agent_md.read_text())
            assert "description" in fm, f"agents/{name}.md missing 'description'"

    def test_body_matches_source_for_every_agent(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        agents_dir = target_repo / ".cursor" / "agents"
        for f in agents_dir.glob("*.md"):
            src = SHARED_DIR / "agents" / f"{f.stem}.md"
            assert src.exists(), f"Source missing: {src}"

            dst_body = _strip_frontmatter(f.read_text())
            assert (
                dst_body == src.read_text()
            ), f"agents/{f.name} body differs from source"


# ── Claude Agents ─────────────────────────────────────────


class TestClaudeAgents:
    """Verify Claude agent generation: exact set, frontmatter, body."""

    def test_exact_agent_set_generated(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        agents_dir = target_repo / ".claude" / "agents"
        generated = {f.stem for f in agents_dir.glob("*.md")}
        assert generated == ALL_AGENT_NAMES

    def test_agents_with_metadata_have_frontmatter(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        agents_dir = target_repo / ".claude" / "agents"
        for name in AGENTS_WITH_METADATA:
            agent_md = agents_dir / f"{name}.md"
            if not agent_md.exists():
                continue
            content = agent_md.read_text()
            assert content.startswith("---"), f"agents/{name}.md missing frontmatter"

    def test_frontmatter_has_name_and_description(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        agents_dir = target_repo / ".claude" / "agents"
        for name in AGENTS_WITH_METADATA:
            agent_md = agents_dir / f"{name}.md"
            if not agent_md.exists():
                continue
            fm = _parse_frontmatter(agent_md.read_text())
            assert "name" in fm, f"agents/{name}.md missing 'name'"
            assert "description" in fm, f"agents/{name}.md missing 'description'"

    def test_body_matches_source_for_every_agent(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        agents_dir = target_repo / ".claude" / "agents"
        for f in agents_dir.glob("*.md"):
            src = SHARED_DIR / "agents" / f"{f.stem}.md"
            assert src.exists(), f"Source missing: {src}"

            dst_body = _strip_frontmatter(f.read_text())
            assert (
                dst_body == src.read_text()
            ), f"agents/{f.name} body differs from source"
