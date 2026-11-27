import json

import pytest
from conftest import V2

SPOKE = str(V2 / "bin/claude-spoke")


def MCP(tk, *local):
    """What the launcher puts in front of a worker's arguments: only the toolkit's server list (and the host's own), no Chrome."""
    return ["--strict-mcp-config", "--mcp-config", f"{tk}/mcp.json", *[a for f in local for a in ("--mcp-config", f"{tk}/{f}")], "--no-chrome"]


def WORKER_SETTINGS(tk):
    """What a worker gets in place of the guard file's path: the same file inline, with Claude's own notifications off (only the coordinator alerts the human) and Remote Control pinned off."""
    base = json.loads((tk / "claude-settings.json").read_text())
    return json.dumps({**base, "preferredNotifChannel": "notifications_disabled", "remoteControlAtStartup": False}, separators=(",", ":"))


def COORD_SETTINGS(tk):
    """What any other session in a synced project (the coordinator or hub) gets: the same file inline, with Remote Control on so its PushNotification reaches the phone."""
    base = json.loads((tk / "claude-settings.json").read_text())
    return json.dumps({**base, "remoteControlAtStartup": True}, separators=(",", ":"))


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
    assert r.returncode == 0 and stubs.calls("claude") == [[*MCP(tk), "--settings", WORKER_SETTINGS(tk), "--model", "m"]], r.stderr   # main's files, from a worker's worktree
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
    assert "TELEMETRY" not in stubs.env("claude")   # an unsynced project: no guards, no OTel
    tk = synced(repo)
    run([SPOKE], cwd=in_spoke(repo), AI_TOOLKIT_OTEL=0)
    assert [c[len(MCP(tk)) + 2:] for c in stubs.calls("claude")[1:]] == [[]] and stubs.calls("claude")[0] == ["-p", "hi"] and "TELEMETRY" not in stubs.env("claude")


@pytest.mark.parametrize("stamp,suffix", [("abcdef012345\n", ",toolkit_version=abcdef012345"), (None, "")])
def test_spoke_tags_a_session_in_a_synced_main_checkout_as_the_coordinator(run, stubs, repo, stamp, suffix):
    tk = synced(repo)
    if stamp is not None:
        (tk / "toolkit-version").write_text(stamp)
    (repo.root / ".ai-toolkit/task.md").write_text("# #464 feat(otel): never read for a coordinator\n")
    assert run([SPOKE], cwd=repo.root).returncode == 0
    env = stubs.env("claude")
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in env and f"OTEL_RESOURCE_ATTRIBUTES=role=coordinator,repo=origin{suffix}\n" in env + "\n"
    assert "spoke_run_id" not in env and "issue=" not in env
    assert run([SPOKE], cwd=repo.root, AI_TOOLKIT_OTEL=0).returncode == 0 and "TELEMETRY" not in stubs.env("claude")   # the opt-out covers the coordinator too


@pytest.mark.parametrize("project,worker,args,ok,passed,fwd", [
    ("synced", False, ["-p", "x"], True, "settings", ["-p", "x"]),     # the project the session starts in has the toolkit's file: the guards ride along
    ("unsynced", False, ["-p", "x"], True, "plain", ["-p", "x"]),      # Orca's one Claude command serves every project: an unsynced one gets plain Claude, no error
    ("unsynced", True, ["-p", "x"], False, None, None),                # a worker with no settings file is refused, never started guardless
    ("synced", False, ["--settings", "mine.json"], False, None, None),    # two --settings do not merge (the last wins and the toolkit's hooks vanish): refused
    ("synced", False, ["--settings=mine.json"], False, None, None),
    ("synced", True, ["--settings", "mine.json"], False, None, None),     # the same refusal for a worker, whose settings are inline JSON
    ("synced", True, ["--settings=mine.json"], False, None, None),
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
        assert tk is not None
        (tk / "claude-settings.json").write_text("{not json" if project == "broken" else "{}")
    r = run([SPOKE, *args], cwd=in_spoke(repo) if worker else repo.wt("w"), AI_TOOLKIT_DIR="/inherited")   # an inherited variable must never survive into a session that has no guards
    calls = stubs.calls("claude")
    assert (r.returncode == 0) == ok and len(calls) == int(ok), r.stderr
    if passed == "settings":
        assert calls == [["--settings", COORD_SETTINGS(tk), *fwd]] and f"AI_TOOLKIT_DIR={tk}" in stubs.env("claude")
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
    assert stubs.calls("claude") == ([[*MCP(tk, *[local] * bool(local)), "--settings", WORKER_SETTINGS(tk), *fwd]] if ok else []) and (ok or "mcp" in r.stderr)


def test_a_worker_gets_notifications_disabled_and_remote_control_off_a_coordinator_remote_control_on(run, stubs, repo):
    tk = synced(repo)
    assert run([SPOKE, "-p"], cwd=in_spoke(repo)).returncode == 0 and run([SPOKE, "-p"], cwd=repo.wt("h")).returncode == 0
    worker, human = stubs.calls("claude")
    cfg = json.loads(worker[worker.index("--settings") + 1])
    assert cfg["preferredNotifChannel"] == "notifications_disabled" and cfg["remoteControlAtStartup"] is False and cfg["disableAllHooks"] is False and cfg["hooks"]["PreToolUse"]   # the guards ride along; off even when the user's own settings turn it on
    hcfg = json.loads(human[human.index("--settings") + 1])
    assert hcfg["remoteControlAtStartup"] is True and "preferredNotifChannel" not in hcfg and hcfg["disableAllHooks"] is False and hcfg["hooks"]["PreToolUse"]   # no run id: its own alerts stay on
    assert human == ["--settings", COORD_SETTINGS(tk), "-p"]


def test_a_human_session_keeps_its_own_mcp_servers(run, stubs, repo):
    tk = synced(repo)
    assert run([SPOKE, "--mcp-config", "mine.json", "-p"], cwd=repo.wt("h")).returncode == 0
    assert stubs.calls("claude") == [["--settings", COORD_SETTINGS(tk), "--mcp-config", "mine.json", "-p"]]   # no run id: nothing added, nothing refused


def test_install_checks_prerequisites_then_prints_the_orca_override_without_a_path_shim(run, stubs, tmp_path):
    # spawns are the cost on macOS: a set ORCA_ROOT_PATH spares load_env its `git rev-parse` (the /usr/bin git there is an xcrun shim), STUB_NOENV the stub its `env`
    quick = {"ORCA_ROOT_PATH": tmp_path, "STUB_NOENV": 1}
    r = run(["bash", f"{V2}/scripts/install.sh"], PATH=f"{tmp_path}:/usr/bin:/bin", **quick)
    assert r.returncode != 0 and "missing prerequisite" in r.stderr and "orca" in r.stderr
    stubs.reply("orca", "1.4.246")
    r = run(["bash", f"{V2}/scripts/install.sh"], **quick)
    assert r.returncode == 0, r.stderr
    assert f"{V2}/bin/claude-spoke" in r.stdout and not any((tmp_path / "home").rglob("claude"))


IDENTITY = ["CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR",
            "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_GATEWAY",
            "CLAUDE_CODE_GATEWAY_TOKEN", "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_USE_ANTHROPIC_AWS", "ANTHROPIC_AWS_API_KEY", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_MANTLE",
            "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE", "CLAUDE_CODE_SESSION_ACCESS_TOKEN", "CLAUDE_CODE_SESSIONS_OAUTH_TOKEN", "CLAUDE_CODE_HOST_AUTH_ENV_VAR"]   # inherited variables that say WHOSE login Claude uses
ROUTING = ["ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_API_BASE_URL", "ANTHROPIC_API_HOST", "ANTHROPIC_UNIX_SOCKET", "CLAUDE_CODE_CUSTOM_OAUTH_URL"]   # ... and WHERE it goes: a gateway a user set may stay when the project lists one stored account
PICK_THE_ACCOUNT = IDENTITY + ROUTING


def accounts(tmp_path, listing, logins=("client",)):
    """A project's CLAUDE_ACCOUNTS in the per-machine env file, a throwaway HOME with a shared ~/.claude, and a config directory ~/.claude-<name> holding a login for each name in `logins`."""
    home = tmp_path / "home"
    (home / ".claude/skills").mkdir(parents=True)
    for f in ("settings.json", "CLAUDE.md"):
        (home / ".claude" / f).write_text("{}\n")
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"notion": {"command": "n"}}, "oauthAccount": {}}))
    for name in logins:
        (home / f".claude-{name}").mkdir()
        (home / f".claude-{name}/.claude.json").write_text('{"oauthAccount": {"emailAddress": "x"}}')
    (tmp_path / "l.env").write_text(f"CLAUDE_ACCOUNTS={listing}\n" if listing else "")
    return {"HOME": home, "AI_TOOLKIT_LOCAL_ENV": tmp_path / "l.env"}


@pytest.mark.parametrize("listing", [None, "max"])
def test_an_unset_or_one_entry_account_list_leaves_the_claude_env_and_argv_unchanged(run, stubs, repo, tmp_path, listing):
    tk = synced(repo)
    env = accounts(tmp_path, listing)
    r = run([SPOKE, "--model", "m"], cwd=in_spoke(repo), ANTHROPIC_BASE_URL="https://gateway.test", **env)
    assert r.returncode == 0 and stubs.calls("claude") == [[*MCP(tk), "--settings", WORKER_SETTINGS(tk), "--model", "m"]], r.stderr
    claude_env = stubs.env("claude")
    assert "OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=abc-123,repo=origin\n" in claude_env + "\n"
    assert "ANTHROPIC_BASE_URL=https://gateway.test\n" in claude_env   # a user who routes Claude through a gateway keeps it when the project lists one account or none
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in claude_env and "CLAUDE_CONFIG_DIR" not in claude_env


@pytest.mark.parametrize("listing, identity_inherited, gateway_kept", [("max", False, True), ("max", True, False), ("client", False, False)])
def test_a_gateway_is_kept_only_for_one_stored_account_and_no_inherited_identity(run, stubs, repo, tmp_path, listing, identity_inherited, gateway_kept):
    synced(repo)   # a max-only project must not run on whatever login the caller's environment names (Orca's account switcher, a parent session on the client account), and a gateway URL kept next to a dropped credential would take the stored login's own to that host
    inherited = {**dict.fromkeys(ROUTING, "zz-gateway-9"), **(dict.fromkeys(IDENTITY, "zz-probe-9") if identity_inherited else {})}
    r = run([SPOKE], cwd=in_spoke(repo), **inherited, **accounts(tmp_path, listing))
    claude_env = stubs.env("claude")
    assert r.returncode == 0 and "zz-probe-9" not in claude_env and all((f"{v}=zz-gateway-9\n" in claude_env) == gateway_kept for v in ROUTING), r.stderr


@pytest.mark.parametrize("account", ["max", "client"])
def test_no_account_kind_exports_a_token_and_every_inherited_login_variable_is_dropped(run, stubs, repo, tmp_path, account):
    synced(repo)   # the launcher handles no token at all (a token in claude's own environment is readable by any process of the user): the stored login and a config-directory login both authenticate by themselves
    env = accounts(tmp_path, "client max")
    r = run([SPOKE, "--account", account], cwd=in_spoke(repo), **dict.fromkeys(PICK_THE_ACCOUNT, "zz-probe-9"), **env)
    claude_env = stubs.env("claude")
    assert r.returncode == 0, r.stderr
    assert "zz-probe-9" not in claude_env and "CLAUDE_CODE_OAUTH" not in claude_env   # a key or token in the caller's env outranks every login: the chosen account must be the one used
    assert (f"CLAUDE_CONFIG_DIR={env['HOME']}/.claude-client\n" in claude_env) == (account == "client") and f"account={account}\n" in claude_env + "\n"


def test_a_one_entry_list_naming_a_config_directory_account_runs_every_session_on_it_even_with_the_otel_opt_out(run, stubs, repo, tmp_path):
    synced(repo)   # the data boundary: a client-only project never falls back to the stored login
    env = accounts(tmp_path, "client")
    for cwd in (in_spoke(repo), repo.root):
        assert run([SPOKE], cwd=cwd, AI_TOOLKIT_OTEL=0, **env).returncode == 0
        assert f"CLAUDE_CONFIG_DIR={env['HOME']}/.claude-client\n" in stubs.env("claude") and "account=" not in stubs.env("claude")   # one entry: no failover, so no account tag


def test_a_coordinator_runs_on_the_account_it_names_else_on_the_first_listed(run, stubs, repo, tmp_path):
    tk = synced(repo)
    env = accounts(tmp_path, "client max")
    assert run([SPOKE], cwd=repo.root, **env).returncode == 0
    assert f"CLAUDE_CONFIG_DIR={env['HOME']}/.claude-client\n" in stubs.env("claude") and "account=client" in stubs.env("claude")   # no --account: the first entry
    assert run([SPOKE, "--account", "max"], cwd=repo.root, **env).returncode == 0
    assert "CLAUDE_CONFIG_DIR" not in stubs.env("claude") and "account=max" in stubs.env("claude")
    assert stubs.calls("claude")[-1] == ["--settings", COORD_SETTINGS(tk)]


@pytest.mark.parametrize("listing,args,logins,why", [   # the name shapes are test_lib's; here each check is wired into the launcher
    ("max client", ["--account", "other"], ("client",), "not listed"),            # an account the project does not list is never used
    ("max Client", ["--account=Client"], ("client",), "not an account name"),   # listed, and still refused for its shape
    ("max client", ["--account", "client"], (), "neither the stored login"),   # no ~/.claude-client: refused, no fallback to another account
])
def test_an_unlisted_malformed_or_unusable_account_is_refused(run, stubs, repo, tmp_path, listing, args, logins, why):
    synced(repo)
    r = run([SPOKE, *args], cwd=in_spoke(repo), **accounts(tmp_path, listing, logins))
    assert r.returncode != 0 and why in r.stderr and stubs.calls("claude") == []


def test_a_config_directory_without_a_login_is_refused_with_the_login_command(run, stubs, repo, tmp_path):
    synced(repo)
    env = accounts(tmp_path, "client max")   # the first entry is the default
    (env["HOME"] / ".claude-client/.claude.json").write_text("{}")
    r = run([SPOKE], cwd=repo.root, **env)
    assert r.returncode != 0 and "auth login" in r.stderr and f"{env['HOME']}/.claude-client" in r.stderr and stubs.calls("claude") == []


def test_a_config_directory_account_runs_on_its_own_login_and_shares_the_users_files(run, stubs, repo, tmp_path):
    synced(repo)
    env = accounts(tmp_path, "max client")
    home = env["HOME"]
    (home / ".claude-client/skills").mkdir()   # a real directory already there is kept, never replaced
    (home / ".claude/commands").mkdir()
    (home / "elsewhere").mkdir()
    (home / ".claude-client/commands").symlink_to(home / "elsewhere")   # a link to somewhere else is not the share either: kept, said so
    r = run([SPOKE, "--account", "client"], cwd=repo.root, **env)
    claude_env, argv = stubs.env("claude"), stubs.calls("claude")[0]
    assert r.returncode == 0 and f"CLAUDE_CONFIG_DIR={home}/.claude-client\n" in claude_env and "CLAUDE_CODE_OAUTH_TOKEN" not in claude_env, r.stderr
    for name in ("settings.json", "CLAUDE.md"):
        assert (home / ".claude-client" / name).resolve() == (home / ".claude" / name).resolve() and (home / ".claude-client" / name).is_symlink()
    assert not (home / ".claude-client/skills").is_symlink() and "skills" in r.stderr   # kept, and said so
    assert (home / ".claude-client/commands").resolve() == (home / "elsewhere").resolve() and "commands" in r.stderr and not (home / ".claude/commands/commands").exists()   # nothing is ever written inside ~/.claude
    assert not (home / ".claude-client/agents").exists()   # nothing to share: no dangling link
    mcp = home / ".claude-client/aitk-user-mcp.json"   # the user-scope MCP servers of ~/.claude.json, which a config directory does not hold
    assert argv[argv.index("--mcp-config") + 1] == str(mcp) and json.loads(mcp.read_text()) == {"mcpServers": {"notion": {"command": "n"}}} and mcp.stat().st_mode & 0o777 == 0o600
    assert run([SPOKE, "--account", "client"], cwd=in_spoke(repo), **env).returncode == 0
    assert str(mcp) not in stubs.calls("claude")[1]   # a worker keeps its strict toolkit list


def test_an_account_flag_in_an_unsynced_project_is_refused(run, stubs, repo, tmp_path):
    env = accounts(tmp_path, "max client")
    r = run([SPOKE, "--account", "client"], cwd=repo.wt("plain"), **env)
    assert r.returncode != 0 and "not listed" in r.stderr and stubs.calls("claude") == []
