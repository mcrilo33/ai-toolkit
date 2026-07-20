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
      cycle_steps: {red|green|review: {model, effort}}  # delegated steps only
      subagents:   {<agent>: {model, effort}}
      afk:         {answerer: {model, effort}}
      overrides:   {by_label: {<label>: {model, effort}}}

Effort defaults to ``max`` wherever it is omitted.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
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
    """A solo-cycle step's declared (model, effort), or None when unset.

    This is the *declared intent* for a delegated cycle step. The step is
    realized by delegating to a routed subagent (see :data:`CYCLE_STEP_AGENTS` /
    :func:`cycle_step_effective_model`); for each delegated step the declared and
    the effective model are bound self-consistent by a real-config test so a
    drift fails loudly rather than routing silently wrong. Undelegated steps
    (anchor/push) carry no declaration and return None here (fail-open).
    """
    steps = _model_section(config).get("cycle_steps")
    if not isinstance(steps, dict):
        return None
    return _spec(steps.get(step))


# The solo-cycle step → delegate subagent map (issue #182). The delegate is what
# actually runs the step, so it carries the model the step executes on (per-span
# attributable in Langfuse). anchor/push have no delegate — they are mechanical,
# driver-run work — so the effective model fails open to the spoke driver model.
CYCLE_STEP_AGENTS: dict[str, str] = {
    "red": "tdd-red",
    "green": "tdd-green",
    "review": "code-review",
}


def cycle_step_agent(step: str) -> str | None:
    """The subagent a solo-cycle step delegates to, or None when driver-run.

    A static wiring fact (which agent runs each step), independent of the config
    file — so it takes no config. None for anchor/push (mechanical, no delegate)
    and for any unknown step.
    """
    return CYCLE_STEP_AGENTS.get(step)


def cycle_step_effective_model(config: dict, step: str) -> ModelSpec | None:
    """The (model, effort) a cycle step actually runs on, or None to fail open.

    Resolves the step's delegate subagent (:func:`cycle_step_agent`) and returns
    that agent's configured routing (:func:`agent_model`). Returns None — so the
    consumer keeps today's behavior (the spoke driver model) — when the step has
    no delegate (anchor/push/unknown) or the delegate carries no config routing.
    """
    agent = cycle_step_agent(step)
    if agent is None:
        return None
    return agent_model(config, agent)


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


# The canonical yml key for the integration branch. A mis-cased hand-edit
# (`baseBranch`, `base-branch`, …) is a DISTINCT key, so its value is silently
# ignored — the same footgun the git-config key has (issue #309).
_BASE_BRANCH_KEY = "base_branch"


def _normalize_key(key: str) -> str:
    """Collapse a config key to compare across camelCase/kebab/snake spellings."""
    return key.replace("-", "").replace("_", "").lower()


def base_branch_camelcase_warning(config: dict) -> str | None:
    """Warn text when a mis-cased key shadows the canonical ``base_branch`` (#309).

    A hand-edited yml key like ``baseBranch`` or ``base-branch`` is a distinct key
    from the canonical ``base_branch`` the resolver reads, so its value is silently
    ignored and the base branch falls through to auto-detection. Returns a warning
    naming the offending key when such a variant carries a value while
    ``base_branch`` itself is unset/blank; otherwise None (the canonical key present,
    or no variant, means there is no footgun).
    """
    if base_branch(config):
        return None
    for key, value in config.items():
        if key == _BASE_BRANCH_KEY:
            continue
        if _normalize_key(key) == "basebranch" and isinstance(value, str) and value.strip():
            return (
                f"config key {key!r} is ignored; the canonical key is 'base_branch'. "
                f"Rename it to: base_branch: {value.strip()}"
            )
    return None


# --- issue_routing: config-driven upstream target for tooling bugs (issue #332) ---
# ai-toolkit is synced INTO host projects, so a tooling defect must be filed to the
# ai-toolkit upstream repo, not the host git remote. The upstream target is config-driven
# (not hardcoded in agent prose a re-sync would clobber): the resolver layers a fork's
# `git config ai-toolkit.upstream-repo` override over `issue_routing.upstream_repo`, and
# never returns empty — it falls back LOUDLY to the documented hardcoded default so a
# missing/blank config can never silently misroute (AFK principle #2).

# The fail-safe upstream target: returned when neither a git-config override nor the config
# supplies a value. The canonical repo; a fork changes the CONFIG, never this literal.
UPSTREAM_REPO_DEFAULT = "mcrilo33/ai-toolkit"

# The fail-safe host-vs-tooling manifest: ai-toolkit-owned path globs in a host project.
# Used when `issue_routing.tooling_paths` is absent, so classification always has a
# boundary to match against. Kept in step with the list bug-triage.md names as tooling.
TOOLING_PATHS_DEFAULT: list[str] = [
    ".claude/**",
    ".ai-toolkit/**",
    ".cursor/**",
    ".github/agents/**",
    ".github/instructions/**",
    "scripts/telemetry/**",
    "shared/**",
]

# The git-config key a fork sets to reroute tooling defects without editing the config
# file or agent prose (survives until the next re-sync, unlike the file default).
_UPSTREAM_REPO_GIT_KEY = "ai-toolkit.upstream-repo"


def _issue_routing_section(config: dict) -> dict:
    section = config.get("issue_routing")
    return section if isinstance(section, dict) else {}


def _config_upstream_repo(config: dict) -> str | None:
    """The `issue_routing.upstream_repo` value, stripped, or None when absent/blank."""
    value = _issue_routing_section(config).get("upstream_repo")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _git_config_upstream_override() -> str | None:
    """A fork's `git config ai-toolkit.upstream-repo`, stripped, or None.

    Best-effort (AFK principle #6): a missing key, a non-git cwd, or a missing git binary
    degrades to None (no override), never raising into the caller — so a broken probe can
    never fail the routing it merely observes. ``LC_ALL=C`` guards the non-C dev-host locale
    (the ``git`` invocation itself is ASCII, but keep it deterministic).
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get", _UPSTREAM_REPO_GIT_KEY],
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except Exception:
        # A read-only probe must NEVER propagate (AFK #6): beyond OSError (missing git),
        # a non-UTF-8 decode under text=True would raise ValueError — all degrade to None.
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def upstream_repo(config: dict) -> str:
    """The upstream repo tooling defects are filed to (issue #332).

    Resolves in priority order, NEVER returning empty (AFK principle #2 — a fail-safe
    default over a silent misroute):

    1. ``git config ai-toolkit.upstream-repo`` — a fork's override;
    2. ``issue_routing.upstream_repo`` in ``settings/ai-toolkit.yml``;
    3. the hardcoded :data:`UPSTREAM_REPO_DEFAULT`.

    When it falls through to the hardcoded default because the config lacks the key,
    :func:`upstream_repo_fallback_warning` surfaces that loudly.

    Args:
        config: The parsed config mapping (from :func:`load_config`).

    Returns:
        An ``owner/repo`` slug; always non-empty.
    """
    override = _git_config_upstream_override()
    if override:
        return override
    return _config_upstream_repo(config) or UPSTREAM_REPO_DEFAULT


def upstream_repo_fallback_warning(config: dict) -> str | None:
    """Warn text when the config lacks ``issue_routing.upstream_repo`` (issue #332).

    The LOUD half of the never-empty fail-safe: a synced-but-un-migrated (or malformed)
    config missing the key means the routing target is not pinned in the committed source,
    so it is made visible here rather than masked. Returns None when the config supplies the
    key (no fallback in play). A live ``git config`` override is a deliberate reroute, not a
    misconfig, so it does not suppress the warning about the file key being absent — but the
    text names only the missing FILE key (not a resolved target), so it stays accurate
    whether the effective value comes from an override or the hardcoded default.
    """
    if _config_upstream_repo(config) is not None:
        return None
    return (
        f"config key 'issue_routing.upstream_repo' is unset in settings/ai-toolkit.yml; the "
        f"resolver uses a `git config {_UPSTREAM_REPO_GIT_KEY}` override if set, else the "
        f"hardcoded default {UPSTREAM_REPO_DEFAULT!r}. Set the key to pin the tooling-defect "
        f"target in the committed source."
    )


def tooling_paths(config: dict) -> list[str]:
    """The ai-toolkit-owned path globs classifying host-vs-tooling defects (issue #332).

    Returns ``issue_routing.tooling_paths`` when configured, else the non-empty
    :data:`TOOLING_PATHS_DEFAULT` — so the classification always has a manifest to match
    against, never an empty boundary that would route every defect to the host repo.
    """
    value = _issue_routing_section(config).get("tooling_paths")
    if isinstance(value, list) and value:
        return [str(p) for p in value]
    return list(TOOLING_PATHS_DEFAULT)


# --- telemetry: client-side Langfuse settings (issue #228) --------------------
# The config carries only the NON-SECRET, client-facing "where/whether to send"
# telemetry settings; the Langfuse secret (LANGFUSE_BASIC_AUTH / secret key) stays
# in ~/.afk-telemetry, resolved by worktree-lib.sh's wt_resolve_langfuse_auth.
# Consumers layer these as env -> config -> hardcoded default, so an env override
# still wins and an un-migrated (telemetry-less) config keeps today's behavior.


def _telemetry_section(config: dict) -> dict:
    section = config.get("telemetry")
    return section if isinstance(section, dict) else {}


def _langfuse_section(config: dict) -> dict:
    langfuse = _telemetry_section(config).get("langfuse")
    return langfuse if isinstance(langfuse, dict) else {}


def _langfuse_str(config: dict, key: str) -> str | None:
    """A ``telemetry.langfuse.<key>`` string, stripped, or None when absent/blank."""
    value = _langfuse_section(config).get(key)
    if isinstance(value, str):
        return value.strip() or None
    return None


def telemetry_enabled(config: dict) -> bool | None:
    """Whether native OTel telemetry is on, or None when unset (issue #228).

    Returns None when the ``telemetry`` section (or its ``enabled`` key) is absent
    or blank, so the consumer keeps its own default rather than the accessor
    fabricating a toggle it was never given. Accepts a YAML bool or a string; only
    an explicit false-y token (false/0/off/no/disabled, case-insensitive) disables.
    """
    value = _telemetry_section(config).get("enabled")
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        return None
    return text.lower() not in {"false", "0", "off", "no", "disabled"}


def langfuse_host(config: dict) -> str | None:
    """The Langfuse base URL (``telemetry.langfuse.host``), or None when unset."""
    return _langfuse_str(config, "host")


def langfuse_project(config: dict) -> str | None:
    """The Langfuse project (``telemetry.langfuse.project``), or None when unset."""
    return _langfuse_str(config, "project")


def langfuse_public_key(config: dict) -> str | None:
    """The Langfuse public key (``telemetry.langfuse.public_key``), or None.

    Public by design (safe to commit) — the SECRET key never enters the config.
    """
    return _langfuse_str(config, "public_key")


def langfuse_otlp_endpoint(config: dict) -> str | None:
    """The OTLP-HTTP endpoint (``telemetry.langfuse.otlp_endpoint``), or None."""
    return _langfuse_str(config, "otlp_endpoint")


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


# --- hooks: per-project granular gate configuration (issue #334) -------------
# The per-project layer atop the global `enabled` switch: each hook can be
# enabled/disabled and (for commit-quality/test-select) rule-configured
# independently. Every accessor defaults to today's behavior, so an un-migrated
# config (no `hooks:` section) is a byte-for-byte no-op. The bash resolver
# (shared/hooks/lib/enabled.sh) reads the sync-materialized git-config keys; these
# accessors are the yml-reading half consumed by the sync + tests.

# Security guards default ON regardless of any blanket disable and are turned off
# ONLY by their own explicit disable — which must be loud (AFK principle #2; fail
# loud, never silently strip security).
SECURITY_GUARDS: frozenset[str] = frozenset(
    {"secrets-scan", "secrets-scan-revert", "block-no-verify"}
)

# The default allowed commit types for commit-quality — today's fixed VALID_TYPES
# list (shared/hooks/commit-quality.sh), returned when a host configures none.
COMMIT_QUALITY_TYPES_DEFAULT: list[str] = [
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
]


def _bool_token(value: object) -> bool | None:
    """Parse a YAML bool or string token, or None when absent/blank.

    Only an explicit false-y token (false/0/off/no/disabled, case-insensitive)
    is False; any other non-blank value is True. None means "unset" so callers
    keep their own default rather than fabricating a toggle.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        return None
    return text.lower() not in {"false", "0", "off", "no", "disabled"}


def _hooks_section(config: dict) -> dict:
    section = config.get("hooks")
    return section if isinstance(section, dict) else {}


def _hook_section(config: dict, name: str) -> dict:
    hook = _hooks_section(config).get(name)
    return hook if isinstance(hook, dict) else {}


def _hook_enabled_explicit(config: dict, name: str) -> bool | None:
    """The explicit ``hooks.<name>.enabled`` bool, or None when unset/blank."""
    return _bool_token(_hook_section(config, name).get("enabled"))


def hook_enabled(config: dict, name: str) -> bool:
    """Whether hook ``name`` is enabled (issue #334).

    Defaults to True (today's behavior) when unset. A security guard
    (:data:`SECURITY_GUARDS`) stays True under any blanket/absent config and is
    turned off ONLY by its own explicit ``enabled: false`` — the loud opt-out is
    surfaced by :func:`security_guard_disable_warning`.
    """
    explicit = _hook_enabled_explicit(config, name)
    if name in SECURITY_GUARDS:
        return explicit is not False
    return True if explicit is None else explicit


def security_guard_disable_warning(config: dict) -> str | None:
    """Warn text naming every explicitly-disabled security guard, or None (#334).

    The LOUD half of the security fail-safe (AFK principle #2): disabling a
    security guard must never be silent. Returns None when no security guard is
    explicitly disabled.
    """
    disabled = [g for g in sorted(SECURITY_GUARDS) if _hook_enabled_explicit(config, g) is False]
    if not disabled:
        return None
    return (
        f"security guard(s) {', '.join(disabled)} are explicitly DISABLED in "
        "settings/ai-toolkit.yml. A security guard should stay ON — remove the "
        "`enabled: false` (and any ai-toolkit-hook-<name>-off marker) unless this "
        "opt-out is deliberate (AFK principle #2: never silently strip security)."
    )


def commit_quality_types(config: dict) -> list[str]:
    """Allowed commit types for commit-quality, or the conventional default (#334).

    Accepts a YAML list or a space/comma/pipe-separated string. Blank/absent ⇒
    the fixed :data:`COMMIT_QUALITY_TYPES_DEFAULT` list, so an un-migrated config
    keeps today's conventional-commit set.
    """
    value = _hook_section(config, "commit-quality").get("types")
    if isinstance(value, list):
        types = [str(t).strip() for t in value if str(t).strip()]
        return types or list(COMMIT_QUALITY_TYPES_DEFAULT)
    if isinstance(value, str) and value.strip():
        return [t for t in re.split(r"[\s,|]+", value.strip()) if t]
    return list(COMMIT_QUALITY_TYPES_DEFAULT)


def commit_quality_require_anchor(config: dict) -> bool:
    """Whether commit-quality requires an issue anchor; default True (#334)."""
    explicit = _bool_token(_hook_section(config, "commit-quality").get("require_issue_anchor"))
    return True if explicit is None else explicit


def test_select_command(config: dict) -> str | None:
    """Persistent test-select runner command (durable ``TEST_SELECT_CMD``), or None."""
    value = _hook_section(config, "test-select").get("command")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def test_select_skip(config: dict) -> bool:
    """Persistent test-select skip toggle (durable ``TEST_SELECT_SKIP``); default False."""
    explicit = _bool_token(_hook_section(config, "test-select").get("skip"))
    return False if explicit is None else explicit


def _hooks_config_records(config: dict) -> str:
    """Tab-separated materialization records for sync-to-repo.sh (issue #334).

    Two record kinds, one per line — ONLY for explicitly-set values, so a re-sync
    never clobbers a host's own git-config override of an unset key:
      ``enabled<TAB><name><TAB><true|false>`` — per hook the config declares enabled
      ``config<TAB><git-config-key><TAB><value>`` — per explicitly-set rule value
    """
    lines: list[str] = []
    for name in _hooks_section(config):
        explicit = _hook_enabled_explicit(config, name)
        if explicit is not None:
            lines.append(f"enabled\t{name}\t{'true' if explicit else 'false'}")
    cq = _hook_section(config, "commit-quality")
    if cq.get("types"):
        lines.append(
            "config\tai-toolkit.hook.commit-quality.types\t"
            + " ".join(commit_quality_types(config))
        )
    if _bool_token(cq.get("require_issue_anchor")) is not None:
        lines.append(
            "config\tai-toolkit.hook.commit-quality.require-anchor\t"
            f"{'true' if commit_quality_require_anchor(config) else 'false'}"
        )
    ts = _hook_section(config, "test-select")
    if test_select_command(config) is not None:
        lines.append(f"config\tai-toolkit.hook.test-select.command\t{test_select_command(config)}")
    if _bool_token(ts.get("skip")) is not None:
        lines.append(
            f"config\tai-toolkit.hook.test-select.skip\t{'true' if test_select_skip(config) else 'false'}"
        )
    return "\n".join(lines)


def _cli(argv: list[str]) -> str:
    """Emit a config value for sync-to-repo.sh to consume (see :func:`main`)."""
    command = argv[1] if len(argv) > 1 else ""
    config = load_config(argv[2] if len(argv) > 2 else None)
    if command == "base-branch":
        warning = base_branch_camelcase_warning(config)
        if warning:
            # stderr only — stdout carries just the value the bash consumer evals.
            print(f"ai-toolkit: WARNING: {warning}", file=sys.stderr)
        return base_branch(config)
    if command == "enabled":
        return "true" if enabled(config) else "false"
    if command == "upstream-repo":
        warning = upstream_repo_fallback_warning(config)
        if warning:
            # stderr only — stdout carries just the resolved value the bash consumer evals.
            print(f"ai-toolkit: WARNING: {warning}", file=sys.stderr)
        return upstream_repo(config)
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
    if command == "telemetry-env":
        # Emit ONLY the keys the operator explicitly set (issue #228); a blank/absent
        # entry yields no line, so the bash consumer keeps its own env-or-hardcoded
        # default (env -> config -> default). Var names carry a _DEFAULT suffix so the
        # consumer layers them behind a live env override. Values are non-secret URLs/
        # ids, but shell-quote anyway — the consumer evals this output.
        lines = []
        enabled_flag = telemetry_enabled(config)
        if enabled_flag is not None:
            lines.append(f"AI_TOOLKIT_OTEL_DEFAULT={shlex.quote('1' if enabled_flag else '0')}")
        host = langfuse_host(config)
        if host is not None:
            lines.append(f"LANGFUSE_HOST_DEFAULT={shlex.quote(host)}")
        endpoint = langfuse_otlp_endpoint(config)
        if endpoint is not None:
            lines.append(f"AI_TOOLKIT_OTEL_SPAN_ENDPOINT_DEFAULT={shlex.quote(endpoint)}")
        project = langfuse_project(config)
        if project is not None:
            lines.append(f"LANGFUSE_PROJECT_DEFAULT={shlex.quote(project)}")
        public_key = langfuse_public_key(config)
        if public_key is not None:
            lines.append(f"LANGFUSE_PUBLIC_KEY_DEFAULT={shlex.quote(public_key)}")
        return "\n".join(lines)
    if command == "hooks-config":
        # Materialization records for the per-hook config (issue #334): stdout carries
        # the tab-separated key/value records apply_hook_config applies to git config;
        # a disabled security guard is surfaced LOUDLY on stderr (AFK principle #2).
        warning = security_guard_disable_warning(config)
        if warning:
            print(f"ai-toolkit: WARNING: {warning}", file=sys.stderr)
        return _hooks_config_records(config)
    raise SystemExit(
        "ai_toolkit_config: unknown command "
        f"{command!r} (base-branch|enabled|upstream-repo|spoke-env|batch-env|telemetry-env|hooks-config)"
    )


def main() -> None:
    """CLI: ``ai_toolkit_config.py <base-branch|enabled|upstream-repo|spoke-env|batch-env|telemetry-env> [config-path]``.

    A thin bash-facing seam so sync-to-repo.sh can set ``ai-toolkit.base-branch``
    and ``ai-toolkit.enabled``, resolve the tooling-defect ``upstream-repo``, emit the
    spoke-default env file, the hub dispatch
    path can read the batch concurrency cap / stagger, and the telemetry consumers
    can read the client-side Langfuse defaults — without a YAML parser of their own.
    """
    print(_cli(sys.argv))


if __name__ == "__main__":
    main()
