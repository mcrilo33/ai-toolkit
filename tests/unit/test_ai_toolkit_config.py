"""Unit tests for the declarative ai-toolkit config (issue #142).

`settings/ai-toolkit.yml` is the single source of truth for toolkit behavior:
the spoke driver's model/effort, every sub-agent's model/effort, the AFK
answerer + per-cycle-step models, per-label overrides, and the integration
`base_branch`. `scripts/ai_toolkit_config.py` parses it (stdlib only, reusing
the metadata.yml reader) and exposes typed accessors that the sync pipeline and
`worktree-new.sh` consume.

These tests cover the parse + accessors against hermetic fixtures, plus a
completeness check pinning the real config's seed routing so a future agent
added without a routing decision fails loudly instead of defaulting to a scarce
model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ai_toolkit_config as cfg  # noqa: E402
from metadata_parser import parse  # noqa: E402

REAL_CONFIG = REPO_ROOT / "settings" / "ai-toolkit.yml"
AGENTS_METADATA = REPO_ROOT / "shared" / "agents" / "metadata.yml"

FABLE = "claude-fable-5"
OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-5"

# Mirrors shared/agents/metadata.yml's #141 routing — now sourced from the config.
EXPECTED_AGENT_ROUTING = {
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


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "ai-toolkit.yml"
    p.write_text(text)
    return p


# ─── parse ───


def test_load_config_returns_nested_mapping(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "base_branch: develop\nmodel:\n  spoke:\n    model: claude-opus-4-8[1m]\n    effort: max\n",
    )

    config = cfg.load_config(str(path))

    assert config["base_branch"] == "develop"
    assert config["model"]["spoke"]["model"] == "claude-opus-4-8[1m]"


# ─── spoke_model ───


def test_spoke_model_returns_model_and_effort(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path,
            "model:\n  spoke:\n    model: claude-opus-4-8[1m]\n    effort: high\n",
        ).as_posix()
    )

    assert cfg.spoke_model(config) == ("claude-opus-4-8[1m]", "high")


def test_spoke_effort_defaults_to_max_when_absent(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(tmp_path, "model:\n  spoke:\n    model: claude-opus-4-8[1m]\n").as_posix()
    )

    assert cfg.spoke_model(config) == ("claude-opus-4-8[1m]", "max")


# ─── agent_model ───


def test_agent_model_returns_routing_for_known_agent(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path,
            "model:\n"
            "  subagents:\n"
            "    architect:\n"
            "      model: claude-fable-5\n"
            "      effort: max\n",
        ).as_posix()
    )

    assert cfg.agent_model(config, "architect") == ("claude-fable-5", "max")


def test_agent_model_is_none_for_unknown_agent(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path, "model:\n  subagents:\n    architect:\n      model: claude-fable-5\n"
        ).as_posix()
    )

    assert cfg.agent_model(config, "nonexistent") is None


def test_agent_effort_defaults_to_max_when_absent(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path, "model:\n  subagents:\n    debug:\n      model: claude-opus-4-8\n"
        ).as_posix()
    )

    assert cfg.agent_model(config, "debug") == ("claude-opus-4-8", "max")


def test_agent_model_is_none_when_model_missing(tmp_path: Path) -> None:
    # A block carrying only effort (no model) is not a valid routing.
    config = cfg.load_config(
        _write(tmp_path, "model:\n  subagents:\n    debug:\n      effort: max\n").as_posix()
    )

    assert cfg.agent_model(config, "debug") is None


# ─── cycle_step_model ───


def test_cycle_step_model_returns_routing(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path,
            "model:\n  cycle_steps:\n    green:\n      model: claude-sonnet-5\n      effort: high\n",
        ).as_posix()
    )

    assert cfg.cycle_step_model(config, "green") == ("claude-sonnet-5", "high")


def test_cycle_step_effort_defaults_to_max(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path, "model:\n  cycle_steps:\n    red:\n      model: claude-opus-4-8\n"
        ).as_posix()
    )

    assert cfg.cycle_step_model(config, "red") == ("claude-opus-4-8", "max")


def test_cycle_step_model_is_none_for_unknown_step(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(tmp_path, "model:\n  cycle_steps:\n    red:\n      model: x\n").as_posix()
    )

    assert cfg.cycle_step_model(config, "deploy") is None


# ─── afk_answerer_model ───


def test_afk_answerer_model_returns_routing(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path,
            "model:\n  afk:\n    answerer:\n      model: claude-opus-4-8\n      effort: max\n",
        ).as_posix()
    )

    assert cfg.afk_answerer_model(config) == ("claude-opus-4-8", "max")


def test_afk_answerer_model_is_none_when_absent(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "model:\n  spoke:\n    model: x\n").as_posix())

    assert cfg.afk_answerer_model(config) is None


# ─── read_mapping edge ───


def test_load_config_empty_file_is_empty_mapping(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "# only a comment\n").as_posix())

    assert config == {}


# ─── base_branch ───


def test_base_branch_returns_value(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "base_branch: develop\n").as_posix())

    assert cfg.base_branch(config) == "develop"


def test_base_branch_empty_when_blank(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(tmp_path, "base_branch:\nmodel:\n  spoke:\n    model: x\n").as_posix()
    )

    assert cfg.base_branch(config) == ""


def test_base_branch_empty_when_absent(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "model:\n  spoke:\n    model: x\n").as_posix())

    assert cfg.base_branch(config) == ""


# ─── model_for_label ───


def test_model_for_label_returns_override(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path,
            "model:\n"
            "  overrides:\n"
            "    by_label:\n"
            "      performance:\n"
            "        model: claude-opus-4-8[1m]\n"
            "        effort: max\n",
        ).as_posix()
    )

    assert cfg.model_for_label(config, "performance") == ("claude-opus-4-8[1m]", "max")


def test_model_for_label_is_none_when_unmatched(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path, "model:\n  overrides:\n    by_label:\n      perf:\n        model: x\n"
        ).as_posix()
    )

    assert cfg.model_for_label(config, "enhancement") is None


# ─── batch settings: concurrency cap + stagger (issue #151) ───


def test_batch_concurrency_cap_returns_int(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "batch:\n  concurrency_cap: 3\n").as_posix())

    assert cfg.batch_concurrency_cap(config) == 3


def test_batch_concurrency_cap_none_when_blank(tmp_path: Path) -> None:
    # A blank value ⇒ auto-derivation (min(2, cores/4)) at the consumer, not a cap.
    config = cfg.load_config(_write(tmp_path, "batch:\n  concurrency_cap:\n").as_posix())

    assert cfg.batch_concurrency_cap(config) is None


def test_batch_concurrency_cap_none_when_section_absent(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "base_branch: main\n").as_posix())

    assert cfg.batch_concurrency_cap(config) is None


def test_batch_concurrency_cap_none_when_non_positive(tmp_path: Path) -> None:
    # A zero / negative / non-numeric override is not a valid ceiling ⇒ auto-derive.
    config = cfg.load_config(_write(tmp_path, "batch:\n  concurrency_cap: 0\n").as_posix())

    assert cfg.batch_concurrency_cap(config) is None


def test_batch_stagger_seconds_returns_int(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "batch:\n  stagger_seconds: 30\n").as_posix())

    assert cfg.batch_stagger_seconds(config) == 30


def test_batch_stagger_seconds_none_when_blank(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "batch:\n  stagger_seconds:\n").as_posix())

    assert cfg.batch_stagger_seconds(config) is None


def test_batch_env_cli_emits_set_values(tmp_path: Path) -> None:
    path = _write(tmp_path, "batch:\n  concurrency_cap: 3\n  stagger_seconds: 20\n")

    out = cfg._cli(["ai_toolkit_config.py", "batch-env", str(path)])

    assert "AI_TOOLKIT_BATCH_CAP=3" in out
    assert "AI_TOOLKIT_BATCH_STAGGER=20" in out


def test_batch_env_cli_empty_when_unset(tmp_path: Path) -> None:
    # Blank config ⇒ no exports, so the bash consumer falls back to its own default.
    path = _write(tmp_path, "batch:\n  concurrency_cap:\n  stagger_seconds:\n")

    assert cfg._cli(["ai_toolkit_config.py", "batch-env", str(path)]) == ""


def test_real_config_batch_defaults_to_autoderive(real_config: dict) -> None:
    # The seed leaves both blank ⇒ the consumer auto-derives the cap and default stagger.
    assert cfg.batch_concurrency_cap(real_config) is None
    assert cfg.batch_stagger_seconds(real_config) is None


# ─── real config: seed routing completeness ───


@pytest.fixture(scope="module")
def real_config() -> dict:
    return cfg.load_config(str(REAL_CONFIG))


def test_real_config_spoke_seed(real_config: dict) -> None:
    assert cfg.spoke_model(real_config) == ("claude-opus-4-8[1m]", "max")


def test_real_config_base_branch_defaults_to_autodetect(real_config: dict) -> None:
    assert cfg.base_branch(real_config) == ""


def test_real_config_routes_every_agent_in_the_roster(real_config: dict) -> None:
    # Cross-check against the LIVE agent roster (shared/agents/metadata.yml), not
    # a hand-maintained copy: a new agent added there without a config routing
    # decision must fail here instead of silently defaulting to a scarce model.
    roster = set(parse(str(AGENTS_METADATA)))

    assert set(real_config["model"]["subagents"]) == roster


def test_seed_routing_matches_the_agent_roster(real_config: dict) -> None:
    # Guards the EXPECTED_AGENT_ROUTING literal itself against drift.
    assert set(EXPECTED_AGENT_ROUTING) == set(parse(str(AGENTS_METADATA)))


@pytest.mark.parametrize(("name", "model"), sorted(EXPECTED_AGENT_ROUTING.items()))
def test_real_config_agent_routing_matches_seed(real_config: dict, name: str, model: str) -> None:
    assert cfg.agent_model(real_config, name) == (model, "max")


# ─── agent_model_overrides (the sync-stamping overlay source) ───


def test_agent_model_overrides_maps_every_subagent(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path,
            "model:\n"
            "  subagents:\n"
            "    architect:\n"
            "      model: claude-fable-5\n"
            "      effort: max\n"
            "    debug:\n"
            "      model: claude-opus-4-8\n",
        ).as_posix()
    )

    assert cfg.agent_model_overrides(config) == {
        "architect": {"model": "claude-fable-5", "effort": "max"},
        "debug": {"model": "claude-opus-4-8", "effort": "max"},
    }


def test_agent_model_overrides_empty_without_subagents(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "model:\n  spoke:\n    model: x\n").as_posix())

    assert cfg.agent_model_overrides(config) == {}


def test_real_config_overrides_cover_the_roster(real_config: dict) -> None:
    assert set(cfg.agent_model_overrides(real_config)) == set(parse(str(AGENTS_METADATA)))
