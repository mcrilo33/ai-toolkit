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


def base_branch(config: dict) -> str:
    """The configured integration branch, or "" for auto-detection.

    Stripped, since this is the one free-text field a human hand-edits and a
    stray trailing space would flow into downstream git-config/branch ops.
    """
    value = config.get("base_branch")
    return value.strip() if isinstance(value, str) else ""
