"""Cached LLM summarizer for rule/skill/reasoning context (Issue #68, RED).

A pluggable cheap summarizer that turns a chunk of content into a one-line
"what this is", keyed by content hash and cached so identical content is never
re-summarized — across re-opens and across spokes. The model is configurable
(default a cheap one) and every failure path is fail-soft: a blank summary
never breaks the view.

These tests pin the module contract; the LLM backend is injected (``complete``)
so no test ever touches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.summarizer import (
    DEFAULT_MODEL,
    SummaryCache,
    content_key,
    resolve_model,
    summarize,
)


class _CountingComplete:
    """A fake LLM backend that records calls and returns a canned summary."""

    def __init__(self, summary: str = "a one-line summary") -> None:
        self.summary = summary
        self.calls: list[tuple[str, str]] = []

    def __call__(self, content: str, model: str) -> str:
        self.calls.append((content, model))
        return self.summary


def test_summarize_returns_the_models_one_line(tmp_path: Path) -> None:
    cache = SummaryCache(tmp_path / "summaries.json")
    backend = _CountingComplete("Coding standards: clarity over cleverness.")

    result = summarize("rule body text", cache=cache, complete=backend)

    assert result == "Coding standards: clarity over cleverness."


def test_summarize_caches_so_second_call_is_a_hit(tmp_path: Path) -> None:
    cache = SummaryCache(tmp_path / "summaries.json")
    backend = _CountingComplete()

    first = summarize("identical content", cache=cache, complete=backend)
    second = summarize("identical content", cache=cache, complete=backend)

    assert first == second
    assert len(backend.calls) == 1  # the second call never reached the model


def test_summarize_persists_cache_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "summaries.json"
    backend = _CountingComplete()
    summarize("shared rule content", cache=SummaryCache(path), complete=backend)

    # A fresh cache instance over the same sidecar — a different spoke re-opening.
    reused = summarize("shared rule content", cache=SummaryCache(path), complete=backend)

    assert reused == backend.summary
    assert len(backend.calls) == 1  # reused from the persisted sidecar, no new call


def test_summarize_recomputes_for_different_content(tmp_path: Path) -> None:
    cache = SummaryCache(tmp_path / "summaries.json")
    backend = _CountingComplete()

    summarize("content A", cache=cache, complete=backend)
    summarize("content B", cache=cache, complete=backend)

    assert len(backend.calls) == 2


def test_summarize_is_blank_when_backend_raises(tmp_path: Path) -> None:
    cache = SummaryCache(tmp_path / "summaries.json")

    def _boom(content: str, model: str) -> str:
        raise RuntimeError("network down")

    result = summarize("rule body", cache=cache, complete=_boom)

    assert result == ""


def test_summarize_does_not_cache_a_failure(tmp_path: Path) -> None:
    cache = SummaryCache(tmp_path / "summaries.json")
    calls = {"n": 0}

    def _flaky(content: str, model: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "recovered summary"

    first = summarize("flaky content", cache=cache, complete=_flaky)
    second = summarize("flaky content", cache=cache, complete=_flaky)

    assert first == ""
    assert second == "recovered summary"  # retried — the failure was never cached


def test_summarize_is_blank_for_empty_content_without_calling_model(tmp_path: Path) -> None:
    cache = SummaryCache(tmp_path / "summaries.json")
    backend = _CountingComplete()

    result = summarize("   ", cache=cache, complete=backend)

    assert result == ""
    assert backend.calls == []  # nothing to summarize — never hit the model


def test_resolve_model_prefers_explicit_then_env_then_default(monkeypatch) -> None:
    monkeypatch.delenv("TELEMETRY_SUMMARY_MODEL", raising=False)
    assert resolve_model() == DEFAULT_MODEL

    monkeypatch.setenv("TELEMETRY_SUMMARY_MODEL", "from-env")
    assert resolve_model() == "from-env"
    assert resolve_model("explicit-model") == "explicit-model"


def test_summarize_passes_the_resolved_model_to_the_backend(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TELEMETRY_SUMMARY_MODEL", "configured-model")
    cache = SummaryCache(tmp_path / "summaries.json")
    backend = _CountingComplete()

    summarize("content", cache=cache, complete=backend)

    assert backend.calls[0][1] == "configured-model"


def test_default_model_is_a_cheap_one() -> None:
    assert DEFAULT_MODEL == "deepseek-flash"


def test_content_key_is_stable_and_model_scoped() -> None:
    assert content_key("body", "model-x") == content_key("body", "model-x")
    assert content_key("body", "model-x") != content_key("body", "model-y")
    assert content_key("body", "model-x") != content_key("other", "model-x")
