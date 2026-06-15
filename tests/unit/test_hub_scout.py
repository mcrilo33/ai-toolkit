"""Unit tests for shared/skills/hub/scripts/hub-scout.sh (issue #40 ST5, Phase 3).

Parallel night spokes are blind to each other; collisions waste night work. Before
any spoke starts, a scout analyzes the whole `night` queue and the USER approves a
batching plan. The split is deliberate:
  * DETERMINISTIC (this script): gather the queue + per-issue file-scope hints,
    compute the pairwise raw file-overlap matrix (a set intersection — facts, not a
    verdict), and the critical-path feasibility arithmetic (does an all-serial /
    all-parallel makespan fit before NIGHT_END?). It sources hub-night.sh so there
    is ONE clock and ONE T_task across scout and supervisor.
  * JUDGMENT (a documented Opus agent step, not this script): classify each overlap
    as PARALLEL / SERIAL / MERGE and stamp the verdict into the issues as
    Serial-after: / Merge-into: body lines.

These tests cover the pure deterministic layer by sourcing the script and calling
its functions directly (a source-guard keeps the dossier from running on import).
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
from pathlib import Path

HUB_SCOUT = (
    Path(__file__).resolve().parents[2] / "shared" / "skills" / "hub" / "scripts" / "hub-scout.sh"
)


def _call(fn_call: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_SCOUT}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
    )


def _epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC).timestamp())


# ── scope_hints: explicit Scope: line wins; backtick fallback; else UNKNOWN ──


def test_scope_hints_prefers_explicit_scope_line() -> None:
    body = "Some text\nScope: shared/hooks/foo.sh tests/unit/test_foo.py\nmore"

    result = _call('scope_hints "$BODY"', env={"BODY": body})

    out = result.stdout.split()
    assert "shared/hooks/foo.sh" in out
    assert "tests/unit/test_foo.py" in out
    assert "UNKNOWN" not in out


def test_scope_hints_falls_back_to_backticked_paths() -> None:
    body = "Touches `scripts/spoke-ready.sh` and `dashboard/app.py` here."

    result = _call('scope_hints "$BODY"', env={"BODY": body})

    out = result.stdout.split()
    assert "scripts/spoke-ready.sh" in out
    assert "dashboard/app.py" in out


def test_scope_hints_unknown_when_no_signal() -> None:
    body = "A vague description with no paths and no scope line."

    result = _call('scope_hints "$BODY"', env={"BODY": body})

    assert result.stdout.strip() == "UNKNOWN", "no signal must be an explicit UNKNOWN, not empty"


# ── overlap: the raw file-intersection (facts, not a classification) ──────────


def test_overlap_reports_shared_files() -> None:
    result = _call("overlap 'a.py b.sh c.md' 'b.sh c.md d.py'")

    out = result.stdout.split()
    assert "b.sh" in out and "c.md" in out
    assert "a.py" not in out and "d.py" not in out


def test_overlap_empty_when_disjoint() -> None:
    result = _call("overlap 'a.py b.sh' 'c.md d.py'")

    assert result.stdout.strip() == "", "disjoint scopes have no overlap"


# ── feasibility: makespan bounds vs time-to-wake (one clock with hub-night) ───


def test_parallel_makespan_packs_into_the_cap() -> None:
    # 6 tasks, cap 3, T_task 90 -> ceil(6/3)=2 waves * 90 = 180 min.
    result = _call("parallel_makespan 6")

    assert result.stdout.strip() == "180"


def test_serial_makespan_is_linear() -> None:
    # 6 tasks fully serialized -> 6 * 90 = 540 min.
    result = _call("serial_makespan 6")

    assert result.stdout.strip() == "540"


def test_fits_before_wake_true_when_makespan_under_window() -> None:
    # 23:00 -> 07:00 is 480 min; a 180-min makespan fits.
    now = _epoch(2026, 6, 15, 23, 0)

    result = _call(f"fits_before_wake 180 {now}")

    assert result.returncode == 0, result.stderr


def test_fits_before_wake_false_when_makespan_overflows() -> None:
    # 540-min all-serial makespan does NOT fit the 480-min window.
    now = _epoch(2026, 6, 15, 23, 0)

    result = _call(f"fits_before_wake 540 {now}")

    assert result.returncode != 0, "an over-committed night must be caught before it is wasted"


# ── directive validation: a dangling or self-referential predecessor is refused ─


def test_validate_serial_after_rejects_self_reference() -> None:
    # An issue cannot be Serial-after itself.
    result = _call("validate_serial_after 41 41 '41 42 43'")

    assert result.returncode != 0


def test_validate_serial_after_rejects_dangling_predecessor() -> None:
    # The predecessor must be in the night queue.
    result = _call("validate_serial_after 42 99 '41 42 43'")

    assert result.returncode != 0


def test_validate_serial_after_accepts_real_predecessor() -> None:
    result = _call("validate_serial_after 42 41 '41 42 43'")

    assert result.returncode == 0, result.stderr


# ── doc guards: the Scope: contract + the scout step ──────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "shared" / "skills"


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text()).lower()


def test_start_task_prompts_for_a_scope_line() -> None:
    # start-task asks the author for a Scope: glob list at issue-creation time so
    # the scout's disjointness filter has a high-quality hint for every night issue.
    flat = _flat(SKILLS_DIR / "start-task" / "SKILL.md")
    assert "scope:" in flat, "start-task must record a Scope: line in the issue body"
    assert "glob" in flat or "files" in flat or "paths" in flat


def test_hub_skill_documents_the_scout_step() -> None:
    flat = _flat(SKILLS_DIR / "hub" / "SKILL.md")
    assert "scout" in flat, "the hub skill must document the pre-flight scout step"
    assert "parallel" in flat and "serial" in flat and "merge" in flat, (
        "the scout classifies overlap into PARALLEL / SERIAL / MERGE"
    )
    assert "approve" in flat, "the user approves the batching plan before any spoke starts"


# ── dossier: the rendered output must include the pairwise overlap matrix ──────


def test_dossier_renders_pairwise_overlap(tmp_path: Path) -> None:
    # Two issues both touching shared/foo.sh -> the dossier must surface that
    # overlap as a fact (the scout's headline output, not just per-issue hints).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    (bodies / "1").write_text("Scope: shared/foo.sh tests/test_one.py\n")
    (bodies / "2").write_text("Scope: shared/foo.sh dashboard/app.py\n")
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "issue" ] && [ "$2" = "list" ]; then echo 1; echo 2; exit 0; fi\n'
        'if [ "$1" = "issue" ] && [ "$2" = "view" ]; then cat "$BODIES/$3"; exit 0; fi\n'
        "exit 1\n"
    )
    gh.chmod(0o755)
    env = {
        **os.environ,
        "TZ": "UTC",
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "BODIES": str(bodies),
        "NIGHT_NOW": str(_epoch(2026, 6, 15, 23, 0)),
    }
    result = subprocess.run(["bash", str(HUB_SCOUT)], capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr
    assert "shared/foo.sh" in result.stdout
    assert "#1" in result.stdout and "#2" in result.stdout
    # The overlap line names BOTH issues and the shared file.
    overlap_lines = [ln for ln in result.stdout.splitlines() if "∩" in ln]
    assert overlap_lines, "the dossier must render a pairwise overlap line"
    assert any("shared/foo.sh" in ln for ln in overlap_lines)
