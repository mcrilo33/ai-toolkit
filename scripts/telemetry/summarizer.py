"""Cached, pluggable one-line summarizer for context items (Issue #68).

Gives each rule / skill / reasoning chunk a one-line "what this is", keyed by a
hash of ``(model, content)`` and cached in a JSON sidecar so identical content
is summarized exactly once — on re-open and across every spoke that loads it.

Design goals (from the issue):

- **Pluggable & cheap** — the LLM backend is a swappable ``complete(content,
  model) -> str`` callable; the default talks to any OpenAI-compatible chat
  endpoint over stdlib ``urllib`` (no new dependency), and the model is
  configurable (env ``TELEMETRY_SUMMARY_MODEL``, default ``deepseek-flash``).
- **Cache by content hash** — never recompute for the same content.
- **Fail-soft** — every failure path (backend error, misconfiguration, empty
  content) yields a blank summary; a blank never breaks the view and is never
  cached, so a transient failure is retried next time.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from collections.abc import Callable
from pathlib import Path

CompleteFn = Callable[[str, str], str]

DEFAULT_MODEL = "deepseek-flash"
_MODEL_ENV = "TELEMETRY_SUMMARY_MODEL"
_BASE_URL_ENV = "TELEMETRY_SUMMARY_BASE_URL"
_API_KEY_ENV = "TELEMETRY_SUMMARY_API_KEY"

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

    Decoupled from the DuckDB store so summaries survive store rebuilds and are
    shared across spokes that point at the same telemetry dir. Reads are lazy and
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


def _default_complete(content: str, model: str) -> str:
    """OpenAI-compatible chat-completion backend over stdlib ``urllib``.

    Reads the endpoint from ``TELEMETRY_SUMMARY_BASE_URL`` (e.g. a local LiteLLM
    proxy) and an optional ``TELEMETRY_SUMMARY_API_KEY``. Returns ``""`` when no
    endpoint is configured so an unconfigured dashboard simply shows no summaries
    rather than erroring.
    """
    base_url = os.environ.get(_BASE_URL_ENV, "").strip()
    if not base_url:
        return ""

    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "max_tokens": 40,
            "temperature": 0.0,
        }
    ).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{base_url.rstrip('/')}/chat/completions"
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]
