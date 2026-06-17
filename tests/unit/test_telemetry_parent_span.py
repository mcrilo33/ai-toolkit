"""Issue #66 — Phase 2: script causality via ``AI_TOOLKIT_PARENT_SPAN``.

The causal model (``docs/dashboard-spoke-trace-scope.md``) needs one correlation
id propagated across every exec boundary so a hook/script span knows its enclosing
parent. The only new write-side plumbing:

* ``shared/hooks/lib/telemetry.sh`` reads ``$AI_TOOLKIT_PARENT_SPAN`` into
  ``parent_id``, falling back to the **spoke root** (``spoke_run_id``) when nothing
  more specific is set. An explicit ``--parent-id`` / ``$TELEMETRY_PARENT_ID``
  still wins.
* a new **PreToolUse(Bash)** hook (``parent-span-export.sh``) rewrites the command
  via ``hookSpecificOutput.updatedInput`` to prepend an **allowlist-safe leading
  assignment** ``AI_TOOLKIT_PARENT_SPAN=<tool_use_id> <command>`` so agent-run
  scripts AND native git-hooks (which inherit the env) carry the parent. A bare
  leading ``VAR=value`` assignment is stripped before Bash permission matching, so
  the exact-match spoke allowlist (``Bash(bash .ai-toolkit/scripts/spoke-push.sh:*)``)
  keeps matching — unlike an ``export …;`` prefix, which splits into a second
  subcommand and breaks per-subcommand matching.
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


class TestParentSpanExportHook:
    def test_prepends_leading_assignment(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": "toolu_xyz",
            "tool_input": {"command": "bash .ai-toolkit/scripts/spoke-push.sh --ready 66"},
        }
        res = _run_hook(payload, _base_env(telemetry_dir), cwd=project_root)

        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        rewritten = out["hookSpecificOutput"]["updatedInput"]["command"]
        assert rewritten == (
            "AI_TOOLKIT_PARENT_SPAN=toolu_xyz bash .ai-toolkit/scripts/spoke-push.sh --ready 66"
        )
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_leading_assignment_not_a_second_subcommand(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        # The rewrite must be a single command with a leading VAR=value assignment
        # (allowlist-safe), never `export …;` which the Bash matcher would treat as
        # two subcommands and re-prompt the exact-match spoke allow rules.
        payload = {
            "tool_name": "Bash",
            "tool_use_id": "toolu_xyz",
            "tool_input": {"command": "git push -u origin feature/66"},
        }
        res = _run_hook(payload, _base_env(telemetry_dir), cwd=project_root)

        rewritten = json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        assert rewritten.startswith("AI_TOOLKIT_PARENT_SPAN=toolu_xyz git push")
        assert "export " not in rewritten
        assert ";" not in rewritten.split("git push", 1)[0]

    def test_idempotent_when_already_prefixed(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_use_id": "toolu_new",
            "tool_input": {"command": "AI_TOOLKIT_PARENT_SPAN=toolu_old git status"},
        }
        res = _run_hook(payload, _base_env(telemetry_dir), cwd=project_root)

        # No double-prefix: the hook leaves an already-stamped command untouched.
        assert res.returncode == 0
        assert res.stdout.strip() == "" or "updatedInput" not in res.stdout

    def test_noop_without_tool_use_id(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        res = _run_hook(payload, _base_env(telemetry_dir), cwd=project_root)

        assert res.returncode == 0
        assert res.stdout.strip() == "" or "updatedInput" not in res.stdout

    def test_noop_when_telemetry_disabled(self, project_root: Path, telemetry_dir: Path) -> None:
        # When telemetry is off, the hook must not touch any command — no rewrite,
        # so a non-telemetry user's allowlist is never perturbed.
        env = _base_env(telemetry_dir)
        env.pop("AI_TOOLKIT_TELEMETRY")
        payload = {
            "tool_name": "Bash",
            "tool_use_id": "toolu_xyz",
            "tool_input": {"command": "bash .ai-toolkit/scripts/spoke-push.sh"},
        }
        res = _run_hook(payload, env, cwd=project_root)

        assert res.returncode == 0
        assert res.stdout.strip() == "" or "updatedInput" not in res.stdout


# ── native git-hook (test-select) inherits the parent via the env ───────────────


class TestNativeHookInheritsParent:
    def test_test_select_hook_span_carries_parent(
        self, tmp_path: Path, telemetry_dir: Path
    ) -> None:
        # A native pre-push hook inherits the env of the `git push` that triggered
        # it. With AI_TOOLKIT_PARENT_SPAN set (by the PreToolUse rewrite on that
        # push), test-select's auto-emitted kind=hook span carries it as parent_id.
        # TEST_SELECT_SKIP short-circuits the suite so this stays instant.
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
        # The PreToolUse rewrite would put the Bash tool_use_id here; spoke-push must
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
