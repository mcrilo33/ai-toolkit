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
    context_summary,
    resolve_content,
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


# --- Subtask 2: content resolution for rule / skill / reasoning context items ---


def test_resolve_content_reads_the_rule_body(tmp_path: Path) -> None:
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "code-quality.md").write_text("# Code Quality\nClarity over cleverness.")

    content = resolve_content("rule", "code-quality", rules_dir=rules)

    assert "Clarity over cleverness" in content


def test_resolve_content_reads_the_skill_body(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "solo-cycle").mkdir(parents=True)
    (skills / "solo-cycle" / "SKILL.md").write_text("# Solo Cycle\nPer-subtask cycle.")

    content = resolve_content("skill", "solo-cycle", skills_dir=skills)

    assert "Per-subtask cycle" in content


def test_resolve_content_passes_reasoning_gist_through() -> None:
    # Reasoning has no file — the privacy-safe gist itself is the content.
    assert resolve_content("reasoning", "Found and fixed the bug") == "Found and fixed the bug"


def test_resolve_content_is_blank_for_a_missing_file(tmp_path: Path) -> None:
    assert resolve_content("rule", "does-not-exist", rules_dir=tmp_path) == ""


def test_context_summary_resolves_the_body_then_summarizes(tmp_path: Path) -> None:
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "code-quality.md").write_text("Clarity over cleverness; surgical changes only.")
    cache = SummaryCache(tmp_path / "summaries.json")
    backend = _CountingComplete("Coding standards: clarity and surgical changes")

    summary = context_summary(
        "rule", "code-quality", cache=cache, rules_dir=rules, complete=backend
    )

    assert summary == "Coding standards: clarity and surgical changes"
    # The backend saw the file body, not the bare rule name.
    assert backend.calls[0][0].startswith("Clarity over cleverness")


def test_context_summary_is_a_cache_hit_on_re_open(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "land").mkdir(parents=True)
    (skills / "land" / "SKILL.md").write_text("Land a finished task from the hub.")
    cache = SummaryCache(tmp_path / "summaries.json")
    backend = _CountingComplete()

    first = context_summary("skill", "land", cache=cache, skills_dir=skills, complete=backend)
    second = context_summary("skill", "land", cache=cache, skills_dir=skills, complete=backend)

    assert first == second
    assert len(backend.calls) == 1  # re-open served from cache, no second LLM call


def test_context_summary_is_blank_when_content_is_missing(tmp_path: Path) -> None:
    cache = SummaryCache(tmp_path / "summaries.json")
    backend = _CountingComplete()

    summary = context_summary("skill", "ghost", cache=cache, skills_dir=tmp_path, complete=backend)

    assert summary == ""
    assert backend.calls == []  # no resolvable content — the model is never called
