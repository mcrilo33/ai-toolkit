"""Shared helper functions + constants for the gate-broker test suite (issue #275).

Imported explicitly by tests/unit/conftest.py and each test_gate_broker*.py."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shlex import quote as shlex_quote

import pytest
from bash_session import BashSession, fresh_call

# gate-broker.sh, like hub-afk.sh, targets the macOS control plane (BSD stat / tmux).
pytestmark = pytest.mark.skipif(
    subprocess.run(["stat", "-f", "%m", "."], capture_output=True).returncode != 0,
    reason="gate-broker.sh requires BSD stat (-f %m) and the macOS tmux hub (#129)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_BROKER = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "gate-broker.sh"
WT_LIB = REPO_ROOT / "scripts" / "worktree-lib.sh"
HUB_INJECT = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-inject.sh"
FIXTURES = REPO_ROOT / "tests" / "unit" / "fixtures"


# Source-time resolution keys (issue #276): these are read while gate-broker.sh is being
# SOURCED (to locate its co-located hub-inject.sh / worktree-lib.sh). A call that overrides one
# cannot reuse the already-sourced coprocess (the resolution is baked at source), so it routes
# to a fresh source instead. INVARIANT: any NEW top-level (source-time) env read added to
# gate-broker.sh must be listed here, or a test overriding it would silently get the stale
# baked value.
_FRESH_SOURCE_KEYS = frozenset({"SCRIPT_DIR", "AFK_HUB_INJECT", "AFK_WT_LIB"})

_SESSION: BashSession | None = None


def _session() -> BashSession:
    """The bash coprocess that sources gate-broker.sh once (issue #276), shared across the
    gate-broker test files (each call runs in a fresh subshell, so the parent stays pristine)."""
    global _SESSION
    if _SESSION is None or not _SESSION.alive:
        _SESSION = BashSession(GATE_BROKER)
    return _SESSION


def _call(
    fn_call: str, *, env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke a shell expression against gate-broker.sh's functions.

    Reuses a bash that sources gate-broker.sh ONCE (issue #276) and runs each call in a fresh
    subshell — the multi-thousand-line source cost is paid once, not once per test. A call whose
    env changes SOURCE-TIME resolution (SCRIPT_DIR / AFK_HUB_INJECT / AFK_WT_LIB) routes to a
    fresh source instead.
    """
    if env and _FRESH_SOURCE_KEYS.intersection(env):
        return fresh_call(GATE_BROKER, fn_call, env=env, stdin=stdin)
    return _session().call(fn_call, env=env, stdin=stdin)


# ── issue #261: Tier-3 headless LLM judge (judge_permission) ──────────────────
# The residue tiers 1-2 did not resolve goes to a TOOLLESS headless judge (Haiku, bounded by
# AFK_JUDGE_TIMEOUT), FAIL-CLOSED on timeout/error/unparseable. Only PARSED verdicts are
# cached by command hash; a failure outcome is never cached (#268).


def _judge_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {"AFK_STATE_DIR": str(tmp_path / "afk-state"), "AFK_JOURNAL_GH_COMMENT": "0"}
    env.update(extra)
    return env


# ── issue #268 AC4: N consecutive judge-unavailable outcomes raise a halt ──────
# A dead judge otherwise denies one command at a time, silently, for the whole window.
# After AFK_JUDGE_HALT_STREAK consecutive unavailable outcomes a drain-level flag is
# raised (a file the supervisor reads to pause dispatch, mirroring the answerer
# auth-failure path); a reachable judge clears it so the drain resumes.


def _judge_journal(tmp_path: Path) -> str:
    j = tmp_path / "afk-state" / "decision-journal.jsonl"
    return j.read_text() if j.exists() else ""


# ── the shared orchestrator: broker_service_gate ──────────────────────────────


def _project_dir_for(projects_root: Path, wt_path: Path) -> Path:
    import re

    slug = re.sub(r"[^A-Za-z0-9]", "-", str(wt_path))
    d = projects_root / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ask_record(question: str, options: list[tuple[str, str]]) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "AskUserQuestion",
                    "id": "tu_1",
                    "input": {
                        "questions": [
                            {
                                "question": question,
                                "options": [
                                    {"label": label, "description": desc} for label, desc in options
                                ],
                            }
                        ]
                    },
                }
            ]
        },
    }


# ── the hardened injector submits (no stranded paste) ─────────────────────────


def _write_fake_tmux(
    tmp_path: Path,
    *,
    on_paste: str = ":",
    on_enter: str = ":",
    on_capture: str = ":",
    pane_path: Path | None = None,
) -> Path:
    """Fake tmux encoding inject_answer's key contract (Escape, `send-keys -l --`
    paste, separate Enter): one single-line bash snippet runs per event, capture-pane
    runs on_capture. One builder so every inject test drives the SAME contract —
    divergent inline fakes would let the suite stay green against a stale contract.
    pane_path additionally makes list-panes advertise an afk:1 pane at that path
    (for callers that locate the pane via _spoke_pane_target). Returns the bin dir
    to prepend to PATH.
    """
    list_panes = f'printf "afk:1\\t%s\\n" "{pane_path}"' if pane_path else ":"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  send-keys)\n"
        '    case "$*" in\n'
        f'      *" -l "*) {on_paste} ;;\n'
        f"      *Enter*) {on_enter} ;;\n"
        "    esac ;;\n"
        f"  capture-pane) {on_capture} ;;\n"
        f"  list-panes) {list_panes} ;;\n"
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    return fake_bin


def _inject_env(projects: Path, fake_bin: Path, **extra: str) -> dict[str, str]:
    return {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_INJECT_POLL_SECONDS": "0",
        **extra,
    }


def _seed_transcript(projects: Path, spoke_repo: Path, content: str = "{}\n") -> Path:
    """A spoke session transcript pinned to a stale mtime (any write reads as advance)."""
    jsonl = _project_dir_for(projects, spoke_repo) / "session.jsonl"
    jsonl.write_text(content)
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    return jsonl


def _user_record(answer: str) -> str:
    """What Claude Code appends on submit: the user turn, JSON-encoded raw-UTF-8."""
    return json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": answer}]}},
        ensure_ascii=False,
    )


# ── option (c): the reasoner tool-call audit (#247) ───────────────────────────
# #244 attributed a whole-tree diff by the SPOKE's transcript activity — leaky at the
# edges because the diff carries no evidence of WHO wrote it. #247 keys the void on
# REASONER evidence: an audit of the reasoner's own tool_use stream. The fingerprint
# stays the trigger ("the tree changed"); the audit is the attribution — void iff the
# reasoner PROVABLY wrote the live tree, drop iff it provably did not. When the audit is
# UNAVAILABLE (a plain-text stub, no stream, no python3) it falls back to the #244
# activity signal, so the whole #244 suite stays green unchanged.


def _assistant_tool_use(name: str, cmd_or_input: dict) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": cmd_or_input}]},
        }
    )


def _result_event(text: str) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text})


# ── subtask C: attended QCM surface + interactive per-gate resolver ────────────


def _fake_tmux_pane(fake_bin: Path, wt: Path, jsonl: Path) -> Path:
    """A tmux stub: list-panes maps a pane to <wt>; the submitting Enter advances the
    spoke transcript so inject_and_verify confirms; every send-keys is logged."""
    log = fake_bin / "tmux.log"
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{wt}" ;;\n'
        f'  send-keys) case "$*" in *Enter*) printf "{{}}\\n" >> "{jsonl}" ;; esac ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    return log


# ── subtask D: automatable-decisions log + codification pass ───────────────────


def _bash_tool_record(command: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "id": "tu_1", "input": {"command": command}}
            ]
        },
    }


_PERMISSION_PROMPT = "Bash command\n  git reset -q\nDo you want to proceed?\n❯ 1. Yes\n  2. No"  # noqa: RUF001 (real Claude Code dialog cursor glyph)


# ── issue #164: the reasoner transcript must not pollute the spoke's session ────
#
# Regression from #155-B: the read-only reasoner runs headless `claude` with cwd = the
# spoke's worktree, so ITS OWN session transcript lands in the SAME
# ~/.claude/projects/<munged-wt>/ dir as the spoke's. `_spoke_jsonl` picked the newest
# jsonl there — the answerer's own transcript — so `_still_parked_same` always saw the
# transcript "move", every AFK answer was dropped as stale, and the spoke sat stranded.
#
# These tests drive the behaviour through the DEFAULT reasoner command (they do NOT
# override AFK_ANSWERER_CMD) so they exercise the exact surface the fix changes. A fake
# `claude` on PATH models the real CLI's session persistence: like the real binary it
# writes its own transcript into the project dir for its cwd — UNLESS invoked with
# `--no-session-persistence`, which suppresses the write. So the fix (adding that flag to
# the default reasoner command — option 2, killing the write at source) flips these from
# RED to GREEN exactly as it does for the real CLI; a downstream `_spoke_jsonl` filter
# (option 3) would satisfy them too, since every assertion is on the spoke's resolved
# transcript, not on how the pollution was avoided.


def _install_fake_claude(fake_bin: Path, decision: str) -> None:
    """Install a fake ``claude`` that models real session-transcript persistence.

    It writes its own transcript into the project dir for its cwd (mirroring the real CLI's
    ``<projects>/<munged-cwd>/`` layout, resolved like the broker via ``CLAUDE_PROJECTS_DIR``
    / ``~/.claude/projects``) UNLESS ``--no-session-persistence`` is present, then prints the
    decision. A ``gh`` stub is also installed so ``build_answerer_prompt`` stays hermetic.
    """
    reasoner_record = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "reasoning about the gate"}]},
        }
    )
    (fake_bin / "claude").write_text(
        "#!/usr/bin/env bash\n"
        "persist=1\n"
        'for a in "$@"; do [ "$a" = "--no-session-persistence" ] && persist=0; done\n'
        "cat >/dev/null 2>&1\n"  # consume the reasoner prompt on stdin
        'if [ "$persist" -eq 1 ]; then\n'
        # Guard: never fall back to the real ~/.claude store — a caller that forgets
        # CLAUDE_PROJECTS_DIR must fail loudly here, not pollute the developer's machine.
        '  base="${CLAUDE_PROJECTS_DIR:?fake claude needs CLAUDE_PROJECTS_DIR}"\n'
        "  slug=\"$(pwd | sed 's/[^A-Za-z0-9]/-/g')\"\n"
        '  mkdir -p "$base/$slug"\n'
        f"  printf '%s\\n' '{reasoner_record}' > \"$base/$slug/reasoner-transcript.jsonl\"\n"
        "fi\n"
        f"printf '%s' '{decision}'\n"
    )
    (fake_bin / "claude").chmod(0o755)
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)


# ── issue #271: a FAILED gate emission must not latch a phantom park ───────────
# extract_pending_question latched gate_plan whenever an assistant turn merely CONTAINED a
# `spoke-ready.sh --gate` Bash — success was never checked, so a DENIED emission (the whole
# #271 incident) left slot_state reading `waiting` forever over a busy spoke, and the watchdog
# then answered a park that never existed. The fix ties the latch to the gate tool_use's
# tool_result: an is_error result un-latches it.


def _gate_bash_turn(plan: str, tool_id: str = "tu_gate", issue: int = 5) -> dict:
    """An assistant turn: a prose plan + a `spoke-ready.sh --gate` Bash carrying a tool_use id."""
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": plan},
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "id": tool_id,
                    "input": {
                        "command": (
                            f"bash .ai-toolkit/scripts/spoke-ready.sh --gate {issue} "
                            "--plan-file .ai-toolkit/gate-plan.md"
                        )
                    },
                },
            ]
        },
    }


def _gate_tool_result(tool_id: str = "tu_gate", *, is_error: bool) -> dict:
    """The user turn Claude Code appends for the gate Bash's result (a hook deny → is_error)."""
    block: dict = {"type": "tool_result", "tool_use_id": tool_id, "content": "result"}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"content": [block]}}


def _spoke_activity_turn() -> dict:
    """The spoke's OWN work after a failed emission — an assistant tool_use, no park."""
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "That was denied — investigating the guard instead."},
                {"type": "tool_use", "name": "Read", "id": "tu_r", "input": {"file_path": "x.sh"}},
            ]
        },
    }


def _write_transcript(projects: Path, wt: Path, records: list[dict]) -> None:
    pd = _project_dir_for(projects, wt)
    (pd / "session.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


# ── #180: a spoke waiting on a background task is busy, not idle ───────────────


def _seed_task_output(tasks_root: Path, wt_path: Path, mtime: int) -> Path:
    """Write a harness background-task output file for `wt_path`, pinned to `mtime`.

    Mirrors the live layout <root>/claude-*/<munged-wt>/<session>/tasks/*.output.
    """
    import re

    slug = re.sub(r"[^A-Za-z0-9]", "-", str(wt_path))
    d = tasks_root / "claude-502" / slug / "sess1" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    out = d / "w3cadq3wh.output"
    out.write_text("running the review workflow...\n")
    os.utime(out, (mtime, mtime))
    return out


# ── issue #175: the structured plan artifact replaces transcript extraction ────
# The gate park hands its plan to the broker through a scripted artifact
# (<wt>/.ai-toolkit/gate-<N>.md, written by spoke-ready.sh --gate) rather than the
# transcript heuristic. The gate route PREFERS the artifact when present (transcript
# fallback intact); _consume_gate_tag removes it alongside the tag.


def _gate_park_transcript(plan: str) -> str:
    """A gate-park transcript line: a prose plan + a spoke-ready --gate Bash, no AskUserQuestion."""
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": plan},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {
                                "command": "bash .ai-toolkit/scripts/spoke-ready.sh --gate 5"
                            },
                        },
                    ]
                },
            }
        )
        + "\n"
    )


def _tag_gate_at_head(wt: Path, issue: int) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "tag", "-a", f"gate/{issue}", "-m", "plan"],
        cwd=wt,
        check=True,
        env=env,
        capture_output=True,
    )


def _gate_broker_env(spoke_repo: Path, tmp_path: Path, *, prompt_log: Path) -> dict[str, str]:
    """Env for a gate-parked broker run: transcript plan + a prompt-capturing answerer.

    The answerer (AFK_ANSWERER_CMD) appends the prompt it receives on stdin to
    ``prompt_log`` then ESCALATEs, so a test can assert which plan the broker fed it.
    """
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(_gate_park_transcript("TRANSCRIPT PLAN prose"))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text("#!/usr/bin/env bash\ntrue\n")
    ready_stub.chmod(0o755)

    return {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SPOKE_READY": str(ready_stub),
        "AFK_STATE_DIR": str(tmp_path / "sd"),
        "AFK_ANSWERER_CMD": f"cat >> '{prompt_log}'; printf 'ESCALATE: capture'",
    }


# ── issue #204: consume a stale gate tag when the answer already landed ─────────
# _consume_gate_tag ran ONLY on the broker's confirmed-inject path. An answer that
# registered late, a wedge respawn started outside the broker, or an attended/manual
# reply in the pane left gate/<N> at the tip — re-read as "waiting" and re-answered,
# and (with the #204 guard) wedging the resumed spoke. The broker now self-heals: when
# the transcript shows a genuine user reply AFTER the PLAN-gate park, it consumes the
# stale tag instead of re-answering.


def _resumed_gate_transcript(plan: str) -> str:
    """A gate-park transcript where a TYPED reply already landed after the park."""
    reply = (
        json.dumps(
            {"type": "user", "promptSource": "typed", "message": {"content": "Approved — proceed."}}
        )
        + "\n"
    )
    return _gate_park_transcript(plan) + reply


# ── issue #203 finding 4: compound-command decomposition + in-worktree lane ────
# A confirmation dialog on a COMPOUND command (cd into the worktree, mv a stashed file
# from the scratchpad, chmod +x it, stash pop, targeted pytest) used to be classified as
# one opaque "risky" string and escalated, wedging the whole drain. classify_permission
# now takes the spoke's worktree, decomposes the command, tracks `cd`, and APPROVES writes
# confined to the worktree or its session scratchpad.


def _slug_for(wt: Path) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]", "-", str(wt))


def _scratchpad_for(tasks_root: Path, wt: Path) -> Path:
    """The spoke's session scratchpad under <tasks_root>/claude-*/<munged-wt>/<sess>/."""
    return tasks_root / "claude-77" / _slug_for(wt) / "sess-1" / "scratchpad"


def _classify_with_wt(cmd: str, wt: Path, tasks_root: Path) -> str:
    result = _call(
        'classify_permission "$CMD" "$WT" | cut -f1',
        env={"CMD": cmd, "WT": str(wt), "AFK_TASKS_ROOT": str(tasks_root)},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# ── issue #181: auto-approve read-only Read permissions inside the repo family ──
# A spoke parks on a `Read` permission dialog for a legitimate, write-free research read —
# reading a hub script/hook (#175 parked on Read(<hub>/.git/hooks/pre-push)) or a sibling
# worktree. extract_pending_command now carries the Read target so classify_permission can
# APPROVE a read confined to the repo family (the main root + its worktrees) and ESCALATE a
# secret-like or out-of-family one. Every OTHER non-Bash tool stays default-deny.


def _read_tool_record(file_path: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "id": "tu_r",
                    "input": {"file_path": file_path},
                }
            ]
        },
    }


def _named_tool_record(name: str, tool_input: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": name, "id": "tu_n", "input": tool_input or {}}]
        },
    }


# ── issue #240: extract_pending_command must return the PENDING (unresolved) tool_use ──
# The permission dialog flushes the pending tool_use to the JSONL as an UNRESOLVED block
# (no matching tool_result) for the whole park, while the PRIOR calls are already resolved.
# The old walk kept the last tool_use in file order regardless of resolution, so a spoke
# that parked right after a completed Write surfaced a phantom "Write" and escalated on it.


def _tool_result_record(tool_use_id: str) -> dict:
    # The user turn Claude Code appends when a tool_use completes — its tool_result carries
    # the matching tool_use_id, which is what marks the tool_use RESOLVED.
    return {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}]
        },
    }


_SMOKE_COMPOUND = "chmod +x scripts/dev/afk-gate-smoke.sh && ./scripts/dev/afk-gate-smoke.sh"


# ── issue #269: residual dialog park-detection net (#254 option b) ─────────────
# A permission dialog can still reach a bypass spoke (any future ask rule/plugin/CC
# change outranks the mode — the #238 proof). When it does, the gated tool_use is NOT
# flushed while the dialog is pending (the #240/#254 finding), so extract_pending_command
# is empty. _permission_pending must DECOUPLE detection (the pane) from extraction (the
# command): a shown pane prompt IS a park even with an empty command. The #240 guard still
# holds: a resolved tool with NO pane prompt yields false (no phantom escalation).


def _fake_tmux_capture(fake_bin: Path, wt: Path, pane_text: str) -> None:
    """A tmux stub whose capture-pane prints <pane_text> and whose list-panes maps the
    pane to <wt>, so _pane_shows_permission_prompt observes exactly that pane content."""
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  capture-pane) printf "%s\\n" {shlex_quote(pane_text)} ;;\n'
        f'  list-panes) printf "afk:1\\t%s\\n" {shlex_quote(str(wt))} ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)


def _resolved_only_transcript(pd: Path) -> None:
    # A completed Write with its matching tool_result -> NO unresolved tool_use, so
    # extract_pending_command returns empty (the dialog-pending #240/#254 shape).
    records = [
        _named_tool_record("Write", {"file_path": "scripts/x.sh", "content": "y"}),
        _tool_result_record("tu_n"),
    ]
    (pd / "session.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


# ── issue #241 S2: the reasoner ALWAYS answers (rule <-> fallback policy binding) ──
# The escalate-and-park posture is gone: the reasoner takes even irreversible/outward/
# scope-changing decisions, preferring the reversible in-scope alternative (that IS the
# answer). The governing rule (afk-answering.md) and the built-in fallback policy the broker
# ships when that file is absent must stay in lockstep — a binding test pins them so a future
# edit can't drift one back toward ESCALATE while the other says always-answer.

RULE_FILE = REPO_ROOT / "shared" / "rules" / "afk-answering.md"


# ── issue #241 S3: classify_permission ESCALATE routes to the reasoner + warns ──
# A permission dialog the mechanical classifier will not auto-approve no longer parks the
# spoke as blocked/<issue>. It routes to the always-answering reasoner: APPROVE a safe,
# reversible, in-scope command; DENY an irreversible/destructive one (Esc-cancel the dialog
# and inject the reversible-path guidance) — never auto-approve a destructive command. Either
# way the taken decision is warned + journaled, and the spoke stays serviced (never blocked).


def _perm_env(tmp_path: Path, spoke_repo: Path, command: str, answerer: str) -> dict[str, str]:
    """Park spoke #5 on a permission dialog for <command>, stub the reasoner with <answerer>,
    and record any blocked escalation. A fake tmux logs every send-keys to _KEYLOG and, on an
    Enter, advances the transcript so approve/inject verification can register."""
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_bash_tool_record(command)) + "\n")
    keylog = tmp_path / "keys.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  send-keys)\n"
        f'    printf "%s\\n" "$*" >> "{keylog}"\n'
        f'    case "$*" in *Enter*) printf "{{}}\\n" >> "{jsonl}" ;; esac ;;\n'
        f'  capture-pane) printf "%s\\n" "{_PERMISSION_PROMPT}" ;;\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    statedir = tmp_path / "sd"
    statedir.mkdir(exist_ok=True)
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    gh = fake_bin / "gh"
    gh.write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    gh.chmod(0o755)
    return {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_STATE_DIR": str(statedir),
        "SPOKE_READY": str(ready_stub),
        "AFK_ANSWERER_CMD": answerer,
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_INJECT_POLL_SECONDS": "0",
        "_KEYLOG": str(keylog),
        "_READY_LOG": str(ready_log),
        "_STATEDIR": str(statedir),
    }


# ── issue #253: programmatic PreToolUse permission decision ───────────────────
# The pane-scrape answering path detects+operates a TUI dialog AFTER it appears — the brittle
# surface behind the #240/#246/#238 bug family. afk_permission_hook_decide moves the COMMON
# case OFF the pane: a spoke PreToolUse hook runs classify_permission on the gated tool call
# and AUTO-APPROVES a benign scoped self-op (permissionDecision:"allow"), so no dialog is ever
# shown. It reuses the SAME classify_permission verdict (one source of truth), journals the
# approve per #241, and NEVER denies — an ESCALATE (or any un-gated context) stays silent so the
# scope-guard hooks' denies stay authoritative and the pane path is untouched.


def _hook_payload(
    tool_name: str, wt: Path, *, command: str | None = None, file_path: str | None = None
) -> str:
    """Build a Claude Code PreToolUse payload as the hook receives it on stdin."""
    inp: dict[str, str] = {}
    if command is not None:
        inp["command"] = command
    if file_path is not None:
        inp["file_path"] = file_path
    return json.dumps({"tool_name": tool_name, "tool_input": inp, "cwd": str(wt)})


def _run_hook(payload: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _call("afk_permission_hook_decide", env=env, stdin=payload)


AFK_PERMISSION_HOOK = REPO_ROOT / "shared" / "hooks" / "afk-permission-hook.sh"


# ── issue #261: the PreToolUse deny-wall (afk_danger_guard_decide) ────────────
# Under bypassPermissions an afk spoke raises NO dialog, so this PreToolUse hook IS the safety
# boundary. It runs classify_danger (Tier 2, deny-first) -> classify_permission (Tier 1, allow)
# -> judge_permission (Tier 3), emitting permissionDecision:"deny" for boundary crossings and the
# judge-dangerous residue. Gated on an issue-numbered spoke branch AND the fail-safe mode gate
# (.ai-toolkit/mode: afk/ambiguous -> ACTIVE, positively-attended -> INERT).

DANGER_GUARD_HOOK = REPO_ROOT / "shared" / "hooks" / "afk-danger-guard.sh"


def _decide(payload: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _call("afk_danger_guard_decide", env=env, stdin=payload)


def _perm(stdout: str) -> str:
    stdout = stdout.strip()
    if not stdout:
        return "(silent)"
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]
