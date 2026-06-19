"""Unit tests for the unified span emit helper (Issue #21, subtask 1 — RED).

The shared helper ``shared/hooks/lib/telemetry.sh`` defines ``telemetry_emit_span``,
the single source of truth for appending one **span** object per event to
``${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl``.

Frozen schema v1 (the contract for downstream issues B + C)::

    {
      "span_id", "parent_id",          # nesting
      "spoke_run_id",                  # ties all spans of one spoke
      "session_id",                    # join key to CC token cost
      "workflow_rev",                  # ai-toolkit short SHA at emit time
      "repo", "branch",
      "kind", "name", "phase",
      "ts_start", "ts_end", "duration_ms",
      "status",
      "human",                         # {type, wait_ms} or null
      "tokens_in", "tokens_out", "cost_usd"   # null at emit; filled by the
                                              # later correlation pass
    }

Discipline mirrors the existing ``telemetry_event()``: opt-in via
``AI_TOOLKIT_TELEMETRY=1``, metadata only (no paths / commands / messages /
payload content), zero bytes on stdout / stderr, no-op when unset.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TELEMETRY_LIB = REPO_ROOT / "shared" / "hooks" / "lib" / "telemetry.sh"

# Every key the frozen schema v1 must carry on every span.
SCHEMA_KEYS = {
    "span_id",
    "parent_id",
    "spoke_run_id",
    "session_id",
    "workflow_rev",
    "repo",
    "branch",
    "kind",
    "name",
    "phase",
    "ts_start",
    "ts_end",
    "duration_ms",
    "status",
    "human",
    # The hook's raising condition (#82) — PreToolUse/PostToolUse/SessionStart/…;
    # set only on kind=hook spans, null on every other span.
    "hook_event",
    # v3 spoke-trace link fields (#50) — pull-only, null on push (#54 track E).
    "summary",
    "emits",
    "sidecar_session",
    "agent_link",
    "tokens_in",
    "tokens_out",
    "cost_usd",
}


def _env(
    telemetry_dir: Path | None = None,
    *,
    enabled: bool = True,
    home: Path | None = None,
    workflow_rev: str | None = "testrev0",
    payload: str | None = None,
) -> dict[str, str]:
    """Ambient env with deterministic telemetry settings."""
    env = os.environ.copy()
    for var in (
        "AI_TOOLKIT_TELEMETRY",
        "AI_TOOLKIT_TELEMETRY_DIR",
        "AI_TOOLKIT_WORKFLOW_REV",
        "CURSOR_PROJECT_DIR",
        "TELEMETRY_PARENT_ID",
        # The pre-push gate runs pytest with AI_TOOLKIT_PARENT_SPAN exported; it outranks
        # the payload tool_use_id (the #66 precedence), so strip it for a deterministic
        # parent_id driven only by what each test sets (mirrors test_telemetry_parent_span).
        "AI_TOOLKIT_PARENT_SPAN",
        # The OTLP fan-out sink (#83) fires whenever this points at a collector; strip it so
        # the events.jsonl tests never accidentally attempt a real POST under a live env.
        "AI_TOOLKIT_OTEL_SPAN_ENDPOINT",
    ):
        env.pop(var, None)
    if enabled:
        env["AI_TOOLKIT_TELEMETRY"] = "1"
    if telemetry_dir is not None:
        env["AI_TOOLKIT_TELEMETRY_DIR"] = str(telemetry_dir)
    if home is not None:
        env["HOME"] = str(home)
    if workflow_rev is not None:
        env["AI_TOOLKIT_WORKFLOW_REV"] = workflow_rev
    if payload is not None:
        env["INPUT"] = payload
    return env


def _emit(
    args: str,
    env: dict[str, str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Source the lib and call telemetry_emit_span with the given arg string."""
    script = f'source "{TELEMETRY_LIB}"; telemetry_emit_span {args}'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
    )


def _read_events(events_file: Path) -> list[dict]:
    return [json.loads(line) for line in events_file.read_text().splitlines()]


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "sample-project"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    return tmp_path / "telemetry"


# ── schema validity ───────────────────────────────────────


class TestSpanSchema:
    def test_emit_writes_one_valid_jsonl_span(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        result = _emit(
            "--kind lifecycle --name worktree-new --phase spawn --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        assert result.returncode == 0
        events = _read_events(telemetry_dir / "events.jsonl")
        assert len(events) == 1
        assert SCHEMA_KEYS.issubset(events[0].keys())

    def test_kind_name_phase_status_round_trip(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        _emit(
            "--kind step --name solo-cycle --phase red --status failure",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["kind"] == "step"
        assert span["name"] == "solo-cycle"
        assert span["phase"] == "red"
        assert span["status"] == "failure"

    def test_tokens_and_cost_null_at_emit(self, project_root: Path, telemetry_dir: Path) -> None:
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["tokens_in"] is None
        assert span["tokens_out"] is None
        assert span["cost_usd"] is None

    def test_ts_fields_iso8601_utc(self, project_root: Path, telemetry_dir: Path) -> None:
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        for field in ("ts_start", "ts_end"):
            ts = datetime.fromisoformat(span[field])
            assert ts.tzinfo is not None
            assert ts.utcoffset() == timedelta(0)

    def test_duration_ms_is_nonnegative_int(self, project_root: Path, telemetry_dir: Path) -> None:
        # A start clock of "0" ms means duration = now - 0 → a large positive int;
        # the contract is only that duration_ms is a non-negative integer.
        _emit(
            "--kind hook --name secrets-scan --status success --start-ms 0",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert isinstance(span["duration_ms"], int)
        assert span["duration_ms"] >= 0

    def test_ts_start_reflects_supplied_start_ms(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        # --start-ms must drive ts_start, not emit time. 1_700_000_000_000 ms
        # == 2023-11-14T22:13:20Z. A start clock that decays to epoch zero
        # (1970) is the bug class this pins.
        _emit(
            "--kind lifecycle --name worktree-new --phase spawn "
            "--status success --start-ms 1700000000000",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["ts_start"] == "2023-11-14T22:13:20Z"
        assert span["ts_start"] < span["ts_end"]

    def test_default_phase_and_human_are_null(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["phase"] is None
        assert span["human"] is None

    def test_human_block_recorded_when_provided(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        _emit(
            "--kind human --name approval --status success "
            "--human-type approval --human-wait-ms 4200",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["human"] == {"type": "approval", "wait_ms": 4200}


# ── v3 spoke-trace additions (Issue #54 track E) ──────────


# The pull-only v3 link fields (#50). Push emitters MUST serialize them, always
# as null — the parser fills them later. ``summary`` is the #47 display field,
# likewise null on push.
V3_NULL_ON_PUSH_KEYS = ("summary", "emits", "sidecar_session", "agent_link")


class TestScriptKindEmission:
    def test_script_kind_round_trips(self, project_root: Path, telemetry_dir: Path) -> None:
        # Track E makes control scripts first-class trace nodes: ``--kind script``
        # must be accepted and serialized like any other kind.
        result = _emit(
            "--kind script --name commit-gauntlet --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        assert result.returncode == 0
        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["kind"] == "script"
        assert span["name"] == "commit-gauntlet"

    def test_v3_link_fields_present_and_null_on_push(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        # The emission link (``emits``) and its siblings are pull-only: the parser
        # fills them. Push emitters serialize them, always as null — a script span
        # never knows the span_id of the marker it produced.
        _emit(
            "--kind script --name commit-gauntlet --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        for key in V3_NULL_ON_PUSH_KEYS:
            assert key in span, f"push span missing v3 field {key!r}"
            assert span[key] is None, f"push span must leave {key!r} null"


# ── context resolution ────────────────────────────────────


class TestSpanContext:
    def test_repo_is_basename(self, project_root: Path, telemetry_dir: Path) -> None:
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["repo"] == "sample-project"
        assert "/" not in span["repo"]

    def test_workflow_rev_from_env_override(self, project_root: Path, telemetry_dir: Path) -> None:
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir, workflow_rev="abc1234"),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["workflow_rev"] == "abc1234"

    def test_session_id_extracted_from_payload(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = json.dumps(
            {
                "session_id": "sess-XYZ",
                "transcript_path": "/secret/path/transcript.jsonl",
                "cwd": "/secret/cwd",
            }
        )
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir, payload=payload),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["session_id"] == "sess-XYZ"

    def test_spoke_run_id_read_from_worktree_file(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        ai_dir = project_root / ".ai-toolkit"
        ai_dir.mkdir()
        (ai_dir / "spoke-run-id").write_text("feature/21-foo+1700000000\n")

        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["spoke_run_id"] == "feature/21-foo+1700000000"

    def test_parent_id_from_env(self, project_root: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        env["TELEMETRY_PARENT_ID"] = "parent-span-1"
        _emit(
            "--kind hook --name secrets-scan --status success",
            env,
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "parent-span-1"

    def test_span_id_is_present_and_unique(self, project_root: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        _emit("--kind hook --name a --status success", env, cwd=project_root)
        _emit("--kind hook --name b --status success", env, cwd=project_root)

        spans = _read_events(telemetry_dir / "events.jsonl")
        ids = {s["span_id"] for s in spans}
        assert all(s["span_id"] for s in spans)
        assert len(ids) == 2


# ── opt-in + invisibility + privacy ───────────────────────


class TestSpanDiscipline:
    def test_noop_when_disabled(self, project_root: Path, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        result = _emit(
            "--kind hook --name secrets-scan --status success",
            _env(enabled=False, home=fake_home),
            cwd=project_root,
        )

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""
        assert not (fake_home / ".ai-toolkit").exists()

    def test_invisible_no_stdout_or_stderr(self, project_root: Path, telemetry_dir: Path) -> None:
        result = _emit(
            "--kind lifecycle --name worktree-new --phase spawn --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        assert result.stdout == ""
        assert result.stderr == ""

    def test_no_payload_content_leaks(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = json.dumps(
            {
                "session_id": "sess-1",
                "transcript_path": "/home/u/SECRETPATH/t.jsonl",
                "cwd": "/home/u/SECRETCWD",
                "tool_input": {"command": "echo SECRETCOMMAND123"},
            }
        )
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir, payload=payload),
            cwd=project_root,
        )

        content = (telemetry_dir / "events.jsonl").read_text()
        assert "SECRETPATH" not in content
        assert "SECRETCWD" not in content
        assert "SECRETCOMMAND123" not in content


# ── hook causal parent + event (Issue #82) ────────────────


def _tool_payload(event: str, tool: str, tool_use_id: str, root: Path) -> str:
    """A Claude Pre/PostToolUse payload carrying the triggering tool's id."""
    return json.dumps(
        {
            "session_id": "sess-1",
            "hook_event_name": event,
            "tool_name": tool,
            "tool_input": {"file_path": str(root / "SMOKE.txt")},
            "tool_use_id": tool_use_id,
            "workspace_roots": [str(root)],
        }
    )


def _lifecycle_payload(event: str, root: Path) -> str:
    """A non-tool hook payload (SessionStart / Stop / …) — no ``tool_use_id``."""
    return json.dumps(
        {"session_id": "sess-1", "hook_event_name": event, "workspace_roots": [str(root)]}
    )


def _seed_spoke(root: Path, run_id: str = "feature/82-x+1700000000") -> None:
    ai_dir = root / ".ai-toolkit"
    ai_dir.mkdir(exist_ok=True)
    (ai_dir / "spoke-run-id").write_text(run_id + "\n")


class TestHookCausalParent:
    """A Pre/PostToolUse hook span nests under the tool that triggered it (#82)."""

    @pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
    def test_tooluse_hook_parents_to_tool_use_id(
        self, event: str, project_root: Path, telemetry_dir: Path
    ) -> None:
        _seed_spoke(project_root)
        payload = _tool_payload(event, "Write", "toolu_write_1", project_root)
        _emit(
            "--kind hook --name secrets-scan.sh --status success",
            _env(telemetry_dir, payload=payload),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "toolu_write_1"

    @pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
    def test_tooluse_hook_records_event(
        self, event: str, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _tool_payload(event, "Write", "toolu_write_1", project_root)
        _emit(
            "--kind hook --name secrets-scan.sh --status success",
            _env(telemetry_dir, payload=payload),
            cwd=project_root,
        )

        assert _read_events(telemetry_dir / "events.jsonl")[0]["hook_event"] == event

    def test_tool_use_id_outranks_stale_parent_span_file(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        # The parent-span file holds the LAST Bash tool_use_id; for a Write hook it is
        # stale. The hook's own payload tool_use_id is the correct, more specific parent.
        ai_dir = project_root / ".ai-toolkit"
        ai_dir.mkdir()
        (ai_dir / "parent-span").write_text("toolu_stale_bash\n")
        payload = _tool_payload("PreToolUse", "Write", "toolu_write_9", project_root)
        _emit(
            "--kind hook --name post-edit-format.sh --status success",
            _env(telemetry_dir, payload=payload),
            cwd=project_root,
        )

        assert _read_events(telemetry_dir / "events.jsonl")[0]["parent_id"] == "toolu_write_9"

    def test_explicit_parent_id_still_wins_over_payload(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _tool_payload("PreToolUse", "Write", "toolu_write_1", project_root)
        env = _env(telemetry_dir, payload=payload)
        env["TELEMETRY_PARENT_ID"] = "parent-explicit"
        _emit("--kind hook --name x.sh --status success", env, cwd=project_root)

        assert _read_events(telemetry_dir / "events.jsonl")[0]["parent_id"] == "parent-explicit"

    def test_non_tool_hook_keeps_spoke_root_parent(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        # SessionStart carries no tool_use_id, so the span still hangs off the spoke
        # root — its event is recorded for the hook line, the nesting is unchanged.
        _seed_spoke(project_root)
        payload = _lifecycle_payload("SessionStart", project_root)
        _emit(
            "--kind hook --name todo-ledger-nudge.sh --status success",
            _env(telemetry_dir, payload=payload),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "feature/82-x+1700000000"
        assert span["hook_event"] == "SessionStart"

    def test_tool_use_id_is_hook_scoped_not_on_step_span(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        # The payload parenting is hook-specific: a step/lifecycle marker emitted on the
        # same payload must NOT inherit the tool_use_id (markers bucket by interval) and
        # carries no hook_event.
        _seed_spoke(project_root)
        payload = _tool_payload("PreToolUse", "Write", "toolu_write_1", project_root)
        _emit(
            "--kind step --name solo-cycle --phase green --status success",
            _env(telemetry_dir, payload=payload),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "feature/82-x+1700000000"
        assert span["hook_event"] is None

    def test_tool_event_payload_does_not_leak_content(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        # The tool-event branch reads only the opaque tool_use_id + event name — never
        # the tool_input (file paths, commands). Pin that the rest of the payload stays out.
        payload = json.dumps(
            {
                "session_id": "sess-1",
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "/home/u/SECRETPATH/x.py", "content": "SECRETBODY"},
                "tool_use_id": "toolu_ok",
                "workspace_roots": [str(project_root)],
            }
        )
        _emit(
            "--kind hook --name secrets-scan.sh --status success",
            _env(telemetry_dir, payload=payload),
            cwd=project_root,
        )

        content = (telemetry_dir / "events.jsonl").read_text()
        assert "SECRETPATH" not in content
        assert "SECRETBODY" not in content

    def test_non_hook_span_leaves_hook_event_null(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        _emit(
            "--kind lifecycle --name worktree-new --phase spawn --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        assert _read_events(telemetry_dir / "events.jsonl")[0]["hook_event"] is None


# ── OTLP / Langfuse fan-out sink (Issue #83) ───────────────

# The endpoint env var that gates the second, INDEPENDENT sink: a single OTLP/HTTP-JSON
# span POSTed to a local collector (which maps resource ``spoke_run_id`` -> a Langfuse
# session). Non-empty + curl present fires it; it does NOT depend on AI_TOOLKIT_TELEMETRY.
OTEL_ENV = "AI_TOOLKIT_OTEL_SPAN_ENDPOINT"

# The forbidden-content needles a payload may carry — the OTLP body is metadata-only.
_SECRET_PAYLOAD = json.dumps(
    {
        "session_id": "sess-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo SECRETCOMMAND123", "file_path": "/x/SECRETPATH/t"},
        "transcript_path": "/home/u/SECRETPATH/transcript.jsonl",
        "tool_use_id": "toolu_ok",
    }
)


def _stub_curl(tmp_path: Path) -> tuple[Path, Path]:
    """Write a stub ``curl`` that records its argv + stdin to a capture file.

    Returns ``(bin_dir, capture)``: prepend ``bin_dir`` to PATH and export
    ``CURL_CAPTURE=capture`` in the child env. The stub redirects nothing of the
    caller's — it only writes the capture — so it never breaks the invisibility test.
    """
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    capture = tmp_path / "curl_capture.txt"
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        '{ printf "ARGV: %s\\n" "$*"; printf "STDIN_START\\n"; cat; '
        'printf "\\nSTDIN_END\\n"; } > "$CURL_CAPTURE"\n'
    )
    stub.chmod(0o755)
    return bin_dir, capture


def _otel_env(
    tmp_path: Path,
    capture_bin: Path,
    capture: Path,
    *,
    endpoint: str | None,
    telemetry_dir: Path | None = None,
    enabled: bool = True,
    payload: str | None = None,
) -> dict[str, str]:
    """An env with the stub curl on PATH (so ``command -v curl`` finds it)."""
    env = _env(telemetry_dir, enabled=enabled, payload=payload)
    env["PATH"] = f"{capture_bin}{os.pathsep}{env['PATH']}"
    env["CURL_CAPTURE"] = str(capture)
    if endpoint is not None:
        env[OTEL_ENV] = endpoint
    return env


def _wait_for_capture(capture: Path, timeout: float = 5.0) -> bool:
    """Poll for the backgrounded curl stub to finish writing its capture file."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if capture.exists() and "STDIN_END" in capture.read_text():
            return True
        time.sleep(0.02)
    return False


def _otlp_body(capture: str) -> dict:
    """Extract + parse the OTLP JSON body the stub captured from curl's stdin."""
    start = capture.index("STDIN_START") + len("STDIN_START")
    end = capture.index("STDIN_END")
    return json.loads(capture[start:end].strip())


def _span(body: dict) -> dict:
    return body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _str_attrs(attributes: list[dict]) -> dict[str, str]:
    """Flatten an OTLP attribute list to {key: stringValue}."""
    return {a["key"]: a["value"]["stringValue"] for a in attributes}


class TestOtlpSinkGate:
    """The OTLP sink is opt-in on the endpoint var and independent of the push sink."""

    def test_no_otlp_call_when_endpoint_unset(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(tmp_path, bin_dir, capture, endpoint=None, telemetry_dir=telemetry_dir)
        result = _emit(
            "--kind step --name solo-cycle --phase green --status success",
            env,
            cwd=project_root,
        )

        assert result.returncode == 0
        assert not _wait_for_capture(capture, timeout=1.0)
        assert not capture.exists()

    def test_events_jsonl_schema_unchanged_when_endpoint_unset(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        # The push sink keeps its exact schema: no OTLP-only keys leak into events.jsonl.
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(tmp_path, bin_dir, capture, endpoint=None, telemetry_dir=telemetry_dir)
        _emit(
            "--kind lifecycle --name worktree-new --phase spawn --status success",
            env,
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert set(span.keys()) == SCHEMA_KEYS

    def test_otlp_fires_when_push_sink_disabled(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        # The key independence guarantee: an AI_TOOLKIT_OTEL spoke gets OTLP spans even
        # when AI_TOOLKIT_TELEMETRY (events.jsonl) is off.
        _seed_spoke(project_root)
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
            enabled=False,
        )
        result = _emit(
            "--kind step --name solo-cycle --phase green --status success",
            env,
            cwd=project_root,
        )

        assert result.returncode == 0
        assert _wait_for_capture(capture)
        assert not (telemetry_dir / "events.jsonl").exists()


class TestOtlpSinkPayload:
    """When fired, the POST carries the proven OTLP shape grouped under the spoke."""

    def test_posts_to_traces_endpoint(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
        )
        _emit("--kind step --name solo-cycle --phase green", env, cwd=project_root)

        assert _wait_for_capture(capture)
        assert "http://localhost:4318/v1/traces" in capture.read_text()

    def test_resource_carries_spoke_run_id_session(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        _seed_spoke(project_root, "feature/83-otel+1700000000")
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
        )
        _emit("--kind step --name solo-cycle --phase green", env, cwd=project_root)

        assert _wait_for_capture(capture)
        body = _otlp_body(capture.read_text())
        res_attrs = _str_attrs(body["resourceSpans"][0]["resource"]["attributes"])
        assert res_attrs["service.name"] == "claude-code"
        assert res_attrs["spoke_run_id"] == "feature/83-otel+1700000000"

    def test_span_name_is_kind_colon_phase(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
        )
        _emit("--kind step --name solo-cycle --phase green --status success", env, cwd=project_root)

        assert _wait_for_capture(capture)
        assert _span(_otlp_body(capture.read_text()))["name"] == "step:green"

    def test_span_name_falls_back_to_name_without_phase(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
        )
        _emit("--kind hook --name secrets-scan --status success", env, cwd=project_root)

        assert _wait_for_capture(capture)
        assert _span(_otlp_body(capture.read_text()))["name"] == "secrets-scan"

    def test_span_attributes_carry_kind_phase_status(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
        )
        _emit("--kind step --name solo-cycle --phase green --status failure", env, cwd=project_root)

        assert _wait_for_capture(capture)
        attrs = _str_attrs(_span(_otlp_body(capture.read_text()))["attributes"])
        assert attrs["workflow.kind"] == "step"
        assert attrs["workflow.phase"] == "green"
        assert attrs["status"] == "failure"

    def test_human_span_carries_decision_and_wait_ms(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        # The proven gate:PLAN node landed with its decision + wait. A human span adds a
        # `decision` attribute (the gate's outcome = status) and `human.wait_ms`.
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
        )
        _emit(
            "--kind human --name plan-gate --phase gate --status success "
            "--human-type gate --human-wait-ms 4200",
            env,
            cwd=project_root,
        )

        assert _wait_for_capture(capture)
        attrs = _str_attrs(_span(_otlp_body(capture.read_text()))["attributes"])
        assert attrs["decision"] == "success"
        assert attrs["human.wait_ms"] == "4200"

    def test_start_ms_drives_span_nanos(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        # start nanos = start_ms * 1e6. 1_700_000_000_000 ms -> 1_700_000_000_000_000_000 ns.
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
        )
        _emit(
            "--kind lifecycle --name worktree-new --phase spawn --start-ms 1700000000000",
            env,
            cwd=project_root,
        )

        assert _wait_for_capture(capture)
        span = _span(_otlp_body(capture.read_text()))
        assert span["startTimeUnixNano"] == "1700000000000000000"
        assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])

    def test_trace_and_span_ids_are_hex(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
        )
        _emit("--kind step --name solo-cycle --phase green", env, cwd=project_root)

        assert _wait_for_capture(capture)
        span = _span(_otlp_body(capture.read_text()))
        assert len(span["traceId"]) == 32 and int(span["traceId"], 16) >= 0
        assert len(span["spanId"]) == 16 and int(span["spanId"], 16) >= 0


class TestOtlpSinkDiscipline:
    """Privacy + invisibility apply to the OTLP body exactly as to events.jsonl."""

    def test_otlp_body_has_no_forbidden_content(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
            payload=_SECRET_PAYLOAD,
        )
        _emit("--kind hook --name secrets-scan.sh --status success", env, cwd=project_root)

        assert _wait_for_capture(capture)
        content = capture.read_text()
        assert "SECRETPATH" not in content
        assert "SECRETCOMMAND123" not in content

    def test_otlp_emit_invisible_and_returns_zero(
        self, project_root: Path, telemetry_dir: Path, tmp_path: Path
    ) -> None:
        bin_dir, capture = _stub_curl(tmp_path)
        env = _otel_env(
            tmp_path,
            bin_dir,
            capture,
            endpoint="http://localhost:4318",
            telemetry_dir=telemetry_dir,
        )
        result = _emit(
            "--kind step --name solo-cycle --phase green --status success",
            env,
            cwd=project_root,
        )

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""
