"""Unit tests for the agent model routing POLICY (issues #141, #142).

Fable budget is exhausted, so no agent may route to it: the design/plan agents
(`architect`, `planner`) now run on Opus 4.8 alongside the reasoning-heavy ones,
Sonnet 5 for the capable-but-routine ones, and Haiku for none. Effort stays
`max` everywhere.

Since #142 the routing lives in `settings/ai-toolkit.yml` (the single source of
truth), NOT in `shared/agents/metadata.yml` frontmatter — sync stamps it into
each agent's frontmatter from the config. These tests therefore read the config;
the completeness check cross-references the live agent roster so any future agent
added without a routing decision fails here instead of silently defaulting to a
scarce model. Config parse mechanics live in test_ai_toolkit_config.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ai_toolkit_config as cfg  # noqa: E402
from metadata_parser import parse  # noqa: E402

AGENTS_METADATA = REPO_ROOT / "shared" / "agents" / "metadata.yml"

FABLE = "claude-fable-5"
OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-5"

EXPECTED_ROUTING = {
    "architect": OPUS,
    "planner": OPUS,
    "debug": OPUS,
    "security-reviewer": OPUS,
    "code-review": OPUS,
    "tdd-red": OPUS,
    "devops": OPUS,
    "tdd-green": SONNET,
    "tdd-refactor": SONNET,
    "refactor": SONNET,
    "documentation": SONNET,
}


@pytest.fixture(scope="module")
def config() -> dict:
    return cfg.load_config()


def test_every_agent_has_an_explicit_routing_decision(config: dict) -> None:
    # Cross-reference the live agent roster, not a frozen copy.
    assert set(cfg.agent_model_overrides(config)) == set(parse(str(AGENTS_METADATA)))


@pytest.mark.parametrize(("name", "model"), sorted(EXPECTED_ROUTING.items()))
def test_agent_model_matches_routing(config: dict, name: str, model: str) -> None:
    assert cfg.agent_model(config, name) == (model, "max")


@pytest.mark.parametrize("name", sorted(EXPECTED_ROUTING))
def test_agent_effort_stays_max(config: dict, name: str) -> None:
    assert cfg.agent_model(config, name)[1] == "max"


@pytest.mark.parametrize("name", sorted(EXPECTED_ROUTING))
def test_no_agent_routes_to_fable(config: dict, name: str) -> None:
    # Fable budget is exhausted — no agent, design/plan included, may route to it.
    assert cfg.agent_model(config, name)[0] != FABLE
