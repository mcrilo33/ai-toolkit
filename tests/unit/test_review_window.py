"""RED-phase unit tests for the review-window open/close ownership fix (#197).

The review-approval window (`.review/.window`) is opened by
``shared/hooks/review-window-open.sh`` when a code-review subagent starts and
removed by ``shared/hooks/review-window-close.sh`` when it stops. The two were
asymmetric and racy:

- open matched the subagent's EXACT identity; close matched any stop payload
  merely *containing* the substring "code-review", so a planner describing next
  steps (or a verify agent quoting the skill name) deleted a live window,
  spuriously hard-blocking the reviewer's ``approve_review``.
- there was no keying between the two: a single global window, check-then-act,
  so any code-review stop closed whatever window existed.

These tests pin the fixed contract:

1. Close matches the reviewer's EXACT identity — a non-reviewer stop that only
   mentions "code-review" leaves the window intact.
2. The window is keyed to its opener (``.review/.window.owner``): a code-review
   stop removes the window only when it owns it — same session id, or a
   legacy/unkeyed window — and a *different* concurrent code-review session
   cannot delete it.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
WINDOW_OPEN = HOOKS_DIR / "review-window-open.sh"
WINDOW_CLOSE = HOOKS_DIR / "review-window-close.sh"

ALLOW = 0

WINDOW = Path(".review") / ".window"
OWNER = Path(".review") / ".window.owner"


# ── payload builders ─────────────────────────────────────────────────


def subagent(
    event: str,
    root: Path,
    *,
    identity_field: str = "subagent_type",
    identity: str | None = "code-review",
    session_id: str | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    payload: dict[str, object] = {
        "hook_event_name": event,
        "workspace_roots": [str(root)],
    }
    if identity is not None:
        payload[identity_field] = identity
    if session_id is not None:
        payload["session_id"] = session_id
    if extra:
        payload.update(extra)
    return json.dumps(payload)


# ── harness ──────────────────────────────────────────────────────────


def _hook_env() -> dict[str, str]:
    # CURSOR_PROJECT_DIR would override project_root_from_payload and point the
    # window at the developer's checkout — strip it so the payload roots win.
    return {k: v for k, v in os.environ.items() if k != "CURSOR_PROJECT_DIR"}


def _run(script: Path, payload: str, *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=_hook_env(),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True)
    return root


def _open(repo: Path, *, session_id: str | None = None) -> None:
    """Open the window through the real hook so the owner sidecar is written."""
    rc = _run(WINDOW_OPEN, subagent("subagentStart", repo, session_id=session_id), cwd=repo)
    assert rc.returncode == ALLOW, rc.stderr
    assert (repo / WINDOW).is_file()


def _legacy_window(repo: Path) -> None:
    """A window with NO owner sidecar — the pre-#197 / test-helper shape."""
    window = repo / WINDOW
    window.parent.mkdir(exist_ok=True)
    window.write_text(str(int(time.time())))


# ── 1. exact-identity close ──────────────────────────────────────────


class TestCloseExactIdentity:
    def test_non_reviewer_stop_mentioning_substring_keeps_window(self, repo: Path) -> None:
        # A planner whose IDENTITY is not code-review but whose prompt mentions
        # it must not close the window (the #197 spurious-close bug).
        _open(repo)
        payload = subagent(
            "subagentStop",
            repo,
            identity="planner",
            extra={"prompt": "planning done; next spawn code-review on the diff"},
        )

        rc = _run(WINDOW_CLOSE, payload, cwd=repo)

        assert rc.returncode == ALLOW
        assert (repo / WINDOW).is_file()

    def test_reviewer_stop_closes_window(self, repo: Path) -> None:
        _open(repo)
        payload = subagent("subagentStop", repo)

        rc = _run(WINDOW_CLOSE, payload, cwd=repo)

        assert rc.returncode == ALLOW
        assert not (repo / WINDOW).exists()

    def test_reviewer_identity_in_agent_field_closes_window(self, repo: Path) -> None:
        _open(repo)
        payload = subagent("subagentStop", repo, identity_field="agent", identity="code-review")

        rc = _run(WINDOW_CLOSE, payload, cwd=repo)

        assert rc.returncode == ALLOW
        assert not (repo / WINDOW).exists()

    def test_no_identity_field_falls_back_to_substring(self, repo: Path) -> None:
        # Unknown payload shape with no identity field: the historical
        # substring grep keeps the hook working (mirrors open.sh).
        _open(repo)
        payload = subagent(
            "subagentStop", repo, identity=None, extra={"description": "code-review run"}
        )

        rc = _run(WINDOW_CLOSE, payload, cwd=repo)

        assert rc.returncode == ALLOW
        assert not (repo / WINDOW).exists()


# ── 2. ownership keying ──────────────────────────────────────────────


class TestCloseOwnership:
    def test_same_session_stop_closes_window(self, repo: Path) -> None:
        _open(repo, session_id="sess-A")
        payload = subagent("subagentStop", repo, session_id="sess-A")

        rc = _run(WINDOW_CLOSE, payload, cwd=repo)

        assert rc.returncode == ALLOW
        assert not (repo / WINDOW).exists()
        assert not (repo / OWNER).exists()

    def test_different_session_stop_keeps_window(self, repo: Path) -> None:
        # A second, concurrent code-review session stopping must not delete a
        # window it did not open.
        _open(repo, session_id="sess-A")
        payload = subagent("subagentStop", repo, session_id="sess-B")

        rc = _run(WINDOW_CLOSE, payload, cwd=repo)

        assert rc.returncode == ALLOW
        assert (repo / WINDOW).is_file()
        assert (repo / OWNER).is_file()

    def test_session_open_but_stop_without_session_still_closes(self, repo: Path) -> None:
        # Cursor's payload field naming is unpinned and may not carry
        # session_id symmetrically on start/stop. A reviewer stop that omits it
        # must still close its OWN window — never leak it to the TTL. Ownership
        # is additive: it only blocks a close by a DIFFERENT session.
        _open(repo, session_id="sess-A")
        payload = subagent("subagentStop", repo)  # no session_id

        rc = _run(WINDOW_CLOSE, payload, cwd=repo)

        assert rc.returncode == ALLOW
        assert not (repo / WINDOW).exists()
        assert not (repo / OWNER).exists()

    def test_legacy_window_without_owner_closes(self, repo: Path) -> None:
        # A window with no owner sidecar (pre-#197) is ownable by any
        # code-review stop — back-compat.
        _legacy_window(repo)
        payload = subagent("subagentStop", repo)

        rc = _run(WINDOW_CLOSE, payload, cwd=repo)

        assert rc.returncode == ALLOW
        assert not (repo / WINDOW).exists()

    def test_open_records_owner_sidecar(self, repo: Path) -> None:
        _open(repo, session_id="sess-A")

        assert (repo / OWNER).read_text().strip() == "sess-A"
