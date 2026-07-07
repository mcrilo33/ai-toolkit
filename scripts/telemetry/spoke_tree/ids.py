"""Deterministic id derivation for the assembled spoke views (#166).

Every id in either view derives from the spoke run id and the source ``(trace_id, observation_id)``
pair, so a rerun resolves to the same trace/observation ids and overwrites in place rather than
appending. This module is the single source of truth for that id namespace: the ``*_PREFIX``
constants and the ``*_id`` functions. It imports nothing from the rest of the package, so it sits
at the bottom of the dependency graph. Family-specific node ids (step / score / loaded-context /
commit / gate-park) live with their family module.
"""

from __future__ import annotations

import hashlib

# Deterministic id prefixes — a rerun resolves to the same trace/observation ids.
_TRACE_PREFIX = "spoketree-"
_ROOT_PREFIX = "spokeroot-"
_COPY_PREFIX = "tree-"
# Guard-group node ids (#157, one per tool / the session root).
_GUARDS_PREFIX = "tree-guards-"
# Synthesized blocked-tool node ids (#157, one per orphaned tool-call id).
_BLOCKED_TOOL_PREFIX = "tree-blocked-"
# View B (#113) id namespace — kept separate so its copies never collide with View A's in the
# local Langfuse store.
_CYCLE_TRACE_PREFIX = "spokecycle-"
_CYCLE_ROOT_PREFIX = "spokecycleroot-"
_CYCLE_COPY_PREFIX = "cyc-"
_CYCLE_STEP_PREFIX = "cycstep-"


def trace_id_for(spoke_run_id: str) -> str:
    """Return the deterministic trace id for a spoke's assembled tree.

    Args:
        spoke_run_id: The spoke run identifier.

    Returns:
        A stable ``spoketree-<sha1[:16]>`` id, identical across reruns.
    """
    return _TRACE_PREFIX + hashlib.sha1(spoke_run_id.encode()).hexdigest()[:16]


def root_id_for(spoke_run_id: str) -> str:
    """Return the deterministic id of the synthetic root span for a spoke."""
    return _ROOT_PREFIX + hashlib.sha1(spoke_run_id.encode()).hexdigest()[:16]


def _copy_id(orig_trace_id: str, orig_obs_id: str) -> str:
    """Return the deterministic copy id for a source observation in the assembled trace."""
    digest = hashlib.sha1(f"{orig_trace_id}:{orig_obs_id}".encode()).hexdigest()[:24]
    return _COPY_PREFIX + digest


def _guards_id(parent_id: str) -> str:
    """Return the deterministic id of the ``guards`` group under ``parent_id`` (a tool / root)."""
    return _GUARDS_PREFIX + hashlib.sha1(parent_id.encode()).hexdigest()[:24]


def _blocked_tool_id(tool_use_id: str) -> str:
    """Return the deterministic id of the ``blocked-tool:*`` node synthesized for a tool-call id."""
    return _BLOCKED_TOOL_PREFIX + hashlib.sha1(tool_use_id.encode()).hexdigest()[:24]


def cycle_trace_id_for(spoke_run_id: str) -> str:
    """Return the deterministic trace id for a spoke's View B (steps -> work) trace."""
    return _CYCLE_TRACE_PREFIX + hashlib.sha1(spoke_run_id.encode()).hexdigest()[:16]


def cycle_root_id_for(spoke_run_id: str) -> str:
    """Return the deterministic id of the View B synthetic root span for a spoke."""
    return _CYCLE_ROOT_PREFIX + hashlib.sha1(spoke_run_id.encode()).hexdigest()[:16]


def _cycle_copy_id(view_a_copy_id: str) -> str:
    """Map a View A copy id into the View B namespace, preserving its stable digest."""
    return _CYCLE_COPY_PREFIX + view_a_copy_id[len(_COPY_PREFIX) :]


def cycle_copy_id_for(orig_trace_id: str, orig_obs_id: str) -> str:
    """Return the deterministic View B copy id for a source observation."""
    return _cycle_copy_id(_copy_id(orig_trace_id, orig_obs_id))


def _cycle_step_id(spoke_run_id: str, key: str) -> str:
    """Return the deterministic id of one View B cycle-axis node (``pre`` / ``post`` / a task id)."""
    digest = hashlib.sha1(f"{spoke_run_id}:cycle:{key}".encode()).hexdigest()[:24]
    return _CYCLE_STEP_PREFIX + digest
