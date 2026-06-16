"""Parse Claude session-log JSONL into pull spans (skill/agent/todo/human/rule).

Reads ``~/.claude/projects/<slug>/<session>.jsonl`` transcripts and reconstructs
the spans the runtime cannot emit at hook time. For ``agent`` spans it walks the
matching ``<session>/subagents/agent-<id>.jsonl`` transcript so the subagent's
own token usage can be attributed to the parent agent span by the correlation
pass.

Issue #51 (track B) extends the pull layer: ``Workflow`` fan-out agents are
discovered at ``<session>/subagents/workflows/wf_*/agent-<id>.jsonl``; nested
Task agents recurse so agent→agent chains reconstruct at any depth (walked once
per agentId via a session-global guard); a ledger-creation ``TodoWrite`` gets a
lead-item label; extended-thinking blocks surface privacy-safe ``reasoning_refs``;
a ``claude -p`` Bash links a ``sidecar_session``; and loaded context becomes
``rule`` spans tagged by ``phase`` subtype.

Issue #59 fixes the loaded-context source: #51 read it from ``Contents of <path>``
system-reminder headers that exist only in the hand-built fixture, so the surface
measured zero on every real spoke. Real transcripts deliver loaded context as
``attachment`` records — ``nested_memory`` (rules / CLAUDE.md / memory) and
``deferred_tools_delta`` (tool schemas) — so each item becomes a ``rule``-kind span
whose ``phase`` names its subtype (``rule`` / ``CLAUDE.md`` / ``memory`` /
``tool-schema``), carrying a per-item token estimate but never the file body.

Privacy: metadata plus short *intent* labels are read out of a record — tool
name, skill name, subagent type, timestamps, token counts, the ``agentId`` link,
and (Issue #47) a few-word ``summary`` per node: the in-progress todo item, an
agent's task ``description``, a human prompt/question's first line, and a tool's
single main parameter (Bash command, file path, Grep pattern). Bulk/long-form
content — thinking, an agent's full prompt, a tool's secondary input (replacement
text, file content) and its output, human answers, and rule/CLAUDE.md/memory file
bodies — is never copied.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from telemetry.spans import Span, derive_span_id

# UPGRADE: this module crossed the 800-line file limit at #51 S4 — decompose the
# sub-agent/workflow walk family (_walk_subagent/_walk_transcript/_link_and_walk_agent/
# _walk_workflow_agents and the workflow helpers) into a telemetry/agent_walk module
# once the parser stabilises; deferred here to avoid a cross-module refactor mid-spoke.

AGENT_TOOLS = frozenset({"Task", "Agent"})
TODO_TOOLS = frozenset({"TodoWrite", "TaskCreate", "TaskUpdate"})

# The one input key naming what a tool acted on — surfaced as the tool leaf's
# summary so the trace reads "what over what" (Issue #47). Only this main
# identifying parameter is read; bulk/secondary fields (Edit's replacement text,
# Write's content, Read's output) are never copied into a span.
TOOL_MAIN_PARAM: dict[str, str] = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Grep": "pattern",
    "Glob": "pattern",
    "LS": "path",
    "WebFetch": "url",
    "WebSearch": "query",
}
# Conservative fallback for tools absent from the map: the first present target
# key. Deliberately excludes free-form content keys (prompt/content/old_string).
_GENERIC_PARAM_KEYS = ("file_path", "path", "command", "pattern", "query", "url")

# A Bash command that launches a headless ``claude -p`` session, and the session id
# it targets — the sidecar link (Issue #51 S3). Matched only when the print/``-p``
# flag is present; an interactive ``claude`` is not a sidecar.
_SIDECAR_PRINT_RE = re.compile(r"(?:^|\s)(?:-p|--print)(?:\s|=|$)")
# Both flag and id forms are anchored on a left word-boundary so a short ``-r`` never
# matches the tail of an unrelated token (``--foo-r``, an earlier ``grep -r``).
_SIDECAR_ID_RE = re.compile(r"(?:^|\s)(?:--session-id|--resume|-r)[=\s]+([A-Za-z0-9._-]+)")

# Loaded context arrives as ``attachment`` records (Issue #59). A ``nested_memory``
# attachment carries one rule / CLAUDE.md / memory file; a ``deferred_tools_delta``
# the tool schemas made available. Only the path/name and a body-size token estimate
# are read — never the file body.
_NESTED_MEMORY = "nested_memory"
_DEFERRED_TOOLS = "deferred_tools_delta"
# ~4 characters per token — a rough size estimate so each loaded item is drillable to
# its per-item context cost without a token-counting API.
_CHARS_PER_TOKEN = 4


@dataclass(slots=True)
class UsageEvent:
    """One assistant turn's ``message.usage``, tagged with its source transcript."""

    session_id: str | None
    ts: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_creation: int
    source: str  # "main" | "subagent"
    agent_id: str | None = None


@dataclass(slots=True)
class ReasoningRef:
    """A privacy-safe per-turn reasoning summary (Issues #51 S3, #59).

    Carries a transcript-link locator (``<session-or-agent-id>#<record-uuid>``),
    timing, and a ``summary`` gist — the first line of the turn's *visible* narration
    text. The redacted extended-thinking body is never read (real thinking is
    signature-only); the dashboard renders a synthetic ``reasoning`` node from these.
    """

    session_id: str | None
    source: str  # "main" | "subagent"
    agent_id: str | None
    ts: str | None
    ref: str
    summary: str | None = None


@dataclass(slots=True)
class ParsedSession:
    """Parser output: spans plus the raw material the correlation pass needs."""

    spans: list[Span] = field(default_factory=list)
    usage_events: list[UsageEvent] = field(default_factory=list)
    agent_links: dict[str, str] = field(default_factory=dict)  # agent span_id -> agentId
    reasoning_refs: list[ReasoningRef] = field(default_factory=list)


def parse_session_file(path: Path) -> ParsedSession:
    """Parse one main session transcript into a :class:`ParsedSession`."""
    records = _load_jsonl(path)
    meta = _session_meta(records)
    results = _tool_results(records)
    todo_summaries = _todo_summaries(records)

    parsed = ParsedSession()
    # One session-global set of every agentId whose transcript has been walked, so a
    # repeated or cyclic agentId (reachable via the recursion in #51 S2) is walked —
    # and its usage emitted — at most once across all top-level and workflow agents.
    seen: set[str] = set()
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        if isinstance(message.get("usage"), dict):
            parsed.usage_events.append(_usage_event(rec, "main", None))
        for block in message.get("content") or []:
            _consume_tool_use(block, rec, results, meta, path, parsed, todo_summaries, seen)

    parsed.spans.extend(_human_prompt_spans(records, meta))
    parsed.reasoning_refs.extend(_reasoning_refs(records, path.stem, "main", None, meta))
    parsed.spans.extend(_context_and_rule_loads(records, meta))

    events, spans, links = _walk_workflow_agents(path, parent_meta=meta, seen=seen)
    parsed.usage_events.extend(events)
    parsed.spans.extend(spans)
    parsed.agent_links.update(links)
    return parsed


def parse_projects_dir(root: Path) -> ParsedSession:
    """Parse every ``<slug>/<session>.jsonl`` under a projects root and merge.

    Subagent transcripts live one level deeper (``<session>/subagents/``), so the
    ``*/*.jsonl`` glob never picks them up as top-level sessions — they are only
    reached via the parent agent span's walk.
    """
    merged = ParsedSession()
    for path in sorted(Path(root).glob("*/*.jsonl")):
        if "subagents" in path.parts:
            continue
        parsed = parse_session_file(path)
        merged.spans.extend(parsed.spans)
        merged.usage_events.extend(parsed.usage_events)
        merged.agent_links.update(parsed.agent_links)
        merged.reasoning_refs.extend(parsed.reasoning_refs)
    return merged


def _consume_tool_use(
    block: object,
    rec: dict,
    results: dict[str, dict],
    meta: dict[str, str | None],
    path: Path,
    parsed: ParsedSession,
    todo_summaries: dict[str, str],
    seen: set[str],
) -> None:
    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
        return
    span = _span_for_tool_use(block, rec, results, meta, todo_summaries)
    if span is None:
        return
    parsed.spans.append(span)
    if span.kind != "agent":
        return
    events, sub_spans, links = _link_and_walk_agent(
        span, block.get("id") or "", results, path, meta, seen
    )
    parsed.usage_events.extend(events)
    parsed.spans.extend(sub_spans)
    parsed.agent_links.update(links)


def _span_for_tool_use(
    block: dict,
    rec: dict,
    results: dict[str, dict],
    meta: dict[str, str | None],
    todo_summaries: dict[str, str],
) -> Span | None:
    name = block.get("name")
    tool_use_id = block.get("id") or ""
    result = results.get(tool_use_id, {})
    ts_start = rec.get("timestamp")
    ts_end = result.get("ts") or ts_start
    inputs = block.get("input") or {}
    common = {
        "span_id": derive_span_id(meta["session_id"] or "", tool_use_id),
        "session_id": meta["session_id"],
        "repo": meta["repo"] or "unknown",
        "branch": meta["branch"],
        "ts_start": ts_start,
        "ts_end": ts_end,
        "duration_ms": _duration_ms(ts_start, ts_end),
        "status": "failure" if result.get("is_error") else "success",
    }
    if name == "Skill":
        return Span(kind="skill", name=inputs.get("skill", "skill"), **common)
    if name in AGENT_TOOLS:
        # summary = the Task tool's short `description`; the long `prompt` is never read.
        return Span(
            kind="agent",
            name=inputs.get("subagent_type", "agent"),
            summary=_snippet(inputs.get("description")),
            **common,
        )
    if name in TODO_TOOLS:
        # name stays the bare tool (grouping key); summary names the ledger item.
        return Span(
            kind="todo",
            name=name if isinstance(name, str) else "todo",
            summary=todo_summaries.get(tool_use_id),
            **common,
        )
    if name == "AskUserQuestion":
        human = {"type": "question", "wait_ms": _duration_ms(ts_start, ts_end)}
        return Span(
            kind="human",
            name="AskUserQuestion",
            summary=_question_snippet(inputs),
            human=human,
            **common,
        )
    # Every other tool_use is a leaf span (Issue #47 S2b) summarising what it acted
    # on — the tool's MAIN identifying parameter only (Bash command, file path, Grep
    # pattern). Bulk/secondary input (replacement text, file content) is never read.
    # A Bash that shells out to a headless ``claude -p`` session is linked via
    # ``sidecar_session`` (Issue #51 S3) so that session's cost can be attributed here.
    return Span(
        kind="tool",
        name=name if isinstance(name, str) else "tool",
        summary=_tool_param(name, inputs),
        sidecar_session=_sidecar_session(name, inputs),
        **common,
    )


def _todo_summaries(records: list[dict]) -> dict[str, str]:
    """Map each todo ``tool_use`` id to the few-word item it advances (Issue #47).

    Two ledger shapes are resolved into one summary per write:

    - ``TodoWrite`` snapshots: diff each write's in-progress set against the
      previous one to isolate the item that *newly* entered progress — the step's
      todo. With nothing in progress, a *ledger-creation* write (Issue #51 S3) —
      one that introduces a brand-new item — falls back to its lead new item, so a
      seed write reads as a real step rather than a bare ``todo``; a write with
      nothing new and nothing in progress still yields no summary.
    - ``TaskCreate`` / ``TaskUpdate`` (incremental, id-keyed): a ``TaskCreate``
      summarises to its ``subject`` and is assigned the next sequential id (the
      runtime numbers them 1, 2, … in creation order); a later ``TaskUpdate``
      resolves its ``taskId`` back to that subject.
    """
    summaries: dict[str, str] = {}
    prev: set[str] = set()
    seen_contents: set[str] = set()
    subject_by_id: dict[str, str] = {}
    created = 0
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            tool = block.get("name")
            if tool not in TODO_TOOLS:
                continue
            inputs = block.get("input") or {}
            tool_use_id = block.get("id") or ""
            if tool == "TodoWrite":
                summary, prev, seen_contents = _derive_todo_summary(inputs, prev, seen_contents)
            elif tool == "TaskCreate":
                created += 1
                summary = _snippet(inputs.get("subject"))
                if summary:
                    subject_by_id[str(created)] = summary
            else:  # TaskUpdate
                summary = subject_by_id.get(str(inputs.get("taskId")))
            if summary:
                summaries[tool_use_id] = summary
    return summaries


def _derive_todo_summary(
    inputs: dict, prev: set[str], seen: set[str]
) -> tuple[str | None, set[str], set[str]]:
    """The label for one ``TodoWrite`` snapshot, plus its in-progress + seen sets.

    Prefers the item that newly entered progress (the step's todo), else any
    in-progress item; with nothing in progress, falls back to the lead item this
    write *creates* (a ledger-creation label), else no summary.
    """
    items = inputs.get("todos")
    if not isinstance(items, list):
        return None, prev, seen
    contents = [item["content"] for item in items if isinstance(item, dict) and item.get("content")]
    in_progress = {
        item["content"]
        for item in items
        if isinstance(item, dict) and item.get("status") == "in_progress" and item.get("content")
    }
    newly = sorted(in_progress - prev)
    if newly:
        summary = newly[0]
    elif in_progress:
        summary = sorted(in_progress)[0]
    else:
        created = [content for content in contents if content not in seen]
        summary = created[0] if created else None
    return _snippet(summary), in_progress, seen | set(contents)


def _tool_param(name: object, inputs: dict) -> str | None:
    """The main identifying parameter of a tool call, as a node-label summary.

    Uses the per-tool key in :data:`TOOL_MAIN_PARAM`, else the first present
    generic target key. Only this one key is read — bulk/secondary fields never
    are — so the trace shows what a tool acted on without leaking its payload.
    """
    key = TOOL_MAIN_PARAM.get(name) if isinstance(name, str) else None
    if key is None:
        key = next((k for k in _GENERIC_PARAM_KEYS if inputs.get(k)), None)
    return _snippet(inputs.get(key)) if key else None


def _sidecar_session(name: object, inputs: dict) -> str | None:
    """The session id of a Bash that shells out to ``claude -p`` (else ``None``).

    Only a ``claude`` invocation carrying the print/``-p`` flag counts as a sidecar;
    the id is read from ``--session-id`` / ``--resume`` / ``-r``. Only this id is
    extracted — the rest of the command (which may hold a prompt) is not surfaced here.
    """
    if name != "Bash":
        return None
    command = inputs.get("command")
    if not isinstance(command, str) or "claude" not in command:
        return None
    if not _SIDECAR_PRINT_RE.search(command):
        return None
    match = _SIDECAR_ID_RE.search(command)
    return match.group(1) if match else None


def _question_snippet(inputs: dict) -> str | None:
    """A few-word snippet of an ``AskUserQuestion``'s first question."""
    questions = inputs.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    first = questions[0]
    return _snippet(first.get("question")) if isinstance(first, dict) else None


def _snippet(text: object) -> str | None:
    """The full first line of free text as a node label, whitespace-collapsed.

    Returns ``None`` for empty / non-string input. Multi-line text keeps only its
    first non-empty line (the gist), but the line is never truncated — the label
    is always readable in full, with no ellipsis.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    first_line = text.strip().splitlines()[0]
    return " ".join(first_line.split()) or None


def _human_prompt_spans(records: list[dict], meta: dict[str, str | None]) -> list[Span]:
    spans: list[Span] = []
    for rec in records:
        if not _is_human_prompt(rec):
            continue
        ts = rec.get("timestamp")
        spans.append(
            Span(
                span_id=derive_span_id(meta["session_id"] or "", rec.get("uuid") or ts or ""),
                kind="human",
                name="prompt",
                summary=_snippet(_prompt_text(rec)),
                session_id=meta["session_id"],
                repo=meta["repo"] or "unknown",
                branch=meta["branch"],
                ts_start=ts,
                ts_end=ts,
                duration_ms=0,
                human={"type": "prompt", "wait_ms": None},
            )
        )
    return spans


def _reasoning_refs(
    records: list[dict],
    stem: str,
    source: str,
    agent_id: str | None,
    meta: dict[str, str | None],
) -> list[ReasoningRef]:
    """One :class:`ReasoningRef` per assistant turn that reasoned (Issue #59).

    A turn reasoned if it carries an extended-thinking block *or* a visible narration
    text block. The ``summary`` gist is the first line of that narration (never the
    redacted thinking body, which is signature-only) — only a locator, timing, and the
    user-visible gist are read, so nothing private leaks.
    """
    refs: list[ReasoningRef] = []
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content") or []
        has_thinking = any(isinstance(b, dict) and b.get("type") == "thinking" for b in content)
        narration = _narration_text(content)
        if not has_thinking and narration is None:
            continue
        refs.append(
            ReasoningRef(
                session_id=meta["session_id"],
                source=source,
                agent_id=agent_id,
                ts=rec.get("timestamp"),
                ref=f"{stem}#{rec.get('uuid') or ''}",
                summary=_snippet(narration),
            )
        )
    return refs


def _narration_text(content: list) -> str | None:
    """The first visible narration text block in an assistant turn (else ``None``)."""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text
    return None


def _context_and_rule_loads(records: list[dict], meta: dict[str, str | None]) -> list[Span]:
    """Extract the window's loaded context from ``attachment`` records (Issue #59).

    A ``nested_memory`` attachment carries one rule / CLAUDE.md / memory file (path +
    body); a ``deferred_tools_delta`` the tool schemas made available. Each item
    becomes a ``rule``-kind span whose ``phase`` names its subtype (``rule`` /
    ``CLAUDE.md`` / ``memory`` / ``tool-schema``), so the dashboard groups them into
    one ``context`` node per subtype. Only the name and a body-size token estimate are
    read — never the file body — and each item is recorded once (deduped on identity).
    """
    spans: list[Span] = []
    seen: set[tuple[str, str]] = set()
    for rec in records:
        attachment = rec.get("attachment")
        if rec.get("type") != "attachment" or not isinstance(attachment, dict):
            continue
        ts = rec.get("timestamp")
        kind = attachment.get("type")
        if kind == _NESTED_MEMORY:
            span = _nested_memory_span(attachment, ts, meta, seen)
            if span is not None:
                spans.append(span)
        elif kind == _DEFERRED_TOOLS:
            spans.extend(_tool_schema_spans(attachment, ts, meta, seen))
    return spans


def _nested_memory_span(
    attachment: dict, ts: str | None, meta: dict[str, str | None], seen: set[tuple[str, str]]
) -> Span | None:
    """One ``rule`` span for a ``nested_memory`` rule / CLAUDE.md / memory file (else None)."""
    path = attachment.get("path")
    if not isinstance(path, str):
        return None
    phase, name = _classify_context_path(path)
    if phase is None or name is None:
        return None
    key = (phase, path)
    if key in seen:
        return None
    seen.add(key)
    content = attachment.get("content")
    body = content.get("content") if isinstance(content, dict) else None
    return _context_span(phase, name, path, _size_summary(body), ts, meta)


def _tool_schema_spans(
    attachment: dict, ts: str | None, meta: dict[str, str | None], seen: set[tuple[str, str]]
) -> list[Span]:
    """One ``rule`` span (``phase='tool-schema'``) per tool a ``deferred_tools_delta`` adds.

    Each deferred tool is name-only until fetched, so its estimate is the name's size;
    the value of the surface is the count (``tool-schema xN``) and the cold-context
    lens — tools made available but never used are trimming candidates.
    """
    names = attachment.get("addedNames")
    if not isinstance(names, list):
        return []
    spans: list[Span] = []
    for name in names:
        if not isinstance(name, str):
            continue
        key = ("tool-schema", name)
        if key in seen:
            continue
        seen.add(key)
        spans.append(
            _context_span("tool-schema", name, f"tool:{name}", _size_summary(name), ts, meta)
        )
    return spans


def _classify_context_path(path: str) -> tuple[str | None, str | None]:
    """Map a ``nested_memory`` path to its ``(phase, name)`` subtype (else ``(None, None)``)."""
    if "/.claude/rules/" in path:
        return "rule", Path(path).stem
    if path.endswith("CLAUDE.md"):
        return "CLAUDE.md", "CLAUDE.md"
    if path.endswith("MEMORY.md") or "/memory/" in path:
        return "memory", Path(path).name
    return None, None


def _size_summary(body: object) -> str:
    """A per-item token estimate label (``~N tokens``) from a loaded item's body size.

    Rough (``~4`` chars/token) and clearly approximate — exact context sizing needs a
    token-counting API — but enough to weigh each item's context cost in the drill-down.
    """
    chars = len(body) if isinstance(body, str) else 0
    return f"~{max(1, chars // _CHARS_PER_TOKEN):,} tokens"


def _context_span(
    phase: str, name: str, identity: str, summary: str, ts: str | None, meta: dict[str, str | None]
) -> Span:
    """A ``rule``-kind loaded-context span tagged by ``phase`` subtype (Issue #59)."""
    return Span(
        span_id=derive_span_id(meta["session_id"] or "", "context", phase, identity),
        kind="rule",
        name=name,
        phase=phase,
        summary=summary,
        session_id=meta["session_id"],
        repo=meta["repo"] or "unknown",
        branch=meta["branch"],
        ts_start=ts,
        ts_end=ts,
        duration_ms=0,
    )


def _is_human_prompt(rec: dict) -> bool:
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return False
    message = rec.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    has_text = any(isinstance(b, dict) and b.get("type") == "text" for b in content)
    has_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return has_text and not has_tool_result


def _prompt_text(rec: dict) -> str | None:
    """The human prompt's text — a plain string or the first text block."""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                return block["text"]
    return None


def _tool_results(records: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for rec in records:
        if rec.get("type") != "user":
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        # A record's toolUseResult carries the agentId for the agent it spawned;
        # a user record holds exactly one tool_result, so the id maps 1:1 to it.
        tool_use_result = rec.get("toolUseResult")
        agent_id = tool_use_result.get("agentId") if isinstance(tool_use_result, dict) else None
        for block in message.get("content") or []:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str):
                continue
            index[tool_use_id] = {
                "ts": rec.get("timestamp"),
                "is_error": bool(block.get("is_error")),
                "agent_id": agent_id,
            }
    return index


def _walk_subagent(
    main_path: Path,
    agent_id: str,
    *,
    parent_meta: dict[str, str | None],
    agent_span_id: str,
    seen: set[str],
) -> tuple[list[UsageEvent], list[Span], dict[str, str]]:
    """Walk a sub-agent transcript into usage events, step spans, and agent links.

    The transcript (``<session>/subagents/agent-<id>.jsonl``) is a session-shaped
    log. Its ``tool_use`` blocks become the sub-agent's spans (#47 S3) — but its
    leading user record is the *orchestrator's task prompt*, so human-prompt spans
    are deliberately NOT emitted (that text stays private; the agent span already
    summarises the task via its ``description``). Each emitted span is re-homed
    onto the spoke: ``session_id`` becomes the parent's (so it joins the run and
    nests under the agent) and ``parent_id`` the agent span; ``span_id`` stays
    derived from the sub-agent transcript, keeping ids idempotent and collision-free.
    """
    sub = main_path.parent / main_path.stem / "subagents" / f"agent-{agent_id}.jsonl"
    if not sub.exists():
        return [], [], {}
    return _walk_transcript(
        _load_jsonl(sub),
        agent_id,
        main_path=main_path,
        parent_meta=parent_meta,
        agent_span_id=agent_span_id,
        seen=seen,
    )


def _walk_transcript(
    records: list[dict],
    agent_id: str,
    *,
    main_path: Path,
    parent_meta: dict[str, str | None],
    agent_span_id: str,
    seen: set[str],
) -> tuple[list[UsageEvent], list[Span], dict[str, str]]:
    """Walk one sub-agent transcript's records into usage events, spans, and links.

    Shared by the Task-spawned walk (:func:`_walk_subagent`) and the workflow walk
    (:func:`_walk_workflow_agents`). A nested Task/Agent ``tool_use`` (Issue #51 S2)
    recurses into its own transcript so agent→agent→… chains reconstruct at any
    depth; ``seen`` guards against a cyclic link looping forever. The leading user
    record is the orchestrator's task prompt, so human-prompt spans are deliberately
    NOT emitted (that text stays private). Each span is re-homed onto the spoke:
    ``session_id`` becomes the parent's and ``parent_id`` the agent span; ``span_id``
    stays derived from the transcript, keeping ids idempotent and collision-free.
    """
    meta = _session_meta(records)
    results = _tool_results(records)
    todo_summaries = _todo_summaries(records)
    events: list[UsageEvent] = []
    spans: list[Span] = []
    links: dict[str, str] = {}
    for rec in records:
        message = rec.get("message")
        if rec.get("type") != "assistant" or not isinstance(message, dict):
            continue
        if isinstance(message.get("usage"), dict):
            events.append(_usage_event(rec, "subagent", agent_id))
        for block in message.get("content") or []:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            span = _span_for_tool_use(block, rec, results, meta, todo_summaries)
            if span is None:
                continue
            span.session_id = parent_meta["session_id"]
            span.repo = parent_meta["repo"] or "unknown"
            span.branch = parent_meta["branch"]
            span.parent_id = agent_span_id
            spans.append(span)
            if span.kind == "agent":
                ev, sp, lk = _link_and_walk_agent(
                    span, block.get("id") or "", results, main_path, parent_meta, seen
                )
                events.extend(ev)
                spans.extend(sp)
                links.update(lk)
    return events, spans, links


def _link_and_walk_agent(
    span: Span,
    tool_use_id: str,
    results: dict[str, dict],
    main_path: Path,
    parent_meta: dict[str, str | None],
    seen: set[str],
) -> tuple[list[UsageEvent], list[Span], dict[str, str]]:
    """Link an agent span to its sub-agent transcript and walk that transcript once.

    The ``agent_link`` display field is always set so the chain renders at any depth
    (Issue #50). The cost-bearing link (``links[span_id] -> agentId``) and the
    transcript walk happen only the FIRST time an ``agentId`` is seen: a repeated or
    cyclic ``agentId`` (Issue #51 S2) is therefore walked at most once — no duplicate
    usage events — and owned by exactly one span, so ``cost.py`` never attributes one
    transcript's tokens to two spans. Returns the nested transcript's
    ``(events, spans, links)``; empty when the id is unresolved or already seen.
    """
    agent_id = results.get(tool_use_id, {}).get("agent_id")
    if not agent_id:
        return [], [], {}
    span.agent_link = agent_id
    if agent_id in seen:
        return [], [], {}
    seen.add(agent_id)
    events, spans, links = _walk_subagent(
        main_path, agent_id, parent_meta=parent_meta, agent_span_id=span.span_id, seen=seen
    )
    links[span.span_id] = agent_id
    return events, spans, links


def _walk_workflow_agents(
    main_path: Path, *, parent_meta: dict[str, str | None], seen: set[str]
) -> tuple[list[UsageEvent], list[Span], dict[str, str]]:
    """Discover every ``Workflow`` fan-out under this session as a drillable subtree.

    Workflow agents live one level deeper than Task sub-agents — at
    ``<session>/subagents/workflows/wf_*/agent-<id>.jsonl`` — and no Task ``tool_use``
    links them, so they are found by walking that tree (Issue #51). Each ``wf_*`` run is
    grouped into a ``workflow → workflow_phase → agent`` subtree from its sidecar
    definition (Issue #58 — see :func:`_walk_one_workflow`). ``seen`` is the
    session-global walked-id set, so an agent already reached through a link is not
    discovered (and walked) again.
    """
    root = main_path.parent / main_path.stem / "subagents" / "workflows"
    if not root.is_dir():
        return [], [], {}
    session_dir = main_path.parent / main_path.stem
    events: list[UsageEvent] = []
    spans: list[Span] = []
    links: dict[str, str] = {}
    for wf_dir in sorted(p for p in root.glob("wf_*") if p.is_dir()):
        wf_events, wf_spans, wf_links = _walk_one_workflow(
            wf_dir, session_dir, main_path=main_path, parent_meta=parent_meta, seen=seen
        )
        events.extend(wf_events)
        spans.extend(wf_spans)
        links.update(wf_links)
    return events, spans, links


def _walk_one_workflow(
    wf_dir: Path,
    session_dir: Path,
    *,
    main_path: Path,
    parent_meta: dict[str, str | None],
    seen: set[str],
) -> tuple[list[UsageEvent], list[Span], dict[str, str]]:
    """Walk one ``wf_*`` fan-out: emit its agent spans (+ nested transcripts) and the
    ``workflow``/``workflow_phase`` containers that group them (Issue #58).

    Each agent gets an ``agent`` span (``name`` = the sidecar ``agentType``, ``summary``
    = the workflow name, window = the transcript's first/last timestamp), its
    ``agent_link`` registered for cost attribution, and its ``tool_use`` blocks walked
    as nested spans. No container span is emitted when every agent id is already
    ``seen`` — a fully-revisited workflow plants no empty subtree.
    """
    defn = _workflow_def(session_dir, wf_dir.name)
    events: list[UsageEvent] = []
    spans: list[Span] = []
    links: dict[str, str] = {}
    agent_phases: list[tuple[Span, str | None]] = []
    for agent_path in sorted(wf_dir.glob("agent-*.jsonl")):
        agent_id = agent_path.stem[len("agent-") :]
        if agent_id in seen:
            continue
        seen.add(agent_id)
        records = _load_jsonl(agent_path)
        span = _workflow_agent_span(
            agent_id, _agent_type(wf_dir, agent_id), defn.name, records, parent_meta
        )
        links[span.span_id] = agent_id
        spans.append(span)
        agent_phases.append((span, defn.agent_phase.get(agent_id)))
        # A workflow agent may itself spawn Task sub-agents — recurse with the
        # session-global ``seen`` (this agent is already in it) so any nested
        # agent is walked once across the whole session.
        sub_events, sub_spans, sub_links = _walk_transcript(
            records,
            agent_id,
            main_path=main_path,
            parent_meta=parent_meta,
            agent_span_id=span.span_id,
            seen=seen,
        )
        events.extend(sub_events)
        spans.extend(sub_spans)
        links.update(sub_links)
    if agent_phases:
        spans.extend(_workflow_containers(wf_dir.name, defn, agent_phases, parent_meta))
    return events, spans, links


def _workflow_containers(
    run_id: str,
    defn: _WorkflowDef,
    agent_phases: list[tuple[Span, str | None]],
    parent_meta: dict[str, str | None],
) -> list[Span]:
    """The ``workflow`` span and one ``workflow_phase`` per phase, re-homing each agent's
    ``parent_id`` onto its phase (or the workflow directly when the phase is unmapped).

    Phases render in the workflow's declared order, restricted to those with a
    discovered agent — an empty phase plants no node. Each container's window spans its
    members' windows and carries no ``agent_link``, so cost stays on the agent leaves
    (own-cost $0; conservation Σ owned == Σ turns).
    """
    session_id = parent_meta["session_id"] or ""
    members = [span for span, _ in agent_phases]
    workflow = _container_span(
        derive_span_id(session_id, run_id), "workflow", defn.name, members, parent_meta
    )
    by_phase: dict[str, list[Span]] = {}
    for span, phase in agent_phases:
        if phase is None:
            span.parent_id = workflow.span_id
        else:
            by_phase.setdefault(phase, []).append(span)
    order = list(defn.phase_order)
    order += [phase for phase in by_phase if phase not in order]
    containers = [workflow]
    for phase in order:
        phase_members = by_phase.get(phase)
        if not phase_members:
            continue
        phase_span = _container_span(
            derive_span_id(session_id, run_id, phase),
            "workflow_phase",
            phase,
            phase_members,
            parent_meta,
        )
        phase_span.parent_id = workflow.span_id
        for member in phase_members:
            member.parent_id = phase_span.span_id
        containers.append(phase_span)
    return containers


def _container_span(
    span_id: str,
    kind: str,
    name: str,
    members: list[Span],
    parent_meta: dict[str, str | None],
) -> Span:
    """A ``workflow``/``workflow_phase`` container bracketing its members' time window."""
    starts = [m.ts_start for m in members if m.ts_start]
    ends = [m.ts_end for m in members if m.ts_end]
    ts_start = min(starts) if starts else None
    ts_end = max(ends) if ends else None
    return Span(
        span_id=span_id,
        kind=kind,
        name=name,
        session_id=parent_meta["session_id"],
        repo=parent_meta["repo"] or "unknown",
        branch=parent_meta["branch"],
        ts_start=ts_start,
        ts_end=ts_end,
        duration_ms=_duration_ms(ts_start, ts_end),
    )


def _workflow_agent_span(
    agent_id: str,
    agent_type: str | None,
    workflow_name: str | None,
    records: list[dict],
    parent_meta: dict[str, str | None],
) -> Span:
    """One ``agent`` span for a discovered workflow agent, bracketing its transcript.

    The window is the transcript's first and last record timestamps; ``name`` is the
    sidecar ``agent-<id>.meta.json`` ``agentType`` (the stable grouping key) and
    ``summary`` the workflow name (the few-word display label).
    """
    ts_start, ts_end = _transcript_window(records)
    return Span(
        span_id=derive_span_id(parent_meta["session_id"] or "", agent_id),
        kind="agent",
        name=agent_type or "agent",
        summary=workflow_name,
        session_id=parent_meta["session_id"],
        repo=parent_meta["repo"] or "unknown",
        branch=parent_meta["branch"],
        ts_start=ts_start,
        ts_end=ts_end,
        duration_ms=_duration_ms(ts_start, ts_end),
        agent_link=agent_id,
    )


@dataclass(slots=True)
class _WorkflowDef:
    """The pull-relevant slice of a ``<session>/workflows/<runId>.json`` definition.

    ``name`` is the display label, ``phase_order`` the declared phase titles in order,
    and ``agent_phase`` maps each ``agentId`` to its ``phaseTitle`` (absent when the
    agent has no phase — it then falls directly under the workflow container).
    """

    name: str
    phase_order: list[str]
    agent_phase: dict[str, str]


def _workflow_def(session_dir: Path, run_id: str) -> _WorkflowDef:
    """Read ``<session>/workflows/<run_id>.json`` for the workflow name, phase order
    and per-agent phase map (Issue #58).

    Degrades gracefully: a missing or malformed definition yields the run id as the
    workflow label, no phases and an empty agent→phase map — so a ``workflow`` container
    is still emitted (its agents fall directly under it) and grouping never depends on
    the sidecar metadata.
    """
    path = session_dir / "workflows" / f"{run_id}.json"
    if not path.is_file():
        return _WorkflowDef(run_id, [], {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _WorkflowDef(run_id, [], {})
    if not isinstance(data, dict):
        return _WorkflowDef(run_id, [], {})
    phases = data.get("phases")
    phase_order = (
        [p["title"] for p in phases if isinstance(p, dict) and isinstance(p.get("title"), str)]
        if isinstance(phases, list)
        else []
    )
    agent_phase: dict[str, str] = {}
    progress = data.get("workflowProgress")
    if isinstance(progress, list):
        for entry in progress:
            if not isinstance(entry, dict) or entry.get("type") != "workflow_agent":
                continue
            agent_id, phase_title = entry.get("agentId"), entry.get("phaseTitle")
            if isinstance(agent_id, str) and isinstance(phase_title, str):
                agent_phase[agent_id] = phase_title
    name = data.get("workflowName")
    return _WorkflowDef(name if isinstance(name, str) else run_id, phase_order, agent_phase)


def _agent_type(wf_dir: Path, agent_id: str) -> str | None:
    """The ``agentType`` from a workflow agent's sidecar ``agent-<id>.meta.json``.

    ``None`` when the sidecar is missing or malformed, so the span falls back to the
    bare ``agent`` name.
    """
    path = wf_dir / f"agent-{agent_id}.meta.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    agent_type = data.get("agentType") if isinstance(data, dict) else None
    return agent_type if isinstance(agent_type, str) else None


def _transcript_window(records: list[dict]) -> tuple[str | None, str | None]:
    """The first and last record timestamps in a transcript (its wall-clock window)."""
    timestamps = sorted(ts for rec in records if (ts := rec.get("timestamp")))
    return (timestamps[0], timestamps[-1]) if timestamps else (None, None)


def _usage_event(rec: dict, source: str, agent_id: str | None) -> UsageEvent:
    message = rec.get("message") or {}
    usage = message.get("usage") or {}
    return UsageEvent(
        session_id=rec.get("sessionId"),
        ts=rec.get("timestamp"),
        model=message.get("model"),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
        source=source,
        agent_id=agent_id,
    )


def _session_meta(records: list[dict]) -> dict[str, str | None]:
    session_id: str | None = None
    repo: str | None = None
    branch: str | None = None
    for rec in records:
        session_id = session_id or rec.get("sessionId")
        cwd = rec.get("cwd")
        if repo is None and cwd:
            repo = Path(cwd).name
        branch = branch or rec.get("gitBranch")
        if session_id and repo and branch:
            break
    return {"session_id": session_id, "repo": repo or "unknown", "branch": branch}


def _load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _duration_ms(start: str | None, end: str | None) -> int:
    if not start or not end:
        return 0
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(int((ended - started).total_seconds() * 1000), 0)
