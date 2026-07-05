"""Unit tests for the agent model routing in shared/agents/metadata.yml (issue #141).

Fable credit is scarce; Opus 4.8 is plentiful. The per-agent `claude.model`
frontmatter must therefore route by role: Fable only for the design/plan agents
(`architect`, `planner`), Opus 4.8 for the reasoning-heavy ones, Sonnet 5 for the
capable-but-routine ones, and Haiku for none. Effort stays `max` everywhere.

The tests parse the real metadata file, so any future agent added without an
explicit routing decision fails the completeness check instead of silently
defaulting to a scarce model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from metadata_parser import parse  # noqa: E402

AGENTS_METADATA = REPO_ROOT / "shared" / "agents" / "metadata.yml"

FABLE = "claude-fable-5"
OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-5"

EXPECTED_ROUTING = {
    "architect": FABLE,
    "planner": FABLE,
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
def agents() -> dict[str, dict]:
    return parse(str(AGENTS_METADATA))


def _claude_block(agents: dict[str, dict], name: str) -> dict:
    return agents[name]["__overrides"]["claude"]


def test_every_agent_has_an_explicit_routing_decision(agents: dict[str, dict]) -> None:
    assert set(agents) == set(EXPECTED_ROUTING)


@pytest.mark.parametrize(("name", "model"), sorted(EXPECTED_ROUTING.items()))
def test_agent_model_matches_routing(agents: dict[str, dict], name: str, model: str) -> None:
    assert _claude_block(agents, name)["model"] == model


@pytest.mark.parametrize("name", sorted(EXPECTED_ROUTING))
def test_agent_effort_stays_max(agents: dict[str, dict], name: str) -> None:
    assert _claude_block(agents, name)["effort"] == "max"


@pytest.mark.parametrize(
    "name", sorted(n for n in EXPECTED_ROUTING if n not in ("architect", "planner"))
)
def test_fable_reserved_for_design_and_plan(agents: dict[str, dict], name: str) -> None:
    assert _claude_block(agents, name)["model"] != FABLE
