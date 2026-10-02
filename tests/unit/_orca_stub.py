"""PATH stub for the `orca` CLI, so dispatch tests never need a real Orca (#363).

`install_orca_stub` writes an executable `orca` into a bin dir that records every call's
argv, replays canned JSON, and for `worktree create` materialises a REAL git worktree so
the later steps of the script under test have a checkout to work in. `install_forbidden_stubs`
adds `tmux` / `code` / a `git` wrapper whose only job is to record being invoked, so a test
can assert the retired launch paths are never taken.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

_STUB = r"""#!__PY__
import json, os, subprocess, sys
from pathlib import Path

HOME = Path(os.environ["ORCA_STUB_HOME"])
argv = sys.argv[1:]
with (HOME / "calls.jsonl").open("a") as fh:
    fh.write(json.dumps(argv) + "\n")
pos = [a for a in argv if not a.startswith("--")]
key = "--version" if "--version" in argv else " ".join(pos[:2])
state_f = HOME / "state.json"
state = json.loads(state_f.read_text()) if state_f.exists() else {"n": {}, "wts": []}
scenario = json.loads((HOME / "scenario.json").read_text())


def opt(name, default=""):
    return argv[argv.index(name) + 1] if name in argv else default


def emit(rc, out, err=""):
    state_f.write_text(json.dumps(state))
    if err:
        sys.stderr.write(err)
    print(out if isinstance(out, str) else json.dumps(out))
    sys.exit(rc)


def ok(result):
    return {"ok": True, "result": result}


for snap in scenario.get("_snapshot", []):
    if snap["key"] == key:
        f = Path(snap["path"])
        with (HOME / "snapshots.jsonl").open("a") as fh:
            fh.write(json.dumps({**snap, "content": f.read_text() if f.exists() else None}) + "\n")
seq = scenario.get(key)
if seq is not None:
    i = state["n"].get(key, 0)
    state["n"][key] = i + 1
    r = seq[min(i, len(seq) - 1)]
    emit(r.get("rc", 0), r.get("out", {}), r.get("stderr", ""))

if key == "--version":
    emit(0, os.environ.get("ORCA_STUB_VERSION", "1.4.218"))
if key == "worktree create":
    name = opt("--name")
    root = opt("--repo").removeprefix("path:")
    path = Path(os.environ.get("ORCA_STUB_WORKSPACES", str(Path(root).parent / "orca-ws"))) / name
    git = os.environ["ORCA_STUB_REAL_GIT"]
    subprocess.run([git, "-C", root, "worktree", "add", "-q", "-b", name, str(path),
                    opt("--base-branch", "HEAD")], check=True)
    wt = {"id": f"stub-repo::{path}", "path": str(path), "branch": f"refs/heads/{name}",
          "displayName": name, "linkedIssue": opt("--issue") or None}
    state["wts"].append(wt)
    emit(0, ok({"worktree": wt}))
if key == "worktree list":
    emit(0, ok({"worktrees": state["wts"]}))
if key == "skills installed":
    emit(0, [{"name": "orca-cli"}, {"name": "orchestration"}])
if key == "worktree set":
    emit(0, ok({}))
if key == "worktree ps":
    emit(0, ok({"worktrees": []}))
if key == "orchestration run-current":
    emit(0, ok({"run": {"id": "run_stub"}}))
if key == "terminal create":
    emit(0, ok({"terminal": {"handle": "term_stub"}}))
if key == "terminal show":
    emit(0, ok({"terminal": {"agentIdentity": "claude"}}))
if key == "terminal wait":
    emit(0, ok({"wait": {"satisfied": True}}))
if key == "orchestration worker-start":
    emit(0, ok({"runId": "run_stub", "taskId": "task_stub", "dispatchId": "ctx_stub",
                "state": "ready", "stage": "input_accepted"}))
emit(2, "", f"orca stub: unhandled command {key!r}\n")
"""


def install_orca_stub(
    bindir: Path,
    *,
    scenario: dict[str, list[dict]] | None = None,
    version: str = "1.4.218",
) -> dict[str, str]:
    """Install the `orca` stub into `bindir` and return the env that activates it.

    Args:
        bindir: Directory that will be put first on PATH.
        scenario: Canned replies keyed by `"<noun> <verb>"` (e.g. `"worktree create"`), each a
            list consumed one per call (the last repeats); a reply is `{"rc", "out", "stderr"}`.
        version: What `orca --version` prints.

    Returns:
        Env entries (PATH prefix excluded) to merge into the subprocess env.
    """
    home = bindir / ".orca-stub"
    home.mkdir(parents=True, exist_ok=True)
    (home / "scenario.json").write_text(json.dumps(scenario or {}))
    (home / "calls.jsonl").touch()
    stub = bindir / "orca"
    stub.write_text(_STUB.replace("__PY__", sys.executable))
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return {
        "ORCA_STUB_HOME": str(home),
        "ORCA_STUB_VERSION": version,
        "ORCA_STUB_REAL_GIT": shutil.which("git") or "git",
        "ORCA_STUB_WORKSPACES": str(bindir.parent / "orca-ws"),
    }


def orca_calls(bindir: Path) -> list[list[str]]:
    """Return every recorded `orca` invocation's argv, in order."""
    log = bindir / ".orca-stub" / "calls.jsonl"
    return [json.loads(ln) for ln in log.read_text().splitlines() if ln]


def install_forbidden_stubs(bindir: Path) -> Path:
    """Install `tmux`, `code` and a `git` wrapper that log being used for a retired path.

    The git wrapper only records `git worktree add` (and refuses it); every other git call is
    forwarded to the real binary, so repo setup and `git branch -m` keep working.

    Returns:
        The log file; it stays empty unless a forbidden command ran.
    """
    log = bindir / "forbidden.log"
    log.touch()
    for name in ("tmux", "code"):
        exe = bindir / name
        exe.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 97\n')
        exe.chmod(0o755)
    git = bindir / "git"
    git.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        f'  if [ "$prev" = worktree ] && [ "$a" = add ]; then echo "git worktree add" >> "{log}"; exit 97; fi\n'
        '  prev="$a"\n'
        "done\n"
        f'exec "{shutil.which("git") or "git"}" "$@"\n'
    )
    git.chmod(0o755)
    return log


def install_dispatch_env(bindir: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env that lets worktree-new.sh dispatch against the stub (no real Orca)."""
    env = {**(base if base is not None else os.environ), **install_orca_stub(bindir)}
    env.update(ORCA_TERMINAL_HANDLE="term_hub", ORCA_SETTLE_SLEEP="0", ORCA_AGENT_SLEEP="0")
    return stub_env(bindir, env)


def stub_env(bindir: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Return `base` (default: os.environ) with `bindir` first on PATH."""
    env = dict(base if base is not None else os.environ)
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    return env
