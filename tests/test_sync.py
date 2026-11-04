import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import V2, git


def write(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def src(tmp_path):
    """A fake toolkit checkout: the real sync/lib scripts around a tiny shared/ tree. Each file it holds is copied by every sync (a few processes each), so only the scripts the tests name are in it."""
    root = tmp_path / "src"
    (root / "v2" / "scripts").mkdir(parents=True)
    for name in ("sync.sh", "lib.sh", "coordinator.sh", "otel.sh"):
        shutil.copy(V2 / "scripts" / name, root / "v2" / "scripts" / name)
    shutil.copytree(V2 / "bin", root / "v2" / "bin")
    (root / "v2" / "settings").mkdir(parents=True)
    shutil.copy(V2 / "settings" / "ai-toolkit.env", root / "v2" / "settings")
    write(root / "v2" / "settings" / "claude" / "settings.json", '{"hooks": {}}\n')
    write(root / "v2" / "settings" / "claude" / "mcp.json", '{"mcpServers": {}}\n')
    write(root / "v2" / "ruff.toml", '# toolkit ruff\ntarget-version = "py312"\n')
    write(root / "v2" / "pyrightconfig.json", '{"typeCheckingMode": "standard"}\n')
    write(root / "v2" / "hooks" / "claude" / "guard.sh", "#!/bin/sh\n").chmod(0o755)
    write(root / "v2" / "hooks" / "git" / "commit-msg", "#!/bin/sh\n").chmod(0o755)
    sh = root / "shared"
    write(sh / "rules" / "guidelines.md", '---\ndescription: "g"\n---\n# Guidelines\n')
    write(sh / "rules" / "security.md", "# Security\n")
    write(sh / "rules" / "python-style.md", "---\npaths:\n  - '**/*.py'\n---\n# Py\n")
    write(sh / "rules" / "on-demand" / "workflow.md", "# Workflow\n")
    write(sh / "skills" / "land" / "SKILL.md", "---\nname: land\ndescription: d\n---\n")
    write(sh / "skills" / "land" / "references" / "r.md")
    write(sh / "skills" / "land" / "scripts" / "__pycache__" / "x.pyc")
    write(sh / "skills" / "land" / ".DS_Store")
    write(sh / "skills" / "hub" / "SKILL.md", "---\nname: hub\ndescription: d\n---\n")
    write(sh / "agents" / "code-review.md", "---\nname: code-review\n---\n")
    write(sh / "prompts" / "commit-msg.md", "---\ndescription: d\n---\n")
    git(root / "v2", "init", "-q")
    git(root / "v2", "remote", "add", "origin", "git@github.com:acme/toolkit.git")
    return root


@pytest.fixture
def target(tmp_path):
    t = tmp_path / "target"
    t.mkdir()
    git(t, "init", "-q")
    return t


@pytest.fixture
def sync(src, target, run):   # the layout tests are about files, not history: they never commit (the host_sync fixture below does)
    return lambda *flags, tgt=None: run(["bash", str(src / "v2" / "scripts" / "sync.sh"), str(tgt or target), "--no-commit", *flags])


MSG = "chore(ai-toolkit): sync orca.yaml with the toolkit (#0)"


@pytest.fixture(scope="session")
def host_templates(tmp_path_factory):  # per xdist worker: a host on main with a local bare origin, empty and with the project's first commit pushed; each test gets a copy of one
    base = tmp_path_factory.mktemp("host_templates")
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def sh(*a, cwd):
        subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True)

    for kind in ("empty", "history"):
        d = base / kind
        d.mkdir()
        sh("init", "-q", "--bare", "--template=", "-b", "main", "origin.git", cwd=d)
        sh("init", "-q", "--template=", "-b", "main", "host", cwd=d)
        for a in (["remote", "add", "origin", str(d / "origin.git")], ["config", "user.name", "t"], ["config", "user.email", "t@t"]):
            sh(*a, cwd=d / "host")
    write(base / "history" / "host" / "README.md", "hi\n")
    for a in (["add", "README.md"], ["commit", "-qm", "chore: init (#1)"], ["push", "-q", "origin", "main"]):
        sh(*a, cwd=base / "history" / "host")
    return base


@pytest.fixture
def host(tmp_path, host_templates, request):
    """A host on its base branch with a local bare repository as its origin: (host dir, origin dir). With the project's first commit pushed, unless the test asks for "empty" (indirect parameter)."""
    kind = getattr(request, "param", "history")
    origin, h = tmp_path / "origin.git", tmp_path / "host"
    shutil.copytree(host_templates / kind / "origin.git", origin)
    shutil.copytree(host_templates / kind / "host", h)   # copies, not git processes: the setup is a handful of file copies
    config = h / ".git" / "config"
    config.write_text(re.sub(r"(?m)^(\s*url = ).*$", lambda m: m[1] + str(origin), config.read_text()))
    return h, origin


@pytest.fixture
def host_sync(src, host, run):   # the committing run, from the toolkit's main checkout
    return lambda *flags, tgt=None, script=None, **env: run(["bash", str(script or src / "v2" / "scripts" / "sync.sh"), str(tgt or host[0]), *flags], **env)


def history(repo, ref="HEAD"):
    r = subprocess.run(["git", "-C", str(repo), "log", "--format=%s", ref], capture_output=True, text=True)
    return r.stdout.splitlines() if r.returncode == 0 else []


def tree(t):
    return {str(p.relative_to(t)): (p.read_bytes(), p.stat().st_mtime_ns) for p in t.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(t).parts}


def manifest(t):
    return (t / ".ai-toolkit" / "sync-manifest").read_text().splitlines()


def exclude(t):
    return (t / ".git" / "info" / "exclude").read_text()


def test_layout_in_target(sync, target):
    r = sync()
    assert r.returncode == 0, r.stderr
    # every rule, the guidelines too, loads from its own subfolder (no paths: = always on, paths: = conditional); the project's CLAUDE.md is never touched
    assert sorted(p.name for p in (target / ".claude" / "rules").iterdir()) == ["ai-toolkit"]
    assert sorted(p.name for p in (target / ".claude" / "rules" / "ai-toolkit").iterdir()) == ["guidelines.md", "python-style.md", "security.md"]
    assert (target / ".claude" / "rules" / "ai-toolkit" / "guidelines.md").read_text() == '---\ndescription: "g"\n---\n# Guidelines\n'
    assert not (target / "CLAUDE.md").exists() and not (target / "CLAUDE.md.bak").exists()
    assert (target / ".ai-toolkit" / "rules" / "workflow.md").is_file()  # on-demand: never auto-loaded
    assert (target / ".claude" / "skills" / "land" / "references" / "r.md").is_file()
    assert (target / ".claude" / "agents" / "code-review.md").is_file()
    assert (target / ".claude" / "commands" / "commit-msg.md").is_file()
    assert (target / ".ai-toolkit" / "claude-settings.json").read_text() == '{"hooks": {}}\n'   # the toolkit's own registrations; the project's .claude/settings.json is never written
    assert (target / ".ai-toolkit" / "mcp.json").read_text() == '{"mcpServers": {}}\n'   # the workers' server list (bin/claude-spoke passes it with --mcp-config)
    assert not (target / ".claude" / "settings.json").exists()
    for p in ("scripts/sync.sh", "scripts/lib.sh", "bin/claude-spoke", "hooks/git/commit-msg", "ai-toolkit.env"):
        assert (target / ".ai-toolkit" / p).is_file(), p
    # the hook scripts are the toolkit's, not the project's .claude/: a worker runs the main checkout's copy; modes survive the copy
    assert os.access(target / ".ai-toolkit" / "hooks" / "claude" / "guard.sh", os.X_OK)
    assert os.access(target / ".ai-toolkit" / "hooks" / "git" / "commit-msg", os.X_OK)
    assert not (target / ".claude" / "hooks").exists()
    assert not list(target.rglob("__pycache__")) and not list(target.rglob(".DS_Store"))
    assert not (target / ".claude" / "prompts").exists() and not (target / ".cursor").exists()


def test_orca_yaml_runs_setup_from_root_path(sync, target):
    sync()
    y = (target / "orca.yaml").read_text()
    assert "setupAgentStartupPolicy: wait-for-setup" in y
    assert 'setup: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh"' in y
    assert 'archive: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/archive.sh"' in y


def test_manifest_is_sorted_relative_and_excludes_itself(sync, target):
    sync()
    m = manifest(target)
    assert m == sorted(set(m))
    assert {".claude/rules/ai-toolkit/guidelines.md", "orca.yaml", ".ai-toolkit/claude-settings.json", ".claude/skills/land/SKILL.md"} <= set(m) and "CLAUDE.md" not in m
    assert ".ai-toolkit/sync-manifest" not in m and all(not p.startswith("/") for p in m)


def test_second_run_changes_nothing(sync, target):
    sync()
    before = tree(target)
    assert sync().returncode == 0
    assert tree(target) == before  # same bytes AND no rewrite (mtime) churn


def test_edit_in_source_reaches_target_by_rename_so_a_running_script_is_not_corrupted(sync, src, target):
    sync()
    write(src / "shared" / "skills" / "land" / "SKILL.md", "new\n")
    source = src / "v2" / "scripts" / "coordinator.sh"
    installed = target / ".ai-toolkit" / "scripts" / "coordinator.sh"
    old, inode = installed.read_text(), installed.stat().st_ino
    with installed.open() as running:   # a running bash holds its script open and reads it by offset
        source.write_text("#!/bin/sh\nnew\n")
        sync()
        assert running.read() == old   # the old inode is untouched: an in-place write would have changed what the loop reads next
    assert installed.read_text() == "#!/bin/sh\nnew\n" and installed.stat().st_ino != inode
    assert (target / ".claude" / "skills" / "land" / "SKILL.md").read_text() == "new\n"
    assert not [p for p in target.rglob("*.new.*")]


def test_a_rewrite_keeps_the_destinations_mode_and_writes_through_a_symlink(sync, src, target, tmp_path):
    sync()
    rule = target / ".claude" / "rules" / "ai-toolkit" / "guidelines.md"
    rule.chmod(0o640)   # the source is a 0600 temp file: the existing mode must survive an update
    real = write(tmp_path / "elsewhere.json", "{}\n")
    (target / ".ai-toolkit" / "claude-settings.json").unlink()
    (target / ".ai-toolkit" / "claude-settings.json").symlink_to(real)
    write(src / "shared" / "rules" / "guidelines.md", "---\ndescription: g\n---\n# Changed\n")
    write(src / "v2" / "settings" / "claude" / "settings.json", '{"hooks": {"x": 1}}\n')
    sync()
    assert rule.read_text() == "---\ndescription: g\n---\n# Changed\n" and rule.stat().st_mode & 0o777 == 0o640
    assert (target / ".ai-toolkit" / "claude-settings.json").is_symlink() and real.read_text() == '{"hooks": {"x": 1}}\n'


def test_gc_removes_what_a_previous_sync_wrote_and_nothing_else(sync, src, target):
    sync()
    mine = write(target / ".claude" / "skills" / "hub" / "mine.md", "user file")
    other = write(target / ".claude" / "rules" / "mine.md", "user rule")
    shutil.rmtree(src / "shared" / "skills" / "land")
    (src / "shared" / "rules" / "security.md").unlink()
    assert sync().returncode == 0
    assert not (target / ".claude" / "skills" / "land").exists()  # file gone, empty dirs pruned
    assert not (target / ".claude" / "rules" / "security.md").exists()
    assert mine.read_text() == "user file" and other.read_text() == "user rule"
    assert (target / ".claude" / "skills" / "hub" / "SKILL.md").is_file()


def test_a_sync_never_writes_or_removes_the_hosts_own_mcp_list(sync, src, target):
    mine = write(target / ".ai-toolkit" / "mcp.local.json", '{"mcpServers": {"mine": {}}}\n')
    write(src / "v2" / "settings" / "claude" / "mcp.json", '{"mcpServers": {"x": {"command": "x"}}}\n')
    assert sync().returncode == 0 and sync().returncode == 0
    assert mine.read_text() == '{"mcpServers": {"mine": {}}}\n' and "x" in (target / ".ai-toolkit" / "mcp.json").read_text()


def test_gc_ignores_manifest_entries_that_escape_the_target(sync, target, tmp_path):
    sync()
    victim = write(tmp_path / "victim.txt", "keep")
    with (target / ".ai-toolkit" / "sync-manifest").open("a") as f:
        f.write("../victim.txt\n/etc/hosts\n")
    sync()
    assert victim.read_text() == "keep"


def test_every_sync_excludes_what_it_wrote_under_claude_and_only_local_only_also_orca_yaml(sync, target):
    write(target / ".git" / "info" / "exclude", "mine\n")
    sync("--local-only")
    sync("--local-only")
    ex = exclude(target)
    assert ex.count("# >>> ai-toolkit sync") == 1 and ex.startswith("mine\n")
    assert {"/.ai-toolkit/", "/.claude/skills/land/SKILL.md", "/.claude/rules/ai-toolkit/security.md", "/orca.yaml"} <= set(ex.splitlines())
    sync()
    lines = exclude(target).splitlines()   # a normal run keeps the .claude/ paths it wrote, one by one (never the whole folder), and drops orca.yaml
    assert {"/.ai-toolkit/", "/.claude/skills/land/SKILL.md", "mine"} <= set(lines) and not {"/.claude/", "/CLAUDE.md", "/orca.yaml"} & set(lines)


def test_local_env_override_is_never_tracked(sync, target):
    sync()
    assert git(target, "status", "--porcelain", "-uall").splitlines() == ["?? orca.yaml"]   # .claude/ and .ai-toolkit/ stay out of git; only orca.yaml is the project's to commit
    write(target / ".ai-toolkit" / "ai-toolkit.local.env", "BASE_BRANCH=dev\n")
    assert "ai-toolkit.local.env" not in git(target, "status", "--porcelain", "-uall")


def test_owner_repo_reads_every_remote_form_and_refuses_what_is_no_repo(src, run):
    forms = ["git@github.com:acme/toolkit.git", "https://github.com/acme/toolkit.git", "https://github.com/acme/toolkit",
             "ssh://git@github.com/acme/toolkit.git", "https://github.com/acme/toolkit/", "../a/b", "/srv/toolkit", ""]
    probe = f'. "{src}/v2/scripts/sync.sh"; for u in "$@"; do owner_repo "$u" || printf -; echo; done'
    r = run(["bash", "-c", probe, "_", *forms])
    assert r.stdout.splitlines() == ["acme/toolkit"] * 5 + ["-"] * 3  # one bash for every form: no full sync per URL


def test_a_host_records_the_toolkits_origin_as_upstream_and_loses_it_with_the_remote(sync, src, target, run):
    def value():
        return [ln for ln in (target / ".ai-toolkit" / "ai-toolkit.env").read_text().splitlines() if ln.startswith("UPSTREAM_REPO=")]
    assert sync().returncode == 0 and value() == ["UPSTREAM_REPO=acme/toolkit"]
    assert (target / ".ai-toolkit" / "ai-toolkit.env").read_text().splitlines().count(f"TOOLKIT_DIR={(src / 'v2').resolve()}") == 1   # where a host's loop finds the newer toolkit
    write(target / ".ai-toolkit" / "ai-toolkit.local.env", "UPSTREAM_REPO=me/fork\n")  # the per-target override still wins
    probe = f'. "{target}/.ai-toolkit/scripts/lib.sh"; ORCA_ROOT_PATH="{target}" load_env; echo "$UPSTREAM_REPO"'
    assert run(["bash", "-c", probe]).stdout.strip() == "me/fork"
    git(src / "v2", "remote", "remove", "origin")   # the toolkit syncing into itself records no upstream and its own path as TOOLKIT_DIR: pinned in the test of that sync below
    r = sync()
    assert r.returncode == 0 and r.stderr.count("UPSTREAM_REPO") == 1 and value() == ["UPSTREAM_REPO="]


def test_a_toolkit_that_moves_during_the_copy_leaves_the_stamp_stale_so_the_next_sync_copies_again(sync, src, target, run, tmp_path):
    v2, stamp = src / "v2", target / ".ai-toolkit" / "toolkit-version"
    git(v2, "add", "-A")
    git(v2, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    write(stamp, "old\n")
    real = shutil.which("git")
    shim = write(tmp_path / "shim" / "git", f'#!/bin/bash\n[[ "$*" == *--short=12* ]] && [ -e "$TMP_SEEN" ] && {{ echo ffffffffffff; exit 0; }}\n[[ "$*" == *--short=12* ]] && touch "$TMP_SEEN"\nexec {real} "$@"\n')   # the second read of HEAD names another commit
    shim.chmod(0o755)
    r = run(["bash", str(v2 / "scripts" / "sync.sh"), str(target), "--no-commit"], PATH=f"{shim.parent}:{os.environ['PATH']}", TMP_SEEN=tmp_path / "seen")
    assert r.returncode == 0 and stamp.read_text() == "old\n" and "moved during the copy" in r.stderr


@pytest.mark.parametrize("state", ["clean", "dirty", "not-git"])
def test_the_toolkit_commit_is_stamped_so_telemetry_can_name_the_version_a_host_runs(sync, src, target, state):
    v2 = src / "v2"
    stamp = target / ".ai-toolkit" / "toolkit-version"
    if state == "not-git":
        shutil.rmtree(v2 / ".git")
    else:
        git(v2, "add", "-A")
        git(v2, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    if state == "dirty":
        write(v2 / "scripts" / "extra.sh")
    sha = "" if state == "not-git" else git(v2, "rev-parse", "--short=12", "HEAD")
    assert sync().returncode == 0
    if state == "not-git":
        assert not stamp.exists() and ".ai-toolkit/toolkit-version" not in manifest(target)
    else:
        assert stamp.read_text() == sha + ("-dirty" if state == "dirty" else "") + "\n" and ".ai-toolkit/toolkit-version" in manifest(target)


def test_an_unmanaged_orca_yaml_is_backed_up_once_then_owned(sync, target):
    write(target / "orca.yaml", "scripts: {}\n")
    sync()
    assert "orca.yaml.bak" not in git(target, "status", "--porcelain", "-uall")   # the project's own copy is kept, out of git status
    assert (target / "orca.yaml.bak").read_text() == "scripts: {}\n" and (target / "orca.yaml").read_text().startswith("setupAgentStartupPolicy")
    (target / "orca.yaml").write_text("edited after sync\n")
    sync()  # now managed: overwritten again, the original backup is kept
    assert (target / "orca.yaml.bak").read_text() == "scripts: {}\n" and (target / "orca.yaml").read_text().startswith("setupAgentStartupPolicy")


def test_missing_settings_json_warns_but_syncs(sync, src, target):
    (src / "v2" / "settings" / "claude" / "settings.json").unlink()
    r = sync()
    assert r.returncode == 0 and "settings.json" in r.stderr
    assert not (target / ".ai-toolkit" / "claude-settings.json").exists() and (target / "orca.yaml").exists()


def test_refuses_a_target_that_is_not_a_git_repo(sync, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = sync(tgt=plain)
    assert r.returncode != 0 and not (plain / ".claude").exists()


def test_rejects_unknown_option(sync):
    assert sync("--bogus").returncode != 0


def v1_target(target):
    """A target a v1 sync filled: manifest.json lists Cursor/Copilot/Claude outputs."""
    listed = [".cursor/rules/a.mdc", ".github/agents/x.md", ".claude/hooks/scripts/old.sh", "../victim.txt"]
    for rel in listed[:3]:
        write(target / rel)
    write(target / ".cursor" / "mine.json", "user")
    write(target / ".ai-toolkit-manifest.json", json.dumps({"toolkit_rev": "x", "tools": {
        "cursor": listed[:1], "copilot": listed[1:2], "claude": listed[2:]}}))


def test_v1_files_are_kept_unless_migrating_and_a_hint_is_printed(sync, target):
    v1_target(target)
    r = sync()
    assert (target / ".cursor" / "rules" / "a.mdc").exists() and "--migrate-v1" in r.stderr


def test_migrate_v1_removes_what_the_v1_manifest_listed_and_nothing_else(sync, target, tmp_path):
    v1_target(target)
    victim = write(tmp_path / "victim.txt", "keep")
    assert sync("--migrate-v1").returncode == 0
    assert not (target / ".cursor" / "rules").exists() and not (target / ".github").exists()
    assert not (target / ".claude" / "hooks" / "scripts").exists() and not (target / ".ai-toolkit-manifest.json").exists()
    assert (target / ".ai-toolkit" / "hooks" / "claude" / "guard.sh").is_file()  # the v2 hook outlives the GC of v1 hooks
    assert (target / ".cursor" / "mine.json").read_text() == "user" and victim.read_text() == "keep"
    assert (target / ".ai-toolkit" / "claude-settings.json").is_file()  # the v2 output survives the GC


def test_otel_sh_stays_in_the_toolkit_checkout(sync, src, target):
    assert (src / "v2" / "scripts" / "otel.sh").is_file()  # the fixture copies the real script
    sync()
    assert not (target / ".ai-toolkit" / "scripts" / "otel.sh").exists() and "otel.sh" not in "".join(manifest(target))
    stale = write(target / ".ai-toolkit" / "scripts" / "otel.sh")  # shipped by an earlier sync
    with (target / ".ai-toolkit" / "sync-manifest").open("a") as f:
        f.write(".ai-toolkit/scripts/otel.sh\n")
    sync()
    assert not stale.exists()


def test_a_hosts_own_files_are_never_touched(sync, target):
    own = {"CLAUDE.md": "the project's\n", ".claude/rules/security.md": "its own security\n", ".claude/agents/code-review.md": "its own agent\n",
           ".claude/settings.json": '{"hooks": {"mine": 1}}\n', ".claude/hooks/mine.sh": "#!/bin/sh\n"}
    for rel, text in own.items():
        write(target / rel, text)
    git(target, "add", "-f", ".claude/agents/code-review.md", ".claude/settings.json")   # .claude/ files the project tracks itself
    r = sync()
    assert r.returncode == 0 and ".claude/agents/code-review.md is tracked" in r.stderr
    assert {rel: (target / rel).read_text() for rel in own} == own and not list(target.rglob("*.bak"))   # byte for byte, no backup of what the sync never writes
    assert ".claude/agents/code-review.md" not in manifest(target) and "/.claude/agents/code-review.md" not in exclude(target).splitlines()
    assert (target / ".claude" / "rules" / "ai-toolkit" / "security.md").is_file() and (target / ".ai-toolkit" / "claude-settings.json").is_file()


@pytest.mark.parametrize("state", ["written", "with-bak", "not-the-syncs", "tracked", "toolkit-itself"])
def test_a_host_synced_the_old_way_is_cleaned_up_from_its_manifest_only(sync, src, target, state):
    target = src / "v2" if state == "toolkit-itself" else target   # the toolkit syncing itself: its .bak files are a v1 sync's output, never the project's own
    sync(tgt=target)
    own = ["CLAUDE.md", ".claude/settings.json"]   # the project's files the old sync took over (each kept as <file>.bak when it existed)
    old = {".claude/rules/security.md": "old flat\n", ".claude/hooks/guard.sh": "old hook\n"}   # toolkit files the old layout wrote
    for rel in own:
        write(target / rel, "mine\n" if state == "not-the-syncs" else "generated\n")
        if state in ("with-bak", "toolkit-itself"):
            write(target / f"{rel}.bak", "the project's\n")
    for rel, text in old.items():
        write(target / rel, text)
    with (target / ".ai-toolkit" / "sync-manifest").open("a") as f:   # what the old sync recorded
        f.write("".join(f"{rel}\n" for rel in [*old, *([] if state == "not-the-syncs" else own)]))
    if state == "tracked":
        git(target, "add", "-f", *own, *old, ".claude/skills/land/SKILL.md")   # the last one is a toolkit file the project committed
    r = sync(tgt=target)
    assert r.returncode == 0 and bool(list(target.rglob("*.bak"))) == (state == "toolkit-itself")   # a .bak is restored over its file, never left behind (but kept in the toolkit)
    if state == "tracked":   # the host's history is the human's: reported, never rewritten, no deletion committed
        assert all((target / rel).read_text() == "generated\n" and f"{rel} is tracked" in r.stderr for rel in own)
        assert all((target / rel).exists() and f"{rel} is tracked" in r.stderr for rel in old) and "CLAUDE.md.bak" in r.stderr
        assert r.stderr.count("land/SKILL.md is tracked") == 1   # one report per file, not one from the write and one from the clean-up
        return
    assert not any((target / rel).exists() for rel in old)
    want = {"written": None, "with-bak": "the project's\n", "not-the-syncs": "mine\n", "toolkit-itself": None}[state]
    assert [(target / rel).read_text() if (target / rel).exists() else None for rel in own] == [want, want]


@pytest.mark.parametrize("host, with_history", [("empty", False), ("history", True), ("history", "no-commit-leftover")], indirect=["host"], ids=["empty-host", "host-with-history", "no-commit-leftover"])
def test_a_host_sync_commits_and_pushes_orca_yaml_once_then_is_idempotent(host_sync, host, src, with_history):
    h, origin = host
    before = history(h)
    if with_history == "no-commit-leftover":   # a --no-commit run leaves orca.yaml changed with the sync's own bytes: the next run commits it, it is no human's edit
        write(h / "orca.yaml", "scripts: {}\n")
        git(h, "add", "orca.yaml")
        git(h, "commit", "-qm", "chore: own orca.yaml (#5)")
        git(h, "push", "-q", "origin", "main")
        before = history(h)
        assert host_sync("--no-commit").returncode == 0 and git(h, "status", "--porcelain").strip() == "M orca.yaml" and history(h) == before
        assert host_sync().returncode == 0 and history(h) == [MSG, *before] and history(origin, "main") == history(h)
        return
    if with_history:
        git(h, "config", "core.hooksPath", str(V2 / "hooks" / "git"))   # the real commit-msg and pre-commit: the message conforms and the script's own commit passes the base-branch bar
        write(h / "wip.txt", "staged by a human\n")
        git(h, "add", "wip.txt")
    r = host_sync()
    assert r.returncode == 0, r.stderr
    assert history(h) == [MSG, *before] and history(origin, "main") == history(h)   # one commit, pushed to the base branch
    assert git(h, "show", "--format=", "--name-only", "HEAD").splitlines() == ["orca.yaml"] and git(h, "rev-parse", "HEAD") == git(origin, "rev-parse", "main")
    assert git(h, "status", "--porcelain").splitlines() == (["A  wip.txt"] if with_history else [])   # clean for the toolkit's paths, a human's staged file untouched
    assert (h / ".claude" / "rules" / "ai-toolkit" / "guidelines.md").exists() and not (h / "CLAUDE.md").exists()
    if not with_history:
        assert host_sync().returncode == 0 and history(h) == [MSG, *before]   # from scratch and again: the second run changes nothing, makes no commit and no push
        return
    write(src / "shared" / "rules" / "security.md", "# Security, changed\n")   # a rule change lives in the untracked .claude/: alone it commits nothing
    script = src / "v2" / "scripts" / "sync.sh"
    script.write_text(script.read_text().replace("wait-for-setup", "wait-for-setup-v2"))   # the toolkit changes orca.yaml
    assert host_sync().returncode == 0 and history(h) == [MSG, MSG, *before] and history(origin, "main") == history(h)   # exactly one new commit, pushed
    assert git(h, "show", "--format=", "--name-only", "HEAD").splitlines() == ["orca.yaml"] and "changed" in (h / ".claude/rules/ai-toolkit/security.md").read_text()


@pytest.mark.parametrize("case", ["unreachable-remote", "a-human-commit-waits", "a-commit-with-the-sync-subject-and-other-content", "an-evil-merge-with-the-sync-subject", "orca-yaml-is-ignored"])
def test_a_refused_push_keeps_the_commit_and_the_next_run_pushes_it(host_sync, host, case, tmp_path):
    h, origin = host
    if case == "unreachable-remote":
        git(h, "config", "remote.origin.url", str(tmp_path / "gone.git"))
        r = host_sync()
        assert r.returncode == 0 and "committed locally" in r.stderr and "push origin HEAD:main" in r.stderr   # says what the human has to do
        assert history(h)[0] == MSG and MSG not in history(origin, "main")   # the commit is kept, not published
        git(h, "config", "remote.origin.url", str(origin))
        before = history(h)
        assert host_sync().returncode == 0 and history(h) == before and history(origin, "main") == before   # the next run pushes the waiting commit, makes none
    elif case == "orca-yaml-is-ignored":   # the tracked file Orca reads would never exist: said, not silent
        write(h / ".gitignore", "orca.yaml\n")
        git(h, "add", ".gitignore")
        git(h, "commit", "-qm", "chore: ignore (#6)")
        git(h, "push", "-q", "origin", "main")
        before = history(h)
        r = host_sync()
        assert r.returncode == 0 and "ignored by git" in r.stderr and history(h) == before and history(origin, "main") == before
    elif case == "an-evil-merge-with-the-sync-subject":   # git log --name-only shows no files for a merge: the gate looks at the net tree change instead
        git(h, "checkout", "-q", "-b", "side")
        git(h, "commit", "-q", "--allow-empty", "-m", "feat: side (#7)")
        git(h, "push", "-q", "origin", "side:main")
        git(h, "checkout", "-q", "main")
        git(h, "merge", "-q", "--no-ff", "--no-commit", "side")
        write(h / "evil.sh", "x\n")
        git(h, "add", "evil.sh")
        git(h, "commit", "-q", "-m", MSG)
        pushed = history(origin, "main")
        r = host_sync()
        assert r.returncode == 0 and "waiting on main" in r.stderr and history(origin, "main") == pushed and MSG in history(h)
    else:   # someone's unpushed work is never published by a sync, whatever subject it carries
        subject = "feat: mine (#2)" if case == "a-human-commit-waits" else MSG
        write(h / "mine.txt", "x\n")
        git(h, "add", "mine.txt")
        git(h, "commit", "-qm", subject)
        r = host_sync()
        assert r.returncode == 0 and "waiting on main" in r.stderr and history(h)[:2] == [MSG, subject]
        assert history(origin, "main") == ["chore: init (#1)"]


@pytest.mark.parametrize("case,message", [
    ("wrong-branch", "base branch"), ("behind-upstream", "origin/main"), ("humans-edit-to-orca-yaml", "orca.yaml"),
    ("linked-worktree-target", "linked worktree"), ("toolkit-copy-in-a-linked-worktree", "linked worktree"), ("linked-worktree-despite-GIT_DIR", "linked worktree")])
def test_a_host_sync_refuses_before_it_writes_or_commits_anything(host_sync, host, src, case, message, tmp_path):
    h, origin = host
    target, script = h, None
    if case == "wrong-branch":
        git(h, "checkout", "-q", "-b", "dev")
    elif case == "behind-upstream":
        other = tmp_path / "other"
        git(tmp_path, "clone", "-q", str(origin), str(other))
        git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "feat: elsewhere (#3)")
        git(other, "push", "-q", "origin", "main")
    elif case == "humans-edit-to-orca-yaml":
        assert host_sync().returncode == 0   # installed and committed
        write(h / "orca.yaml", "scripts: {}\n")
    elif case in ("linked-worktree-target", "linked-worktree-despite-GIT_DIR"):   # an environment variable must not turn a linked worktree into a main checkout
        target = tmp_path / "wt"
        git(h, "worktree", "add", "-q", "-b", "feat", str(target))
    else:
        git(src / "v2", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "chore: toolkit (#4)")
        git(src / "v2", "worktree", "add", "-q", "-b", "work", str(tmp_path / "toolkit-wt"))
        shutil.copytree(src / "v2" / "settings", tmp_path / "toolkit-wt" / "settings", dirs_exist_ok=True)   # the worktree has what sync.sh reads next to it
        script = tmp_path / "toolkit-wt" / "scripts" / "sync.sh"
        shutil.copytree(src / "v2" / "scripts", script.parent, dirs_exist_ok=True)
    count, pushed = len(history(target)), history(origin, "main")
    r = host_sync(tgt=target, script=script, **({"GIT_DIR": h / ".git"} if "GIT_DIR" in case else {}))
    assert r.returncode != 0 and message in r.stderr, r.stderr
    assert len(history(target)) == count and history(origin, "main") == pushed   # no commit, no push
    if case == "humans-edit-to-orca-yaml":
        assert (h / "orca.yaml").read_text() == "scripts: {}\n"   # kept, not swept into a commit
    else:
        assert not (target / ".ai-toolkit").exists() and not (target / "orca.yaml").exists()   # refused before anything was written


@pytest.mark.parametrize("how", ["--no-commit", "--local-only", "the-toolkit-itself", "the-toolkit-through-a-symlink"])
def test_no_commit_local_only_and_the_toolkit_itself_commit_nothing(host_sync, host, src, how, tmp_path):
    h, origin = host
    git(h, "checkout", "-q", "-b", "dev")   # off the base branch: a committing run would refuse, so these runs prove they never try
    git(src / "v2", "symbolic-ref", "HEAD", "refs/heads/trunk")   # the toolkit is not on BASE_BRANCH either
    (tmp_path / "link").symlink_to(src / "v2")
    before = (history(h), history(origin, "main"))
    r = host_sync(tgt=tmp_path / "link" if how.endswith("symlink") else src / "v2") if how.startswith("the-toolkit") else host_sync(how)
    assert r.returncode == 0, r.stderr
    assert (history(h), history(origin, "main")) == before and history(src / "v2") == []   # no commit anywhere, no push
    assert (h / "orca.yaml").exists() == (not how.startswith("the-toolkit"))   # orca.yaml is written (no-commit) or written and excluded (local-only), never committed


# --- base lint and typecheck config: proposed to a Python host that has none, never overwritten ---
CONFIGS = ("ruff.toml", "pyrightconfig.json")
OWN = {
    "ruff.toml": ({"ruff.toml": "line-length = 79\n"}, {"pyrightconfig.json"}),
    ".ruff.toml": ({".ruff.toml": "line-length = 79\n"}, {"pyrightconfig.json"}),
    "tool.ruff": ({"pyproject.toml": "[project]\nname = 'x'\n[tool.ruff.lint]\nselect = ['E']\n"}, {"pyrightconfig.json"}),
    "pyrightconfig": ({"pyrightconfig.json": "{}\n"}, {"ruff.toml"}),
    "tool.pyright": ({"pyproject.toml": "[tool.pyright]\nstrict = ['.']\n"}, {"ruff.toml"}),
    "tool.mypy": ({"pyproject.toml": "[tool.mypy]\nstrict = true\n"}, {"ruff.toml"}),
    "mypy.ini": ({"mypy.ini": "[mypy]\nstrict = True\n"}, {"ruff.toml"}),
    ".mypy.ini": ({".mypy.ini": "[mypy]\nstrict = True\n"}, {"ruff.toml"}),
    "setup.cfg": ({"setup.cfg": "[metadata]\nname = x\n[mypy]\nstrict = True\n"}, {"ruff.toml"}),
    "pyproject-without-tools": ({"pyproject.toml": "[project]\nname = 'x'\n"}, {"ruff.toml", "pyrightconfig.json"}),
    "setup.cfg-without-mypy": ({"setup.cfg": "[metadata]\nname = x\n"}, {"ruff.toml", "pyrightconfig.json"}),
    "both": ({"ruff.toml": "a\n", "mypy.ini": "[mypy]\n"}, set()),
}


def python_host(t, **own):
    write(t / "pkg" / "app.py", "x = 1\n")
    for rel, text in own.items():
        write(t / rel, text)
    git(t, "add", ".")


def test_the_base_configs_are_proposed_once_to_a_python_host_that_has_none(sync, src, target):
    python_host(target)
    r = sync()
    assert r.returncode == 0, r.stderr
    for c in CONFIGS:
        assert (target / c).read_bytes() == (src / "v2" / c).read_bytes() and f"proposed {c}" in r.stdout
        assert c not in manifest(target) and f"/{c}" not in exclude(target).splitlines()   # the project's from now on: to commit, never updated or dropped by a sync
    assert not list(target.rglob("*.bak")) and not list(target.rglob("*.new.*"))


@pytest.mark.parametrize("case", OWN)
def test_a_hosts_own_lint_or_typecheck_config_is_kept_byte_for_byte_and_only_the_missing_half_is_proposed(sync, src, target, case):
    own, proposed = OWN[case]
    python_host(target, **own)
    r = sync()
    assert r.returncode == 0, r.stderr
    assert all((target / rel).read_text() == t for rel, t in own.items())
    assert {c for c in CONFIGS if (target / c).is_file() and c not in own} == proposed
    assert all((target / c).read_bytes() == (src / "v2" / c).read_bytes() for c in proposed)
    assert not list(target.rglob("*.bak")) and not list(target.rglob("*.new.*"))


def test_a_second_sync_never_rewrites_a_proposed_file_the_project_edited(sync, src, target):
    python_host(target)
    sync()
    write(target / "ruff.toml", "line-length = 79\n")
    (src / "v2" / "ruff.toml").write_text("# toolkit ruff, changed\n")
    (src / "v2" / "pyrightconfig.json").write_text("{}\n")
    before = tree(target)
    r = sync()
    assert r.returncode == 0 and "proposed" not in r.stdout
    assert {k: v for k, v in tree(target).items() if k in CONFIGS} == {k: v for k, v in before.items() if k in CONFIGS}
    assert (target / "ruff.toml").read_text() == "line-length = 79\n" and not list(target.rglob("*.bak"))


@pytest.mark.parametrize("files", [{}, {"README.md": "x\n", "pyproject.toml": "[project]\nname = 'x'\n"}])
def test_a_host_with_no_tracked_python_is_proposed_nothing(sync, target, files):
    for rel, text in files.items():
        write(target / rel, text)
    write(target / "untracked.py", "x = 1\n")   # tracked is what counts
    if files:
        git(target, "add", *files)
    r = sync()
    assert r.returncode == 0 and not any((target / c).exists() for c in CONFIGS) and "proposed" not in r.stdout


def test_the_toolkit_syncing_into_itself_writes_nothing_of_the_base_configs(sync, src):
    toolkit = src / "v2"
    write(toolkit / "scripts" / "helper.py", "x = 1\n")
    git(toolkit, "add", "scripts/helper.py")
    before = {c: (toolkit / c).read_bytes() for c in CONFIGS}
    r = sync(tgt=toolkit)
    assert r.returncode == 0 and {c: (toolkit / c).read_bytes() for c in CONFIGS} == before
    assert not list(toolkit.rglob("*.bak")) and "proposed" not in r.stdout
    assert (toolkit / ".ai-toolkit" / "ai-toolkit.env").read_text() == (toolkit / "settings" / "ai-toolkit.env").read_text().replace("TOOLKIT_DIR=", f"TOOLKIT_DIR={toolkit.resolve()}")   # no upstream recorded though the toolkit has an origin, and its own path as TOOLKIT_DIR


def test_the_base_pyrightconfig_keeps_pyrights_default_excludes():
    """Setting `exclude` drops pyright's built-in ones, so a whole-project run would check .venv and node_modules."""
    defaults = {"**/.*", "**/node_modules", "**/__pycache__"}
    config = json.loads((V2 / "pyrightconfig.json").read_text())
    assert defaults <= set(config.get("exclude", defaults))
