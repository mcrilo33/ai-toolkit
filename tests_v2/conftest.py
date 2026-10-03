"""Fixtures: PATH stubs for orca/gh/claude, a temp repo with a bare origin, env isolation."""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

V2 = Path(__file__).resolve().parent.parent / "v2"
# records argv (US-separated) + env; replays <name>.<arg1>_<arg2>.<call#> > <name>.<arg1>_<arg2> > <name>
STUB = """#!/bin/sh
n=$(basename "$0"); d=$STUB_DIR; env > "$d/$n.env"
printf '%s' "$n" >> "$d/calls.log"; for a in "$@"; do printf '\\037%s' "$a" >> "$d/calls.log"; done; echo >> "$d/calls.log"
k="$n.$(echo "$1_$2" | tr -c 'A-Za-z0-9_\\n' _)"; c=$(( $(cat "$d/$k.count" 2>/dev/null || echo 0) + 1 )); echo $c > "$d/$k.count"
for f in "$d/$k.$c" "$d/$k" "$d/$n"; do [ -f "$f" ] && { cat "$f"; exit "$(cat "$f.rc" 2>/dev/null || echo 0)"; }; done; exit 0
"""


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for k in [k for k in os.environ if k.startswith(("GIT_", "OTEL_", "ORCA_", "CLAUDE_CODE_", "AI_TOOLKIT_"))]:
        monkeypatch.delenv(k)
    (tmp_path / "gitconfig").write_text("[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def stubs(monkeypatch, tmp_path):
    d = tmp_path / "stubs"
    (d / "bin").mkdir(parents=True)
    for name in ("orca", "gh", "claude"):
        (d / "bin" / name).write_text(STUB)
        (d / "bin" / name).chmod(0o755)
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


@pytest.fixture
def repo(tmp_path):
    origin, root = tmp_path / "origin.git", tmp_path / "root"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(root)], check=True, capture_output=True)
    (root / "README").write_text("x")
    for args in (["add", "."], ["commit", "-qm", "init"], ["push", "-q", "origin", "main"]):
        git(root, *args)
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
