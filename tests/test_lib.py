import json
import time

import pytest
from conftest import V2


def lib(run, code, **env):
    return run(["bash", "-euo", "pipefail", "-c", f". {V2}/scripts/lib.sh; {code}"], **env)


def test_env_precedence_is_caller_then_local_then_defaults(run, tmp_path):
    (tmp_path / "d.env").write_text('A=default\nB=default\nC="two words"\nD=default\n# c\n')
    (tmp_path / "l.env").write_text("B=local\nD=\n")
    r = lib(run, 'load_env; echo "$A|$B|$C|$D"', AI_TOOLKIT_ENV=tmp_path / "d.env", AI_TOOLKIT_LOCAL_ENV=tmp_path / "l.env", A="caller")
    assert r.stdout.strip() == "caller|local|two words|"   # an empty local value opts out of a non-empty default
    shipped = lib(run, 'unset DISPATCH_LABEL; load_env; echo "$DISPATCH_LABEL"', AI_TOOLKIT_LOCAL_ENV=tmp_path / "none.env")
    assert shipped.stdout.strip() == "ai-toolkit"   # the dispatch label is on by default
    assert len(lib(run, 'load_env; echo "$SPOKE_MODEL $BASE_BRANCH $OTEL_ENDPOINT"').stdout.split()) == 3


def test_orca_mutate_replays_an_unsettled_call_with_the_same_request_id(run, stubs):
    stubs.reply("orca.orchestration_reply", '{"error":{"code":"runtime_unavailable"}}', rc=1, n=1)
    stubs.reply("orca.orchestration_reply", '{"ok":true}', n=2)
    r = lib(run, "orca_mutate orchestration reply --id m1", ORCA_RETRY_SLEEP=0)
    first, second = stubs.calls("orca")
    assert r.returncode == 0 and r.stdout.strip() == '{"ok":true}' and first == second and "--retry-request" in first


def test_orca_mutate_does_not_replay_a_definite_failure(run, stubs):
    stubs.reply("orca", '{"error":{"code":"worktree_dirty"}}', rc=1)
    assert lib(run, "orca_mutate worktree rm --worktree x", ORCA_RETRY_SLEEP=0).returncode == 1
    assert len(stubs.calls("orca")) == 1


def test_issue_footer_reads_the_last_matching_line(run):
    r = lib(run, 'b=$(printf "t\\nScope: a.py\\nScope: hello.py tests/\\n"); issue_footer "$b" Scope; issue_footer "$b" Model; echo end')
    assert r.stdout.splitlines() == ["hello.py tests/", "end"]


MAIN_ROW = {"path": "MAIN", "repoId": "this", "isMainWorktree": True}   # the main checkout's own row, as a real scoped list returns it


@pytest.mark.parametrize("rows, want", [
    ([MAIN_ROW, {"path": "/w5", "linkedIssue": 5, "repoId": "this"}, {"path": "/other", "linkedIssue": 5, "repoId": "other"}], "path:/w5"),
    ([MAIN_ROW, {"path": "/w1", "linkedIssue": 1, "repoId": "this"}], None),
    ([MAIN_ROW, {"path": "/w5", "linkedIssue": 5, "repoId": "this"}, {"path": "/w5b", "linkedIssue": 5, "repoId": "this"}], None),
    ([MAIN_ROW, {"path": "/other", "linkedIssue": 5, "repoId": "other"}], None),
    (None, None),
], ids=["one", "none", "two-in-this-repo", "another-repo-only", "orca-failed"])   # #531: a worktree of another Orca repo linked to the same number never resolves
def test_worktree_of_resolves_only_this_repos_worktree_for_an_issue(run, stubs, repo, rows, want):
    if rows is None:
        stubs.reply("orca.worktree_list", "boom", rc=1)
    else:
        rows = [{**r, "path": str(repo.root.resolve()) if r["path"] == "MAIN" else r["path"]} for r in rows]
        stubs.reply("orca.worktree_list", json.dumps({"result": {"worktrees": rows}}))
    r = run(["bash", "-euo", "pipefail", "-c", f". {V2}/scripts/lib.sh; worktree_of 5"], cwd=repo.root)
    assert (r.returncode, r.stdout) == ((0, want) if want else (1, ""))
    assert stubs.calls("orca")[0][:4] == ["worktree", "list", "--repo", f"path:{repo.root.resolve()}"]


def test_gh_binary_can_be_swapped_by_env_for_stubbed_runs(run, stubs, tmp_path, link_script):
    alt = link_script(tmp_path / "alt-gh", '#!/bin/sh\necho "alt $*"\n')
    assert lib(run, "gh issue view 4", AI_TOOLKIT_GH=alt).stdout.strip() == "alt issue view 4"
    lib(run, "gh issue view 4")
    assert stubs.calls("gh") == [["issue", "view", "4"]]


HOLD = 3601   # the one-hour hold reads its own bare +%s, which the fake answers a second after the combined read
NOV25 = "2026 11 25"   # the zone's calendar date in the faked clock below (the time-of-day cases never read it)


@pytest.mark.parametrize("text, hms, seconds, ymd", [
    ("6pm (Europe/Paris)", "12 00 00", 6 * 3600, NOV25), ("6:30pm (Europe/Paris)", "12 00 00", 6 * 3600 + 1800, NOV25),   # later today, in the zone's own clock
    ("6pm (Europe/Paris)", "18 00 30", 300, NOV25),   # the reset passed while the loop read the message: stale, a short hold, never tomorrow's 6pm
    ("6pm (Europe/Paris)", "23 00 00", 19 * 3600, NOV25), ("12am (Europe/Paris)", "23 00 00", 3600, NOV25), ("12pm (Europe/Paris)", "11 00 00", 3600, NOV25),   # long past or midnight: the next one
    ("soon", "12 00 00", HOLD, NOV25), ("6pm (Nowhere/Land)", "12 00 00", HOLD, NOV25), ("13pm (Europe/Paris)", "12 00 00", HOLD, NOV25),   # unreadable: a one-hour hold, never unbounded
    ("Nov 28, 6pm (Europe/Paris)", "12 00 00", 3 * 86400 + 6 * 3600, NOV25),   # #565: a dated reset (a weekly limit): days ahead in the zone's own calendar
    ("Dec 1, 6:30pm (Europe/Paris)", "12 00 00", 2 * 86400 + 6 * 3600 + 1800, "2026 11 29"),   # across a month end
    ("Jan 2, 6pm (Europe/Paris)", "12 00 00", 3 * 86400 + 6 * 3600, "2026 12 30"),   # across the year end: next year's date
    ("Mar 1, 6pm (Europe/Paris)", "12 00 00", 2 * 86400 + 6 * 3600, "2028 02 28"),   # a leap year has a 29 February
    ("Nov 25, 6pm (Europe/Paris)", "12 00 00", 6 * 3600, NOV25),   # the same day, later
    ("Nov 25, 6pm (Europe/Paris)", "18 00 30", 300, NOV25), ("Nov 20, 6pm (Europe/Paris)", "12 00 00", 300, NOV25),   # a dated reset already passed: the short hold, never next year's
    ("Dec 31, 6pm (Europe/Paris)", "12 00 00", 300, "2027 01 01"),   # December's date read in January: passed, not next December
    ("Foo 28, 6pm (Europe/Paris)", "12 00 00", HOLD, NOV25), ("Nov 40, 6pm (Europe/Paris)", "12 00 00", HOLD, NOV25),   # unreadable date: a one-hour hold
])
def test_reset_epoch_is_the_next_reset_in_the_zone_and_never_an_unbounded_hold(run, text, hms, seconds, ymd):
    fake = 'date() { case "$1" in +%s) echo 1000001;; "+%s %H %M %S %Y %m %d") echo "1000000 $HMS $YMD";; *) return 1;; esac; }'   # one call answers the instant, the zone's clock = $HMS on $YMD; a bare +%s is a second later: only the fallbacks read it
    r = lib(run, f'{fake}; echo $(( $(reset_epoch "$T") - 1000000 ))', T=text, HMS=hms, YMD=ymd)
    assert r.returncode == 0 and r.stdout.strip() == str(seconds), r.stderr


@pytest.mark.parametrize("body, rc, want", [
    ("Error: Claude hit its usage limit. You've hit your weekly limit · resets Nov 28, 6pm (Europe/Paris)", 0, "Nov 28, 6pm (Europe/Paris)"),
    ("Error: Claude hit its usage limit. You've hit your weekly limit · resets Dec 3, 6:30pm (Europe/Paris)", 0, "Dec 3, 6:30pm (Europe/Paris)"),
    ("Error: Claude hit its usage limit. You've hit your limit · resets 6pm (Europe/Paris)", 0, "6pm (Europe/Paris)"),
    ("Error: Claude hit its usage limit. Try again later.", 0, ""),
    ("A worker says: Claude hit its usage limit, resets Nov 28, 6pm (Europe/Paris)", 1, ""),
], ids=["dated", "dated-minutes", "time-of-day", "no-reset", "not-orcas-text"])   # #565: a weekly limit's reset starts with a date
def test_usage_limit_reset_prints_the_reset_text_with_its_date_when_it_has_one(run, body, rc, want):
    r = lib(run, 'rc=0; usage_limit_reset "$B" || rc=$?; echo "|$rc"', B=body)
    assert r.stdout == f"{want}|{rc}\n", r.stderr


@pytest.mark.parametrize("days, kept", [(3, True), (7, True), (30, False)], ids=["3-days", "7-days", "30-days"])   # a weekly reset is kept, a record far beyond the cap is dropped
def test_free_account_keeps_a_record_up_to_a_week_ahead_and_drops_one_beyond(run, tmp_path, days, kept):
    (tmp_path / "state/run_t/exhausted").mkdir(parents=True)
    f = tmp_path / "state/run_t/exhausted/max"
    f.write_text(f"{int(time.time()) + days * 86400}\n")
    r = lib(run, "free_account run_t || true", CLAUDE_ACCOUNTS="max client", AITK_STATE_DIR=tmp_path / "state")
    assert (r.stdout, f.exists()) == (("client", True) if kept else ("max", False)), r.stderr
