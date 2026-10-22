import pytest
from conftest import V2

SPOKE = str(V2 / "bin/claude-spoke")


def MCP(tk, *local):
    """What the launcher puts in front of a worker's arguments: only the toolkit's server list (and the host's own), no Chrome."""
    return ["--strict-mcp-config", "--mcp-config", f"{tk}/mcp.json", *[a for f in local for a in ("--mcp-config", f"{tk}/{f}")], "--no-chrome"]


def in_spoke(repo, run_id="abc-123"):
    wt = repo.wt("wp0")
    (wt / ".ai-toolkit").mkdir()
    (wt / ".ai-toolkit/spoke-run-id").write_text(run_id + "\n")
    return wt


def synced(repo):
    """What sync.sh leaves in the main checkout: the toolkit's hook registrations and MCP server list. Returns the physical .ai-toolkit dir."""
    (repo.root / ".ai-toolkit").mkdir(exist_ok=True)
    (repo.root / ".ai-toolkit/mcp.json").write_text('{"mcpServers": {}}\n')
    (repo.root / ".ai-toolkit/claude-settings.json").write_text('{"disableAllHooks": false, "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}\n')
    return repo.root.resolve() / ".ai-toolkit"


def test_spoke_exports_native_otel_env_and_passes_args_through(run, stubs, repo, tmp_path):
    (tmp_path / "l.env").write_text("SECRET_PROBE=s3cret\n")
    tk = synced(repo)
    wt = in_spoke(repo)
    (wt / ".ai-toolkit/task.md").write_text("# #464 feat(otel): tag spoke telemetry\n\nbody mentioning # #7\n")
    r = run([SPOKE, "--model", "m"], cwd=wt, OTEL_ENDPOINT="http://collector:4317", AI_TOOLKIT_LOCAL_ENV=tmp_path / "l.env")
    env = stubs.env("claude")
    assert r.returncode == 0 and stubs.calls("claude") == [[*MCP(tk), "--settings", f"{tk}/claude-settings.json", "--model", "m"]], r.stderr   # main's files, from a worker's worktree
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in env and "OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317" in env
    assert "OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=abc-123,repo=origin,issue=464" in env and "OTLP_HEADERS" not in env
    assert "SECRET_PROBE" not in env and "s3cret" not in env
    assert f"AI_TOOLKIT_DIR={tk}" in env   # the hooks and /coordinate read the guards' place from here


@pytest.mark.parametrize("task_md", [None, "hello\n", "# #12x title\n"])
def test_spoke_leaves_the_issue_attribute_out_when_task_md_is_missing_or_does_not_parse(run, stubs, repo, task_md):
    synced(repo)
    wt = in_spoke(repo)
    if task_md is not None:
        (wt / ".ai-toolkit/task.md").write_text(task_md)
    r = run([SPOKE], cwd=wt)
    assert r.returncode == 0 and len(stubs.calls("claude")) == 1, r.stderr
    assert "OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=abc-123,repo=origin\n" in stubs.env("claude") + "\n" and "issue=" not in stubs.env("claude")


@pytest.mark.parametrize("stamp,suffix", [
    ("abcdef012345\n", ",toolkit_version=abcdef012345"), ("abcdef012345-dirty\n", ",toolkit_version=abcdef012345-dirty"), ("abcdef0", ",toolkit_version=abcdef0"),
    (None, ""), ("abc,x=y\n", ""), ("abcdef\n", ""), ("ABCDEF012345\n", ""), ("abcdef012345\nabcdef012345\n", ""), ("abcdef012345-dirty,x=y\n", ""),
])
def test_spoke_tags_the_toolkit_version_only_from_a_well_formed_stamp(run, stubs, repo, stamp, suffix):
    tk = synced(repo)
    if stamp is not None:
        (tk / "toolkit-version").write_text(stamp)
    r = run([SPOKE], cwd=in_spoke(repo))
    assert r.returncode == 0, r.stderr
    assert f"OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=abc-123,repo=origin{suffix}\n" in stubs.env("claude") + "\n"


def test_spoke_is_a_plain_claude_without_a_run_id_or_when_opted_out(run, stubs, repo):
    assert run([SPOKE, "-p", "hi"], cwd=repo.wt("plain")).returncode == 0
    tk = synced(repo)
    run([SPOKE], cwd=in_spoke(repo), AI_TOOLKIT_OTEL=0)
    assert [c[len(MCP(tk)) + 2:] for c in stubs.calls("claude")[1:]] == [[]] and stubs.calls("claude")[0] == ["-p", "hi"] and "TELEMETRY" not in stubs.env("claude")


@pytest.mark.parametrize("project,worker,args,ok,passed,fwd", [
    ("synced", False, ["-p", "x"], True, "settings", ["-p", "x"]),     # the project the session starts in has the toolkit's file: the guards ride along
    ("unsynced", False, ["-p", "x"], True, "plain", ["-p", "x"]),      # Orca's one Claude command serves every project: an unsynced one gets plain Claude, no error
    ("unsynced", True, ["-p", "x"], False, None, None),                # a worker with no settings file is refused, never started guardless
    ("synced", False, ["--settings", "mine.json"], False, None, None),    # two --settings do not merge (the last wins and the toolkit's hooks vanish): refused
    ("synced", False, ["--settings=mine.json"], False, None, None),
    ("synced", False, ["--settings", '{"disableAllHooks":true}'], False, None, None),   # what Orca's Chat UI adds on its own: every hook off
    ("synced", False, ['--settings={"disableAllHooks":true}'], False, None, None),
    ("synced", False, ["--settings", "ai-toolkit", "--settings", "mine.json"], False, None, None),   # the sentinel does not license a second --settings
    ("synced", False, ["-p", "--settings", "ai-toolkit", "--verbose"], True, "settings", ["-p", "--verbose"]),   # the sentinel (Orca's Claude arguments) resolves to this project's guard file
    ("synced", False, ["--settings=ai-toolkit", "-p"], True, "settings", ["-p"]),
    ("unsynced", False, ["--settings", "ai-toolkit", "-p"], True, "plain", ["-p"]),   # unsynced: the sentinel is dropped, chat still starts
    ("unsynced", True, ["--settings=ai-toolkit"], False, None, None),                 # a worker without its guard file is never started, sentinel or not
    ("broken", False, ["-p", "x"], False, None, None),          # a file claude might skip with a warning, or one that registers nothing, would start a session without guards: refused
    ("empty", False, ["-p", "x"], False, None, None),
])
def test_the_launcher_passes_the_settings_file_of_the_project_it_starts_in(run, stubs, repo, project, worker, args, ok, passed, fwd):
    tk = synced(repo) if project in ("synced", "broken", "empty") else None
    if project in ("broken", "empty"):
        (tk / "claude-settings.json").write_text("{not json" if project == "broken" else "{}")
    r = run([SPOKE, *args], cwd=in_spoke(repo) if worker else repo.wt("w"), AI_TOOLKIT_DIR="/inherited")   # an inherited variable must never survive into a session that has no guards
    calls = stubs.calls("claude")
    assert (r.returncode == 0) == ok and len(calls) == int(ok), r.stderr
    if passed == "settings":
        assert calls == [["--settings", f"{tk}/claude-settings.json", *fwd]] and f"AI_TOOLKIT_DIR={tk}" in stubs.env("claude")
    if passed == "plain":
        assert calls == [fwd] and "AI_TOOLKIT_DIR" not in stubs.env("claude")   # not even the inherited one
    if not ok:
        assert "settings" in r.stderr


@pytest.mark.parametrize("local,args,ok,fwd", [
    (None, ["-p", "x"], True, ["-p", "x"]),
    ("mcp.local.json", ["-p", "x"], True, ["-p", "x"]),               # the host's own list rides along after the toolkit's
    (None, ["--mcp-config", "mine.json"], False, None),               # the launcher owns the list: a caller's own would add servers
    (None, ["--mcp-config=mine.json"], False, None),
    (None, ["--strict-mcp-config"], False, None),
    (None, ["--chrome"], False, None),                                # a later --chrome would switch the Chrome tools back on
    ("missing", ["-p"], False, None),                                 # no list file: refused, never every server of the host user
    ("empty", ["-p"], False, None),
    ("broken", ["-p"], False, None),                                  # a list that is not {"mcpServers": {...}} is refused
    ("noservers", ["-p"], False, None),
    ("broken-local", ["-p"], False, None),                            # the same rule for the host's own list
])
def test_a_worker_starts_with_only_the_toolkit_mcp_list(run, stubs, repo, local, args, ok, fwd):
    tk = synced(repo)
    bad = {"empty": "", "broken": "{not json", "noservers": "{}"}
    if local == "missing":
        (tk / "mcp.json").unlink()
    elif local in bad:
        (tk / "mcp.json").write_text(bad[local])
    elif local == "broken-local":
        (tk / "mcp.local.json").write_text("{not json")
    elif local:
        (tk / local).write_text('{"mcpServers": {"mine": {"command": "true"}}}\n')
    r = run([SPOKE, *args], cwd=in_spoke(repo))
    assert (r.returncode == 0) == ok, r.stderr
    assert stubs.calls("claude") == ([[*MCP(tk, *[local] * bool(local)), "--settings", f"{tk}/claude-settings.json", *fwd]] if ok else []) and (ok or "mcp" in r.stderr)


def test_a_human_session_keeps_its_own_mcp_servers(run, stubs, repo):
    tk = synced(repo)
    assert run([SPOKE, "--mcp-config", "mine.json", "-p"], cwd=repo.wt("h")).returncode == 0
    assert stubs.calls("claude") == [["--settings", f"{tk}/claude-settings.json", "--mcp-config", "mine.json", "-p"]]   # no run id: nothing added, nothing refused


def test_install_checks_prerequisites_then_prints_the_orca_override_without_a_path_shim(run, stubs, tmp_path):
    # spawns are the cost on macOS: a set ORCA_ROOT_PATH spares load_env its `git rev-parse` (the /usr/bin git there is an xcrun shim), STUB_NOENV the stub its `env`
    quick = dict(ORCA_ROOT_PATH=tmp_path, STUB_NOENV=1)
    r = run(["bash", f"{V2}/scripts/install.sh"], PATH=f"{tmp_path}:/usr/bin:/bin", **quick)
    assert r.returncode != 0 and "missing prerequisite" in r.stderr and "orca" in r.stderr
    stubs.reply("orca", "1.4.219")
    r = run(["bash", f"{V2}/scripts/install.sh"], **quick)
    assert r.returncode == 0, r.stderr
    assert f"{V2}/bin/claude-spoke" in r.stdout and not any((tmp_path / "home").rglob("claude"))
