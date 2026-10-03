import json

import pytest
from conftest import V2

DISPATCH = str(V2 / "scripts/dispatch.sh")
ISSUE = {"number": 7, "title": "Add Hello, World!", "body": "Do it.\n\nScope: hello.py tests/\nGate: plan\n"}
NAME = "7-add-hello-world"


def strip(argv):
    """Drop the replay id and --json that orca_mutate/orca_json add, so the argv reads like the design."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == "--retry-request":
            skip = True
        elif a != "--json":
            out.append(a)
    return out


@pytest.fixture
def d(stubs, repo, run):
    wt = repo.wt(NAME)
    (wt / ".ai-toolkit").mkdir()
    (wt / ".ai-toolkit/setup-done").write_text("")
    root = str(repo.root.resolve())
    stubs.reply("gh.issue_view", json.dumps(ISSUE))
    stubs.reply("orca.worktree_create", json.dumps({"result": {"worktree": {"path": str(wt), "branch": f"refs/heads/{NAME}"}}}))
    stubs.reply("orca.terminal_create", '{"result":{"terminal":{"handle":"term_agent"}}}')
    stubs.reply("orca.terminal_show", '{"result":{"terminal":{"agentIdentity":"claude"}}}')
    stubs.reply("orca.orchestration_worker_start", '{"result":{"dispatchId":"ctx_1","state":"ready"}}')

    def go(*args, **env):
        e = {"RUN": "run_t", "ORCA_TERMINAL_HANDLE": "term_coord", "AI_TOOLKIT_POLL": 0, **env}
        return run(["bash", DISPATCH, *args], cwd=repo.root, **e)

    def orca():
        return [strip(c) for c in stubs.calls("orca")]

    return type("D", (), {"go": staticmethod(go), "orca": staticmethod(orca), "wt": wt, "root": root, "stubs": stubs, "repo": repo})


def worker_start(d):
    return next(c for c in d.orca() if c[:2] == ["orchestration", "worker-start"])


def test_two_step_launch_runs_the_exact_orca_calls_in_order(d):
    r = d.go("7")
    assert r.returncode == 0, r.stderr
    calls = d.orca()
    assert [c[:2] for c in calls] == [["worktree", "create"], ["terminal", "create"], ["terminal", "show"],
                                      ["orchestration", "worker-start"], ["worktree", "set"]]
    assert calls[0] == ["worktree", "create", "--repo", f"path:{d.root}", "--name", NAME, "--base-branch", "origin/main", "--setup", "run"]
    term = next(c for c in calls if c[:2] == ["terminal", "create"])
    assert term[2:6] == ["--worktree", f"path:{d.wt}", "--title", f"{NAME}-agent"]
    assert term[6] == "--command" and term[7].endswith(
        "/bin/claude-spoke --model claude-sonnet-5-5 --effort high --dangerously-skip-permissions")
    ws = worker_start(d)
    seed = ws[ws.index("--spec") + 1]
    assert ws == ["orchestration", "worker-start", "--run", "run_t", "--from", "term_coord", "--worktree", f"path:{d.wt}",
                  "--terminal", "term_agent", "--task-title", "#7 Add Hello, World!", "--spec", seed, "--timeout-ms", "120000"]
    assert calls[-1] == ["worktree", "set", "--worktree", f"path:{d.wt}", "--issue", "7", "--workspace-status", "in-progress"]
    assert json.loads(r.stdout) == {"issue": 7, "dispatch": "ctx_1", "worktree": str(d.wt), "terminal": "term_agent"}
    assert d.stubs.calls("gh") == [["issue", "view", "7", "--json", "number,title,body"]]


def test_seed_for_a_plan_gate_asks_before_coding_and_never_touches_the_base(d):
    d.go("7")
    ws = worker_start(d)
    seed = ws[ws.index("--spec") + 1]
    for part in (".ai-toolkit/task.md", "approve,revise", "do not edit code before approve", "RED", "git push -u origin HEAD",
                 "worker_done --outcome succeeded", "Never merge or push the base branch"):
        assert part in seed, part


def test_gate_none_and_model_footer_override_the_defaults(d):
    d.stubs.reply("gh.issue_view", json.dumps({**ISSUE, "body": "x\nScope: a\nGate: none\nModel: claude-opus-5-5 medium\n"}))
    d.go("7")
    term = next(c for c in d.orca() if c[:2] == ["terminal", "create"])
    assert term[7].endswith("--model claude-opus-5-5 --effort medium --dangerously-skip-permissions")
    ws = worker_start(d)
    assert "approve,revise" not in ws[ws.index("--spec") + 1]


def test_missing_gate_footer_falls_back_to_the_env_default(d):
    d.stubs.reply("gh.issue_view", json.dumps({**ISSUE, "body": "no footer"}))
    d.go("7")
    ws = worker_start(d)
    assert "approve,revise" in ws[ws.index("--spec") + 1]  # GATE_DEFAULT=plan


@pytest.mark.parametrize("title,slug", [
    ("Fix: the très-long Ünïcode title & other symbols that keep going and going", "fix-the-tr-s-long-n-code-title-other-sym"),
    ("***", "issue"),
])
def test_slug_is_ascii_lowercase_and_bounded(d, title, slug):
    d.stubs.reply("gh.issue_view", json.dumps({**ISSUE, "title": title}))
    d.go("7")
    name = d.orca()[0][d.orca()[0].index("--name") + 1]
    assert name == f"7-{slug}"


def test_a_model_footer_cannot_inject_into_the_terminal_command(d):
    d.stubs.reply("gh.issue_view", json.dumps({**ISSUE, "body": "Model: x; touch /tmp/pwned\n"}))
    r = d.go("7")
    assert r.returncode == 1 and "bad Model footer" in r.stderr
    assert not any(c[:2] in (["terminal", "create"], ["orchestration", "worker-start"]) for c in d.orca())


def test_trust_dialog_is_dismissed_with_down_then_enter(d):
    d.stubs.reply("orca.terminal_show", '{"result":{"terminal":{}}}', n=1)
    d.stubs.reply("orca.terminal_read", '{"result":{"terminal":{"tail":["Do you trust this folder?","1. Yes","2. No, exit"]}}}')
    assert d.go("7").returncode == 0
    sends = [c for c in d.orca() if c[:2] == ["terminal", "send"]]
    assert sends == [["terminal", "send", "--terminal", "term_agent", "--text", "\x1b[B"],
                     ["terminal", "send", "--terminal", "term_agent", "--enter"]]


def test_setup_that_never_finishes_launches_nothing(d):
    (d.wt / ".ai-toolkit/setup-done").unlink()
    r = d.go("7", DISPATCH_TRIES=2)
    assert r.returncode == 1 and "setup" in r.stderr
    assert [c[:2] for c in d.orca()] == [["worktree", "create"]]


def test_agent_that_never_comes_up_is_not_handed_a_task(d):
    d.stubs.reply("orca.terminal_show", '{"result":{"terminal":{}}}')
    r = d.go("7", DISPATCH_TRIES=2)
    assert r.returncode == 1 and "claude did not start" in r.stderr
    assert not any(c[:2] in (["orchestration", "worker-start"], ["worktree", "set"]) for c in d.orca())


def test_failed_worker_start_exits_non_zero_without_linking_the_issue(d):
    d.stubs.reply("orca.orchestration_worker_start", '{"result":{"state":"failed","failedStage":"setup"}}', rc=1)
    r = d.go("7")
    assert r.returncode == 1 and "worker-start" in r.stderr
    assert not any(c[:2] == ["worktree", "set"] for c in d.orca())


def test_a_run_is_required_and_never_inferred(d):
    r = d.go("7", RUN="")
    assert r.returncode == 2 and "Run" in r.stderr and d.orca() == []
    assert d.go().returncode == 2 and d.go("--bogus").returncode == 2
    assert d.go("--run", "run_flag", "7", RUN="").returncode == 0
    assert worker_start(d)[2:4] == ["--run", "run_flag"]
    assert not any(c[:2] == ["orchestration", "run-current"] for c in d.orca())


def test_override_launch_is_a_single_worker_start_on_a_new_worktree(d):
    d.stubs.reply("orca.worktree_show", json.dumps({"result": {"worktree": {"path": str(d.wt)}}}))
    r = d.go("7", DISPATCH_LAUNCH="override")
    assert r.returncode == 0, r.stderr
    ws = worker_start(d)
    seed = ws[ws.index("--spec") + 1]
    assert ws == ["orchestration", "worker-start", "--run", "run_t", "--from", "term_coord", "--worktree", "new-top-level",
                  "--repo", f"path:{d.root}", "--name", NAME, "--base-branch", "origin/main", "--agent", "claude",
                  "--model", "claude-sonnet-5-5", "--effort", "high", "--task-title", "#7 Add Hello, World!", "--spec", seed,
                  "--timeout-ms", "120000"]
    assert not any(c[0] == "terminal" or c[:2] == ["worktree", "create"] for c in d.orca())
    assert d.orca()[-1] == ["worktree", "set", "--worktree", f"name:{NAME}", "--issue", "7", "--workspace-status", "in-progress"]


def issue(n, body="Scope: a.py", labels=(), blocked=()):
    return {"number": n, "body": body, "labels": {"nodes": [{"name": x} for x in labels]},
            "blockedBy": {"nodes": [{"number": b, "state": s} for b, s in blocked]}}


NEXT = [
    ("lowest number first", [issue(5, "Scope: b.py"), issue(3, "Scope: a.py")], [], 3),
    ("priority beats a lower number", [issue(3), issue(9, "Scope: z.py", ["priority"])], [], 9),
    ("hold is skipped", [issue(3, labels=["hold"]), issue(4, "Scope: b.py")], [], 4),
    ("blocked label is skipped", [issue(3, labels=["blocked"]), issue(4, "Scope: b.py")], [], 4),
    ("open blocker is skipped", [issue(3, blocked=[(1, "OPEN")]), issue(4, "Scope: b.py")], [], 4),
    ("closed blockers are fine", [issue(3, blocked=[(1, "CLOSED"), (2, "CLOSED")])], [], 3),
    ("overlapping scope with an in-flight issue is skipped",
     [issue(2, "Scope: a.py tests/"), issue(3, "Scope: tests/, b.py"), issue(4, "Scope: c.py")], [2], 4),
    ("an in-flight issue is not picked again", [issue(2), issue(3, "Scope: b.py")], [2], 3),
    ("an exclusive in-flight issue blocks everything", [issue(2, "Scope: *"), issue(3, "Scope: b.py")], [2], None),
    ("an exclusive candidate waits for an idle board", [issue(2, "no scope line"), issue(3, "Scope: b.py")], [3], None),
    ("an exclusive candidate runs on an idle board", [issue(2, "Scope: *")], [], 2),
    ("nothing open", [], [], None),
]


def stub_board(d, nodes, busy):
    d.stubs.reply("gh.api_graphql", json.dumps(nodes))
    d.stubs.reply("orca.worktree_list", json.dumps({"result": {"worktrees": [{"linkedIssue": None}, *({"linkedIssue": b} for b in busy)]}}))


@pytest.mark.parametrize("nodes,busy,want", [x[1:] for x in NEXT], ids=[x[0] for x in NEXT])
def test_next_picks_the_first_ready_issue(d, nodes, busy, want):
    stub_board(d, nodes, busy)
    r = d.go("--next", "--dry-run")
    if want is None:
        assert r.returncode == 3 and "nothing ready" in r.stderr
    else:
        assert r.returncode == 0 and r.stdout.strip() == str(want), r.stderr
    assert [c[:2] for c in d.orca()] == [["worktree", "list"]]   # a dry run launches nothing


def test_next_dispatches_the_pick(d):
    stub_board(d, [issue(5, "Scope: b.py"), issue(3, "Scope: a.py")], [])
    r = d.go("--next")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["issue"] == 3 and d.stubs.calls("gh")[-1] == ["issue", "view", "3", "--json", "number,title,body"]
    assert any(c[:2] == ["orchestration", "worker-start"] for c in d.orca())


def test_nothing_ready_dispatches_nothing(d):
    stub_board(d, [], [])
    assert d.go("--next").returncode == 3
    assert not any(c[:2] == ["orchestration", "worker-start"] for c in d.orca())
