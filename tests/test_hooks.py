import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from conftest import V2, git

HOOKS = V2 / "hooks"
CLAUDE = HOOKS / "claude"
AWS, GHP = "AKIA" + "A" * 16, "ghp_" + "a" * 36
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def build(base):
    """base/root = main checkout on main (bare origin), 1-x = spoke worktree (marker), 2-y = plain linked worktree."""
    def sh(*a, cwd=base):
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})
    root = base / "root"
    sh("init", "-q", "--bare", "-b", "main", "origin.git")
    sh("clone", "-q", "origin.git", "root")
    (root / "README").write_text("x")
    for a in (["add", "."], ["commit", "-qm", "init"], ["push", "-q", "origin", "main"]):
        sh(*a, cwd=root)
    for b in ("1-x", "2-y"):
        sh("worktree", "add", "-q", "-b", b, str(base / b), cwd=root)
    (base / "1-x/.ai-toolkit").mkdir()
    (base / "1-x/.ai-toolkit/spoke-run-id").write_text("rid\n")
    for d in ("home", "scratch"):
        (base / d).mkdir()
    return {"root": root, "wt": base / "2-y", "spoke": base / "1-x", "home": base / "home",
            "env": {"HOME": str(base / "home"), "TMPDIR": str(base / "scratch"), **GIT_ENV}}


@pytest.fixture(autouse=True)
def no_base_override(monkeypatch):
    monkeypatch.delenv("BASE_BRANCH", raising=False)


@pytest.fixture(scope="module")
def shared(tmp_path_factory):  # one repo per worker: the table tests only read it
    return build(tmp_path_factory.mktemp("hooks"))


@pytest.fixture
def places(tmp_path):  # a private repo for the tests that move refs or commit
    return build(tmp_path)


def call(script, cwd, tool="Bash", raw=None, env=None, **tool_input):
    body = raw if raw is not None else json.dumps({"tool_name": tool, "cwd": str(cwd), "tool_input": tool_input})
    e = {**os.environ, "CLAUDE_PROJECT_DIR": str(cwd), **(env or {})}
    return subprocess.run(["/bin/bash", str(CLAUDE / script)], input=body, cwd=cwd, env=e, capture_output=True, text=True)


def check(script, places, where, want, tool="Bash", env=None, **ti):
    for n, p in places.items():
        ti = {k: v.replace("{%s}" % n, str(p)) for k, v in ti.items()}
    r = call(script, places[where], tool, env={**places["env"], **(env or {})}, **ti)
    assert r.returncode == want, f"{where}: {ti} -> rc={r.returncode} {r.stderr}"
    if want == 2:
        assert script.removesuffix(".sh") in r.stderr and "blocked" in r.stderr


# --- push-guard ---------------------------------------------------------------------------------
PUSH_DENY = [
    ("wt", c) for c in [
        "git push origin main", "git push -u origin HEAD:main", "git push origin HEAD:refs/heads/main",
        "git push origin feat:main", "git push origin :main", "git push origin --delete main",
        "git push origin +2-y", "git push --force", "git push --force-with-lease origin 2-y", "git push -f",
        "git push -fu origin 2-y", "git push --no-verify", "git push --all", "git push --mirror",
        "git commit --no-verify -m x", "git commit -nm x", "git commit -an -m x", "git merge --no-verify x",
        "git -c core.hooksPath=/dev/null commit -m x", "git -c core.HOOKSpath=x commit -m x",
        "git config core.hooksPath /dev/null", "git config --global core.hooksPath x", "git -C . config core.hookspath x",
        "git -C . push -f", "git -C . push origin main", 'bash -c "git push --force"', "sh -c 'git push -f'",
        "bash -c'git push -f'", 'git pu""sh -f', 'git push origin "ma""in"', "cd sub && git push origin main",
        "echo ok; git push -f", "FOO=1 git push -f", "env git push -f", "/usr/bin/git push -f", "echo $(git push -f)",
        "git push origin main>/dev/null", "time git push origin main", "git --no-pager push -f",
        "git checkout main", "git switch main", "git checkout -B main", "git -C . checkout main",
        "git -C", "git -C {root} push", "git --git-dir /nonexistent push -f", "git --work-tree /x push origin main",
        "git --namespace foo push -f", "git push --repo=origin main", "git push origin 'refs/heads/*:refs/heads/*'", "GIT push -f", "/usr/bin/GIT push origin main",
    ]
] + [("root", c) for c in ["git push", "git push origin", "git push origin HEAD", "git push -u origin main"]]
PUSH_ALLOW = [
    ("wt", c) for c in [
        "git push -u origin 2-y", "git push origin HEAD", "git push", "git push origin 2-y:2-y", "git push origin mainline",
        "git push origin feature-main", "git status", "git log --oneline -3", 'git commit -m "feat: running tests #1"',
        "git commit -am wip", "git commit --amend --no-edit", "git commit -mrunning", "git checkout -b new",
        "git checkout 2-y", "git checkout main -- README", "git fetch origin main", "git pull origin main",
        "git -C . log", "git config user.name x", "ls -la", "gh pr list", "grep -rn force src", "git",
    ]
] + [("root", c) for c in ["git checkout main", "git checkout -b x", "git push -u origin topic", "git status"]]


@pytest.mark.parametrize("where,cmd", PUSH_DENY)
def test_push_guard_denies(shared, where, cmd):
    check("push-guard.sh", shared, where, 2, command=cmd)


@pytest.mark.parametrize("where,cmd", PUSH_ALLOW)
def test_push_guard_allows(shared, where, cmd):
    check("push-guard.sh", shared, where, 0, command=cmd)


def test_push_guard_base_branch_comes_from_env_then_origin_head(places):
    check("push-guard.sh", places, "wt", 2, env={"BASE_BRANCH": "trunk"}, command="git push origin trunk")
    check("push-guard.sh", places, "wt", 0, env={"BASE_BRANCH": "trunk"}, command="git push origin main")
    git(places["root"], "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
    check("push-guard.sh", places, "wt", 2, command="git push origin develop")
    check("push-guard.sh", places, "wt", 0, command="git push origin main")


# --- danger-guard -------------------------------------------------------------------------------
RM_DENY = [
    "rm -rf /", "rm -rf /etc/x", "rm -rf ~", "rm -rf ~/x", "rm -rf $HOME/x", 'rm -rf "${HOME}/x"', "rm -fr /usr/local",
    "rm -r --force /usr", "rm --recursive /usr", "rm /usr/x -rf", "rm -rf ../../../../../../../..", "rm -rf .",
    "rm -rf {wt}", "rm -rf {home}/x", "rm -rf build /etc/x", "rm -rf $FOO/x", 'bash -c "rm -rf /usr"',
    "echo hi && rm -rf /usr", "rm -rf /tmp", "rm -rf /private/tmp", 'rm -rf "$HOME"/x', "bash -c'rm -rf /usr'", "'r'm -rf /usr", 'r""m -rf /usr',
    "git stash -a", "git stash --all", "git stash push -a", "git stash push -au", "git -C . stash -a", "rm -rf /etc/absent/", "git clean -fdx", "git clean -xdf", "git clean -fdX",
    "git clean -f -x", "git -C . clean -fdx", "bash -c 'git clean -fdx'", "rm -rf $UNSET_TMP/x", "rm -rf ~root/x", "RM -rf /usr",
]
RM_ALLOW = [
    "rm -rf build", 'rm -rf "$TMPDIR"/x', 'rm -rf "${TMPDIR}"/x', 'rm -rf "$TMPDIR"/claude-*', "rm -rf build/ dist/ .venv/ node_modules/", "rm -rf ./build/", "rm -rf /tmp/absent-x/",
    "rm -rf $TMPDIR/absent-x/", "rm -rf ./dist node_modules", "rm file.txt", "rm -rf /tmp/xyz", "rm -rf $TMPDIR/xyz",
    "rm -rf {spoke}/sub", "rm -rf src/*.pyc", "rm -rf build 2>/dev/null", "ls /etc",
]
WRITE_DENY = [
    "echo x > .github/workflows/ci.yml", "echo x >> {spoke}/.github/workflows/ci.yml", "tee orca.yaml",
    "sed -i s/a/b/ orca.yaml", "sed -i '' s/a/b/ orca.yaml", "cp x ~/.claude/settings.json",
    'cat y > "$HOME/.claude/settings.json"', "mv a {home}/.claude/settings.json", "echo x > ./orca.yaml", "echo x > $PWD/orca.yaml", "echo x > ${PWD}/orca.yaml", 'echo x > "$PWD"/orca.yaml',
    "echo x > $CLAUDE_PROJECT_DIR/orca.yaml", 'echo x > "$(pwd)/orca.yaml"', "cp s.json $PWD/.claude/settings.json",
    "tee ${CLAUDE_PROJECT_DIR}/.github/workflows/ci.yml", "echo x >orca.yaml", "rm -rf .claude", "rm -rf .claude/", "mv .claude {home}/x",
    "rm -rf .ai-toolkit", "rm -r .ai-toolkit/", "rm .ai-toolkit/spoke-run-id", "rm -rf .claude/hooks", "rm -rf .claude/hooks/",
    "mv .claude/hooks h", "rm -rf .claude; ls",
    "python3 -c \"open('orca.yaml','w')\"", "rm .github/workflows/ci.yml", 'bash -c "echo > orca.yaml"',
    "echo x > .claude/settings.json", "tee .claude/settings.local.json", "rm .claude/hooks/push-guard.sh",
    "chmod -x .claude/hooks/push-guard.sh", "echo > {spoke}/.claude/hooks/x.sh", "cd x && echo y > orca.yaml",
    "cd .github/workflows && echo x > ci.yml", "python3 - <<EOF\nopen('orca.yaml','w')\nEOF", "cat y > .GITHUB/Workflows/ci.yml",
    "echo x > ORCA.YAML",
]
WRITE_ALLOW = [
    "touch sub/orca.yaml", "echo x > v2/orca.yaml", "cp a docs/.github/workflows/x.yml", "rm -rf .claude/cache", "ls .claude",
    "cat .ai-toolkit/task.md", "git clean -fd", "git clean -n", "git stash", "git stash push -u -m wip", "git stash list", "git stash pop", "git clean -fd -e keep", "grep -n x 2>/dev/null orca.yaml",
    "ls 2>&1 .github/workflows", "cat orca.yaml >/dev/null 2>&1", "echo x &>/dev/null; cat orca.yaml",
    "cat orca.yaml", "git add orca.yaml .github/workflows/ci.yml", "ls .github/workflows", "cat .claude/settings.json",
    "grep -n hooks .claude/settings.json", "echo hi > out.txt", "cat orca.yaml > /dev/null", "cat orca.yaml > out.txt",
    "cat .github/workflows/ci.yml 2>&1 | head", "git diff -- orca.yaml", "sed -n 1,5p orca.yaml",
    "git commit -m 'update orca.yaml'", "echo x > .claude/rules/a.md",
]


@pytest.mark.parametrize("cmd", RM_DENY + WRITE_DENY)
def test_danger_guard_denies_bash(shared, cmd):
    check("danger-guard.sh", shared, "spoke", 2, command=cmd)


@pytest.mark.parametrize("cmd", RM_ALLOW + WRITE_ALLOW)
def test_danger_guard_allows_bash(shared, cmd):
    check("danger-guard.sh", shared, "spoke", 0, command=cmd)


def test_danger_guard_a_home_inside_a_temp_root_is_still_protected(shared):
    scratch = shared["env"]["TMPDIR"]
    check("danger-guard.sh", shared, "spoke", 2, env={"HOME": f"{scratch}/h"}, command="rm -rf ~/x")
    check("danger-guard.sh", shared, "spoke", 0, env={"HOME": f"{scratch}/h"}, command="rm -rf /tmp/x")


@pytest.mark.parametrize("where,cmd,want", [
    ("root", "git reset --hard", 2), ("root", "git reset --hard HEAD~1", 2), ("root", 'bash -c "git reset --hard"', 2),
    ("wt", "git -C {root} reset --hard", 2), ("root", "git -C . reset --hard", 2), ("wt", "git reset --hard", 0),
    ("wt", "git --work-tree={root} reset --hard", 2), ("root", "git reset --soft HEAD~1", 0), ("root", "git reset HEAD README", 0), ("wt", "git -C {wt} reset --hard", 0),
])
def test_danger_guard_reset_hard_only_in_the_main_checkout(shared, where, cmd, want):
    check("danger-guard.sh", shared, where, want, command=cmd)


@pytest.mark.parametrize("tool,key,path", [
    ("Write", "file_path", ".github/workflows/ci.yml"), ("Edit", "file_path", "{spoke}/.github/workflows/new.yml"),
    ("MultiEdit", "file_path", "sub/../.github/workflows/ci.yml"), ("Write", "file_path", "orca.yaml"),
    ("Edit", "file_path", "{spoke}/orca.yaml"), ("Write", "file_path", "{home}/.claude/settings.json"),
    ("Write", "file_path", "~/.claude/settings.json"), ("NotebookEdit", "notebook_path", ".github/workflows/n.ipynb"),
    ("Write", "file_path", ".claude/settings.json"), ("Edit", "file_path", ".claude/settings.local.json"),
    ("Write", "file_path", ".claude/hooks/push-guard.sh"), ("Write", "file_path", ".claude/hooks"),
    ("Write", "file_path", ".ai-toolkit/spoke-run-id"), ("Write", "file_path", "./orca.yaml"),
])
def test_danger_guard_denies_protected_file_writes(shared, tool, key, path):
    check("danger-guard.sh", shared, "spoke", 2, tool=tool, **{key: path})


@pytest.mark.parametrize("path", ["src/app.py", "{spoke}/v2/orca.yaml", "{spoke}/docs/.github/workflows/x.yml", "/tmp/fixture/orca.yaml", ".ai-toolkit/task.md", ".claude/rules/x.md", "docs/orca.yaml.md", ".github/CODEOWNERS", "README.md"])
def test_danger_guard_allows_ordinary_file_writes(shared, path):
    check("danger-guard.sh", shared, "spoke", 0, tool="Write", file_path=path)


def test_spoke_only_protections_do_not_bind_the_human_session(shared):
    def dg(where, want, tool="Write", **ti):
        check("danger-guard.sh", shared, where, want, tool=tool, **ti)
    for p in (".claude/settings.json", ".claude/hooks/x.sh", ".claude/settings.local.json"):
        dg("root", 0, file_path=p)
    dg("root", 0, "Bash", command="echo > .claude/settings.json")
    dg("root", 0, "Bash", command="rm -rf .claude")
    dg("root", 2, file_path=".github/workflows/ci.yml")
    dg("root", 2, file_path="{home}/.claude/settings.json")
    dg("root", 0, "AskUserQuestion", questions="[]")
    dg("wt", 0, "AskUserQuestion", questions="[]")
    dg("spoke", 2, "AskUserQuestion", questions="[]")


def test_danger_guard_uses_the_project_dir_for_the_marker_and_the_worktree_root(shared):
    sp, plain = str(shared["spoke"]), str(shared["wt"])
    check("danger-guard.sh", shared, "wt", 2, "AskUserQuestion", env={"CLAUDE_PROJECT_DIR": sp}, questions="[]")
    check("danger-guard.sh", shared, "spoke", 0, "AskUserQuestion", env={"CLAUDE_PROJECT_DIR": plain}, questions="[]")
    for cwd, cmd, want in (("/", "rm -rf usr", 2), ("/", f"rm -rf {sp}/build", 0), (sp, f"echo x > {sp}/orca.yaml", 2)):
        r = call("danger-guard.sh", cwd, command=cmd, env={"CLAUDE_PROJECT_DIR": sp, **shared["env"]})
        assert r.returncode == want, (cwd, cmd, r.stderr)


# --- secrets-scan -------------------------------------------------------------------------------
SECRETS = [AWS, GHP, "sk-ant-api03-" + "a" * 10 + "_-" + "b" * 10, "sk-" + "a" * 24, "sk-lf-" + "a" * 24, "xoxb-1234567890-" + "a" * 24, "k=sk_live_" + "a" * 24,
           "github_pat_" + "a" * 30, "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12,
           "LANGFUSE_BASIC_AUTH='Basic " + "QUJD" * 6 + "'", "x\n" * 50 + f"token {AWS} end"]
CLEAN = ["KEY = os.environ['API_KEY']\n", "sk-short AKIA123 ghp_tooShort key-abc", ""]


@pytest.mark.parametrize("secret", SECRETS)
def test_secrets_scan_denies_every_pattern_without_echoing_it(shared, secret):
    r = call("secrets-scan.sh", shared["wt"], "Write", file_path="a.py", content=secret)
    assert r.returncode == 2 and "secrets-scan" in r.stderr and secret[-20:] not in r.stderr


@pytest.mark.parametrize("tool,ti", [
    ("Edit", {"new_string": GHP}), ("NotebookEdit", {"new_source": AWS}),
    ("MultiEdit", {"edits": [{"new_string": "b"}, {"new_string": GHP}]}),
])
def test_secrets_scan_reads_every_edit_tool_shape(shared, tool, ti):
    assert call("secrets-scan.sh", shared["wt"], tool, file_path="a", **ti).returncode == 2


@pytest.mark.parametrize("tool,ti", [("Write", {"content": c}) for c in CLEAN] + [
    ("Edit", {"old_string": AWS, "new_string": "x"}), ("Read", {"file_path": "a"}), ("MultiEdit", {"edits": [{"new_string": "b"}]}),
])
def test_secrets_scan_allows_clean_writes(shared, tool, ti):
    assert call("secrets-scan.sh", shared["wt"], tool, **ti).returncode == 0


# --- fail closed --------------------------------------------------------------------------------
@pytest.mark.parametrize("script", ["push-guard.sh", "danger-guard.sh", "secrets-scan.sh"])
def test_unparseable_payload_or_missing_jq_is_a_deny(shared, tmp_path, script):
    for raw in ("not json", "[1, 2]", '{"tool_input": "x"}'):
        r = call(script, shared["wt"], raw=raw)
        assert r.returncode == 2 and "fail-closed" in r.stderr, raw
    (tmp_path / "nojq").mkdir()
    for tool in ("cat", "git", "sed", "tr", "dirname", "grep"):
        (tmp_path / "nojq" / tool).symlink_to(shutil.which(tool))
    r = call(script, shared["wt"], raw='{"tool_name":"Bash","tool_input":{"command":"ls"}}', env={"PATH": str(tmp_path / "nojq")})
    assert r.returncode == 2 and "fail-closed" in r.stderr


def test_settings_register_the_hooks_at_the_synced_path(shared, tmp_path):
    cfg = json.loads((V2 / "settings/claude/settings.json").read_text())
    assert "permissions" not in cfg and set(cfg["hooks"]) == {"PreToolUse"}  # no auto-allow, no TDD/plan hooks (D2/D8)
    shutil.copytree(CLAUDE, tmp_path / "proj/.claude/hooks")  # what sync (WP4) puts in a target
    seen = {}
    for e in cfg["hooks"]["PreToolUse"]:
        cmd = e["hooks"][0]["command"]
        script = cmd.split("/")[-1].rstrip('"')
        seen[script] = e["matcher"].split("|")
        tool, ti = ("Bash", {"command": "git push --force"}) if script == "push-guard.sh" else ("Write", {"file_path": "orca.yaml", "content": AWS})
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path / "proj")},
                           input=json.dumps({"tool_name": tool, "cwd": str(tmp_path / "proj"), "tool_input": ti}))
        assert r.returncode == 2 and script.removesuffix(".sh") in r.stderr, (cmd, r.stderr)
    assert seen["push-guard.sh"] == ["Bash"] and "AskUserQuestion" in seen["danger-guard.sh"] and "Bash" not in seen["secrets-scan.sh"]


# --- native git hooks ---------------------------------------------------------------------------
def commit(cwd, msg, **env):
    return subprocess.run(["git", "-C", str(cwd), "commit", "--allow-empty", "-q", "-m", msg], capture_output=True, text=True,
                          env={**os.environ, "GIT_EDITOR": ":", **env})


@pytest.fixture
def hooked(places):
    git(places["root"], "config", "core.hooksPath", str(HOOKS / "git"))
    return places


@pytest.mark.parametrize("msg", [
    "feat: add x #12", "fix(core): y (#3)", "chore!: z Refs #4", "refactor(a/b): c\n\nbody\n\nCloses #9", "Merge branch 'x'",
    "fixup! feat: a", 'Revert "feat: a"', "docs(Readme): x #1", "feat: x\n\n#7 is the anchor",
])
def test_commit_msg_accepts(hooked, msg):
    r = commit(hooked["wt"], msg)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("msg,why", [
    ("add x #12", "<type>"), ("feat: add x", "#"), ("feat:no space #1", "<type>"), ("Feat: x #1", "<type>"),
    ("wip #1", "<type>"), ("feat: x\n\n# only a comment line mentions #5", "#"),
])
def test_commit_msg_rejects(hooked, msg, why):
    r = commit(hooked["wt"], msg)
    assert r.returncode != 0 and "commit-msg" in r.stderr and why in r.stderr


def test_pre_commit_blocks_the_base_branch_only_in_the_main_checkout(hooked):
    root, wt, msg = hooked["root"], hooked["wt"], "chore: x #1"
    r = commit(root, msg)
    assert r.returncode != 0 and "main checkout" in r.stderr
    assert commit(wt, msg).returncode == 0
    assert commit(root, msg, AI_TOOLKIT_ALLOW_BASE_COMMIT="1").returncode == 0
    git(root, "checkout", "-q", "-b", "topic")
    assert commit(root, msg).returncode == 0
    assert commit(root, msg, BASE_BRANCH="topic").returncode != 0


def test_pre_commit_allows_the_first_commit_of_a_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path / "fresh")], check=True)
    git(tmp_path / "fresh", "config", "core.hooksPath", str(HOOKS / "git"))
    assert commit(tmp_path / "fresh", "chore: init #1").returncode == 0
    assert commit(tmp_path / "fresh", "chore: second #1").returncode != 0


def test_hook_line_budgets():
    n = lambda *g: sum(len(f.read_text().splitlines()) for q in g for f in HOOKS.glob(q))  # noqa: E731
    assert n("claude/*.sh") <= 150 and n("git/*") <= 40
    assert len((V2 / "settings/claude/settings.json").read_text().splitlines()) <= 40
