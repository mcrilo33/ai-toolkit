"""Cached, pluggable one-line summarizer for context items (Issue #68).

Gives each rule / skill / reasoning chunk a one-line "what this is", keyed by a
hash of ``(model, content)`` and cached in a JSON sidecar so identical content
is summarized exactly once — on re-open and across every spoke that loads it.

Design goals (from the issue):

- **Pluggable & cheap** — the LLM backend is a swappable ``complete(content,
  model) -> str`` callable; the default talks to any OpenAI-compatible chat
  endpoint over stdlib ``urllib`` (no new dependency), defaulting to DeepSeek so
  summaries work out of the box, and the model is configurable (env
  ``TELEMETRY_SUMMARY_MODEL``, default ``deepseek-v4-flash``).
- **Cache by content hash** — never recompute for the same content.
- **Fail-soft** — every failure path (backend error, misconfiguration, empty
  content) yields a blank summary; a blank never breaks the view and is never
  cached, so a transient failure is retried next time.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.request
from collections.abc import Callable
from pathlib import Path

CompleteFn = Callable[[str, str], str]

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
_MODEL_ENV = "TELEMETRY_SUMMARY_MODEL"
_BASE_URL_ENV = "TELEMETRY_SUMMARY_BASE_URL"
_API_KEY_ENV = "TELEMETRY_SUMMARY_API_KEY"
_API_KEY_FALLBACK_ENV = "DEEPSEEK_API_KEY"
_KEYCHAIN_SERVICE = "DEEPSEEK_API_KEY"

# Keep cost bounded: a one-liner needs the gist, not the whole document.
_MAX_CONTENT_CHARS = 6000
_REQUEST_TIMEOUT_S = 20

_SYSTEM_PROMPT = (
    "You label developer-context items for a dashboard. Given the content of a "
    "rule, skill, or reasoning trace, reply with a single terse line (max ~12 "
    "words) describing what it is. No quotes, no trailing period, no preamble."
)


def resolve_model(explicit: str | None = None) -> str:
    """The summary model: explicit arg, else ``TELEMETRY_SUMMARY_MODEL``, else default."""
    return explicit or os.environ.get(_MODEL_ENV) or DEFAULT_MODEL


def content_key(content: str, model: str) -> str:
    """Stable cache key for a ``(model, content)`` pair.

    Model-scoped so switching models doesn't serve summaries from another model.
    """
    digest = hashlib.sha1(f"{model}\n{content}".encode())
    return digest.hexdigest()


class SummaryCache:
    """A content-hash → summary cache persisted as a JSON sidecar.

    A standalone sidecar so summaries survive re-ingestion of the span data and
    are shared across spokes that point at the same telemetry dir. Reads are lazy and
    writes are immediate (write-through), so a crash never loses a computed line.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._entries: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._entries is not None:
            return self._entries
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._entries = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            # Missing or corrupt sidecar — start clean rather than break the view.
            self._entries = {}
        return self._entries

    def get(self, key: str) -> str | None:
        """The cached summary for ``key``, or ``None`` on a miss."""
        return self._load().get(key)

    def set(self, key: str, summary: str) -> None:
        """Store ``summary`` under ``key`` and write the sidecar through to disk."""
        entries = self._load()
        entries[key] = summary
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        except OSError:
            # An unwritable sidecar must not break summarization — keep the
            # in-memory entry so this session still gets cache hits.
            pass


def summarize(
    content: str,
    *,
    model: str | None = None,
    cache: SummaryCache | None = None,
    complete: CompleteFn | None = None,
) -> str:
    """Return a one-line summary of ``content``, cached by content hash.

    Args:
        content: The text to summarize (rule body, skill body, reasoning gist).
        model: Model override; falls back to env then the cheap default.
        cache: Content-hash cache. On a hit the backend is never called.
        complete: LLM backend ``(content, model) -> str``; defaults to an
            OpenAI-compatible HTTP call.

    Returns:
        The one-line summary, or ``""`` on any failure / empty input. A blank
        result is never cached, so a transient failure is retried next time.
    """
    if not content or not content.strip():
        return ""

    resolved_model = resolve_model(model)
    key = content_key(content, resolved_model)

    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit

    backend = complete or _default_complete
    try:
        summary = (backend(content[:_MAX_CONTENT_CHARS], resolved_model) or "").strip()
    except Exception:
        return ""

    if summary and cache is not None:
        cache.set(key, summary)
    return summary


def resolve_content(
    kind: str,
    identifier: str,
    *,
    rules_dir: Path | None = None,
    skills_dir: Path | None = None,
) -> str:
    """The text to summarize for a context item, by kind.

    - ``rule`` → the rule markdown body at ``rules_dir/<identifier>.md``.
    - ``skill`` → the skill body at ``skills_dir/<identifier>/SKILL.md``.
    - ``reasoning`` → ``identifier`` *is* the privacy-safe gist (no file to read).

    Returns ``""`` when the kind is unknown, the directory is unset, or the file is
    missing/unreadable — the summarizer then yields a blank, never an error.
    """
    if kind == "reasoning":
        return identifier or ""
    if kind == "rule" and rules_dir is not None:
        return _read_text(Path(rules_dir) / f"{identifier}.md")
    if kind == "skill" and skills_dir is not None:
        return _read_text(Path(skills_dir) / identifier / "SKILL.md")
    return ""


def context_summary(
    kind: str,
    identifier: str,
    *,
    cache: SummaryCache | None = None,
    rules_dir: Path | None = None,
    skills_dir: Path | None = None,
    model: str | None = None,
    complete: CompleteFn | None = None,
) -> str:
    """Resolve a context item's content and return its cached one-line summary.

    The single entry point the dashboard calls per rule/skill/reasoning node: it
    locates the content (:func:`resolve_content`) then summarizes it
    (:func:`summarize`). Unresolvable content yields a blank without any LLM call.
    """
    content = resolve_content(kind, identifier, rules_dir=rules_dir, skills_dir=skills_dir)
    return summarize(content, model=model, cache=cache, complete=complete)


def _read_text(path: Path) -> str:
    """The file's text, or ``""`` if it is missing or unreadable (fail-soft)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _resolve_api_key() -> str:
    """The DeepSeek API key from env then the macOS Keychain, or ``""`` if absent.

    Resolution order: ``TELEMETRY_SUMMARY_API_KEY`` → ``DEEPSEEK_API_KEY`` →
    ``security find-generic-password`` (macOS Keychain). Any miss or lookup
    failure yields ``""`` so the dashboard simply shows no summaries.
    """
    for env_name in (_API_KEY_ENV, _API_KEY_FALLBACK_ENV):
        key = os.environ.get(env_name, "").strip()
        if key:
            return key
    return _keychain_api_key()


def _keychain_api_key() -> str:
    """The key stored under ``DEEPSEEK_API_KEY`` in the macOS Keychain, or ``""``."""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                os.environ.get("USER", ""),
                "-s",
                _KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _default_complete(content: str, model: str) -> str:
    """OpenAI-compatible chat-completion backend over stdlib ``urllib``.

    Talks to ``TELEMETRY_SUMMARY_BASE_URL`` (default DeepSeek), authenticating
    with the key from :func:`_resolve_api_key`. ``thinking`` is disabled so a
    reasoning model spends its token budget on the answer, not hidden reasoning
    (which otherwise blanks ``message.content``). Returns ``""`` when no key is
    available — no key, no summaries, never an error.
    """
    api_key = _resolve_api_key()
    if not api_key:
        return ""

    base_url = os.environ.get(_BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL

    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "max_tokens": 40,
            "temperature": 0.0,
            "thinking": {"type": "disabled"},
        }
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    url = f"{base_url.rstrip('/')}/chat/completions"
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]
