from conftest import V2

SPOKE = str(V2 / "bin/claude-spoke")


def in_spoke(repo, run_id="abc-123"):
    wt = repo.wt("wp0")
    (wt / ".ai-toolkit").mkdir()
    (wt / ".ai-toolkit/spoke-run-id").write_text(run_id + "\n")
    return wt


def test_spoke_exports_native_otel_env_and_passes_args_through(run, stubs, repo):
    r = run([SPOKE, "--model", "m"], cwd=in_spoke(repo), OTEL_ENDPOINT="http://collector:4317")
    env = stubs.env("claude")
    assert r.returncode == 0 and stubs.calls("claude") == [["--model", "m"]], r.stderr
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in env and "OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317" in env
    assert "OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=abc-123,repo=origin" in env and "OTLP_HEADERS" not in env


def test_spoke_is_a_plain_claude_without_a_run_id_or_when_opted_out(run, stubs, repo):
    assert run([SPOKE, "-p", "hi"], cwd=repo.wt("plain")).returncode == 0
    run([SPOKE], cwd=in_spoke(repo), AI_TOOLKIT_OTEL=0)
    assert stubs.calls("claude") == [["-p", "hi"], []] and "TELEMETRY" not in stubs.env("claude")


def test_install_checks_prerequisites_then_prints_the_orca_override_without_a_path_shim(run, stubs, tmp_path):
    r = run(["bash", f"{V2}/scripts/install.sh"], PATH=f"{tmp_path}:/usr/bin:/bin")
    assert r.returncode != 0 and "missing prerequisite" in r.stderr and "orca" in r.stderr
    stubs.reply("orca", "1.4.219")
    r = run(["bash", f"{V2}/scripts/install.sh"])
    assert r.returncode == 0, r.stderr
    assert f"{V2}/bin/claude-spoke" in r.stdout and not any((tmp_path / "home").rglob("claude"))
