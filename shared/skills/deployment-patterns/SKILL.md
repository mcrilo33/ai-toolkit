# Deployment Patterns

Production deployment strategies, CI/CD pipeline design, health checks, environment
configuration, and rollback procedures.

## When to Use

- Designing or reviewing a deployment pipeline
- Setting up health checks, rollback, or environment config
- User says "deploy pattern for X", "how should I ship this", "add health checks"

**Do NOT use when:**

- Writing GitHub Actions YAML (use `ci-cd-review` skill + `github-actions` rule)
- Docker-only work (use `docker-patterns`)
- Application code changes (use `backend-patterns` or `frontend-patterns`)

## Deployment Strategies

### Rolling Deployment (default)

Replace instances one at a time. Zero-downtime if health checks pass.

```
[v1] [v1] [v1] [v1]
[v2] [v1] [v1] [v1]  ← one instance updated
[v2] [v2] [v1] [v1]  ← second instance updated
[v2] [v2] [v2] [v2]  ← all updated
```

**Use when:** Standard releases, backward-compatible changes, most deployments.

### Blue-Green Deployment

Run two identical environments. Switch traffic atomically.

```
Blue  (v1) ← live traffic
Green (v2) ← deploy + test here
                ↓
Green (v2) ← live traffic (swap)
Blue  (v1) ← standby / rollback target
```

**Use when:** Database migrations, major version bumps, need instant rollback.

### Canary Deployment

Route a small percentage of traffic to the new version. Monitor before full rollout.

```
v1 ← 95% traffic
v2 ← 5% traffic → monitor errors, latency, metrics
         ↓
v2 ← 25% → 50% → 100% (if healthy)
```

**Use when:** High-risk changes, public-facing APIs, gradual confidence building.

## Health Checks

Every service must expose health endpoints:

| Endpoint | Purpose | Status codes |
|----------|---------|-------------|
| `/health` or `/healthz` | Basic liveness — "is the process running?" | `200` or `503` |
| `/ready` or `/readyz` | Readiness — "can it serve traffic?" (DB, cache, deps) | `200` or `503` |

**Rules:**

- Liveness: fast, no dependency checks — just return 200
- Readiness: check database connection, cache, critical external services
- Return structured JSON: `{ "status": "ok", "checks": { "db": "ok", "cache": "ok" } }`
- Kubernetes probes: `livenessProbe` → `/healthz`, `readinessProbe` → `/readyz`
- Set appropriate `initialDelaySeconds`, `periodSeconds`, `failureThreshold`

## Environment Configuration

Follow the [Twelve-Factor App](https://12factor.net/config) methodology:

**Rules:**

- All config via environment variables — never hardcoded
- Validate all env vars at startup — fail fast if required vars are missing
- Use a schema (Zod, Pydantic, envconfig) to parse and validate
- Group by concern: `DATABASE_URL`, `REDIS_URL`, `API_KEY_STRIPE`
- Document all required env vars in `.env.example` (no real values)
- Secrets via secrets manager (Vault, AWS Secrets Manager, Keychain) — not `.env` in production

## Rollback Strategy

### Instant Rollback

- Keep previous version's container image/artifact tagged and available
- Rollback = redeploy previous tag (no rebuild)
- Database: only backward-compatible migrations (expand-contract pattern)

### Rollback Checklist

```
- [ ] Identify the failing version and symptoms
- [ ] Trigger rollback (redeploy previous version tag)
- [ ] Verify health checks pass on rolled-back version
- [ ] Check for data migration conflicts (can v1 read v2's data?)
- [ ] Notify the team with incident details
- [ ] Create a post-mortem issue
```

## Production Readiness Checklist

### Application

```
- [ ] Health check endpoints implemented and tested
- [ ] Structured logging with correlation IDs
- [ ] Graceful shutdown handles in-flight requests
- [ ] Environment variables validated at startup
- [ ] Error responses don't leak stack traces or internal details
```

### Infrastructure

```
- [ ] Auto-scaling configured (min/max instances, CPU/memory triggers)
- [ ] Load balancer health checks point to /readyz
- [ ] TLS/HTTPS enforced — no plain HTTP in production
- [ ] DNS and CDN configured for static assets
```

### Monitoring

```
- [ ] Application metrics exported (request rate, latency, error rate)
- [ ] Alerts configured for error rate spikes and latency degradation
- [ ] Log aggregation in place (structured, searchable)
- [ ] Uptime monitoring for critical endpoints
```

### Security

```
- [ ] No hardcoded secrets in code or config files
- [ ] Secrets loaded from secrets manager
- [ ] Network policies restrict inter-service traffic
- [ ] Container images scanned for vulnerabilities
- [ ] Dependencies audited (npm audit, pip-audit, etc.)
```

### Operations

```
- [ ] Runbook documented for common failure scenarios
- [ ] On-call rotation defined
- [ ] Rollback procedure tested
- [ ] Backup and restore procedure verified
```
