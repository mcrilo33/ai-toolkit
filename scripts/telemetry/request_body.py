#!/usr/bin/env python3
"""Itemize a spoke's loaded context from an untruncated raw API request body.

Claude Code's ``OTEL_LOG_RAW_API_BODIES=file:<dir>`` mode dumps each outgoing
Messages-API request verbatim to ``<dir>/<uuid>.request.json`` — the full
``tools`` array and the complete ``system`` / ``messages`` prefix, with no 60KB
inline cap. That makes every loaded-context section itemizable by name and exact
size, replacing the unreliable cache-arithmetic floor/mcp derivation.

This module parses one such file into named :class:`ContextItem` entries:

- ``tools[*]`` — one item per tool schema, named by the tool name; an
  ``mcp__server__tool`` name is split into the ``mcp`` category, every other tool
  into ``tools``. The serialized schema is the text whose tokens are measured.
- ``system[*]`` — one item per system block, labeled by position (billing header /
  identity preamble / base system prompt / tool-use + output prompt).
- ``messages[0]`` — each ``<system-reminder>`` block, classified by kind
  (session-start-hook / deferred-tools / agent-types / skills / rules+memory+env),
  plus the residual user prompt; all in the ``context`` category.

It also records every ``cache_control`` prefix boundary (read directly, not
inferred) and counts the deferred tools that are named-only in a reminder — their
schemas are NOT in ``tools``, so they are counted but never sized.

Token size is measured by :func:`measure_request_items`, reusing
``measure_context_cost``'s counter + char/4 fallback. Import-safe: no environment
or network at import; the HTTP token counter is injected by the caller.

Privacy: ``metadata.user_id`` is PII and is never read or logged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from telemetry.measure_context_cost import TokenCounter, _count

# Positional labels for the system blocks (verified anatomy: 4 blocks). Indices
# past the list fall back to a ``system[i]`` label so a longer array still itemizes.
_SYSTEM_LABELS = (
    "billing header",
    "identity preamble",
    "base system prompt",
    "tool-use + output prompt",
)

# Each ``<system-reminder>`` block is classified by the first kind whose signature
# substring appears in it; an unrecognized block falls back to ``other-reminder``.
_REMINDER_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("session-start-hook", ("SessionStart hook",)),
    ("deferred-tools", ("deferred tools are now available", "via ToolSearch")),
    ("agent-types", ("agent types for the Agent tool", "Available agent types")),
    ("skills", ("skills are available for use with the Skill tool",)),
    ("rules+memory+env", ("# claudeMd", "Memory Index", "currentDate", "CLAUDE.md")),
)

# Source label stamped on every measured row (distinguishes from the disk fallback).
_SOURCE = "request-body"

# Categories matched by a stable name/label key across turns (one entry per name within a
# snapshot). Messages are matched separately, by content hash, since their position shifts.
_NAMED_CATEGORIES = ("tools", "mcp", "system")

# A turn that drops at least this many message tokens is labeled a compaction (the large
# REMOVED the AC calls for). Tuned for real sessions, where compaction sheds tens of
# thousands of tokens at once.
# UPGRADE: switch to a ratio of the prior snapshot total if a small session ever compacts
# below this floor — when per-spoke prefix sizes vary enough that a flat floor mislabels.
COMPACTION_DROP_TOKENS = 10_000

# One ``<system-reminder>…</system-reminder>`` block (DOTALL: spans newlines).
_REMINDER_RE = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.DOTALL)
# A bare identifier — a stripped reminder line that is exactly one tool name.
_TOOL_NAME_LINE_RE = re.compile(r"[A-Za-z_]\w*")

# A section boundary inside the rules+memory+env block (#99): a ``Contents of <path>`` file
# header or one of the runtime's own env-section headers. Only these literal headers end a
# section — a generic ``# Heading`` is NOT a boundary, so a rule file's own markdown headings
# (e.g. ``# Code Quality``) stay inside that file's item instead of splitting it.
_RULES_BOUNDARY_RE = re.compile(
    r"^(?:Contents of \S+.*|# (?:claudeMd|currentDate|userEmail)\b.*)$", re.MULTILINE
)
# The path on a ``Contents of <path> …:`` header line — basenamed to name the rule item.
# The path stops at the first space or trailing colon (``file.md:`` and ``file.md (desc):``).
_CONTENTS_PATH_RE = re.compile(r"^Contents of (\S+?):?(?:\s|$)")
# One ``- <name>: …`` listing line inside the skills reminder (#99), name captured.
_SKILL_LINE_RE = re.compile(r"^- ([A-Za-z][\w:-]*):", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One named, individually-sizable loaded-context entry from the request body.

    Attributes:
        category: Section key — ``tools`` / ``mcp`` / ``system`` / ``context``.
        name: Display name (tool name, system block label, or reminder kind).
        text: The exact source text whose tokens are measured.
        cached: Whether the entry lies within the cached prefix — at or before the last
            ``cache_control`` breakpoint, which is what the prompt cache actually reuses
            (the marker closes a prefix segment; everything ahead of it is cached too).
    """

    category: str
    name: str
    text: str
    cached: bool = False


@dataclass(frozen=True, slots=True)
class ContextDelta:
    """The per-component change in loaded context between two consecutive snapshots.

    Attributes:
        added: Measured rows for items present only in the newer snapshot (a tool schema
            loaded via ToolSearch, a new message, ...). Each row carries positive ``tokens``.
        removed: Measured rows for items present only in the older snapshot (messages dropped
            by compaction, a tool unloaded). Each row's ``tokens`` is the size that left.
        changed: Measured rows for named items present in both whose text resized, each with
            extra ``prev_tokens`` and signed ``delta_tokens`` keys.
        net_tokens: ``sum(added) - sum(removed) + sum(changed deltas)`` -- the net token change,
            reconciled (approximately) against that turn's observed ``cache_creation``.
        label: ``"compaction"`` when the dropped message tokens cross
            :data:`COMPACTION_DROP_TOKENS`, else None.
    """

    added: list[dict[str, object]]
    removed: list[dict[str, object]]
    changed: list[dict[str, object]]
    net_tokens: int
    label: str | None = None


@dataclass(frozen=True, slots=True)
class CacheBoundary:
    """A ``cache_control`` marker location: the ``system`` or ``messages`` block index."""

    location: str
    index: int


@dataclass(frozen=True, slots=True)
class RequestBody:
    """The parsed loaded-context view of one ``.request.json`` dump.

    Attributes:
        items: Every itemizable loaded-context entry (tools, system, context).
        cache_boundaries: The ``cache_control`` prefix breakpoints, in order.
        deferred_tool_count: Deferred tools named-only in a reminder (counted, not sized).
        model: The request's model id, when present.
    """

    items: list[ContextItem]
    cache_boundaries: list[CacheBoundary]
    deferred_tool_count: int
    model: str | None


def _tool_items(tools: object, *, cached: bool) -> list[ContextItem]:
    """Itemize the ``tools`` array, splitting ``mcp__`` tools into the ``mcp`` category.

    Tools precede ``system`` and ``messages`` in the request, so they are inside the cached
    prefix whenever any ``cache_control`` breakpoint exists at all (``cached``).
    """
    items: list[ContextItem] = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        category = "mcp" if str(name).startswith("mcp__") else "tools"
        items.append(ContextItem(category, str(name), json.dumps(tool, ensure_ascii=False), cached))
    return items


def _block_text(block: object) -> str | None:
    """Return a content block's text whether it is a raw string or a ``{text: …}`` dict."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        text = block.get("text")
        return text if isinstance(text, str) else None
    return None


def _is_cached(block: object) -> bool:
    """Whether a content block carries a ``cache_control`` marker."""
    return isinstance(block, dict) and "cache_control" in block


def _last_marker_index(blocks: list[object]) -> int:
    """Return the index of the last ``cache_control``-marked block, or -1 if none."""
    return max((i for i, block in enumerate(blocks) if _is_cached(block)), default=-1)


def _system_items(
    system: object, *, any_later_boundary: bool
) -> tuple[list[ContextItem], list[CacheBoundary]]:
    """Itemize ``system`` blocks by positional label and collect their cache boundaries.

    A block is in the cached prefix when a later section (``messages``) holds a breakpoint
    (``any_later_boundary``) or it sits at/before this section's own last breakpoint.

    UPGRADE: labels are positional (the verified 4-block anatomy: billing header / identity
    preamble / base system prompt / tool-use + output prompt). If Claude Code reorders or
    drops a system block, every label shifts — switch to content-based detection then.
    """
    blocks = system if isinstance(system, list) else [system]
    last_marker = _last_marker_index(blocks)
    items: list[ContextItem] = []
    boundaries: list[CacheBoundary] = []
    for index, block in enumerate(blocks):
        text = _block_text(block)
        if text is None:
            continue
        name = _SYSTEM_LABELS[index] if index < len(_SYSTEM_LABELS) else f"system[{index}]"
        if _is_cached(block):
            boundaries.append(CacheBoundary("system", index))
        cached = any_later_boundary or index <= last_marker
        items.append(ContextItem("system", name, text, cached))
    return items, boundaries


def _classify_reminder(text: str) -> str:
    """Classify one reminder block by the first matching kind signature."""
    for kind, signatures in _REMINDER_SIGNATURES:
        if any(signature in text for signature in signatures):
            return kind
    return "other-reminder"


def _is_deferred_tool_line(line: str) -> bool:
    """Whether a reminder line is a deferred-tool name rather than prose.

    Deferred-tool names are bare identifiers that carry an uppercase letter or an ``mcp__``
    separator (``CronCreate``, ``WebFetch``, ``mcp__srv__tool``); requiring one excludes the
    lowercase single-word prose lines that can otherwise share the reminder's tail.
    """
    stripped = line.strip()
    if not _TOOL_NAME_LINE_RE.fullmatch(stripped):
        return False
    return any(char.isupper() for char in stripped) or "__" in stripped


def _count_deferred(text: str) -> int:
    """Count the deferred-tool name lines (one per line) listed in a deferred-tools reminder."""
    return sum(_is_deferred_tool_line(line) for line in text.splitlines())


def _message_items(
    messages: object,
) -> tuple[list[ContextItem], list[CacheBoundary], int]:
    """Itemize ``messages[0]`` into reminder blocks + the residual prompt.

    Each ``<system-reminder>`` is split out and classified; whatever text remains in a
    block after the reminders are removed is the user prompt. A block (and every reminder /
    prompt within it) is in the cached prefix when it sits at/before the last ``messages``
    breakpoint. Deferred tools named in a deferred-tools reminder are tallied (their schemas
    are absent, so never sized).
    """
    if not isinstance(messages, list) or not messages:
        return [], [], 0
    content = messages[0].get("content") if isinstance(messages[0], dict) else None
    blocks: list[object] = content if isinstance(content, list) else [content]
    last_marker = _last_marker_index(blocks)
    items: list[ContextItem] = []
    boundaries: list[CacheBoundary] = []
    deferred = 0
    for index, block in enumerate(blocks):
        text = _block_text(block)
        if text is None:
            continue
        if _is_cached(block):
            boundaries.append(CacheBoundary("messages", index))
        cached = index <= last_marker
        for full, inner in ((m.group(0), m.group(1)) for m in _REMINDER_RE.finditer(text)):
            kind = _classify_reminder(inner)
            items.append(ContextItem("context", kind, full, cached))
            if kind == "deferred-tools":
                deferred += _count_deferred(inner)
        residual = _REMINDER_RE.sub("", text).strip()
        if residual:
            items.append(ContextItem("context", "prompt", residual, cached))
    return items, boundaries, deferred


def parse_request_obj(obj: dict[str, object]) -> RequestBody:
    """Parse an already-loaded request-body dict into its itemized loaded context.

    The cached-prefix flags are resolved across sections in request order (tools → system →
    messages): messages are parsed first to learn whether a later breakpoint exists, so the
    system and tool sections ahead of it inherit the cached flag correctly.
    """
    message_items, message_boundaries, deferred = _message_items(obj.get("messages"))
    system_items, system_boundaries = _system_items(
        obj.get("system"), any_later_boundary=bool(message_boundaries)
    )
    tool_items = _tool_items(obj.get("tools"), cached=bool(system_boundaries or message_boundaries))
    model = obj.get("model")
    return RequestBody(
        items=[*tool_items, *system_items, *message_items],
        cache_boundaries=[*system_boundaries, *message_boundaries],
        deferred_tool_count=deferred,
        model=model if isinstance(model, str) else None,
    )


def parse_request_body(path: Path) -> RequestBody:
    """Parse a ``.request.json`` dump at ``path`` into its itemized loaded context."""
    return parse_request_obj(_load(path))


def _split_rules_items(text: str, *, cached: bool) -> list[ContextItem]:
    """Split a rules+memory+env block into one ``rules`` item per file plus an env item (#99).

    The block interleaves ``Contents of <path>`` file sections with ``# Header`` lines
    (claudeMd intro / Memory Index / currentDate / userEmail). Each file section becomes a
    ``rules`` item named by the path's basename; every non-file segment is concatenated into a
    single ``environment`` item. With no boundary at all the whole text is one ``environment``
    item, so no tokens are dropped.
    """
    boundaries = list(_RULES_BOUNDARY_RE.finditer(text))
    if not boundaries:
        return [ContextItem("environment", "environment", text, cached)] if text.strip() else []
    items: list[ContextItem] = []
    env_parts: list[str] = [text[: boundaries[0].start()]]
    for index, boundary in enumerate(boundaries):
        end = boundaries[index + 1].start() if index + 1 < len(boundaries) else len(text)
        segment = text[boundary.start() : end]
        match = _CONTENTS_PATH_RE.match(segment.lstrip())
        if match:
            items.append(ContextItem("rules", Path(match.group(1)).name, segment, cached))
        else:
            env_parts.append(segment)
    if any(part.strip() for part in env_parts):
        items.append(ContextItem("environment", "environment", "".join(env_parts), cached))
    return items


def _split_skill_items(text: str, *, cached: bool) -> list[ContextItem]:
    """Split a skills reminder into one ``skills`` item per ``- <name>: …`` line (#99).

    Each skill's item text runs from its listing line up to the next skill (the leading prose
    header before the first skill is left out and falls into the reconciled remainder). With no
    listing line the whole text is one ``skills`` item, so nothing is dropped.
    """
    matches = list(_SKILL_LINE_RE.finditer(text))
    if not matches:
        return [ContextItem("skills", "skills", text, cached)] if text.strip() else []
    items: list[ContextItem] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        items.append(ContextItem("skills", match.group(1), text[match.start() : end], cached))
    return items


def _last_marker_message_index(messages: list[object]) -> int:
    """Return the highest message index carrying any ``cache_control`` block, or -1 if none."""
    last = -1
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks: list[object] = content if isinstance(content, list) else [content]
        if any(_is_cached(block) for block in blocks):
            last = index
    return last


def _decompose_first_message(first: object, *, any_later_marker: bool) -> list[ContextItem]:
    """Itemize ``messages[0]`` for the decomposition: sub-split reminders + residual prompt.

    Reuses the #87 reminder classification but routes the rules+memory+env and skills kinds
    through the per-file / per-skill splitters; any ``Contents of <path>`` text outside a
    recognized reminder (rules injected as bare blocks) is split too.
    """
    content = first.get("content") if isinstance(first, dict) else None
    blocks: list[object] = content if isinstance(content, list) else [content]
    last_marker = _last_marker_index(blocks)
    items: list[ContextItem] = []
    for index, block in enumerate(blocks):
        text = _block_text(block)
        if text is None:
            continue
        cached = any_later_marker or index <= last_marker
        for full, inner in ((m.group(0), m.group(1)) for m in _REMINDER_RE.finditer(text)):
            items.extend(_decompose_reminder(full, inner, cached=cached))
        residual = _REMINDER_RE.sub("", text).strip()
        if residual:
            items.extend(_decompose_residual(residual, cached=cached))
    return items


def _decompose_reminder(full: str, inner: str, *, cached: bool) -> list[ContextItem]:
    """Route one reminder to the per-file / per-skill splitter or keep it as a context item."""
    kind = _classify_reminder(inner)
    if kind == "rules+memory+env":
        return _split_rules_items(inner, cached=cached)
    if kind == "skills":
        return _split_skill_items(inner, cached=cached)
    return [ContextItem("context", kind, full, cached)]


def _decompose_residual(residual: str, *, cached: bool) -> list[ContextItem]:
    """Itemize non-reminder text: split any ``Contents of`` rule blocks, else the prompt."""
    if _CONTENTS_PATH_RE.search(residual) or _RULES_BOUNDARY_RE.search(residual):
        return _split_rules_items(residual, cached=cached)
    return [ContextItem("context", "prompt", residual, cached)]


def decompose_request_obj(obj: dict[str, object]) -> list[ContextItem]:
    """Itemize a whole request body for the per-``llm_request`` cache decomposition (#99).

    Unlike :func:`parse_request_obj` (the #87 turn-0 baseline, ``messages[0]`` only) this
    itemizes the ENTIRE body in request order — tools, system, ``messages[0]`` with the
    rules+memory+env block split per rule file and the skills block split per skill, then every
    later message whole — each :class:`ContextItem` carrying a ``cached`` flag. A message at or
    before the last ``cache_control`` marker is cached (read from cache); a message after it is
    new (written this turn). The caller splits these items into the observed ``cache_read`` /
    ``cache_creation`` token budgets.

    Args:
        obj: An already-loaded request-body dict.

    Returns:
        The itemized loaded context, in request order, with per-item cached flags.
    """
    messages = obj.get("messages")
    messages = messages if isinstance(messages, list) else []
    last_marker_msg = _last_marker_message_index(messages)
    any_msg_marker = last_marker_msg >= 0
    system_items, system_boundaries = _system_items(
        obj.get("system"), any_later_boundary=any_msg_marker
    )
    tool_items = _tool_items(obj.get("tools"), cached=bool(system_boundaries) or any_msg_marker)
    first_items = (
        _decompose_first_message(messages[0], any_later_marker=last_marker_msg > 0)
        if messages
        else []
    )
    return [
        *tool_items,
        *system_items,
        *first_items,
        *_later_message_items(messages, last_marker_msg),
    ]


def _later_message_items(messages: list[object], last_marker_msg: int) -> list[ContextItem]:
    """Itemize ``messages[1:]`` whole, cached iff at or before the last marker message."""
    items: list[ContextItem] = []
    for index in range(1, len(messages)):
        message = messages[index]
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "?"))
        text = json.dumps(
            {"role": role, "content": message.get("content")}, ensure_ascii=False, sort_keys=True
        )
        items.append(
            ContextItem("messages", f"msg[{index}]:{role}", text, index <= last_marker_msg)
        )
    return items


def decompose_request_body(path: Path) -> list[ContextItem]:
    """Itemize a ``.request.json`` dump at ``path`` for the cache decomposition (see above)."""
    return decompose_request_obj(_load(path))


def snapshot_items_from_path(path: Path) -> list[ContextItem]:
    """Itemize a ``.request.json`` dump at ``path`` for diffing (see :func:`snapshot_items`)."""
    return snapshot_items(_load(path))


def _load(path: Path) -> dict[str, object]:
    """Load and return the JSON object at ``path``."""
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def measure_request_items(
    items: list[ContextItem], *, counter: TokenCounter, price: float
) -> list[dict[str, object]]:
    """Measure tokens and cost for each loaded-context item.

    Args:
        items: Parsed items (see :func:`parse_request_obj`).
        counter: Token counter; raises ``CountTokensError`` when unreachable, which
            triggers the ``len(text) // 4`` fallback (the row is flagged ``estimated``).
        price: Cache-creation price in USD per token.

    Returns:
        One dict per item with keys ``category``, ``name``, ``tokens``, ``cost_usd``,
        ``source``, ``cached``, ``estimated``.
    """
    rows: list[dict[str, object]] = []
    for item in items:
        tokens, fell_back = _count(item.text, counter)
        rows.append(
            {
                "category": item.category,
                "name": item.name,
                "tokens": tokens,
                "cost_usd": tokens * price,
                "source": _SOURCE,
                "cached": item.cached,
                "estimated": fell_back,
            }
        )
    return rows


def _full_message_items(messages: object) -> list[ContextItem]:
    """Itemize the WHOLE ``messages`` array, one :class:`ContextItem` per message.

    Unlike :func:`_message_items` (which only splits ``messages[0]`` into reminder kinds for
    the #87 turn-0 baseline), this captures every turn so the differ can see messages added
    and dropped. Each item's ``text`` is the canonical ``{role, content}`` JSON — its content
    hash, not its position, is the matching identity (so a compaction that shifts indices does
    not churn the survivors). The name carries the index purely for display.
    """
    if not isinstance(messages, list):
        return []
    items: list[ContextItem] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "?"))
        text = json.dumps(
            {"role": role, "content": message.get("content")}, ensure_ascii=False, sort_keys=True
        )
        items.append(ContextItem("messages", f"msg[{index}]:{role}", text))
    return items


def snapshot_items(obj: dict[str, object]) -> list[ContextItem]:
    """Itemize one request body for diffing: tools + system + every message.

    Reuses the #87 :func:`_tool_items` / :func:`_system_items` itemizers and adds full
    per-message items (see :func:`_full_message_items`). The cached-prefix flags are
    irrelevant to a diff, so they are left False; :func:`parse_request_obj` (the #87 turn-0
    baseline path) is untouched.
    """
    tools = _tool_items(obj.get("tools"), cached=False)
    system, _ = _system_items(obj.get("system"), any_later_boundary=False)
    return [*tools, *system, *_full_message_items(obj.get("messages"))]


def _content_hash(text: str) -> str:
    """Return a stable hash of an item's text — the content identity used to match messages."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _measure_one(item: ContextItem, *, counter: TokenCounter, price: float) -> dict[str, object]:
    """Measure one item into a row, reusing :func:`measure_request_items`'s row shape."""
    return measure_request_items([item], counter=counter, price=price)[0]


def _named_index(items: list[ContextItem]) -> dict[tuple[str, str], ContextItem]:
    """Index the name-matched categories (tools / mcp / system) by ``(category, name)``."""
    return {
        (item.category, item.name): item for item in items if item.category in _NAMED_CATEGORIES
    }


def _changed_row(
    prev: ContextItem, curr: ContextItem, *, counter: TokenCounter, price: float
) -> dict[str, object]:
    """Build a size-change row for a named item, adding its prior size and signed delta."""
    row = _measure_one(curr, counter=counter, price=price)
    prev_tokens, _ = _count(prev.text, counter)
    row["prev_tokens"] = prev_tokens
    row["delta_tokens"] = int(row["tokens"]) - prev_tokens  # type: ignore[arg-type]
    return row


def _diff_named(
    prev_items: list[ContextItem],
    curr_items: list[ContextItem],
    *,
    counter: TokenCounter,
    price: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Diff the name-matched categories into ``(added, removed, changed)`` rows."""
    prev, curr = _named_index(prev_items), _named_index(curr_items)
    added = [
        _measure_one(item, counter=counter, price=price)
        for key, item in curr.items()
        if key not in prev
    ]
    removed = [
        _measure_one(item, counter=counter, price=price)
        for key, item in prev.items()
        if key not in curr
    ]
    changed = [
        _changed_row(prev[key], item, counter=counter, price=price)
        for key, item in curr.items()
        if key in prev and prev[key].text != item.text
    ]
    return added, removed, changed


def _group_messages_by_hash(items: list[ContextItem]) -> dict[str, list[ContextItem]]:
    """Group message items by content hash, preserving order within each hash bucket."""
    groups: dict[str, list[ContextItem]] = defaultdict(list)
    for item in items:
        if item.category == "messages":
            groups[_content_hash(item.text)].append(item)
    return groups


def _diff_messages(
    prev_items: list[ContextItem],
    curr_items: list[ContextItem],
    *,
    counter: TokenCounter,
    price: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Diff messages by content-hash multiset into ``(added, removed)`` rows.

    A message is matched by the hash of its ``{role, content}`` text, not its index, so the
    survivors of a compaction (now at shifted positions) are not re-churned. Surplus copies on
    the newer side are ADDED; surplus on the older side are REMOVED. Messages never size-change
    (the API conversation is append-only bar compaction), so there is no changed bucket.
    """
    prev = _group_messages_by_hash(prev_items)
    curr = _group_messages_by_hash(curr_items)
    added: list[dict[str, object]] = []
    for digest, items in curr.items():
        surplus = max(0, len(items) - len(prev.get(digest, [])))
        added.extend(_measure_one(item, counter=counter, price=price) for item in items[:surplus])
    removed: list[dict[str, object]] = []
    for digest, items in prev.items():
        surplus = max(0, len(items) - len(curr.get(digest, [])))
        removed.extend(_measure_one(item, counter=counter, price=price) for item in items[:surplus])
    return added, removed


def _turn_label(removed: list[dict[str, object]]) -> str | None:
    """Label a turn ``"compaction"`` when its dropped message tokens cross the threshold."""
    dropped = sum(int(row["tokens"]) for row in removed if row["category"] == "messages")  # type: ignore[arg-type]
    return "compaction" if dropped >= COMPACTION_DROP_TOKENS else None


def diff_snapshots(
    prev_items: list[ContextItem],
    curr_items: list[ContextItem],
    *,
    counter: TokenCounter,
    price: float,
) -> ContextDelta:
    """Diff two consecutive snapshots into per-component added / removed / size-changed rows.

    Named items (tools / mcp / system) are matched by ``(category, name)`` — appearing only in
    the newer snapshot is ADDED, only in the older is REMOVED, and a text resize at the same
    key is SIZE-CHANGED with a signed ``delta_tokens``. Messages are matched by content hash
    (see :func:`_diff_messages`). ``net_tokens`` sums the three buckets and the turn is labeled
    a compaction when the dropped message tokens cross :data:`COMPACTION_DROP_TOKENS`.

    Args:
        prev_items: The older snapshot's items (see :func:`snapshot_items`).
        curr_items: The newer snapshot's items.
        counter: Token counter; raises ``CountTokensError`` to trigger the char/4 fallback.
        price: Cache-creation price in USD per token.

    Returns:
        The :class:`ContextDelta` for the transition.
    """
    added, removed, changed = _diff_named(prev_items, curr_items, counter=counter, price=price)
    msg_added, msg_removed = _diff_messages(prev_items, curr_items, counter=counter, price=price)
    added.extend(msg_added)
    removed.extend(msg_removed)
    net = (
        sum(int(row["tokens"]) for row in added)  # type: ignore[arg-type]
        - sum(int(row["tokens"]) for row in removed)  # type: ignore[arg-type]
        + sum(int(row["delta_tokens"]) for row in changed)  # type: ignore[arg-type]
    )
    return ContextDelta(added, removed, changed, net, _turn_label(removed))


def first_real_request(paths: list[Path]) -> Path | None:
    """Return the first request file with a non-empty ``tools`` array, else None.

    The first ``llm_request`` of a session can be a degenerate aux call carrying a tiny
    prefix and no tools; it is skipped so the itemization reflects the full prefix.

    Args:
        paths: Candidate ``.request.json`` paths, oldest first (the caller sorts by mtime).

    Returns:
        The first path whose body has a non-empty ``tools`` array, or None when none do
        (the caller then falls back to disk measurement).
    """
    for path in paths:
        try:
            obj = _load(path)
        except (OSError, json.JSONDecodeError):
            continue
        tools = obj.get("tools")
        if isinstance(tools, list) and tools:
            return Path(path)
    return None
