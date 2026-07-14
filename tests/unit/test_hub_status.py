"""Unit tests for shared/skills/hub/scripts/hub-status.sh.

The dashboard's worktree state label drives the hub's merge proposals, so the
push/mergeable classification must be correct: push state is measured against
the branch's own upstream, mergeability against the default branch. A `gh` stub
keeps the issue-survey section hermetic (no network, no real GitHub remote).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

HUB_STATUS = (
    Path(__file__).resolve().parents[2] / "shared" / "skills" / "hub" / "scripts" / "hub-status.sh"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def hub_with_spokes(tmp_path: Path) -> Path:
    """A hub (main checkout) with two spoke worktrees: one fully pushed and one
    with a local-only commit."""
    remote = tmp_path / "remote.git"
    hub = tmp_path / "hub"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "remote", "add", "origin", str(remote))
    _git(hub, "push", "-q", "-u", "origin", "main")

    # Pushed spoke: a commit on its branch, pushed to its upstream.
    pushed = tmp_path / "pushed"
    _git(hub, "worktree", "add", "-q", "-b", "feature/1-pushed", str(pushed))
    (pushed / "a.txt").write_text("a\n")
    _git(pushed, "add", "a.txt")
    _git(pushed, "commit", "-qm", "feat: a", "-m", "Refs #1")
    _git(pushed, "push", "-q", "-u", "origin", "feature/1-pushed")

    # Unpushed spoke: a local-only commit, no upstream.
    unpushed = tmp_path / "unpushed"
    _git(hub, "worktree", "add", "-q", "-b", "feature/2-unpushed", str(unpushed))
    (unpushed / "b.txt").write_text("b\n")
    _git(unpushed, "add", "b.txt")
    _git(unpushed, "commit", "-qm", "feat: b", "-m", "Refs #2")

    # Pushed-but-not-ahead spoke: branched from main, pushed, no new commits —
    # nothing left to merge, so "pushed" (not "pushed → mergeable").
    pushed_even = tmp_path / "even"
    _git(hub, "worktree", "add", "-q", "-b", "feature/3-even", str(pushed_even))
    _git(pushed_even, "push", "-q", "-u", "origin", "feature/3-even")

    # Ad-hoc spoke: non-numeric slug, so no issue to correlate with.
    adhoc = tmp_path / "adhoc"
    _git(hub, "worktree", "add", "-q", "-b", "chore/adhoc-slug", str(adhoc))
    return hub


def _run_hub_status_proc(
    hub: Path,
    tmp_path: Path,
    *,
    panes: str = "",
    inside_tmux: bool = False,
    current_session: str = "hub-sess",
    issue_state: str = "",
    tmux_fail: bool = False,
    projects_dir: Path | None = None,
    listening: str = "4317 4318",
    otel: str | None = None,
    batch_plan: str | None = None,
    hub_agents_dir: Path | None = None,
    afk_state_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run hub-status.sh from the hub with `gh`, `tmux` and `lsof` stubs on PATH.

    Args:
        hub: Main checkout to run the script from.
        tmp_path: Test tmpdir; the stub bin dir is created under it.
        panes: Multi-line ``session:window<TAB>path`` text the tmux stub
            prints for ``list-panes`` (empty → no panes).
        inside_tmux: When True, set a fake TMUX env var; otherwise TMUX is
            popped so the run is hermetic.
        current_session: What the tmux stub prints for ``display-message``.
        issue_state: When non-empty, the gh stub answers ``issue view`` with
            this on stdout (exit 0); ``issue list`` keeps failing. When empty,
            every gh call exits 1 (degrades to "(none open)").
        tmux_fail: When True, every tmux invocation exits 1 (no server).
        projects_dir: Claude projects root exported as CLAUDE_PROJECTS_DIR.
            When None, a nonexistent dir under tmp_path is exported so the
            host's real ~/.claude/projects can never leak into a test.
        listening: Space-separated ports the ``lsof`` stub reports as LISTEN
            (#138 collector-down warning). Defaults to the collector pair being
            up so unrelated tests never trip the warning; the stub makes the
            probe hermetic both ways — a really-running host collector must
            never steer the matrix.
        otel: Value for AI_TOOLKIT_OTEL; None leaves it unset (the host's own
            opt-in/out is always popped first).

    Returns:
        The CompletedProcess with captured stdout/stderr.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    if issue_state:
        gh.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "issue" ] && [ "$2" = "view" ]; then\n'
            "  printf '%s\\n' \"$HUB_STATUS_TEST_ISSUE_STATE\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n"
        )
    else:
        gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    tmux = bindir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        '[ "${HUB_STATUS_TEST_TMUX_FAIL:-}" = "1" ] && exit 1\n'
        'case "$1" in\n'
        "  list-panes)\n"
        '    [ -n "${HUB_STATUS_TEST_PANES:-}" ] && printf \'%s\\n\' "$HUB_STATUS_TEST_PANES"\n'
        "    ;;\n"
        "  display-message)\n"
        "    printf '%s\\n' \"$HUB_STATUS_TEST_SESSION\"\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )
    tmux.chmod(0o755)
    lsof = bindir / "lsof"
    lsof.write_text(
        "#!/bin/sh\n"
        'p=""\n'
        'for a in "$@"; do case "$a" in -iTCP:*) p="${a#-iTCP:}";; esac; done\n'
        'case " ${HUB_STATUS_TEST_LISTENING:-} " in *" $p "*) exit 0;; esac\n'
        "exit 1\n"
    )
    lsof.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("TMUX", None)  # hermetic by default; faked only when inside_tmux
    # The host's base-branch override (#117) must never steer the script under test.
    env.pop("AI_TOOLKIT_BASE_BRANCH", None)
    # The host's OTel opt-in must never gate the collector-down warning (#138).
    env.pop("AI_TOOLKIT_OTEL", None)
    if otel is not None:
        env["AI_TOOLKIT_OTEL"] = otel
    env["HUB_STATUS_TEST_LISTENING"] = listening
    if inside_tmux:
        env["TMUX"] = "/tmp/tmux-test/default,1234,0"
    env["HUB_STATUS_TEST_PANES"] = panes
    env["HUB_STATUS_TEST_SESSION"] = current_session
    env["HUB_STATUS_TEST_ISSUE_STATE"] = issue_state
    env["HUB_STATUS_TEST_TMUX_FAIL"] = "1" if tmux_fail else ""
    # A batch-plan.sh override for the Scheduling section (#223): points hub-status at a
    # stub so the section is hermetic without a real gh graphql round-trip.
    if batch_plan is not None:
        env["BATCH_PLAN"] = batch_plan
    fallback_projects = tmp_path / "no-claude-projects"
    env["CLAUDE_PROJECTS_DIR"] = str(
        projects_dir if projects_dir is not None else fallback_projects
    )
    # Hub-agents journal dir (#245): default to a nonexistent dir so the host's
    # real .ai-toolkit/hub-agents can never leak into a test.
    fallback_agents = tmp_path / "no-hub-agents"
    env["AI_TOOLKIT_HUB_AGENTS_DIR"] = str(
        hub_agents_dir if hub_agents_dir is not None else fallback_agents
    )
    # AFK state dir (#277): default to a nonexistent dir so the host's real decision journal
    # can never leak into a test; the waived-gates section then reads a seeded journal here.
    fallback_afk = tmp_path / "no-afk-state"
    env["AFK_STATE_DIR"] = str(afk_state_dir if afk_state_dir is not None else fallback_afk)
    return subprocess.run(
        ["bash", str(HUB_STATUS)],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )


def _run_hub_status(
    hub: Path,
    tmp_path: Path,
    *,
    panes: str = "",
    inside_tmux: bool = False,
    current_session: str = "hub-sess",
    issue_state: str = "",
    tmux_fail: bool = False,
    projects_dir: Path | None = None,
    listening: str = "4317 4318",
    otel: str | None = None,
    batch_plan: str | None = None,
    hub_agents_dir: Path | None = None,
    afk_state_dir: Path | None = None,
) -> str:
    """Run hub-status.sh and return its stdout (see _run_hub_status_proc)."""
    return _run_hub_status_proc(
        hub,
        tmp_path,
        panes=panes,
        inside_tmux=inside_tmux,
        current_session=current_session,
        issue_state=issue_state,
        tmux_fail=tmux_fail,
        projects_dir=projects_dir,
        listening=listening,
        otel=otel,
        batch_plan=batch_plan,
        hub_agents_dir=hub_agents_dir,
        afk_state_dir=afk_state_dir,
    ).stdout


def _todo(content: str, status: str) -> dict[str, str]:
    return {"content": content, "status": status, "activeForm": content}


def _todowrite_line(todos: list[dict[str, str]]) -> str:
    """A transcript line for an assistant turn that calls TodoWrite."""
    block = {"type": "tool_use", "name": "TodoWrite", "input": {"todos": todos}}
    return json.dumps({"type": "assistant", "message": {"content": [block]}})


def _write_transcript(
    projects_dir: Path,
    worktree: Path,
    lines: list[str],
    *,
    name: str = "sess.jsonl",
    mtime: float | None = None,
) -> Path:
    """Write a session .jsonl under the projects dir slugged for the worktree.

    Args:
        projects_dir: Root that hub-status reads via CLAUDE_PROJECTS_DIR.
        worktree: Worktree path the transcript belongs to; resolved first
            (git reports realpaths, e.g. /private/var on macOS tmpdirs),
            then slugged by replacing every non-alphanumeric char with ``-``.
        lines: Raw transcript lines, written newline-joined.
        name: Transcript file name within the project dir.
        mtime: When set, the file's mtime (newest .jsonl wins selection).

    Returns:
        Path of the written transcript file.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(worktree.resolve()))
    project_dir = projects_dir / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    transcript = project_dir / name
    transcript.write_text("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(transcript, (mtime, mtime))
    return transcript


def _taskcreate_lines(use_id: str, task_id: str, subject: str) -> list[str]:
    """Real-shape TaskCreate pair: assistant tool_use + user tool_result.

    Mirrors claude 2.1.175 transcripts: the tool_use input carries NO task id
    (only subject/description/activeForm); the id arrives in the tool_result
    line's top-level ``toolUseResult.task``.
    """
    use = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": use_id,
                    "name": "TaskCreate",
                    "input": {
                        "subject": subject,
                        "description": subject,
                        "activeForm": subject,
                    },
                }
            ]
        },
    }
    result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": use_id,
                    "type": "tool_result",
                    "content": f"Task #{task_id} created successfully: {subject}",
                }
            ],
        },
        "toolUseResult": {"task": {"id": task_id, "subject": subject}},
    }
    return [json.dumps(use), json.dumps(result)]


def _taskupdate_lines(use_id: str, task_id: str, from_status: str, to_status: str) -> list[str]:
    """Real-shape TaskUpdate pair: the status transition is authoritative in
    the tool_result's ``toolUseResult.statusChange``, not the input."""
    use = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": use_id,
                    "name": "TaskUpdate",
                    "input": {"taskId": task_id, "status": to_status},
                }
            ]
        },
    }
    result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": use_id,
                    "type": "tool_result",
                    "content": f"Updated task #{task_id} status",
                }
            ],
        },
        "toolUseResult": {
            "success": True,
            "taskId": task_id,
            "updatedFields": ["status"],
            "statusChange": {"from": from_status, "to": to_status},
        },
    }
    return [json.dumps(use), json.dumps(result)]


def test_pushed_spoke_with_ready_marker_is_mergeable(hub_with_spokes: Path, tmp_path: Path) -> None:
    _git(hub_with_spokes, "tag", "ready/1", "feature/1-pushed")
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "pushed → mergeable" in line


def test_unpushed_spoke_is_unpushed(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if "feature/2-unpushed" in ln)
    assert "unpushed" in line


def test_pushed_even_spoke_is_pushed_not_mergeable(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if "feature/3-even" in ln)
    assert "pushed" in line and "mergeable" not in line


# --- Ready-to-land marker (issue #16) ----------------------------------------
# A per-subtask push is indistinguishable from task completion, so "pushed →
# mergeable" must require an explicit `ready/<issue>` tag at the branch tip.


def test_pushed_spoke_without_marker_shows_in_progress(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "pushed (in progress)" in line
    assert "mergeable" not in line


def test_pushed_spoke_with_stale_marker_shows_in_progress(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    # Marker sha != branch tip: the spoke pushed more work after tagging, so
    # the completion claim no longer covers the tip — treat as in progress.
    seed_sha = _git(hub_with_spokes, "rev-parse", "main").strip()
    _git(hub_with_spokes, "tag", "ready/1", seed_sha)

    out = _run_hub_status(hub_with_spokes, tmp_path)

    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "pushed (in progress)" in line
    assert "mergeable" not in line


def test_adhoc_pushed_spoke_is_mergeable_without_marker(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    # Non-numeric slug = express lane: there is no issue to anchor a marker to,
    # and the one push IS completion — exempt from the marker requirement.
    adhoc = tmp_path / "adhoc"
    (adhoc / "c.txt").write_text("c\n")
    _git(adhoc, "add", "c.txt")
    _git(adhoc, "commit", "-qm", "chore: c", "-m", "Refs #0")
    _git(adhoc, "push", "-q", "-u", "origin", "chore/adhoc-slug")

    out = _run_hub_status(hub_with_spokes, tmp_path)

    line = next(ln for ln in out.splitlines() if "chore/adhoc-slug" in ln)
    assert "pushed → mergeable" in line


def test_hub_branch_labelled_hub(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("main"))
    assert "(hub)" in line


# --- Cross-session correlation (issue #8) -----------------------------------


def test_spoke_pane_listed_across_sessions(hub_with_spokes: Path, tmp_path: Path) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes)

    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "tmux 0:3" in line


def test_worktree_row_shows_issue_number_and_state(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path, issue_state="OPEN")

    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "#1 OPEN" in line


def test_issue_state_degrades_to_question_mark_without_gh(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)

    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "#1 ?" in line


def test_jump_select_window_when_pane_in_current_session(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(
        hub_with_spokes, tmp_path, panes=panes, inside_tmux=True, current_session="0"
    )

    assert "tmux select-window -t '0:3'" in out


def test_jump_switch_client_when_pane_in_other_session(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(
        hub_with_spokes, tmp_path, panes=panes, inside_tmux=True, current_session="hub-sess"
    )

    assert "tmux switch-client -t '0:3'" in out


def test_jump_attach_when_outside_tmux(hub_with_spokes: Path, tmp_path: Path) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes)

    assert "tmux attach -t 0 \\; select-window -t '0:3'" in out


def test_worktree_without_pane_shows_no_pane(hub_with_spokes: Path, tmp_path: Path) -> None:
    panes = f"1:1\t{tmp_path / 'somewhere-unrelated'}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes)

    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "no pane" in line
    assert "select-window" not in out
    assert "switch-client" not in out


def test_non_numeric_slug_has_no_issue_column(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path, issue_state="OPEN")

    line = next(ln for ln in out.splitlines() if "chore/adhoc-slug" in ln)
    assert "#" not in line


# --- TodoWrite ledger column (issue #8; kept as the older-runtime fallback) ---


def test_todos_subline_shows_done_count_and_in_progress_item(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    lines = [
        _todowrite_line(
            [
                _todo("RED the thing", "pending"),
                _todo("GREEN the thing", "pending"),
                _todo("REFACTOR the thing", "pending"),
            ]
        ),
        "not json",
        _todowrite_line(
            [
                _todo("RED the thing", "completed"),
                _todo("GREEN the thing", "in_progress"),
                _todo("REFACTOR the thing", "pending"),
            ]
        ),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "↳ todos: 1/3 · step: GREEN" in out


def test_todos_newest_jsonl_wins(hub_with_spokes: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    old_todos = [
        _todo("one", "completed"),
        _todo("two", "completed"),
        _todo("three", "completed"),
    ]
    new_todos = [_todo("alpha", "pending"), _todo("beta", "pending")]
    _write_transcript(
        projects,
        tmp_path / "pushed",
        [_todowrite_line(old_todos)],
        name="old.jsonl",
        mtime=1_000_000.0,
    )
    _write_transcript(
        projects,
        tmp_path / "pushed",
        [_todowrite_line(new_todos)],
        name="new.jsonl",
        mtime=2_000_000.0,
    )

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "todos: 0/2" in out
    assert "3/3" not in out


def test_todos_without_in_progress_omits_suffix(hub_with_spokes: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    todos = [_todo("done thing", "completed"), _todo("next thing", "pending")]
    _write_transcript(projects, tmp_path / "pushed", [_todowrite_line(todos)])

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    line = next((ln for ln in out.splitlines() if "todos:" in ln), "")
    assert "↳ todos: 1/2" in line
    assert "step:" not in line


def test_todos_none_marker_when_transcript_has_no_todowrite(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        json.dumps({"type": "user", "message": {"content": "do the thing"}}),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "↳ todos: none" in out


def test_todos_survive_malformed_lines_and_entries(hub_with_spokes: Path, tmp_path: Path) -> None:
    # A valid-JSON non-object line must not abort the scan (a later TodoWrite
    # still wins), non-dict ledger entries are ignored, and the in_progress
    # content is rendered single-line, truncated to 60 chars.
    projects = tmp_path / "projects"
    long_content = "x" * 70 + "\nsecond line"
    lines = [
        _todowrite_line([_todo("stale", "completed")]),
        json.dumps([1, 2, 3]),
        _todowrite_line(["stray-string", _todo(long_content, "in_progress")]),  # type: ignore[list-item]
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert f"↳ todos: 0/1 · step: {'x' * 60}" in out
    assert "second line" not in out
    assert "todos: 1/1" not in out


def test_todos_subline_omitted_without_project_dir(hub_with_spokes: Path, tmp_path: Path) -> None:
    # Positive control: the unpushed spoke HAS a transcript so its row gets a
    # todos sub-line, proving the column is active — while the pushed spoke
    # (no project dir under the root) must get no todos sub-line at all.
    projects = tmp_path / "projects"
    control = [_todowrite_line([_todo("only step", "completed")])]
    _write_transcript(projects, tmp_path / "unpushed", control)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    lines = out.splitlines()
    unpushed_idx = next(i for i, ln in enumerate(lines) if "feature/2-unpushed" in ln)
    assert "↳ todos: 1/1" in lines[unpushed_idx + 1]
    assert out.count("todos:") == 1


# --- Tasks-system ledger (issue #12) ------------------------------------------
# Current runtimes (claude ≥2.1.175 with tasks enabled) keep the session ledger
# via TaskCreate/TaskUpdate, not TodoWrite. The dashboard must reconstruct
# done/total + in_progress from those records, keeping TodoWrite as fallback.


def test_tasks_ledger_shows_done_count_and_in_progress_subject(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    lines = [
        *_taskcreate_lines("toolu_01", "1", "ANCHOR the work"),
        *_taskcreate_lines("toolu_02", "2", "RED the thing"),
        *_taskcreate_lines("toolu_03", "3", "GREEN the thing"),
        *_taskupdate_lines("toolu_04", "1", "pending", "in_progress"),
        *_taskupdate_lines("toolu_05", "1", "in_progress", "completed"),
        *_taskupdate_lines("toolu_06", "2", "pending", "in_progress"),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "↳ todos: 1/3 · step: RED" in out


def test_tasks_ledger_preferred_over_todowrite(hub_with_spokes: Path, tmp_path: Path) -> None:
    # A transcript carrying BOTH systems (e.g. a TodoWrite from an earlier
    # runtime plus a live Tasks ledger) must render the Tasks counts.
    projects = tmp_path / "projects"
    lines = [
        _todowrite_line([_todo("old one", "completed"), _todo("old two", "completed")]),
        *_taskcreate_lines("toolu_01", "1", "fresh task"),
        *_taskcreate_lines("toolu_02", "2", "other task"),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "↳ todos: 0/2" in out
    assert "2/2" not in out


def test_taskcreate_without_tool_result_not_counted(hub_with_spokes: Path, tmp_path: Path) -> None:
    # The id only exists in the tool_result; a dangling tool_use (interrupted
    # turn) has no id to track and must not inflate the total.
    projects = tmp_path / "projects"
    dangling = json.loads(_taskcreate_lines("toolu_99", "9", "never confirmed")[0])
    lines = [
        *_taskcreate_lines("toolu_01", "1", "real task"),
        *_taskcreate_lines("toolu_02", "2", "second task"),
        json.dumps(dangling),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "↳ todos: 0/2" in out
    assert "0/3" not in out


def test_tasks_deleted_status_removes_entry(hub_with_spokes: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    lines = [
        *_taskcreate_lines("toolu_01", "1", "kept task"),
        *_taskcreate_lines("toolu_02", "2", "doomed task"),
        *_taskupdate_lines("toolu_03", "2", "pending", "deleted"),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "↳ todos: 0/1" in out
    assert "doomed task" not in out


# --- Step + attention line (issue #12 scope addition) -------------------------
# Per worktree row the todos sub-line also surfaces: the cycle step from the
# in_progress item (keyword or truncated text), the transcript's activity age,
# and a waiting-on-input flag for an open AskUserQuestion or a trailing
# notification event.


def _ask_user_question_line(use_id: str) -> str:
    """An assistant turn posing an AskUserQuestion (real tool_use shape)."""
    block = {
        "type": "tool_use",
        "id": use_id,
        "name": "AskUserQuestion",
        "input": {
            "questions": [
                {"question": "Push now?", "header": "Push", "options": [], "multiSelect": False}
            ]
        },
    }
    return json.dumps({"type": "assistant", "message": {"content": [block]}})


def _tool_result_line(use_id: str, content: str = "answered") -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"tool_use_id": use_id, "type": "tool_result", "content": content}],
            },
        }
    )


def test_step_keyword_extracted_from_in_progress_item(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    lines = [
        *_taskcreate_lines("toolu_01", "1", "Subtask 1 · review — approve the diff"),
        *_taskupdate_lines("toolu_02", "1", "pending", "in_progress"),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "↳ todos: 0/1 · step: REVIEW" in out


def test_step_falls_back_to_truncated_text_without_keyword(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    # No cycle keyword → the item text as-is; "REDESIGN" must not match RED
    # (word-boundary, not substring).
    projects = tmp_path / "projects"
    lines = [
        *_taskcreate_lines("toolu_01", "1", "REDESIGN api layer"),
        *_taskupdate_lines("toolu_02", "1", "pending", "in_progress"),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "↳ todos: 0/1 · step: REDESIGN api layer" in out


def test_activity_age_active_seconds(hub_with_spokes: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        tmp_path / "pushed",
        [_todowrite_line([_todo("one", "completed")])],
        mtime=time.time() - 5,
    )

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert re.search(r"↳ todos: 1/1 · active \d+s ago", out)


def test_activity_age_idle_minutes(hub_with_spokes: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        tmp_path / "pushed",
        [_todowrite_line([_todo("one", "completed")])],
        mtime=time.time() - 9 * 60 - 5,
    )

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "↳ todos: 1/1 · idle 9m" in out


def test_waiting_flag_on_open_ask_user_question(hub_with_spokes: Path, tmp_path: Path) -> None:
    # An AskUserQuestion tool_use with no matching tool_result is an open
    # question — the spoke is blocked on the user.
    projects = tmp_path / "projects"
    lines = [
        *_taskcreate_lines("toolu_01", "1", "Subtask 1 · PUSH — ship it"),
        *_taskupdate_lines("toolu_02", "1", "pending", "in_progress"),
        _ask_user_question_line("toolu_ask"),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "⚠ WAITING ON INPUT" in out


def test_no_waiting_flag_when_question_answered(hub_with_spokes: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    lines = [
        _ask_user_question_line("toolu_ask"),
        _tool_result_line("toolu_ask"),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "WAITING ON INPUT" not in out


def test_no_waiting_flag_when_session_moved_past_open_question(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    # The question was never answered but a later meaningful event exists
    # (e.g. the user queued a new prompt) — the spoke is not blocked.
    projects = tmp_path / "projects"
    lines = [
        _ask_user_question_line("toolu_ask"),
        json.dumps({"type": "user", "message": {"role": "user", "content": "do this instead"}}),
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "on it"}]}}
        ),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "WAITING ON INPUT" not in out


def test_waiting_flag_on_trailing_notification_event(hub_with_spokes: Path, tmp_path: Path) -> None:
    # A notification entry as the newest transcript event flags waiting even
    # with no ledger of either kind.
    projects = tmp_path / "projects"
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        json.dumps({"type": "notification", "content": "Claude is waiting for your input"}),
    ]
    _write_transcript(projects, tmp_path / "pushed", lines)

    out = _run_hub_status(hub_with_spokes, tmp_path, projects_dir=projects)

    assert "↳ todos: none" in out
    assert "⚠ WAITING ON INPUT" in out


def test_degrades_when_tmux_unavailable(hub_with_spokes: Path, tmp_path: Path) -> None:
    # TMUX is set but every tmux call fails (stale env, dead server): the
    # script must still exit 0 and print the branch-state rows.
    result = _run_hub_status_proc(hub_with_spokes, tmp_path, inside_tmux=True, tmux_fail=True)

    line = next(ln for ln in result.stdout.splitlines() if "feature/1-pushed" in ln)
    assert result.returncode == 0
    assert "pushed (in progress)" in line


# --- configurable base branch (issue #117) --------------------------------------


def _add_develop_base(hub: Path) -> None:
    """Configure `develop` (one commit ahead of main) as the integration base.

    Leaves the hub checked out back on main, so a dashboard still keyed to
    literal main would report different ahead/behind counts.
    """
    _git(hub, "checkout", "-q", "-b", "develop")
    (hub / "develop.txt").write_text("develop\n")
    _git(hub, "add", "develop.txt")
    _git(hub, "commit", "-qm", "feat: develop seed", "-m", "Refs #0")
    _git(hub, "checkout", "-q", "main")
    _git(hub, "config", "ai-toolkit.base-branch", "develop")


def test_status_measures_against_configured_base(hub_with_spokes: Path, tmp_path: Path) -> None:
    # feature/1-pushed is 1 ahead of main; with develop (main+1) configured as
    # the base it reads ↑1 ↓1 — the counts must be measured vs the RESOLVED
    # base, not literal main.
    _add_develop_base(hub_with_spokes)

    out = _run_hub_status(hub_with_spokes, tmp_path)

    row = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "↑1" in row
    assert "↓1" in row


# --- Collector-down warning (#138) -------------------------------------------
# A pull survey must also surface a dark capture stack: when ≥1 spoke pane is
# live and the collector ports (4317 gRPC / 4318 OTLP-HTTP) are not listening,
# every span those spokes emit is dropped at source — hub-status must scream.
# Silent when no spoke is live, when the collector is up, or on an explicit
# operator opt-out (AI_TOOLKIT_OTEL=0; unset still warns — spokes are
# default-on).


def test_collector_warning_when_spoke_live_and_ports_down(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes, listening="")

    assert "COLLECTOR DOWN" in out
    assert "4317" in out
    assert "4318" in out
    assert "hub-otel-watch" in out  # the re-arm command
    # Prominent: the warning leads the dashboard, before the Worktrees survey.
    assert out.index("COLLECTOR DOWN") < out.index("Worktrees")


def test_collector_warning_silent_when_collector_up(hub_with_spokes: Path, tmp_path: Path) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes, listening="4317 4318")

    assert "COLLECTOR DOWN" not in out


def test_collector_warning_silent_when_no_spoke_pane(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path, panes="", listening="")

    assert "COLLECTOR DOWN" not in out


def test_collector_warning_silent_when_only_hub_pane(hub_with_spokes: Path, tmp_path: Path) -> None:
    # A pane sitting in the hub main checkout is not a live spoke.
    panes = f"0:1\t{hub_with_spokes}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes, listening="")

    assert "COLLECTOR DOWN" not in out


def test_collector_warning_fires_when_one_port_dark(hub_with_spokes: Path, tmp_path: Path) -> None:
    # Both ports are load-bearing (gRPC traces + OTLP-HTTP cycle spans): 4317
    # up with 4318 dark must still warn, naming the dark port.
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes, listening="4317")

    assert "COLLECTOR DOWN" in out
    assert "4318" in out


def test_collector_warning_respects_explicit_optout(hub_with_spokes: Path, tmp_path: Path) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes, listening="", otel="0")

    assert "COLLECTOR DOWN" not in out


# ── Global on/off switch banner (issue #154) ────────────────────────


def _disable_toolkit(hub: Path) -> None:
    """Drop the off marker at the hub's git-common-dir (disables the toolkit)."""
    common = Path(_git(hub, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = (hub / common).resolve()
    (common / "ai-toolkit-off").write_text("")


def test_off_banner_shown_when_disabled(hub_with_spokes: Path, tmp_path: Path) -> None:
    """When the toolkit is disabled, hub-status leads with a loud AI-TOOLKIT OFF
    banner so a forgotten `off` can't silently ship gutted code to main (#154)."""
    _disable_toolkit(hub_with_spokes)

    out = _run_hub_status(hub_with_spokes, tmp_path)

    assert "AI-TOOLKIT OFF" in out
    # It must LEAD — before the worktree survey (mirrors the collector-down test).
    assert out.index("AI-TOOLKIT OFF") < out.index("Worktrees")


def test_no_off_banner_when_enabled(hub_with_spokes: Path, tmp_path: Path) -> None:
    """Nothing about the switch is shown when the toolkit is enabled (the default)."""
    out = _run_hub_status(hub_with_spokes, tmp_path)

    assert "AI-TOOLKIT OFF" not in out


# ── Scheduling section (issue #223) ───────────────────────────────────────────


def _batch_plan_stub(tmp_path: Path, *, stdout: str = "", exit_code: int = 0) -> str:
    """A batch-plan.sh stub that echoes fixed `--explain` output.

    hub-status invokes `bash <BATCH_PLAN> --explain`; the stub just prints the
    canned scheduling view so the section is hermetic (no gh graphql round-trip).
    """
    stub = tmp_path / "batch-plan-stub.sh"
    body = "#!/usr/bin/env bash\n"
    if stdout:
        # printf the literal fixture verbatim.
        body += "cat <<'SCHED'\n" + stdout + "\nSCHED\n"
    body += f"exit {exit_code}\n"
    stub.write_text(body)
    stub.chmod(0o755)
    return str(stub)


def test_scheduling_section_header_present(hub_with_spokes: Path, tmp_path: Path) -> None:
    # Even with an empty schedule the section header is shown, so the hub survey
    # always has a scheduling pane (#223).
    stub = _batch_plan_stub(tmp_path, stdout="")

    out = _run_hub_status(hub_with_spokes, tmp_path, batch_plan=stub)

    assert "Scheduling" in out


def test_scheduling_section_renders_dispositions(hub_with_spokes: Path, tmp_path: Path) -> None:
    fixture = (
        "#189   in-flight              exclusive (Scope: *) — holds back #222, #214\n"
        "#222   blocked-by-scope:#189  (hub-afk.sh)"
    )
    stub = _batch_plan_stub(tmp_path, stdout=fixture)

    out = _run_hub_status(hub_with_spokes, tmp_path, batch_plan=stub)

    assert "Scheduling" in out
    assert "#189" in out and "exclusive (Scope: *)" in out
    assert "blocked-by-scope:#189" in out
    # The section is rendered AFTER the worktree survey.
    assert out.index("Scheduling") > out.index("Worktrees")


def test_scheduling_section_empty_when_nothing_dispatchable(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    stub = _batch_plan_stub(tmp_path, stdout="")

    out = _run_hub_status(hub_with_spokes, tmp_path, batch_plan=stub)

    # An empty schedule still reports the section, with a clear "nothing" marker.
    scheduling = out[out.index("Scheduling") :]
    assert re.search(r"nothing dispatchable|none", scheduling, re.I)


# ── Hub agents section (issue #245) ───────────────────────────────────────────
# Hub-side agent work (pre-land reviews, bug-scopers) runs via hub-agent.sh, which
# journals start/end records. hub-status surfaces the LIVE ones (a label whose
# latest event is `start`, no matching `end`) in a "Hub agents" section, so the
# operator can see and jump to running hub agents just like spoke panes. The
# render is strictly best-effort/read-only: a missing or malformed journal must
# never break the survey.


def _write_hub_agents_journal(agents_dir: Path, records: list[dict[str, object]]) -> None:
    """Write journal.jsonl under the hub-agents dir from a list of records."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records]
    (agents_dir / "journal.jsonl").write_text("\n".join(lines) + "\n")


def _start_record(
    label: str,
    purpose: str,
    *,
    age_s: float = 5.0,
    run_id: str | None = None,
    pid: int | None = None,
) -> dict[str, object]:
    # pid defaults to the live test process so the liveness check keeps the row;
    # a dead pid is passed explicitly by the retire-dead-agent test.
    return {
        "event": "start",
        "label": label,
        "purpose": purpose,
        "pid": pid if pid is not None else os.getpid(),
        "run_id": run_id or f"hub-{label}-1",
        "ts_epoch": int(time.time() - age_s),
        "log": f"/tmp/hub-agents/{label}.log",
    }


def _end_record(
    label: str, status: str = "success", *, run_id: str | None = None
) -> dict[str, object]:
    return {
        "event": "end",
        "label": label,
        "run_id": run_id or f"hub-{label}-1",
        "status": status,
        "ts_epoch": int(time.time()),
    }


def _dead_pid() -> int:
    """A pid that named a real process which has since exited — a crashed worker
    that never wrote its end record. Spawn a trivial process and reap it."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_hub_agents_section_lists_live_run(hub_with_spokes: Path, tmp_path: Path) -> None:
    agents = tmp_path / "hub-agents"
    _write_hub_agents_journal(agents, [_start_record("review-236", "pre-land review #236")])

    out = _run_hub_status(hub_with_spokes, tmp_path, hub_agents_dir=agents)

    assert "Hub agents" in out
    section = out[out.index("Hub agents") :]
    assert "hub:review-236" in section
    assert "pre-land review #236" in section


def test_hub_agents_section_omits_completed_run(hub_with_spokes: Path, tmp_path: Path) -> None:
    # A run with a matching `end` is done — not shown; with nothing else live the
    # section reports its empty marker.
    agents = tmp_path / "hub-agents"
    _write_hub_agents_journal(
        agents,
        [_start_record("review-236", "pre-land review #236"), _end_record("review-236")],
    )

    out = _run_hub_status(hub_with_spokes, tmp_path, hub_agents_dir=agents)

    section = out[out.index("Hub agents") :]
    assert "review-236" not in section
    assert re.search(r"none", section, re.I)


def test_hub_agents_section_empty_marker_without_journal(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    # No journal at all: the section header still renders with a "none" marker.
    out = _run_hub_status(hub_with_spokes, tmp_path, hub_agents_dir=tmp_path / "absent")

    assert "Hub agents" in out
    section = out[out.index("Hub agents") :]
    assert re.search(r"none", section, re.I)


def test_hub_agents_section_after_worktrees(hub_with_spokes: Path, tmp_path: Path) -> None:
    agents = tmp_path / "hub-agents"
    _write_hub_agents_journal(agents, [_start_record("scope-240", "bug-scoper #240")])

    out = _run_hub_status(hub_with_spokes, tmp_path, hub_agents_dir=agents)

    assert out.index("Hub agents") > out.index("Worktrees")


def test_hub_agents_section_survives_malformed_journal(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    # A garbage line and a record missing keys must not abort the render nor the
    # survey: the one valid live entry still shows, and the script exits 0.
    agents = tmp_path / "hub-agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "journal.jsonl").write_text(
        "not json at all\n"
        + json.dumps({"event": "start"})  # no label
        + "\n"
        + json.dumps(_start_record("review-241", "delta re-review #241"))
        + "\n"
    )

    result = _run_hub_status_proc(hub_with_spokes, tmp_path, hub_agents_dir=agents)

    assert result.returncode == 0
    assert "hub:review-241" in result.stdout


def test_hub_agents_section_retires_dead_pid(hub_with_spokes: Path, tmp_path: Path) -> None:
    # A start with no end but a dead pid (worker crashed / window killed) must not
    # linger as running.
    agents = tmp_path / "hub-agents"
    _write_hub_agents_journal(
        agents, [_start_record("review-236", "pre-land review #236", pid=_dead_pid())]
    )

    out = _run_hub_status(hub_with_spokes, tmp_path, hub_agents_dir=agents)

    section = out[out.index("Hub agents") :]
    assert "review-236" not in section
    assert re.search(r"none", section, re.I)


def test_hub_agents_section_keeps_live_sibling_when_same_label_ends(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    # Two runs share a label; the first ends. Keyed on run_id, the second (still
    # live) must remain shown — an end must retire only its own run.
    agents = tmp_path / "hub-agents"
    _write_hub_agents_journal(
        agents,
        [
            _start_record("review-236", "first pass", run_id="hub-review-236-a"),
            _start_record("review-236", "second pass", run_id="hub-review-236-b"),
            _end_record("review-236", run_id="hub-review-236-a"),
        ],
    )

    out = _run_hub_status(hub_with_spokes, tmp_path, hub_agents_dir=agents)

    section = out[out.index("Hub agents") :]
    assert "hub:review-236" in section
    assert "second pass" in section


# ── Waived gates section (issue #277) ─────────────────────────────────────────
# Every PLAN gate the /afk fast-path WAIVED (auto-approved without the reasoner) is a
# park:gate line in the decision journal. The hub survey surfaces those so an operator sees
# "this gate was fast-pathed, and why" at a glance — the gh comment alone is not a hub view.


def _write_decision_journal(state_dir: Path, records: list[dict]) -> None:
    """Seed <state_dir>/decision-journal.jsonl with one JSON record per line."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "decision-journal.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )


def test_waived_gates_section_renders_fast_path_waives(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    state = tmp_path / "afk-state"
    _write_decision_journal(
        state,
        [
            {
                "ts": 1000,
                "issue": "277",
                "park": "gate",
                "decision": "fast-path auto-approved: plan restates issue body (coverage 0.92)",
                "reversibility": "reversible",
                "reasoning_ref": "",
            },
            # A non-gate answer must NOT appear under waived gates.
            {"ts": 1001, "issue": "300", "park": "answer", "decision": "injected answer"},
        ],
    )

    out = _run_hub_status(hub_with_spokes, tmp_path, afk_state_dir=state)

    assert "Waived gates" in out
    section = out[out.index("Waived gates") :]
    assert "#277" in section
    assert "coverage 0.92" in section
    assert "#300" not in section, "a non-gate answer must not surface as a waived gate"


def test_waived_gates_section_graceful_without_journal(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    # No decision journal (state dir absent): the section still renders with a clear marker.
    out = _run_hub_status(hub_with_spokes, tmp_path, afk_state_dir=tmp_path / "nonexistent-afk")

    assert "Waived gates" in out
    section = out[out.index("Waived gates") :]
    assert re.search(r"none|no decision journal", section, re.I)
