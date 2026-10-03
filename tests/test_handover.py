"""coordinator.sh hand-over (WP7): the session takes the Run back (consumer_fenced), --stop, and who holds the Run."""
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest
from test_coordinator import C, arg, key, msg  # noqa: F401  (C is the shared fixture)

SESSION = "term_session"


def holder(handle):
    return json.dumps({"result": {"run": {"id": "run_t", "coordinator_handle": handle, "consumer_generation": 3}}})


def show(C, *handles):   # run-show replies in call order (the last one repeats); "term_c" is the coordinator's own handle
    count = Path(os.environ["STUB_DIR"]) / "orca.orchestration_run_show.count"
    count.unlink(missing_ok=True)
    C.stubs.reply("orca.orchestration_run_show", holder(handles[-1]))
    for n, h in enumerate(handles[:-1], 1):
        C.stubs.reply("orca.orchestration_run_show", holder(h), n=n)


def hold_file(tmp_path, pid, handle="term_c", mode="auto until 23:00"):
    d = tmp_path / "state/run_t"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"holder.{pid}").write_text(f"{handle} {mode}\n")
    return d / f"holder.{pid}"


def fake_coordinator():   # a live process whose command is coordinator.sh: what a holder.<pid> file must point at
    return subprocess.Popen(["coordinator.sh", "60"], executable="/bin/sleep")


def reap(p):
    p.kill()
    p.wait()


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def no_ack_no_retry(C):
    assert not C.calls("orca orchestration check ack") and len(C.calls("orca orchestration run-use")) == 1


def test_a_fenced_wait_ends_the_coordinator_with_exit_0_naming_the_new_holder_and_never_retries(C):
    show(C, "term_c", "term_c", SESSION)   # bind, loop top, then the wait: fenced
    C.stubs.reply("orca.orchestration_check", '{"error":{"code":"consumer_fenced"}}', rc=1)
    r = C.go(COORD_MAX_TICKS=5)
    assert r.returncode == 0 and f"Run run_t taken back by {SESSION}" in r.stdout
    assert len(C.calls("orca orchestration check")) == 1
    no_ack_no_retry(C)


def test_a_fence_with_an_unreadable_holder_still_exits_0(C):
    C.stubs.reply("orca.orchestration_check", '{"error":{"code":"consumer_fenced"}}', rc=1)
    r = C.go(COORD_MAX_TICKS=5)
    assert r.returncode == 0 and "taken back by another terminal" in r.stdout and len(C.calls("orca orchestration check")) == 1


def test_a_takeover_during_a_batch_stops_before_the_next_handler_and_before_the_ack(C):
    show(C, "term_c", "term_c", SESSION)   # bind, loop top, then before the first message: taken
    C.mail([msg("worker_done", "msg_d", outcome="succeeded")])
    r = C.go()
    assert r.returncode == 0 and "taken back by" in r.stdout and not C.stubs.calls("land.sh")
    no_ack_no_retry(C)


def test_a_takeover_while_a_dispatch_fails_exits_instead_of_blocking_the_issue(C):
    show(C, "term_c", "term_c", SESSION)   # the fenced dispatch.sh must not label the issue blocked
    C.stubs.reply(key("dispatch.sh", "--next", "--dry-run"), "5\n")
    C.stubs.reply(key("dispatch.sh", "5"), "boom", rc=1)
    r = C.go()
    assert r.returncode == 0 and "taken back by" in r.stdout and not C.calls("gh issue edit") and not C.calls("orca orchestration check")


def test_run_use_never_fences_the_new_holder_a_failure_is_just_an_error(C):
    C.stubs.reply("orca.orchestration_run_use", "boom", rc=1)
    r = C.go()
    assert r.returncode == 1 and "run-use failed" in r.stderr and not C.calls("orca orchestration check")


def test_the_holder_file_names_the_handle_and_mode_while_running_and_is_gone_after_exit(C, tmp_path):
    (tmp_path / "cmds/dispatch.sh").write_text('#!/bin/sh\ncat "$AITK_STATE_DIR"/run_t/holder.* > "$AITK_STATE_DIR/seen" 2>&1\nexit 3\n')
    assert C.go("--answer", "human", "--until", "23:59", "--drain", AI_TOOLKIT_NOW="10:00").returncode == 0
    assert (tmp_path / "state/seen").read_text() == "term_c human until 23:59 drain\n"
    assert not list((tmp_path / "state/run_t").glob("holder.*"))


def test_exiting_removes_only_its_own_holder_file(C, tmp_path):
    other = hold_file(tmp_path, 4242, "term_other")   # another coordinator.sh's file survives this one's cleanup
    assert C.go().returncode == 0 and other.exists()


def test_stop_binds_the_caller_to_the_run_and_returns_at_once_when_no_coordinator_holds_it(C):
    show(C, SESSION)
    r = C.go("--stop", ORCA_TERMINAL_HANDLE="term_me")
    assert r.returncode == 0 and "no coordinator.sh" in r.stdout
    assert C.calls("orca orchestration run-use")[0][2:6] == ["--id", "run_t", "--from", "term_me"]
    assert not C.calls("orca orchestration check") and not C.calls("orca orchestration worker-list") and not C.stubs.calls("dispatch.sh")


def test_stop_waits_until_the_coordinator_that_held_the_run_is_gone(C, tmp_path):
    show(C, "term_c")
    sleeper = fake_coordinator()
    hold_file(tmp_path, sleeper.pid)
    t = threading.Timer(0.3, lambda: reap(sleeper))
    t.start()
    try:
        r = C.go("--stop", ORCA_TERMINAL_HANDLE="term_me", COORD_STOP_TRIES=400, COORD_STOP_POLL=0.05)
    finally:
        t.cancel(); reap(sleeper)
    assert r.returncode == 0 and "stopped" in r.stdout and C.calls("orca orchestration run-use")


def test_stop_reports_a_coordinator_still_finishing_a_step_and_exits_1(C, tmp_path):
    show(C, "term_c")
    p = fake_coordinator()
    try:
        hold_file(tmp_path, p.pid)
        r = C.go("--stop", ORCA_TERMINAL_HANDLE="term_me", COORD_STOP_TRIES=1, COORD_STOP_POLL=0)
    finally:
        reap(p)
    assert r.returncode == 1 and "still" in r.stderr and C.calls("orca orchestration run-use")   # the Run is taken anyway: it exits at its next step


@pytest.mark.parametrize("seen", [["term_me"], []])   # a second --stop: run-show already names the caller (or fails): the loop's holder.<pid> still counts
def test_a_second_stop_while_the_loop_is_still_landing_does_not_return_0(C, tmp_path, seen):
    show(C, *(seen or [""]))
    p = fake_coordinator()
    try:
        hold_file(tmp_path, p.pid)
        r = C.go("--stop", ORCA_TERMINAL_HANDLE="term_me", COORD_STOP_TRIES=1, COORD_STOP_POLL=0)
    finally:
        reap(p)
    assert r.returncode == 1 and "still" in r.stderr


def test_a_holder_file_whose_pid_is_now_some_other_process_is_stale(C, tmp_path):
    show(C, "term_c")
    hold_file(tmp_path, os.getpid())   # alive, but its command is not coordinator.sh: a recycled pid
    assert C.go("--stop", ORCA_TERMINAL_HANDLE="term_me", COORD_STOP_TRIES=1, COORD_STOP_POLL=0).returncode == 0


def test_stop_ignores_the_stale_file_of_a_dead_coordinator(C, tmp_path):
    show(C, "term_c")
    hold_file(tmp_path, dead_pid())
    assert C.go("--stop", ORCA_TERMINAL_HANDLE="term_me", COORD_STOP_TRIES=1, COORD_STOP_POLL=0).returncode == 0


def test_stop_needs_an_orca_terminal_and_a_run(C):
    show(C, SESSION)
    assert C.go("--stop", ORCA_TERMINAL_HANDLE="").returncode == 1
    assert C.go("--stop", run_id=None, ORCA_TERMINAL_HANDLE="term_me").returncode == 2


def test_status_says_who_holds_the_run_and_the_mode(C, tmp_path):
    show(C, "term_c")
    p = fake_coordinator()
    try:
        hold_file(tmp_path, p.pid, "term_c", "auto until 23:00 drain")
        out = C.go("--status", ORCA_TERMINAL_HANDLE="").stdout
    finally:
        reap(p)
    assert "held by: coordinator.sh (auto until 23:00 drain)" in out and "term_c" in out
    show(C, SESSION)   # the session took it back: the file names another handle, so it is not the holder
    out = C.go("--status", ORCA_TERMINAL_HANDLE="").stdout
    assert f"held by: a session ({SESSION})" in out and "coordinator.sh (" not in out


def test_status_ignores_a_holder_file_whose_process_is_dead_and_names_nobody_when_unbound(C, tmp_path):
    show(C, "term_c")
    hold_file(tmp_path, dead_pid())
    assert "held by: a session (term_c)" in C.go("--status", ORCA_TERMINAL_HANDLE="").stdout
    C.stubs.reply("orca.orchestration_run_show", '{"result":{"run":{"id":"run_t"}}}')
    assert "held by: nobody" in C.go("--status", ORCA_TERMINAL_HANDLE="").stdout


def test_a_takeover_during_answer_sh_exits_instead_of_asking_the_human(C):
    show(C, "term_c", "term_c", "term_c", SESSION)   # bind, tick top, the message, then the failed reply: taken
    C.stubs.reply("orca.orchestration_reply", '{"error":{"code":"consumer_fenced"}}', rc=1)
    C.mail([msg("question", "msg_q", question="PLAN?")])
    r = C.go("--answer", "auto")
    assert r.returncode == 0 and "taken back by" in r.stdout and not C.calls("gh issue comment") and not C.stubs.calls("notify")
    assert not C.calls("orca worktree set") and not C.calls("orca orchestration check ack")


def test_a_replayed_worker_done_for_an_already_closed_issue_is_acked_without_landing_or_blocking(C):
    C.stubs.reply("gh.issue_view", "CLOSED\n")
    C.mail([msg("worker_done", "msg_d", outcome="succeeded")])
    r = C.go()
    assert r.returncode == 0 and "already landed" in r.stdout
    assert not C.stubs.calls("land.sh") and not C.calls("gh issue edit") and not C.stubs.calls("notify") and C.calls("orca orchestration check ack")


@pytest.mark.parametrize("bad", ["../x", "run_", "run_A1", "x run_1", "run_1/../../y"])
def test_a_run_id_must_look_like_an_orca_run_id(C, bad):
    assert C.go(run_id=bad).returncode == 2
    assert C.go("--reply", "msg_q", "approve", run_id=bad).returncode == 2 and not C.stubs.calls("orca")
