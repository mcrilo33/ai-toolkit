"""Pure helpers for summarizing a turn's token-usage dict.

These functions operate on the ``usage`` mapping that Anthropic-style LLM
responses carry (``cache_read_input_tokens``, ``cache_creation_input_tokens``,
…). They are dependency-free and side-effect-free so they can be reused across
the telemetry parser, the cost roll-up, and the dashboard.
"""

from __future__ import annotations


def cache_reuse_ratio(usage: dict) -> float:
    """Return the fraction of a turn's prompt cache that was reused.

    The ratio compares cache *reads* (tokens served from an existing prompt
    cache) against the total cache footprint (reads plus the tokens written to
    create the cache this turn). A value near ``1.0`` means the turn reused an
    existing cache; near ``0.0`` means it mostly paid to build one.

    Args:
        usage: A token-usage mapping. ``cache_read_input_tokens`` and
            ``cache_creation_input_tokens`` are read; each defaults to ``0``
            when absent.

    Returns:
        ``cache_read_input_tokens / (cache_read_input_tokens +
        cache_creation_input_tokens)`` as a float, or ``0.0`` when that
        denominator is ``0`` (no cache activity).
    """
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    denominator = cache_read + cache_creation
    if denominator == 0:
        return 0.0
    return cache_read / denominator
