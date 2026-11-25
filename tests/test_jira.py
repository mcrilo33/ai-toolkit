import json

import pytest
from conftest import STUB, STUB_TEE, V2, git

POST_LAND = str(V2 / "shared/skills/jira/scripts/post-land.sh")
READ_TICKET = str(V2 / "shared/skills/jira/scripts/read-ticket.sh")
POST_DISPATCH = str(V2 / "shared/skills/jira/scripts/post-dispatch.sh")
TOKEN = "tok3n-SECRET"
CONFIG = "JIRA_SITE=acme.atlassian.net\nJIRA_EMAIL=dev@acme.test\n"
TRANSITIONS = json.dumps({"transitions": [{"id": "21", "name": "In Review"}, {"id": "31", "name": "Done"}]})
def para(*texts):
    return {"type": "paragraph", "content": [{"type": "text", "text": t} for t in texts]}


TICKET = json.dumps({"fields": {"summary": "Add the CSV export", "issuetype": {"name": "Story"}, "status": {"name": "To Do"}, "description": {
    "type": "doc", "version": 1, "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Acceptance"}]},
        para("Users can ", "export", " a CSV."),
        {"type": "bulletList", "content": [{"type": "listItem", "content": [para("header row")]}, {"type": "listItem", "content": [para("UTF-8")]}]}]}}})
CURL = "curl.__config__"   # the stub keys its replies on the first two argv words: --config -
SECURITY = "security.find_generic_password__a"   # find-generic-password -a


@pytest.fixture
def J(stubs, tmp_path, link_script, run, monkeypatch):
    for k in ("JIRA_SITE", "JIRA_EMAIL", "JIRA_LAND_TRANSITION", "JIRA_START_TRANSITION"):
        monkeypatch.delenv(k, raising=False)   # the caller env would win over the local env file under test
    link_script(tmp_path / "stubs/bin/curl", STUB_TEE + "# jira\n")   # a text of its own: no other test shares, and so can rewrite, this file
    link_script(tmp_path / "stubs/bin/security", STUB)
    stubs.reply(SECURITY, TOKEN + "\n")

    def go(*args, cfg=CONFIG, cwd=None, script=POST_LAND, **env):
        local = tmp_path / "local.env"   # land.sh does not export what load_env reads, so the script must read it itself
        local.write_text(cfg)
        return run(["bash", script, *(args or ("12", "PM-17", "abc1234"))], cwd=cwd or tmp_path,
                   AI_TOOLKIT_LOCAL_ENV=local, **{"BASE_BRANCH": "mc/ai-develop", **env})

    def curl():   # (argv words, stdin) of every curl call
        return stubs.calls("curl")

    return type("J", (), {"go": staticmethod(go), "curl": staticmethod(curl), "stubs": stubs, "tmp": tmp_path})


def body_text(argv):   # the text of every node of the ADF comment
    doc = json.loads(argv[argv.index("--data") + 1])
    return "".join(n["text"] for p in doc["body"]["content"] for n in p["content"])


def test_comment_only_posts_one_comment_with_sha_issue_and_repo(J):
    proj = J.tmp / "proj"
    proj.mkdir()
    git(proj, "init", "-q")
    git(proj, "remote", "add", "origin", "git@github.com:acme/app.git")
    r = J.go(cwd=proj)
    assert r.returncode == 0, r.stderr
    (argv,) = J.curl()
    assert argv[-1] == "https://acme.atlassian.net/rest/api/3/issue/PM-17/comment"
    assert {"--fail", "--max-time", "20", "--config"} <= set(argv)
    assert body_text(argv) == "Landed on mc/ai-develop in abc1234 (GitHub acme/app#12); it reaches the next branch with its PR."


@pytest.mark.parametrize("script,args,cfg,first_reply", [
    (POST_LAND, ("12", "PM-17", "abc1234"), "", ""),
    (READ_TICKET, ("PM-17",), "", TICKET),
    (POST_DISPATCH, ("12", "PM-17"), "JIRA_START_TRANSITION=done\n", TRANSITIONS),
], ids=["post-land", "read-ticket", "post-dispatch"])
def test_the_token_reaches_curl_on_stdin_never_in_argv(J, script, args, cfg, first_reply):
    J.stubs.reply(f"{CURL}.1", first_reply)
    assert J.go(*args, script=script, cfg=CONFIG + cfg).returncode == 0
    assert TOKEN not in (J.tmp / "stubs/calls.log").read_text()
    assert f"dev@acme.test:{TOKEN}" in (J.tmp / "stubs/curl.stdin").read_text()


def test_the_comment_names_the_issue_without_a_remote(J):
    assert J.go().returncode == 0
    assert body_text(J.curl()[0]).endswith("(GitHub #12); it reaches the next branch with its PR.")


def test_a_transition_name_is_resolved_to_its_id_and_posted(J):
    J.stubs.reply(f"{CURL}.2", TRANSITIONS)
    r = J.go(cfg=CONFIG + "JIRA_LAND_TRANSITION=done\n")
    assert r.returncode == 0, r.stderr
    comment, get, post = J.curl()
    assert comment[-1].endswith("/issue/PM-17/comment")
    assert get[-1].endswith("/issue/PM-17/transitions") and "POST" not in get
    assert post[-1].endswith("/issue/PM-17/transitions")
    assert json.loads(post[post.index("--data") + 1]) == {"transition": {"id": "31"}}


def test_an_unknown_transition_name_fails_and_lists_the_available_ones(J):
    J.stubs.reply(f"{CURL}.2", TRANSITIONS)
    r = J.go(cfg=CONFIG + "JIRA_LAND_TRANSITION=Shipped\n")
    assert r.returncode != 0
    assert "Shipped" in r.stderr and "In Review" in r.stderr and "Done" in r.stderr
    assert len(J.curl()) == 2   # the comment and the lookup, no transition posted


def test_a_missing_token_fails_before_any_request(J):
    J.stubs.reply(SECURITY, "", rc=44)
    r = J.go()
    assert r.returncode != 0 and "JIRA_API_TOKEN" in r.stderr
    assert J.curl() == []


def test_an_http_error_fails_with_a_reason(J):
    J.stubs.reply(CURL, "", rc=22)
    r = J.go()
    assert r.returncode != 0 and "PM-17" in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1


@pytest.mark.parametrize("issue,key,sha", [("12", k, "abc1234") for k in ["pm-17", "PM-17;x", "PM-1/../2", "PM-", "$(id)-1", "PM-17 "]]
                         + [("1x", "PM-17", "abc1234"), ("12", "PM-17", "XYZ"), ("12", "PM-17", "abc;1234")])
def test_a_bad_argument_is_refused_before_any_call(J, issue, key, sha):
    r = J.go(issue, key, sha)
    assert r.returncode != 0
    assert J.curl() == [] and J.stubs.calls("security") == []


@pytest.mark.parametrize("cfg,base", [("JIRA_EMAIL=dev@acme.test\n", "b"), ("JIRA_SITE=acme.atlassian.net\n", "b"), ("JIRA_SITE=a.b/x\nJIRA_EMAIL=dev@acme.test\n", "b"),
                                      (CONFIG, "")])
def test_missing_or_malformed_config_fails_before_any_call(J, cfg, base):
    r = J.go(cfg=cfg, BASE_BRANCH=base)
    assert r.returncode != 0 and ("JIRA_" in r.stderr or "BASE_BRANCH" in r.stderr)
    assert J.curl() == [] and J.stubs.calls("security") == []


def test_read_ticket_prints_summary_type_status_and_the_description_flattened_from_adf(J):
    J.stubs.reply(f"{CURL}.1", TICKET)
    r = J.go("PM-17", script=READ_TICKET)
    assert r.returncode == 0, r.stderr
    (argv,) = J.curl()
    assert argv[-1] == "https://acme.atlassian.net/rest/api/3/issue/PM-17?fields=summary,description,issuetype,status"
    for line in ("Add the CSV export", "Story", "To Do", "Acceptance", "Users can export a CSV.", "- header row", "- UTF-8"):
        assert line in r.stdout


@pytest.mark.parametrize("script,args", [(READ_TICKET, ("pm-17",)), (READ_TICKET, ("PM-1/../2",)), (POST_DISPATCH, ("12", "pm-17")), (POST_DISPATCH, ("1x", "PM-17"))],
                         ids=["read-lower", "read-path", "dispatch-lower", "dispatch-issue"])
def test_a_bad_key_or_issue_is_refused_by_the_start_scripts_before_any_call(J, script, args):
    r = J.go(*args, script=script, cfg=CONFIG + "JIRA_START_TRANSITION=done\n")
    assert r.returncode != 0
    assert J.curl() == [] and J.stubs.calls("security") == []


def test_post_dispatch_without_a_start_transition_makes_no_request(J):
    r = J.go("12", "PM-17", script=POST_DISPATCH)
    assert r.returncode == 0, r.stderr
    assert J.curl() == [] and J.stubs.calls("security") == []


def test_post_dispatch_resolves_the_start_transition_by_name_and_posts_its_id(J):
    J.stubs.reply(f"{CURL}.1", TRANSITIONS)
    r = J.go("12", "PM-17", script=POST_DISPATCH, cfg=CONFIG + "JIRA_START_TRANSITION=in review\n")
    assert r.returncode == 0, r.stderr
    get, post = J.curl()
    assert get[-1].endswith("/issue/PM-17/transitions") and "POST" not in get
    assert post[-1].endswith("/issue/PM-17/transitions") and json.loads(post[post.index("--data") + 1]) == {"transition": {"id": "21"}}


def test_post_dispatch_with_an_unknown_start_transition_fails_and_lists_the_available_ones(J):
    J.stubs.reply(f"{CURL}.1", TRANSITIONS)
    r = J.go("12", "PM-17", script=POST_DISPATCH, cfg=CONFIG + "JIRA_START_TRANSITION=Started\n")
    assert r.returncode != 0
    assert "Started" in r.stderr and "In Review" in r.stderr and "Done" in r.stderr
    assert len(J.curl()) == 1   # the lookup, no transition posted
