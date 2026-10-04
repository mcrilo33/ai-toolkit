import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest_plugins = ["pytester"]  # the time-limit pin runs an inner session

ROOT = Path(__file__).resolve().parent.parent
V2 = ROOT / "v2" if (ROOT / "v2").is_dir() else ROOT  # cutover.sh moves v2/* to the root
# records argv (US-separated) + env; replays <name>.<arg1>_<arg2>.<call#> > <name>.<arg1>_<arg2> > <name>
STUB = """#!/bin/bash
n=${0##*/}; d=$STUB_DIR; [ -n "$STUB_NOENV" ] || env > "$d/$n.env"
printf '%s' "$n" >> "$d/calls.log"; for a in "$@"; do printf '\\037%s' "$a" >> "$d/calls.log"; done; echo >> "$d/calls.log"
k="$1_$2"; k="$n.${k//[^A-Za-z0-9_]/_}"; c=0; [ -f "$d/$k.count" ] && read -r c < "$d/$k.count"; c=$((c + 1)); echo $c > "$d/$k.count"
for f in "$d/$k.$c" "$d/$k" "$d/$n"; do   # builtins only: a stub call costs one process, since the suite is bound by spawning
  [ -f "$f" ] || continue; IFS= read -r -d '' out < "$f"; printf '%s' "$out"; rc=0; [ -f "$f.rc" ] && read -r rc < "$f.rc"; exit "$rc"
done; exit 0
"""


STUB_TEE = STUB.replace("n=${0##*/}", """IFS= read -r -d '' in; printf '%s' "$in" > "$STUB_DIR/${0##*/}.stdin"; n=${0##*/}""", 1)   # also records its stdin


@pytest.fixture(scope="session")
def link_script(tmp_path_factory):   # (path, text) -> path: a script is written once per worker and every call links to it
    d, made = tmp_path_factory.mktemp("scripts"), {}   # the first run of each new executable file costs ~0.1 s on macOS and queues behind every other worker's, so a test never writes one; a symlink to a file already run is free

    def link(path, text):
        if text not in made:
            made[text] = d / str(len(made))
            made[text].write_text(text)
            made[text].chmod(0o755)
        path.unlink(missing_ok=True)
        path.symlink_to(made[text])
        return path
    return link


PHASES = pytest.StashKey[dict]()


def pytest_addoption(parser):
    parser.addini("test_time_limit", "seconds one test may take, setup + call + teardown; over it the test fails", default="5")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):   # no marker or option opts a test out: a slow test is made fast or deleted
    rep = (yield).get_result()
    phases = item.stash.setdefault(PHASES, {})
    phases[call.when] = (call.duration, rep.passed)
    spent = sum(d for d, _ in phases.values())
    limit = float(item.config.getini("test_time_limit"))
    if call.when == "teardown" and all(ok for _, ok in phases.values()) and spent > limit:   # a test that already failed is not reported twice
        parts = " + ".join(f"{k} {d:.2f}" for k, (d, _) in phases.items())
        rep.outcome, rep.longrepr = "failed", f"{item.nodeid} took {spent:.2f} s ({parts}), limit {limit:g} s"


def pytest_terminal_summary(terminalreporter):   # each CI leg shows its margin: the three slowest tests
    spent = {}
    for reports in terminalreporter.stats.values():
        for rep in reports:
            if hasattr(rep, "duration"):
                spent[rep.nodeid] = spent.get(rep.nodeid, 0) + rep.duration
    for nodeid, secs in sorted(spent.items(), key=lambda kv: -kv[1])[:3]:
        terminalreporter.write_line(f"slowest: {secs:6.2f} s {nodeid[:150]}")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for k in [k for k in os.environ if k.startswith(("GIT_", "OTEL_", "ORCA_", "CLAUDE_CODE_", "AI_TOOLKIT_"))]:
        monkeypatch.delenv(k)
    monkeypatch.delenv("CLAUDE_REAL", raising=False)
    monkeypatch.chdir(tmp_path)  # no test may read the real checkout's local env
    (tmp_path / "gitconfig").write_text("[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def stubs(monkeypatch, tmp_path, link_script):
    d = tmp_path / "stubs"
    (d / "bin").mkdir(parents=True)
    for name in ("orca", "gh", "claude"):
        link_script(d / "bin" / name, STUB)
    monkeypatch.setenv("STUB_DIR", str(d))
    monkeypatch.setenv("PATH", f"{d / 'bin'}:{os.environ['PATH']}")

    def reply(key, out="", rc=0, n=None):
        f = d / (key if n is None else f"{key}.{n}")
        f.write_text(out)
        (d / f"{f.name}.rc").write_text(str(rc))

    def calls(name):
        rows = [ln.split("\x1f") for ln in (d / "calls.log").read_text().splitlines()] if (d / "calls.log").exists() else []
        return [r[1:] for r in rows if r[0] == name]

    return SimpleNamespace(reply=reply, calls=calls, env=lambda n: (d / f"{n}.env").read_text())


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="session")
def origin_template(tmp_path_factory):  # per xdist worker: a bare origin with `init` on main and a clone of it; each test gets copies of both
    base = tmp_path_factory.mktemp("origin_template")
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def sh(*a, cwd=base):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, env=env, check=True, capture_output=True)

    sh("init", "-q", "--bare", "-b", "main", "origin.git")
    sh("clone", "-q", "origin.git", "seed")
    (base / "seed" / "README").write_text("x")
    for a in (["add", "."], ["commit", "-qm", "init"], ["push", "-q", "origin", "main"]):
        sh(*a, cwd=base / "seed")
    sh("clone", "-q", "origin.git", "root")   # after the push, so origin/HEAD is set as in a real clone
    return base


@pytest.fixture
def repo(tmp_path, origin_template):
    origin, root = tmp_path / "origin.git", tmp_path / "root"
    shutil.copytree(origin_template / "origin.git", origin)
    shutil.copytree(origin_template / "root", root)   # a copy, not a clone: one git process fewer per test
    config = root / ".git" / "config"
    config.write_text(re.sub(r"(?m)^(\s*url = ).*$", lambda m: m[1] + str(origin), config.read_text()))
    (root / ".claude" / "hooks").mkdir(parents=True)
    (root / ".claude" / "hooks" / "guard.sh").write_text("#!/bin/sh\n")

    def wt(b):
        git(root, "worktree", "add", "-q", "-b", b, str(tmp_path / b))
        return tmp_path / b

    return SimpleNamespace(root=root, wt=wt)


@pytest.fixture
def run():
    return lambda argv, cwd=None, **env: subprocess.run(
        argv, cwd=cwd, env={**os.environ, **{k: str(v) for k, v in env.items()}}, capture_output=True, text=True)
