"""Unit tests for scripts/telemetry/morning.py (issue #40 ST6).

The morning report needs per-spoke cost without re-deriving it. cost_for_issue
REUSES the #35 pull layer's authoritative `spoke_run_summary.total_cost_usd`
(itself the sum of the run's distinct sessions' ccusage totals), joining a spoke
run to its issue by the branch-prefix shape of the spoke_run_id
(`<type>/<issue>-<slug>+<epoch>`). A re-used issue number or an ambiguous match
yields None (blank cost) rather than a wrong number.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.morning import cost_for_issue  # noqa: E402
from telemetry.queries import connect  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
EVENTS = FIXTURES / "events.jsonl"
PROJECTS = FIXTURES / "projects"
SESSION_ID = "11111111-1111-1111-1111-111111111111"
RUN = "feature/22-demo+1700000000"
CCUSAGE = {SESSION_ID: 2.80}


@pytest.fixture()
def con():
    connection = connect(events_path=EVENTS, projects_root=PROJECTS, ccusage_costs=CCUSAGE)
    yield connection
    connection.close()


def test_cost_for_issue_returns_the_spoke_run_total(con) -> None:
    expected = con.execute(
        "SELECT total_cost_usd FROM spoke_run_summary WHERE spoke_run_id = ?", [RUN]
    ).fetchone()[0]

    assert cost_for_issue(con, 22) == pytest.approx(expected)


def test_cost_for_issue_none_when_no_matching_run(con) -> None:
    # No spoke run for issue 999 -> blank cost, never a wrong number.
    assert cost_for_issue(con, 999) is None


def test_cost_for_issue_does_not_confuse_a_longer_issue_number(con) -> None:
    # Issue 2 must NOT match feature/22-demo (the join is on `/<issue>-`).
    assert cost_for_issue(con, 2) is None
