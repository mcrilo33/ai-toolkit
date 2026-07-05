"""Cycle-step span contract: no per-commit step spans (#100), gate-time markers (#139).

Originally (Issue #21) each solo-cycle gate emitted a ``kind: step`` span tagged with
its phase, on top of the automatic ``kind: hook`` span. Issue #100 DROPS that emission:
cycle steps are now derived in the assembler from the todo ledger (``TaskCreate`` subject
+ ``TaskUpdate`` ``in_progress``/``completed`` windows), so the flat per-commit ``step:*``
markers are pure noise in the spoke-tree.

Issue #139 adds back exactly TWO gate-time markers — ``step:review`` from the
review-stamp gate and ``step:push`` from ``spoke-push.sh`` — because those two ledger
step containers otherwise stay near-empty on a healthy Claude Code run (nothing else
emits during their windows). The markers go through one idempotent helper,
``telemetry_mark_cycle_step``: it emits a single ``kind: step`` span per (phase, key)
and records the key in a sentinel file (``.ai-toolkit/cycle-step-<phase>``), so a gate
retry or a re-push never duplicates the span.

These tests pin both contracts: the per-commit gate hooks (#100 set) still emit their
single ``kind: hook`` span and never a ``kind: step`` span; ``telemetry_mark_step`` (the
#21 helper) no longer exists; and the #139 marker helper + its two gate entry points
fire once per step, idempotently.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = ROOT / "shared" / "hooks"
TELEMETRY_LIB = HOOKS_DIR / "lib" / "telemetry.sh"
GIT_PUSH_REVIEW = HOOKS_DIR / "git-push-review.sh"
REVIEW_WINDOW_OPEN = HOOKS_DIR / "review-window-open.sh"
COMMIT_GAUNTLET = HOOKS_DIR / "commit-gauntlet.sh"
RED_PROOF_VERIFY = HOOKS_DIR / "red-proof-verify.sh"
SPOKE_PUSH = ROOT / "scripts" / "spoke-push.sh"


def _env(telemetry_dir: Path, *, enabled: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    # AI_TOOLKIT_PARENT_SPAN / TELEMETRY_PARENT_ID outrank the payload-derived
    # parent and the push gate exports them — strip so a run under a live gate
    # behaves like a clean shell. The OTLP endpoint would add a second sink.
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
    return env


def _shell_payload(command: str, root: Path) -> str:
    return json.dumps(
        {
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "cwd": "",
            "workspace_roots": [str(root)],
        }
    )


def _run(script: Path, payload: str, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
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
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "sample-project"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    return tmp_path / "telemetry"


class TestStepEmissionRemoved:
    def test_telemetry_lib_no_longer_defines_mark_step(self) -> None:
        text = TELEMETRY_LIB.read_text()
        assert "telemetry_mark_step()" not in text  # the #21 helper stays gone

    def test_no_gate_hook_calls_mark_step(self) -> None:
        # The #100 set never regains a step emission — neither the old helper
        # nor the #139 marker helper belongs in the per-commit gate hooks.
        for hook in (GIT_PUSH_REVIEW, REVIEW_WINDOW_OPEN, COMMIT_GAUNTLET, RED_PROOF_VERIFY):
            text = hook.read_text()
            assert "telemetry_mark_step" not in text, hook.name
            assert "telemetry_mark_cycle_step" not in text, hook.name


class TestGatesEmitNoStepSpan:
    def test_push_gate_emits_hook_but_no_step(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _shell_payload("git push origin main", project_root)

        _run(GIT_PUSH_REVIEW, payload, _env(telemetry_dir), project_root)

        assert _steps(telemetry_dir) == []
        assert any(e.get("kind") == "hook" for e in _events(telemetry_dir))

    def test_review_gate_emits_no_step(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = json.dumps(
            {"subagent_type": "code-review", "workspace_roots": [str(project_root)]}
        )

        _run(REVIEW_WINDOW_OPEN, payload, _env(telemetry_dir), project_root)

        assert _steps(telemetry_dir) == []

    def test_green_gate_emits_no_step_on_plain_commit(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _shell_payload("git commit -m 'feat: add thing'", project_root)

        _run(COMMIT_GAUNTLET, payload, _env(telemetry_dir), project_root)

        assert _steps(telemetry_dir) == []

    def test_red_gate_emits_no_step(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _shell_payload(
            "git commit -m 'test: red' -m 'Tested-RED: tests/x.py::test_y'", project_root
        )

        _run(RED_PROOF_VERIFY, payload, _env(telemetry_dir), project_root)

        assert _steps(telemetry_dir) == []


# ── #139: the idempotent gate-time cycle-step marker ─────────────────────────


def _mark(phase: str, key: str, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    """Source the telemetry lib and invoke ``telemetry_mark_cycle_step``."""
    return subprocess.run(
        [
            "bash",
            "-c",
            f'. "{TELEMETRY_LIB}" && telemetry_mark_cycle_step "$1" "$2"',
            "bash",
            phase,
            key,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
    )


def _sentinel(project_root: Path, phase: str) -> Path:
    return project_root / ".ai-toolkit" / f"cycle-step-{phase}"


class TestCycleStepMarkerHelper:
    def test_helper_is_defined_in_lib(self) -> None:
        assert "telemetry_mark_cycle_step()" in TELEMETRY_LIB.read_text()

    def test_emits_step_span_with_phase(self, project_root: Path, telemetry_dir: Path) -> None:
        result = _mark("review", "key1", _env(telemetry_dir), project_root)

        assert result.returncode == 0, result.stderr
        steps = _steps(telemetry_dir)
        assert len(steps) == 1
        assert steps[0]["phase"] == "review"
        assert steps[0]["name"] == "solo-cycle"
        assert steps[0]["status"] == "success"

    def test_is_silent(self, project_root: Path, telemetry_dir: Path) -> None:
        result = _mark("review", "key1", _env(telemetry_dir), project_root)

        assert result.stdout == ""
        assert result.stderr == ""

    def test_same_key_reemit_is_skipped(self, project_root: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)

        _mark("push", "key1", env, project_root)
        _mark("push", "key1", env, project_root)

        assert len(_steps(telemetry_dir)) == 1

    def test_new_key_emits_again(self, project_root: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)

        _mark("push", "key1", env, project_root)
        _mark("push", "key2", env, project_root)

        assert len(_steps(telemetry_dir)) == 2

    def test_phases_track_separate_sentinels(self, project_root: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)

        _mark("review", "key1", env, project_root)
        _mark("push", "key1", env, project_root)

        assert sorted(e["phase"] for e in _steps(telemetry_dir)) == ["push", "review"]

    def test_disabled_run_emits_nothing_and_leaves_no_sentinel(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        # A telemetry-off run must not poison the sentinel: turning telemetry on
        # later must still emit the marker for the same key.
        _mark("push", "key1", _env(telemetry_dir, enabled=False), project_root)

        assert _events(telemetry_dir) == []
        assert not _sentinel(project_root, "push").exists()

        _mark("push", "key1", _env(telemetry_dir), project_root)

        assert len(_steps(telemetry_dir)) == 1

    def test_survives_set_e_caller(self, project_root: Path, telemetry_dir: Path) -> None:
        # The gates that call the helper run under `set -euo pipefail` — no
        # failure inside the helper may abort them. The skipped-reemit path is
        # the riskiest (early return from the redirected group), so exercise it
        # twice and require the caller to reach its final echo both times.
        script = (
            f'set -euo pipefail; . "{TELEMETRY_LIB}"; '
            "telemetry_mark_cycle_step push key1; "
            "telemetry_mark_cycle_step push key1; "
            "echo reached"
        )
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=_env(telemetry_dir),
            cwd=str(project_root),
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "reached\n"
        assert len(_steps(telemetry_dir)) == 1

    def test_default_key_is_head_sha(self, tmp_path: Path, telemetry_dir: Path) -> None:
        # No explicit key → the sentinel records the project root's HEAD sha,
        # so a re-run at the same tip is deduped and a new commit emits again.
        repo = tmp_path / "repo"
        repo.mkdir()
        git_env = {**_env(telemetry_dir), "GIT_CONFIG_GLOBAL": "/dev/null"}

        def _git(*args: str) -> None:
            subprocess.run(
                ["git", *args], cwd=str(repo), check=True, capture_output=True, env=git_env
            )

        _git("init", "-q")
        _git("config", "user.email", "t@t.t")
        _git("config", "user.name", "t")
        _git("config", "commit.gpgsign", "false")
        (repo / "f.txt").write_text("one\n")
        _git("add", "f.txt")
        _git("commit", "-qm", "one")

        _mark("push", "", git_env, repo)
        _mark("push", "", git_env, repo)

        assert len(_steps(telemetry_dir)) == 1
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()
        assert (repo / ".ai-toolkit" / "cycle-step-push").read_text().strip() == head

        (repo / "f.txt").write_text("two\n")
        _git("add", "f.txt")
        _git("commit", "-qm", "two")
        _mark("push", "", git_env, repo)

        assert len(_steps(telemetry_dir)) == 2


# ── #139 ST2: spoke-push.sh emits step:push once per pushed HEAD ─────────────
#
# Hermetic setup mirrors test_spoke_push.py: a local bare ``origin`` (no
# network) and a feature-branch checkout, git config pinned to nothing so a
# host's global/system config never reaches the fixture repo.


def _push_env(telemetry_dir: Path) -> dict[str, str]:
    return {
        **_env(telemetry_dir),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


@pytest.fixture()
def push_repo(tmp_path: Path, telemetry_dir: Path) -> Path:
    """A feature-branch checkout with a local bare ``origin``."""
    env = _push_env(telemetry_dir)
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
    _git("checkout", "-q", "-b", "feature/139-step-push")
    (repo / "work.txt").write_text("work\n")
    _git("add", "work.txt")
    _git("commit", "-qm", "feat: work")
    return repo


def _spoke_push(repo: Path, telemetry_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SPOKE_PUSH)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_push_env(telemetry_dir),
    )


def _commit_more(repo: Path, telemetry_dir: Path, content: str) -> None:
    env = _push_env(telemetry_dir)
    (repo / "work.txt").write_text(content)
    subprocess.run(
        ["git", "add", "work.txt"], cwd=str(repo), check=True, capture_output=True, env=env
    )
    subprocess.run(
        ["git", "commit", "-qm", "feat: more"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env=env,
    )


class TestSpokePushEmitsStepPush:
    def test_successful_push_emits_step_push_once(
        self, push_repo: Path, telemetry_dir: Path
    ) -> None:
        result = _spoke_push(push_repo, telemetry_dir)

        assert result.returncode == 0, result.stdout + result.stderr
        steps = _steps(telemetry_dir)
        assert len(steps) == 1
        assert steps[0]["phase"] == "push"
        assert steps[0]["name"] == "solo-cycle"

    def test_sentinel_records_pushed_head(self, push_repo: Path, telemetry_dir: Path) -> None:
        _spoke_push(push_repo, telemetry_dir)

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(push_repo),
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert _sentinel(push_repo, "push").read_text().strip() == head

    def test_repush_same_head_emits_no_duplicate(
        self, push_repo: Path, telemetry_dir: Path
    ) -> None:
        _spoke_push(push_repo, telemetry_dir)
        result = _spoke_push(push_repo, telemetry_dir)

        assert result.returncode == 0, result.stdout + result.stderr
        assert len(_steps(telemetry_dir)) == 1

    def test_new_commit_push_emits_again(self, push_repo: Path, telemetry_dir: Path) -> None:
        _spoke_push(push_repo, telemetry_dir)
        _commit_more(push_repo, telemetry_dir, "more\n")
        _spoke_push(push_repo, telemetry_dir)

        assert len(_steps(telemetry_dir)) == 2

    def test_rejected_push_emits_no_marker(
        self, push_repo: Path, tmp_path: Path, telemetry_dir: Path
    ) -> None:
        # A push that is ATTEMPTED and then rejected (non-fast-forward) must not
        # emit either: the marker sits below the bare wt_git_push under `set -e`,
        # and this pins that placement. Advance the remote branch from a second
        # checkout so the spoke's tip is behind.
        env = _push_env(telemetry_dir)
        other = tmp_path / "other"
        subprocess.run(
            ["git", "clone", "-q", str(push_repo), str(other)],
            check=True,
            capture_output=True,
            env=env,
        )
        for args in (
            ("config", "user.email", "t@t.t"),
            ("config", "user.name", "t"),
            ("config", "commit.gpgsign", "false"),
            ("remote", "set-url", "origin", str(tmp_path / "remote.git")),
            ("commit", "-qm", "feat: remote moved", "--allow-empty"),
            ("push", "-q", "origin", "feature/139-step-push"),
        ):
            subprocess.run(["git", *args], cwd=str(other), check=True, capture_output=True, env=env)

        result = _spoke_push(push_repo, telemetry_dir)

        assert result.returncode != 0
        assert _steps(telemetry_dir) == []
        assert not _sentinel(push_repo, "push").exists()

    def test_refused_push_emits_no_marker(self, push_repo: Path, telemetry_dir: Path) -> None:
        # On the default branch the script refuses before pushing — no PUSH
        # step happened, so no marker may be emitted.
        env = _push_env(telemetry_dir)
        subprocess.run(
            ["git", "checkout", "-q", "main"],
            cwd=str(push_repo),
            check=True,
            capture_output=True,
            env=env,
        )

        result = _spoke_push(push_repo, telemetry_dir)

        assert result.returncode != 0
        assert _steps(telemetry_dir) == []
