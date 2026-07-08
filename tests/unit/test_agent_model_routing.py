"""Unit tests for the agent model routing POLICY (issues #141, #142).

Fable credit is scarce; Opus 4.8 is plentiful. The per-agent model must
therefore route by role: Fable only for the design/plan agents (`architect`,
`planner`), Opus 4.8 for the reasoning-heavy ones, Sonnet 5 for the
capable-but-routine ones, and Haiku for none. Effort stays `max` everywhere.

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

OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-5"

EXPECTED_ROUTING = {
    # architect/planner carried claude-fable-5 until it was retired (#218); they
    # fell back to opus, the strongest reasoning model still available.
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
    routed = cfg.agent_model(config, name)
    assert routed is not None
    assert routed[1] == "max"


def test_fable_is_fully_retired(config: dict) -> None:
    # claude-fable-5 was retired (#218); no agent may route to it any more.
    for name in EXPECTED_ROUTING:
        routed = cfg.agent_model(config, name)
        assert routed is not None
        assert routed[0] != "claude-fable-5"
