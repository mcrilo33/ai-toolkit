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

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ai_toolkit_config as cfg  # noqa: E402
from metadata_parser import parse  # noqa: E402

REAL_CONFIG = REPO_ROOT / "settings" / "ai-toolkit.yml"
AGENTS_METADATA = REPO_ROOT / "shared" / "agents" / "metadata.yml"
SECRETS_SCAN = REPO_ROOT / "shared" / "hooks" / "secrets-scan.sh"

# A synthetic AWS-key-shaped value: matches secrets-scan's AKIA[0-9A-Z]{16} pattern
# but is not a real credential. Built by concatenation so this test file itself
# carries no contiguous secret literal (which the pre-write scanner would block).
FAKE_SECRET = "AKIA" + "1234567890ABCDEF"

FABLE = "claude-fable-5"
OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-5"

# Mirrors shared/agents/metadata.yml's #141 routing — now sourced from the config.
# architect/planner returned to FABLE when claude-fable-5 became available again
# (they fell back to OPUS during the #218 retirement window).
# (model, effort) per agent — budget routing (2026-07-15): design/judgment agents
# keep max effort on the scarce models; the routine workhorses (reviews, scoping,
# green/refactor) run Sonnet at high effort. Per-issue escalation stays available
# via the lane:reasoning label override.
EXPECTED_AGENT_ROUTING = {
    "architect": (FABLE, "max"),
    "planner": (FABLE, "max"),
    "debug": (OPUS, "max"),
    "security-reviewer": (OPUS, "max"),
    "code-review": (OPUS, "high"),
    "tdd-red": (OPUS, "high"),
    "devops": (OPUS, "max"),
    "bug-scoper": (OPUS, "high"),
    "tdd-green": (SONNET, "high"),
    "tdd-refactor": (SONNET, "high"),
    "refactor": (SONNET, "high"),
    "documentation": (SONNET, "high"),
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
    model = config["model"]
    assert isinstance(model, dict)
    spoke = model["spoke"]
    assert isinstance(spoke, dict)
    assert spoke["model"] == "claude-opus-4-8[1m]"


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


# ─── cycle_step_agent / cycle_step_effective_model (the delegation consumer) ───


@pytest.mark.parametrize(
    ("step", "agent"),
    [("red", "tdd-red"), ("green", "tdd-green"), ("review", "code-review")],
)
def test_cycle_step_agent_returns_delegate(step: str, agent: str) -> None:
    # The step→delegate map is a static wiring fact (which agent runs each step),
    # independent of the config file — so it takes no config argument.
    assert cfg.cycle_step_agent(step) == agent


@pytest.mark.parametrize("step", ["anchor", "push"])
def test_cycle_step_agent_is_none_for_undelegated_step(step: str) -> None:
    # anchor/push are mechanical (driver-run) — no subagent delegate, so the
    # effective model fails open to the spoke driver.
    assert cfg.cycle_step_agent(step) is None


def test_cycle_step_agent_is_none_for_unknown_step() -> None:
    assert cfg.cycle_step_agent("deploy") is None


def test_cycle_step_effective_model_returns_delegate_routing(tmp_path: Path) -> None:
    # GREEN runs on whatever model the green delegate (tdd-green) is routed to —
    # the effective model is read from the delegate's config entry, not hardcoded.
    config = cfg.load_config(
        _write(
            tmp_path,
            "model:\n"
            "  subagents:\n"
            "    tdd-green:\n"
            "      model: claude-sonnet-5\n"
            "      effort: max\n",
        ).as_posix()
    )

    assert cfg.cycle_step_effective_model(config, "green") == ("claude-sonnet-5", "max")


def test_cycle_step_effective_model_fails_open_for_undelegated_step(tmp_path: Path) -> None:
    # push has no delegate ⇒ None ⇒ the consumer keeps the spoke driver model.
    config = cfg.load_config(
        _write(
            tmp_path,
            "model:\n  subagents:\n    tdd-green:\n      model: claude-sonnet-5\n",
        ).as_posix()
    )

    assert cfg.cycle_step_effective_model(config, "push") is None


def test_cycle_step_effective_model_fails_open_when_delegate_unrouted(tmp_path: Path) -> None:
    # green is delegated, but the config routes no tdd-green ⇒ None ⇒ fail-open to
    # today's behavior (the spoke driver model), never a crash or a scarce default.
    config = cfg.load_config(_write(tmp_path, "model:\n  spoke:\n    model: x\n").as_posix())

    assert cfg.cycle_step_effective_model(config, "green") is None


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


# ─── base_branch camelCase key guard (issue #309) ───


@pytest.mark.parametrize("key", ["baseBranch", "basebranch", "base-branch"])
def test_base_branch_camelcase_warning_flags_variant_key(tmp_path: Path, key: str) -> None:
    # A mis-cased key is a DISTINCT key from `base_branch`, so its value is silently
    # ignored → the warning names it and the canonical key.
    config = cfg.load_config(_write(tmp_path, f"{key}: develop\n").as_posix())

    warning = cfg.base_branch_camelcase_warning(config)

    assert warning is not None
    assert "base_branch" in warning
    assert key in warning


def test_base_branch_camelcase_warning_none_for_canonical_key(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "base_branch: develop\n").as_posix())

    assert cfg.base_branch_camelcase_warning(config) is None


def test_base_branch_camelcase_warning_none_when_canonical_also_set(tmp_path: Path) -> None:
    # The canonical key present ⇒ no footgun (it wins), even alongside a variant.
    config = cfg.load_config(
        _write(tmp_path, "base_branch: develop\nbaseBranch: wrongcase\n").as_posix()
    )

    assert cfg.base_branch_camelcase_warning(config) is None


def test_base_branch_camelcase_warning_none_when_variant_blank(tmp_path: Path) -> None:
    # A blank variant carries no value to ignore, so nothing to warn about.
    config = cfg.load_config(_write(tmp_path, "baseBranch:\n").as_posix())

    assert cfg.base_branch_camelcase_warning(config) is None


def test_base_branch_cli_warns_on_variant_key_via_stderr(tmp_path: Path) -> None:
    # stdout carries ONLY the value (empty, since the variant is ignored); the warning
    # goes to stderr so the bash consumer's captured value stays clean.
    path = _write(tmp_path, "baseBranch: develop\n")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "ai_toolkit_config.py"),
            "base-branch",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == ""
    assert "base_branch" in result.stderr
    assert "baseBranch" in result.stderr


def test_base_branch_cli_silent_for_canonical_key(tmp_path: Path) -> None:
    path = _write(tmp_path, "base_branch: develop\n")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "ai_toolkit_config.py"),
            "base-branch",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "develop"
    assert result.stderr.strip() == ""


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


def test_batch_stagger_seconds_zero_is_honored_as_disabled(tmp_path: Path) -> None:
    # Unlike the cap (where 0 ⇒ auto-derive), stagger 0 is a meaningful "off".
    config = cfg.load_config(_write(tmp_path, "batch:\n  stagger_seconds: 0\n").as_posix())

    assert cfg.batch_stagger_seconds(config) == 0


def test_batch_stagger_seconds_none_when_negative(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "batch:\n  stagger_seconds: -5\n").as_posix())

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


def test_real_config_batch_cap_and_stagger(real_config: dict) -> None:
    # Speed routing (2026-07-16, ample budget): cap raised to 8 for more parallel
    # spokes on future disjoint batches, and the stagger pinned to 20s (down from the
    # blank auto-derive ~45) now that #276's testmon pre-warm shrank first-push spikes.
    assert cfg.batch_concurrency_cap(real_config) == 8
    assert cfg.batch_stagger_seconds(real_config) == 20


# ─── real config: seed routing completeness ───


@pytest.fixture(scope="module")
def real_config() -> dict:
    return cfg.load_config(str(REAL_CONFIG))


def test_real_config_spoke_seed(real_config: dict) -> None:
    # Budget routing (2026-07-15): routine spokes on Sonnet/high, no 1m tier;
    # per-issue escalation via lane:reasoning or an explicit Model: line.
    assert cfg.spoke_model(real_config) == ("claude-opus-4-8", "high")


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


@pytest.mark.parametrize(("name", "spec"), sorted(EXPECTED_AGENT_ROUTING.items()))
def test_real_config_agent_routing_matches_seed(
    real_config: dict, name: str, spec: tuple[str, str]
) -> None:
    assert cfg.agent_model(real_config, name) == spec


# ─── real config: cycle-step routing is a live, self-consistent SSOT (issue #182) ───


# Derive from the LIVE map (not a hand-kept literal) so a new delegated step added
# to CYCLE_STEP_AGENTS is automatically drift-guarded here, like the agent roster.
@pytest.mark.parametrize("step", sorted(cfg.CYCLE_STEP_AGENTS))
def test_real_config_cycle_step_declared_model_matches_its_delegate(
    real_config: dict, step: str
) -> None:
    # `model.cycle_steps.<step>` declares the intent; the delegate subagent is the
    # mechanism that delivers it. Bind them: a drift (e.g. cycle_steps.green bumped
    # to Opus while tdd-green stays Sonnet) must fail here, not route silently wrong.
    delegate = cfg.CYCLE_STEP_AGENTS[step]

    assert cfg.cycle_step_model(real_config, step) == cfg.agent_model(real_config, delegate)


@pytest.mark.parametrize("step", sorted(cfg.CYCLE_STEP_AGENTS))
def test_real_config_cycle_step_effective_model_delivers_declared_model(
    real_config: dict, step: str
) -> None:
    # The effective (delegate-resolved) model a step actually runs on equals the
    # declared cycle_steps model — so GREEN demonstrably runs on the cheaper model.
    assert cfg.cycle_step_effective_model(real_config, step) == cfg.cycle_step_model(
        real_config, step
    )


@pytest.mark.parametrize("step", ["anchor", "push"])
def test_real_config_mechanical_steps_fail_open_to_driver(real_config: dict, step: str) -> None:
    # anchor/push have no delegate and are intentionally NOT declared in
    # cycle_steps — both the declared and the effective model resolve to None so
    # the consumer keeps the spoke driver model (the approved fail-open for #182).
    assert cfg.cycle_step_effective_model(real_config, step) is None
    assert cfg.cycle_step_model(real_config, step) is None


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


# ─── telemetry: client-side Langfuse settings (issue #228) ───

# The full seed block, reused by the CLI + real-config tests. Only client-side,
# non-secret settings — the Langfuse SECRET stays in ~/.afk-telemetry, never here.
_TELEMETRY_SEED = (
    "telemetry:\n"
    "  enabled: true\n"
    "  langfuse:\n"
    "    host: http://localhost:3000\n"
    "    project: proj-quicktest\n"
    "    public_key: pk-lf-quicktest\n"
    "    otlp_endpoint: http://localhost:4318\n"
)


def test_telemetry_enabled_true(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "telemetry:\n  enabled: true\n").as_posix())

    assert cfg.telemetry_enabled(config) is True


def test_telemetry_enabled_false(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "telemetry:\n  enabled: false\n").as_posix())

    assert cfg.telemetry_enabled(config) is False


def test_telemetry_enabled_none_when_section_absent(tmp_path: Path) -> None:
    # Absent ⇒ None so the consumer keeps its own default (backward-compat: on),
    # NOT a forced value — the accessor never fabricates a toggle it wasn't given.
    config = cfg.load_config(_write(tmp_path, "base_branch: main\n").as_posix())

    assert cfg.telemetry_enabled(config) is None


def test_telemetry_enabled_none_when_blank(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "telemetry:\n  enabled:\n").as_posix())

    assert cfg.telemetry_enabled(config) is None


def test_telemetry_enabled_off_token_disables(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "telemetry:\n  enabled: off\n").as_posix())

    assert cfg.telemetry_enabled(config) is False


def test_langfuse_host_returns_value(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(tmp_path, "telemetry:\n  langfuse:\n    host: http://lf.example:3000\n").as_posix()
    )

    assert cfg.langfuse_host(config) == "http://lf.example:3000"


def test_langfuse_project_returns_value(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(tmp_path, "telemetry:\n  langfuse:\n    project: proj-abc\n").as_posix()
    )

    assert cfg.langfuse_project(config) == "proj-abc"


def test_langfuse_public_key_returns_value(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(tmp_path, "telemetry:\n  langfuse:\n    public_key: pk-lf-abc\n").as_posix()
    )

    assert cfg.langfuse_public_key(config) == "pk-lf-abc"


def test_langfuse_otlp_endpoint_returns_value(tmp_path: Path) -> None:
    config = cfg.load_config(
        _write(
            tmp_path, "telemetry:\n  langfuse:\n    otlp_endpoint: http://lf.example:4318\n"
        ).as_posix()
    )

    assert cfg.langfuse_otlp_endpoint(config) == "http://lf.example:4318"


def test_langfuse_accessors_none_when_section_absent(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "telemetry:\n  enabled: true\n").as_posix())

    assert cfg.langfuse_host(config) is None
    assert cfg.langfuse_project(config) is None
    assert cfg.langfuse_public_key(config) is None
    assert cfg.langfuse_otlp_endpoint(config) is None


def test_langfuse_host_none_when_blank(tmp_path: Path) -> None:
    config = cfg.load_config(_write(tmp_path, "telemetry:\n  langfuse:\n    host:\n").as_posix())

    assert cfg.langfuse_host(config) is None


# ─── telemetry-env CLI seam (mirrors batch-env) ───


def test_telemetry_env_cli_emits_set_values(tmp_path: Path) -> None:
    path = _write(tmp_path, _TELEMETRY_SEED)

    out = cfg._cli(["ai_toolkit_config.py", "telemetry-env", str(path)])

    assert "AI_TOOLKIT_OTEL_DEFAULT=1" in out
    assert "LANGFUSE_HOST_DEFAULT=http://localhost:3000" in out
    assert "AI_TOOLKIT_OTEL_SPAN_ENDPOINT_DEFAULT=http://localhost:4318" in out
    assert "LANGFUSE_PROJECT_DEFAULT=proj-quicktest" in out
    assert "LANGFUSE_PUBLIC_KEY_DEFAULT=pk-lf-quicktest" in out


def test_telemetry_env_cli_enabled_false_emits_zero(tmp_path: Path) -> None:
    path = _write(tmp_path, "telemetry:\n  enabled: false\n")

    out = cfg._cli(["ai_toolkit_config.py", "telemetry-env", str(path)])

    assert "AI_TOOLKIT_OTEL_DEFAULT=0" in out


def test_telemetry_env_cli_empty_when_unset(tmp_path: Path) -> None:
    # No telemetry section ⇒ no exports, so the bash consumer keeps its own default.
    path = _write(tmp_path, "base_branch: main\n")

    assert cfg._cli(["ai_toolkit_config.py", "telemetry-env", str(path)]) == ""


# ─── real config: telemetry seed ───


def test_real_config_telemetry_enabled(real_config: dict) -> None:
    # The hub captures telemetry, so the seed ships enabled. (Downstream opt-in-off
    # is an explicit `telemetry.enabled: false`, documented — not this repo's seed.)
    assert cfg.telemetry_enabled(real_config) is True


def test_real_config_langfuse_seed(real_config: dict) -> None:
    assert cfg.langfuse_host(real_config) == "http://localhost:3000"
    assert cfg.langfuse_project(real_config) == "proj-quicktest"
    assert cfg.langfuse_public_key(real_config) == "pk-lf-quicktest"
    assert cfg.langfuse_otlp_endpoint(real_config) == "http://localhost:4318"


# ─── secret boundary: secrets-scan blocks a secret in ai-toolkit.yml (issue #228) ───

# ai-toolkit.yml is committed AND synced into downstream projects, so the Langfuse
# SECRET must never live there — only the public host/project/public_key/endpoint.
# These pin the boundary as an enforced invariant, not just a documented one: the
# pre-write secrets-scan hook DENIES (exit 2) a secret-shaped value written to
# ai-toolkit.yml, while the declared-public key value is allowed through.


def _scan_write(content: str) -> subprocess.CompletedProcess[str]:
    """Run the pre-write secrets-scan hook on a Write to settings/ai-toolkit.yml."""
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(REPO_ROOT / "settings" / "ai-toolkit.yml"),
                "content": content,
            },
        }
    )
    return subprocess.run(
        ["bash", str(SECRETS_SCAN)], input=payload, capture_output=True, text=True
    )


def test_secrets_scan_blocks_secret_in_ai_toolkit_yml() -> None:
    # A secret-key-shaped value under telemetry.langfuse in ai-toolkit.yml must be
    # DENIED before the write lands (exit 2), enforcing the "secrets never in the
    # committed config" boundary rather than merely documenting it.
    content = f"telemetry:\n  langfuse:\n    secret_key: {FAKE_SECRET}\n"

    result = _scan_write(content)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "Secret detected" in result.stderr


def test_secrets_scan_allows_public_key_in_ai_toolkit_yml() -> None:
    # The declared-PUBLIC settings (host / project / public_key) are safe to commit,
    # so the seed's own shape must pass — the boundary blocks secrets, not the public
    # surface it is meant to carry.
    content = (
        "telemetry:\n"
        "  enabled: true\n"
        "  langfuse:\n"
        "    host: http://localhost:3000\n"
        "    project: proj-quicktest\n"
        "    public_key: pk-lf-quicktest\n"
        "    otlp_endpoint: http://localhost:4318\n"
    )

    result = _scan_write(content)

    assert result.returncode == 0, result.stdout + result.stderr
