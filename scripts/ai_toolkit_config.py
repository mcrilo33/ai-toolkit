#!/usr/bin/env python3
"""Read the declarative ai-toolkit config (``settings/ai-toolkit.yml``).

The config is the single source of truth for toolkit behavior — model/effort
routing (spoke driver, sub-agents, cycle steps, AFK answerer, per-label
overrides) and the integration ``base_branch``. This module parses it (stdlib
only, reusing :func:`metadata_parser.read_mapping`) and exposes typed accessors
consumed by the sync pipeline and ``worktree-new.sh``.

Config shape (block-style YAML; see settings/ai-toolkit.yml for the full seed)::

    base_branch: <name-or-empty>
    model:
      spoke:       {model, effort}
      cycle_steps: {anchor|red|green|review|push: {model, effort}}
      subagents:   {<agent>: {model, effort}}
      afk:         {answerer: {model, effort}}
      overrides:   {by_label: {<label>: {model, effort}}}

Effort defaults to ``max`` wherever it is omitted.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from metadata_parser import Value, read_mapping  # noqa: E402

# The config lives at <repo>/settings/ai-toolkit.yml (scripts/ is a sibling).
DEFAULT_CONFIG_PATH = _SCRIPT_DIR.parent / "settings" / "ai-toolkit.yml"

DEFAULT_EFFORT = "max"

# A resolved (model, effort) pair.
ModelSpec = tuple[str, str]


def load_config(path: str | None = None) -> dict[str, Value]:
    """Parse the ai-toolkit config file into a nested mapping.

    Args:
        path: Path to the config file; defaults to ``settings/ai-toolkit.yml``.

    Returns:
        The parsed config (empty dict for an empty/comment-only file).
    """
    return read_mapping(str(path) if path is not None else str(DEFAULT_CONFIG_PATH))


def _spec(block: object) -> ModelSpec | None:
    """Resolve a ``{model, effort}`` block into a (model, effort) pair.

    Returns None when the block is absent or carries no ``model`` (effort alone
    is meaningless), so callers can distinguish "unset" from a real routing.
    """
    if not isinstance(block, dict):
        return None
    model = block.get("model")
    if not model:
        return None
    effort = block.get("effort") or DEFAULT_EFFORT
    return model, effort


def _model_section(config: dict) -> dict:
    section = config.get("model")
    return section if isinstance(section, dict) else {}


def spoke_model(config: dict) -> ModelSpec | None:
    """The spoke driver's (model, effort), or None when unset."""
    return _spec(_model_section(config).get("spoke"))


def agent_model(config: dict, name: str) -> ModelSpec | None:
    """A sub-agent's (model, effort) by agent name, or None when unrouted."""
    subagents = _model_section(config).get("subagents")
    if not isinstance(subagents, dict):
        return None
    return _spec(subagents.get(name))


def cycle_step_model(config: dict, step: str) -> ModelSpec | None:
    """A solo-cycle step's (model, effort), or None when unset.

    Schema-only for now — no consumer selects a model per cycle step yet.
    """
    steps = _model_section(config).get("cycle_steps")
    if not isinstance(steps, dict):
        return None
    return _spec(steps.get(step))


def afk_answerer_model(config: dict) -> ModelSpec | None:
    """The AFK answerer's (model, effort), or None when unset.

    Schema-only for now — the AFK answerer wiring lands in a follow-up.
    """
    afk = _model_section(config).get("afk")
    if not isinstance(afk, dict):
        return None
    return _spec(afk.get("answerer"))


def model_for_label(config: dict, label: str) -> ModelSpec | None:
    """The per-label override (model, effort) for ``label``, or None."""
    overrides = _model_section(config).get("overrides")
    if not isinstance(overrides, dict):
        return None
    by_label = overrides.get("by_label")
    if not isinstance(by_label, dict):
        return None
    return _spec(by_label.get(label))


def agent_model_overrides(config: dict) -> dict[str, dict[str, str]]:
    """Per-agent ``{model, effort}`` overlay for sync frontmatter stamping.

    Returns ``{agent_name: {"model": …, "effort": …}}`` for every routed
    sub-agent — the shape :func:`metadata_parser.apply_overrides` consumes.
    """
    subagents = _model_section(config).get("subagents")
    if not isinstance(subagents, dict):
        return {}
    overrides: dict[str, dict[str, str]] = {}
    for name in subagents:
        spec = agent_model(config, name)
        if spec is not None:
            overrides[name] = {"model": spec[0], "effort": spec[1]}
    return overrides


def _bounded_int(value: object, *, minimum: int) -> int | None:
    """Parse an integer ≥ ``minimum``, or None when unset/blank/non-numeric/too-small.

    The batch settings (issue #151) are hand-edited, so a blank or malformed entry
    must fall back to the consumer's own default rather than silently becoming a real
    value. ``minimum`` distinguishes the cap (≥1: 0 is not a valid ceiling ⇒ auto-derive)
    from the stagger (≥0: 0 is a meaningful "disable the stagger").
    """
    if isinstance(value, bool):  # bool is an int subclass — reject it explicitly
        return None
    if isinstance(value, int):
        return value if value >= minimum else None
    if isinstance(value, str):
        text = value.strip()
        # `.isdigit()` is non-negative-only, so a leading '-' already yields None.
        if text.isdigit():
            n = int(text)
            return n if n >= minimum else None
    return None


def _batch_section(config: dict) -> dict:
    section = config.get("batch")
    return section if isinstance(section, dict) else {}


def batch_concurrency_cap(config: dict) -> int | None:
    """The configured max concurrent spokes, or None to auto-derive (issue #151).

    Blank/absent/non-positive ⇒ None, so the dispatch consumer falls back to
    ``min(2, cores/4)`` rather than treating a mis-edit as an unlimited or zero cap.
    """
    return _bounded_int(_batch_section(config).get("concurrency_cap"), minimum=1)


def batch_stagger_seconds(config: dict) -> int | None:
    """Seconds between consecutive spawns in one batch, or None for the default.

    ``0`` is honored (disable the stagger); blank/absent/negative ⇒ None, so the
    consumer falls back to its own default (45s).
    """
    return _bounded_int(_batch_section(config).get("stagger_seconds"), minimum=0)


def base_branch(config: dict) -> str:
    """The configured integration branch, or "" for auto-detection.

    Stripped, since this is the one free-text field a human hand-edits and a
    stray trailing space would flow into downstream git-config/branch ops.
    """
    value = config.get("base_branch")
    return value.strip() if isinstance(value, str) else ""


def enabled(config: dict) -> bool:
    """Whether toolkit enforcement is on by default (issue #154).

    Defaults to True when the key is absent or blank, so an un-migrated config
    keeps today's full enforcement. Accepts a YAML bool or a string; only an
    explicit false-y value (false/0/off/no/disabled, case-insensitive) disables.
    This is the durable DEFAULT the sync writes to ``git config
    ai-toolkit.enabled``; the sync-safe quick flip is the git-common-dir marker.
    """
    value = config.get("enabled")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"false", "0", "off", "no", "disabled"}


def _cli(argv: list[str]) -> str:
    """Emit a config value for sync-to-repo.sh to consume (see :func:`main`)."""
    command = argv[1] if len(argv) > 1 else ""
    config = load_config(argv[2] if len(argv) > 2 else None)
    if command == "base-branch":
        return base_branch(config)
    if command == "enabled":
        return "true" if enabled(config) else "false"
    if command == "spoke-env":
        spec = spoke_model(config) or ("claude-opus-4-8[1m]", DEFAULT_EFFORT)
        # shell-quote the values: worktree-new.sh sources / evals this output, so
        # an unquoted space or metacharacter would corrupt the assignment (silently
        # unset the default) or execute — even though real model IDs are safe today.
        return (
            f"WT_AGENT_MODEL_DEFAULT={shlex.quote(spec[0])}\n"
            f"WT_AGENT_EFFORT_DEFAULT={shlex.quote(spec[1])}"
        )
    if command == "batch-env":
        # Emit ONLY the values the operator explicitly set (issue #151); a blank
        # entry yields no line so the bash consumer keeps its own default (auto
        # cap, 45s stagger). Values are plain integers, but shell-quote anyway.
        lines = []
        cap = batch_concurrency_cap(config)
        if cap is not None:
            lines.append(f"AI_TOOLKIT_BATCH_CAP={shlex.quote(str(cap))}")
        stagger = batch_stagger_seconds(config)
        if stagger is not None:
            lines.append(f"AI_TOOLKIT_BATCH_STAGGER={shlex.quote(str(stagger))}")
        return "\n".join(lines)
    raise SystemExit(
        f"ai_toolkit_config: unknown command {command!r} (base-branch|enabled|spoke-env|batch-env)"
    )


def main() -> None:
    """CLI: ``ai_toolkit_config.py <base-branch|enabled|spoke-env|batch-env> [config-path]``.

    A thin bash-facing seam so sync-to-repo.sh can set ``ai-toolkit.base-branch``
    and ``ai-toolkit.enabled``, emit the spoke-default env file, and the hub
    dispatch path can read the batch concurrency cap / stagger, without a YAML
    parser of their own.
    """
    print(_cli(sys.argv))


if __name__ == "__main__":
    main()
