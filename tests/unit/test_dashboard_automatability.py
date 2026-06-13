"""Automatability-candidates query tests (Issue #23, subtask 4 — RED).

The automatability panel ranks human-interaction points by how worth automating
they look: frequency x low decision-variance x on-critical-path. It only
SURFACES candidates — scoring "is it actually automatable" is a later LLM-judge
follow-up. These tests pin the ranking against the fixture's human spans:

- solo-cycle/green/approval — 3 spans, all status success (consistent), on the
  critical path (kind=step).
- solo-cycle/review/question — 2 spans, mixed status (success + warn), on the
  critical path.
- post-edit-format/prompt — 1 span, on a hook (off the critical path).
"""

from __future__ import annotations

from _dashboard_helpers import store


def _candidate(rows, name, phase, human_type):
    for row in rows:
        if (row["name"], row["phase"], row["human_type"]) == (name, phase, human_type):
            return row
    raise KeyError((name, phase, human_type))


def test_only_human_spans_are_candidates():
    rows = store().automatability_candidates()

    # exactly the three human-interaction points in the fixture
    assert len(rows) == 3
    keys = {(r["name"], r["phase"], r["human_type"]) for r in rows}
    assert keys == {
        ("solo-cycle", "green", "approval"),
        ("solo-cycle", "review", "question"),
        ("post-edit-format", None, "prompt"),
    }


def test_ranked_by_score_descending():
    rows = store().automatability_candidates()

    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    top = rows[0]
    assert (top["name"], top["phase"], top["human_type"]) == (
        "solo-cycle",
        "green",
        "approval",
    )


def test_frequent_consistent_on_path_scores_highest():
    green = _candidate(store().automatability_candidates(), "solo-cycle", "green", "approval")

    assert green["frequency"] == 3
    assert green["consistency"] == 1.0  # all 3 spans share status -> low variance
    assert green["on_critical_path"] == 1.0  # all kind=step
    assert green["score"] == 3.0  # 3 * 1.0 * 1.0


def test_mixed_status_lowers_consistency():
    review = _candidate(store().automatability_candidates(), "solo-cycle", "review", "question")

    assert review["frequency"] == 2
    # statuses success + warn -> modal fraction 1/2
    assert review["consistency"] == 0.5
    assert review["on_critical_path"] == 1.0
    assert review["score"] == 1.0  # 2 * 0.5 * 1.0


def test_off_critical_path_scores_zero():
    prompt = _candidate(store().automatability_candidates(), "post-edit-format", None, "prompt")

    assert prompt["on_critical_path"] == 0.0  # span is a hook, not step/lifecycle
    assert prompt["score"] == 0.0


def test_mean_wait_ms_reported():
    green = _candidate(store().automatability_candidates(), "solo-cycle", "green", "approval")

    # waits 4000, 6000, 3000 -> mean 4333.33
    assert round(green["mean_wait_ms"], 2) == 4333.33


def test_min_frequency_filter_excludes_rare_interactions():
    rows = store().automatability_candidates(min_frequency=2)

    keys = {(r["name"], r["phase"], r["human_type"]) for r in rows}
    # the single post-edit-format/prompt interaction drops out
    assert ("post-edit-format", None, "prompt") not in keys
    assert ("solo-cycle", "green", "approval") in keys
