"""Issue #66 — Phase 2: script causality via ``AI_TOOLKIT_PARENT_SPAN``.

The causal model (``docs/dashboard-spoke-trace-scope.md``) needs one correlation
id propagated across every exec boundary so a hook/script span knows its enclosing
parent. The only new write-side plumbing:

* ``shared/hooks/lib/telemetry.sh`` reads ``$AI_TOOLKIT_PARENT_SPAN`` into
  ``parent_id``, falling back to the **spoke root** (``spoke_run_id``) when nothing
  more specific is set. An explicit ``--parent-id`` / ``$TELEMETRY_PARENT_ID``
  still wins.
* a new **PreToolUse(Bash)** hook (``parent-span-export.sh``) records the Bash
  call's ``tool_use_id`` to ``<root>/.ai-toolkit/parent-span``; telemetry.sh reads
  that file (after ``$AI_TOOLKIT_PARENT_SPAN``, before the spoke root) so agent-run
  scripts AND the native git-hooks they trigger carry the parent. It does NOT
  rewrite the command — prepending a ``VAR=value`` assignment would break the
  exact-match Bash allowlist (env-assignment prefixes are not stripped before
  matching — anthropics/claude-code#15292), re-prompting every allowlisted spoke
  command.
* a parent script that shells a telemetry-emitting child (``spoke-push`` →
  ``spoke-ready``) exports its OWN span id so the child nests under it.

These are end-to-end env-propagation tests: set the env (or drive the real
scripts) and assert the emitted span's ``parent_id``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TELEMETRY_LIB = REPO_ROOT / "shared" / "hooks" / "lib" / "telemetry.sh"
PARENT_SPAN_HOOK = REPO_ROOT / "shared" / "hooks" / "parent-span-export.sh"
TEST_SELECT = REPO_ROOT / "shared" / "hooks" / "test-select.sh"
SPOKE_PUSH = REPO_ROOT / "scripts" / "spoke-push.sh"

# Pin git config to nothing so the host's global/system config never reaches the
# fixture commits, and so a decoy $HOME is ignored.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _base_env(telemetry_dir: Path, **extra: str) -> dict[str, str]:
    """Deterministic telemetry env: opt-in, sandboxed dir, fixed workflow_rev."""
    env = os.environ.copy()
    for var in (
        "AI_TOOLKIT_TELEMETRY",
        "AI_TOOLKIT_TELEMETRY_DIR",
        "AI_TOOLKIT_WORKFLOW_REV",
        "AI_TOOLKIT_PARENT_SPAN",
        "CURSOR_PROJECT_DIR",
        "TELEMETRY_PARENT_ID",
        "INPUT",
    ):
        env.pop(var, None)
    env["AI_TOOLKIT_TELEMETRY"] = "1"
    env["AI_TOOLKIT_TELEMETRY_DIR"] = str(telemetry_dir)
    env["AI_TOOLKIT_WORKFLOW_REV"] = "testrev0"
    env.update(extra)
    return env


def _emit(args: str, env: dict[str, str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    script = f'source "{TELEMETRY_LIB}"; telemetry_emit_span {args}'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, cwd=str(cwd)
    )


def _read_events(events_file: Path) -> list[dict]:
    return [json.loads(line) for line in events_file.read_text().splitlines()]


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "sample-project"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    return tmp_path / "telemetry"


# ── telemetry.sh: AI_TOOLKIT_PARENT_SPAN → parent_id ────────────────────────────


class TestParentSpanEnv:
    def test_env_parent_span_becomes_parent_id(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        env = _base_env(telemetry_dir, AI_TOOLKIT_PARENT_SPAN="toolu_abc123")
        _emit("--kind script --name spoke-push --status success", env, cwd=project_root)

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "toolu_abc123"

    def test_explicit_flag_beats_env(self, project_root: Path, telemetry_dir: Path) -> None:
        # An explicit --parent-id is the caller's deliberate choice and must win
        # over the ambient env correlation id.
        env = _base_env(telemetry_dir, AI_TOOLKIT_PARENT_SPAN="toolu_env")
        _emit(
            "--kind script --name spoke-ready --parent-id span_explicit --status success",
            env,
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "span_explicit"

    def test_telemetry_parent_id_var_beats_env(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        env = _base_env(
            telemetry_dir,
            AI_TOOLKIT_PARENT_SPAN="toolu_env",
            TELEMETRY_PARENT_ID="span_inproc",
        )
        _emit("--kind hook --name secrets-scan --status success", env, cwd=project_root)

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "span_inproc"


# ── telemetry.sh: spoke-root fallback ───────────────────────────────────────────


class TestSpokeRootFallback:
    def test_falls_back_to_spoke_run_id(self, project_root: Path, telemetry_dir: Path) -> None:
        # No AI_TOOLKIT_PARENT_SPAN, no --parent-id: an emit INSIDE a spoke hangs off
        # the spoke root, identified by spoke_run_id, rather than orphaning at null.
        run_id = "feature/66-demo+1700000000"
        (project_root / ".ai-toolkit").mkdir(parents=True)
        (project_root / ".ai-toolkit" / "spoke-run-id").write_text(run_id + "\n")

        env = _base_env(telemetry_dir)
        _emit("--kind hook --name secrets-scan --status success", env, cwd=project_root)

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["spoke_run_id"] == run_id
        assert span["parent_id"] == run_id

    def test_env_parent_beats_spoke_root(self, project_root: Path, telemetry_dir: Path) -> None:
        run_id = "feature/66-demo+1700000000"
        (project_root / ".ai-toolkit").mkdir(parents=True)
        (project_root / ".ai-toolkit" / "spoke-run-id").write_text(run_id + "\n")

        env = _base_env(telemetry_dir, AI_TOOLKIT_PARENT_SPAN="toolu_specific")
        _emit("--kind hook --name secrets-scan --status success", env, cwd=project_root)

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "toolu_specific"

    def test_outside_a_spoke_parent_id_stays_null(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        # No spoke-run-id file and no env: there is no root to attach to, so the
        # span stays unparented (null), preserving the pre-#66 contract.
        env = _base_env(telemetry_dir)
        _emit("--kind hook --name secrets-scan --status success", env, cwd=project_root)

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] is None


# ── PreToolUse(Bash) hook: parent-span-export ───────────────────────────────────


def _run_hook(payload: dict, env: dict[str, str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PARENT_SPAN_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
    )


def _parent_span_file(root: Path) -> Path:
    return root / ".ai-toolkit" / "parent-span"


class TestParentSpanExportHook:
    def test_records_tool_use_id_to_file(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": "toolu_xyz",
            "tool_input": {"command": "bash .ai-toolkit/scripts/spoke-push.sh --ready 66"},
        }
        res = _run_hook(payload, _base_env(telemetry_dir), cwd=project_root)

        assert res.returncode == 0, res.stderr
        assert _parent_span_file(project_root).read_text().strip() == "toolu_xyz"

    def test_never_rewrites_the_command(self, project_root: Path, telemetry_dir: Path) -> None:
        # The whole point: the hook must NOT touch the command (no updatedInput), so
        # the exact-match Bash allowlist is never perturbed. Stdout stays empty.
        payload = {
            "tool_name": "Bash",
            "tool_use_id": "toolu_xyz",
            "tool_input": {"command": "bash .ai-toolkit/scripts/spoke-push.sh --ready 66"},
        }
        res = _run_hook(payload, _base_env(telemetry_dir), cwd=project_root)

        assert res.stdout.strip() == ""
        assert "updatedInput" not in res.stdout

    def test_overwrites_with_latest_id(self, project_root: Path, telemetry_dir: Path) -> None:
        # Each Bash call rewrites the pointer to its own tool_use_id (the parent of
        # whatever that command spawns), so a stale value never lingers.
        for tid in ("toolu_old", "toolu_new"):
            _run_hook(
                {"tool_name": "Bash", "tool_use_id": tid, "tool_input": {"command": "ls"}},
                _base_env(telemetry_dir),
                cwd=project_root,
            )

        assert _parent_span_file(project_root).read_text().strip() == "toolu_new"

    def test_noop_without_tool_use_id(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        res = _run_hook(payload, _base_env(telemetry_dir), cwd=project_root)

        assert res.returncode == 0
        assert not _parent_span_file(project_root).exists()

    def test_rejects_unclean_id(self, project_root: Path, telemetry_dir: Path) -> None:
        # A non-opaque id (anything outside [A-Za-z0-9_-]) is dropped, not recorded.
        payload = {
            "tool_name": "Bash",
            "tool_use_id": "toolu_x; rm -rf /",
            "tool_input": {"command": "ls"},
        }
        res = _run_hook(payload, _base_env(telemetry_dir), cwd=project_root)

        assert res.returncode == 0
        assert not _parent_span_file(project_root).exists()

    def test_noop_when_telemetry_disabled(self, project_root: Path, telemetry_dir: Path) -> None:
        # When telemetry is off, the hook records nothing.
        env = _base_env(telemetry_dir)
        env.pop("AI_TOOLKIT_TELEMETRY")
        payload = {
            "tool_name": "Bash",
            "tool_use_id": "toolu_xyz",
            "tool_input": {"command": "bash .ai-toolkit/scripts/spoke-push.sh"},
        }
        res = _run_hook(payload, env, cwd=project_root)

        assert res.returncode == 0
        assert not _parent_span_file(project_root).exists()


# ── telemetry.sh: the parent-span file feeds parent_id (hook → script handoff) ──


class TestParentSpanFileResolution:
    def test_file_becomes_parent_id(self, project_root: Path, telemetry_dir: Path) -> None:
        # The hook+emit handoff: with the pointer file present and no more specific
        # id in the env, an emitted span hangs off the recorded tool_use_id.
        (project_root / ".ai-toolkit").mkdir(parents=True)
        _parent_span_file(project_root).write_text("toolu_fromfile\n")

        _emit(
            "--kind hook --name test-select.sh --status success",
            _base_env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "toolu_fromfile"

    def test_env_beats_file(self, project_root: Path, telemetry_dir: Path) -> None:
        # A parent shell that exported its own span id (script → script) outranks the
        # file the hook recorded for the enclosing Bash tool call.
        (project_root / ".ai-toolkit").mkdir(parents=True)
        _parent_span_file(project_root).write_text("toolu_fromfile\n")

        env = _base_env(telemetry_dir, AI_TOOLKIT_PARENT_SPAN="span_from_parent")
        _emit("--kind script --name spoke-ready --status success", env, cwd=project_root)

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "span_from_parent"

    def test_file_beats_spoke_root(self, project_root: Path, telemetry_dir: Path) -> None:
        (project_root / ".ai-toolkit").mkdir(parents=True)
        _parent_span_file(project_root).write_text("toolu_fromfile\n")
        (project_root / ".ai-toolkit" / "spoke-run-id").write_text("feature/66-demo+1700000000\n")

        _emit(
            "--kind hook --name secrets-scan --status success",
            _base_env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "toolu_fromfile"


# ── native git-hook (test-select) inherits the parent via the env ───────────────


class TestNativeHookInheritsParent:
    def test_test_select_hook_span_carries_parent(
        self, tmp_path: Path, telemetry_dir: Path
    ) -> None:
        # A native pre-push hook inherits the env of the `git push` that triggered
        # it. With AI_TOOLKIT_PARENT_SPAN set on that push (spoke-push exports its
        # span id around the push), test-select's auto-emitted kind=hook span carries
        # it as parent_id. TEST_SELECT_SKIP short-circuits the suite so this is instant.
        env = _base_env(
            tmp_path,
            AI_TOOLKIT_PARENT_SPAN="toolu_pushcall",
            TEST_SELECT_SKIP="1",
        )
        env["AI_TOOLKIT_TELEMETRY_DIR"] = str(telemetry_dir)
        subprocess.run(
            ["bash", str(TEST_SELECT)],
            input="",
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_path),
        )

        spans = _read_events(telemetry_dir / "events.jsonl")
        hook_spans = [s for s in spans if s["kind"] == "hook"]
        assert hook_spans, "test-select must emit a kind=hook span at exit"
        assert all(s["parent_id"] == "toolu_pushcall" for s in hook_spans)


# ── script → script: spoke-push exports its own span id for spoke-ready ─────────


def _make_repo_with_remote(tmp_path: Path) -> Path:
    """A clone-with-origin on a feature branch — enough to drive a real push."""
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run(
            ["git", "config", k, v], cwd=str(repo), check=True, capture_output=True, env=_GIT_ENV
        )
    (repo / "README.md").write_text("seed\n")
    (repo / ".gitignore").write_text(".ai-toolkit/\n")
    subprocess.run(
        ["git", "add", "."], cwd=str(repo), check=True, capture_output=True, env=_GIT_ENV
    )
    subprocess.run(
        ["git", "commit", "-qm", "seed", "-m", "Refs #66"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "push", "-q", "-u", "origin", "main"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/66-demo"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    (repo / ".ai-toolkit").mkdir()
    (repo / ".ai-toolkit" / "spoke-run-id").write_text("feature/66-demo+1700000000\n")
    return repo


class TestScriptToScriptParent:
    def test_spoke_ready_nests_under_spoke_push(self, tmp_path: Path, telemetry_dir: Path) -> None:
        repo = _make_repo_with_remote(tmp_path)
        # In a real run the hook records the Bash tool_use_id to the pointer file and
        # spoke-push reads it; here we set the env directly (a higher-precedence
        # source) to the same effect. spoke-push must
        # carry it as ITS parent, and export its OWN span id for the spoke-ready child.
        env = {
            **_GIT_ENV,
            "AI_TOOLKIT_TELEMETRY": "1",
            "AI_TOOLKIT_TELEMETRY_DIR": str(telemetry_dir),
            "AI_TOOLKIT_WORKFLOW_REV": "testrev0",
            "AI_TOOLKIT_PARENT_SPAN": "toolu_pushcall",
        }
        res = subprocess.run(
            ["bash", str(SPOKE_PUSH), "--ready", "66"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=env,
        )
        assert res.returncode == 0, f"spoke-push failed:\n{res.stdout}\n{res.stderr}"

        spans = _read_events(telemetry_dir / "events.jsonl")
        push = next(s for s in spans if s["name"] == "spoke-push")
        ready = next(s for s in spans if s["name"] == "spoke-ready")

        # spoke-push hangs off the Bash tool call that ran it…
        assert push["parent_id"] == "toolu_pushcall"
        # …and spoke-ready hangs off spoke-push (script → script containment).
        assert ready["parent_id"] == push["span_id"]
