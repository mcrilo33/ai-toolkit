"""Integration tests for scripts/sync-to-repo.sh.

Runs the sync script against a temporary git repo and verifies that
the correct files are generated with the right frontmatter and content.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-to-repo.sh"
SHARED_DIR = REPO_ROOT / "shared"


# ── Expected hook counts derived from shared/hooks/metadata.yml ──────────


def _hook_event_counts(tool_event_map: dict[str, str], *, tool: str | None = None) -> Counter[str]:
    """Count expected ai-toolkit hooks per platform event from metadata.yml.

    Returns a Counter keyed by the *platform* event name, so the same source of
    truth (shared/hooks/metadata.yml) drives the assertion for every tool.

    A per-tool ``event`` override (e.g. the Cursor migration's
    ``cursor: event: beforeShellExecution``) is honored when ``tool`` is given,
    mirroring hooks_generator.py.
    """
    meta = yaml.safe_load((SHARED_DIR / "hooks" / "metadata.yml").read_text())
    counts: Counter[str] = Counter()
    for data in meta.values():
        canonical = data.get("event", "")
        if tool and isinstance(data.get(tool), dict) and data[tool].get("event"):
            canonical = data[tool]["event"]
        platform_event = tool_event_map.get(canonical, canonical)
        if platform_event:
            counts[platform_event] += 1
    return counts


# Canonical → platform event maps (mirror hooks_generator.py)
_CURSOR_EVENT_MAP = {
    "preToolUse": "preToolUse",
    "postToolUse": "postToolUse",
    "beforeShellExecution": "beforeShellExecution",
    "afterShellExecution": "afterShellExecution",
    "afterFileEdit": "afterFileEdit",
    "beforeReadFile": "beforeReadFile",
}
_CLAUDE_EVENT_MAP = {"preToolUse": "PreToolUse", "postToolUse": "PostToolUse"}

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
    "scientific-integrity",
    "planning-hub",
}

# Rules that have no applyTo/globs in metadata
RULES_WITHOUT_GLOB = {"library-research", "planning-hub"}

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
    d.name for d in (SHARED_DIR / "skills").iterdir() if d.is_dir() and (d / "SKILL.md").exists()
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
        yaml.safe_load(_skills_meta.read_text()).items() if _skills_meta.exists() else []
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
        yaml.safe_load(_agents_meta.read_text()).items() if _agents_meta.exists() else []
    )
    if "disallowedTools" in data
}

# Skills with subdirectories (references, scripts, templates, assets)
SKILL_SUBDIRS = ("references", "scripts", "templates", "assets")
SKILLS_WITH_SUBDIRS = {
    d.name: [sub for sub in SKILL_SUBDIRS if (d / sub).is_dir()]
    for d in (SHARED_DIR / "skills").iterdir()
    if d.is_dir() and (d / "SKILL.md").exists() and any((d / sub).is_dir() for sub in SKILL_SUBDIRS)
}


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (between --- markers) and leading blank line."""
    parts = text.split("---", 2)
    return parts[2].lstrip("\n") if len(parts) >= 3 else text


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract frontmatter fields as a flat dict (surrounding quotes stripped)."""
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
            val = val.strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            result[key.strip()] = val
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

    def test_copilot_instruction_files_have_frontmatter(self, target_repo: Path) -> None:
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
            f.stem.removesuffix(".instructions") for f in instructions.glob("*.instructions.md")
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

    def test_frontmatter_has_description_and_alwaysApply(self, target_repo: Path) -> None:
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
                assert fm.get("globs") == str(expected_globs), f"{f.name}: globs mismatch"

    def test_alwaysApply_value_matches_metadata(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        meta = yaml.safe_load((SHARED_DIR / "rules" / "metadata.yml").read_text())
        rules_dir = target_repo / ".cursor" / "rules"
        for f in rules_dir.glob("*.mdc"):
            rule_name = f.stem
            expected = str(meta.get(rule_name, {}).get("alwaysApply", "")).lower()
            fm = _parse_frontmatter(f.read_text())
            assert fm.get("alwaysApply", "").lower() == expected, f"{f.name}: alwaysApply mismatch"

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
        assert generated.isdisjoint(excluded), f"Unexpected rules in Claude: {generated & excluded}"

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
            d.name for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
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
            assert content.startswith("---"), f"skills/{name}/SKILL.md missing frontmatter"

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
            assert "allowed-tools" in fm, f"skills/{name}/SKILL.md missing 'allowed-tools'"
            assert fm["allowed-tools"], f"skills/{name}/SKILL.md has empty 'allowed-tools'"

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
            assert dst_body == src.read_text(), f"skills/{d.name}/SKILL.md body differs from source"

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
            d.name for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
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
            assert content.startswith("---"), f"skills/{name}/SKILL.md missing frontmatter"

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
            assert dst_body == src.read_text(), f"skills/{d.name}/SKILL.md body differs from source"

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
            d.name for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
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
            assert content.startswith("---"), f"skills/{name}/SKILL.md missing frontmatter"

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
            assert dst_body == src.read_text(), f"skills/{d.name}/SKILL.md body differs from source"

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
        generated = {f.stem.removesuffix(".agent") for f in agents_dir.glob("*.agent.md")}
        assert generated == ALL_AGENT_NAMES

    def test_agents_with_metadata_have_frontmatter(self, target_repo: Path) -> None:
        _run_sync(target_repo, "copilot")

        agents_dir = target_repo / ".github" / "agents"
        for name in AGENTS_WITH_METADATA:
            agent_md = agents_dir / f"{name}.agent.md"
            if not agent_md.exists():
                continue
            content = agent_md.read_text()
            assert content.startswith("---"), f"agents/{name}.agent.md missing frontmatter"

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
            assert "disallowedTools" in fm, f"agents/{name}.agent.md missing 'disallowedTools'"

    def test_body_matches_source_for_every_agent(self, target_repo: Path) -> None:
        """Body after frontmatter matches the original shared agent."""
        _run_sync(target_repo, "copilot")

        agents_dir = target_repo / ".github" / "agents"
        for f in agents_dir.glob("*.agent.md"):
            agent_name = f.stem.removesuffix(".agent")
            src = SHARED_DIR / "agents" / f"{agent_name}.md"
            assert src.exists(), f"Source missing: {src}"

            dst_body = _strip_frontmatter(f.read_text())
            assert dst_body == src.read_text(), f"agents/{f.name} body differs from source"


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
            assert dst_body == src.read_text(), f"agents/{f.name} body differs from source"


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
            assert dst_body == src.read_text(), f"agents/{f.name} body differs from source"


# ── Hook idempotency / reconciliation ─────────────────────


class TestHookIdempotency:
    """Repeated syncs must converge to a fixed point and self-heal bloat.

    Guards the original bug: pure-append hook merges grew .cursor/hooks.json and
    .claude/settings.json without bound on every sync.
    """

    def _owned_cursor_entries(self, hooks_json: dict) -> Counter[str]:
        counts: Counter[str] = Counter()
        for event, entries in hooks_json.get("hooks", {}).items():
            for e in entries:
                if "hooks/scripts/" in e.get("command", ""):
                    counts[event] += 1
        return counts

    def _owned_claude_handlers(self, settings: dict) -> Counter[str]:
        counts: Counter[str] = Counter()
        for event, groups in settings.get("hooks", {}).items():
            for group in groups:
                for handler in group.get("hooks", []):
                    if "hooks/scripts/" in handler.get("command", ""):
                        counts[event] += 1
        return counts

    def test_cursor_config_byte_identical_across_runs(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")
        first = (target_repo / ".cursor" / "hooks.json").read_text()
        _run_sync(target_repo, "all")
        second = (target_repo / ".cursor" / "hooks.json").read_text()
        assert first == second, ".cursor/hooks.json changed on second sync"

    def test_claude_settings_byte_identical_across_runs(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")
        first = (target_repo / ".claude" / "settings.json").read_text()
        _run_sync(target_repo, "all")
        second = (target_repo / ".claude" / "settings.json").read_text()
        assert first == second, ".claude/settings.json changed on second sync"

    def test_cursor_counts_match_metadata(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")
        hooks_json = json.loads((target_repo / ".cursor" / "hooks.json").read_text())
        assert self._owned_cursor_entries(hooks_json) == _hook_event_counts(
            _CURSOR_EVENT_MAP, tool="cursor"
        )

    def test_claude_counts_match_metadata(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")
        settings = json.loads((target_repo / ".claude" / "settings.json").read_text())
        assert self._owned_claude_handlers(settings) == _hook_event_counts(_CLAUDE_EVENT_MAP)

    def test_counts_stable_after_repeated_runs(self, target_repo: Path) -> None:
        """Five syncs yield exactly one entry per shared hook per event."""
        for _ in range(5):
            _run_sync(target_repo, "all")
        hooks_json = json.loads((target_repo / ".cursor" / "hooks.json").read_text())
        settings = json.loads((target_repo / ".claude" / "settings.json").read_text())
        assert self._owned_cursor_entries(hooks_json) == _hook_event_counts(
            _CURSOR_EVENT_MAP, tool="cursor"
        )
        assert self._owned_claude_handlers(settings) == _hook_event_counts(_CLAUDE_EVENT_MAP)

    def test_self_heals_bloated_cursor_file(self, target_repo: Path) -> None:
        """A pre-bloated hooks.json shrinks to the canonical set on next sync."""
        _run_sync(target_repo, "all")
        cursor_file = target_repo / ".cursor" / "hooks.json"
        bloated = json.loads(cursor_file.read_text())
        for event in list(bloated["hooks"]):
            bloated["hooks"][event] = [dict(e) for e in bloated["hooks"][event] for _ in range(5)]
        cursor_file.write_text(json.dumps(bloated, indent=2))

        _run_sync(target_repo, "all")
        healed = json.loads(cursor_file.read_text())
        assert self._owned_cursor_entries(healed) == _hook_event_counts(
            _CURSOR_EVENT_MAP, tool="cursor"
        )

    def test_user_authored_hook_survives_sync(self, target_repo: Path) -> None:
        """A non-ai-toolkit hook is preserved across reconciliation."""
        _run_sync(target_repo, "all")
        cursor_file = target_repo / ".cursor" / "hooks.json"
        data = json.loads(cursor_file.read_text())
        user_entry = {"command": "./scripts/user-precommit.sh", "matcher": "git commit"}
        data["hooks"].setdefault("beforeShellExecution", []).append(user_entry)
        cursor_file.write_text(json.dumps(data, indent=2))

        _run_sync(target_repo, "all")
        result = json.loads(cursor_file.read_text())
        assert user_entry in result["hooks"]["beforeShellExecution"]

    def test_removed_shared_hook_drops_downstream(self, target_repo: Path) -> None:
        """A stale owned entry absent from the generated set is dropped on sync."""
        _run_sync(target_repo, "all")
        cursor_file = target_repo / ".cursor" / "hooks.json"
        data = json.loads(cursor_file.read_text())
        stale = {
            "command": "./.cursor/hooks/scripts/deleted-hook.sh",
            "matcher": "git commit",
        }
        data["hooks"].setdefault("beforeShellExecution", []).append(stale)
        cursor_file.write_text(json.dumps(data, indent=2))

        _run_sync(target_repo, "all")
        result = json.loads(cursor_file.read_text())
        assert stale not in result["hooks"]["beforeShellExecution"]


# ── Manifest + stale-file GC + dry-run ────────────────────


class TestManifestAndGC:
    """Sync writes a .ai-toolkit-manifest.json and GCs stale toolkit outputs."""

    MANIFEST_NAME = ".ai-toolkit-manifest.json"

    def test_sync_cursor_writes_manifest_with_cursor_list(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")

        manifest = json.loads((target_repo / self.MANIFEST_NAME).read_text())
        assert manifest["tools"]["cursor"], "cursor list is empty"
        assert ".cursor/rules/guidelines.mdc" in manifest["tools"]["cursor"]

    def test_resync_deletes_stale_manifest_listed_file(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")
        stale = target_repo / ".cursor" / "rules" / "obsolete-rule.mdc"
        stale.write_text("---\ndescription: obsolete\n---\n\n# Obsolete\n")
        manifest_file = target_repo / self.MANIFEST_NAME
        manifest = json.loads(manifest_file.read_text())
        manifest["tools"]["cursor"].append(".cursor/rules/obsolete-rule.mdc")
        manifest_file.write_text(json.dumps(manifest))

        _run_sync(target_repo, "cursor")

        assert not stale.exists()
        manifest = json.loads(manifest_file.read_text())
        assert ".cursor/rules/obsolete-rule.mdc" not in manifest["tools"]["cursor"]

    def test_user_file_not_in_manifest_survives_resync(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")
        user_rule = target_repo / ".cursor" / "rules" / "my-own-rule.mdc"
        user_rule.write_text("---\ndescription: mine\n---\n\n# Mine\n")
        manifest_file = target_repo / self.MANIFEST_NAME
        manifest = json.loads(manifest_file.read_text())
        assert ".cursor/rules/my-own-rule.mdc" not in manifest["tools"]["cursor"]

        _run_sync(target_repo, "cursor")

        assert user_rule.exists()
        manifest = json.loads(manifest_file.read_text())
        assert ".cursor/rules/my-own-rule.mdc" not in manifest["tools"]["cursor"]

    def test_dry_run_touches_nothing_on_fresh_target(self, target_repo: Path) -> None:
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), str(target_repo), "cursor", "--dry-run"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "[dry-run]" in result.stdout
        assert not (target_repo / ".cursor").exists()
        assert not (target_repo / self.MANIFEST_NAME).exists()

    def test_manifest_byte_identical_after_second_sync(self, target_repo: Path) -> None:
        _run_sync(target_repo, "cursor")
        first = (target_repo / self.MANIFEST_NAME).read_bytes()

        _run_sync(target_repo, "cursor")

        second = (target_repo / self.MANIFEST_NAME).read_bytes()
        assert first == second

    def test_sync_all_writes_all_three_tool_lists(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")

        manifest = json.loads((target_repo / self.MANIFEST_NAME).read_text())
        assert manifest["tools"]["copilot"], "copilot list is empty"
        assert manifest["tools"]["cursor"], "cursor list is empty"
        assert manifest["tools"]["claude"], "claude list is empty"


# ── MCP servers (review-stamp) ────────────────────────────


class TestMcpServerSync:
    """Sync installs the review-stamp MCP server into the target.

    The code-review agent frontmatter references
    ./.ai-toolkit/mcp/review-stamp/run.sh — that path must exist in every
    synced target, be executable, and be manifest-tracked so GC manages it.
    """

    MANIFEST_NAME = ".ai-toolkit-manifest.json"
    RUN_SH = ".ai-toolkit/mcp/review-stamp/run.sh"
    SERVER_PY = ".ai-toolkit/mcp/review-stamp/server.py"

    def test_run_sh_installed_and_executable(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        run_sh = target_repo / self.RUN_SH
        assert run_sh.is_file(), f"{self.RUN_SH} not installed"
        assert os.access(run_sh, os.X_OK), f"{self.RUN_SH} not executable"

    def test_server_py_installed(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        assert (target_repo / self.SERVER_PY).is_file()

    def test_installed_files_match_source(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        src_dir = REPO_ROOT / "mcp" / "review-stamp"
        assert (target_repo / self.RUN_SH).read_bytes() == (src_dir / "run.sh").read_bytes()
        assert (target_repo / self.SERVER_PY).read_bytes() == (src_dir / "server.py").read_bytes()

    def test_both_files_recorded_in_manifest_for_every_tool(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")

        manifest = json.loads((target_repo / self.MANIFEST_NAME).read_text())
        for tool in ("copilot", "cursor", "claude"):
            assert self.RUN_SH in manifest["tools"][tool], f"{tool}: run.sh missing"
            assert self.SERVER_PY in manifest["tools"][tool], f"{tool}: server.py missing"

    def test_resync_is_byte_identical(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")
        first = {p: (target_repo / p).read_bytes() for p in (self.RUN_SH, self.SERVER_PY)}

        _run_sync(target_repo, "all")

        second = {p: (target_repo / p).read_bytes() for p in (self.RUN_SH, self.SERVER_PY)}
        assert first == second

    def test_dry_run_does_not_install(self, target_repo: Path) -> None:
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), str(target_repo), "claude", "--dry-run"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert not (target_repo / ".ai-toolkit").exists()
        assert f"[dry-run] would write {self.RUN_SH}" in result.stdout
