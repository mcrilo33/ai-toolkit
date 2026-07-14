"""Unit tests for the agent model routing POLICY (issues #141, #142).

Fable credit is scarce; Opus 4.8 is plentiful. The per-agent model must
therefore route by role: Fable only for the design/plan agents (`architect`,
`planner`), Opus 4.8 for the reasoning-heavy ones, Sonnet 5 for the
capable-but-routine ones, and Haiku for none. Effort routes by role too
(budget policy, 2026-07-15): `max` is reserved for the design/judgment agents;
the routine workhorses run `high`. Per-issue escalation stays available via the
`lane:reasoning` label override in the config.

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

# (model, effort) per agent. Budget routing (2026-07-15): reviews and scoping
# moved to Sonnet/high — the volume drivers of subagent usage — while design and
# judgment agents keep their scarce models at max.
EXPECTED_ROUTING = {
    # architect/planner returned to claude-fable-5 when it became available again
    # (they fell back to opus during the #218 retirement window).
    "architect": (FABLE, "max"),
    "planner": (FABLE, "max"),
    "debug": (OPUS, "max"),
    "security-reviewer": (OPUS, "max"),
    "code-review": (SONNET, "high"),
    "tdd-red": (OPUS, "high"),
    "devops": (OPUS, "max"),
    "bug-scoper": (SONNET, "high"),
    "tdd-green": (SONNET, "high"),
    "tdd-refactor": (SONNET, "high"),
    "refactor": (SONNET, "high"),
    "documentation": (SONNET, "high"),
}

# Agents allowed to burn `max` effort — the design/judgment set only.
MAX_EFFORT_AGENTS = {"architect", "planner", "debug", "security-reviewer", "devops"}


@pytest.fixture(scope="module")
def config() -> dict:
    return cfg.load_config()


def test_every_agent_has_an_explicit_routing_decision(config: dict) -> None:
    # Cross-reference the live agent roster, not a frozen copy.
    assert set(cfg.agent_model_overrides(config)) == set(parse(str(AGENTS_METADATA)))


@pytest.mark.parametrize(("name", "spec"), sorted(EXPECTED_ROUTING.items()))
def test_agent_model_matches_routing(config: dict, name: str, spec: tuple) -> None:
    assert cfg.agent_model(config, name) == spec


@pytest.mark.parametrize("name", sorted(EXPECTED_ROUTING))
def test_max_effort_reserved_for_judgment_agents(config: dict, name: str) -> None:
    # Budget invariant: only the design/judgment agents may run at max effort;
    # every other agent runs high. A new agent bumped to max must be a conscious
    # decision here, not a default.
    routed = cfg.agent_model(config, name)
    assert routed is not None
    expected = "max" if name in MAX_EFFORT_AGENTS else "high"
    assert routed[1] == expected


def test_fable_reserved_for_design_agents(config: dict) -> None:
    # claude-fable-5 is back after the #218 retirement window, but stays scarce:
    # only the design/plan agents (architect, planner) may route to it.
    for name in EXPECTED_ROUTING:
        routed = cfg.agent_model(config, name)
        assert routed is not None
        if name in ("architect", "planner"):
            assert routed[0] == FABLE
        else:
            assert routed[0] != FABLE
