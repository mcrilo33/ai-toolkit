import json
import os
import re
import stat
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
    def inbox(open_ids=(), answered=(), replies_only=(), pad=0):   # the Run's inbox: unanswered questions wait for the human
        rows = [{**msg("question", i), "run_id": "run_t", "thread_id": i} for i in [*open_ids, *answered]]
        rows += [{"id": f"r_{i}", "type": "status", "run_id": "run_t", "thread_id": i, "body": "approve", "payload": None} for i in [*answered, *replies_only]]
        rows += [{"id": f"x{i}", "type": "status", "run_id": "run_other", "thread_id": None, "body": "", "payload": None} for i in range(pad)]   # other Runs' mail
        stubs.reply("orca.orchestration_inbox", json.dumps({"result": {"messages": rows}}))

    def spool(mid, body):   # a queued human reply, as `coordinator.sh --reply` writes it
        (tmp_path / "state/run_t/replies").mkdir(parents=True, mode=0o700, exist_ok=True)
        (tmp_path / "state/run_t/replies" / mid).write_text(body + "\n")

    stubs.reply("dispatch.sh", '{"issue":1}')
    stubs.reply(key("dispatch.sh", "--next", "--dry-run"), "", rc=3)   # nothing ready
    stubs.reply("answer.sh", "approve\n")
    workers(row())
    mail()

    def go(*args, run_id="run_t", **env):
        cmd_env = {f"{n.split('.')[0].upper()}_CMD": cmds / n for n in ("dispatch.sh", "land.sh", "answer.sh", "notify")}
        return run(["bash", CO, *(["--run", run_id] if run_id else []), *args], cwd=repo.root,
                   **{"ORCA_TERMINAL_HANDLE": "term_c", "AI_TOOLKIT_POLL": 0, "COORD_MAX_TICKS": 1, "AITK_STATE_DIR": tmp_path / "state", "COORD_BELL_TTY": tmp_path / "bell", **cmd_env, **env})

    def trail():   # [("orca orchestration check ack", argv), ("gh issue edit", argv), ("land.sh", argv)...] in call order
        rows = (Path(os.environ["STUB_DIR"]) / "calls.log").read_text().splitlines()
        return [(" ".join([n, *a[:2]] if n in ("orca", "gh") else [n]) + (" ack" if "--ack" in a else ""), a)
                for n, *a in ([x for x in r.split("\x1f") if x != "--json"] for r in rows)]

    def calls(kind):
        return [a for k, a in trail() if k == kind]

    def blocked():   # label + comment + notification + release; the worktree is kept and nothing is re-dispatched
        ks = [k for k, _ in trail()]
        return (any(a[3:] == ["--add-label", "blocked"] for a in calls("gh issue edit")) and "gh issue comment" in ks and "notify" in ks
                and "orca orchestration worker-release" in ks and "--address" not in sum(stubs.calls("dispatch.sh"), []) and "orca worktree rm" not in ks)

    return type("C", (), dict(go=staticmethod(go), mail=staticmethod(mail), inbox=staticmethod(inbox), spool=staticmethod(spool), spooled=lambda m: (tmp_path / "state/run_t/replies" / m).exists(), workers=staticmethod(workers), row=staticmethod(row), wt=wt,
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


REPLY = "bash {}/scripts/coordinator.sh --run run_t --reply msg_q approve".format(V2)


@pytest.mark.parametrize("mode, rc", [("human", 0), ("auto", 1)])   # auto: the answerer found nothing usable. Never a blind approve
def test_a_question_for_the_human_is_notified_with_the_exact_reply_command_and_acked_without_waiting(C, mode, rc):
    C.stubs.reply("answer.sh", "", rc=rc)
    C.mail([msg("question", "msg_q", question="PLAN?")])
    assert C.go("--answer", mode).returncode == 0
    assert not C.calls("orca orchestration reply") and C.calls("orca orchestration check ack") and (len(C.stubs.calls("answer.sh")) == 1) == (mode == "auto")
    assert REPLY in C.calls("gh issue comment")[0][-1] and "revise:" in C.calls("gh issue comment")[0][-1] and "--reply msg_q" in C.stubs.calls("notify")[0][0]


def test_reply_queues_a_one_line_request_in_a_private_spool_outside_any_worktree_and_needs_no_orca(C, tmp_path):
    r = C.go("--reply", "msg_q", "revise:  use tmp", ORCA_TERMINAL_HANDLE="", AITK_STATE_DIR="")
    d = tmp_path / "home/.ai-toolkit/coordinator/run_t/replies"
    assert r.returncode == 0 and (d / "msg_q").read_text() == "revise: use tmp\n" and stat.S_IMODE(d.stat().st_mode) == 0o700
    assert not C.stubs.calls("orca") and not list(d.glob(".*"))
    assert C.go("--reply", "msg_q", "approve", ORCA_TERMINAL_HANDLE="").returncode == 0 and C.spooled("msg_q")   # AITK_STATE_DIR is a base: <dir>/<run-id>/replies


@pytest.mark.parametrize("args", [["--reply", "msg_q", "maybe"], ["--reply", "msg_q", "revise:"], ["--reply", "../x", "approve"], ["--reply", "msg_q"]])
def test_reply_refuses_a_bad_message_id_or_body_and_a_missing_run(C, tmp_path, args):
    assert C.go(*args).returncode == 2 and not C.spooled("msg_q") and not C.spooled("../x")
    assert C.go("--reply", "msg_q", "approve", run_id=None).returncode == 2


def test_a_queued_reply_is_sent_as_the_bound_consumer_deleted_and_commented_on_the_issue(C):
    C.inbox(["msg_q"])
    C.spool("msg_q", "revise: use tmp")
    assert C.go().returncode == 0
    rep = C.calls("orca orchestration reply")[0]
    assert (arg(rep, "--id"), arg(rep, "--body"), arg(rep, "--run"), arg(rep, "--from")) == ("msg_q", "revise: use tmp", "run_t", "term_c")
    assert not C.spooled("msg_q") and "use tmp" in C.calls("gh issue comment")[0][-1]


def test_a_reply_for_an_unknown_or_already_answered_question_is_dropped_and_a_failed_one_stays_queued(C):
    C.inbox(["msg_open"], answered=["msg_done"])
    C.spool("msg_done", "approve")
    C.spool("msg_nope", "approve")
    r = C.go()
    assert not C.calls("orca orchestration reply") and not C.spooled("msg_done") and not C.spooled("msg_nope") and r.stderr.count("dropped") == 2
    C.stubs.reply("orca.orchestration_reply", "boom", rc=1)
    C.spool("msg_open", "approve")
    C.go()
    assert C.spooled("msg_open")   # retried on the next wake


def test_a_queued_reply_is_only_dropped_when_its_question_is_seen_answered_or_the_inbox_page_was_not_truncated(C):
    C.inbox(pad=200)   # a full page: msg_old may be older than everything shown
    C.spool("msg_old", "approve")
    r = C.go()
    assert C.spooled("msg_old") and not C.calls("orca orchestration reply") and "truncated" in r.stderr   # kept and warned
    C.inbox(replies_only=["msg_old"], pad=199)   # ...but its reply row is on the page: positively answered
    C.go()
    assert not C.spooled("msg_old")


@pytest.mark.parametrize("body", ["rm -rf /", "revise:", "approve please", ""])
def test_a_spool_file_with_a_tampered_body_is_dropped_unsent(C, body):
    C.inbox(["msg_q"])
    C.spool("msg_q", body)
    r = C.go()
    assert not C.spooled("msg_q") and not C.calls("orca orchestration reply") and "dropped" in r.stderr


def test_worker_list_is_read_page_by_page_so_rounds_survive_more_than_a_page_of_dispatches(C):
    (Path(os.environ["STUB_DIR"]) / "orca.orchestration_worker_list.count").unlink(missing_ok=True)
    for n, rows, cur in ((1, [C.row("ctx_9", st="completed")], "c1"), (2, [C.row("ctx_1")], None)):
        C.stubs.reply("orca.orchestration_worker_list", json.dumps({"result": {"workers": rows, "page": {"nextCursor": cur}}}), n=n)
    C.stubs.reply("land.sh", "BLOCKER: x\n", rc=3)
    C.mail([msg("worker_done", outcome="succeeded")])
    C.go("--cap", "0")
    first, second = C.calls("orca orchestration worker-list")[:2]
    assert "--cursor" not in first and arg(second, "--cursor") == "c1"
    assert [a[0] for a in C.stubs.calls("dispatch.sh")].count("--address") == 1   # ctx_1 sits on page 2; the 2 rows count as one spent round


@pytest.mark.parametrize("open_ids, ms", [([], "300000"), (["msg_q"], "30000")])
def test_the_wait_is_30_s_while_a_human_question_is_pending_and_300_s_otherwise(C, open_ids, ms):
    C.inbox(open_ids)
    C.go()
    assert arg(C.calls("orca orchestration check")[0], "--timeout-ms") == ms


def test_a_pending_human_question_does_not_stop_dispatch_or_land(C):
    C.inbox(["msg_q"])
    C.stubs.reply(key("dispatch.sh", "--next", "--dry-run"), "5\n")
    C.mail([msg("worker_done", "msg_d", outcome="succeeded")])
    C.go("--answer", "human", "--cap", "2")
    assert ["5"] in C.stubs.calls("dispatch.sh") and C.stubs.calls("land.sh") == [["--review", "1"]]


@pytest.mark.parametrize("kind, extra", [("worker_done", {"outcome": "failed"}), ("escalation", {})])
def test_a_failed_or_escalating_worker_blocks_the_issue(C, kind, extra):
    C.mail([msg(kind, **extra)])
    C.go()
    assert C.blocked() and not C.stubs.calls("land.sh") and C.calls("orca orchestration check ack")


def test_a_successful_worker_is_landed_with_review_and_one_delivery_is_acked_once_after_all_its_messages(C):
    C.stubs.reply("orca.orchestration_reply", "boom", rc=1)   # a failing handler must not stop the rest, nor the ack
    C.mail([msg("question", "msg_q", question="q"), msg("worker_done", "msg_d", outcome="succeeded")])
    C.go()
    assert C.stubs.calls("land.sh") == [["--review", "1"]] and len(C.calls("orca orchestration check ack")) == 1
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
        if spent < limit:   # a fresh terminal through dispatch.sh (same launch path, shim env), a new Task whose spec is the reason
            d = [a for a in C.stubs.calls("dispatch.sh") if a[0] == "--address"][-1]
            assert d[1].startswith("address:") and d[2] == "1" and (rc != 3 or "(1) a.py:3 - bug (2) b.py:1" in d[1]) and not C.calls("gh issue edit")
    assert len([a for a in C.stubs.calls("dispatch.sh") if a[0] == "--address"]) == limit and C.calls("gh issue edit")[0][3:] == ["--add-label", "blocked"]


@pytest.mark.parametrize("rc, out", [(3, "no verdict\n"), (4, "land.sh: CI for abc timed out\n"), (4, "land.sh: main keeps moving; land again\n"),
                                     (2, "land.sh: refused: the spoke worktree is dirty\n"), (1, "land.sh: boom\n")])
def test_a_land_that_the_worker_cannot_fix_blocks_at_once(C, rc, out):
    C.stubs.reply("land.sh", out, rc=rc)
    C.mail([msg("worker_done", outcome="succeeded")])
    C.go()
    assert C.blocked()


@pytest.mark.parametrize("cleanup_rc", [0, 6])
def test_landed_but_cleanup_incomplete_finishes_once_and_parks_a_still_open_issue(C, cleanup_rc):
    C.stubs.reply(key("land.sh", "--review", "1"), "land.sh: landed abc but cleanup is incomplete\n", rc=6)
    C.stubs.reply(key("land.sh", "--cleanup-only", "--branch"), "", rc=cleanup_rc)
    C.mail([msg("worker_done", outcome="succeeded")])
    C.go()
    co = C.stubs.calls("land.sh")[1]
    assert co[:3] == ["--cleanup-only", "--branch", "1-x"] and arg(co, "--tip") and co[-1] == "1" and len(C.stubs.calls("land.sh")) == 2
    assert bool(C.calls("gh issue edit")) == (cleanup_rc == 6)   # else `--next` would pick the open issue again


def test_a_worker_that_exited_without_worker_done_is_retried_once_on_the_tenth_empty_wait_then_blocked(C):
    C.workers(C.row(lv="exited"))
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


def test_status_prints_the_run_workers_and_unanswered_questions_with_their_reply_command_and_changes_nothing(C):
    C.inbox(["msg_q"], answered=["msg_a"])
    r = C.go("--status", ORCA_TERMINAL_HANDLE="")   # read-only: no coordinator terminal needed
    assert r.returncode == 0 and "run_t" in r.stdout and "ctx_1" in r.stdout and REPLY in r.stdout and "msg_a" not in r.stdout
    assert [k for k in C.kinds() if k.startswith("orca orchestration") and k.split()[2] not in ("worker-list", "inbox")] == []


LONG_Q = "\n".join(f"plan line {i}: " + "word " * 30 for i in range(40))   # long lines AND too many of them


def lines_before(out, needle):   # the output lines above the first line that contains needle
    ls = out.splitlines()
    return ls[:next(i for i, ln in enumerate(ls) if needle in ln)]


def test_the_human_sees_the_question_wrapped_and_capped_above_the_reply_line(C):
    C.mail([msg("question", "msg_q", question=LONG_Q)])
    r = C.go("--answer", "human")
    shown = [ln for ln in lines_before(r.stdout, "--reply msg_q") if ln.startswith("  | ")]
    assert r.returncode == 0 and 20 <= len(shown) <= 25 and "plan line 0:" in shown[0] and all(len(ln) <= 110 for ln in shown)
    assert "plan line 30" not in r.stdout   # capped, not dumped


def test_status_shows_each_open_question_text_above_its_reply_command(C):
    C.stubs.reply("orca.orchestration_inbox", json.dumps({"result": {"messages": [{**msg("question", "msg_q", question="PLAN: add hello.py first"), "run_id": "run_t", "thread_id": "msg_q"}]}}))
    out = C.go("--status", ORCA_TERMINAL_HANDLE="").stdout
    assert "  | PLAN: add hello.py first" in lines_before(out, "--reply msg_q")


def test_a_failing_desktop_notification_is_warned_about_not_swallowed(C, stubs):
    (Path(os.environ["STUB_DIR"]) / "bin" / "osascript").write_text(STUB)
    (Path(os.environ["STUB_DIR"]) / "bin" / "osascript").chmod(0o755)
    stubs.reply("osascript", "", rc=1)
    C.mail([msg("question", "msg_q", question="PLAN?")])
    r = C.go("--answer", "human", NOTIFY_CMD="")   # empty = the real osascript path
    assert r.returncode == 0 and "osascript" in r.stderr and "warning" in r.stderr and C.stubs.calls("osascript")


def test_a_pending_human_gate_sets_the_worktree_comment_and_rings_the_bell_then_the_reply_clears_the_comment(C, tmp_path):
    C.mail([msg("question", "msg_q", question="PLAN: add hello.py\nthen a test")])
    assert C.go("--answer", "human").returncode == 0
    cm = [a for a in C.calls("orca worktree set")]
    assert len(cm) == 1 and arg(cm[0], "--worktree") == f"path:{C.wt}"
    assert arg(cm[0], "--comment") == f"GATE waiting: PLAN: add hello.py then a test | reply: coordinator.sh --reply msg_q approve"
    assert (tmp_path / "bell").read_text() == "\a"   # one bell per gate, on the coordinator's terminal
    C.inbox(["msg_q"]); C.spool("msg_q", "approve")
    assert C.go().returncode == 0
    last = C.calls("orca worktree set")[-1]   # Orca ignores --comment "": the comment is overwritten, never cleared
    assert arg(last, "--comment") == "gate answered by the human: approve" and arg(last, "--worktree") == f"path:{C.wt}"


def test_an_auto_answered_gate_neither_rings_nor_comments(C, tmp_path):
    C.mail([msg("question", "msg_q", question="PLAN?")])
    assert C.go("--answer", "auto").returncode == 0
    assert not C.calls("orca worktree set") and not (tmp_path / "bell").exists()
