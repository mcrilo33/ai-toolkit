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


@pytest.mark.parametrize("where,cmd,asks", [
    ("root", "git reset --hard", True), ("root", "git reset --hard HEAD~1", True), ("root", 'bash -c "git reset --hard"', True),
    ("wt", "git -C {root} reset --hard", True), ("root", "git -C . reset --hard", True), ("wt", "git reset --hard", False),
    ("wt", "git --work-tree={root} reset --hard", True), ("root", "git reset --soft HEAD~1", False), ("root", "git reset HEAD README", False),
    ("wt", "git -C {wt} reset --hard", False),
])
def test_danger_guard_reset_hard_asks_only_in_the_main_checkout(shared, where, cmd, asks):
    r = call("danger-guard.sh", shared[where], command=cmd.replace("{root}", str(shared["root"])).replace("{wt}", str(shared["wt"])), env=shared["env"])
    assert r.returncode == 0 and r.stderr == ""
    if asks:
        assert "reset --hard" in json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    else:
        assert r.stdout == ""


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


def test_spoke_only_protections_do_not_bind_the_human_session(shared, tmp_path):
    def dg(where, want, tool="Write", **ti):
        check("danger-guard.sh", shared, where, want, tool=tool, **ti)
    for p in (".claude/settings.json", ".claude/hooks/x.sh", ".claude/settings.local.json"):
        dg("root", 0, file_path=p)
    dg("root", 0, "Bash", command="echo > .claude/settings.json")
    dg("root", 0, "Bash", command="rm -rf .claude")
    dg("root", 0, "AskUserQuestion", questions="[]")
    dg("wt", 0, "AskUserQuestion", questions="[]")
    dg("spoke", 2, "AskUserQuestion", questions="[]")
    install_relay(tmp_path / "w", "installed")  # not a permission, so never an ask: denied in a worker even when someone could answer
    r = call("danger-guard.sh", tmp_path / "w", "AskUserQuestion", env=shared["env"], questions="[]")
    assert r.returncode == 2 and r.stdout == "" and "AskUserQuestion is off in a spoke" in r.stderr


# where, how the worker is set up, does a sensitive operation ask? Everything danger-guard guards asks when someone can answer (no worker: the human's own
# prompt; a worker with the PermissionRequest relay installed: the relay puts it to the Run) and is denied when no one can (a worker without the relay: a
# local ask nobody sees would hang). Denied in both, never asked: AskUserQuestion in a worker (below), a worker's write to the human's reply spool (lanes
# marked D) and an unparsable payload (the fail-closed test).
WF_SCENARIOS = {
    "no-run-main-checkout": ("root", None, True), "no-run-plain-worktree": ("wt", None, True),
    "worker-with-the-relay-installed": ("spoke", "installed", True),
    "worker-whose-settings-lack-the-relay": ("spoke", "unregistered", False),
    "worker-without-the-relay-file": ("spoke", "nofile", False),
    "worker-without-any-claude-dir": ("spoke", "bare", False),
}
W = "worker-only"  # first element of a lane's names: the operation is sensitive in a worker only
D = "hard-deny"  # a lane's name: denied in every scenario, never asked (the human's reply spool: an approved write would forge the human's reply)
# tool, tool_input, what the ask's reason must name (one string, or a tuple: a compound names every sensitive segment; D: never asks)
WF_LANES = [
    ("Write", {"file_path": ".github/workflows/ci.yml"}, ".github/workflows/ci.yml"),
    ("Edit", {"file_path": "{spoke}/.github/workflows/new.yml"}, "new.yml"),
    ("MultiEdit", {"file_path": "sub/../.github/workflows/ci.yml"}, ".github/workflows/ci.yml"),
    ("NotebookEdit", {"notebook_path": ".github/workflows/n.ipynb"}, ".github/workflows/n.ipynb"),
    ("Bash", {"command": "echo x > .github/workflows/ci.yml"}, "echo x > .github/workflows/ci.yml"),
    ("Bash", {"command": "sed -i s/a/b/ .github/workflows/ci.yml"}, "sed -i s/a/b/ .github/workflows/ci.yml"),
    ("Bash", {"command": "cd .github/workflows && echo x > ci.yml"}, "cd .github/workflows && echo x > ci.yml"),
    ("Write", {"file_path": "orca.yaml"}, "orca.yaml"), ("Edit", {"file_path": "./orca.yaml"}, "./orca.yaml"),
    ("Write", {"file_path": "{home}/.claude/settings.json"}, ".claude/settings.json"),
    ("Write", {"file_path": "~/.claude/settings.json"}, "~/.claude/settings.json"),
    ("Bash", {"command": "echo x > orca.yaml"}, "echo x > orca.yaml"), ("Bash", {"command": "cp x ~/.claude/settings.json"}, "cp x ~/.claude/settings.json"),
    ("Bash", {"command": "rm ~/.claude/settings.json"}, "rm ~/.claude/settings.json"),
    ("Write", {"file_path": ".claude/settings.json"}, (W, ".claude/settings.json")),
    ("Edit", {"file_path": ".claude/settings.local.json"}, (W, ".claude/settings.local.json")),
    ("Write", {"file_path": ".claude/hooks/push-guard.sh"}, (W, ".claude/hooks/push-guard.sh")),
    ("Write", {"file_path": ".ai-toolkit/spoke-run-id"}, (W, ".ai-toolkit/spoke-run-id")),
    ("Bash", {"command": "rm .claude/settings.local.json"}, (W, "rm .claude/settings.local.json")),
    # the coordinator's reply spool: a worker writing there would answer its own gate or permission question as the human
    ("Write", {"file_path": "{home}/.ai-toolkit/coordinator/run_t/replies/msg_p"}, (W, D)),
    ("Bash", {"command": "echo allow > ~/.ai-toolkit/coordinator/run_t/replies/msg_p"}, (W, D)),
    ("Bash", {"command": "cp x $HOME/.ai-toolkit/coordinator/run_t/replies/msg_p"}, (W, D)),
    ("Bash", {"command": "echo x > orca.yaml;\necho allow > ~/.ai-toolkit/coordinator/run_t/replies/msg_p"}, (W, D)),  # a hard deny beats an ask in a compound
    # rm -r: outside the worktree, the worktree root, home, another checkout, an unexpanded variable
    ("Bash", {"command": "rm -rf /usr"}, "outside the worktree: /usr"), ("Bash", {"command": "rm -rf ~/x"}, "rm -r of ~/x (worktree root or home)"),
    ("Bash", {"command": "rm -rf ."}, "rm -r of . (worktree root or home)"), ("Bash", {"command": "rm -rf {other}"}, "another git checkout or worktree: {other}"),
    ("Bash", {"command": "rm -rf $FOO/x"}, "unexpanded variable"),
    # git: reset --hard in the main checkout (or one the guard cannot pin down), and the clean/stash verbs that remove the ignored .claude/ and .ai-toolkit/
    ("Bash", {"command": "git -C {root} reset --hard"}, "reset --hard in the main checkout"),
    ("Bash", {"command": "git --work-tree={root} reset --hard"}, "reset --hard with --git-dir/--work-tree"),
    ("Bash", {"command": "git clean -fdx"}, "git clean -fdx"), ("Bash", {"command": "git stash -a"}, "git stash -a"),
    # a compound asks once and names every sensitive segment, not only the first
    ("Bash", {"command": "echo x > .github/workflows/ci.yml; rm -rf /usr"}, ("ci.yml", "outside the worktree: /usr")),
    ("Bash", {"command": "echo x > .github/workflows/ci.yml && echo y > orca.yaml"}, ("ci.yml", "orca.yaml")),
    ("Bash", {"command": "rm -rf /usr; echo x > orca.yaml; git -C {root} reset --hard; git clean -fdx"},
     ("outside the worktree: /usr", "orca.yaml", "reset --hard in the main checkout", "git clean -fdx")),
]


def install_relay(spoke, how):
    """A worker dir as sync leaves it: the marker, and per `how` the relay file and its registration in .claude/settings.json."""
    (spoke / ".ai-toolkit").mkdir(parents=True, exist_ok=True)
    (spoke / ".ai-toolkit/spoke-run-id").write_text("rid\n")
    if how != "bare":
        (spoke / ".claude/hooks").mkdir(parents=True, exist_ok=True)
        (spoke / ".claude/settings.json").write_text(json.dumps({"hooks": {"PermissionRequest": [{"matcher": "*", "hooks": [{"type": "command", "command": (
            'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/permission-relay.sh"' if how != "unregistered" else "true")}]}]}}))
    if how in ("installed", "unregistered"):
        (spoke / ".claude/hooks/permission-relay.sh").write_text("#!/bin/bash\n")


# every lane is proven once, on the ask path (a worker with the relay installed); each other scenario is proven on one file lane and one Bash lane
RELAYED = "worker-with-the-relay-installed"
FILE_LANE, BASH_LANE = WF_LANES[0], next(lane for lane in WF_LANES if lane[0] == "Bash")
WF_CASES = [(RELAYED, *lane) for lane in WF_LANES] + [("no-run-plain-worktree", *FILE_LANE), ("no-run-main-checkout", *BASH_LANE)] + [
    (scenario, *lane) for scenario in ("worker-whose-settings-lack-the-relay", "worker-without-the-relay-file", "worker-without-any-claude-dir")
    for lane in (FILE_LANE, BASH_LANE)]


@pytest.mark.parametrize("scenario,tool,ti,named", WF_CASES)
def test_danger_guard_sensitive_operations_ask_only_where_the_prompt_reaches_someone(shared, tmp_path, scenario, tool, ti, named):
    where, how, asks = WF_SCENARIOS[scenario]
    named = (named,) if isinstance(named, str) else named
    if named[0] == W:
        named = named[1:]
    if named[0] == D:
        asks, named = False, ()
    places = {**shared, "spoke": tmp_path / "w"}
    if where == "spoke":
        install_relay(places["spoke"], how)
    sub = lambda v: v.replace("{spoke}", str(places[where])).replace("{home}", str(shared["home"])).replace("{root}", str(shared["root"])).replace("{other}", str(shared["spoke"]))  # noqa: E731
    ti, named = {k: sub(v) for k, v in ti.items()}, [sub(n) for n in named]
    r = call("danger-guard.sh", places[where], tool, env=shared["env"], **ti)
    if asks:
        out = json.loads(r.stdout)["hookSpecificOutput"]  # one object: a compound asks once
        assert (r.returncode, out["hookEventName"], out["permissionDecision"], r.stderr) == (0, "PreToolUse", "ask", "")
        assert all(n in out["permissionDecisionReason"] for n in named) and "danger-guard" in out["permissionDecisionReason"], out["permissionDecisionReason"]
    else:
        assert r.returncode == 2 and r.stdout == "" and "danger-guard: blocked" in r.stderr, (r.returncode, r.stdout, r.stderr)
        assert r.stderr.count("\n") == 1 and "cannot parse" not in r.stderr, r.stderr  # one clear reason, no trap noise


def test_danger_guard_uses_the_project_dir_for_the_marker_and_the_worktree_root(shared):
    sp, plain = str(shared["spoke"]), str(shared["wt"])
    check("danger-guard.sh", shared, "wt", 2, "AskUserQuestion", env={"CLAUDE_PROJECT_DIR": sp}, questions="[]")
    check("danger-guard.sh", shared, "spoke", 0, "AskUserQuestion", env={"CLAUDE_PROJECT_DIR": plain}, questions="[]")
    for cwd, cmd, want in (("/", "rm -rf usr", 2), ("/", f"rm -rf {sp}/build", 0), (sp, f"echo x > {sp}/orca.yaml", 2)):
        r = call("danger-guard.sh", cwd, command=cmd, env={"CLAUDE_PROJECT_DIR": sp, **shared["env"]})
        assert r.returncode == want, (cwd, cmd, r.stderr)


# --- permission-relay ---------------------------------------------------------------------------
# PermissionRequest fires for every tool-permission dialog a worker would show (a PreToolUse "ask", an `ask` rule, a prompt Claude Code raises itself
# in bypass mode, a subagent's), measured on claude 2.1.289 in bypass mode. Its payload: tool_name, tool_input, cwd, permission_mode; no reason, no tool_use_id.
ASK_SHIM = """#!/bin/bash
printf '%s\\0' "$@" > "$SHIM_ARGS"; echo x >> "$SHIM_ARGS.n"
sleep "${SHIM_SLEEP:-0}"; printf '%s' "${SHIM_REPLY-}"; exit "${SHIM_RC:-0}"
"""


@pytest.fixture
def relay(tmp_path, link_script):
    """A worker dir (marker + task.md), a shim `orca` that records its argv and prints SHIM_REPLY, and a runner returning (hook output, the ask argv or None)."""
    spoke, shim = tmp_path / "w", tmp_path / "shim"
    (spoke / ".ai-toolkit").mkdir(parents=True)
    shim.mkdir()
    (spoke / ".ai-toolkit/spoke-run-id").write_text("rid\n")
    (spoke / ".ai-toolkit/task.md").write_text("# #394 feat(gate): relay every permission prompt\n\nbody\n")
    link_script(shim / "orca", ASK_SHIM)

    def run(tool="Write", ti=None, reply="allow", rc=0, raw=None, env=None, where=None, **e):
        payload = {"hook_event_name": "PermissionRequest", "tool_name": tool, "cwd": str(where or spoke), "permission_mode": "bypassPermissions",
                   "tool_input": {"file_path": ".github/workflows/ci.yml", "content": "x"} if ti is None else ti}
        env = {"PATH": f"{shim}:{os.environ['PATH']}", "SHIM_ARGS": str(tmp_path / "args"), "SHIM_REPLY": reply, "SHIM_RC": str(rc), "ORCA_TERMINAL_HANDLE": "term_w",
               **(env or {}), **{k: str(v) for k, v in e.items()}}
        r = call("permission-relay.sh", where or spoke, raw=raw if raw is not None else json.dumps(payload), env=env)
        args = (tmp_path / "args").read_bytes().decode().split("\0")[:-1] if (tmp_path / "args").exists() else None
        out = json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None
        return SimpleNamespace(rc=r.returncode, out=out, args=args, stderr=r.stderr, asks=len((tmp_path / "args.n").read_text().split()) if (tmp_path / "args.n").exists() else 0,
                               behavior=out and out["decision"]["behavior"], question=args and args[args.index("--question") + 1])

    return SimpleNamespace(run=run, dir=spoke, shim=shim, tmp=tmp_path)


@pytest.mark.parametrize("reply, behavior", [("allow", "allow"), ("allow\n", "allow"), ("  allow  \n", "allow"), ("deny", "deny"), ("", "deny"), ("allowed", "deny"),
                                             ("approve", "deny"), ("allow please", "deny"), ("ALLOW", "deny"), ("allow\ndeny", "deny"), ("no", "deny")])
def test_relay_allows_only_on_the_exact_word_allow(relay, reply, behavior):
    r = relay.run(reply=reply)
    assert (r.rc, r.out["hookEventName"], r.behavior) == (0, "PermissionRequest", behavior) and r.asks == 1
    assert ("message" in r.out["decision"]) == (behavior == "deny")


def test_relay_does_nothing_outside_a_worker(relay):
    (relay.dir / ".ai-toolkit/spoke-run-id").unlink()
    r = relay.run()
    assert (r.rc, r.out, r.asks, r.stderr) == (0, None, 0, "")  # no output = the local prompt stays


def test_relay_asks_the_run_one_question_with_options_a_timeout_and_the_workers_handle(relay):
    r = relay.run("Bash", {"command": "cd wf && rm -rf *", "description": "wipe the dir"})
    assert r.asks == 1 and r.args[:2] == ["orchestration", "ask"] and r.args[r.args.index("--from") + 1] == "term_w"
    assert r.args[r.args.index("--options") + 1] == "allow,deny" and 0 < int(r.args[r.args.index("--timeout-ms") + 1]) <= 560000
    q = r.question
    assert q.startswith("PERMISSION REQUEST") and "not a plan gate" in q.splitlines()[0]  # what the loop and the coordinate skill key on
    assert "#394" in q and str(relay.dir) in q and "tool: Bash" in q and "cd wf && rm -rf *" in q and "wipe the dir" in q and "bypassPermissions" in q


@pytest.mark.parametrize("tool, ti, shown", [
    ("Write", {"file_path": "/w/.github/workflows/ci.yml", "content": "name: ci\non: push\n"}, ["/w/.github/workflows/ci.yml", "name: ci", "on: push"]),
    ("Edit", {"file_path": "/w/a.py", "old_string": "OLD TEXT", "new_string": "NEW TEXT"}, ["/w/a.py", "OLD TEXT", "NEW TEXT"]),
    ("NotebookEdit", {"notebook_path": "/w/n.ipynb", "new_source": "print(1)"}, ["/w/n.ipynb", "print(1)"]),
    ("Bash", {"command": "line one\nline two"}, ["line one", "line two"]),
    ("mcp__x__do", {"target": "prod", "n": 3}, ["tool: mcp__x__do", "target", "prod"])])
def test_relay_question_carries_the_whole_change_whatever_the_tool(relay, tool, ti, shown):
    q = relay.run(tool, ti).question
    assert all(s in q for s in shown) and f"tool: {tool}" in q and "reason: unknown" in q


def test_relay_truncates_only_past_a_cap_and_says_so(relay):
    q = relay.run("Write", {"file_path": "/w/a", "content": "A" * 300}, PERMISSION_RELAY_CAP=100).question
    assert "A" * 100 in q and "A" * 101 not in q and "truncated" in q
    q = relay.run("Write", {"file_path": "/w/a", "content": "B" * 300}).question  # the default cap is generous
    assert "B" * 300 in q and "truncated" not in q


@pytest.mark.parametrize("tool", ["AskUserQuestion", "ExitPlanMode"])
def test_relay_denies_the_tools_a_yes_cannot_answer_without_asking(relay, tool):
    r = relay.run(tool, {"questions": []}, reply="allow")  # measured: a hook allow does not resolve ExitPlanMode; AskUserQuestion needs the user's answers
    assert (r.rc, r.behavior, r.asks) == (0, "deny", 0) and "worker_done" in r.out["decision"]["message"]


def fail_closed(relay, **kw):
    r = relay.run(**kw)
    assert (r.rc, r.behavior) == (0, "deny") and "worker_done" in r.out["decision"]["message"], (kw, r.stderr)
    return r


def test_relay_denies_when_the_ask_fails_even_if_it_printed_allow(relay):
    assert fail_closed(relay, reply="allow", rc=1).asks == 1  # orca erroring, or no Run to ask


def test_relay_denies_without_a_terminal_handle(relay):
    assert fail_closed(relay, env={"ORCA_TERMINAL_HANDLE": ""}).asks == 0


def test_relay_denies_when_orca_is_missing(relay):
    fail_closed(relay, env={"PATH": "/usr/bin:/bin"})  # no orca on the path at all


def test_relay_denies_on_a_timeout_before_the_hook_does_so_no_dialog_is_left_waiting(relay):
    r = fail_closed(relay, SHIM_SLEEP=3, PERMISSION_RELAY_KILL_S=1)  # measured: when the hook times out it is killed with NO decision and the local dialog stays
    assert "timed out" in r.out["decision"]["message"]


@pytest.mark.parametrize("raw", ["not json", "", "{"])
def test_relay_denies_an_unparsable_payload(relay, raw):
    assert fail_closed(relay, raw=raw).asks == 0


def test_relay_denies_without_jq(relay):
    (relay.tmp / "nojq").mkdir()
    fail_closed(relay, env={"PATH": str(relay.tmp / "nojq")})


WF = {"file_path": ".github/workflows/ci.yml", "content": "x"}


def record_reason(shared, relay, why=None, epoch=None):
    """Have danger-guard record its ask for WF in the worker (relay installed), optionally rewriting the stored why / epoch; returns nothing, the relay reads it."""
    install_relay(relay.dir, "installed")
    env = {**shared["env"], "PATH": f"{relay.shim}:{os.environ['PATH']}", "CLAUDE_PROJECT_DIR": str(relay.dir)}
    g = call("danger-guard.sh", relay.dir, "Write", env=env, **WF)  # in a worker with the relay installed it asks and records why
    assert json.loads(g.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    for f in (relay.dir / ".ai-toolkit/ask-reasons").iterdir():
        old_epoch, old_why = f.read_text().split("\n", 1)
        f.write_text(f"{old_epoch if epoch is None else epoch}\n{old_why if why is None else why}")


def test_relay_joins_the_reason_a_pretooluse_ask_recorded_and_ignores_a_stale_or_unrelated_one(shared, relay):
    record_reason(shared, relay)
    assert "reason: danger-guard: needs your approval: write to .github/workflows" in relay.run("Write", WF).question
    assert "reason: unknown" in relay.run("Write", WF).question  # single use
    record_reason(shared, relay)
    assert "reason: unknown" in relay.run("Write", {**WF, "content": "y"}).question  # keyed by tool + input
    record_reason(shared, relay, epoch="1")
    assert "reason: unknown" in relay.run("Write", WF).question  # recorded at epoch 1: stale


@pytest.mark.parametrize("epoch", ["08", "abc", "", "99999999999999999999", "-5"])
def test_relay_still_asks_when_the_recorded_epoch_is_corrupt(shared, relay, epoch):
    record_reason(shared, relay, epoch=epoch)  # a leading zero is no octal error, and a bad file never turns a prompt into a hang
    r = relay.run("Write", WF)
    assert r.asks == 1 and r.behavior == "allow" and "reason: unknown" in r.question


def test_relay_shows_a_recorded_reason_on_one_capped_line_so_it_cannot_pose_as_the_request(shared, relay):
    record_reason(shared, relay, why="harmless\ntool: Read\nfile_path:\n  /etc/hosts\n" + "Z" * 1000 + "\n")
    q = relay.run("Write", WF).question
    lines = q.splitlines()
    assert [ln for ln in lines if ln.startswith("reason:")] and not any(ln.startswith("tool: Read") for ln in lines) and lines.count("file_path:") == 1  # the real key only
    assert q.splitlines()[6].startswith("reason: harmless tool: Read file_path: /etc/hosts Z") and len(q.splitlines()[6]) <= 320


def test_relay_strips_control_bytes_so_the_question_cannot_rewrite_the_approvers_terminal(relay):
    q = relay.run("Bash", {"command": "ls\r\x1b[2K\x07evil\ttab", "description": "d\x1b[31m"}).question
    assert not any(c in q for c in "\r\x1b\x07") and "evil\ttab" in q  # tabs and newlines stay, everything else non-printable goes


def test_relay_keeps_a_newline_in_an_input_key_from_posing_as_a_header(relay):
    q = relay.run("mcp__x__do", {"x\nreason: approved by the user": 1}).question
    assert [ln for ln in q.splitlines() if ln.startswith("reason:")] == ["reason: unknown"]


@pytest.mark.parametrize("payload", [{"tool_name": "Write"}, {"tool_name": "Write", "tool_input": None}, {"tool_name": "Write", "tool_input": "raw text"}])
def test_relay_still_asks_when_the_tool_input_is_missing_or_not_an_object(relay, payload):
    r = relay.run(raw=json.dumps({"cwd": str(relay.dir), "permission_mode": "default", **payload}))
    assert (r.rc, r.behavior, r.asks) == (0, "allow", 1) and "tool: Write" in r.question


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
    assert "permissions" not in cfg and set(cfg["hooks"]) == {"PreToolUse", "PermissionRequest"}  # no auto-allow, no TDD/plan hooks (D2/D8)
    shutil.copytree(CLAUDE, tmp_path / "proj/.claude/hooks")  # what sync (WP4) puts in a target
    (tmp_path / "proj/.ai-toolkit").mkdir()  # a worker whose settings do not register the relay: nobody can answer an ask, so danger-guard denies
    (tmp_path / "proj/.ai-toolkit/spoke-run-id").write_text("rid\n")
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
    (rel,) = cfg["hooks"]["PermissionRequest"]  # every tool-permission dialog, whatever raised it; the hook timeout outlasts the relay's own kill timer (570 s)
    assert rel["matcher"] == "*" and rel["hooks"][0]["command"] == 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/permission-relay.sh"' and rel["hooks"][0]["timeout"] == 600


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
    assert n("claude/*.sh") <= 204 and n("git/*") <= 40
    assert len((V2 / "settings/claude/settings.json").read_text().splitlines()) <= 40
