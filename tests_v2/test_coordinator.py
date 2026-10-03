import json
import os
import re
from pathlib import Path

import pytest
from conftest import STUB, V2

CO = str(V2 / "scripts/coordinator.sh")
EMPTY = json.dumps({"result": {"messages": [], "deliveryId": None}})


def key(name, a1="", a2=""):   # the stub's reply key for `name a1 a2`
    return name + "." + re.sub(r"[^A-Za-z0-9_]", "_", f"{a1}_{a2}")


def msg(kind, mid="msg_1", **payload):
    return {"id": mid, "type": kind, "body": "the body", "payload": json.dumps({"taskId": "task_ctx_1", "dispatchId": "ctx_1", **payload})}


def arg(a, flag):
    return a[a.index(flag) + 1]


def in_order(kinds, *seq):
    assert [kinds.index(s) for s in seq] == sorted(kinds.index(s) for s in seq)


@pytest.fixture
def C(stubs, repo, run, tmp_path):
    cmds, wt = tmp_path / "cmds", str(repo.wt("1-x"))
    cmds.mkdir()
    for n in ("dispatch.sh", "land.sh", "answer.sh", "notify"):   # sub-commands are stubs that also record their stdin
        (cmds / n).write_text(STUB.replace("n=$(basename", 'cat > "$STUB_DIR/$(basename "$0").stdin"; n=$(basename', 1))
        (cmds / n).chmod(0o755)

    def row(d="ctx_1", st="dispatched", lv="live"):
        return {"dispatchId": d, "taskId": f"task_{d}", "dispatchStatus": st, "agentTerminalHandle": "term_w",
                "resource": {"worktreeId": f"r::{wt}"}, "projection": {"liveness": {"verdict": lv}}}

    def workers(*rows):
        stubs.reply("orca.orchestration_worker_list", json.dumps({"result": {"workers": list(rows)}}))

    def mail(*batches):   # one batch per wait; a non-empty batch costs a second check call, its ack
        n = 0
        (tmp_path / "stubs/orca.orchestration_check.count").unlink(missing_ok=True)   # numbering restarts for each coordinator run
        for i, b in enumerate(batches):
            n += 1
            stubs.reply("orca.orchestration_check", json.dumps({"result": {"messages": b, "deliveryId": f"d{i}" if b else None}}), n=n)
            n += 1 if b else 0
        stubs.reply("orca.orchestration_check", EMPTY)

    stubs.reply("orca.worktree_list", json.dumps({"result": {"worktrees": [{"path": wt, "linkedIssue": 1}]}}))
    stubs.reply("orca.orchestration_run_create", '{"result":{"run":{"id":"run_new"}}}')
    stubs.reply("orca.terminal_show", '{"result":{"terminal":{"agentIdentity":"claude"}}}')
    stubs.reply("dispatch.sh", "", rc=3)   # nothing ready
    stubs.reply("answer.sh", "approve\n")
    workers(row())
    mail()

    def go(*args, run_id="run_t", **env):
        cmd_env = {f"{n.split('.')[0].upper()}_CMD": cmds / n for n in ("dispatch.sh", "land.sh", "answer.sh", "notify")}
        return run(["bash", CO, *(["--run", run_id] if run_id else []), *args], cwd=repo.root,
                   **{"ORCA_TERMINAL_HANDLE": "term_c", "AI_TOOLKIT_POLL": 0, "COORD_MAX_TICKS": 1, "COORD_HUMAN_IN": "", **cmd_env, **env})

    def trail():   # [("orca orchestration check ack", argv), ("gh issue edit", argv), ("land.sh", argv)...] in call order
        rows = (Path(os.environ["STUB_DIR"]) / "calls.log").read_text().splitlines()
        return [(" ".join([n, *a[:2]] if n in ("orca", "gh") else [n]) + (" ack" if "--ack" in a else ""), a)
                for n, *a in ([x for x in r.split("\x1f") if x != "--json"] for r in rows)]

    def calls(kind):
        return [a for k, a in trail() if k == kind]

    def blocked():   # label + comment + notification + release; the worktree is kept and nothing is re-dispatched
        ks = [k for k, _ in trail()]
        return (any(a[3:] == ["--add-label", "blocked"] for a in calls("gh issue edit")) and "gh issue comment" in ks and "notify" in ks
                and "orca orchestration worker-release" in ks and "orca orchestration worker-start" not in ks and "orca worktree rm" not in ks)

    return type("C", (), dict(go=staticmethod(go), mail=staticmethod(mail), workers=staticmethod(workers), row=staticmethod(row), wt=wt,
                              calls=staticmethod(calls), blocked=staticmethod(blocked), stubs=stubs, kinds=staticmethod(lambda: [k for k, _ in trail()]),
                              stdin=staticmethod(lambda n: (tmp_path / f"stubs/{n}.stdin").read_text())))


def test_a_given_run_is_rebound_without_one_a_run_is_created_and_every_wait_names_it_and_the_wake_types(C):
    assert C.go().returncode == 0 and C.calls("orca orchestration run-use")[0][2:6] == ["--id", "run_t", "--from", "term_c"]
    w = C.calls("orca orchestration check")[0]
    assert arg(w, "--run") == "run_t" and arg(w, "--terminal") == "term_c" and "--wait" in w and arg(w, "--types") == "question,worker_done,escalation"
    r = C.go(run_id=None)
    assert "run_new" in r.stdout and arg(C.calls("orca orchestration run-create")[0], "--from") == "term_c"
    assert arg(C.calls("orca orchestration check")[-1], "--run") == "run_new"


def test_a_run_taken_over_by_another_terminal_stops_the_coordinator(C):
    C.stubs.reply("orca.orchestration_run_use", '{"error":{"code":"consumer_fenced"}}', rc=1)
    r = C.go()
    assert r.returncode == 1 and "another" in r.stderr and not C.calls("orca orchestration check")
    C.stubs.reply("orca.orchestration_run_use", "{}")
    C.stubs.reply("orca.orchestration_check", '{"error":{"code":"consumer_fenced"}}', rc=1)
    assert C.go().returncode == 1


def test_question_auto_runs_the_answerer_in_the_workers_worktree_replies_then_acks(C):
    C.stubs.reply("answer.sh", "revise: drop the extra file\nWARN: touches CI\n")
    C.mail([msg("question", "msg_q", question="PLAN: do X?")])
    assert C.go("--answer", "auto").returncode == 0
    assert C.stubs.calls("answer.sh") == [[C.wt]] and C.stdin("answer.sh") == "PLAN: do X?"
    rep = C.calls("orca orchestration reply")[0]
    assert (arg(rep, "--id"), arg(rep, "--body"), arg(rep, "--run")) == ("msg_q", "revise: drop the extra file", "run_t")
    in_order(C.kinds(), "answer.sh", "orca orchestration reply", "orca orchestration check ack")   # the warning goes to the human
    assert arg(C.calls("orca orchestration check ack")[0], "--ack") == "d0" and "touches CI" in C.calls("gh issue comment")[0][-1] and C.stubs.calls("notify")


@pytest.mark.parametrize("mode, rc", [("human", 0), ("auto", 1)])   # auto: the answerer found nothing usable. Never a blind approve
def test_the_human_is_prompted_on_the_coordinator_terminal_and_what_is_typed_is_replied(C, tmp_path, mode, rc):
    C.stubs.reply("answer.sh", "", rc=rc)
    (tmp_path / "typed").write_text("maybe\nrevise:\nrevise:  use a tmp dir\n")   # two invalid lines are re-prompted
    C.mail([msg("question", "msg_q", question="PLAN?")])
    r = C.go("--answer", mode, COORD_HUMAN_IN=tmp_path / "typed")
    rep = C.calls("orca orchestration reply")[0]
    assert (arg(rep, "--id"), arg(rep, "--body")) == ("msg_q", "revise: use a tmp dir") and "PLAN?" in r.stdout and C.stubs.calls("notify")
    assert (len(C.stubs.calls("answer.sh")) == 1) == (mode == "auto") and "terminal send --terminal term_c" in C.calls("gh issue comment")[0][-1]
    in_order(C.kinds(), "notify", "orca orchestration reply", "orca orchestration check ack")


@pytest.mark.parametrize("mode, rc, typed", [("human", 0, ""), ("auto", 1, None)])
def test_a_question_nobody_answers_is_acked_and_left_parked_with_the_takeover_hint(C, tmp_path, mode, rc, typed):
    C.stubs.reply("answer.sh", "", rc=rc)
    env = {}
    if typed is not None:   # a terminal that gives no input (EOF), or none at all
        (tmp_path / "typed").write_text(typed)
        env = {"COORD_HUMAN_IN": tmp_path / "typed"}
    C.mail([msg("question", "msg_q", question="q")])
    C.go("--answer", mode, **env)
    assert not C.calls("orca orchestration reply") and C.calls("orca orchestration check ack") and C.stubs.calls("notify")
    assert "run-use --id run_t" in C.calls("gh issue comment")[0][-1]


def test_human_mode_without_a_terminal_to_ask_on_is_refused(C):
    assert C.go("--answer", "human").returncode == 2


@pytest.mark.parametrize("kind, extra", [("worker_done", {"outcome": "failed"}), ("escalation", {})])
def test_a_failed_or_escalating_worker_blocks_the_issue(C, kind, extra):
    C.mail([msg(kind, **extra)])
    C.go()
    assert C.blocked() and not C.stubs.calls("land.sh") and C.calls("orca orchestration check ack")


def test_a_successful_worker_is_landed_with_review_and_one_delivery_is_acked_once_after_all_its_messages(C):
    C.stubs.reply("orca.orchestration_reply", "boom", rc=1)   # a failing handler must not stop the rest, nor the ack
    C.mail([msg("question", "msg_q", question="q"), msg("worker_done", "msg_d", outcome="succeeded")])
    C.go()
    assert C.stubs.calls("land.sh") == [["--review", "--dispatch", "ctx_1", "1"]] and len(C.calls("orca orchestration check ack")) == 1
    assert "msg_q" in C.calls("gh issue comment")[0][-1] and C.stubs.calls("notify")   # the unreplied question is handed to the human
    in_order(C.kinds(), "orca orchestration reply", "land.sh", "orca orchestration check ack")


@pytest.mark.parametrize("rc, out, limit", [(3, "BLOCKER: a.py:3 - bug\nBLOCKER: b.py:1 - no test\n", 2), (4, "land.sh: CI is red for abc\n", 1),
                                            (5, "land.sh: merge conflict: main into 1-x\n", 1)])
def test_a_rejected_land_re_dispatches_the_reason_to_the_same_worker_a_bounded_number_of_times(C, rc, out, limit):
    C.stubs.reply("land.sh", out, rc=rc)
    for spent in range(limit + 1):   # rounds already spent = dispatches on this worktree - 1
        C.workers(*[C.row(f"ctx_{i}", st="completed") for i in range(1, spent + 1)], C.row(f"ctx_{spent + 1}"))
        C.mail([msg("worker_done", dispatchId=f"ctx_{spent + 1}", outcome="succeeded")])
        C.go()
        if spent < limit:
            ws = C.calls("orca orchestration worker-start")[-1]
            assert arg(ws, "--terminal") == "term_w" and arg(ws, "--worktree") == f"path:{C.wt}" and "--task" not in ws and "--retry-of" not in ws
            assert arg(ws, "--spec").startswith("address:") and (rc != 3 or "(1) a.py:3 - bug (2) b.py:1" in arg(ws, "--spec"))
            assert not C.calls("gh issue edit")
    assert len(C.calls("orca orchestration worker-start")) == limit and C.calls("gh issue edit")[0][3:] == ["--add-label", "blocked"]   # then blocked


def test_a_worker_terminal_that_is_no_longer_claude_blocks_instead_of_re_dispatching(C):
    C.stubs.reply("land.sh", "BLOCKER: x\n", rc=3)
    C.stubs.reply("orca.terminal_show", '{"result":{"terminal":{"agentIdentity":null}}}')
    C.mail([msg("worker_done", outcome="succeeded")])
    C.go(COORD_AGENT_TRIES=2)
    assert C.blocked() and len(C.calls("orca terminal show")) == 2


@pytest.mark.parametrize("rc, out", [(3, "no verdict\n"), (4, "land.sh: CI for abc timed out\n"), (4, "land.sh: main keeps moving; land again\n"),
                                     (2, "land.sh: refused: the spoke worktree is dirty\n"), (1, "land.sh: boom\n")])
def test_a_land_that_the_worker_cannot_fix_blocks_at_once(C, rc, out):
    C.stubs.reply("land.sh", out, rc=rc)
    C.mail([msg("worker_done", outcome="succeeded")])
    C.go()
    assert C.blocked()


@pytest.mark.parametrize("cleanup_rc", [0, 6])
def test_landed_but_cleanup_incomplete_finishes_once_and_parks_a_still_open_issue(C, cleanup_rc):
    C.stubs.reply(key("land.sh", "--review", "--dispatch"), "land.sh: landed abc but cleanup is incomplete\n", rc=6)
    C.stubs.reply(key("land.sh", "--cleanup-only", "--branch"), "", rc=cleanup_rc)
    C.mail([msg("worker_done", outcome="succeeded")])
    C.go()
    co = C.stubs.calls("land.sh")[1]
    assert co[:3] == ["--cleanup-only", "--branch", "1-x"] and arg(co, "--tip") and co[-1] == "1" and len(C.stubs.calls("land.sh")) == 2
    assert bool(C.calls("gh issue edit")) == (cleanup_rc == 6)   # else `--next` would pick the open issue again


def test_a_worker_that_exited_without_worker_done_is_retried_once_on_the_tenth_empty_wait_then_blocked(C):
    C.workers(C.row(lv="exited"))
    C.stubs.reply(key("dispatch.sh", "--retry-of", "ctx_1"), '{"issue":1}')
    C.go("--cap", "1", COORD_MAX_TICKS=9)
    assert not C.stubs.calls("dispatch.sh")
    C.go("--cap", "1", COORD_MAX_TICKS=10)
    assert C.stubs.calls("dispatch.sh")[-1] == ["--retry-of", "ctx_1", "--task", "task_ctx_1", "1"]
    C.workers(C.row(st="completed", lv="exited"))   # a settled worker's process is expected to be gone
    C.go("--cap", "0", COORD_MAX_TICKS=10)
    assert len(C.stubs.calls("dispatch.sh")) == 1
    C.workers(C.row("ctx_1", st="failed", lv="exited"), C.row("ctx_2", lv="live"))   # the relaunched worker is alive: left alone
    C.go("--cap", "1", COORD_MAX_TICKS=10)
    assert len(C.stubs.calls("dispatch.sh")) == 1 and not C.calls("gh issue edit")
    C.workers(C.row("ctx_1", st="failed", lv="exited"), C.row("ctx_2", lv="exited"))   # it died too
    C.go("--cap", "1", COORD_MAX_TICKS=10)
    assert C.blocked() and len(C.stubs.calls("dispatch.sh")) == 1


def test_slots_are_filled_up_to_the_cap_and_a_failed_dispatch_is_cleaned_up_and_blocks_the_issue(C):
    C.stubs.reply(key("dispatch.sh", "--next", "--dry-run"), "5\n")
    C.stubs.reply(key("dispatch.sh", "5"), '{"issue":5}')
    C.go("--cap", "2")
    assert C.stubs.calls("dispatch.sh") == [["--next", "--dry-run"], ["5"]]   # one live worker + cap 2 = one more
    C.go("--cap", "1")
    assert len(C.stubs.calls("dispatch.sh")) == 2
    C.stubs.reply(key("dispatch.sh", "5"), "boom", rc=1)
    C.go("--cap", "2")
    assert C.calls("orca worktree rm")[0][2:4] == ["--worktree", "issue:5"] and C.calls("gh issue edit")[0][:3] == ["issue", "edit", "5"]


def test_drain_stops_when_nothing_is_ready_and_no_worker_is_live_but_not_before(C):
    C.workers(C.row(st="completed"))
    assert C.go("--drain", COORD_MAX_TICKS=5).returncode == 0 and not C.calls("orca orchestration check")
    C.workers(C.row())
    assert C.go("--drain", COORD_MAX_TICKS=2).returncode == 0 and len(C.calls("orca orchestration check")) == 2


@pytest.mark.parametrize("now, until, runs", [("10:00", "10:00", False), ("10:30", "10:00", True), ("09:59", "10:00", True), ("23:30", "00:30", True)])
def test_until_stops_the_loop_at_the_clock_time_and_wraps_past_midnight(C, now, until, runs):
    C.go("--until", until, AI_TOOLKIT_NOW=now)
    assert bool(C.calls("orca orchestration check")) == runs


def test_status_prints_the_run_workers_and_unanswered_questions_and_changes_nothing(C):
    rows = [{**msg("question", i), "run_id": "run_t", "thread_id": i} for i in ("msg_q", "msg_a")]
    rows.append({"id": "msg_r", "type": "status", "run_id": "run_t", "thread_id": "msg_a", "body": "approve", "payload": None})   # answers msg_a
    C.stubs.reply("orca.orchestration_inbox", json.dumps({"result": {"messages": rows}}))
    r = C.go("--status")
    assert r.returncode == 0 and "run_t" in r.stdout and "ctx_1" in r.stdout and "msg_q" in r.stdout and "msg_a" not in r.stdout
    assert [k for k in C.kinds() if k.startswith("orca orchestration") and k.split()[2] not in ("worker-list", "inbox")] == []
