"""Unit tests for the commit + gate-park timeline nodes (:mod:`telemetry.spoke_tree.commits`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.commits import (
    _COMMIT_FIELD_SEP,
    _commit_events,
    _gate_park_bounds,
    _gate_park_event,
    _gate_park_ms,
    _parse_commits,
)

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
