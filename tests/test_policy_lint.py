"""Policy lint (06 section 7): the shared/ content is the product, so its shape is pinned here."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "shared"
CHECKS_TIMEOUT_CAP = 5  # minutes: a deliberate ceiling on suite duration; raising it is a reviewed decision
ALWAYS_ON = {"security", "agent-orchestration", "scientific-integrity"}  # top-level rules without paths:
GUIDELINES = "guidelines"  # becomes CLAUDE.md, always on
MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
CLAUDE_TOOLS = EDIT_TOOLS | {"Bash", "Read", "Grep", "Glob", "WebFetch", "WebSearch", "Agent"}
READ_ONLY_AGENTS = {"architect", "planner", "code-review", "security-reviewer", "bug-scoper", "followup-scoper"}
RULE_KEYS = {"description", "paths"}
SKILL_KEYS = {"name", "description", "argument-hint", "disable-model-invocation", "user-invocable", "allowed-tools",
              "paths", "context", "agent", "when_to_use", "arguments", "model", "effort", "hooks", "shell"}
AGENT_KEYS = {"name", "description", "model", "effort", "tools", "disallowedTools", "skills", "maxTurns", "color"}
COMMAND_KEYS = {"description", "argument-hint", "allowed-tools", "model"}
# deleted mechanisms (06 section 2): nothing under shared/ may point at them
DELETED = re.compile(r"spoke-ready|review-stamp|approve_review|tmux|\bgate/|\.review/|hub-afk|worktree-(new|land|done)"
                     r"|test-select|metadata\.yml|source-task|next-batch|spoke-push|batch-plan|gate-broker"
                     r"|red-proof|reviewer-sep|commit-gauntlet|commit-quality|Tested-RED|todo-ledger|hub-guard|\bready/|/source\b"
                     r"|ai-toolkit\.yml|issue_routing|hub-(status|notify|inject|agent)|transition-log|verify-(skills|rules|agents)"
                     r"|bootstrap-test-suite|sync-to-repo|Cursor|Copilot")
LINE_CAPS = {"skills/solo-cycle/SKILL.md": 120, "skills/coordinate/SKILL.md": 130, "skills/hub/SKILL.md": 60, "skills/afk/SKILL.md": 40,
             "skills/land/SKILL.md": 30, "skills/start-task/SKILL.md": 60,
             "rules/on-demand/workflow.md": 100, "rules/on-demand/planning-hub.md": 50,
             "rules/on-demand/afk-answering.md": 120, "agents/code-review.md": 110}
VERDICT_KEYS = ("verdict", "blockers", "warnings", "tdd_followed", "tests_weakened", "summary")


def frontmatter(path):
    """(fields, body) of a flat-YAML block: `key: value`, `key: [a, b]`, or `key:` + `- item` lines."""
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, f"{path}: no frontmatter"
    fields, key = {}, None
    for line in m.group(1).splitlines():
        if line.startswith("  - ") and key:
            fields[key].append(line[4:].strip().strip("'\""))
        else:
            k, sep, v = line.partition(":")
            assert sep and re.fullmatch(r"[A-Za-z_-]+", k), f"{path}: unparsable frontmatter line {line!r}"
            key, v = k, v.strip()
            if v.startswith("["):
                fields[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            else:
                fields[k] = [] if v == "" else v
    return fields, text[m.end():]


def files(pattern):
    found = sorted(SHARED.glob(pattern))
    assert found, pattern
    return found


def ident(p):
    return pytest.param(p, id=str(p.relative_to(SHARED)))


RULES = [ident(p) for p in files("rules/*.md") + files("rules/on-demand/*.md")]
SKILLS = [ident(p) for p in files("skills/*/SKILL.md")]
AGENTS = [ident(p) for p in files("agents/*.md")]
COMMANDS = [ident(p) for p in files("prompts/*.md")]


def check_description(path, fm):
    d = fm.get("description", "")
    assert d and d != '""', f"{path}: description missing"
    # YAML-safe without a YAML parser: double-quoted, no inner quote or backslash
    assert re.fullmatch(r'"[^"\\]+"', d), f"{path}: description must be one double-quoted line"
    assert len(d) <= 1026, f"{path}: description over 1024 chars"


@pytest.mark.parametrize("path", RULES)
def test_rule_frontmatter(path):
    fm, _ = frontmatter(path)
    check_description(path, fm)
    assert set(fm) <= RULE_KEYS, f"{path}: Cursor/Copilot keys? {set(fm) - RULE_KEYS}"
    name, on_demand = path.stem, path.parent.name == "on-demand"
    if on_demand:
        assert "paths" not in fm  # not loaded by Claude Code at all: skills and answer.sh read it by path
    elif name == GUIDELINES or name in ALWAYS_ON:
        assert "paths" not in fm, f"{name} is always on (no paths:)"
    else:
        assert fm.get("paths"), f"{name}: a conditional rule needs a paths: list (no paths: = always loaded)"
        assert isinstance(fm["paths"], list)


@pytest.mark.parametrize("path", SKILLS)
def test_skill_frontmatter(path):
    fm, _ = frontmatter(path)
    check_description(path, fm)
    assert fm.get("name") == path.parent.name
    assert set(fm) <= SKILL_KEYS, set(fm) - SKILL_KEYS
    if "model" in fm:
        assert fm["model"] in MODELS


@pytest.mark.parametrize("path", AGENTS)
def test_agent_frontmatter(path):
    fm, _ = frontmatter(path)
    check_description(path, fm)
    assert fm.get("name") == path.stem
    assert set(fm) <= AGENT_KEYS, set(fm) - AGENT_KEYS
    assert fm.get("model") in MODELS, f"{path.stem}: model must be a Claude 5 id, got {fm.get('model')!r}"
    assert "[1m]" not in path.read_text().split("\n---\n")[0]
    assert fm.get("effort") in EFFORTS
    denied = {t.strip() for t in fm.get("disallowedTools", "").split(",") if t.strip()}
    assert denied <= CLAUDE_TOOLS  # real Claude Code tool names, not the Copilot ones v1 carried
    for s in fm.get("skills", []):
        assert (SHARED / "skills" / s / "SKILL.md").is_file(), f"{path.stem}: unknown skill {s}"
    if path.stem in READ_ONLY_AGENTS:
        assert EDIT_TOOLS <= denied


@pytest.mark.parametrize("path", COMMANDS)
def test_command_frontmatter(path):
    fm, _ = frontmatter(path)
    check_description(path, fm)
    assert set(fm) <= COMMAND_KEYS, set(fm) - COMMAND_KEYS


def test_code_review_ends_with_the_json_verdict_contract():
    body = (SHARED / "agents" / "code-review.md").read_text()
    assert "REQUEST_CHANGES" in body and "APPROVE" in body
    assert all(f'"{k}"' in body for k in VERDICT_KEYS), "the verdict JSON must name every key"


def test_deleted_skills_and_metadata_files_are_gone():
    gone = ["source-task", "next-batch", "verify-agents", "verify-rules", "verify-skills", "bootstrap-test-suite"]
    assert [g for g in gone if (SHARED / "skills" / g).exists()] == []
    assert not (SHARED / "skills" / "hub" / "scripts").exists()
    assert not (SHARED / "rules" / "afk-design-principles.md").exists()
    assert not [m for c in ("rules", "skills", "agents", "prompts") for m in [SHARED / c / "metadata.yml"] if m.exists()]


def test_no_policy_file_references_a_deleted_mechanism():
    hits = []
    for cat in ("rules", "skills", "agents", "prompts"):
        for p in sorted((SHARED / cat).rglob("*")):
            if p.is_file() and p.suffix in {".md", ".sh", ".py", ".yml", ".yaml", ".json", ".txt", ""}:
                hits += [f"{p.relative_to(SHARED)}:{n}: {m.group(0)}"
                         for n, ln in enumerate(p.read_text(errors="ignore").splitlines(), 1)
                         for m in [DELETED.search(ln)] if m]
    assert hits == []


@pytest.mark.parametrize("rel,cap", sorted(LINE_CAPS.items()))
def test_rewritten_policy_stays_within_its_line_cap(rel, cap):
    assert len((SHARED / rel).read_text().splitlines()) <= cap


def test_ci_checks_job_is_capped_to_keep_suite_duration_visible():
    job = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["checks"]
    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int) and not isinstance(timeout, bool), "checks needs a job-level timeout-minutes"
    assert 0 < timeout <= CHECKS_TIMEOUT_CAP
