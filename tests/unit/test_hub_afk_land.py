"""Mirror tests for hub-afk-land.sh (issue #307 split).

The LAND lane extracted from hub-afk.sh: auto_land, the land-retry and #285 conflict-
resolution lanes, the review-gate consult, answer_pass, the ready/blocked tip probes, and
the #285 conflict-resolve prompt. This is a behaviour-neutral MOVE, so these tests assert
the functions are (a) reachable through the entry lib and (b) physically located in the
module file — the logic itself stays covered by test_hub_afk.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _hub_afk_support import HUB_SCRIPTS_DIR, _call, function_source_file

MODULE = "hub-afk-land.sh"

# Representative public + helper functions the land lane owns.
LAND_FUNCTIONS = [
    "auto_land",
    "answer_pass",
    "_ready_at_tip",
    "_blocked_at_tip",
    "_afk_review_verdict",
    "_afk_phase_max_seconds",
    "_afk_run_with_heartbeat",
    "_afk_land_retry_max",
    "_afk_route_conflict_resolution",
    "_afk_conflict_resolve_prompt",
    "_afk_hub_is_dirty",
    "_afk_stash_hub",
    "_afk_restore_hub",
    "_afk_escalate_land_precondition",
    # #356: the cross-scope land guard.
    "_afk_issue_scope",
    "_afk_land_own_scope",
    "_afk_file_in_scope",
    "_afk_landing_changed_files",
    "_afk_live_scopes",
    "_afk_scope_owner",
    "_afk_land_scope_guard",
]


@pytest.mark.parametrize("fn", LAND_FUNCTIONS)
def test_land_function_is_reachable_through_entry(fn: str) -> None:
    result = _call(f"type -t {fn}")

    assert result.stdout.strip() == "function", (
        f"{fn} is not defined after sourcing hub-afk.sh — the entry does not source "
        f"{MODULE}, or the function was dropped in the move"
    )


@pytest.mark.parametrize("fn", LAND_FUNCTIONS)
def test_land_function_lives_in_module_file(fn: str) -> None:
    src = function_source_file(fn)

    assert src.endswith(MODULE), (
        f"{fn} resolves from {src!r}, not {MODULE} — a behaviour-neutral move must place it "
        f"in the module, not leave it in the entry"
    )


def test_module_file_is_present_and_executable() -> None:
    mod = HUB_SCRIPTS_DIR / MODULE
    import os

    assert mod.is_file(), f"{MODULE} missing"
    assert os.access(mod, os.X_OK), f"{MODULE} is not executable"


def test_phase_max_seconds_defaults_hold() -> None:
    # A pure helper: sanity that the moved code still evaluates (default + numeric guard).
    assert _call("_afk_phase_max_seconds").stdout.strip() == "900"
    assert _call("_afk_phase_max_seconds", env={"AFK_PHASE_MAX_SECONDS": "5"}).stdout.strip() == "5"
    assert (
        _call("_afk_phase_max_seconds", env={"AFK_PHASE_MAX_SECONDS": "x"}).stdout.strip() == "900"
    ), "a non-numeric override falls back to the 900 default"


# ── #356: the cross-scope land guard ─────────────────────────────────────────
# Scope disjointness used to be enforced ONLY at dispatch. A spoke that wrote outside its
# declared Scope: hit no signal at commit, push, or land — so auto_land merged #353's
# out-of-scope telemetry.sh edit, a file live #352 owned, manufacturing a conflict. The land
# guard compares the landing diff against the issue's Scope: and refuses to land an
# out-of-scope file that falls inside a LIVE sibling's Scope: (routing it to a reversible,
# recorded outcome instead of a merge into main).


def _seed_task_md(wt: Path, scope_line: str) -> None:
    """Write the spoke's on-disk contract carrying its declared Scope: line."""
    (wt / ".ai-toolkit").mkdir(parents=True, exist_ok=True)
    (wt / ".ai-toolkit" / "task.md").write_text(f"# task\n\n{scope_line}\nGate: plan\n")


@pytest.mark.parametrize(
    "file,scope,in_scope",
    [
        pytest.param(
            "shared/hooks/lib/telemetry.sh",
            "shared/hooks/lib/telemetry.sh a/b.sh",
            True,
            id="exact",
        ),
        pytest.param("scripts/x.sh", "shared/hooks/lib/telemetry.sh", False, id="not-listed"),
        pytest.param("a/b/c.py", "a/*", True, id="glob-star"),
        pytest.param(
            "shared/hooks/lib/x.sh", "shared/hooks/lib/", True, id="dir-prefix-trailing-slash"
        ),
        pytest.param("shared/hooks/lib/x.sh", "shared/hooks/lib", True, id="dir-prefix-bare"),
        pytest.param(
            "shared/hooks/libextra.sh", "shared/hooks/lib", False, id="bare-dir-no-false-prefix"
        ),
        pytest.param("anything/at/all.sh", "*", True, id="star-owns-everything"),
        pytest.param("other.sh", "a/*", False, id="glob-miss"),
    ],
)
def test_file_in_scope(file: str, scope: str, in_scope: bool) -> None:
    result = _call(f"_afk_file_in_scope '{file}' '{scope}'; echo RC=$?")

    assert (f"RC={'0' if in_scope else '1'}") in result.stdout, result.stdout + result.stderr


def _guard_expr(wt: Path, issue: str, changed: list[str], siblings: dict[str, str | None]) -> str:
    """Build a _afk_land_scope_guard invocation with the diff + live siblings stubbed.

    siblings maps issue-number -> its Scope: line, or None to model a gh fetch failure
    (rc 1). inflight_issues yields the landing issue plus every sibling key.
    """
    nums = "\\n".join([issue, *siblings.keys()])
    changed_lines = "\\n".join(changed)
    cases = " ".join(
        f"{n}) printf '{s}\\n'; return 0;;" if s is not None else f"{n}) return 1;;"
        for n, s in siblings.items()
    )
    return (
        f"inflight_issues() {{ printf '{nums}\\n'; }}; "
        f'_afk_issue_scope() {{ case "$1" in {cases} *) return 1;; esac; }}; '
        f"_afk_landing_changed_files() {{ printf '{changed_lines}\\n'; }}; "
        f"_afk_land_scope_guard '{wt}' {issue}; echo RC=$?"
    )


def test_land_scope_guard_lands_when_diff_in_scope(tmp_path: Path) -> None:
    # Every changed file sits inside the issue's own Scope: → land, no warning (AC5).
    wt = tmp_path / "s353"
    wt.mkdir()
    _seed_task_md(wt, "Scope: scripts/worktree-lib.sh scripts/worktree-otel-lib.sh")
    expr = _guard_expr(
        wt, "353", ["scripts/worktree-lib.sh"], {"352": "shared/hooks/lib/telemetry.sh"}
    )

    r = _call(expr, env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert "WARNING" not in r.stderr, r.stderr


def test_land_scope_guard_refuses_out_of_scope_owned_by_live_sibling(tmp_path: Path) -> None:
    # The #352/#353 replay: #353 lands telemetry.sh (outside its Scope:), which live #352
    # owns → refuse (RC=1), naming the file (AC2, AC6).
    wt = tmp_path / "s353"
    wt.mkdir()
    _seed_task_md(
        wt, "Scope: scripts/worktree-lib.sh scripts/worktree-otel-lib.sh scripts/worktree-gh-lib.sh"
    )
    expr = _guard_expr(
        wt,
        "353",
        ["shared/hooks/lib/telemetry.sh", "scripts/worktree-lib.sh"],
        {"352": "shared/hooks/lib/telemetry.sh"},
    )

    r = _call(expr, env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert "RC=1" in r.stdout, r.stdout + r.stderr
    assert "WARNING: #353" in r.stderr and "telemetry.sh" in r.stderr, r.stderr


def test_land_scope_guard_warns_only_when_no_live_owner(tmp_path: Path) -> None:
    # Out-of-scope file but NO live sibling owns it → warn + land (dead-sibling tier, AC7).
    wt = tmp_path / "s353"
    wt.mkdir()
    _seed_task_md(wt, "Scope: scripts/worktree-lib.sh")
    expr = _guard_expr(wt, "353", ["shared/hooks/lib/telemetry.sh"], {})  # no siblings

    r = _call(expr, env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert "WARNING: #353" in r.stderr and "telemetry.sh" in r.stderr, r.stderr


def test_land_scope_guard_unknown_sibling_scope_fails_closed(tmp_path: Path) -> None:
    # A live sibling whose Scope: cannot be resolved (gh failure) conservatively OWNS the
    # out-of-scope file → refuse (AC4 fail-closed).
    wt = tmp_path / "s353"
    wt.mkdir()
    _seed_task_md(wt, "Scope: scripts/worktree-lib.sh")
    expr = _guard_expr(wt, "353", ["shared/hooks/lib/telemetry.sh"], {"352": None})

    r = _call(expr, env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert "RC=1" in r.stdout, r.stdout + r.stderr


def test_land_scope_guard_star_scope_exempt(tmp_path: Path) -> None:
    # Scope: * owns everything → nothing is out of scope → land silently (AC5).
    wt = tmp_path / "s353"
    wt.mkdir()
    _seed_task_md(wt, "Scope: *")
    expr = _guard_expr(wt, "353", ["anything.sh"], {"352": "anything.sh"})

    r = _call(expr, env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert "WARNING" not in r.stderr, r.stderr


def test_land_scope_guard_no_task_md_lands_loudly(tmp_path: Path) -> None:
    # No .ai-toolkit/task.md → own scope unresolvable → land but LOUD (never silent, AC4).
    wt = tmp_path / "s353"
    wt.mkdir()
    expr = _guard_expr(wt, "353", ["anything.sh"], {})

    r = _call(expr, env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert "356" in r.stderr, r.stderr  # a visible drain signal naming the guard


def test_land_scope_guard_loud_when_base_unresolvable(tmp_path: Path) -> None:
    # The landing base cannot be resolved (the diff-scope comparison cannot run) → land but
    # LOUD, never a silent fail-open (AC4).
    wt = tmp_path / "s353"
    wt.mkdir()
    _seed_task_md(wt, "Scope: a.sh")
    expr = (
        "_afk_landing_changed_files() { return 1; }; "
        "inflight_issues() { printf '353\\n'; }; "
        f"_afk_land_scope_guard '{wt}' 353; echo RC=$?"
    )

    r = _call(expr, env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert "WARNING: #353" in r.stderr and "base" in r.stderr, r.stderr


def _git(repo: Path, *args: str) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ["PATH"],
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def test_landing_changed_files_is_the_merge_base_diff(tmp_path: Path) -> None:
    # The load-bearing crux: the file set is merge-base(default, HEAD)..HEAD — it INCLUDES the
    # branch's new + modified files and EXCLUDES commits added to the default branch after the
    # branch point (so a diverged base never masks or manufactures an out-of-scope file).
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature/9-x")
    (repo / "new.txt").write_text("new\n")
    (repo / "base.txt").write_text("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "work")
    # A commit landed on main AFTER the branch point — must NOT appear in the landing diff.
    _git(repo, "checkout", "-q", "main")
    (repo / "main_only.txt").write_text("m\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main-only")
    _git(repo, "checkout", "-q", "feature/9-x")

    r = _call(
        f"_afk_landing_changed_files '{repo}'",
        env={"MAIN_ROOT": str(repo), "AFK_DEFAULT_BRANCH": "main"},
    )

    assert set(r.stdout.split()) == {"new.txt", "base.txt"}, r.stdout + r.stderr


def test_auto_land_refuses_to_invoke_land_on_live_sibling_collision(tmp_path: Path) -> None:
    # End-to-end wiring: auto_land must NOT call the land script when the landing diff carries
    # a file a live sibling owns (#356). The land recorder stays empty; the refusal is loud.
    spoke = tmp_path / "spoke"
    spoke.mkdir()
    _git(spoke, "init", "-q")
    _git(spoke, "commit", "-q", "--allow-empty", "-m", "init")
    _seed_task_md(spoke, "Scope: scripts/worktree-lib.sh")
    _git(spoke, "tag", "ready/353")

    land_log = tmp_path / "land.log"
    wt_land = tmp_path / "wtland.sh"
    wt_land.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "{land_log}"\n')
    wt_land.chmod(0o755)

    expr = (
        f"inflight_worktrees() {{ printf '{spoke}\\t353\\n'; }}; "
        "inflight_issues() { printf '352\\n353\\n'; }; "
        "_afk_issue_scope() { case \"$1\" in 352) printf 'shared/hooks/lib/telemetry.sh\\n';; *) return 1;; esac; }; "
        "_afk_landing_changed_files() { printf 'shared/hooks/lib/telemetry.sh\\n'; }; "
        "_afk_review_verdict() { printf 'APPROVE\\n'; }; "
        "auto_land"
    )
    r = _call(
        expr,
        env={
            "WT_LAND": str(wt_land),
            "AFK_STATE_DIR": str(tmp_path / "st"),
            "AFK_REVIEW_GATE": "0",
        },
    )

    assert not land_log.exists() or land_log.read_text().strip() == "", (
        "auto_land must refuse to invoke the land script on a live-sibling collision; "
        f"land log={land_log.read_text() if land_log.exists() else '<none>'}\n{r.stderr}"
    )
    assert "WARNING: #353" in r.stderr, r.stderr
