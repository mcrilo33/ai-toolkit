"""cycle-step-mark hook: derive solo-cycle step markers from mechanical witnesses (#178).

Cycle-step state was LLM-narrated — the RED/GREEN step containers had no explicit
``step:*`` marker at all, and ``step:review``/``step:push`` only fired from their
specific gates (the review-stamp MCP call, ``spoke-push.sh``), which miss the common
spoke paths (an Agent-tool code-review that never touches the MCP; a mid-cycle or
quick-lane ``git push``). This hook closes the gap: on every ``PostToolUse`` it reads
the mechanical witness of the transition and emits the marker through the idempotent
``telemetry_mark_cycle_step`` helper.

Witness -> phase:
    ``git commit`` whose HEAD message carries ``Tested-RED``  -> red
    ``git commit`` whose HEAD message does not                -> green
    a ``git push`` that advanced ``@{upstream}`` to HEAD       -> push
    a Write of a ``.review/<hash>.json`` artifact             -> review

Because every emission is keyed on ``(phase, HEAD sha)``, this hook dedupes with the
#139 emitters that mark the same phases from their own paths — belt-and-suspenders,
never a double span. A tool call that is not a witness (``ls``, a source-file Write)
emits nothing, and a telemetry-off run is a silent no-op.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = ROOT / "shared" / "hooks"
HOOK = HOOKS_DIR / "cycle-step-mark.sh"


def _env(telemetry_dir: Path, *, enabled: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    for var in (
        "AI_TOOLKIT_TELEMETRY",
        "AI_TOOLKIT_TELEMETRY_DIR",
        "CURSOR_PROJECT_DIR",
        "AI_TOOLKIT_PARENT_SPAN",
        "TELEMETRY_PARENT_ID",
        "AI_TOOLKIT_OTEL_SPAN_ENDPOINT",
    ):
        env.pop(var, None)
    if enabled:
        env["AI_TOOLKIT_TELEMETRY"] = "1"
    env["AI_TOOLKIT_TELEMETRY_DIR"] = str(telemetry_dir)
    env["AI_TOOLKIT_WORKFLOW_REV"] = "testrev0"
    # WT_SPOKE marks the spoke role — cycle steps only mint in a spoke worktree.
    env["WT_SPOKE"] = "feature/178-derive"
    # Pin git to nothing so the host's global/system config never reaches the fixture.
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    return env


def _bash_payload(command: str) -> str:
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )


def _write_payload(file_path: str) -> str:
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": file_path},
        }
    )


def _run(payload: str, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
    )


def _events(telemetry_dir: Path) -> list[dict]:
    f = telemetry_dir / "events.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()]


def _steps(telemetry_dir: Path) -> list[dict]:
    return [e for e in _events(telemetry_dir) if e.get("kind") == "step"]


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    return tmp_path / "telemetry"


@pytest.fixture()
def repo(tmp_path: Path, telemetry_dir: Path) -> Path:
    """A feature-branch checkout with a local bare ``origin`` (no network)."""
    env = _env(telemetry_dir)
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=env
    )
    repo = tmp_path / "spoke"

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, env=env)

    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=env
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git("config", k, v)
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md")
    _git("commit", "-qm", "chore: seed")
    _git("remote", "add", "origin", str(remote))
    _git("checkout", "-q", "-b", "feature/178-derive")
    return repo


def _git(repo: Path, env: dict, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, env=env)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, env: dict, subject: str, *extra: str) -> None:
    (repo / "work.txt").write_text(subject + "\n")
    _git(repo, env, "add", "work.txt")
    _git(repo, env, "commit", "-qm", subject, *[a for e in extra for a in ("-m", e)])


class TestFileExists:
    def test_hook_is_executable(self) -> None:
        assert HOOK.exists(), "cycle-step-mark.sh not found"
        assert os.access(HOOK, os.X_OK), "cycle-step-mark.sh must be executable"


class TestCommitWitness:
    def test_commit_with_tested_red_emits_step_red(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        _commit(repo, env, "test: add red pin", "Tested-RED: tests/x.py::test_y")

        result = _run(_bash_payload("git commit -m 'test: add red pin'"), env, repo)

        assert result.returncode == 0, result.stderr
        steps = _steps(telemetry_dir)
        assert len(steps) == 1
        assert steps[0]["phase"] == "red"
        assert steps[0]["name"] == "solo-cycle"

    def test_commit_without_tested_red_emits_step_green(
        self, repo: Path, telemetry_dir: Path
    ) -> None:
        env = _env(telemetry_dir)
        _commit(repo, env, "feat: implement thing")

        result = _run(_bash_payload("git commit -m 'feat: implement thing'"), env, repo)

        assert result.returncode == 0, result.stderr
        steps = _steps(telemetry_dir)
        assert len(steps) == 1
        assert steps[0]["phase"] == "green"

    def test_marker_keyed_on_committed_head(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        _commit(repo, env, "feat: thing")

        _run(_bash_payload("git commit -m 'feat: thing'"), env, repo)

        sentinel = repo / ".ai-toolkit" / "cycle-step-green"
        assert sentinel.read_text().strip() == _head(repo)

    def test_same_commit_reemit_is_deduped(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        _commit(repo, env, "feat: thing")

        _run(_bash_payload("git commit -m 'feat: thing'"), env, repo)
        _run(_bash_payload("git commit -m 'feat: thing'"), env, repo)

        assert len(_steps(telemetry_dir)) == 1

    def test_red_then_green_are_separate_markers(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        _commit(repo, env, "test: red", "Tested-RED: tests/x.py::test_y")
        _run(_bash_payload("git commit -m 'test: red'"), env, repo)
        _commit(repo, env, "feat: green")
        _run(_bash_payload("git commit -m 'feat: green'"), env, repo)

        assert sorted(e["phase"] for e in _steps(telemetry_dir)) == ["green", "red"]


class TestPushWitness:
    def test_push_advancing_upstream_emits_step_push(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        _commit(repo, env, "feat: work")
        _git(repo, env, "push", "-q", "-u", "origin", "feature/178-derive")

        result = _run(_bash_payload("git push -u origin feature/178-derive"), env, repo)

        assert result.returncode == 0, result.stderr
        steps = _steps(telemetry_dir)
        assert len(steps) == 1
        assert steps[0]["phase"] == "push"

    def test_push_marker_keyed_on_pushed_head(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        _commit(repo, env, "feat: work")
        _git(repo, env, "push", "-q", "-u", "origin", "feature/178-derive")

        _run(_bash_payload("git push -u origin feature/178-derive"), env, repo)

        sentinel = repo / ".ai-toolkit" / "cycle-step-push"
        assert sentinel.read_text().strip() == _head(repo)

    def test_push_when_upstream_not_at_head_emits_nothing(
        self, repo: Path, telemetry_dir: Path
    ) -> None:
        # A `git push` command witnessed while the branch has NO upstream at HEAD
        # (nothing was actually pushed) must not fabricate a push marker.
        env = _env(telemetry_dir)
        _commit(repo, env, "feat: unpushed work")

        result = _run(_bash_payload("git push -u origin feature/178-derive"), env, repo)

        assert result.returncode == 0, result.stderr
        assert _steps(telemetry_dir) == []


class TestReviewWitness:
    def test_review_json_write_emits_step_review(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        (repo / ".review").mkdir()
        artifact = repo / ".review" / "abc123.json"
        artifact.write_text("{}")

        result = _run(_write_payload(str(artifact)), env, repo)

        assert result.returncode == 0, result.stderr
        steps = _steps(telemetry_dir)
        assert len(steps) == 1
        assert steps[0]["phase"] == "review"

    def test_review_accepts_relative_path(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)

        _run(_write_payload(".review/abc123.json"), env, repo)

        steps = _steps(telemetry_dir)
        assert len(steps) == 1
        assert steps[0]["phase"] == "review"

    def test_review_window_dotfile_is_not_a_witness(self, repo: Path, telemetry_dir: Path) -> None:
        # `.review/.window` is the review-window sentinel, not a review artifact.
        env = _env(telemetry_dir)

        _run(_write_payload(".review/.window"), env, repo)

        assert _steps(telemetry_dir) == []

    def test_non_review_json_write_is_not_a_witness(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)

        _run(_write_payload("src/config.json"), env, repo)

        assert _steps(telemetry_dir) == []


class TestWitnessPrecision:
    def test_merge_commit_is_not_a_green_step(self, repo: Path, telemetry_dir: Path) -> None:
        # A reconciliation merge (`git merge origin/main`) lands a 2-parent commit
        # with no Tested-RED trailer; it must NOT be mislabeled a GREEN cycle step.
        env = _env(telemetry_dir)
        _commit(repo, env, "feat: base")
        _git(repo, env, "checkout", "-q", "-b", "side")
        (repo / "side.txt").write_text("side\n")
        _git(repo, env, "add", "side.txt")
        _git(repo, env, "commit", "-qm", "feat: side")
        _git(repo, env, "checkout", "-q", "feature/178-derive")
        _git(repo, env, "merge", "-q", "--no-ff", "-m", "merge side", "side")

        _run(_bash_payload("git commit -m x"), env, repo)

        assert _steps(telemetry_dir) == []

    def test_prose_mentioning_tested_red_is_green_not_red(
        self, repo: Path, telemetry_dir: Path
    ) -> None:
        # A GREEN implementation commit whose body merely references the pin (no
        # `Tested-RED:` trailer) must classify green, not red.
        env = _env(telemetry_dir)
        (repo / "work.txt").write_text("impl\n")
        _git(repo, env, "add", "work.txt")
        _git(
            repo,
            env,
            "commit",
            "-qm",
            "feat: implement thing",
            "-m",
            "Implements the behavior asserted by the Tested-RED pin from the prior commit.",
        )

        _run(_bash_payload("git commit -m 'feat: implement thing'"), env, repo)

        steps = _steps(telemetry_dir)
        assert len(steps) == 1
        assert steps[0]["phase"] == "green"

    def test_compound_commit_and_push_emits_both(self, repo: Path, telemetry_dir: Path) -> None:
        # A single Bash call that both commits and pushes carries BOTH witnesses.
        env = _env(telemetry_dir)
        _commit(repo, env, "feat: work")
        _git(repo, env, "push", "-q", "-u", "origin", "feature/178-derive")

        _run(
            _bash_payload("git commit -m 'feat: work' && git push origin feature/178-derive"),
            env,
            repo,
        )

        assert sorted(e["phase"] for e in _steps(telemetry_dir)) == ["green", "push"]

    def test_gh_pr_create_is_not_a_push(self, repo: Path, telemetry_dir: Path) -> None:
        # `gh pr create` at a HEAD whose upstream already equals HEAD must NOT be
        # credited as a push (is_git_push excludes gh pr).
        env = _env(telemetry_dir)
        _commit(repo, env, "feat: work")
        _git(repo, env, "push", "-q", "-u", "origin", "feature/178-derive")

        _run(_bash_payload("gh pr create --fill"), env, repo)

        assert _steps(telemetry_dir) == []

    def test_nested_review_dir_is_not_a_witness(self, repo: Path, telemetry_dir: Path) -> None:
        # Only the worktree-root .review/ counts; a nested `.review/` elsewhere is a
        # normal project path, not a review artifact.
        env = _env(telemetry_dir)

        _run(_write_payload("packages/x/.review/schema.json"), env, repo)

        assert _steps(telemetry_dir) == []

    def test_unborn_branch_commit_emits_nothing(self, tmp_path: Path, telemetry_dir: Path) -> None:
        # A `git commit` witnessed before the first commit (unresolvable HEAD) must
        # emit no phantom green marker.
        env = _env(telemetry_dir)
        empty = tmp_path / "unborn"
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(empty)],
            check=True,
            capture_output=True,
            env=env,
        )

        result = _run(_bash_payload("git commit -m x"), env, empty)

        assert result.returncode == 0
        assert _events(telemetry_dir) == []


class TestNonWitnessCalls:
    def test_plain_bash_command_emits_nothing(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)

        _run(_bash_payload("ls -la"), env, repo)

        assert _steps(telemetry_dir) == []

    def test_git_add_is_not_a_commit(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)

        _run(_bash_payload("git add -A"), env, repo)

        assert _steps(telemetry_dir) == []

    def test_no_wt_spoke_is_a_noop(self, repo: Path, telemetry_dir: Path) -> None:
        # Outside a spoke (hub, /quick lane) WT_SPOKE is unset — no cycle markers.
        env = _env(telemetry_dir)
        env.pop("WT_SPOKE", None)
        _commit(repo, env, "feat: quick fix")

        _run(_bash_payload("git commit -m 'feat: quick fix'"), env, repo)

        assert _events(telemetry_dir) == []


class TestInvisibleAndGated:
    def test_hook_is_silent_and_exit_zero(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        _commit(repo, env, "feat: thing")

        result = _run(_bash_payload("git commit -m 'feat: thing'"), env, repo)

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""

    def test_no_hook_span_noise_on_every_call(self, repo: Path, telemetry_dir: Path) -> None:
        # Like parent-span-export, this hook fires on EVERY Bash/Write call, so it
        # must NOT arm the per-hook kind=hook span — that would be pure noise.
        env = _env(telemetry_dir)

        _run(_bash_payload("ls -la"), env, repo)

        assert _events(telemetry_dir) == []

    def test_telemetry_off_is_a_noop(self, repo: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir, enabled=False)
        _commit(repo, env, "feat: thing")

        result = _run(_bash_payload("git commit -m 'feat: thing'"), env, repo)

        assert result.returncode == 0
        assert _events(telemetry_dir) == []
        assert not (repo / ".ai-toolkit" / "cycle-step-green").exists()

    def test_survives_set_e_and_missing_jq_gracefully(
        self, repo: Path, telemetry_dir: Path
    ) -> None:
        # Malformed payload must not crash the hook or leak output.
        env = _env(telemetry_dir)

        result = _run("not-json", env, repo)

        assert result.returncode == 0
        assert result.stdout == ""
