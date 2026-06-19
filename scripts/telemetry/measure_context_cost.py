#!/usr/bin/env python3
"""Measure the token cost of the source-measurable loaded-context categories.

Claude Code writes a session prefix to the prompt cache (counted as
``cache_creation`` tokens on the first call). That prefix is assembled from
several categories. Some are reconstructable from on-disk sources in the
worktree — project rules, auto-memory, the injected skills/sub-agents lists,
and a small environment block. Others (MCP connector schemas, built-in tool
schemas, the base system prompt) are NOT sourceable from a standalone script.

This script measures ONLY the source-measurable categories and stores their
token count and cost in a reusable manifest at ``<root>/.ai-toolkit/context-cost.json``.
A later decomposition step reads those values and reconciles the unmeasured
categories as a remainder: ``cache_creation_total - measured_total``.

Token counting uses the Anthropic ``count_tokens`` endpoint when reachable
(``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_API_KEY`` or ``--endpoint`` / ``--api-key``);
when unreachable or uncredentialed it falls back to a ``len(text) // 4`` estimate
and marks the category ``estimated``. It never hard-fails on missing creds.

Import-safe: no environment is read at import time, so the pure helpers can be
unit-tested without network or credentials. Configuration is read in :func:`main`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

# Default model for the count_tokens call — small/cheap; token counts are
# model-family-stable, so the haiku tokenizer matches the prefix Claude Code caches.
DEFAULT_MODEL = "claude-haiku-4-5"

# Default cache-creation price (USD per token), Opus tier. Override with --price.
DEFAULT_PRICE = 0.00000625

DEFAULT_ENDPOINT = "https://api.anthropic.com"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
ANTHROPIC_VERSION = "2023-06-01"

MANIFEST_REL = Path(".ai-toolkit/context-cost.json")

# A fixed placeholder for the environment block's date line: the real date is
# injected per-session by the runtime, so a literal keeps the block deterministic.
DATE_PLACEHOLDER = "<session-date>"

NOTE = (
    "MCP, built-in tools, and the base system prompt are NOT measured here; "
    "reconcile them as cache_creation_total - measured_total."
)

# Top-level ``key: value`` frontmatter line (no indentation — nested keys ignored).
_FM_KEY = re.compile(r"^([A-Za-z][\w-]*):\s*(.*)$")

# A callable that returns the token count of a text, or raises CountTokensError.
TokenCounter = Callable[[str], int]


class CountTokensError(RuntimeError):
    """Raised when the count_tokens endpoint is unreachable or returns no count."""


@dataclass(frozen=True, slots=True)
class Category:
    """A source-measurable loaded-context category, pre-assembly of its tokens.

    Attributes:
        name: Stable category key (``rules`` / ``memory`` / ``skills`` / ``sub-agents``
            / ``environment``).
        text: The concatenated/assembled source text whose tokens are measured.
        source_files: Repo-relative paths the text was built from (may be empty).
        estimated: Forced-estimate flag — true for reconstructed categories whose
            text never matches the runtime byte-for-byte (the environment block).
    """

    name: str
    text: str
    source_files: list[str] = field(default_factory=list)
    estimated: bool = False


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the top-level scalar keys of a YAML frontmatter block.

    Only ``key: value`` lines at column zero between the opening and closing
    ``---`` fences are read; nested/list values are ignored. A file with no
    leading frontmatter fence yields an empty mapping (handled gracefully).

    Args:
        text: Full file text.

    Returns:
        Mapping of frontmatter key to its unquoted scalar value.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = _FM_KEY.match(line)
        if match:
            result[match.group(1)] = _strip_quotes(match.group(2).strip())
    return result


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
        return value[1:-1]
    return value


def _concat_files(paths: list[Path], root: Path) -> tuple[str, list[str]]:
    """Concatenate existing files (sorted) into one text and list their rel paths."""
    texts: list[str] = []
    names: list[str] = []
    for path in sorted(paths):
        if path.is_file():
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
            names.append(str(path.relative_to(root)))
    return "\n".join(texts), names


def _frontmatter_listing(files: list[Path], root: Path) -> tuple[str, list[str]]:
    """Build a ``- name: description`` listing from each file's frontmatter.

    Mirrors how Claude Code injects the available-skills and agent-types lists:
    name + description only, never the file body. Files without a parseable
    ``name`` are skipped.

    Args:
        files: Candidate markdown files (e.g. SKILL.md or agent .md files).
        root: Worktree root for relative path reporting.

    Returns:
        A ``(listing_text, source_files)`` pair.
    """
    entries: list[str] = []
    names: list[str] = []
    for path in sorted(files):
        front = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        name = front.get("name")
        if not name:
            continue
        entries.append(f"- {name}: {front.get('description', '')}")
        names.append(str(path.relative_to(root)))
    return "\n".join(entries), names


def assemble_rules(root: Path) -> Category:
    """Assemble the rules category: CLAUDE.md + ``.claude/rules/*.md`` text."""
    paths = [root / "CLAUDE.md", *(root / ".claude" / "rules").glob("*.md")]
    text, files = _concat_files(paths, root)
    return Category("rules", text, files)


def assemble_memory(root: Path) -> Category:
    """Assemble the memory category: MEMORY.md + a ``memory/`` dir's ``*.md`` if present."""
    paths = [root / "MEMORY.md", *(root / "memory").glob("*.md")]
    text, files = _concat_files(paths, root)
    return Category("memory", text, files)


def assemble_skills(root: Path) -> Category:
    """Assemble the available-skills list from each ``.claude/skills/*/SKILL.md``."""
    files = list((root / ".claude" / "skills").glob("*/SKILL.md"))
    text, names = _frontmatter_listing(files, root)
    return Category("skills", text, names)


def assemble_agents(root: Path) -> Category:
    """Assemble the agent-types list from each ``.claude/agents/*.md`` frontmatter."""
    files = list((root / ".claude" / "agents").glob("*.md"))
    text, names = _frontmatter_listing(files, root)
    return Category("sub-agents", text, names)


def assemble_environment(root: Path) -> Category:
    """Reconstruct a best-effort environment block (always marked estimated).

    The runtime injects platform, cwd, current date, and user email; the date
    is a placeholder here (it is per-session) and the email is read from git
    config when available. The block never matches byte-for-byte, so it is
    flagged ``estimated`` regardless of how its tokens are counted.
    """
    lines = [
        f"platform: {sys.platform}",
        f"cwd: {root}",
        f"date: {DATE_PLACEHOLDER}",
        f"user_email: {_git_email(root) or '<unknown>'}",
    ]
    return Category("environment", "\n".join(lines), [], estimated=True)


def _git_email(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "config", "user.email"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    email = result.stdout.strip()
    return email or None


def assemble_categories(root: Path) -> list[Category]:
    """Assemble every source-measurable category from the worktree at ``root``."""
    return [
        assemble_rules(root),
        assemble_memory(root),
        assemble_skills(root),
        assemble_agents(root),
        assemble_environment(root),
    ]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _count(text: str, counter: TokenCounter) -> tuple[int, bool]:
    """Count tokens via ``counter``; fall back to ``len // 4`` on failure.

    Returns:
        A ``(tokens, fell_back)`` pair — ``fell_back`` is true when the estimate
        was used because the counter raised.
    """
    try:
        return counter(text), False
    except CountTokensError:
        return len(text) // 4, True


def measure_categories(
    categories: list[Category], *, counter: TokenCounter, price: float
) -> list[dict[str, object]]:
    """Measure tokens, cost, and content hash for each assembled category.

    Args:
        categories: Assembled categories to measure.
        counter: Token counter; raises :class:`CountTokensError` when unreachable.
        price: Cache-creation price in USD per token.

    Returns:
        One dict per category with keys ``name``, ``tokens``, ``cost_usd``,
        ``source_files``, ``content_hash``, ``estimated``.
    """
    rows: list[dict[str, object]] = []
    for category in categories:
        tokens, fell_back = _count(category.text, counter)
        rows.append(
            {
                "name": category.name,
                "tokens": tokens,
                "cost_usd": tokens * price,
                "source_files": category.source_files,
                "content_hash": _content_hash(category.text),
                "estimated": fell_back or category.estimated,
            }
        )
    return rows


def build_manifest(
    categories: list[Category],
    *,
    counter: TokenCounter,
    price: float,
    generated_at: str,
    cc_version: str | None,
) -> dict[str, object]:
    """Build the full context-cost manifest from assembled categories.

    Args:
        categories: Assembled categories to measure.
        counter: Token counter; raises :class:`CountTokensError` when unreachable.
        price: Cache-creation price in USD per token.
        generated_at: ISO timestamp recorded as the manifest's mint time.
        cc_version: Claude Code version string, or None if unavailable.

    Returns:
        The manifest dict ready to serialize to JSON.
    """
    rows = measure_categories(categories, counter=counter, price=price)
    return {
        "generated_at": generated_at,
        "cc_version": cc_version,
        "price_per_token": price,
        "categories": rows,
        "measured_total_tokens": sum(cast(int, row["tokens"]) for row in rows),
        "note": NOTE,
    }


def write_manifest(root: Path, manifest: dict[str, object]) -> Path:
    """Write ``manifest`` to ``<root>/.ai-toolkit/context-cost.json`` and return the path."""
    path = root / MANIFEST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def count_tokens_remote(text: str, *, endpoint: str, api_key: str, model: str) -> int:
    """Count tokens of ``text`` via the Anthropic ``count_tokens`` endpoint.

    Args:
        text: Category text sent as a single user message.
        endpoint: API base URL (no trailing path).
        api_key: Anthropic API key for the ``x-api-key`` header.
        model: Model id whose tokenizer is used.

    Returns:
        The reported ``input_tokens`` count.

    Raises:
        CountTokensError: On any transport error or a malformed response.
    """
    url = endpoint.rstrip("/") + COUNT_TOKENS_PATH
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": text or " "}]}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise CountTokensError(f"count_tokens request failed: {exc}") from exc
    tokens = data.get("input_tokens")
    if not isinstance(tokens, int):
        raise CountTokensError(f"count_tokens returned no input_tokens: {data!r}")
    return tokens


def make_counter(*, endpoint: str, api_key: str | None, model: str) -> TokenCounter:
    """Build a token counter; the counter raises :class:`CountTokensError` if uncredentialed."""

    def counter(text: str) -> int:
        if not api_key:
            raise CountTokensError("no ANTHROPIC_API_KEY configured")
        return count_tokens_remote(text, endpoint=endpoint, api_key=api_key, model=model)

    return counter


def claude_version() -> str | None:
    """Return the ``claude --version`` string, or None when unavailable."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_args(argv: list[str], env: dict[str, str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Worktree root to measure.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model id for count_tokens.")
    parser.add_argument(
        "--endpoint",
        default=env.get("ANTHROPIC_BASE_URL", DEFAULT_ENDPOINT),
        help="Anthropic API base URL.",
    )
    parser.add_argument(
        "--api-key", default=env.get("ANTHROPIC_API_KEY"), help="Anthropic API key."
    )
    parser.add_argument(
        "--price", type=float, default=DEFAULT_PRICE, help="Cache-creation USD per token."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    """Assemble, measure, and write the context-cost manifest.

    Args:
        argv: CLI arguments excluding the program name; defaults to ``sys.argv[1:]``.
        env: Environment mapping for credential defaults; defaults to ``os.environ``.

    Returns:
        Process exit code (0 on success).
    """
    resolved_env = dict(os.environ) if env is None else env
    args = _parse_args(sys.argv[1:] if argv is None else argv, resolved_env)
    root = args.root.resolve()
    counter = make_counter(endpoint=args.endpoint, api_key=args.api_key, model=args.model)
    manifest = build_manifest(
        assemble_categories(root),
        counter=counter,
        price=args.price,
        generated_at=_now_iso(),
        cc_version=claude_version(),
    )
    path = write_manifest(root, manifest)
    print(f"wrote {path} — measured_total_tokens={manifest['measured_total_tokens']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
