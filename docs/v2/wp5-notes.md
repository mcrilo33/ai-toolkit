# v2 WP5 notes: native OTel collector (reduced by D1)

Files: `v2/langfuse/otelcol.yaml`, `v2/langfuse/compose.yaml`, `v2/scripts/otel.sh`, `tests_v2/test_otel.py`.
`lib.sh` and `claude-spoke` are untouched: the shim's env already matches the receivers (a test pins the ports).

## Decisions
- **Traces** -> OTTL transform -> Langfuse (`otlp_http/langfuse`): token remap to `gen_ai.usage.*` (else cost reads $0),
  span renames (`tool:X`, `hook:X`, `sub-agent:X`), `spoke_run_id` -> `langfuse.session.id`, repo metadata,
  prompt / command / model-output -> `langfuse.observation.input/output`.
- **Logs + metrics** (coordinator decision, D1 "raw from day one") -> `file` exporters: rotated JSONL in `/raw`, the host
  dir `AITK_OTEL_RAW_DIR` (default `~/.ai-toolkit/otel-raw`; `otel.sh up` refuses a dir inside a git worktree).
  25 MB x (5 backups + 1) per signal, about 300 MB max. Logs carry `claude_code.api_request` (tokens/cost), `tool`
  decisions, `user_prompt`, `assistant_response`, `system_prompt`, hooks, MCP, each with `spoke_run_id`.
- Dropped vs the old config: message bridge + `logs` fork, Prometheus/8889, `langfuse.environment` stamp, the
  `trace.tags` repo tag, metadata for new_context/system_reminders/tools/system_prompt_preview.
- compose: container `lf-collector` (same name as today), image pinned `0.154.0` (was `:latest`), ports bound to
  127.0.0.1, `restart: unless-stopped`. `otel.sh` builds `LANGFUSE_BASIC_AUTH` from `LANGFUSE_PUBLIC_KEY` +
  `LANGFUSE_SECRET_KEY` (local env file) and passes it to docker via the environment only, never argv.

## Findings for later WPs
- **With `ENABLE_BETA_TRACING_DETAILED=1`, Claude sends logs AND metrics to `BETA_TRACING_ENDPOINT` (:4418, HTTP), not
  to the gRPC endpoint.** The `otlp/beta` receiver therefore feeds all three pipelines. Without it, only traces arrive.
- The old `lf-collector` container (old config, bridge fork) still owns the ports: `otel.sh up` will clash until it is
  removed (`docker rm -f lf-collector`) at cutover. WP6/cutover should do that.
- `otlphttp` is a deprecated alias in 0.154 (`otlp_http`); the old config still uses it.
- `LANGFUSE_SECRET_KEY` must be in `.ai-toolkit/ai-toolkit.local.env` (or the caller env); `ai-toolkit.env` only has the
  public key slot. The coordinator's session scores (later batch) need the same key.
- Langfuse's session API has no delete: the two live-check sessions (`live-1791...`) stay in the local Langfuse.

## Live check (read-only on Langfuse, throwaway collector on :14317/:14418, removed afterwards)
Shim-launched `claude -p "say hi"` (haiku) with the v2 config: session `<spoke_run_id>` exists with 1 trace;
`claude_code.llm_request` has `usageDetails` (input/output/cache_read) and a calculated cost; `hook:Stop`,
`hook:UserPromptSubmit` renames applied; `raw/logs.jsonl` + `raw/metrics.jsonl` written. NOT verified live:
`otel.sh up` itself (it would replace the user's running `lf-collector`); covered by the docker-stub tests,
`docker compose config` and `otelcol validate` instead.

## Budgets (lines)
otelcol.yaml 48/80 · compose.yaml 14/20 · otel.sh 31/40 · test_otel.py 112 (own file, no conftest change).
