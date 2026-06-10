#!/usr/bin/env python3
"""review-stamp MCP server — HMAC-signed review-approval authenticator.

A stdlib-only JSON-RPC 2.0 server over stdio exposing one tool,
``approve_review``. The tool stages the working tree (``git add -A``),
computes the staged diff hash, and writes a signed review-evidence artifact
at ``.review/<hash>.json``.

FRAMING CONTRACT: newline-delimited JSON — exactly one JSON object per line
on stdin and stdout. NOT Content-Length (LSP-style) framing.

CRITICAL INVARIANT (hash parity): the artifact filename hash must be
byte-identical to ``review_diff_hash <root> <base> staged`` from
shared/hooks/lib/utils.sh. Parity is achieved by invoking the exact same git
command and applying the same CRLF stripping (``sed 's/\\r$//'`` strips a CR
immediately before each newline) before sha256.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SERVER_NAME = "review-stamp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

APPROVE_REVIEW_TOOL: dict = {
    "name": "approve_review",
    "description": (
        "Record a signed review verdict bound to the staged diff. Stages the "
        "working tree (git add -A), computes the staged diff hash, and writes "
        "an HMAC-signed .review/<hash>.json review-evidence artifact."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["APPROVE", "REQUEST_CHANGES"],
                "description": "Review verdict for the staged diff.",
            },
            "summary": {
                "type": "string",
                "description": "Short review summary recorded in the artifact.",
            },
        },
        "required": ["verdict", "summary"],
    },
}

VALID_VERDICTS = frozenset({"APPROVE", "REQUEST_CHANGES"})


class ToolError(RuntimeError):
    """Tool-level failure surfaced as a JSON-RPC error object."""


def repo_root() -> Path:
    """Resolve the repository root.

    Returns:
        $CURSOR_PROJECT_DIR when set, else the nearest ancestor of the
        current working directory containing ``.git``, else the cwd itself.
    """
    env_root = os.environ.get("CURSOR_PROJECT_DIR")
    if env_root:
        return Path(env_root)
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True)


def base_ref(root: Path) -> str:
    """Resolve the diff base exactly like utils.sh ``review_base_ref``.

    Fallback chain: merge-base with the tracked upstream (``@{upstream}``),
    then ``origin/main``, then ``origin/HEAD``.

    Args:
        root: Repository root directory.

    Returns:
        The merge-base commit sha, or an empty string when none resolves.
    """
    for ref in ("@{upstream}", "origin/main", "origin/HEAD"):
        out = _git(root, "merge-base", ref, "HEAD")
        if out.returncode == 0:
            return out.stdout.decode().strip()
    return ""


def staged_diff_hash(root: Path, base: str) -> str:
    """Hash the staged diff byte-identically to utils.sh ``review_diff_hash``.

    Runs the same git invocation as the ``staged`` variant and applies the
    same line-ending normalization: ``sed 's/\\r$//'`` removes a CR directly
    before each newline, which on this byte stream is exactly the
    ``\\r\\n -> \\n`` replacement.

    Args:
        root: Repository root directory.
        base: Base commit sha from :func:`base_ref`.

    Returns:
        The 64-char sha256 hex digest of the normalized staged diff.
    """
    out = _git(
        root,
        "diff", "--no-color", "--no-ext-diff", "-M", "--cached", base,
        "--", ".", ":(exclude).review/",
    )
    diff = out.stdout if out.returncode == 0 else b""
    return hashlib.sha256(diff.replace(b"\r\n", b"\n")).hexdigest()


def sign(key: str, diff_hash: str, verdict: str) -> str:
    """HMAC-SHA256 hex digest over ``<hash>:<verdict>`` keyed by the stamp key."""
    message = f"{diff_hash}:{verdict}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def approve_review(arguments: dict) -> dict:
    """Execute the approve_review tool: stage, hash, sign, write artifact.

    Args:
        arguments: Tool arguments carrying ``verdict`` and ``summary``.

    Returns:
        The artifact dict that was written to ``.review/<hash>.json``.

    Raises:
        ToolError: When the verdict is not in the declared enum, when
            REVIEW_STAMP_KEY is unset (no artifact is written and no
            ``.review`` directory is created), or no base ref resolves.
    """
    verdict = str(arguments.get("verdict", ""))
    if verdict not in VALID_VERDICTS:
        raise ToolError(
            f"invalid verdict: {verdict!r} — must be one of "
            f"{sorted(VALID_VERDICTS)}."
        )
    key = os.environ.get("REVIEW_STAMP_KEY", "")
    if not key:
        raise ToolError(
            "REVIEW_STAMP_KEY is not set — cannot sign the review artifact. "
            "Launch the server via run.sh so the key loads from the Keychain."
        )
    root = repo_root()
    _git(root, "add", "-A")
    base = base_ref(root)
    if not base:
        raise ToolError(
            "no diff base resolves (@{upstream}, origin/main, origin/HEAD) — "
            "cannot bind the approval to a diff hash."
        )
    diff_hash = staged_diff_hash(root, base)
    artifact = {
        "verdict": verdict,
        "summary": str(arguments.get("summary", "")),
        "reviewer": "code-review",
        # timezone.utc (not datetime.UTC): must run on the PATH python3,
        # which can be the Xcode CLT 3.9 where datetime.UTC does not exist.
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),  # noqa: UP017
        "diff_hash": diff_hash,
        "signature": sign(key, diff_hash, verdict),
        "sig_alg": "HMAC-SHA256",
    }
    review_dir = root / ".review"
    review_dir.mkdir(exist_ok=True)
    (review_dir / f"{diff_hash}.json").write_text(json.dumps(artifact, indent=2) + "\n")
    return artifact


def _result(msg_id: int | str, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: int | str, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _handle_tools_call(msg_id: int | str, params: dict) -> dict:
    name = params.get("name", "")
    if name != "approve_review":
        return _error(msg_id, -32602, f"unknown tool: {name}")
    try:
        artifact = approve_review(params.get("arguments") or {})
    except ToolError as e:
        return _error(msg_id, -32000, str(e))
    text = (
        f"review recorded: .review/{artifact['diff_hash']}.json "
        f"({artifact['verdict']})"
    )
    return _result(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})


def handle(msg: dict) -> dict | None:
    """Dispatch one JSON-RPC message.

    Args:
        msg: Parsed JSON-RPC request or notification.

    Returns:
        The response object, or None for notifications (no id — no response).
    """
    msg_id = msg.get("id")
    if msg_id is None:
        return None
    method = msg.get("method", "")
    if method == "initialize":
        return _result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "tools/list":
        return _result(msg_id, {"tools": [APPROVE_REVIEW_TOOL]})
    if method == "tools/call":
        return _handle_tools_call(msg_id, msg.get("params") or {})
    return _error(msg_id, -32601, f"method not found: {method}")


def main() -> None:
    """Serve newline-delimited JSON-RPC 2.0 over stdio until stdin closes."""
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
