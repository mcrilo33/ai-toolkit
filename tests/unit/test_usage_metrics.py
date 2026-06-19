"""Unit tests for the cache-reuse ratio helper (telemetry usage metrics).

``cache_reuse_ratio`` summarizes how much of a turn's cached prompt was *reused*
(``cache_read_input_tokens``) versus freshly *written* this turn
(``cache_creation_input_tokens``). It is a pure function over a usage dict, so
the cases below pin both the normal ratio and the zero-denominator contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.usage_metrics import cache_reuse_ratio


class TestCacheReuseRatio:
    def test_normal_ratio(self) -> None:
        # Arrange — 75 read of a 75+25 cache footprint.
        usage = {"cache_read_input_tokens": 75, "cache_creation_input_tokens": 25}

        # Act
        ratio = cache_reuse_ratio(usage)

        # Assert
        assert ratio == 0.75

    def test_all_reads_no_creation(self) -> None:
        # Arrange — pin the upper boundary: everything reused, nothing created.
        usage = {"cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}

        # Act
        ratio = cache_reuse_ratio(usage)

        # Assert
        assert ratio == 1.0

    def test_zero_denominator_returns_zero(self) -> None:
        # Arrange — no cache activity at all (denominator is 0).
        usage = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

        # Act
        ratio = cache_reuse_ratio(usage)

        # Assert
        assert ratio == 0.0
