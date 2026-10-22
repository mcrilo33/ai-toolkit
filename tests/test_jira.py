import json

import pytest
from conftest import STUB, STUB_TEE, V2, git

POST_LAND = str(V2 / "shared/skills/jira/scripts/post-land.sh")
TOKEN = "tok3n-SECRET"
CONFIG = "JIRA_SITE=acme.atlassian.net\nJIRA_EMAIL=dev@acme.test\n"
TRANSITIONS = json.dumps({"transitions": [{"id": "21", "name": "In Review"}, {"id": "31", "name": "Done"}]})
CURL = "curl.__config__"   # the stub keys its replies on the first two argv words: --config -
SECURITY = "security.find_generic_password__a"   # find-generic-password -a


@pytest.fixture
def J(stubs, tmp_path, link_script, run):
    link_script(tmp_path / "stubs/bin/curl", STUB_TEE + "# jira\n")   # a text of its own: no other test shares, and so can rewrite, this file
    link_script(tmp_path / "stubs/bin/security", STUB)
    stubs.reply(SECURITY, TOKEN + "\n")

    def go(*args, cfg=CONFIG, cwd=None, **env):
        local = tmp_path / "local.env"   # land.sh does not export what load_env reads, so the script must read it itself
        local.write_text(cfg)
        return run(["bash", POST_LAND, *(args or ("12", "PM-17", "abc1234"))], cwd=cwd or tmp_path,
                   AI_TOOLKIT_LOCAL_ENV=local, BASE_BRANCH="mc/ai-develop", **env)

    def curl():   # (argv words, stdin) of every curl call
        return stubs.calls("curl")

    return type("J", (), dict(go=staticmethod(go), curl=staticmethod(curl), stubs=stubs, tmp=tmp_path))


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


def test_the_token_reaches_curl_on_stdin_never_in_argv(J):
    assert J.go().returncode == 0
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


@pytest.mark.parametrize("key", ["pm-17", "PM-17;x", "PM-1/../2", "PM-", "$(id)-1", "PM-17 "])
def test_a_bad_key_is_refused_before_any_call(J, key):
    r = J.go("12", key, "abc1234")
    assert r.returncode != 0
    assert J.curl() == [] and J.stubs.calls("security") == []


@pytest.mark.parametrize("cfg", ["JIRA_EMAIL=dev@acme.test\n", "JIRA_SITE=acme.atlassian.net\n", "JIRA_SITE=a.b/x\nJIRA_EMAIL=dev@acme.test\n"])
def test_missing_or_malformed_config_fails_before_any_call(J, cfg):
    r = J.go(cfg=cfg)
    assert r.returncode != 0 and "JIRA_" in r.stderr
    assert J.curl() == [] and J.stubs.calls("security") == []
