"""RED-phase unit tests for the review-stamp MCP server.

The server (``mcp/review-stamp/server.py``) is an HMAC-signed review-approval
authenticator: a stdlib-Python JSON-RPC 2.0 server over stdio exposing one
tool, ``approve_review``, that stages the working tree (``git add -A``),
computes the staged diff hash, and writes a signed ``.review/<hash>.json``
review-evidence artifact.

FRAMING CONTRACT: NEWLINE-DELIMITED JSON — exactly one JSON object per line on
stdin and stdout. NOT Content-Length (LSP-style) framing.

CRITICAL INVARIANT (hash parity): the hash in the artifact filename must be
byte-identical to ``review_diff_hash <root> <base> staged`` from
shared/hooks/lib/utils.sh — i.e. sha256 over
``git diff --no-color --no-ext-diff -M --cached <base> -- . ':(exclude).review/'``
with CRLF stripped. Parity is asserted by running the toolkit's REAL utils.sh
in a subprocess against the same repo state, never by re-implementing it here.
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

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "mcp" / "review-stamp" / "server.py"
UTILS = REPO_ROOT / "shared" / "hooks" / "lib" / "utils.sh"

TEST_KEY = "test-key-123"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    )
    return out.stdout


def _utils_staged_hash(repo: Path) -> str:
    """Compute the staged diff hash with the toolkit's ACTUAL utils.sh.

    This is the parity oracle: source shared/hooks/lib/utils.sh and run
    review_base_ref + review_diff_hash exactly as the push gate would.
    """
    script = (
        f'source "{UTILS}"; '
        f'BASE=$(review_base_ref "{repo}"); '
        f'review_diff_hash "{repo}" "$BASE" staged'
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    digest = out.stdout.strip()
    assert len(digest) == 64, f"utils.sh did not produce a sha256: {digest!r}"
    return digest


def _artifacts(repo: Path) -> list[Path]:
    review_dir = repo / ".review"
    if not review_dir.is_dir():
        return []
    return sorted(review_dir.glob("*.json"))


def _staged_files(repo: Path) -> list[str]:
    return _git(repo, "diff", "--cached", "--name-only").splitlines()


class ReviewStampClient:
    """Newline-delimited JSON-RPC 2.0 client for the review-stamp server.

    One JSON object per line in each direction (the framing contract). Every
    failure path raises AssertionError with the server's stderr so a missing
    or crashed server fails loudly and for the right reason.
    """

    def __init__(self, repo: Path, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            ["python3", str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo),
            env=env,
        )
        self._id = 0
        time.sleep(0.2)  # let an unstartable server die before the first request

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        assert self.proc.poll() is None, (
            f"server exited rc={self.proc.returncode} before {method!r}: {self._stderr_tail()}"
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line.strip(), (
            f"no response line from server for {method!r}: {self._stderr_tail()}"
        )
        return json.loads(line)

    def initialize(self) -> dict:
        return self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        )

    def approve_review(self, verdict: str, summary: str) -> dict:
        return self.request(
            "tools/call",
            {"name": "approve_review", "arguments": {"verdict": verdict, "summary": summary}},
        )

    def _stderr_tail(self) -> str:
        if self.proc.poll() is None:
            return "(server still running)"
        assert self.proc.stderr is not None
        return (self.proc.stderr.read() or "").strip()[-500:]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)


@pytest.fixture()
def review_repo(tmp_path: Path) -> Path:
    """Git repo with a resolvable base (origin/main) and UNSTAGED changes.

    Mirrors the test_cursor_hooks fixtures: seed commit pushed to a bare
    remote, feature branch tracking origin/main, then working-tree changes
    left unstaged — the server itself must run `git add -A`.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.test")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(repo, "init", "-q", "--bare", str(tmp_path / "remote.git"))
    _git(repo, "remote", "add", "origin", str(tmp_path / "remote.git"))
    _git(repo, "push", "-q", "-u", "origin", "HEAD:main")
    _git(repo, "checkout", "-q", "-b", "feature/review-stamp")
    _git(repo, "branch", "--set-upstream-to=origin/main", "feature/review-stamp")
    (repo / "README.md").write_text("seed\nchanged\n")
    (repo / "src.py").write_text("def f():\n    return 1\n")
    return repo


@pytest.fixture()
def start_server(review_repo: Path):
    """Factory starting the server against review_repo; auto-closed on teardown."""
    clients: list[ReviewStampClient] = []

    def _start(*, key: str | None = TEST_KEY) -> ReviewStampClient:
        assert SERVER.is_file(), f"server not implemented yet: {SERVER}"
        env = {k: v for k, v in os.environ.items() if k != "REVIEW_STAMP_KEY"}
        env["CURSOR_PROJECT_DIR"] = str(review_repo)
        if key is not None:
            env["REVIEW_STAMP_KEY"] = key
        client = ReviewStampClient(review_repo, env)
        clients.append(client)
        return client

    yield _start
    for client in clients:
        client.close()


class TestReviewStampServer:
    def test_initialize_reports_server_name(self, start_server) -> None:
        client = start_server()

        response = client.initialize()

        assert response["result"]["serverInfo"]["name"] == "review-stamp"
        assert "protocolVersion" in response["result"]
        assert "capabilities" in response["result"]

    def test_tools_list_exposes_approve_review_schema(self, start_server) -> None:
        client = start_server()
        client.initialize()

        response = client.request("tools/list")

        tools = {t["name"]: t for t in response["result"]["tools"]}
        assert "approve_review" in tools
        schema = tools["approve_review"]["inputSchema"]
        assert set(schema["required"]) == {"verdict", "summary"}
        assert schema["properties"]["verdict"]["enum"] == ["APPROVE", "REQUEST_CHANGES"]

    def test_approve_writes_artifact_with_all_fields(
        self, start_server, review_repo: Path
    ) -> None:
        client = start_server()
        client.initialize()

        response = client.approve_review("APPROVE", "looks good")

        assert "error" not in response
        artifacts = _artifacts(review_repo)
        assert len(artifacts) == 1
        artifact = json.loads(artifacts[0].read_text())
        assert artifact["verdict"] == "APPROVE"
        assert artifact["summary"] == "looks good"
        assert artifact["reviewer"] == "code-review"
        assert artifact["timestamp"]
        assert artifact["sig_alg"] == "HMAC-SHA256"
        assert artifact["diff_hash"] == artifacts[0].stem
        assert artifact["signature"]

    def test_artifact_hash_matches_utils_review_diff_hash(
        self, start_server, review_repo: Path
    ) -> None:
        # CRITICAL invariant: byte-identical parity with utils.sh review_diff_hash.
        client = start_server()
        client.initialize()

        client.approve_review("APPROVE", "parity check")

        artifacts = _artifacts(review_repo)
        assert len(artifacts) == 1
        assert artifacts[0].stem == _utils_staged_hash(review_repo)

    def test_signature_is_hmac_over_hash_and_verdict(
        self, start_server, review_repo: Path
    ) -> None:
        client = start_server()
        client.initialize()

        client.approve_review("APPROVE", "sig check")

        artifacts = _artifacts(review_repo)
        assert len(artifacts) == 1
        diff_hash = artifacts[0].stem
        artifact = json.loads(artifacts[0].read_text())
        expected = hmac.new(
            TEST_KEY.encode(), f"{diff_hash}:APPROVE".encode(), hashlib.sha256
        ).hexdigest()
        assert artifact["signature"] == expected

    def test_request_changes_verdict_recorded_with_valid_signature(
        self, start_server, review_repo: Path
    ) -> None:
        client = start_server()
        client.initialize()

        client.approve_review("REQUEST_CHANGES", "needs work")

        artifacts = _artifacts(review_repo)
        assert len(artifacts) == 1
        diff_hash = artifacts[0].stem
        artifact = json.loads(artifacts[0].read_text())
        assert artifact["verdict"] == "REQUEST_CHANGES"
        expected = hmac.new(
            TEST_KEY.encode(), f"{diff_hash}:REQUEST_CHANGES".encode(), hashlib.sha256
        ).hexdigest()
        assert artifact["signature"] == expected

    def test_missing_key_returns_error_and_writes_no_artifact(
        self, start_server, review_repo: Path
    ) -> None:
        client = start_server(key=None)
        client.initialize()

        response = client.approve_review("APPROVE", "no key in env")

        assert "error" in response
        assert response["error"]["code"] != 0
        assert not (review_repo / ".review").exists()

    @pytest.mark.parametrize("bad_verdict", ["approve ", "LGTM"])
    def test_invalid_verdict_returns_error_and_writes_no_artifact(
        self, start_server, review_repo: Path, bad_verdict: str
    ) -> None:
        """A verdict outside the declared enum is rejected before any write."""
        client = start_server()
        client.initialize()

        response = client.approve_review(bad_verdict, "should be rejected")

        assert "error" in response
        assert response["error"]["code"] != 0
        assert _artifacts(review_repo) == []

    def test_untracked_file_is_staged_and_included_in_hash(
        self, start_server, review_repo: Path
    ) -> None:
        (review_repo / "extra.txt").write_text("brand new\n")
        client = start_server()
        client.initialize()

        client.approve_review("APPROVE", "covers untracked file")

        assert "extra.txt" in _staged_files(review_repo)
        artifacts = _artifacts(review_repo)
        assert len(artifacts) == 1
        assert artifacts[0].stem == _utils_staged_hash(review_repo)
