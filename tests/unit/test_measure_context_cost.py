"""Unit tests for the source-measurable loaded-context cost measurer (Issue #83).

The measurer assembles rules / memory / skills / sub-agents / environment from a
worktree, counts each category's tokens (via Anthropic count_tokens or a char/4
fallback), and writes a reusable ``.ai-toolkit/context-cost.json`` manifest. These
AAA tests build a tiny on-disk worktree under ``tmp_path`` and stub the token
counter -- no network is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.measure_context_cost import (
    FLOOR_SOURCE,
    Category,
    CountTokensError,
    Item,
    assemble_agents,
    assemble_categories,
    assemble_items,
    assemble_memory,
    assemble_rules,
    assemble_skills,
    build_manifest,
    measure_categories,
    measure_framework_floor,
    measure_items,
    parse_frontmatter,
    write_manifest,
)

# --- helpers -----------------------------------------------------------------


def _make_worktree(root: Path) -> None:
    """Populate ``root`` with a minimal but realistic loaded-context layout."""
    (root / "CLAUDE.md").write_text("# Project\nRules here.\n", encoding="utf-8")
    rules = root / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "python-style.md").write_text("Use type hints.\n", encoding="utf-8")

    skill_dir = root / ".claude" / "skills" / "afk"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nname: afk\ndescription: "Drain the backlog."\n---\n# AFK\nbody\n',
        encoding="utf-8",
    )

    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "architect.md").write_text(
        "---\nname: architect\ndescription: System design.\n---\nbody\n",
        encoding="utf-8",
    )


def _fixed_counter(value: int):
    """A token counter returning a constant, ignoring its input text."""

    def counter(_text: str) -> int:
        return value

    return counter


def _failing_counter():
    """A token counter that always signals the endpoint is unreachable."""

    def counter(_text: str) -> int:
        raise CountTokensError("unreachable")

    return counter


# --- frontmatter parsing -----------------------------------------------------


def test_parse_frontmatter_reads_name_and_description() -> None:
    text = '---\nname: afk\ndescription: "Drain the backlog."\nother: x\n---\nbody\n'

    front = parse_frontmatter(text)

    assert front["name"] == "afk"
    assert front["description"] == "Drain the backlog."


def test_parse_frontmatter_without_fence_returns_empty() -> None:
    front = parse_frontmatter("# Just a heading\nno frontmatter here\n")

    assert front == {}


# --- assembly ----------------------------------------------------------------


def test_assemble_rules_globs_claude_md_and_rule_files(tmp_path: Path) -> None:
    _make_worktree(tmp_path)

    category = assemble_rules(tmp_path)

    assert category.name == "rules"
    assert "CLAUDE.md" in category.source_files
    assert ".claude/rules/python-style.md" in category.source_files
    assert "Use type hints." in category.text


def test_assemble_skills_builds_name_description_listing(tmp_path: Path) -> None:
    _make_worktree(tmp_path)

    category = assemble_skills(tmp_path)

    assert category.text == "- afk: Drain the backlog."
    assert category.source_files == [".claude/skills/afk/SKILL.md"]


def test_assemble_agents_builds_name_description_listing(tmp_path: Path) -> None:
    _make_worktree(tmp_path)

    category = assemble_agents(tmp_path)

    assert category.name == "sub-agents"
    assert category.text == "- architect: System design."


def test_assemble_skills_ignores_files_without_frontmatter_name(tmp_path: Path) -> None:
    _make_worktree(tmp_path)
    broken = tmp_path / ".claude" / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("# No frontmatter at all\n", encoding="utf-8")

    category = assemble_skills(tmp_path)

    assert category.text == "- afk: Drain the backlog."


def test_assemble_memory_empty_when_absent(tmp_path: Path) -> None:
    _make_worktree(tmp_path)

    category = assemble_memory(tmp_path)

    assert category.text == ""
    assert category.source_files == []


# --- per-item assembly -------------------------------------------------------


def test_assemble_items_yields_one_item_per_rule_file(tmp_path: Path) -> None:
    _make_worktree(tmp_path)

    items = assemble_items(tmp_path)

    rules = [i for i in items if i.category == "rules"]
    names = {i.name for i in rules}
    assert names == {"CLAUDE.md", "python-style.md"}
    by_name = {i.name: i for i in rules}
    assert by_name["python-style.md"].source == ".claude/rules/python-style.md"
    assert "Use type hints." in by_name["python-style.md"].text


def test_assemble_items_yields_one_item_per_skill_by_name(tmp_path: Path) -> None:
    _make_worktree(tmp_path)

    items = assemble_items(tmp_path)

    skills = [i for i in items if i.category == "skills"]
    assert [i.name for i in skills] == ["afk"]
    assert skills[0].text == "- afk: Drain the backlog."
    assert skills[0].source == ".claude/skills/afk/SKILL.md"


def test_assemble_items_yields_one_item_per_agent_by_name(tmp_path: Path) -> None:
    _make_worktree(tmp_path)

    items = assemble_items(tmp_path)

    agents = [i for i in items if i.category == "sub-agents"]
    assert [i.name for i in agents] == ["architect"]


def test_assemble_items_environment_is_single_estimated_item(tmp_path: Path) -> None:
    _make_worktree(tmp_path)

    items = assemble_items(tmp_path)

    env = [i for i in items if i.category == "environment"]
    assert len(env) == 1
    assert env[0].estimated is True


def test_assemble_items_skips_skill_without_frontmatter_name(tmp_path: Path) -> None:
    _make_worktree(tmp_path)
    broken = tmp_path / ".claude" / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")

    skills = [i for i in assemble_items(tmp_path) if i.category == "skills"]

    assert [i.name for i in skills] == ["afk"]


def test_measure_items_returns_tokens_cost_source_per_item() -> None:
    items = [
        Item("rules", "CLAUDE.md", "abcdefgh", ".claude/CLAUDE.md"),
        Item("environment", "environment", "platform: x", "reconstructed", estimated=True),
    ]

    rows = measure_items(items, counter=_fixed_counter(40), price=0.001)

    assert rows[0] == {
        "category": "rules",
        "name": "CLAUDE.md",
        "tokens": 40,
        "cost_usd": 0.04,
        "source": ".claude/CLAUDE.md",
        "estimated": False,
    }
    assert rows[1]["estimated"] is True


def test_measure_items_falls_back_to_char_div_4_when_counter_fails() -> None:
    items = [Item("rules", "x.md", "12345678", "x.md")]  # 8 chars -> 2 tokens

    rows = measure_items(items, counter=_failing_counter(), price=0.001)

    assert rows[0]["tokens"] == 2
    assert rows[0]["estimated"] is True


# --- token math --------------------------------------------------------------


def test_measure_categories_uses_counter_value_for_tokens_and_cost() -> None:
    categories = [Category("rules", "some text", ["CLAUDE.md"])]

    rows = measure_categories(categories, counter=_fixed_counter(100), price=0.001)

    assert rows[0]["tokens"] == 100
    assert rows[0]["cost_usd"] == 0.1
    assert rows[0]["estimated"] is False


def test_measure_categories_falls_back_to_char_div_4_when_counter_fails() -> None:
    categories = [Category("rules", "12345678", [])]  # 8 chars -> 2 tokens

    rows = measure_categories(categories, counter=_failing_counter(), price=0.001)

    assert rows[0]["tokens"] == 2
    assert rows[0]["estimated"] is True


def test_environment_category_estimated_even_when_counter_succeeds() -> None:
    categories = [Category("environment", "platform: x", [], estimated=True)]

    rows = measure_categories(categories, counter=_fixed_counter(50), price=0.001)

    assert rows[0]["tokens"] == 50
    assert rows[0]["estimated"] is True


# --- content hash determinism ------------------------------------------------


def test_content_hash_stable_for_same_input() -> None:
    categories = [Category("rules", "stable text", [])]
    counter = _fixed_counter(10)

    first = measure_categories(categories, counter=counter, price=0.001)
    second = measure_categories(categories, counter=counter, price=0.001)

    assert first[0]["content_hash"] == second[0]["content_hash"]


def test_content_hash_changes_when_input_changes() -> None:
    counter = _fixed_counter(10)

    before = measure_categories([Category("rules", "text A", [])], counter=counter, price=0.001)
    after = measure_categories([Category("rules", "text B", [])], counter=counter, price=0.001)

    assert before[0]["content_hash"] != after[0]["content_hash"]


# --- manifest shape ----------------------------------------------------------


def test_build_manifest_has_required_keys_and_total(tmp_path: Path) -> None:
    _make_worktree(tmp_path)
    categories = assemble_categories(tmp_path)

    manifest = build_manifest(
        categories,
        counter=_fixed_counter(7),
        price=0.001,
        generated_at="2026-06-19T00:00:00+00:00",
        cc_version="1.2.3",
    )

    assert set(manifest) >= {
        "generated_at",
        "cc_version",
        "price_per_token",
        "categories",
        "measured_total_tokens",
        "note",
    }
    rows = cast(list[dict[str, object]], manifest["categories"])
    assert manifest["measured_total_tokens"] == sum(cast(int, r["tokens"]) for r in rows)


def test_write_manifest_round_trips_to_expected_path(tmp_path: Path) -> None:
    _make_worktree(tmp_path)
    manifest = build_manifest(
        assemble_categories(tmp_path),
        counter=_fixed_counter(3),
        price=0.001,
        generated_at="2026-06-19T00:00:00+00:00",
        cc_version=None,
    )

    path = write_manifest(tmp_path, manifest)

    assert path == tmp_path / ".ai-toolkit" / "context-cost.json"
    assert json.loads(path.read_text(encoding="utf-8"))["note"] == manifest["note"]


# --- framework-floor calibration ---------------------------------------------


def test_measure_framework_floor_sums_cached_and_uncached_input(tmp_path: Path) -> None:
    payload: dict[str, object] = {
        "usage": {
            "cache_read_input_tokens": 24000,
            "cache_creation_input_tokens": 600,
            "input_tokens": 5,
        }
    }

    floor = measure_framework_floor(
        cache_path=tmp_path / "floor.json",
        runner=lambda _model: payload,
        version_fn=lambda: "1.2.3",
    )

    assert floor is not None
    assert floor["tokens"] == 24604  # 24000 + 600 + 5 - 1 ("ping")
    assert floor["estimated"] is False
    assert floor["source"] == FLOOR_SOURCE


def test_measure_framework_floor_reads_usage_nested_under_result(tmp_path: Path) -> None:
    payload: dict[str, object] = {
        "result": {"usage": {"cache_read_input_tokens": 100, "input_tokens": 3}}
    }

    floor = measure_framework_floor(
        cache_path=tmp_path / "floor.json",
        runner=lambda _model: payload,
        version_fn=lambda: "v",
    )

    assert floor is not None
    assert floor["tokens"] == 102  # 100 + 0 + 3 - 1


def test_measure_framework_floor_caches_by_version(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(model: str) -> dict[str, object]:
        calls.append(model)
        return {"usage": {"input_tokens": 101}}

    cache = tmp_path / "floor.json"
    first = measure_framework_floor(cache_path=cache, runner=runner, version_fn=lambda: "v1")
    second = measure_framework_floor(cache_path=cache, runner=runner, version_fn=lambda: "v1")

    assert len(calls) == 1  # second call served from the cache
    assert first == second


def test_measure_framework_floor_recalibrates_when_version_changes(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(model: str) -> dict[str, object]:
        calls.append(model)
        return {"usage": {"input_tokens": 101}}

    cache = tmp_path / "floor.json"
    measure_framework_floor(cache_path=cache, runner=runner, version_fn=lambda: "v1")
    measure_framework_floor(cache_path=cache, runner=runner, version_fn=lambda: "v2")

    assert len(calls) == 2  # a new version invalidates the cached floor


def test_measure_framework_floor_force_ignores_cache(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(model: str) -> dict[str, object]:
        calls.append(model)
        return {"usage": {"input_tokens": 101}}

    cache = tmp_path / "floor.json"
    measure_framework_floor(cache_path=cache, runner=runner, version_fn=lambda: "v1")
    measure_framework_floor(cache_path=cache, runner=runner, version_fn=lambda: "v1", force=True)

    assert len(calls) == 2


def test_measure_framework_floor_none_when_runner_fails(tmp_path: Path) -> None:
    floor = measure_framework_floor(
        cache_path=tmp_path / "floor.json",
        runner=lambda _model: None,
        version_fn=lambda: "v1",
    )

    assert floor is None


def test_measure_framework_floor_none_when_no_usage(tmp_path: Path) -> None:
    floor = measure_framework_floor(
        cache_path=tmp_path / "floor.json",
        runner=lambda _model: {"type": "result"},
        version_fn=lambda: "v1",
    )

    assert floor is None


def test_manifest_idempotent_for_same_sources(tmp_path: Path) -> None:
    _make_worktree(tmp_path)
    counter = _fixed_counter(11)

    first = build_manifest(
        assemble_categories(tmp_path),
        counter=counter,
        price=0.001,
        generated_at="2026-06-19T00:00:00+00:00",
        cc_version=None,
    )
    second = build_manifest(
        assemble_categories(tmp_path),
        counter=counter,
        price=0.001,
        generated_at="2026-06-19T00:00:00+00:00",
        cc_version=None,
    )

    assert first["categories"] == second["categories"]
    assert first["measured_total_tokens"] == second["measured_total_tokens"]
