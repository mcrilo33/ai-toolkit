"""Parse Claude session-log JSONL into pull spans (skill/agent/todo/human).

Reads ``~/.claude/projects/<slug>/<session>.jsonl`` transcripts and reconstructs
the spans the runtime cannot emit at hook time. For ``agent`` spans it walks the
matching ``<session>/subagents/agent-<id>.jsonl`` transcript so the subagent's
own token usage can be attributed to the parent agent span by the correlation
pass.

Privacy: only metadata is read out of a record — tool name, skill name,
subagent type, timestamps, token counts, the ``agentId`` link. Prompt text,
answers, thinking, and tool output are never copied into a span.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from telemetry.spans import Span, derive_span_id

AGENT_TOOLS = frozenset({"Task", "Agent"})
TODO_TOOLS = frozenset({"TodoWrite", "TaskCreate", "TaskUpdate"})


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

    parsed = ParsedSession()
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        if isinstance(message.get("usage"), dict):
            parsed.usage_events.append(_usage_event(rec, "main", None))
        for block in message.get("content") or []:
            _consume_tool_use(block, rec, results, meta, path, parsed)

    parsed.spans.extend(_human_prompt_spans(records, meta))
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
) -> None:
    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
        return
    span = _span_for_tool_use(block, rec, results, meta)
    if span is None:
        return
    parsed.spans.append(span)
    if span.kind != "agent":
        return
    agent_id = results.get(block.get("id") or "", {}).get("agent_id")
    if agent_id:
        parsed.agent_links[span.span_id] = agent_id
        parsed.usage_events.extend(_walk_subagent(path, agent_id))


def _span_for_tool_use(
    block: dict, rec: dict, results: dict[str, dict], meta: dict[str, str | None]
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
        return Span(kind="agent", name=inputs.get("subagent_type", "agent"), **common)
    if name in TODO_TOOLS:
        return Span(kind="todo", name=name, **common)
    if name == "AskUserQuestion":
        human = {"type": "question", "wait_ms": _duration_ms(ts_start, ts_end)}
        return Span(kind="human", name="AskUserQuestion", human=human, **common)
    return None


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


def _walk_subagent(main_path: Path, agent_id: str) -> list[UsageEvent]:
    sub = main_path.parent / main_path.stem / "subagents" / f"agent-{agent_id}.jsonl"
    if not sub.exists():
        return []
    events: list[UsageEvent] = []
    for rec in _load_jsonl(sub):
        message = rec.get("message")
        if rec.get("type") != "assistant" or not isinstance(message, dict):
            continue
        if isinstance(message.get("usage"), dict):
            events.append(_usage_event(rec, "subagent", agent_id))
    return events


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
