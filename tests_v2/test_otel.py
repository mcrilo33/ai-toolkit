import os
from urllib.parse import urlparse

import pytest
import yaml
from conftest import STUB, V2

OTEL = str(V2 / "scripts/otel.sh")
COLLECTOR = yaml.safe_load((V2 / "langfuse/otelcol.yaml").read_text())
COMPOSE = yaml.safe_load((V2 / "langfuse/compose.yaml").read_text())


@pytest.fixture
def docker(stubs, tmp_path, monkeypatch):
    (tmp_path / "stubs/bin/docker").write_text(STUB)
    (tmp_path / "stubs/bin/docker").chmod(0o755)
    (tmp_path / "local.env").write_text("LANGFUSE_PUBLIC_KEY=pk-lf-1\nLANGFUSE_SECRET_KEY=sk-lf-2\n")
    monkeypatch.setenv("AI_TOOLKIT_LOCAL_ENV", str(tmp_path / "local.env"))
    monkeypatch.setenv("AITK_OTEL_RAW_DIR", str(tmp_path / "raw"))
    return stubs


def test_up_starts_the_compose_stack_with_auth_in_the_child_env_only(run, docker, tmp_path):
    r = run([OTEL, "up"])
    (argv,) = docker.calls("docker")
    assert r.returncode == 0, r.stderr
    assert argv[:4] == ["compose", "-p", "ai-toolkit-otel", "-f"] and os.path.normpath(argv[4]) == str(V2 / "langfuse/compose.yaml") and argv[5:] == ["up", "-d"]
    env = docker.env("docker")
    assert "LANGFUSE_BASIC_AUTH=Basic cGstbGYtMTpzay1sZi0y" in env  # base64 of pk-lf-1:sk-lf-2
    assert "LANGFUSE_OTLP_ENDPOINT=http://host.docker.internal:3000/api/public/otel" in env
    assert f"AITK_OTEL_RAW_DIR={tmp_path / 'raw'}" in env and (tmp_path / "raw").is_dir()
    assert not any("sk-lf" in a or "cGstbGYt" in a for a in argv)


def test_up_without_credentials_fails_before_docker(run, docker, tmp_path):
    (tmp_path / "local.env").write_text("")
    r = run([OTEL, "up"])
    assert r.returncode != 0 and "LANGFUSE_SECRET_KEY" in r.stderr and docker.calls("docker") == []


def test_up_refuses_a_raw_dir_inside_a_git_worktree(run, docker, repo):
    r = run([OTEL, "up"], cwd=repo.root, AITK_OTEL_RAW_DIR=repo.root / "raw")
    assert r.returncode != 0 and "git" in r.stderr and docker.calls("docker") == []


def test_down_and_status_argv_and_status_exit_code(run, docker):
    assert run([OTEL, "down"]).returncode == 0
    assert docker.calls("docker")[0][-1] == "down"
    r = run([OTEL, "status"])
    assert r.returncode == 1 and r.stdout.strip() == "down" and docker.calls("docker")[1][5:] == ["ps", "--status", "running", "--quiet"]
    docker.reply("docker", "abc123\n")
    r = run([OTEL, "status"])
    assert r.returncode == 0 and r.stdout.strip() == "up"


def test_unknown_subcommand_is_a_usage_error(run, docker):
    r = run([OTEL, "frobnicate"])
    assert r.returncode != 0 and "usage" in r.stderr and docker.calls("docker") == []


def test_collector_wires_receivers_through_the_transform_to_langfuse():
    svc = COLLECTOR["service"]["pipelines"]
    assert set(svc) == {"traces", "logs", "metrics"}
    for name, p in svc.items():
        assert set(p["receivers"]) <= set(COLLECTOR["receivers"]), name
        assert set(p.get("processors", [])) <= set(COLLECTOR["processors"]), name
        assert set(p["exporters"]) <= set(COLLECTOR["exporters"]), name
    assert svc["traces"] == {"receivers": ["otlp", "otlp/beta"], "processors": ["transform/langfuse", "batch"], "exporters": ["otlphttp/langfuse"]}
    lf = COLLECTOR["exporters"]["otlphttp/langfuse"]
    assert lf["endpoint"] == "${env:LANGFUSE_OTLP_ENDPOINT}" and lf["headers"]["Authorization"] == "${env:LANGFUSE_BASIC_AUTH}"


def test_transform_keeps_session_and_token_mapping_and_drops_the_bridge_machinery():
    stmts = "\n".join(s for g in COLLECTOR["processors"]["transform/langfuse"]["trace_statements"] for s in g["statements"])
    assert 'set(attributes["langfuse.session.id"], resource.attributes["spoke_run_id"])' in stmts
    for usage in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        assert f"gen_ai.usage.{usage}" in stmts
    assert "Concat([\"tool:\"" in stmts
    raw = (V2 / "langfuse/otelcol.yaml").read_text().lower()
    assert "bridge" not in raw and "prometheus" not in raw and "8889" not in raw


def test_logs_and_metrics_land_as_rotated_jsonl_in_the_mounted_raw_dir():
    for sig in ("logs", "metrics"):
        (exp,) = COLLECTOR["service"]["pipelines"][sig]["exporters"]
        cfg = COLLECTOR["exporters"][exp]
        assert exp.startswith("file/") and cfg["path"].startswith("/raw/") and cfg["rotation"]["max_megabytes"] and cfg["rotation"]["max_backups"]
    assert "${AITK_OTEL_RAW_DIR:?}:/raw" in COMPOSE["services"]["collector"]["volumes"]


def test_compose_mounts_the_config_readonly_and_forwards_secrets_without_values():
    svc = COMPOSE["services"]["collector"]
    assert "./otelcol.yaml:/etc/otelcol-contrib/config.yaml:ro" in svc["volumes"]
    assert svc["environment"] == ["LANGFUSE_OTLP_ENDPOINT", "LANGFUSE_BASIC_AUTH"]
    assert svc["container_name"] == "lf-collector" and "host.docker.internal:host-gateway" in svc["extra_hosts"]


def test_the_shim_exports_to_the_collectors_receiver_ports(run, stubs, repo):
    wt = repo.wt("wp5")
    (wt / ".ai-toolkit").mkdir()
    (wt / ".ai-toolkit/spoke-run-id").write_text("rid\n")
    assert run([str(V2 / "bin/claude-spoke")], cwd=wt).returncode == 0
    env = dict(ln.split("=", 1) for ln in stubs.env("claude").splitlines() if "=" in ln)
    proto = COLLECTOR["receivers"]
    grpc = proto["otlp"]["protocols"]["grpc"]["endpoint"].rsplit(":", 1)[1]
    beta = proto["otlp/beta"]["protocols"]["http"]["endpoint"].rsplit(":", 1)[1]
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc" and str(urlparse(env["OTEL_EXPORTER_OTLP_ENDPOINT"]).port) == grpc
    assert str(urlparse(env["BETA_TRACING_ENDPOINT"]).port) == beta
    published = [p.split(":")[-2] for p in COMPOSE["services"]["collector"]["ports"]]
    assert {grpc, beta} <= set(published)
