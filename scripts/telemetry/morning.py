"""Per-spoke cost for the night morning report (issue #40, Phase 4).

The morning report needs each spoke's cost without re-deriving it. ``cost_for_issue``
REUSES the #35 pull layer's authoritative ``spoke_run_summary.total_cost_usd`` (the
sum of the run's distinct sessions' ``ccusage`` totals), joining a spoke run to its
issue by the ``spoke_run_id`` branch-prefix shape ``<type>/<issue>-<slug>+<epoch>``.
A re-used issue number or an ambiguous match yields ``None`` (blank cost) rather than
a wrong number.

The ``--cost-for`` CLI is what ``hub-morning.sh`` shells out to per row; it degrades
to blank (prints nothing, exits 0) on any failure — absent ``ccusage``, no telemetry,
import error — so the report never breaks on a missing cost.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing duckdb at module load for the type only
    import duckdb


def cost_for_issue(con: "duckdb.DuckDBPyConnection", issue: int | str) -> float | None:
    """Return the spoke run's total cost (USD) for ``issue``, or ``None``.

    Args:
        con: A ``queries.connect`` DuckDB connection (exposes ``spoke_run_summary``).
        issue: The GitHub issue number.

    Returns:
        The run's ``total_cost_usd`` when exactly one spoke run matches the
        ``%/<issue>-%+%`` id shape with a non-null cost; otherwise ``None``.
    """
    pattern = f"%/{issue}-%+%"
    rows = con.execute(
        "SELECT total_cost_usd FROM spoke_run_summary WHERE spoke_run_id LIKE ?",
        [pattern],
    ).fetchall()
    if len(rows) != 1 or rows[0][0] is None:
        return None
    return float(rows[0][0])


def _print_cost(issue: int, events: Path | None, projects: Path | None) -> None:
    """Best-effort: print the spoke cost for ``issue`` (blank line on any failure)."""
    try:
        from telemetry.cost import load_ccusage_costs
        from telemetry.queries import connect

        events_path = events or (Path.home() / ".ai-toolkit" / "telemetry" / "events.jsonl")
        projects_root = projects or (Path.home() / ".claude" / "projects")
        con = connect(
            events_path=events_path,
            projects_root=projects_root,
            ccusage_costs=load_ccusage_costs(),
        )
        try:
            cost = cost_for_issue(con, issue)
        finally:
            con.close()
        if cost is not None:
            print(f"{cost:.2f}")
    except Exception:
        # Degrade to blank — a missing cost must never break the morning report.
        return


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="night morning-report cost lookup")
    parser.add_argument("--cost-for", type=int, metavar="ISSUE", dest="cost_for")
    parser.add_argument("--events", type=Path, default=None)
    parser.add_argument("--projects", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.cost_for is not None:
        _print_cost(args.cost_for, args.events, args.projects)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
