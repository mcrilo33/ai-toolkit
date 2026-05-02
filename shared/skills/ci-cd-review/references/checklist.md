# GitHub Actions workflow review checklist

## Structure and design

- [ ] Workflow `name` is clear, descriptive, and unique
- [ ] Triggers (`on`) are appropriate with branch/path filters
- [ ] `concurrency` is set for critical workflows
- [ ] `permissions` default to `contents: read` at workflow level
- [ ] Reusable workflows (`workflow_call`) are used where applicable
- [ ] Jobs and steps have meaningful names

## Jobs and steps

- [ ] Jobs represent distinct phases (`lint`, `test`, `build`, `deploy`)
- [ ] `needs` dependencies are correctly defined
- [ ] `outputs` are used for inter-job communication
- [ ] `if` conditions are used for conditional execution
- [ ] All `uses` actions are pinned to full commit SHA with version comment
- [ ] `run` commands are efficient (combined with `&&`, no unnecessary steps)
- [ ] `env` variables are scoped appropriately (no hardcoded secrets)
- [ ] `timeout-minutes` is set on long-running jobs

## Security

- [ ] Secrets accessed exclusively via `${{ secrets.NAME }}`
- [ ] OIDC used for cloud authentication where possible
- [ ] `GITHUB_TOKEN` permissions are explicitly scoped and minimal
- [ ] SCA tools (dependency-review-action, Snyk, Trivy) are integrated
- [ ] SAST tools (CodeQL, SonarQube) are integrated; critical findings block builds
- [ ] Secret scanning is enabled; pre-commit hooks recommended
- [ ] Container images are signed (Cosign/Notary) if applicable
- [ ] Self-hosted runners are hardened with restricted network access

## Optimization

- [ ] `actions/cache` is used for dependencies with `hashFiles` keys
- [ ] `restore-keys` provide fallback for cache misses
- [ ] `strategy.matrix` parallelizes tests across environments
- [ ] `fetch-depth: 1` is used unless full history is needed
- [ ] Artifacts transfer data between jobs instead of rebuilding
- [ ] Git LFS is optimized or disabled if not needed

## Testing

- [ ] Unit tests run early in the pipeline on every push/PR
- [ ] Integration tests use `services` for dependencies
- [ ] E2E tests run against staging with flakiness mitigation
- [ ] Performance tests are integrated for critical paths with thresholds
- [ ] Test reports (JUnit XML, coverage) are uploaded as artifacts
- [ ] Code coverage is tracked and enforced

## Deployment

- [ ] Staging and production use GitHub `environment` with protection rules
- [ ] Manual approvals are required for production deployments
- [ ] Rollback strategy is defined and tested
- [ ] Deployment type (rolling, blue/green, canary) matches risk tolerance
- [ ] Post-deployment health checks and smoke tests run automatically
- [ ] Workflow handles transient failures with retries

## Observability

- [ ] Logging is adequate for debugging workflow failures
- [ ] Application metrics are collected (Prometheus, etc.)
- [ ] Alerts are configured for critical failures and anomalies
- [ ] Artifact `retention-days` are set appropriately
