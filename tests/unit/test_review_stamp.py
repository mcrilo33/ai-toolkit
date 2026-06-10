"""RED-phase unit tests for review-stamp signature verification and guards.

Covers three shell components of the HMAC-signed review-approval authenticator:

1. Push-gate signature verification — ``shared/hooks/reviewer-sep-warn.sh``
   gains HMAC verification of the ``.review/<hash>.json`` artifact (helpers
   ``review_stamp_key`` / ``review_stamp_verify_sig`` in lib/utils.sh). With a
   resolvable key, an invalid or missing signature is enforced via
   ship_gate_enforce (deny on Cursor, advisory elsewhere). With no resolvable
   key, today's existence-check behavior is kept plus a warn naming
   REVIEW_STAMP_KEY.
2. Cursor MCP guard — ``shared/hooks/review-stamp-guard.sh``
   (beforeMCPExecution, failClosed) allows ``approve_review`` only inside a
   fresh review window (``.review/.window``, TTL 1800s).
3. Window lifecycle — ``shared/hooks/review-window-open.sh`` (subagentStart)
   creates the window only for code-review subagents;
   ``shared/hooks/review-window-close.sh`` (subagentStop) removes it.

Signature contract: HMAC-SHA256 hex over the string "<hash>:<verdict>" keyed
by REVIEW_STAMP_KEY (env first, else macOS Keychain item REVIEW_STAMP_KEY).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
UTILS = HOOKS_DIR / "lib" / "utils.sh"
REVIEWER_SEP = HOOKS_DIR / "reviewer-sep-warn.sh"
GUARD = HOOKS_DIR / "review-stamp-guard.sh"
WINDOW_OPEN = HOOKS_DIR / "review-window-open.sh"
WINDOW_CLOSE = HOOKS_DIR / "review-window-close.sh"

BLOCK = 2
ALLOW = 0

TEST_KEY = "test-key-123"
WRONG_KEY = "not-the-real-key"
WINDOW_TTL_SECONDS = 1800


# ── payload builders (mirror test_cursor_hooks.py shapes) ────────────


def cursor_shell(command: str, root: Path) -> str:
    return json.dumps(
        {
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "cwd": "",
            "workspace_roots": [str(root)],
        }
    )


def claude_bash(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def cursor_mcp(tool_name: str, root: Path) -> str:
    # beforeMCPExecution carries tool identity and workspace roots but NO
    # agent identity — the guard must decide from tool_name + window state.
    return json.dumps(
        {
            "hook_event_name": "beforeMCPExecution",
            "tool_name": tool_name,
            "tool_input": {"verdict": "APPROVE", "summary": "x"},
            "workspace_roots": [str(root)],
        }
    )


def cursor_subagent(event: str, agent: str, root: Path) -> str:
    # Payload field naming for subagent identity is not pinned by Cursor docs;
    # the scripts grep the raw payload for "code-review" — carry it in both
    # plausible fields.
    return json.dumps(
        {
            "hook_event_name": event,
            "subagent_type": agent,
            "agent": agent,
            "workspace_roots": [str(root)],
        }
    )


# ── harness ──────────────────────────────────────────────────────────


def _hook_env(*, key: str | None = None, home: Path | None = None) -> dict[str, str]:
    """Controlled hook environment: REVIEW_STAMP_KEY never leaks in from the
    developer machine; HOME redirect makes the macOS Keychain lookup fail."""
    env = {k: v for k, v in os.environ.items() if k != "REVIEW_STAMP_KEY"}
    env.pop("CURSOR_PROJECT_DIR", None)
    if key is not None:
        env["REVIEW_STAMP_KEY"] = key
    if home is not None:
        env["HOME"] = str(home)
    return env


def _run(
    script: Path, payload: str, *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )


def _sign(key: str, diff_hash: str, verdict: str) -> str:
    return hmac.new(key.encode(), f"{diff_hash}:{verdict}".encode(), hashlib.sha256).hexdigest()


def _range_hash(repo: Path) -> str:
    """Push-time hash oracle: utils.sh review_diff_hash over BASE..HEAD."""
    script = (
        f'source "{UTILS}"; '
        f'BASE=$(review_base_ref "{repo}"); '
        f'review_diff_hash "{repo}" "$BASE" range'
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    digest = out.stdout.strip()
    assert len(digest) == 64, f"utils.sh did not produce a sha256: {digest!r}"
    return digest


def _write_artifact(
    repo: Path, diff_hash: str, verdict: str, *, signature: str | None
) -> Path:
    artifact: dict[str, str] = {
        "verdict": verdict,
        "summary": "test artifact",
        "reviewer": "code-review",
        "timestamp": "2026-06-10T00:00:00Z",
        "diff_hash": diff_hash,
        "sig_alg": "HMAC-SHA256",
    }
    if signature is not None:
        artifact["signature"] = signature
    path = repo / ".review" / f"{diff_hash}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(artifact))
    return path


def _accessor(func_call: str, *, env: dict[str, str]) -> subprocess.CompletedProcess:
    script = f'source "{UTILS}"\n{func_call}\n'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env
    )


@pytest.fixture()
def pushed_repo(tmp_path: Path) -> Path:
    """Repo with origin/main upstream and ONE unpushed source commit.

    Same shape as _add_upstream_with_change in test_cursor_hooks.py — the
    reviewer-sep gate needs a resolvable merge-base to compute the range hash.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
        )

    git("init", "-q")
    git("config", "user.email", "test@test.test")
    git("config", "user.name", "test")
    git("config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("seed\n")
    git("add", "README.md")
    git("commit", "-qm", "chore: seed", "-m", "Refs #0")
    git("init", "-q", "--bare", str(tmp_path / "remote.git"))
    git("remote", "add", "origin", str(tmp_path / "remote.git"))
    git("push", "-q", "-u", "origin", "HEAD:main")
    git("checkout", "-q", "-b", "feature/1-x")
    git("branch", "--set-upstream-to=origin/main", "feature/1-x")
    (repo / "src.py").write_text("def f():\n    return 1\n")
    git("add", "src.py")
    git("commit", "-qm", "feat: add", "-m", "Refs #1")
    return repo


@pytest.fixture()
def windowed_repo(tmp_path: Path) -> Path:
    """Plain git repo for window-guard tests (no upstream needed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True)
    return repo


def _open_window(repo: Path, *, age_seconds: int = 0) -> Path:
    window = repo / ".review" / ".window"
    window.parent.mkdir(exist_ok=True)
    window.write_text(str(int(time.time())))
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(window, (old, old))
    return window


# ── utils.sh signature helpers ───────────────────────────────────────


class TestStampHelpers:
    def test_review_stamp_key_resolves_from_env(self) -> None:
        env = _hook_env(key=TEST_KEY)

        out = _accessor('printf "%s" "$(review_stamp_key)"', env=env)

        assert out.stdout == TEST_KEY

    def test_review_stamp_verify_sig_accepts_valid_signature(self) -> None:
        diff_hash = "a" * 64
        signature = _sign(TEST_KEY, diff_hash, "APPROVE")
        env = _hook_env(key=TEST_KEY)

        out = _accessor(
            f'review_stamp_verify_sig "{diff_hash}" "APPROVE" "{signature}" "{TEST_KEY}" '
            "&& echo ok || echo bad",
            env=env,
        )

        assert out.stdout.strip() == "ok"

    def test_review_stamp_verify_sig_rejects_wrong_key_signature(self) -> None:
        # The helper must EXIST and reject — a missing function also "fails",
        # so probe definition first or this test would pass vacuously at RED.
        diff_hash = "a" * 64
        signature = _sign(WRONG_KEY, diff_hash, "APPROVE")
        env = _hook_env(key=TEST_KEY)

        out = _accessor(
            "declare -F review_stamp_verify_sig >/dev/null "
            f'&& {{ review_stamp_verify_sig "{diff_hash}" "APPROVE" "{signature}" "{TEST_KEY}" '
            "&& echo ok || echo bad; } || echo missing",
            env=env,
        )

        assert out.stdout.strip() == "bad"


# ── reviewer-sep-warn: HMAC verification at the push gate ────────────


class TestSignatureGate:
    def test_valid_signed_approve_allows_cursor_push(self, pushed_repo: Path) -> None:
        diff_hash = _range_hash(pushed_repo)
        _write_artifact(
            pushed_repo, diff_hash, "APPROVE", signature=_sign(TEST_KEY, diff_hash, "APPROVE")
        )
        env = _hook_env(key=TEST_KEY)

        rc = _run(REVIEWER_SEP, cursor_shell("git push", pushed_repo), cwd=pushed_repo, env=env)

        assert rc.returncode == ALLOW

    def test_wrong_key_signature_denies_cursor_push(self, pushed_repo: Path) -> None:
        # Simplest deterministic tamper: the signature was minted with a key
        # other than the verification key.
        diff_hash = _range_hash(pushed_repo)
        _write_artifact(
            pushed_repo, diff_hash, "APPROVE", signature=_sign(WRONG_KEY, diff_hash, "APPROVE")
        )
        env = _hook_env(key=TEST_KEY)

        rc = _run(REVIEWER_SEP, cursor_shell("git push", pushed_repo), cwd=pushed_repo, env=env)

        assert rc.returncode == BLOCK

    def test_missing_signature_field_denies_cursor_push(self, pushed_repo: Path) -> None:
        diff_hash = _range_hash(pushed_repo)
        _write_artifact(pushed_repo, diff_hash, "APPROVE", signature=None)
        env = _hook_env(key=TEST_KEY)

        rc = _run(REVIEWER_SEP, cursor_shell("git push", pushed_repo), cwd=pushed_repo, env=env)

        assert rc.returncode == BLOCK

    def test_no_key_keeps_existence_check_and_warns(
        self, pushed_repo: Path, tmp_path: Path
    ) -> None:
        # No env key and HOME redirected at tmp so the macOS Keychain lookup
        # cannot resolve one either → existence-check behavior, plus a warn
        # naming REVIEW_STAMP_KEY so the skipped verification is visible.
        diff_hash = _range_hash(pushed_repo)
        _write_artifact(pushed_repo, diff_hash, "APPROVE", signature=None)
        env = _hook_env(key=None, home=tmp_path / "empty-home")

        rc = _run(REVIEWER_SEP, cursor_shell("git push", pushed_repo), cwd=pushed_repo, env=env)

        assert rc.returncode == ALLOW
        assert "REVIEW_STAMP_KEY" in rc.stderr

    def test_tampered_artifact_is_advisory_on_claude(self, pushed_repo: Path) -> None:
        diff_hash = _range_hash(pushed_repo)
        _write_artifact(
            pushed_repo, diff_hash, "APPROVE", signature=_sign(WRONG_KEY, diff_hash, "APPROVE")
        )
        env = _hook_env(key=TEST_KEY)

        rc = _run(REVIEWER_SEP, claude_bash("git push"), cwd=pushed_repo, env=env)

        assert rc.returncode == ALLOW
        assert "signature" in rc.stderr.lower()


# ── review-stamp-guard: beforeMCPExecution window gate ───────────────


class TestReviewWindowGuard:
    def test_allows_non_approve_review_tool_calls(self, windowed_repo: Path) -> None:
        payload = cursor_mcp("some_other_tool", windowed_repo)

        rc = _run(GUARD, payload, cwd=windowed_repo, env=_hook_env())

        assert rc.returncode == ALLOW

    def test_denies_approve_review_with_no_window(self, windowed_repo: Path) -> None:
        payload = cursor_mcp("approve_review", windowed_repo)

        rc = _run(GUARD, payload, cwd=windowed_repo, env=_hook_env())

        assert rc.returncode == BLOCK

    def test_allows_approve_review_with_fresh_window(self, windowed_repo: Path) -> None:
        _open_window(windowed_repo)
        payload = cursor_mcp("approve_review", windowed_repo)

        rc = _run(GUARD, payload, cwd=windowed_repo, env=_hook_env())

        assert rc.returncode == ALLOW

    def test_denies_approve_review_with_stale_window(self, windowed_repo: Path) -> None:
        _open_window(windowed_repo, age_seconds=WINDOW_TTL_SECONDS + 60)
        payload = cursor_mcp("approve_review", windowed_repo)

        rc = _run(GUARD, payload, cwd=windowed_repo, env=_hook_env())

        assert rc.returncode == BLOCK


# ── review-window-open / review-window-close lifecycle ───────────────


class TestReviewWindowLifecycle:
    def test_open_creates_window_for_code_review_subagent(
        self, windowed_repo: Path
    ) -> None:
        payload = cursor_subagent("subagentStart", "code-review", windowed_repo)

        rc = _run(WINDOW_OPEN, payload, cwd=windowed_repo, env=_hook_env())

        assert rc.returncode == ALLOW
        assert (windowed_repo / ".review" / ".window").is_file()

    def test_open_ignores_other_subagents(self, windowed_repo: Path) -> None:
        payload = cursor_subagent("subagentStart", "tdd-green", windowed_repo)

        rc = _run(WINDOW_OPEN, payload, cwd=windowed_repo, env=_hook_env())

        assert rc.returncode == ALLOW
        assert not (windowed_repo / ".review" / ".window").exists()

    def test_close_removes_window_for_code_review_subagent(
        self, windowed_repo: Path
    ) -> None:
        _open_window(windowed_repo)
        payload = cursor_subagent("subagentStop", "code-review", windowed_repo)

        rc = _run(WINDOW_CLOSE, payload, cwd=windowed_repo, env=_hook_env())

        assert rc.returncode == ALLOW
        assert not (windowed_repo / ".review" / ".window").exists()
