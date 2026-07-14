"""REASON-stage tests (gate-broker-answerer.sh, #275 partition).

See shared/skills/hub/scripts/gate-broker-answerer.sh.
"""

import json
import os
import subprocess
from pathlib import Path
from shlex import quote as shlex_quote

import pytest
from _gate_broker_support import (
    _PERMISSION_PROMPT,
    FIXTURES,
    RULE_FILE,
    _ask_record,
    _assistant_tool_use,
    _bash_tool_record,
    _call,
    _fake_tmux_pane,
    _gate_park_transcript,
    _perm_env,
    _project_dir_for,
    _result_event,
    _tag_gate_at_head,
)


@pytest.fixture(autouse=True)
def _isolated_afk_state(tmp_path, monkeypatch):
    """Pin the state dir so no test touches the real hub state (mirrors test_gate_broker)."""
    monkeypatch.setenv("AFK_STATE_DIR", str(tmp_path / "afk-state"))
    monkeypatch.setenv("AFK_HEARTBEAT", str(tmp_path / "afk-heartbeat"))


ANSWERER_SURFACE = (
    "reasoner_allowed_tools",
    "assert_readonly_tools",
    "read_decisions_digest",
    "log_decision",
    "codify_decisions",
    "broker_journal_decision",
    "broker_warn_continue",
    "build_answerer_prompt",
    "run_answerer",
    "parse_decision",
    "parse_decision_field",
    "is_auth_failure",
)


def test_answerer_module_surface_loads() -> None:
    # The answerer module's public surface must resolve after the entry lib sources it — proof
    # the fail-closed module source loop wired gate-broker-answerer.sh in (a missing module
    # would leave run_answerer / parse_decision undefined and the drain could not reason).
    fns = " ".join(ANSWERER_SURFACE)
    result = _call(
        f'for fn in {fns}; do command -v "$fn" >/dev/null || {{ echo "missing: $fn"; exit 1; }}; done; echo OK'
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"


def test_parse_decision_extracts_answer() -> None:
    # A representative parse lands identically through the split module.
    result = _call("parse_decision 'reasoning here\nANSWER: use Redis'")

    assert result.returncode == 0, result.stderr
    kind, _, text = result.stdout.strip().partition("\t")
    assert kind == "ANSWER"
    assert text == "use Redis"


# ── subtask B: read-only-worktree reasoner + evidence + mutation guard ─────────


def test_reasoner_allowed_tools_are_read_only() -> None:
    # The reasoner runs the code-review/Explore posture: Read/Grep/Glob (+ a narrow
    # read-only git helper), NEVER Edit/Write/NotebookEdit. The guard rejects any
    # mutating tool or a bare unrestricted Bash.
    tools = _call("reasoner_allowed_tools")
    assert tools.returncode == 0, tools.stderr
    listed = tools.stdout.strip()
    assert "Read" in listed and "Grep" in listed and "Glob" in listed
    for banned in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        assert banned not in listed, f"{banned} must not be in the reasoner allowlist: {listed}"

    ok = _call('assert_readonly_tools "$(reasoner_allowed_tools)"; echo RC=$?')
    assert ok.stdout.strip().splitlines()[-1] == "RC=0", ok.stdout + ok.stderr

    for bad in ("Read,Write", "Read,Bash", "Edit"):
        rej = _call(f'assert_readonly_tools "{bad}"; echo RC=$?', env={})
        assert rej.stdout.strip().splitlines()[-1] == "RC=1", f"{bad} must be rejected"


@pytest.mark.parametrize(
    "spec,rc",
    [
        ("Bash(git status:*)", "0"),  # a read-only git verb is allowed
        ("Bash(git diff)", "0"),
        ("Bash(git push:*)", "1"),  # a scoped-but-MUTATING git verb must be rejected
        ("Bash(git commit:*)", "1"),
        ("Bash(git reset:*)", "1"),
        ("Bash(rm -rf /)", "1"),  # a scoped Bash must not smuggle arbitrary commands
    ],
)
def test_assert_readonly_tools_vets_scoped_bash_verb(spec: str, rc: str) -> None:
    result = _call(f'assert_readonly_tools "{spec}"; echo RC=$?', env={})
    assert result.stdout.strip().splitlines()[-1] == f"RC={rc}", f"{spec}: {result.stdout}"


def test_worktree_fingerprint_detects_deletion(spoke_repo: Path) -> None:
    (spoke_repo / "keep.txt").write_text("data")
    subprocess.run(["git", "add", "keep.txt"], cwd=spoke_repo, check=True, capture_output=True)
    fp1 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()

    (spoke_repo / "keep.txt").unlink()
    fp2 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()

    assert fp1 and fp2 != fp1, "deleting a tracked file must change the fingerprint"


def test_broker_service_gate_escalates_when_fingerprint_unavailable(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # Fail-safe: a git worktree whose fingerprint comes back empty (tooling absent) can't
    # be verified read-only, so the gate escalates rather than trusting the answer. Force
    # the empty fingerprint by overriding the fingerprint fn after sourcing.
    env = {**waiting_spoke_env, "AFK_ANSWERER_CMD": "printf 'ANSWER: go ahead'"}

    result = _call(
        "_broker_worktree_fingerprint() { printf ''; }; "
        f"broker_service_gate '{spoke_repo}' 5 unattended",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    _rl = Path(env["_READY_LOG"])
    log = _rl.read_text() if _rl.exists() else ""
    assert "--blocked 5" not in log, f"unverifiable read-only must warn-and-continue: {log}"
    assert "WARNING: #5" in result.stderr, result.stderr
    assert "fingerprint" in result.stderr.lower(), result.stderr


def test_worktree_fingerprint_tracks_only_tracked_content(spoke_repo: Path) -> None:
    # A content fingerprint of the TRACKED worktree content (issue #168): deterministic
    # across a no-op, UNCHANGED by a parked spoke's own untracked runtime writes (a
    # still-finishing push gate's `.testmondata`, OTel dumps under `.ai-toolkit/` — the
    # false-positive that burned three healthy reasoner runs), and changed ONLY by a
    # content edit of a tracked file.
    (spoke_repo / "a.txt").write_text("one")
    subprocess.run(["git", "add", "a.txt"], cwd=spoke_repo, check=True, capture_output=True)
    fp1 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    fp1b = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    assert fp1 and fp1 == fp1b, "fingerprint must be deterministic"

    (spoke_repo / ".testmondata").write_text("push-gate coverage db")
    (spoke_repo / ".testmondata-shm").write_text("wal")
    (spoke_repo / ".ai-toolkit" / "raw-bodies").mkdir(parents=True)
    (spoke_repo / ".ai-toolkit" / "raw-bodies" / "dump.json").write_text("{}")
    fp2 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    assert fp2 == fp1, "untracked spoke-runtime writes must NOT drift the fingerprint"

    (spoke_repo / "a.txt").write_text("one-edited")
    fp3 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    assert fp3 != fp1, "a content edit of a tracked file must change the fingerprint"


def test_worktree_fingerprint_detects_untracked_creation(spoke_repo: Path) -> None:
    # #203 finding 2: a reasoner that CREATES a brand-new untracked-not-ignored file mutates
    # the worktree. The tracked-only fingerprint (#168) missed it — the read-only DETECTION
    # layer must catch it. Untracked-not-ignored (`--others --exclude-standard`) closes the
    # gap while the ignored runtime artifacts (the #168 false-positive class) stay excluded.
    fp1 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()

    (spoke_repo / "reasoner_new.py").write_text("print('created by the reasoner')\n")
    fp2 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()

    assert fp1 and fp2 != fp1, "a new untracked-not-ignored file must change the fingerprint"


def test_broker_service_gate_voids_answer_when_reasoner_creates_file(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # The read-only guard must void an answer when the reasoner CREATES a new untracked file
    # (not just when it edits tracked content): a creation is a mutation of a read-only tree,
    # so the gate escalates rather than trusting the answer (#203 finding 2). Since #237 runs
    # the reasoner in an isolated copy, the write here targets the ABSOLUTE live-tree path —
    # modelling an isolation BYPASS the fingerprint backstop must still catch.
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": f"printf 'x' > '{spoke_repo}/reasoner_new.py'; printf 'ANSWER: go ahead'",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    _rl = Path(env["_READY_LOG"])
    log = _rl.read_text() if _rl.exists() else ""
    assert "--blocked 5" not in log, f"a voided file-creation must warn-and-continue: {log}"
    assert "WARNING: #5" in result.stderr, result.stderr
    assert "worktree" in result.stderr.lower() or "mutat" in result.stderr.lower(), result.stderr


def test_reasoner_runs_in_isolated_copy_not_live_tree(spoke_repo: Path, tmp_path: Path) -> None:
    # Write isolation (#237): the reasoner is seeded with cwd = a THROWAWAY COPY of the
    # worktree, NOT $wt itself, so a tool that ignores the read-only allowlist writes into
    # the copy — never the live tree — while its reads still see the worktree's content.
    # A write to cwd + a read of a committed file prove both halves.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    real = subprocess.run(
        ["bash", "-c", f"cd '{spoke_repo}' && pwd -P"], capture_output=True, text=True
    ).stdout.strip()

    result = _call(
        f"run_answerer 5 'q' '{spoke_repo}'",
        env={
            "AFK_ANSWERER_CMD": "printf x > escaped_probe.txt; cat .gitignore; pwd -P",
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert ".testmondata" in result.stdout, (
        f"the copy must mirror the worktree's committed content: {result.stdout}"
    )
    assert not (spoke_repo / "escaped_probe.txt").exists(), (
        "a reasoner write must land in the copy, never the live tree"
    )
    assert result.stdout.strip().splitlines()[-1] != real, (
        f"the reasoner's cwd must be an isolated copy, not the live worktree: {result.stdout}"
    )


def test_broker_service_gate_voids_answer_when_reasoner_mutates_tracked_content(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # The read-only guard, narrowed to TRACKED content (#168): a reasoner that mutates a
    # tracked file has its answer VOIDED and the gate escalated (unattended) — a tracked
    # mutation is never trusted, even alongside a plausible ANSWER. (Untracked runtime
    # drift no longer voids — see test_broker_service_gate_injects_despite_runtime_drift.)
    # Since #237 runs the reasoner in an isolated copy, the write targets the ABSOLUTE
    # live-tree path — an isolation BYPASS the fingerprint backstop must still catch.
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": f"printf 'mutated' > '{spoke_repo}/tracked.txt'; printf 'ANSWER: go ahead'",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    _rl = Path(env["_READY_LOG"])
    log = _rl.read_text() if _rl.exists() else ""
    assert "--blocked 5" not in log, f"a voided tracked mutation must warn-and-continue: {log}"
    assert "WARNING: #5" in result.stderr, result.stderr
    assert "worktree" in result.stderr.lower() or "mutat" in result.stderr.lower(), result.stderr


def test_broker_service_gate_no_void_when_spoke_self_resumes_during_reasoning(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # #244: since #237 the reasoner runs in an isolated snapshot copy, so a LIVE-tree diff
    # during run_answerer is almost always the SPOKE's OWN concurrent edits — it self-resumed
    # mid-GREEN — not the reasoner. The read-only void attributes a tree diff to the reasoner
    # ONLY when NO genuine spoke turn landed during the step. Here the spoke self-resumes: the
    # answerer CMD both edits the live tree AND appends the spoke's own assistant tool_use (an
    # Edit) to the live transcript — a genuine spoke turn — so the diff is the spoke's. The stale
    # answer is dropped, NOT voided: no gate-voided marker, no blocked tag. Contrast the :750
    # backstop, where the spoke stays idle (no turn appended) and the same absolute write DOES void.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    live_jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(live_jsonl, (1_000_000_000, 1_000_000_000))  # pin OLD (mtime is irrelevant to the fix)
    # The spoke's own assistant work: a tool_use record (it ran Edit) — genuine spoke activity.
    resumed = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {}}]},
        }
    )
    env = {
        **waiting_spoke_env,
        # Model the spoke self-resuming DURING the reason step: it edits its own tracked file (a
        # live-tree diff the fingerprint sees) AND appends its own assistant turn to the transcript.
        "AFK_ANSWERER_CMD": (
            f"printf 'edited by the spoke' > '{spoke_repo}/tracked.txt'; "
            f"printf '%s\\n' '{resumed}' >> '{live_jsonl}'; "
            "printf 'ANSWER: go ahead'"
        ),
        "AFK_STATE_DIR": str(statedir),
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert not (statedir / "gate-voided-5").exists(), (
        "the spoke's own concurrent edit must not be voided as a reasoner mutation"
    )
    log = Path(env["_READY_LOG"]).read_text() if Path(env["_READY_LOG"]).exists() else ""
    assert "--blocked 5" not in log, f"a self-resumed spoke must not be blocked: {log}"
    assert "voiding its answer" not in result.stderr, (
        f"a self-resumed spoke's edit must not read as a reasoner void: {result.stderr}"
    )


def test_broker_service_gate_voids_masked_escape_no_spoke_activity(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # #244 review finding 1: a genuine reasoner escape (absolute-path live-tree write) that
    # coincides with a #240 NON-TURN transcript bump must STILL void — a mtime bump alone must not
    # mask the breach. The answerer writes the live tree by absolute path AND `touch`es the pinned-
    # old jsonl (a non-turn bump, NOT a spoke turn), while the spoke stays parked. No genuine spoke
    # activity landed, so the diff is the reasoner's: void + escalate, never a silent drop.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    live_jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(live_jsonl, (1_000_000_000, 1_000_000_000))
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": (
            f"printf 'escaped' > '{spoke_repo}/tracked.txt'; "
            f"touch '{live_jsonl}'; printf 'ANSWER: go ahead'"
        ),
        "AFK_STATE_DIR": str(statedir),
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert (statedir / "gate-voided-5").exists(), (
        "an escape masked by a non-turn mtime bump must still mint the void marker"
    )
    assert "voiding its answer" in result.stderr, result.stderr


def test_broker_service_gate_voids_commit_escape_on_gate_parked_spoke(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # #244 review finding 2: a reasoner escape that COMMITS to a GATE-parked spoke's live worktree
    # moves HEAD off the gate tag. Keying the void on _still_parked_same (which folds in the gate
    # tag) would route this to the silent DROP branch; keying on genuine spoke activity does not —
    # the commit leaves no spoke turn in the transcript, so the HEAD-moving escape still voids.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(_gate_park_transcript("PLAN prose"))
    os.utime(pd / "session.jsonl", (1_000_000_000, 1_000_000_000))
    _tag_gate_at_head(spoke_repo, 5)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{tmp_path}/ready.log"\n')
    ready_stub.chmod(0o755)
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "SPOKE_READY": str(ready_stub),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_STATE_DIR": str(statedir),
        "AFK_JOURNAL_GH_COMMENT": "0",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        # The reasoner escapes isolation and commits to the LIVE tree, moving HEAD off gate/5.
        "AFK_ANSWERER_CMD": (
            f"git -C '{spoke_repo}' commit --allow-empty -q -m 'chore: sneaky'; "
            "printf 'ANSWER: go ahead'"
        ),
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert (statedir / "gate-voided-5").exists(), (
        "a HEAD-moving commit-escape on a gate-parked spoke must still void, not silently drop"
    )
    assert "voiding its answer" in result.stderr, result.stderr


def test_spoke_activity_appended_classifies_turns(spoke_repo: Path, tmp_path: Path) -> None:
    # The #244 void discriminator: rc 0 when a genuine spoke turn (assistant tool_use / typed
    # reply) appended, rc 1 when only a non-turn write did, rc 2 when the transcript is unreadable.
    # The void gate treats BOTH rc 1 and rc 2 as a breach (fail SAFE), so rc 2 must be distinct.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    env = {"CLAUDE_PROJECTS_DIR": str(projects)}

    jsonl.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}}
        )
        + "\n"
    )
    activity = _call(f"_spoke_activity_appended '{spoke_repo}' ''; echo RC=$?", env=env)
    assert activity.stdout.strip().splitlines()[-1] == "RC=0", "an assistant tool_use is activity"

    jsonl.write_text(  # a synthetic tool_result user record — a #240 non-turn write, not a turn
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result"}]}}) + "\n"
    )
    non_turn = _call(f"_spoke_activity_appended '{spoke_repo}' ''; echo RC=$?", env=env)
    assert non_turn.stdout.strip().splitlines()[-1] == "RC=1", "a non-turn write is not activity"

    missing = _call(
        f"_spoke_activity_appended '{spoke_repo}' ''; echo RC=$?",
        env={"CLAUDE_PROJECTS_DIR": str(tmp_path / "nonexistent")},
    )
    assert missing.stdout.strip().splitlines()[-1] == "RC=2", (
        "an unreadable transcript is rc 2 (unavailable) — the void gate voids on it, fail-safe"
    )

    # A record whose `message` is a non-dict must not crash the scanner (would surface as rc 2).
    jsonl.write_text(json.dumps({"type": "assistant", "message": "oops-a-string"}) + "\n")
    malformed = _call(f"_spoke_activity_appended '{spoke_repo}' ''; echo RC=$?", env=env)
    assert malformed.stdout.strip().splitlines()[-1] == "RC=1", (
        "a non-dict message must be skipped as non-activity, never crash the scan into rc 2"
    )

    # Truncation guard: activity mode must NOT from-0 rescan (which would match the PRE-park
    # AskUserQuestion — itself an assistant tool_use — and mask a real escape). Feed a `sizes`
    # snapshot claiming a larger offset than the file holds, so the truncation branch fires.
    jsonl.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Ask"}]}}
        )
        + "\n"
    )
    inflated = f"999999\t{jsonl}"
    truncated = _call(f"_spoke_activity_appended '{spoke_repo}' '{inflated}'; echo RC=$?", env=env)
    assert truncated.stdout.strip().splitlines()[-1] == "RC=1", (
        "activity mode must skip a truncated file, not from-0 match the pre-park record (fail-safe)"
    )


def test_reasoner_wrote_live_tree_classifies_tool_calls(spoke_repo: Path) -> None:
    # The #247 attribution primitive: rc 0 when the reasoner's tool_use stream shows a LIVE-tree
    # write (a write tool under $wt, or a mutating Bash referencing the absolute $wt path); rc 1
    # when the stream is present but shows NO live write (a definite "the reasoner didn't write");
    # rc 2 when the input is not an auditable stream (a plain-text stub → fall back to #244).
    wt = spoke_repo

    def rc(raw: str) -> str:
        r = _call(
            '_reasoner_wrote_live_tree "$RAW" "$WT"; echo RC=$?', env={"RAW": raw, "WT": str(wt)}
        )
        return r.stdout.strip().splitlines()[-1]

    # rc 0 — a write tool whose absolute path is under $wt (an isolation escape).
    assert (
        rc(
            _assistant_tool_use("Write", {"file_path": f"{wt}/x.py"})
            + "\n"
            + _result_event("ANSWER: ok")
        )
        == "RC=0"
    )
    # rc 0 — a Bash that mutates the absolute $wt path.
    assert (
        rc(
            _assistant_tool_use("Bash", {"command": f"printf x > {wt}/tracked.txt"})
            + "\n"
            + _result_event("ANSWER: ok")
        )
        == "RC=0"
    )
    # rc 1 — a RELATIVE write lands in the #237 snapshot copy, never the live tree — not a breach.
    assert (
        rc(
            _assistant_tool_use("Write", {"file_path": "relative.py"})
            + "\n"
            + _result_event("ANSWER: ok")
        )
        == "RC=1"
    )
    # rc 1 — a read-only `git -C $wt status` references $wt but cannot mutate it — not a breach.
    assert (
        rc(
            _assistant_tool_use("Bash", {"command": f"git -C {wt} status"})
            + "\n"
            + _result_event("ANSWER: ok")
        )
        == "RC=1"
    )
    # rc 1 — a read-only inspection PIPED to a pager references $wt but writes nothing (a bare pipe
    # is not a mutation metachar), so it must not spuriously void a valid answer (review finding 3).
    assert (
        rc(
            _assistant_tool_use("Bash", {"command": f"git -C {wt} log | head"})
            + "\n"
            + _result_event("ANSWER: ok")
        )
        == "RC=1"
    )
    # rc 1 — a SIBLING worktree whose path merely shares $wt as a string prefix is NOT the live tree;
    # a bare-substring match would misclassify it as an escape (review finding 2).
    assert (
        rc(
            _assistant_tool_use("Bash", {"command": f"cat {wt}-2/notes.txt"})
            + "\n"
            + _result_event("ANSWER: ok")
        )
        == "RC=1"
    )
    # rc 0 — a read-only verb CHAINED to a write of $wt must still be caught: the metachar guard
    # keeps a compound from smuggling a mutation past a leading read verb.
    assert (
        rc(
            _assistant_tool_use("Bash", {"command": f"git -C {wt} status && rm {wt}/tracked.txt"})
            + "\n"
            + _result_event("ANSWER: ok")
        )
        == "RC=0"
    )
    # rc 2 — a plain-text stub carries no auditable stream → unavailable → the caller falls back.
    assert rc("reasoning\nANSWER: go ahead") == "RC=2"


def test_reasoner_wrote_live_tree_resolves_symlinked_path(spoke_repo: Path, tmp_path: Path) -> None:
    # A live-tree write whose absolute path reaches $wt through a symlink alias must still be caught
    # (review finding 5): path_under_wt compares the symlink-resolved form on both sides.
    alias = tmp_path / "alias"
    alias.symlink_to(spoke_repo)  # alias/x.py resolves to spoke_repo/x.py
    r = _call(
        '_reasoner_wrote_live_tree "$RAW" "$WT"; echo RC=$?',
        env={
            "RAW": _assistant_tool_use("Write", {"file_path": f"{alias}/x.py"})
            + "\n"
            + _result_event("ANSWER: ok"),
            "WT": str(spoke_repo),
        },
    )
    assert r.stdout.strip().splitlines()[-1] == "RC=0", (
        "a write via a symlink alias of $wt must be caught"
    )


def test_answerer_output_normalization_reads_real_stream_json() -> None:
    # #247 CRITICAL: the stream-json → final-text extraction is a SINGLE normalization step whose
    # output feeds parse_decision, parse_decision_field (REVERSIBILITY/WARN) AND is_auth_failure —
    # else the #241 reversibility class + WARN silently drop to empty under stream-json. Pinned
    # against a REAL captured `--output-format stream-json --verbose` sample so the result-event
    # shape we extract from can't silently drift.
    sample = (FIXTURES / "answerer_stream_sample.jsonl").read_text()

    norm = _call('_normalize_answerer_output "$RAW"', env={"RAW": sample}).stdout
    assert "ANSWER: hello from the stream sample" in norm
    assert "REVERSIBILITY: reversible" in norm
    assert "WARN: nothing to check" in norm

    dec = _call('parse_decision "$(_normalize_answerer_output "$RAW")"', env={"RAW": sample})
    kind, _, text = dec.stdout.strip().partition("\t")
    assert kind == "ANSWER" and text == "hello from the stream sample", dec.stdout

    rev = _call(
        'parse_decision_field "$(_normalize_answerer_output "$RAW")" REVERSIBILITY',
        env={"RAW": sample},
    )
    assert rev.stdout.strip() == "reversible", rev.stdout
    warn = _call(
        'parse_decision_field "$(_normalize_answerer_output "$RAW")" WARN', env={"RAW": sample}
    )
    assert warn.stdout.strip() == "nothing to check", warn.stdout

    # A plain-text stub (the #244 answerer stubs) passes through — its DECISION lines are preserved.
    passthrough = _call(
        '_normalize_answerer_output "$RAW"', env={"RAW": "reasoning\nANSWER: go ahead"}
    ).stdout
    assert "ANSWER: go ahead" in passthrough

    # Fallback shape (review finding 6): if the CLI ever emits NO result event, the answer is still
    # recovered from the assistant `text` blocks — real claude emits both, so the answer survives a
    # drift in either shape.
    assistant_only = (
        json.dumps({"type": "system", "subtype": "init", "model": "m"})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "REVERSIBILITY: reversible\nANSWER: from assistant",
                        }
                    ]
                },
            }
        )
    )
    norm2 = _call('_normalize_answerer_output "$RAW"', env={"RAW": assistant_only})
    kind2, _, text2 = (
        _call('parse_decision "$(_normalize_answerer_output "$RAW")"', env={"RAW": assistant_only})
        .stdout.strip()
        .partition("\t")
    )
    assert "ANSWER: from assistant" in norm2.stdout, norm2.stdout
    assert kind2 == "ANSWER" and text2 == "from assistant", norm2.stdout


def test_broker_service_gate_voids_reasoner_escape_coincident_with_spoke_activity(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # #247 acceptance (residual 1): a real reasoner isolation-escape (absolute-path live-tree
    # write) that lands in the SAME window as a GENUINE spoke turn is attributed WHOLLY to the
    # spoke by #244 and dropped — no void, no triage marker. Keying on the reasoner's own
    # tool_use audit closes it: the audit sees the live-tree write, so it VOIDS even amid the
    # coincident spoke activity.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    live_jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(live_jsonl, (1_000_000_000, 1_000_000_000))
    # The spoke self-resumes concurrently: its own assistant tool_use — genuine #244 activity.
    resumed = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {}}]},
        }
    )
    tool_event = _assistant_tool_use(
        "Bash", {"command": f"printf mutated > {spoke_repo}/tracked.txt"}
    )
    env = {
        **waiting_spoke_env,
        # The reasoner escapes isolation (an absolute-path live write, on stdout as a stream-json
        # tool_use AND performed for real so the fingerprint changes) WHILE the spoke self-resumes.
        "AFK_ANSWERER_CMD": (
            f"printf 'escaped' > '{spoke_repo}/tracked.txt'; "
            f"printf '%s\\n' '{tool_event}'; "
            f"printf '%s\\n' '{_result_event('ANSWER: go ahead')}'; "
            f"printf '%s\\n' '{resumed}' >> '{live_jsonl}'"
        ),
        "AFK_STATE_DIR": str(statedir),
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert (statedir / "gate-voided-5").exists(), (
        "a reasoner escape coincident with genuine spoke activity must VOID (residual 1 closed)"
    )
    assert "voiding its answer" in result.stderr, result.stderr


def test_broker_service_gate_no_void_when_reasoner_clean_and_only_tool_result(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # #247 acceptance (residual 2): a self-resuming spoke whose ONLY appended record is a
    # synthetic tool_result (its tool_use flushed just before the parked_sizes snapshot) reads as
    # NO activity under #244 → false-void. Keying on the reasoner's own audit closes it: the audit
    # shows the reasoner made NO live-tree write, so the diff is the spoke's → DROP, never void.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    live_jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(live_jsonl, (1_000_000_000, 1_000_000_000))
    tool_result = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result"}]}})
    read_event = _assistant_tool_use("Read", {"file_path": "README.md"})
    env = {
        **waiting_spoke_env,
        # The spoke edits its own tracked file (a live-tree diff) but its self-resume leaves only a
        # tool_result appended; the reasoner's audit stream shows only a clean READ (no live write).
        "AFK_ANSWERER_CMD": (
            f"printf 'edited by the spoke' > '{spoke_repo}/tracked.txt'; "
            f"printf '%s\\n' '{read_event}'; "
            f"printf '%s\\n' '{_result_event('ANSWER: go ahead')}'; "
            f"printf '%s\\n' '{tool_result}' >> '{live_jsonl}'"
        ),
        "AFK_STATE_DIR": str(statedir),
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert not (statedir / "gate-voided-5").exists(), (
        "a clean reasoner audit (no live write) must DROP the stale answer, never void (residual 2)"
    )
    assert "voiding its answer" not in result.stderr, result.stderr


def test_broker_service_gate_voids_unmodelled_escape_when_spoke_silent(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # #247 review finding 1 (the fail-safe): a reasoner escape via a vector the audit does NOT model
    # (its stream shows only a clean READ) that changes the live tree while the spoke is TOTALLY
    # SILENT must still VOID — a clean audit must not be trusted alone to DROP an unattributable
    # change. The audit returns rc 1 (stream, no modelled write); no transcript record is appended,
    # so the fail-safe voids (the restored #244 "unconfirmed change => VOID").
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    live_jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(live_jsonl, (1_000_000_000, 1_000_000_000))  # pinned old; the spoke appends NOTHING
    read_event = _assistant_tool_use("Read", {"file_path": "README.md"})
    env = {
        **waiting_spoke_env,
        # The tree changes (absolute write) but the reasoner's stream shows only a Read and the spoke
        # appends no record — an unmodelled escape coincident with a silent spoke.
        "AFK_ANSWERER_CMD": (
            f"printf 'escaped' > '{spoke_repo}/tracked.txt'; "
            f"printf '%s\\n' '{read_event}'; "
            f"printf '%s\\n' '{_result_event('ANSWER: go ahead')}'"
        ),
        "AFK_STATE_DIR": str(statedir),
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert (statedir / "gate-voided-5").exists(), (
        "a clean-audit change the spoke cannot be shown to have made must VOID (fail-safe, finding 1)"
    )
    assert "voiding its answer" in result.stderr, result.stderr


def test_broker_service_gate_isolates_reasoner_writes_from_live_tree(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # Write isolation headline (#237): a reasoner that writes a TRACKED file via a RELATIVE
    # path (its cwd) leaves $wt byte-for-byte unchanged — the write lands in the throwaway
    # copy, not the live tree — so the healthy answer (approving an in-tree op) INJECTS and
    # the gate does NOT escalate. Contrast the two backstop tests, which write the ABSOLUTE
    # live-tree path and still escalate.
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add tracked"],
        cwd=spoke_repo,
        check=True,
        env=git_env,
        capture_output=True,
    )
    fake_bin = tmp_path / "bin"  # the waiting_spoke_env fake bin (holds gh); add tmux
    jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # pin old so the inject's append advances it
    tmux_log = _fake_tmux_pane(fake_bin, spoke_repo, jsonl)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "printf 'mutated' > tracked.txt; printf 'ANSWER: yes, the in-tree chmod is fine'",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert (spoke_repo / "tracked.txt").read_text() == "original", (
        "the reasoner's write must land in the copy — the live tree must be byte-for-byte unchanged"
    )
    ready_log = Path(env["_READY_LOG"])
    ready_text = ready_log.read_text() if ready_log.exists() else ""
    assert "--blocked" not in ready_text, (
        f"isolation must not escalate a healthy answer: {ready_text}"
    )
    assert "chmod is fine" in tmux_log.read_text(), (
        f"the healthy answer must inject despite the in-copy write: {tmux_log.read_text()}"
    )


def test_broker_service_gate_injects_despite_runtime_drift(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # Issue #168 headline regression: a parked spoke's own push gate writes `.testmondata`
    # during the reason step. That untracked runtime drift must NOT void a healthy answer —
    # the guard only cares about tracked content. The answer INJECTS; the gate does NOT
    # escalate to blocked.
    fake_bin = tmp_path / "bin"  # the waiting_spoke_env fake bin (holds gh); add tmux
    jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # pin old so the inject's append advances it
    tmux_log = _fake_tmux_pane(fake_bin, spoke_repo, jsonl)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "printf x > .testmondata; printf 'ANSWER: use Redis'",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    ready_log = Path(env["_READY_LOG"])
    ready_text = ready_log.read_text() if ready_log.exists() else ""
    assert "--blocked" not in ready_text, f"untracked runtime drift must not escalate: {ready_text}"
    assert "use Redis" in tmux_log.read_text(), (
        f"the healthy answer must inject despite the .testmondata write: {tmux_log.read_text()}"
    )


def test_snapshot_isolates_linked_worktree_refs_from_shared_gitdir(
    linked_spoke_repo: Path, tmp_path: Path
) -> None:
    # #239 headline: a linked worktree's `.git` is a gitfile still pointing at the SHARED
    # common gitdir, so a git write-verb inside the #237 snapshot copy (which cp -R'd the
    # gitfile verbatim) resolves to the REAL refs and mutates them. The private-gitdir
    # snapshot must isolate them: a reasoner `git commit --allow-empty` + `git update-ref`
    # inside the copy leaves the live worktree's HEAD and branch tip byte-for-byte unchanged.
    wt = linked_spoke_repo

    def _rev(ref: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", ref], cwd=wt, capture_output=True, text=True
        ).stdout.strip()

    head_before, branch_before = _rev("HEAD"), _rev("feature/x")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    result = _call(
        f"run_answerer 5 'q' '{wt}'",
        env={
            "AFK_ANSWERER_CMD": (
                "git commit --allow-empty -q -m 'chore: sneaky'; "
                "git update-ref refs/heads/feature/x HEAD; printf 'ANSWER: ok'"
            ),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _rev("HEAD") == head_before, (
        "a reasoner git write in the copy must not move the live linked-worktree HEAD"
    )
    assert _rev("feature/x") == branch_before, (
        "a reasoner git write in the copy must not rewrite the live branch tip"
    )


def test_broker_service_gate_voids_answer_when_reasoner_mutates_refs(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # Defense-in-depth backstop (#239), parallel to the tracked-content void at :703: a
    # reasoner ref write to the LIVE $wt (absolute-path bypass of the snapshot) is now
    # DETECTED by the ref-covering fingerprint, so broker_service_gate voids the answer and
    # escalates to blocked/<issue> — the content-only fingerprint used to miss it entirely.
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": (
            f"git -C '{spoke_repo}' commit --allow-empty -q -m 'chore: sneaky'; "
            "printf 'ANSWER: go ahead'"
        ),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    _rl = Path(env["_READY_LOG"])
    log = _rl.read_text() if _rl.exists() else ""
    assert "--blocked 5" not in log, f"a voided ref write must warn-and-continue: {log}"
    assert "WARNING: #5" in result.stderr, result.stderr
    assert "worktree" in result.stderr.lower() or "mutat" in result.stderr.lower(), result.stderr


def test_fingerprint_immune_to_sibling_ref_changes(linked_spoke_repo: Path) -> None:
    # #239 review: the fingerprint folds in only THIS worktree's HEAD, NOT `git for-each-ref`.
    # A linked worktree shares the ref namespace, so ordinary concurrent /afk-drain activity (a
    # sibling spoke's branch, a hub auto-land advancing main) must NOT flip the spoke's
    # fingerprint and terminally false-void a correct answer. Only a ref write that moves THIS
    # worktree's own HEAD counts.
    wt = linked_spoke_repo
    main = wt.parent / "main"
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    fp1 = _call(f"_broker_worktree_fingerprint '{wt}'").stdout.strip()

    # a sibling branch appears in the SHARED gitdir — models a concurrent drain sibling
    subprocess.run(
        ["git", "branch", "feature/sibling"], cwd=main, check=True, env=git_env, capture_output=True
    )
    fp2 = _call(f"_broker_worktree_fingerprint '{wt}'").stdout.strip()
    assert fp1 and fp2 == fp1, "a sibling ref change must not drift the spoke's fingerprint"

    # but a commit on the spoke's OWN branch (moves HEAD) MUST change the fingerprint
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "chore: local"],
        cwd=wt,
        check=True,
        env=git_env,
        capture_output=True,
    )
    fp3 = _call(f"_broker_worktree_fingerprint '{wt}'").stdout.strip()
    assert fp3 != fp1, "a ref write that moves the spoke's own HEAD must change the fingerprint"


def test_snapshot_falls_back_to_copy_when_private_gitdir_fails(
    linked_spoke_repo: Path, tmp_path: Path
) -> None:
    # #239 review: if _broker_private_gitdir fails, the snapshot must STILL run the reasoner in a
    # copy — a partial private $dest/.git is never a pointer to the shared common dir, so write
    # isolation holds — rather than silently reverting to running against the LIVE tree.
    wt = linked_spoke_repo
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    real = subprocess.run(
        ["bash", "-c", f"cd '{wt}' && pwd -P"], capture_output=True, text=True
    ).stdout.strip()

    result = _call(
        f"_broker_private_gitdir() {{ return 1; }}; run_answerer 5 'q' '{wt}'",
        env={
            "AFK_ANSWERER_CMD": "printf x > escaped_probe.txt; pwd -P",
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert not (wt / "escaped_probe.txt").exists(), (
        "a private-gitdir failure must not drop the reasoner into the live tree"
    )
    assert result.stdout.strip().splitlines()[-1] != real, (
        f"the reasoner's cwd must stay an isolated copy on private-gitdir failure: {result.stdout}"
    )


def test_reasoner_prompt_has_readonly_posture_and_evidence(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    result = _call(
        "build_answerer_prompt 5 'Which store?' '/some/worktree'",
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    low = result.stdout.lower()
    assert "read-only" in low, "the prompt must state the reasoner has read-only worktree access"
    assert "evidence" in low, "the prompt must ask the reasoner to cite worktree evidence"
    assert "prior gate decisions" in low or "decisions-digest" in low, "digest section missing"
    # #239 secondary facet: post-snapshot the reasoner's cwd is a throwaway COPY, so the
    # prompt must NOT disclose the live-tree absolute path (which invited an absolute-path
    # write into the real $wt) and must point cwd at the copy instead.
    assert "/some/worktree" not in result.stdout, (
        "the prompt must not disclose the live worktree's absolute path"
    )
    assert "copy" in low, "the prompt must describe the reasoner's cwd as a throwaway copy"


def test_read_decisions_digest_reflects_prior_outcomes(spoke_repo: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "statedir"
    statedir.mkdir()
    # The decisions log line format shared with subtask D's writer:
    # <ts>\t<issue>\t<gate_type>\t<signature>\t<decision>
    (statedir / "decisions.log").write_text(
        "1700000000\t5\tpermission\tgit-reset-self-stage\tAPPROVE\n"
        "1700000001\t9\tplan\tsome-other\tANSWER\n"
    )
    env = {"AFK_STATE_DIR": str(statedir)}

    hit = _call("read_decisions_digest 5", env=env)
    assert hit.returncode == 0, hit.stderr
    assert "git-reset-self-stage" in hit.stdout, hit.stdout
    assert "some-other" not in hit.stdout, "digest must be scoped to this spoke's issue"

    miss = _call("read_decisions_digest 5", env={"AFK_STATE_DIR": str(tmp_path / "empty")})
    assert miss.returncode == 0, miss.stderr
    assert miss.stdout.strip() == "", "no log ⇒ empty digest (D populates it)"


@pytest.mark.parametrize(
    "cmd",
    [
        "git reset -q; git add tests/x.py",
        "git reset HEAD -- tests/other.py; git add tests/other.py",
        "git reset;   git add a/b/c.py",
    ],
)
def test_decision_signature_collides_across_arg_variation(cmd: str) -> None:
    # The signature normalises a command to its verb skeleton so recurrences of the SAME
    # shape (different files/flags) collide into one automatable signature.
    result = _call('_broker_decision_signature permission "$CMD"', env={"CMD": cmd})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "git-reset+git-add", result.stdout


def test_log_decision_appends_tsv_and_digest_reflects(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1700000000"}

    r = _call('log_decision 5 permission "git reset -q; git add tests/x.py" APPROVE', env=env)
    assert r.returncode == 0, r.stderr

    line = (statedir / "decisions.log").read_text().strip()
    fields = line.split("\t")
    assert fields[1] == "5" and fields[2] == "permission"
    assert fields[3] == "git-reset+git-add" and fields[4] == "APPROVE"

    # The B-subtask reader consumes exactly this format.
    digest = _call("read_decisions_digest 5", env={"AFK_STATE_DIR": str(statedir)})
    assert "git-reset+git-add" in digest.stdout and "APPROVE" in digest.stdout


def test_codify_proposes_rule_for_recurring_unanimous_signature(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    log = statedir / "decisions.log"
    # The #149 git-reset self-stage case: the same signature auto-approved twice.
    log.write_text(
        "1\t5\tpermission\tgit-reset+git-add\tAPPROVE\n"
        "2\t7\tpermission\tgit-reset+git-add\tAPPROVE\n"
        "3\t9\tpermission\tgit-push+origin\tESCALATE\n"  # single occurrence → no rule
        "4\t11\tpermission\tgit-clean\tAPPROVE\n"  # conflicting decisions → no rule
        "5\t12\tpermission\tgit-clean\tESCALATE\n"
    )
    env = {"AFK_STATE_DIR": str(statedir)}

    result = _call("codify_decisions 2", env=env)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "git-reset+git-add" in out and "APPROVE" in out, out
    assert "git-push+origin" not in out, "a single occurrence must not become a rule"
    assert "git-clean" not in out, "a conflicting signature must not become a rule"


def test_decide_permission_logs_auto_approve(spoke_repo: Path, tmp_path: Path) -> None:
    # Integration: the #149 git-reset self-stage auto-approve is recorded to the
    # automatable-decisions log with its signature, so codify can later graduate it.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_bash_tool_record("git reset -q; git add tests/x.py")) + "\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = fake_bin / "tmux.log"
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{tmux_log}"\n'
        'case "$1" in\n'
        f'  capture-pane) printf "%s\\n" "{_PERMISSION_PROMPT}" ;;\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        f'  send-keys) case "$*" in *Enter*) printf "{{}}\\n" >> "{jsonl}" ;; esac ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_STATE_DIR": str(statedir),
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    log = statedir / "decisions.log"
    assert log.exists(), "a safe auto-approve must be logged"
    fields = log.read_text().strip().split("\t")
    assert fields[2] == "permission" and fields[3] == "git-reset+git-add" and fields[4] == "APPROVE"


def test_run_answerer_does_not_pollute_spoke_jsonl(
    spoke_repo: Path, reasoner_env: dict[str, str]
) -> None:
    # After the reasoner runs, the spoke's OWN transcript must still be the one
    # `_spoke_jsonl` resolves — not the reasoner's fresh transcript.
    result = _call(
        f"run_answerer 5 'q' '{spoke_repo}' >/dev/null; _spoke_jsonl '{spoke_repo}'",
        env=reasoner_env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == reasoner_env["_SPOKE_JSONL"], (
        f"_spoke_jsonl must resolve the spoke's own transcript, not the reasoner's: {result.stdout}"
    )


def test_still_parked_same_survives_reasoner_transcript(
    spoke_repo: Path, reasoner_env: dict[str, str]
) -> None:
    # `_still_parked_same` must judge freshness against the spoke's transcript alone: a
    # reasoner write during the reason step is NOT the spoke moving on. Snapshot the clock,
    # run the reasoner (which writes its own transcript), then assert the spoke still reads
    # as parked on the same question.
    question = "Q: Which store?\n  - Redis: fast"

    result = _call(
        f"before=\"$(_transcript_mtime '{spoke_repo}')\"; "
        f"run_answerer 5 'q' '{spoke_repo}' >/dev/null; "
        f'_still_parked_same \'{spoke_repo}\' 5 0 "$QUESTION" "$before"; echo RC=$?',
        env={**reasoner_env, "QUESTION": question},
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=0", (
        f"a reasoner write must not make the spoke read as 'moved on': {result.stdout}{result.stderr}"
    )


def test_extract_pending_question_ignores_reasoner_transcript(
    spoke_repo: Path, reasoner_env: dict[str, str]
) -> None:
    # The reasoner transcript carries no AskUserQuestion; if `extract_pending_question`
    # read it instead of the spoke's, the park would vanish. It must keep returning the
    # spoke's real question after the reasoner runs.
    result = _call(
        f"run_answerer 5 'q' '{spoke_repo}' >/dev/null; extract_pending_question '{spoke_repo}'",
        env=reasoner_env,
    )

    assert "Which store?" in result.stdout, (
        f"extract_pending_question must read the spoke's transcript, not the reasoner's: {result.stdout}"
    )


# ── issue #171: harden the answer path (freshness, timeouts, classifier gaps) ──


# subtask 1: the reasoner is bounded so a hung headless claude never freezes the tick ──


def test_run_answerer_delegates_to_shared_timeout(spoke_repo: Path, tmp_path: Path) -> None:
    # In production hub-afk.sh defines _afk_with_timeout (which tree-kills a wedged grandchild
    # so it can't hold run_answerer's capture pipe open); run_answerer must REUSE it and pass
    # the configured AFK_ANSWERER_TIMEOUT seconds, not roll its own bound. A stub echoes the
    # seconds it was handed and runs the command, proving both the delegation and the budget.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    result = _call(
        '_afk_with_timeout() { echo "BOUND=$1"; shift; "$@"; }; run_answerer 5 \'q\'',
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AFK_ANSWERER_TIMEOUT": "42",
            "AFK_ANSWERER_CMD": "printf 'ANSWER: ok'",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "BOUND=42" in result.stdout, (
        f"must delegate to _afk_with_timeout with the budget: {result.stdout}"
    )
    assert "ANSWER: ok" in result.stdout, (
        f"the answerer must still run under the bound: {result.stdout}"
    )


def test_answerer_timeout_rejects_zero_budget() -> None:
    # AFK_ANSWERER_TIMEOUT=0 (or non-numeric) must not disable the bound — `timeout 0` and
    # perl `alarm 0` both mean "no limit". _afk_answerer_timeout falls back to the default.
    for spec in ("0", "00", "abc", ""):
        got = _call("_afk_answerer_timeout", env={"AFK_ANSWERER_TIMEOUT": spec}).stdout.strip()
        assert got == "900", f"AFK_ANSWERER_TIMEOUT={spec!r} must fall back to 900, got {got}"
    ok = _call("_afk_answerer_timeout", env={"AFK_ANSWERER_TIMEOUT": "30"}).stdout.strip()
    assert ok == "30", ok


def test_broker_service_gate_escalates_when_answerer_times_out(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # A timed-out reasoner (the bound returns nonzero with no output) is a "no decision":
    # the gate must escalate to blocked/<issue> — the existing fail-safe — never hang.
    env = {**waiting_spoke_env, "AFK_ANSWERER_CMD": "printf 'ANSWER: should never run'"}

    result = _call(
        f"_afk_with_timeout() {{ return 124; }}; broker_service_gate '{spoke_repo}' 5 unattended",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    _rl = Path(env["_READY_LOG"])
    log = _rl.read_text() if _rl.exists() else ""
    assert "--blocked 5" not in log, f"a timed-out answerer must warn-and-continue: {log}"
    assert "WARNING: #5" in result.stderr, result.stderr
    assert "no decision" in result.stderr.lower(), result.stderr


def test_run_answerer_standalone_fallback_bounds_a_slow_answerer(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # Sourced standalone (no hub-afk _afk_with_timeout) on a coreutils-less host, the
    # self-contained fallback (perl alarm) must still bound the reasoner: a slow answerer is
    # killed before it prints, and run_answerer returns nonzero (→ no decision → escalate).
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    result = _call(
        "run_answerer 5 'q'; echo RC=$?",
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AFK_ANSWERER_TIMEOUT": "1",
            "AFK_ANSWERER_CMD": "sleep 5; printf 'ANSWER: too late'",
        },
    )

    assert "too late" not in result.stdout, f"a slow answerer must be killed first: {result.stdout}"
    assert result.stdout.strip().splitlines()[-1] != "RC=0", (
        "a timed-out answerer must return nonzero"
    )


# subtask 2: a stale ESCALATE / no-decision must not strand an actively-working spoke ──


def test_broker_service_gate_drops_escalation_when_spoke_moves_on(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The answerer takes minutes; if the spoke moved on meanwhile (a human replied, the turn
    # resumed) an ESCALATE / no-decision must be DROPPED with a log, never stamped as a
    # spurious blocked/<N> on an actively-working spoke. Model "moved on" by having the
    # answerer advance the spoke's own transcript mid-reason, then escalate.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # pin old so the reasoner write advances it
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SPOKE_READY": str(ready_stub),
        # The reasoner bumps the spoke transcript (a human reply landed) then escalates.
        "AFK_ANSWERER_CMD": f"printf '{{}}\\n' >> '{jsonl}'; printf 'ESCALATE: needs a human'",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    ready_text = ready_log.read_text() if ready_log.exists() else ""
    assert "--blocked" not in ready_text, f"a moved-on spoke must not be escalated: {ready_text}"
    assert "dropping the escalation" in result.stderr.lower(), result.stderr


def test_spoke_moved_on_requires_a_confirmed_advance(spoke_repo: Path, tmp_path: Path) -> None:
    # The escalation gate must fail SAFE: it drops a real escalation ONLY on a demonstrated
    # transcript advance, never on an ambiguous probe (an empty/garbage baseline). Otherwise a
    # transient stat miss would silently swallow a blocked/<N> and strand the spoke unsurfaced.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    env = {"CLAUDE_PROJECTS_DIR": str(projects)}

    def moved_on(before: str) -> str:
        out = _call(f"_spoke_moved_on '{spoke_repo}' '{before}'; echo RC=$?", env=env)
        return out.stdout.strip().splitlines()[-1]

    assert moved_on("1000000000") == "RC=1", "unchanged mtime is not movement"
    os.utime(jsonl, (1_000_000_050, 1_000_000_050))
    assert moved_on("1000000000") == "RC=0", "a strictly newer write is movement"
    assert moved_on("") == "RC=1", "an empty baseline is not confident movement (fail safe)"
    assert moved_on("nope") == "RC=1", (
        "a non-numeric baseline is not confident movement (fail safe)"
    )


# subtask 3: blocked-at-tip over a still-parked spoke reads as waiting, not terminal ──


def test_slot_state_blocked_at_tip_with_pending_question_is_waiting(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A spurious blocked/<N> over a spoke still parked on a question must NOT read as terminal
    # 'done' (which stranded it — never re-answered, never reaped). With an extractable pending
    # question it reads 'waiting' (re-answerable); reconcile clears the tag once commits land.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n"
    )
    subprocess.run(
        ["git", "tag", "-f", "blocked/5"], cwd=spoke_repo, check=True, capture_output=True
    )

    result = _call(f"slot_state '{spoke_repo}' 5", env={"CLAUDE_PROJECTS_DIR": str(projects)})

    assert result.stdout.strip() == "waiting", result.stdout + result.stderr


def test_slot_state_blocked_at_tip_without_pending_stays_done(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The terminal reading is preserved when the spoke is NOT parked: a genuine blocked/<N>
    # with no extractable question/permission is still 'done'.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}
        )
        + "\n"
    )
    subprocess.run(
        ["git", "tag", "-f", "blocked/5"], cwd=spoke_repo, check=True, capture_output=True
    )

    result = _call(f"slot_state '{spoke_repo}' 5", env={"CLAUDE_PROJECTS_DIR": str(projects)})

    assert result.stdout.strip() == "done", result.stdout + result.stderr


# ── issue #241: decision journal + warn-and-continue foundation ────────────────
# The /afk answerer now ALWAYS answers: every former terminal stop site takes the best
# action, WARNS loudly, journals the decision, and parks the spoke LAST on an exponential
# backoff instead of abandoning it. These pin the shared primitives every converted site
# builds on: the decision journal, the loud warn record, the warn-continue seam (which must
# NOT emit a blocked marker), and the backoff that gates re-service.


def test_broker_journal_decision_appends_structured_line(tmp_path: Path) -> None:
    import json as _json

    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {
        "AFK_STATE_DIR": str(statedir),
        "AFK_NOW": "1700000000",
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    r = _call(
        "broker_journal_decision 41 permission 'denied force-push; use a new branch' irreversible",
        env=env,
    )
    assert r.returncode == 0, r.stderr

    line = (statedir / "decision-journal.jsonl").read_text().strip()
    rec = _json.loads(line)
    assert rec["issue"] == "41"
    assert rec["park"] == "permission"
    assert rec["reversibility"] == "irreversible"
    assert "force-push" in rec["decision"]


def test_broker_journal_decision_posts_issue_comment(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh_log = tmp_path / "gh.log"
    gh = bindir / "gh"
    gh.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "' + str(gh_log) + '"\n')
    gh.chmod(0o755)
    env = {
        "AFK_STATE_DIR": str(statedir),
        "PATH": f"{bindir}:{os.environ['PATH']}",
    }

    r = _call("broker_journal_decision 41 gate 'approved the plan' reversible", env=env)
    assert r.returncode == 0, r.stderr
    # The journal posts a per-decision issue comment (the morning post-review surface, #241 §10).
    assert gh_log.exists(), "no gh call recorded"
    assert "issue comment 41" in gh_log.read_text()


def test_broker_warn_writes_record_and_logs_warning(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1700000000"}

    r = _call("broker_warn 41 'took the reversible alternative'", env=env)
    assert r.returncode == 0, r.stderr

    assert "WARNING" in r.stderr and "#41" in r.stderr
    rec = (statedir / "warned-41.txt").read_text().strip()
    assert rec.split("\t")[0] == "1700000000"
    assert "reversible alternative" in rec


def test_broker_warn_continue_does_not_block(spoke_repo: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {"AFK_STATE_DIR": str(statedir), "AFK_JOURNAL_GH_COMMENT": "0"}

    r = _call(
        f"broker_warn_continue '{spoke_repo}' 41 permission 'denied; use reversible path' irreversible",
        env=env,
    )
    assert r.returncode == 0, r.stderr

    # Warn-and-continue NEVER escalates: a warned record exists, a journal line exists,
    # but NO durable blocked record is written (the difference from _escalate_blocked).
    assert (statedir / "warned-41.txt").exists()
    assert (statedir / "decision-journal.jsonl").exists()
    assert not (statedir / "blocked-41.txt").exists(), "warn-continue must not block the spoke"


def test_warned_backoff_gates_retry_and_grows(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {
        "AFK_STATE_DIR": str(statedir),
        "AFK_WARN_BACKOFF_BASE": "60",
        "AFK_WARN_BACKOFF_CAP": "1800",
    }

    r = _call(
        "( export AFK_NOW=1000; _afk_warned_arm 41 ); "
        "_afk_warned_due 41 1000 && echo A-DUE || echo A-WAIT; "
        "_afk_warned_due 41 1060 && echo B-DUE || echo B-WAIT; "
        "( export AFK_NOW=1060; _afk_warned_arm 41 ); "
        "_afk_warned_due 41 1100 && echo C-DUE || echo C-WAIT; "
        "_afk_warned_due 41 1180 && echo D-DUE || echo D-WAIT",
        env=env,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "A-WAIT" in out, out  # within the base 60s backoff → parked LAST
    assert "B-DUE" in out, out  # 60s elapsed → due for re-service
    assert "C-WAIT" in out, out  # second warn doubled the backoff to 120s; only 40s elapsed
    assert "D-DUE" in out, out  # 120s elapsed → due again


def test_broker_journal_decision_escapes_control_chars(tmp_path: Path) -> None:
    import json as _json

    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {"AFK_STATE_DIR": str(statedir), "AFK_JOURNAL_GH_COMMENT": "0"}

    # A decision built from captured tool output can carry a CR / control byte; the journal
    # must still be valid JSONL a strict parser accepts (the record advertises "structured").
    r = _call(
        "broker_journal_decision 41 permission \"$(printf 'denied\\rforce-push\\tfoo')\" irreversible",
        env=env,
    )
    assert r.returncode == 0, r.stderr

    raw = (statedir / "decision-journal.jsonl").read_text()
    assert "\r" not in raw, "raw CR must not survive into the journal line"
    rec = _json.loads(raw.strip())  # must parse — control chars neutralized
    assert "force-push" in rec["decision"]


def test_clear_warned_records_resets_window(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"}

    r = _call(
        "broker_warn 41 'w'; _afk_warned_arm 41; "
        "broker_warn 42 'w'; _afk_warned_arm 42; "
        "_afk_clear_warned 41; "  # one-issue clear (a genuine progress signal)
        "_clear_warned_records",  # full window reset
        env=env,
    )
    assert r.returncode == 0, r.stderr
    # Both the human-facing record and the backoff bookkeeping are gone after the resets.
    assert not (statedir / "warned-41.txt").exists()
    assert not (statedir / "warned-state-41").exists()
    assert not (statedir / "warned-42.txt").exists()
    assert not (statedir / "warned-state-42").exists()


# ── issue #274: the warned-retry backoff is lane-scoped ───────────────────────
# A single per-issue backoff file let the ANSWER lane's re-answer ceiling pace the LAND
# lane (auto_land), silently starving the land of a ready spoke (#269). The backoff is now
# keyed per (issue, lane): the default (empty) lane keeps the historical
# "warned-state-<issue>" name for the answer/service lane; auto_land arms + reads a distinct
# "land" lane so the two never leak into one another.


def test_warned_backoff_lanes_are_independent(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {
        "AFK_STATE_DIR": str(statedir),
        "AFK_WARN_BACKOFF_BASE": "1000000",  # arm to the far future so an armed lane reads WAIT
        "AFK_NOW": "1000",
    }

    # Arming ONE lane must leave the OTHER lane untouched (still due).
    r = _call(
        "_afk_warned_arm 5 land; "  # arm ONLY the land lane
        "_afk_warned_due 5 1000 '' && echo D-DUE || echo D-WAIT; "  # default lane never armed
        "_afk_warned_due 5 1000 land && echo L-DUE || echo L-WAIT; "  # land lane parked
        "_afk_warned_arm 7 ''; "  # arm ONLY the default (answer) lane
        "_afk_warned_due 7 1000 land && echo L7-DUE || echo L7-WAIT; "  # land lane never armed
        "_afk_warned_due 7 1000 '' && echo D7-DUE || echo D7-WAIT",  # default lane parked
        env=env,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "D-DUE" in out, out  # a land-lane arm does not pace the default lane
    assert "L-WAIT" in out, out
    assert "L7-DUE" in out, out  # an answer-lane arm does not pace the land lane (#269 root)
    assert "D7-WAIT" in out, out


def test_clear_warned_drops_both_lanes(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    # A genuine-progress clear must wipe EVERY lane's backoff (the answer lane AND the land
    # lane), so a stale record in either can never keep pacing a spoke that has moved on.
    (statedir / "warned-state-5").write_text("3\t2000\n")
    (statedir / "warned-state-5-land").write_text("2\t2000\n")
    (statedir / "warned-5.txt").write_text("1000\tstuck\n")

    r = _call("_afk_clear_warned 5", env={"AFK_STATE_DIR": str(statedir)})

    assert r.returncode == 0, r.stderr
    assert not (statedir / "warned-state-5").exists(), "default lane cleared"
    assert not (statedir / "warned-state-5-land").exists(), "land lane cleared too (#274)"
    assert not (statedir / "warned-5.txt").exists(), "human record cleared"


def test_warned_next_reports_the_lane_due_epoch(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {
        "AFK_STATE_DIR": str(statedir),
        "AFK_WARN_BACKOFF_BASE": "60",
        "AFK_NOW": "1000",
    }
    # The auto_land skip log names the next-due epoch so a paced land is visible, not silent.
    r = _call("_afk_warned_arm 5 land; _afk_warned_next 5 land", env=env)
    assert r.stdout.strip() == "1060", r.stdout + r.stderr
    # A never-armed lane reports nothing (no crash, empty line).
    r2 = _call("_afk_warned_next 6 land", env={"AFK_STATE_DIR": str(statedir)})
    assert r2.returncode == 0 and r2.stdout.strip() == "", r2.stdout + r2.stderr


def test_warned_lane_maps_land_and_review_to_the_land_lane() -> None:
    # auto_land is the only land-lane consumer: its own park kinds (land, review) pace the LAND
    # lane; every other kind stays on the default (answer/service) lane.
    r = _call(
        'printf "[%s]\\n" "$(_afk_warned_lane land)"; '
        'printf "[%s]\\n" "$(_afk_warned_lane review)"; '
        'printf "[%s]\\n" "$(_afk_warned_lane reap)"; '
        'printf "[%s]\\n" "$(_afk_warned_lane answer)"'
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("[land]") == 2, r.stdout  # land + review
    assert r.stdout.count("[]") == 2, r.stdout  # reap + answer → default lane (empty)


def test_land_lane_cap_stays_below_the_watchdog_land_ceiling() -> None:
    # #274 AC4: the land lane caps its backoff below HUB_WATCHDOG_LAND_CEILING (900s) so a done
    # spoke's land is always re-attempted before the watchdog escalates. Non-land kinds use the
    # default cap (empty override → _afk_warned_arm's AFK_WARN_BACKOFF_CAP).
    r = _call(
        'printf "[%s]\\n" "$(_afk_warned_lane_cap land)"; '
        'printf "[%s]\\n" "$(_afk_warned_lane_cap reap)"'
    )
    assert r.returncode == 0, r.stderr
    lines = [ln.strip("[]") for ln in r.stdout.splitlines() if ln.strip()]
    assert lines[0].isdigit() and int(lines[0]) < 900, r.stdout  # land cap < 900s ceiling
    assert lines[1] == "", r.stdout  # non-land → no override


def test_land_lane_cap_rejects_a_non_numeric_override(spoke_repo: Path, tmp_path: Path) -> None:
    # #274 review (CONFIRMED): a typo'd AFK_LAND_BACKOFF_CAP (e.g. "15m") must clamp to the LAND
    # default (600s) at the lane, NOT fall through to _afk_warned_arm's 1800s answer default — else
    # the land backoff would exceed the 900s watchdog ceiling and re-invert the fix.
    r = _call(
        'printf "[%s]\\n" "$(_afk_warned_lane_cap land)"',
        env={"AFK_LAND_BACKOFF_CAP": "15m"},
    )
    assert "[600]" in r.stdout, r.stdout + r.stderr

    # End-to-end: an arm through broker_warn_continue with a bad cap env still caps at 600, so even
    # a high attempt count cannot push the next-due past 600s past now.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "warned-state-5-land").write_text("20\t0\n")  # pre-seed a high attempt count
    _call(
        f"broker_warn_continue '{spoke_repo}' 5 land x reversible",
        env={
            "AFK_STATE_DIR": str(statedir),
            "AFK_JOURNAL_GH_COMMENT": "0",
            "AFK_WARN_BACKOFF_BASE": "60",
            "AFK_LAND_BACKOFF_CAP": "nonsense",
            "AFK_NOW": "1000",
        },
    )
    nxt = int((statedir / "warned-state-5-land").read_text().split("\t")[1])
    assert nxt <= 1000 + 600, f"a bad land cap env must clamp to 600, not 1800; got {nxt - 1000}s"


def test_broker_warn_continue_routes_land_park_to_the_land_lane(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A land-park warn (auto_land's land-failure / retry-exhausted path) must arm the LAND lane
    # ONLY — never the default lane an answerer reads — so the two pacing clocks stay separate.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {
        "AFK_STATE_DIR": str(statedir),
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_WARN_BACKOFF_BASE": "60",
        "AFK_NOW": "1000",
    }

    _call(f"broker_warn_continue '{spoke_repo}' 5 land 'land failed' reversible", env=env)

    assert (statedir / "warned-state-5-land").exists(), "a land-park warn arms the LAND lane"
    assert not (statedir / "warned-state-5").exists(), "it must NOT arm the default (answer) lane"


def test_default_answerer_policy_binds_to_rule_file() -> None:
    policy = _call("_default_answerer_policy").stdout
    rule = RULE_FILE.read_text()

    # The output token ESCALATE: is retired from BOTH surfaces — the reasoner never emits it.
    assert "ESCALATE:" not in policy, "the fallback policy must not instruct an ESCALATE output"
    assert "ESCALATE:" not in rule, "the rule must not instruct an ESCALATE output"
    # Both instruct the single ANSWER: output and the REVERSIBILITY: reversibility-class line.
    for surface, name in ((policy, "fallback policy"), (rule, "rule file")):
        low = surface.lower()
        assert "ANSWER:" in surface, f"{name} must instruct the ANSWER output line"
        assert "REVERSIBILITY:" in surface, f"{name} must instruct the REVERSIBILITY class line"
        # A DISTINCTIVE phrase, not the bare "reversible" (which matches inside "irreversible"
        # and would pass even if the prefer-reversible instruction were deleted).
        assert "reversible, in-scope" in low, f"{name} must state the prefer-reversible posture"
        # WARN must fire for all four risk classes, in lockstep across the surfaces.
        for cls in ("irreversible", "outward", "scope"):
            assert cls in low, f"{name} must name the {cls} risk class for WARN"


def test_answerer_prompt_instructs_answer_only(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    out = _call(
        "build_answerer_prompt 5 'Which store?' '/some/worktree'",
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    ).stdout

    assert "ANSWER:" in out, "the prompt must instruct the reasoner to end with ANSWER:"
    assert "ESCALATE:" not in out, "the always-answer prompt must not offer an ESCALATE output"


def test_parse_decision_field_extracts_reversibility_and_warn() -> None:
    raw = "reasoning\nREVERSIBILITY: irreversible\nWARN: took a critical call\nANSWER: deny; rebase instead"

    rev = _call(f"parse_decision_field {shlex_quote(raw)} REVERSIBILITY").stdout.strip()
    warn = _call(f"parse_decision_field {shlex_quote(raw)} WARN").stdout.strip()
    dec = _call(f"parse_decision {shlex_quote(raw)}").stdout.strip()

    assert rev == "irreversible", rev
    assert warn == "took a critical call", warn
    # parse_decision still extracts the ANSWER decision unchanged.
    kind, _, text = dec.partition("\t")
    assert kind == "ANSWER" and text == "deny; rebase instead", dec


def test_permission_escalate_reasoner_approve_injects_yes_and_warns(
    spoke_repo: Path, tmp_path: Path
) -> None:
    env = _perm_env(
        tmp_path,
        spoke_repo,
        "npm run deploy",  # unrecognised -> classify ESCALATE
        "printf 'REVERSIBILITY: reversible\\nANSWER: APPROVE'",
    )

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    keys = Path(env["_KEYLOG"]).read_text() if Path(env["_KEYLOG"]).exists() else ""
    statedir = Path(env["_STATEDIR"])
    # The reasoner approved -> the "Yes" (option 1) keystroke was delivered.
    assert any(line.split()[-1] == "1" for line in keys.splitlines()), keys
    # Taken decision is warned + journaled, and the spoke is NEVER blocked.
    assert (statedir / "warned-5.txt").exists()
    assert (statedir / "decision-journal.jsonl").exists()
    ready = Path(env["_READY_LOG"])
    assert not ready.exists() or "--blocked 5" not in ready.read_text()


def test_permission_escalate_reasoner_deny_cancels_and_redirects(
    spoke_repo: Path, tmp_path: Path
) -> None:
    destructive = "git reset --hard origin/main"  # irreversible -> must be denied
    env = _perm_env(
        tmp_path,
        spoke_repo,
        destructive,
        "printf 'REVERSIBILITY: irreversible\\nANSWER: DENY: do not hard-reset; create a backup branch first'",
    )

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    keys = Path(env["_KEYLOG"]).read_text() if Path(env["_KEYLOG"]).exists() else ""
    statedir = Path(env["_STATEDIR"])
    # Deny cancels the dialog (Escape) and never sends the bare "Yes" (option 1).
    assert "Escape" in keys, keys
    assert not any(line.split()[-1] == "1" for line in keys.splitlines()), (
        "an irreversible command must never be auto-approved"
    )
    # The reversible-path guidance was injected to the spoke.
    assert "backup branch" in keys, keys
    # Warned + journaled with the irreversible class; never blocked.
    assert "irreversible" in (statedir / "decision-journal.jsonl").read_text()
    ready = Path(env["_READY_LOG"])
    assert not ready.exists() or "--blocked 5" not in ready.read_text()


def test_permission_approve_delivery_failure_warns_not_blocks(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A known-safe command classifies APPROVE, but the Yes keystroke fails to register (the
    # transcript never advances). #241: that no longer parks the spoke blocked/<issue> — it
    # warns and retries on the backoff.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_bash_tool_record("git reset -q; git add tests/x.py")) + "\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # pinned mtime: no Enter-append -> no advance
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  capture-pane) printf "%s\\n" "{_PERMISSION_PROMPT}" ;;\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        "esac\nexit 0\n"  # send-keys is a no-op: the transcript never advances -> delivery fails
    )
    (fake_bin / "tmux").chmod(0o755)
    statedir = tmp_path / "sd"
    statedir.mkdir()
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_STATE_DIR": str(statedir),
        "SPOKE_READY": str(ready_stub),
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_INJECT_POLL_SECONDS": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    assert (statedir / "warned-5.txt").exists(), "a failed approval delivery must warn, not park"
    assert not ready_log.exists() or "--blocked 5" not in ready_log.read_text()


def test_permission_reasoner_auth_failure_warns_not_denies(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # If the supervisor's own token dies while the reasoner decides a permission dialog, the
    # blob is an auth error, not a decision. The permission path must detect it (rc != 0 + auth
    # signature), raise the global halt flag — and #241 §9 WARN the spoke (not block it, not
    # inject a spurious denial into the live dialog).
    env = {
        **_perm_env(
            tmp_path,
            spoke_repo,
            "npm run deploy",  # ESCALATE -> reasoner
            "printf 'Invalid API key . Please run /login'; exit 1",
        ),
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(
        f"broker_service_gate '{spoke_repo}' 5 unattended; echo AUTH=$_AFK_AUTH_FAILED",
        env=env,
    )
    assert result.returncode == 0, result.stderr

    assert "AUTH=1" in result.stdout, "an auth failure must raise the global halt flag"
    ready = Path(env["_READY_LOG"])
    assert not ready.exists() or "--blocked 5" not in ready.read_text(), (
        "#241: auth warns, never blocks"
    )
    assert "WARNING: #5" in result.stderr, result.stderr
    # No spurious denial: the reversible-path guidance was never injected.
    keys = Path(env["_KEYLOG"]).read_text() if Path(env["_KEYLOG"]).exists() else ""
    assert "reversible, in-scope path" not in keys, "auth failure must not inject a spurious deny"


# ── issue #241 S4: staleness recomputes against the current park, never bare-drops ──
# Pre-#241 a park-signature change dropped the answer and returned. #241 §4: if the spoke is
# still parked (on a possibly-changed prompt), recompute against the CURRENT park in the same
# pass — a recurring false-staleness (a non-turn write bumping the transcript mtime) otherwise
# strands the spoke (the #240 hang class). The #89 protection stays: a spoke that genuinely
# MOVED ON (no park extractable) is still dropped, never injected mid-turn.


def test_staleness_recomputes_against_current_park(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    calls = tmp_path / "answerer.calls"
    # The reasoner touches the LIVE transcript, so the post-reason _still_parked_same mtime
    # check always reports "changed" — a false staleness. The pane still shows the park, so #241
    # must recompute (re-run) rather than drop. The recompute is depth-bounded to one re-run.
    live_jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(
        live_jsonl, (1_000_000_000, 1_000_000_000)
    )  # pin OLD so the reasoner's touch reads as newer
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": f"printf x >> '{calls}'; touch '{live_jsonl}'; printf 'ANSWER: pick Redis'",
        "AFK_REANSWER_CEILING": "5",  # keep the ceiling out of this test
        "AFK_STATE_DIR": str(tmp_path / "sd"),
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    n = calls.read_text().count("x") if calls.exists() else 0
    assert n == 2, f"a still-parked staleness must recompute once (not bare-drop); ran {n}"


# ── issue #241 S5: the human-decision chokepoint warns-and-continues, never parks ──
# _broker_on_human_decision (unattended) is the ONE seam every void/fingerprint/inject-failure/
# ESCALATE/no-decision escalation funnels through. #241 converts it from _escalate_blocked
# (terminal blocked/<issue>) to broker_warn_continue: warn loudly, journal the taken decision,
# and keep the spoke serviced. The mutation-void becomes backoff-paced, not terminal-forever.


def test_unattended_escalate_warns_not_blocks(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "printf 'reasoning\\nESCALATE: this is a human call'",
        "AFK_STATE_DIR": str(statedir),
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    log = Path(env["_READY_LOG"]).read_text() if Path(env["_READY_LOG"]).exists() else ""
    assert "--blocked 5" not in log, "the answerer's human-call must warn-and-continue, not park"
    assert "WARNING: #5" in result.stderr, result.stderr
    assert (statedir / "warned-5.txt").exists()
    assert (statedir / "decision-journal.jsonl").exists()


def test_mutation_void_warns_not_blocks(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # A reasoner that mutates the read-only live tree still has its answer VOIDED (never
    # injected), but #241 warns-and-continues instead of parking blocked/<issue>.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": f"printf 'mutated' > '{spoke_repo}/tracked.txt'; printf 'ANSWER: go ahead'",
        "AFK_STATE_DIR": str(statedir),
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    log = Path(env["_READY_LOG"]).read_text() if Path(env["_READY_LOG"]).exists() else ""
    assert "--blocked 5" not in log, "a voided mutation must warn-and-continue, not park"
    assert "WARNING: #5" in result.stderr, result.stderr
    assert (statedir / "warned-5.txt").exists()


# ── issue #241 hub-review: journal-before-inject + success-path WARN journaling ──


def test_permission_approve_journals_before_inject(spoke_repo: Path, tmp_path: Path) -> None:
    # BLOCKER 1: the reasoner-APPROVE decision must be journaled BEFORE approve_permission
    # delivers the "Yes" keypress — so the audit record can never be lost if the inject crashes
    # or races the command it authorized. The fake tmux records, at the moment the approve "1"
    # keystroke fires, whether the journal line already exists.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    journal = statedir / "decision-journal.jsonl"
    probe = tmp_path / "probe"
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(json.dumps(_bash_tool_record("npm run deploy")) + "\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  send-keys)\n"
        f'    case "$*" in *" 1") [ -f "{journal}" ] && echo EXISTS >> "{probe}" || echo MISSING >> "{probe}" ;; esac ;;\n'
        f'  capture-pane) printf "%s\\n" "{_PERMISSION_PROMPT}" ;;\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_STATE_DIR": str(statedir),
        "SPOKE_READY": str(ready_stub),
        "AFK_ANSWERER_CMD": "printf 'REVERSIBILITY: reversible\\nANSWER: APPROVE'",
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_INJECT_POLL_SECONDS": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    assert probe.read_text().strip() == "EXISTS", (
        "the decision must be journaled BEFORE the approve keystroke fires"
    )
    # The approve keystroke does not advance the transcript here, so delivery FAILS — the durable
    # journal must record the PROVISIONAL pre-keypress intent and the delivery-failure distinctly,
    # and must NOT contain a 'delivered' line that would read as authorized-and-ran (#241 review).
    journal_text = journal.read_text()
    assert "APPROVING (delivery pending)" in journal_text, (
        "the pre-keypress line must be provisional, not a completed-approval record"
    )
    assert "delivery FAILED" in journal_text, (
        "a failed approval delivery must be journaled distinctly"
    )
    assert "APPROVED (delivered)" not in journal_text, (
        "a failed delivery must never leave a 'delivered' record a reader takes as ran"
    )


def test_success_answer_journals_warn_and_reversibility(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # BLOCKER 2: a successful main answer whose reasoner reply carries a WARN / non-reversible
    # class is recorded for morning review — a loud warned record AND a journal line.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    fake_bin = tmp_path / "bin"
    jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # pin old so the inject's append advances it
    _fake_tmux_pane(fake_bin, spoke_repo, jsonl)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": (
            "printf 'REVERSIBILITY: irreversible\\nWARN: double-check the migration\\nANSWER: proceed with Redis'"
        ),
        "AFK_STATE_DIR": str(statedir),
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    assert (statedir / "warned-5.txt").exists(), "a WARN-flagged answer must warn for review"
    journal = (statedir / "decision-journal.jsonl").read_text()
    assert "irreversible" in journal and "double-check the migration" in journal


def test_success_answer_routine_journals_file_only(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # WARNING: a routine (reversible, no-WARN) successful answer journals to the per-run FILE
    # but does NOT gh-comment (that would be per-answer noise) and does NOT warn.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    fake_bin = tmp_path / "bin"
    jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    _fake_tmux_pane(fake_bin, spoke_repo, jsonl)
    gh_log = tmp_path / "gh.log"
    (fake_bin / "gh").write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{gh_log}"\n')
    (fake_bin / "gh").chmod(0o755)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "printf 'REVERSIBILITY: reversible\\nANSWER: use Redis'",
        "AFK_STATE_DIR": str(statedir),
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        # NB: AFK_JOURNAL_GH_COMMENT left ON — the routine path must still not comment.
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    assert (statedir / "decision-journal.jsonl").exists(), "a routine answer still journals (file)"
    assert not (statedir / "warned-5.txt").exists(), "a routine answer must NOT warn"
    assert not gh_log.exists() or "issue comment" not in gh_log.read_text(), (
        "a routine answer must NOT post a gh comment"
    )


def test_ceiling_mechanical_approve_is_paced_not_every_tick(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # #241 review (regression for the N1 fix): a mechanically-auto-approvable permission that keeps
    # re-appearing at the SAME (tip, park-signature) — the approve keypress doesn't advance it — is
    # PACED by the ceiling backoff once exhausted, NOT re-warned + re-approved every tick.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    keylog = tmp_path / "keys.log"
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(_bash_tool_record("git reset -q; git add tests/x.py")) + "\n"  # classify APPROVE
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  send-keys) case "$*" in *" 1") printf "1\\n" >> "{keylog}" ;; esac ;;\n'
        f'  capture-pane) printf "%s\\n" "{_PERMISSION_PROMPT}" ;;\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        "esac\nexit 0\n"  # the approve never advances the transcript → the dialog re-appears
    )
    (fake_bin / "tmux").chmod(0o755)
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text("#!/usr/bin/env bash\n:\n")
    ready_stub.chmod(0o755)
    base = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_STATE_DIR": str(statedir),
        "SPOKE_READY": str(ready_stub),
        "AFK_REANSWER_CEILING": "1",
        "AFK_WARN_BACKOFF_BASE": "60",
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }
    # Five ticks; the last three are past the 60s backoff window opened at tick 2.
    for now in ("1000", "1000", "1100", "1100", "1100"):
        _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env={**base, "AFK_NOW": now})

    approves = keylog.read_text().count("1") if keylog.exists() else 0
    assert approves <= 2, (
        f"a re-appearing auto-approve must be backoff-paced, not every tick; fired {approves}"
    )


def test_success_answer_case_insensitive_reversibility_stays_routine(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # #241 review: a routine answer whose class is 'Reversible' (capitalized / punctuated) must be
    # read as reversible — routine, file-only journal, NO loud warned record.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    fake_bin = tmp_path / "bin"
    jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    _fake_tmux_pane(fake_bin, spoke_repo, jsonl)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "printf 'REVERSIBILITY: Reversible.\\nANSWER: use Redis'",
        "AFK_STATE_DIR": str(statedir),
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    assert (statedir / "decision-journal.jsonl").exists(), "a routine answer still journals (file)"
    assert not (statedir / "warned-5.txt").exists(), (
        "a 'Reversible.' class must be read as reversible → routine, not a loud warned record"
    )


def test_permission_deny_delivery_failure_journaled_distinctly(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # #241 review (CONFIRMED): the DENY path must NOT swallow the redirect inject rc. When the
    # decline-and-redirect fails to reach the spoke (dead pane / failed inject), the durable
    # journal must record the failure DISTINCTLY — never as a clean, delivered denial.
    destructive = "git reset --hard origin/main"  # irreversible -> reasoner denies
    env = _perm_env(
        tmp_path,
        spoke_repo,
        destructive,
        "printf 'REVERSIBILITY: irreversible\\nANSWER: DENY: create a backup branch first'",
    )
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    # Rewrite tmux so send-keys never advances the transcript -> the redirect inject FAILS.
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  send-keys) printf "%s\\n" "$*" >> "{env["_KEYLOG"]}" ;;\n'
        f'  capture-pane) printf "%s\\n" "{_PERMISSION_PROMPT}" ;;\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    jsonl = _project_dir_for(Path(env["CLAUDE_PROJECTS_DIR"]), spoke_repo) / "session.jsonl"
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # no external advance masks the failure

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    journal = (Path(env["_STATEDIR"]) / "decision-journal.jsonl").read_text()
    assert "redirect delivery FAILED" in journal, (
        "a failed deny-redirect must be journaled distinctly, not as a clean denial"
    )
    keys = Path(env["_KEYLOG"]).read_text() if Path(env["_KEYLOG"]).exists() else ""
    assert not any(line.split()[-1:] == ["1"] for line in keys.splitlines()), (
        "an irreversible command must never be auto-approved"
    )


def test_success_answer_quoted_irreversible_stays_flagged(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # #241 review (Finding 2 fail-safe): a class that LEADS with punctuation ('"irreversible"')
    # must still be read as non-reversible and flagged with a loud warned record — the old
    # trailing-strip collapsed it to empty and mis-filed a noteworthy decision as routine.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    fake_bin = tmp_path / "bin"
    jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    _fake_tmux_pane(fake_bin, spoke_repo, jsonl)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "printf 'REVERSIBILITY: \"irreversible\"\\nANSWER: proceed with care'",
        "AFK_STATE_DIR": str(statedir),
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
    assert result.returncode == 0, result.stderr

    assert (statedir / "warned-5.txt").exists(), (
        "a quoted 'irreversible' class must fail SAFE to a loud warned record, not routine"
    )
    assert "irreversible" in (statedir / "decision-journal.jsonl").read_text()


def test_broker_warn_continue_unconditionally_advances_backoff(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # #241 review r2.2: broker_warn_continue must ALWAYS advance the warned-retry backoff. It is
    # reached not only from broker_service_gate but also from hub-afk's reap/land/dispatch passes
    # (_warn_parked_last), which have no per-tick reset — a suppression guard that skipped the arm
    # there froze the next-due timestamp and re-warned every tick. Pin the monotonic-growth
    # invariant the revert restored: repeated calls keep advancing the attempt counter.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {
        "AFK_STATE_DIR": str(statedir),
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_WARN_BACKOFF_BASE": "60",
        "AFK_NOW": "1000",
    }
    result = _call(
        f"broker_warn_continue '{spoke_repo}' 5 escalate 'first' reversible; "
        f"broker_warn_continue '{spoke_repo}' 5 escalate 'second' reversible; "
        'IFS=$\'\\t\' read -r a _ < "$(_afk_warned_state_file 5)"; printf "attempt=%s\\n" "$a"',
        env=env,
    )
    assert "attempt=2" in result.stdout, result.stdout + result.stderr


# ── issue #277: PLAN-gate fast-path (waive a redundant reasoner round trip) ─────
# When a spoke's posted PLAN restates the issue body, the gate is auto-approved WITHOUT
# the expensive run_answerer round trip. The waive is recorded on three audit surfaces
# (journal + gh comment, a distinct fast-path span, a hub-status ledger); a genuinely
# divergent plan still falls through to the full reasoner unchanged.

_RESTATE_BODY = (
    "afk plan-gate waive\n\n"
    "The measured drain spends a full answerer round trip on redundant PLAN gates. "
    "Force LC_ALL=C before the pgrep process probe helper so a non-english argv never "
    "dies with an illegal byte sequence. Record the waive on the decision journal, a "
    "visible hub-status marker, and a distinct langfuse span so an operator reviewing a "
    "landed issue can always tell human-answered versus reasoner-answered versus fast-pathed."
)
_RESTATING_PLAN = (
    "Force LC_ALL=C before the pgrep process probe helper so a non-english argv never dies "
    "with the illegal byte sequence. Record the waive on the decision journal, a visible "
    "hub-status marker, and a distinct langfuse span, so an operator reviewing a landed issue "
    "can tell human-answered versus reasoner-answered versus fast-pathed."
)
_DIVERGENT_PLAN = (
    "Redesign the observability warehouse: introduce a Kafka ingestion queue, migrate the "
    "DuckDB schemas to ClickHouse, stand up Prometheus exporters and Grafana dashboards, and "
    "rewrite the Streamlit frontend as a Next.js single-page application with GraphQL."
)


def _fastpath_env(
    spoke_repo: Path,
    tmp_path: Path,
    *,
    plan: str,
    body: str,
    canary: Path,
    issue: int = 5,
    extra: dict[str, str] | None = None,
    write_artifact: bool = True,
    transcript_plan: str = "transcript fallback plan",
) -> dict[str, str]:
    """A GATE-parked spoke (#175 plan artifact + tag) + a fake gh returning <body> + a fake tmux
    pane + a recording spoke-ready + a CANARY reasoner that writes <canary> if it ever runs.

    write_artifact=False models a bare ``--gate`` park that wrote NO plan artifact, so the only
    plan text is <transcript_plan> (the transcript-extraction fallback)."""
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(_gate_park_transcript(transcript_plan))
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # pin old so the inject's append advances it

    art = spoke_repo / ".ai-toolkit"
    art.mkdir(exist_ok=True)
    if write_artifact:
        (art / f"gate-{issue}.md").write_text(plan)
    _tag_gate_at_head(spoke_repo, issue)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text("#!/usr/bin/env bash\ncat <<'GHBODY'\n" + body + "\nGHBODY\n")
    (fake_bin / "gh").chmod(0o755)
    tmux_log = _fake_tmux_pane(fake_bin, spoke_repo, jsonl)

    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)

    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "SPOKE_READY": str(ready_stub),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_STATE_DIR": str(tmp_path / "sd"),
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_ANSWERER_CMD": f"printf ran > '{canary}'; printf 'REVERSIBILITY: reversible\\nANSWER: Approved'",
        "_READY_LOG": str(ready_log),
        "_TMUX_LOG": str(tmux_log),
    }
    if extra:
        env.update(extra)
    return env


def test_broker_plan_is_restatement_scores_and_thresholds(spoke_repo: Path) -> None:
    # The coverage helper ECHOES the bag-of-words overlap ratio AND returns rc 0 only for a
    # confident restatement (coverage >= threshold AND enough significant tokens).
    restate = _call(
        f"_broker_plan_is_restatement {shlex_quote(_RESTATING_PLAN)} {shlex_quote(_RESTATE_BODY)}; "
        'echo "|rc=$?|"'
    )
    assert restate.returncode == 0
    assert "|rc=0|" in restate.stdout, restate.stdout
    cov = float(restate.stdout.split("|rc=")[0].strip())
    assert cov >= 0.85, f"a restating plan must score high coverage, got {cov}"

    divergent = _call(
        f"_broker_plan_is_restatement {shlex_quote(_DIVERGENT_PLAN)} {shlex_quote(_RESTATE_BODY)}; "
        'echo "|rc=$?|"'
    )
    assert "|rc=1|" in divergent.stdout, divergent.stdout
    cov_div = float(divergent.stdout.split("|rc=")[0].strip())
    assert cov_div < 0.85, f"a divergent plan must score low coverage, got {cov_div}"


def test_broker_plan_is_restatement_short_plan_falls_through(spoke_repo: Path) -> None:
    # A trivially SHORT plan cannot be judged a confident restatement even at coverage 1.0 —
    # too few significant tokens — so it falls through (rc 1) to the full reasoner.
    out = _call(
        f"_broker_plan_is_restatement 'apply the fix' {shlex_quote(_RESTATE_BODY)}; echo \"|rc=$?|\""
    )
    assert "|rc=1|" in out.stdout, out.stdout


def test_broker_service_gate_fastpaths_a_restating_plan(spoke_repo: Path, tmp_path: Path) -> None:
    # AC1 headline: a PLAN-gate park whose posted plan restates the issue body is resolved
    # WITHOUT invoking run_answerer — the canary reasoner never runs, the spoke gets an approve
    # inject, and the waive lands a park:gate journal line naming the coverage.
    canary = tmp_path / "reasoner-ran"
    env = _fastpath_env(
        spoke_repo, tmp_path, plan=_RESTATING_PLAN, body=_RESTATE_BODY, canary=canary
    )

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert not canary.exists(), "the reasoner must NOT run when the plan restates the body"
    tmux_log = Path(env["_TMUX_LOG"]).read_text()
    assert "proceed to implementation" in tmux_log, f"the approve reply must inject: {tmux_log}"
    ready = Path(env["_READY_LOG"])
    assert "--blocked" not in (ready.read_text() if ready.exists() else ""), (
        "a waive must not block"
    )
    journal = (tmp_path / "sd" / "decision-journal.jsonl").read_text()
    assert '"park":"gate"' in journal, f"the waive must journal a park:gate line: {journal}"
    assert "fast-path" in journal and "coverage" in journal, journal


def test_broker_service_gate_reasoner_runs_on_divergent_plan(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # AC1 contrast: a genuinely DIVERGENT plan is NOT a restatement, so the gate falls through
    # to the full reasoner — the canary runs and its answer path takes over.
    canary = tmp_path / "reasoner-ran"
    env = _fastpath_env(
        spoke_repo, tmp_path, plan=_DIVERGENT_PLAN, body=_RESTATE_BODY, canary=canary
    )

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert canary.exists(), "a divergent plan must fall through to the full reasoner"


def test_broker_service_gate_fastpath_ignores_transcript_fallback(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # Regression (#277 review): a bare `--gate` park that wrote NO plan artifact must NEVER be
    # fast-pathed off the transcript-extraction fallback ($orig_question) — that narration is not
    # a plan the spoke authored, and (issue-derived) it scores high coverage, so trusting it could
    # auto-approve a plan that was never written. Even a fully-restating transcript must run the
    # reasoner when no artifact exists.
    canary = tmp_path / "reasoner-ran"
    env = _fastpath_env(
        spoke_repo,
        tmp_path,
        plan=_RESTATING_PLAN,
        body=_RESTATE_BODY,
        canary=canary,
        write_artifact=False,
        transcript_plan=_RESTATING_PLAN,
    )

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert canary.exists(), (
        "a bare --gate park with no artifact must run the reasoner, never fast-path the transcript"
    )


def test_broker_service_gate_fastpath_disabled_falls_through(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # AFK_FASTPATH=0 is the documented kill switch: even a restating plan runs the full reasoner.
    canary = tmp_path / "reasoner-ran"
    env = _fastpath_env(
        spoke_repo,
        tmp_path,
        plan=_RESTATING_PLAN,
        body=_RESTATE_BODY,
        canary=canary,
        extra={"AFK_FASTPATH": "0"},
    )

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert canary.exists(), "AFK_FASTPATH=0 must disable the waive and run the reasoner"


def test_broker_service_gate_fastpath_inject_failure_falls_through(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A fast-path whose approve inject cannot be confirmed must NOT swallow the gate — it falls
    # through to the full reasoner. Model a broken pane: list-panes maps it, but the submitting
    # Enter never advances the transcript, so inject_and_verify fails.
    canary = tmp_path / "reasoner-ran"
    env = _fastpath_env(
        spoke_repo,
        tmp_path,
        plan=_RESTATING_PLAN,
        body=_RESTATE_BODY,
        canary=canary,
        extra={"AFK_INJECT_MENU_PAUSE": "0", "AFK_INJECT_VERIFY_SECONDS": "0"},
    )
    fake_bin = tmp_path / "bin"
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        "esac\nexit 0\n"  # never advances the transcript -> verify fails
    )
    (fake_bin / "tmux").chmod(0o755)

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert canary.exists(), "a failed fast-path inject must fall through to the reasoner"


def test_broker_service_gate_fastpath_emits_distinct_span(spoke_repo: Path, tmp_path: Path) -> None:
    # AC4: the waive emits a DISTINCT afk-answer span variant (status fast-path), never folded
    # into a normal success answer; telemetry-off stays a no-op (no events file at all).
    canary = tmp_path / "reasoner-ran"
    tele = tmp_path / "telemetry"
    env = _fastpath_env(
        spoke_repo,
        tmp_path,
        plan=_RESTATING_PLAN,
        body=_RESTATE_BODY,
        canary=canary,
        extra={
            "AI_TOOLKIT_TELEMETRY": "1",
            "AI_TOOLKIT_TELEMETRY_DIR": str(tele),
            "AI_TOOLKIT_OTEL_SPAN_ENDPOINT": "",
        },
    )

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    events = tele / "events.jsonl"
    assert events.exists(), "telemetry-on must emit a span"
    spans = [json.loads(line) for line in events.read_text().splitlines()]
    fast = [s for s in spans if s.get("name") == "afk-answer" and s.get("status") == "fast-path"]
    assert fast, f"the waive must emit a status=fast-path afk-answer span: {spans}"
    assert not any(s.get("status") == "success" for s in spans), (
        "a waive must not also emit a normal success answer span"
    )


def test_broker_service_gate_fastpath_telemetry_off_is_noop(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # AC4 tail: with telemetry off the fast-path emits no span at all.
    canary = tmp_path / "reasoner-ran"
    tele = tmp_path / "telemetry"
    env = _fastpath_env(
        spoke_repo,
        tmp_path,
        plan=_RESTATING_PLAN,
        body=_RESTATE_BODY,
        canary=canary,
        extra={"AI_TOOLKIT_TELEMETRY_DIR": str(tele), "AI_TOOLKIT_OTEL_SPAN_ENDPOINT": ""},
    )

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert not (tele / "events.jsonl").exists(), "telemetry-off must be a no-op"
