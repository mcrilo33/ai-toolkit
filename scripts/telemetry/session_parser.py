"""Parse Claude session-log JSONL into pull spans (skill/agent/todo/human).

Reads ``~/.claude/projects/<slug>/<session>.jsonl`` transcripts and reconstructs
the spans the runtime cannot emit at hook time. For ``agent`` spans it walks the
matching ``<session>/subagents/agent-<id>.jsonl`` transcript so the subagent's
own token usage can be attributed to the parent agent span by the correlation
pass.

Privacy: metadata plus short *intent* labels are read out of a record — tool
name, skill name, subagent type, timestamps, token counts, the ``agentId`` link,
and (Issue #47) a few-word ``summary`` per node: the in-progress todo item, an
agent's task ``description``, a human prompt/question's first line, and a tool's
single main parameter (Bash command, file path, Grep pattern). Bulk/long-form
content — thinking, an agent's full prompt, a tool's secondary input (replacement
text, file content) and its output, and human answers — is never copied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from telemetry.spans import Span, derive_span_id

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
class ParsedSession:
    """Parser output: spans plus the raw material the correlation pass needs."""

    spans: list[Span] = field(default_factory=list)
    usage_events: list[UsageEvent] = field(default_factory=list)
    agent_links: dict[str, str] = field(default_factory=dict)  # agent span_id -> agentId


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
    return Span(
        kind="tool",
        name=name if isinstance(name, str) else "tool",
        summary=_tool_param(name, inputs),
        **common,
    )


def _todo_summaries(records: list[dict]) -> dict[str, str]:
    """Map each todo ``tool_use`` id to the few-word item it advances (Issue #47).

    Two ledger shapes are resolved into one summary per write:

    - ``TodoWrite`` snapshots: diff each write's in-progress set against the
      previous one to isolate the item that *newly* entered progress — the step's
      todo. No distinguishable transition falls back to whatever is in progress;
      nothing in progress yields no summary (the span keeps its bare tool name).
    - ``TaskCreate`` / ``TaskUpdate`` (incremental, id-keyed): a ``TaskCreate``
      summarises to its ``subject`` and is assigned the next sequential id (the
      runtime numbers them 1, 2, … in creation order); a later ``TaskUpdate``
      resolves its ``taskId`` back to that subject.
    """
    summaries: dict[str, str] = {}
    prev: set[str] = set()
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
                summary, prev = _derive_todo_summary(inputs, prev)
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


def _derive_todo_summary(inputs: dict, prev: set[str]) -> tuple[str | None, set[str]]:
    """The in-progress item for one ``TodoWrite`` snapshot, plus its in-progress set."""
    items = inputs.get("todos")
    if not isinstance(items, list):
        return None, prev
    in_progress = {
        item["content"]
        for item in items
        if isinstance(item, dict) and item.get("status") == "in_progress" and item.get("content")
    }
    newly = sorted(in_progress - prev)
    summary = newly[0] if newly else (sorted(in_progress)[0] if in_progress else None)
    return _snippet(summary), in_progress


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
    """Discover the agents of every ``Workflow`` fan-out under this session (Issue #51).

    Workflow agents live one level deeper than Task sub-agents — at
    ``<session>/subagents/workflows/wf_*/agent-<id>.jsonl`` — and no Task ``tool_use``
    links them, so they are found by walking that tree. Each gets its own ``agent``
    span (``name`` = the ``meta.json`` ``agentType``, ``summary`` = the workflow name,
    window = the transcript's first/last timestamp), its ``agent_link`` registered for
    cost attribution, and its ``tool_use`` blocks walked as nested spans — so its turns
    no longer orphan to ``(unresolved)``. ``seen`` is the session-global walked-id set,
    so an agent already reached through a link is not discovered (and walked) again.
    """
    root = main_path.parent / main_path.stem / "subagents" / "workflows"
    if not root.is_dir():
        return [], [], {}
    events: list[UsageEvent] = []
    spans: list[Span] = []
    links: dict[str, str] = {}
    for wf_dir in sorted(p for p in root.glob("wf_*") if p.is_dir()):
        name, agent_types = _workflow_meta(wf_dir)
        for agent_path in sorted(wf_dir.glob("agent-*.jsonl")):
            agent_id = agent_path.stem[len("agent-") :]
            if agent_id in seen:
                continue
            seen.add(agent_id)
            records = _load_jsonl(agent_path)
            span = _workflow_agent_span(
                agent_id, agent_types.get(agent_id), name, records, parent_meta
            )
            links[span.span_id] = agent_id
            spans.append(span)
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
    return events, spans, links


def _workflow_agent_span(
    agent_id: str,
    agent_type: str | None,
    workflow_name: str | None,
    records: list[dict],
    parent_meta: dict[str, str | None],
) -> Span:
    """One ``agent`` span for a discovered workflow agent, bracketing its transcript.

    The window is the transcript's first and last record timestamps; ``name`` is the
    ``meta.json`` ``agentType`` (the stable grouping key) and ``summary`` the workflow
    name (the few-word display label).
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


def _workflow_meta(wf_dir: Path) -> tuple[str | None, dict[str, str]]:
    """The workflow name and a per-agent ``agentType`` map from ``wf_*/meta.json``.

    Degrades gracefully: a missing or malformed ``meta.json`` yields the directory
    name as the workflow label and an empty agent-type map (agents fall back to the
    bare ``agent`` name), so discovery never depends on the sidecar metadata.
    """
    meta_path = wf_dir / "meta.json"
    if not meta_path.is_file():
        return wf_dir.name, {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return wf_dir.name, {}
    name = meta.get("name") if isinstance(meta, dict) else None
    agents = meta.get("agents") if isinstance(meta, dict) else None
    agent_types: dict[str, str] = {}
    if isinstance(agents, dict):
        for agent_id, info in agents.items():
            if isinstance(info, dict) and isinstance(info.get("agentType"), str):
                agent_types[agent_id] = info["agentType"]
    return (name if isinstance(name, str) else wf_dir.name), agent_types


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
