"""Unit tests for the commit + gate-park timeline nodes (:mod:`telemetry.spoke_tree.commits`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.commits import (
    _COMMIT_FIELD_SEP,
    _commit_events,
    _first_commit_at,
    _gate_park_bounds,
    _gate_park_event,
    _gate_park_ms,
    _parse_commits,
)
from telemetry.spoke_tree.observations import _iso_to_epoch

_US = _COMMIT_FIELD_SEP


def _dump(*commits: tuple[str, str, str, list[tuple[int, int, str]]]) -> str:
    lines: list[str] = []
    for sha, authored, subject, numstat in commits:
        lines.append(f"commit{_US}{sha}{_US}{authored}{_US}{subject}")
        lines.extend(f"{a}\t{d}\t{p}" for a, d, p in numstat)
    return "\n".join(lines)


class TestParseCommits:
    def test_parses_sha_message_and_numstat(self) -> None:
        dump = _dump(
            ("a" * 40, "2026-01-02T00:00:00Z", "feat: x", [(10, 2, "f.py"), (3, 0, "g.py")])
        )
        commits = _parse_commits(dump)
        assert len(commits) == 1
        assert commits[0]["sha"] == "a" * 40
        assert commits[0]["message"] == "feat: x"
        assert commits[0]["additions"] == 13
        assert commits[0]["deletions"] == 2
        assert commits[0]["files"] == ["f.py", "g.py"]

    def test_binary_counts_contribute_zero(self) -> None:
        dump = _dump(("b" * 40, "2026-01-02T00:00:00Z", "bin", [(0, 0, "img.png")]))
        assert _parse_commits(dump.replace("0\t0", "-\t-"))[0]["additions"] == 0


class TestFirstCommitAt:
    def test_returns_earliest_author_time_regardless_of_order(self) -> None:
        commits = [
            {"authored_at": "2026-01-02T00:00:05Z"},
            {"authored_at": "2026-01-02T00:00:01Z"},
            {"authored_at": "2026-01-02T00:00:09Z"},
        ]
        assert _first_commit_at(commits) == "2026-01-02T00:00:01Z"

    def test_orders_by_parsed_datetime_across_mixed_iso_forms(self) -> None:
        commits = [
            {"authored_at": "2026-01-02T00:00:03+00:00"},
            {"authored_at": "2026-01-02T00:00:02Z"},
        ]
        assert _first_commit_at(commits) == "2026-01-02T00:00:02Z"

    def test_empty_commits_yields_none(self) -> None:
        assert _first_commit_at([]) is None

    def test_unparseable_author_times_are_skipped(self) -> None:
        commits = [{"authored_at": "not-a-date"}, {"authored_at": "2026-01-02T00:00:04Z"}]
        assert _first_commit_at(commits) == "2026-01-02T00:00:04Z"

    def test_mixed_naive_and_aware_author_times_do_not_crash(self) -> None:
        # A naive vs aware pair is uncomparable; the loop must skip rather than raise TypeError and
        # abort the best-effort land-time build (mirrors _earliest_after's guard).
        commits = [{"authored_at": "2026-01-02T05:00:00"}, {"authored_at": "2026-01-02T04:00:00Z"}]
        assert _first_commit_at(commits) == "2026-01-02T05:00:00"


class TestCommitEvents:
    def test_builds_one_node_per_commit_under_parent(self) -> None:
        commits = [
            {
                "sha": "a" * 40,
                "authored_at": "2026-01-02T00:00:00Z",
                "message": "feat: x",
                "files": ["f.py"],
                "additions": 5,
                "deletions": 1,
            }
        ]
        events = _commit_events(
            commits, spoke_run_id="sp", trace_id="T", cycle=False, parent_for=lambda _t: "ROOT"
        )
        assert len(events) == 1
        body = events[0]["body"]
        assert body["name"] == "commit:aaaaaaa"
        assert body["parentObservationId"] == "ROOT"
        assert body["metadata"]["additions"] == 5


class TestGatePark:
    def _traces(self) -> list:
        return [
            (
                "tr",
                [
                    {
                        "name": "script:gate",
                        "startTime": "2026-01-02T00:00:00Z",
                        "endTime": "2026-01-02T00:00:01Z",
                    },
                    {
                        "type": "GENERATION",
                        "name": "llm_request",
                        "startTime": "2026-01-02T00:00:11Z",
                    },
                ],
            )
        ]

    def test_bounds_span_gate_end_to_resumption(self) -> None:
        assert _gate_park_bounds(self._traces()) == (
            "2026-01-02T00:00:01Z",
            "2026-01-02T00:00:11Z",
        )

    def test_gate_park_ms_is_the_wait(self) -> None:
        assert _gate_park_ms(self._traces()) == 10000

    def test_no_gate_yields_none(self) -> None:
        assert _gate_park_bounds([("tr", [])]) is None

    def test_gate_park_event_carries_wait_name(self) -> None:
        event = _gate_park_event(
            self._traces(), spoke_run_id="sp", trace_id="T", cycle=False, parent_id="ROOT"
        )
        assert event is not None
        assert event["body"]["name"] == "wait:gate-park"
        assert event["body"]["endTime"] == "2026-01-02T00:00:11Z"

    # #345: the park ends at the drain's answer-attempt epoch when present (the true resumption
    # stage_gate_answer_ms already uses), not the first-activity resume that collapses the window
    # under /afk; it falls back to the resume when the epoch is absent or predates the onset.
    def test_bounds_end_at_answer_epoch_when_present(self) -> None:
        answer = _iso_to_epoch("2026-01-02T00:01:41Z")
        assert _gate_park_bounds(self._traces(), answer_epoch=answer) == (
            "2026-01-02T00:00:01Z",
            "2026-01-02T00:01:41Z",
        )

    def test_bounds_fall_back_to_resume_when_answer_epoch_absent(self) -> None:
        assert _gate_park_bounds(self._traces(), answer_epoch=None) == (
            "2026-01-02T00:00:01Z",
            "2026-01-02T00:00:11Z",
        )

    def test_bounds_fall_back_when_answer_epoch_predates_onset(self) -> None:
        stale = _iso_to_epoch("2026-01-01T00:00:00Z")
        assert _gate_park_bounds(self._traces(), answer_epoch=stale) == (
            "2026-01-02T00:00:01Z",
            "2026-01-02T00:00:11Z",
        )

    def test_bounds_use_answer_epoch_even_without_a_resume(self) -> None:
        # Gate but no post-gate activity: the answer epoch still bounds the park (the resume
        # fallback alone would yield None).
        traces = [
            (
                "tr",
                [
                    {
                        "name": "script:gate",
                        "startTime": "2026-01-02T00:00:00Z",
                        "endTime": "2026-01-02T00:00:01Z",
                    }
                ],
            )
        ]
        answer = _iso_to_epoch("2026-01-02T00:01:41Z")
        assert _gate_park_bounds(traces, answer_epoch=answer) == (
            "2026-01-02T00:00:01Z",
            "2026-01-02T00:01:41Z",
        )

    def test_gate_park_ms_widens_to_answer_epoch(self) -> None:
        answer = _iso_to_epoch("2026-01-02T00:01:41Z")
        assert _gate_park_ms(self._traces(), answer_epoch=answer) == 100000

    def test_gate_park_event_ends_at_answer_epoch(self) -> None:
        answer = _iso_to_epoch("2026-01-02T00:01:41Z")
        event = _gate_park_event(
            self._traces(),
            spoke_run_id="sp",
            trace_id="T",
            cycle=False,
            parent_id="ROOT",
            answer_epoch=answer,
        )
        assert event is not None
        assert event["body"]["endTime"] == "2026-01-02T00:01:41Z"
