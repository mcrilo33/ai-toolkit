import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import STUB_TEE, V2

ANSWER, REVIEW = str(V2 / "scripts/answer.sh"), str(V2 / "scripts/review.sh")
OK = {"verdict": "APPROVE", "blockers": [], "warnings": ["a.py:1 - nit"], "tdd_followed": True, "tests_weakened": False, "summary": "fine"}


def sh(argv, cwd, input="", **env):
    return subprocess.run(argv, cwd=cwd, input=input, capture_output=True, text=True, env={**os.environ, **{k: str(v) for k, v in env.items()}})


def tee_stdin(link_script):   # the claude stub also records the prompt it was given on stdin
    d = Path(os.environ["STUB_DIR"])
    link_script(d / "bin/claude", STUB_TEE)
    return lambda: (d / "claude.stdin").read_text()


@pytest.fixture
def A(stubs, tmp_path, link_script):
    stdin, wt, rule = tee_stdin(link_script), tmp_path / "wt", tmp_path / "rule.md"
    wt.mkdir()
    rule.write_text("be decisive")

    def go(out, rc=0, rule_path=None, input="", **env):
        stubs.reply("claude", out, rc=rc)
        return sh(["bash", ANSWER, str(wt)], tmp_path, input, ANSWER_RULE=rule_path or rule, **env)

    return type("A", (), {"go": staticmethod(go), "wt": wt, "rule": rule, "stubs": stubs, "stdin": staticmethod(stdin)})


@pytest.mark.parametrize("q", ["PERMISSION REQUEST (not a plan gate: reply allow or deny)\ntool: Bash\ncommand:\n  ls\n", "\n  PERMISSION REQUEST\ntool: Write"])
def test_answer_never_answers_a_permission_question_even_when_claude_would_approve(A, q):
    r = A.go("EVIDENCE: fine\nANSWER: approve\n", input=q)   # the auto loop denies these itself; this is the backstop if one is ever piped here
    assert r.returncode == 1 and r.stdout == "" and "permission" in r.stderr.lower() and not A.stubs.calls("claude")


def test_answer_runs_read_only_claude_in_the_worktree_with_the_rule_and_the_question(A):
    r = A.go("thinking...\nREVERSIBILITY: reversible\nANSWER:   Approve  \n", input="PLAN: do X. Approve?", ANSWER_MODEL="m-1")
    assert r.returncode == 0 and r.stdout == "approve\n", r.stderr
    argv = A.stubs.calls("claude")[0]
    assert argv[:3] == ["-p", "--model", "m-1"] and ["--append-system-prompt-file", str(A.rule), "--allowedTools", "Read,Grep,Glob"] == argv[3:7]
    assert f"PWD={A.wt.resolve()}" in A.stubs.env("claude") and A.stdin().startswith("PLAN: do X. Approve?") and "no issue number" in A.stdin()
    # the issue changed after dispatch: the gate reads the live text; an unreadable issue keeps the copy, warns and says so in the prompt
    task = A.wt / ".ai-toolkit/task.md"
    task.parent.mkdir()
    task.write_text("# #9 old\n\nold body\n")
    A.stubs.reply("gh", '{"number":9,"title":"new","body":"new body"}')
    assert A.go("ANSWER: approve\n", input="PLAN: do X.").returncode == 0
    assert A.stubs.calls("gh")[0][:3] == ["issue", "view", "9"] and task.read_text() == "# #9 new\n\nnew body\n" and "stale" not in A.stdin()
    A.stubs.reply("gh", "boom", rc=1)
    task.write_text("# #9 old\n\nold body\n")
    r = A.go("ANSWER: approve\n", input="PLAN: do X.")
    assert r.returncode == 0 and task.read_text() == "# #9 old\n\nold body\n"
    assert "warning" in r.stderr and "may be stale" in A.stdin() and A.stdin().startswith("PLAN: do X.")
    task.chmod(0o444); task.parent.chmod(0o555)   # an unwritable copy is a stale copy too, never a failed gate
    A.stubs.reply("gh", '{"number":9,"title":"new","body":"new body"}')
    r = A.go("ANSWER: approve\n", input="PLAN: do X.")
    task.parent.chmod(0o755)
    assert r.returncode == 0 and "may be stale" in A.stdin() and task.read_text() == "# #9 old\n\nold body\n"


@pytest.mark.parametrize("line, body", [
    ("revise: drop the extra file", "revise: drop the extra file"),
    ("approve with: drop the extra file", "approve with: drop the extra file"),
    ("Approve  With:   drop the extra file  ", "approve with: drop the extra file"),
])
def test_revise_and_approve_with_keep_their_text_and_warn_lines_follow_the_answer(A, line, body):
    r = A.go(f"WARN: touches the CI config\nREVERSIBILITY: scope\nWARN: second\nANSWER: {line}\n")
    assert r.returncode == 0 and r.stdout == f"{body}\nWARN: touches the CI config\nWARN: second\n"


@pytest.mark.parametrize("out", [
    "", "I think it is fine\n", "ANSWER: maybe\n", "ANSWER:\n", "ANSWER: revise:\n", "ANSWER: revise\n", "ANSWER: approve it\n",
    "ANSWER: approve\nand then more text\n", "**ANSWER: approve**\n", "answer: approve please\n",
    "ANSWER: approve with:\n", "ANSWER: approve with\n", "ANSWER: approve with:   \n", "ANSWER: approve withdraw\n", "ANSWER: approve withdraw: x\n", "ANSWER: approve please with: x\n",
])
def test_anything_but_a_clean_final_answer_line_is_never_an_approve(A, out):
    r = A.go(out)
    assert r.returncode == 1 and r.stdout == ""


def test_a_failing_claude_or_a_missing_rule_is_an_escalation_not_an_approve(A, tmp_path):
    assert A.go("ANSWER: approve\n", rc=1).returncode == 1
    r = A.go("ANSWER: approve\n", rule_path=tmp_path / "nope.md")
    assert r.returncode == 1 and "rule" in r.stderr and r.stdout == ""


@pytest.fixture
def R(stubs, repo, link_script):
    stdin, wt = tee_stdin(link_script), repo.wt("9-feat")
    show = json.dumps({"result": {"worktree": {"path": str(wt), "branch": "refs/heads/9-feat"}}})
    stubs.reply("orca.worktree_show", show)

    def go(verdict, rc=0, **env):
        stubs.reply("claude", verdict if isinstance(verdict, str) else json.dumps(verdict), rc=rc)
        return sh(["bash", REVIEW, "9"], repo.root, **env)

    return type("R", (), {"go": staticmethod(go), "wt": wt, "stubs": stubs, "stdin": staticmethod(stdin), "show": show})


def test_approve_exits_0_and_runs_the_code_review_agent_read_only_in_the_worktree(R):
    r = R.go(OK, REVIEW_MODEL="fable-x", BASE_BRANCH="trunk")
    assert r.returncode == 0 and "SUMMARY: fine" in r.stdout
    argv = R.stubs.calls("claude")[0]
    assert argv[:3] == ["-p", "--model", "fable-x"] and argv[argv.index("--agent") + 1] == "code-review"
    tools = argv[argv.index("--allowedTools") + 1]
    assert "Read" in tools and "git diff" in tools and not {"Edit", "Write"} & set(tools.replace("(", ",").split(","))
    assert "origin/trunk...9-feat" in R.stdin() and "JSON" in R.stdin() and f"PWD={R.wt.resolve()}" in R.stubs.env("claude")
    # the issue changed after dispatch: the gate reads the live text; an unreadable issue keeps the copy, warns and says so in the prompt
    task = R.wt / ".ai-toolkit/task.md"
    task.parent.mkdir()
    task.write_text("# #9 old\n\nold body\n")
    R.stubs.reply("gh", '{"number":9,"title":"new","body":"new body"}')
    assert R.go(OK).returncode == 0
    assert R.stubs.calls("gh")[0][:3] == ["issue", "view", "9"] and task.read_text() == "# #9 new\n\nnew body\n" and "stale" not in R.stdin()
    R.stubs.reply("gh", "boom", rc=1)
    task.write_text("# #9 old\n\nold body\n")
    r = R.go(OK)
    assert r.returncode == 0 and task.read_text() == "# #9 old\n\nold body\n"
    assert "warning" in r.stderr and "may be stale" in R.stdin() and "JSON" in R.stdin()


@pytest.mark.parametrize("patch, word", [({"verdict": "REQUEST_CHANGES", "blockers": ["a.py:3 - off by one"]}, "off by one"), ({"tests_weakened": True}, "tests_weakened"),
                                         ({"tdd_followed": False}, "tdd_followed"), ({"blockers": ["x.py:1 - bug"]}, "bug")])
def test_request_changes_exits_3_with_blocker_lines_and_an_approve_that_contradicts_its_own_fields_is_rejected(R, patch, word):
    r = R.go({**OK, **patch})
    assert r.returncode == 3 and "BLOCKER:" in r.stdout and word in r.stdout


@pytest.mark.parametrize("out", [
    "", "Looks good to me!", "```json\nnot json\n```", json.dumps({**OK, "verdict": "MAYBE"}), json.dumps({**OK, "blockers": None}),
    json.dumps({k: v for k, v in OK.items() if k != "summary"}), json.dumps({**OK, "tdd_followed": "yes"}), "[1]", "{} trailing",
])
def test_an_unparseable_verdict_exits_1_after_one_retry_and_is_never_an_approve(R, out):
    r = R.go(out)
    assert r.returncode == 1 and "unparseable" in r.stderr and len(R.stubs.calls("claude")) == 2


def test_a_fenced_object_is_accepted_and_a_garbled_first_try_is_retried_once(R):
    R.stubs.reply("claude._p___model", "sorry, here you go", n=1)
    R.stubs.reply("claude._p___model", "```json\n" + json.dumps(OK) + "\n```", n=2)
    assert R.go(OK).returncode == 0 and len(R.stubs.calls("claude")) == 2


def test_a_missing_worktree_or_a_failing_claude_is_an_error_not_an_approve(R):
    R.stubs.reply("orca.worktree_show", "nope", rc=1)
    assert R.go(OK).returncode == 1
    R.stubs.reply("orca.worktree_show", R.show)
    assert R.go(OK, rc=1).returncode == 1
