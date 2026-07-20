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
        # Mirror hooks_generator's skip-unmapped rule (#176): a Claude-only canonical
        # event (e.g. `notification`) is skipped for the other platforms rather than
        # passthrough-emitting a bucket they never fire.
        if tool in ("cursor", "copilot") and canonical in _CLAUDE_ONLY_EVENTS:
            continue
        platform_event = tool_event_map.get(canonical, canonical)
        if platform_event:
            counts[platform_event] += 1
    return counts


# Canonical events Claude fires but Copilot/Cursor do not — the generator skips them for
# those platforms rather than emitting a bogus bucket (mirror hooks_generator.py, #176).
_CLAUDE_ONLY_EVENTS = {"notification"}


# Canonical → platform event maps (mirror hooks_generator.py)
_CURSOR_EVENT_MAP = {
    "preToolUse": "preToolUse",
    "postToolUse": "postToolUse",
    "beforeShellExecution": "beforeShellExecution",
    "afterShellExecution": "afterShellExecution",
    "afterFileEdit": "afterFileEdit",
    "beforeReadFile": "beforeReadFile",
}
_CLAUDE_EVENT_MAP = {
    "preToolUse": "PreToolUse",
    "postToolUse": "PostToolUse",
    "sessionStart": "SessionStart",
    "notification": "Notification",
}

# ── Expected rules derived from metadata ─────────────────

# Rules that have the tool-relevant field in metadata.yml:
# Copilot: emits all rules that have at least one of (name, description, applyTo)
# Cursor:  emits all rules that have at least one of (description, globs, alwaysApply)
# Claude:  emits rules that have 'paths' (conditional) OR alwaysApply: true with no
#          'paths' (always-on, empty frontmatter); on-demand rules (neither) are not
#          emitted (issue #320)

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
    "operational-gotchas",
    "github-actions",
    "library-research",
    "agent-orchestration",
    "scientific-integrity",
    "planning-hub",
    "issue-hygiene",
    "afk-answering",
    "afk-design-principles",
    "bug-triage",
}

# Rules that have no applyTo/globs in metadata
RULES_WITHOUT_GLOB = {
    "library-research",
    "planning-hub",
    "issue-hygiene",
    "afk-answering",
    "bug-triage",
}

# Rules that define 'paths' in metadata → generated as conditional Claude rules
CLAUDE_RULES_WITH_PATHS = {
    "afk-design-principles",
    "code-quality",
    "python-style",
    "gitignore-template",
    "markdown-style",
    "mermaid-conventions",
    "pytest-conventions",
    "operational-gotchas",
    "github-actions",
}

# Rules with alwaysApply: true and no 'paths' → generated as always-on Claude rules
# (empty frontmatter, loaded at session start). Mirrors
# tests/unit/test_rules_governance.py::EXPECTED_ALWAYS_ON (issue #320).
CLAUDE_ALWAYS_ON_RULES = {
    "guidelines",
    "security",
    "agent-orchestration",
    "scientific-integrity",
}

# The full Claude rule set: conditional + always-on. On-demand rules (no 'paths',
# alwaysApply falsy) are surfaced via their skills, never emitted here.
CLAUDE_RULES_GENERATED = CLAUDE_RULES_WITH_PATHS | CLAUDE_ALWAYS_ON_RULES

# On-demand rules: neither 'paths' nor alwaysApply: true → not generated for Claude.
CLAUDE_RULES_ON_DEMAND = {
    "workflow",
    "planning-hub",
    "issue-hygiene",
    "bug-triage",
    "afk-answering",
    "library-research",
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


# Session-tree paths synced exactly once (issue #330). A repeated `_run_sync` against one
# of these is a no-op — see `_run_sync`. Only the shared read-only trees built by
# `build_synced_tree` register here; every per-test `target_repo` is a fresh dir that never
# does, so mutating / idempotency tests always run a real (and repeatable) sync.
_SHARED_SYNCED: set[str] = set()

# A cheap stand-in for a suppressed re-sync's result. The read-only classes that share a
# tree ignore the returned CompletedProcess (they assert on the generated files); the
# tests that inspect stdout/returncode all use a fresh, unregistered target_repo.
_NOOP_SYNC_RESULT = subprocess.CompletedProcess(["sync-to-repo.sh"], 0, "", "")


def _run_sync(target: Path, tool: str = "all") -> subprocess.CompletedProcess[str]:
    """Run sync-to-repo.sh and return the result.

    #330: the sync output is a pure function of the constant shared/ source, so a target
    already synced once as a shared read-only tree (registered in `_SHARED_SYNCED`) is not
    re-synced — the read-only assertion tests below share one `synced_<tool>` tree instead
    of each re-running a full sync (a #328-flagged wall-clock hotspot). A per-test
    target_repo is a fresh dir absent from that set, so it always runs a real sync, even
    when a mutating/idempotency test syncs the same dir twice.
    """
    if str(target) in _SHARED_SYNCED:
        return _NOOP_SYNC_RESULT
    return subprocess.run(
        ["bash", str(SYNC_SCRIPT), str(target), tool],
        capture_output=True,
        text=True,
        check=True,
    )


# ── Shared once-synced trees (issue #330) ─────────────────
# Sync each tool ONCE per session into its own git repo; the pure read-only classes below
# consume the matching tree via a class-scoped `target_repo` override. Tests that MUTATE
# the tree, sync twice (reconciliation / idempotency), write a pre-sync config, or inspect
# the sync's own stdout keep the per-test `target_repo` — its fresh dir never registers in
# `_SHARED_SYNCED`, so it always runs a real sync.


def build_synced_tree(tmp_path_factory: pytest.TempPathFactory, tool: str) -> Path:
    """Sync `tool` once into a fresh session-lived git repo and return its path."""
    target = tmp_path_factory.mktemp(f"synced-{tool}")
    subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True)
    _run_sync(target, tool)
    _SHARED_SYNCED.add(str(target))  # subsequent _run_sync on this tree is a no-op
    return target


@pytest.fixture(scope="session")
def synced_copilot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_synced_tree(tmp_path_factory, "copilot")


@pytest.fixture(scope="session")
def synced_cursor(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_synced_tree(tmp_path_factory, "cursor")


@pytest.fixture(scope="session")
def synced_claude(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_synced_tree(tmp_path_factory, "claude")


@pytest.fixture(scope="session")
def synced_all(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_synced_tree(tmp_path_factory, "all")


# ── Copilot ───────────────────────────────────────────────


class TestSyncCopilot:
    """Verify Copilot file generation."""

    @pytest.fixture
    def target_repo(self, synced_copilot: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_copilot

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

    @pytest.fixture
    def target_repo(self, synced_copilot: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_copilot

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

    @pytest.fixture
    def target_repo(self, synced_cursor: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_cursor

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

    @pytest.fixture
    def target_repo(self, synced_cursor: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_cursor

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

    @pytest.fixture
    def target_repo(self, synced_claude: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_claude

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

    @pytest.fixture
    def target_repo(self, synced_claude: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_claude

    def test_exact_rule_set_generated(self, target_repo: Path) -> None:
        """Conditional (paths) plus always-on (alwaysApply, no paths) rules generate."""
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        generated = {f.stem for f in rules_dir.glob("*.md")}
        assert generated == CLAUDE_RULES_GENERATED

    def test_conditional_frontmatter_has_paths_field(self, target_repo: Path) -> None:
        """Every conditional (paths) rule carries a non-empty 'paths' field."""
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        for name in CLAUDE_RULES_WITH_PATHS:
            fm = _parse_frontmatter((rules_dir / f"{name}.md").read_text())
            assert "paths" in fm, f"{name}.md missing 'paths' field"
            assert fm["paths"], f"{name}.md has empty paths"

    def test_always_on_frontmatter_has_no_paths(self, target_repo: Path) -> None:
        """Always-on rules emit empty frontmatter (no 'paths') so CC loads them always."""
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        for name in CLAUDE_ALWAYS_ON_RULES:
            fm = _parse_frontmatter((rules_dir / f"{name}.md").read_text())
            assert "paths" not in fm, f"{name}.md must not carry 'paths' (always-on)"

    def test_no_disallowed_fields_in_frontmatter(self, target_repo: Path) -> None:
        """Claude frontmatter emits only 'paths' — never alwaysApply/globs/applyTo."""
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        for f in rules_dir.glob("*.md"):
            fm = _parse_frontmatter(f.read_text())
            for disallowed in ("alwaysApply", "globs", "applyTo"):
                assert disallowed not in fm, f"{f.name}: '{disallowed}' leaked into frontmatter"

    def test_paths_value_matches_metadata(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        meta = yaml.safe_load((SHARED_DIR / "rules" / "metadata.yml").read_text())
        rules_dir = target_repo / ".claude" / "rules"
        for f in rules_dir.glob("*.md"):
            rule_name = f.stem
            expected = str(meta.get(rule_name, {}).get("paths", ""))
            fm = _parse_frontmatter(f.read_text())
            assert fm.get("paths", "") == expected, f"{f.name}: paths mismatch"

    def test_on_demand_rules_excluded(self, target_repo: Path) -> None:
        """On-demand rules (no paths, alwaysApply falsy) are surfaced via skills, not here."""
        _run_sync(target_repo, "claude")

        rules_dir = target_repo / ".claude" / "rules"
        generated = {f.stem for f in rules_dir.glob("*.md")}
        assert generated.isdisjoint(CLAUDE_RULES_ON_DEMAND), (
            f"Unexpected on-demand rules in Claude: {generated & CLAUDE_RULES_ON_DEMAND}"
        )

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

    def test_sync_all_creates_all_tool_dirs(self, synced_all: Path) -> None:
        assert (synced_all / ".github").is_dir()
        assert (synced_all / ".cursor").is_dir()
        assert (synced_all / ".claude").is_dir()

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

    def test_content_preserved_after_frontmatter(self, synced_copilot: Path) -> None:
        """Original rule body is intact after frontmatter injection."""
        src = SHARED_DIR / "rules" / "security.md"
        dst = synced_copilot / ".github" / "instructions" / "security.instructions.md"
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

    @pytest.fixture
    def target_repo(self, synced_copilot: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_copilot

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

    @pytest.fixture
    def target_repo(self, synced_cursor: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_cursor

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

    @pytest.fixture
    def target_repo(self, synced_claude: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_claude

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

    @pytest.fixture
    def target_repo(self, synced_copilot: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_copilot

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

    @pytest.fixture
    def target_repo(self, synced_cursor: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_cursor

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

    @pytest.fixture
    def target_repo(self, synced_claude: Path) -> Path:
        """#330: read-only assertions share one session-synced tree."""
        return synced_claude

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
        assert self._owned_claude_handlers(settings) == _hook_event_counts(
            _CLAUDE_EVENT_MAP, tool="claude"
        )

    def test_counts_stable_after_repeated_runs(self, target_repo: Path) -> None:
        """Five syncs yield exactly one entry per shared hook per event."""
        for _ in range(5):
            _run_sync(target_repo, "all")
        hooks_json = json.loads((target_repo / ".cursor" / "hooks.json").read_text())
        settings = json.loads((target_repo / ".claude" / "settings.json").read_text())
        assert self._owned_cursor_entries(hooks_json) == _hook_event_counts(
            _CURSOR_EVENT_MAP, tool="cursor"
        )
        assert self._owned_claude_handlers(settings) == _hook_event_counts(
            _CLAUDE_EVENT_MAP, tool="claude"
        )

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

    def test_sync_cursor_writes_manifest_with_cursor_list(self, synced_cursor: Path) -> None:
        manifest = json.loads((synced_cursor / self.MANIFEST_NAME).read_text())
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

    def test_sync_all_writes_all_three_tool_lists(self, synced_all: Path) -> None:
        manifest = json.loads((synced_all / self.MANIFEST_NAME).read_text())
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

    def test_run_sh_installed_and_executable(self, synced_claude: Path) -> None:
        run_sh = synced_claude / self.RUN_SH
        assert run_sh.is_file(), f"{self.RUN_SH} not installed"
        assert os.access(run_sh, os.X_OK), f"{self.RUN_SH} not executable"

    def test_server_py_installed(self, synced_claude: Path) -> None:
        assert (synced_claude / self.SERVER_PY).is_file()

    def test_installed_files_match_source(self, synced_claude: Path) -> None:
        src_dir = REPO_ROOT / "mcp" / "review-stamp"
        assert (synced_claude / self.RUN_SH).read_bytes() == (src_dir / "run.sh").read_bytes()
        assert (synced_claude / self.SERVER_PY).read_bytes() == (src_dir / "server.py").read_bytes()

    def test_both_files_recorded_in_manifest_for_every_tool(self, synced_all: Path) -> None:
        manifest = json.loads((synced_all / self.MANIFEST_NAME).read_text())
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


# ── Config-driven behavior (issue #142) ───────────────────


BASE_BRANCH_LIB = REPO_ROOT / "shared" / "hooks" / "lib" / "base-branch.sh"


def _run_sync_with_config(target: Path, config: Path, tool: str = "claude") -> None:
    """Run sync with AI_TOOLKIT_CONFIG pointed at a specific config file."""
    subprocess.run(
        ["bash", str(SYNC_SCRIPT), str(target), tool],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "AI_TOOLKIT_CONFIG": str(config)},
    )


def _git_config(repo: Path, key: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), "config", "--get", key], capture_output=True, text=True
    )


class TestConfigDrivenSync:
    """settings/ai-toolkit.yml is the source of truth for model + base_branch."""

    def test_claude_agent_model_stamped_from_config(self, target_repo: Path) -> None:
        # The real seed config routes architect→opus, debug→opus, tdd-green→sonnet.
        # (architect carried claude-fable-5 until it went unavailable again, then
        # fell back to opus, as during the #218 retirement window.)
        _run_sync(target_repo, "claude")

        agents = target_repo / ".claude" / "agents"
        assert (
            _parse_frontmatter((agents / "architect.md").read_text())["model"] == "claude-opus-4-8"
        )
        assert _parse_frontmatter((agents / "debug.md").read_text())["model"] == "claude-opus-4-8"
        assert (
            _parse_frontmatter((agents / "tdd-green.md").read_text())["model"] == "claude-sonnet-5"
        )

    def test_claude_agent_effort_stamped_from_config(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        agents = target_repo / ".claude" / "agents"
        assert _parse_frontmatter((agents / "architect.md").read_text())["effort"] == "max"

    def test_spoke_env_emitted_with_spoke_model(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        env = target_repo / ".ai-toolkit" / "scripts" / "spoke-model.env"
        assert env.exists()
        text = env.read_text()
        # Budget routing (2026-07-15): routine spokes on Sonnet/high, no 1m tier.
        # (The emitter shell-quotes only values that need it — the old [1m] did;
        # a plain model id is emitted bare.)
        assert "WT_AGENT_MODEL_DEFAULT=claude-opus-4-8" in text
        assert "WT_AGENT_EFFORT_DEFAULT=high" in text

    def test_base_branch_set_from_config(self, target_repo: Path, tmp_path: Path) -> None:
        config = tmp_path / "cfg.yml"
        config.write_text(
            "base_branch: develop\nmodel:\n  spoke:\n    model: claude-opus-4-8[1m]\n"
        )

        _run_sync_with_config(target_repo, config)

        assert _git_config(target_repo, "ai-toolkit.base-branch").stdout.strip() == "develop"

    def test_absent_base_branch_leaves_config_unset(self, target_repo: Path) -> None:
        # The real seed config has an empty base_branch ⇒ auto-detection preserved.
        _run_sync(target_repo, "claude")

        assert _git_config(target_repo, "ai-toolkit.base-branch").returncode != 0

    def test_empty_base_branch_preserves_a_prior_value(
        self, target_repo: Path, tmp_path: Path
    ) -> None:
        # A downstream's own base branch survives a re-sync from an EMPTY source
        # yml (issue #309): the empty ai-toolkit source must NOT clobber the
        # downstream's per-project ai-toolkit.base-branch.
        subprocess.run(
            ["git", "-C", str(target_repo), "config", "ai-toolkit.base-branch", "develop"],
            check=True,
            capture_output=True,
        )
        config = tmp_path / "cfg.yml"
        config.write_text("base_branch:\nmodel:\n  spoke:\n    model: claude-opus-4-8[1m]\n")

        _run_sync_with_config(target_repo, config)

        assert _git_config(target_repo, "ai-toolkit.base-branch").stdout.strip() == "develop"

    def test_base_branch_resolves_via_wt_base_branch(
        self, target_repo: Path, tmp_path: Path
    ) -> None:
        config = tmp_path / "cfg.yml"
        config.write_text(
            "base_branch: develop\nmodel:\n  spoke:\n    model: claude-opus-4-8[1m]\n"
        )

        _run_sync_with_config(target_repo, config)

        got = subprocess.run(
            ["bash", "-c", f'source "{BASE_BRANCH_LIB}"; wt_base_branch "{target_repo}"'],
            capture_output=True,
            text=True,
        )
        assert got.stdout.strip() == "develop"


def _resolve_base_branch(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{BASE_BRANCH_LIB}"; wt_base_branch "{root}"'],
        capture_output=True,
        text=True,
    )


class TestBaseBranchCamelCaseGuard:
    """The camelCase key footgun (issue #309): ai-toolkit.baseBranch flattens to
    `basebranch`, but the resolver reads the hyphenated ai-toolkit.base-branch, so
    a mis-cased hand-set is silently ignored → falls through to origin/HEAD. The
    resolver and the sync must both WARN when the camelCase key is set alone."""

    def test_resolver_warns_when_only_camelcase_key_set(self, target_repo: Path) -> None:
        subprocess.run(
            ["git", "-C", str(target_repo), "config", "ai-toolkit.baseBranch", "develop"],
            check=True,
            capture_output=True,
        )

        got = _resolve_base_branch(target_repo)

        # stdout stays the clean resolved branch (fell through — camelCase ignored);
        # stderr carries the loud warning naming the hyphenated key.
        assert got.stdout.strip() == "main"
        assert "base-branch" in got.stderr
        assert "baseBranch" in got.stderr

    def test_resolver_silent_when_hyphenated_key_set(self, target_repo: Path) -> None:
        # Both keys present: the hyphenated one wins and there is no footgun, so no warn.
        subprocess.run(
            ["git", "-C", str(target_repo), "config", "ai-toolkit.baseBranch", "wrongcase"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(target_repo), "config", "ai-toolkit.base-branch", "develop"],
            check=True,
            capture_output=True,
        )

        got = _resolve_base_branch(target_repo)

        assert got.stdout.strip() == "develop"
        assert got.stderr.strip() == ""

    def test_resolver_silent_when_no_base_branch_config(self, target_repo: Path) -> None:
        got = _resolve_base_branch(target_repo)

        assert got.stdout.strip() == "main"
        assert got.stderr.strip() == ""

    def test_sync_warns_when_only_camelcase_key_set(
        self, target_repo: Path, tmp_path: Path
    ) -> None:
        subprocess.run(
            ["git", "-C", str(target_repo), "config", "ai-toolkit.baseBranch", "develop"],
            check=True,
            capture_output=True,
        )
        config = tmp_path / "cfg.yml"
        config.write_text("base_branch:\nmodel:\n  spoke:\n    model: claude-opus-4-8[1m]\n")

        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), str(target_repo), "claude"],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "AI_TOOLKIT_CONFIG": str(config)},
        )

        assert "base-branch" in result.stdout
        assert "baseBranch" in result.stdout


class TestLocalOnlyExclude:
    """--local-only writes the ai-toolkit paths to the target's .git/info/exclude,
    so a personal deployment never propagates to teammates (per-clone, not committed)."""

    def _exclude(self, target: Path) -> Path:
        return target / ".git" / "info" / "exclude"

    def _run(self, target: Path, tool: str = "claude") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SYNC_SCRIPT), str(target), tool, "--local-only"],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_writes_the_ai_toolkit_block(self, target_repo: Path) -> None:
        self._run(target_repo)

        text = self._exclude(target_repo).read_text()
        assert "# >>> ai-toolkit (local, personal — do not commit) >>>" in text
        assert "/.claude/" in text
        assert "/.ai-toolkit/" in text

    def test_ai_toolkit_paths_are_ignored(self, target_repo: Path) -> None:
        self._run(target_repo)

        for p in (".claude/", ".ai-toolkit/", ".github/hooks/", ".cursor/"):
            r = subprocess.run(
                ["git", "check-ignore", p], cwd=target_repo, capture_output=True, text=True
            )
            assert r.returncode == 0, f"{p} should be locally ignored, got rc={r.returncode}"

    def test_real_github_ci_is_not_ignored(self, target_repo: Path) -> None:
        # SURGICAL: the project's real .github/workflows must never be caught.
        (target_repo / ".github" / "workflows").mkdir(parents=True)
        (target_repo / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
        self._run(target_repo)

        r = subprocess.run(
            ["git", "check-ignore", ".github/workflows/ci.yml"],
            cwd=target_repo,
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0, ".github/workflows/ci.yml must NOT be excluded (real CI)"

    def test_is_idempotent(self, target_repo: Path) -> None:
        self._run(target_repo)
        self._run(target_repo)

        text = self._exclude(target_repo).read_text()
        assert text.count("# >>> ai-toolkit") == 1, "the block must not duplicate on re-sync"

    def test_default_sync_writes_no_exclude(self, target_repo: Path) -> None:
        # Opt-in only: without --local-only, nothing is added (teams that commit
        # ai-toolkit are unaffected).
        _run_sync(target_repo, "claude")

        exclude = self._exclude(target_repo)
        assert not exclude.exists() or "ai-toolkit (local" not in exclude.read_text()
