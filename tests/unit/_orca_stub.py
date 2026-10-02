"""PATH stub for the `orca` CLI, so dispatch tests never need a real Orca (#363).

`install_orca_stub` writes an executable `orca` into a bin dir that records every call's
argv, replays canned JSON, and for `worktree create` materialises a REAL git worktree so
the later steps of the script under test have a checkout to work in. `worktree list` / `show`
are built from the cwd repo's real `git worktree list --porcelain` (`linkedIssue` and
`displayName` come from a sidecar, see `orca_link`), and `worktree rm` does what Orca does:
`git worktree remove`, then drop the local branch. `install_forbidden_stubs`
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

HOME = Path(os.environ.get("ORCA_STUB_HOME") or Path(__file__).resolve().parent / ".orca-stub")
GIT = os.environ.get("ORCA_STUB_REAL_GIT") or "__GIT__"
argv = sys.argv[1:]
with (HOME / "calls.jsonl").open("a") as fh:
    fh.write(json.dumps(argv) + "\n")
pos = [a for a in argv if not a.startswith("--")]
key = "--version" if "--version" in argv else " ".join(pos[:2])
state_f = HOME / "state.json"
state = json.loads(state_f.read_text()) if state_f.exists() else {"n": {}}
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


def fail(code, message, rc=1):
    emit(rc, {"ok": False, "error": {"code": code, "message": message}}, message + "\n")


def meta():
    f = HOME / "meta.json"
    return json.loads(f.read_text()) if f.exists() else {}


def link(path, **fields):
    m = meta()
    m.setdefault(os.path.realpath(path), {}).update(fields)
    (HOME / "meta.json").write_text(json.dumps(m))


def rows(repo):
    out = subprocess.run([GIT, "-C", repo, "worktree", "list", "--porcelain"],
                         capture_output=True, text=True).stdout
    found, m = [], meta()
    for i, block in enumerate(b for b in out.strip().split("\n\n") if b):
        info = dict(ln.partition(" ")[::2] for ln in block.splitlines())
        path = info["worktree"]
        extra = m.get(os.path.realpath(path), {})
        found.append({"id": f"stub-repo::{path}", "path": path, "branch": info.get("branch", ""),
                      "displayName": extra.get("displayName")
                      or (info.get("branch", "").rpartition("/")[2] or Path(path).name),
                      "linkedIssue": extra.get("linkedIssue"), "isMainWorktree": i == 0})
    return found


def find(selector, repo):
    kind, _, value = selector.partition(":")
    for row in rows(repo):
        if kind == "path" and os.path.realpath(row["path"]) == os.path.realpath(value):
            return row
        if kind == "id" and row["id"] == value:
            return row
        if kind == "issue" and str(row["linkedIssue"]) == value:
            return row
        if kind == "branch" and row["branch"].removeprefix("refs/heads/") == value:
            return row
    fail("selector_not_found", f"orca stub: no worktree matches {selector}")


def remove(row, repo, rm):
    if "--force" not in argv and subprocess.run(
            [GIT, "-C", row["path"], "status", "--porcelain"], capture_output=True, text=True).stdout.strip():
        fail("worktree_dirty", f"orca stub: {row['path']} has uncommitted changes; pass --force")
    subprocess.run([GIT, "-C", repo, "worktree", "remove", "--force", row["path"]], check=True)
    flag = {"merged": "-d", "always": "-D"}.get(rm.get("branch", "merged"))
    if flag and row["branch"]:
        subprocess.run([GIT, "-C", repo, "branch", flag, row["branch"].removeprefix("refs/heads/")],
                       capture_output=True)


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
repo = (opt("--repo") or "path:" + os.getcwd()).removeprefix("path:")
if key == "worktree create":
    name = opt("--name")
    path = Path(os.environ.get("ORCA_STUB_WORKSPACES", str(Path(repo).parent / "orca-ws"))) / name
    subprocess.run([GIT, "-C", repo, "worktree", "add", "-q", "-b", name, str(path),
                    opt("--base-branch", "HEAD")], check=True)
    link(path, linkedIssue=opt("--issue") or None)
    emit(0, ok({"worktree": find(f"path:{path}", repo)}))
if key == "worktree list":
    emit(0, ok({"worktrees": rows(repo)}))
if key == "worktree show":
    emit(0, ok({"worktree": find(opt("--worktree"), repo)}))
if key == "worktree rm":
    row, rm = find(opt("--worktree"), repo), scenario.get("_rm", {})
    if rm.get("refuse"):
        fail("worktree_refused", f"orca stub: refusing to remove {row['path']}")
    if rm.get("drop") == "before":
        fail("runtime_unavailable", "orca stub: runtime_unavailable")
    remove(row, repo, rm)
    if rm.get("drop") == "after":
        fail("runtime_unavailable", "orca stub: runtime_unavailable")
    emit(0, ok({"removed": row["path"]}))
if key == "orchestration worker-release":
    emit(0, ok({"released": opt("--dispatch")}))
if key == "skills installed":
    emit(0, [{"name": "orca-cli"}, {"name": "orchestration"}])
if key == "worktree set":
    if "--issue" in argv:
        link(find(opt("--worktree"), repo)["path"], linkedIssue=opt("--issue"))
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
    stub.write_text(
        _STUB.replace("__PY__", sys.executable).replace("__GIT__", shutil.which("git") or "git")
    )
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


def orca_scenario(bindir: Path, scenario: dict) -> None:
    """Replace the installed stub's scenario (canned replies plus the `_rm` switches).

    `_rm` simulates `worktree rm`: `refuse` (Orca declines), `drop` (`"after"`: the removal
    happened but the CLI died with `runtime_unavailable`; `"before"`: it died without removing)
    and `branch` (`merged` = `git branch -d` like Orca, `always` = `-D`, `never` = keep it).
    """
    (bindir / ".orca-stub" / "scenario.json").write_text(json.dumps(scenario))


def orca_link(bindir: Path, path: Path, *, issue: int | None = None, name: str = "") -> None:
    """Record the `linkedIssue` / `displayName` the stub reports for the worktree at `path`."""
    meta_f = bindir / ".orca-stub" / "meta.json"
    meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
    entry = meta.setdefault(os.path.realpath(path), {})
    entry.update({"linkedIssue": issue} if issue is not None else {})
    entry.update({"displayName": name} if name else {})
    meta_f.write_text(json.dumps(meta))


def install_forbidden_stubs(bindir: Path) -> Path:
    """Install `tmux`, `code` and a `git` wrapper that log being used for a retired path.

    The git wrapper only records `git worktree add|remove|prune` (and refuses it); every other
    git call is forwarded to the real binary, so repo setup and `git branch -m` keep working.
    The `orca` stub calls the real git by absolute path, so it never trips the wrapper.

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
        '  if [ "$prev" = worktree ]; then case "$a" in add|remove|prune)\n'
        f'    echo "git worktree $a" >> "{log}"; exit 97 ;; esac; fi\n'
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
