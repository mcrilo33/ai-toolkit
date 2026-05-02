# CI/CD deployment reference

## Environments

- Use GitHub `environment` rules for staging and production
- Configure protection rules: manual approvals, required reviewers, branch restrictions
- Use environment-specific secrets for access control

## Staging deployment

- Deploy on merge to `develop` or `release/*` branches
- Mirror production infrastructure, config, and data (anonymized)
- Run automated smoke tests and post-deployment validation
- Implement automated promotion to production on success

## Production deployment

- Require manual approval from designated reviewers
- Monitor closely during and immediately after deployment
- Have a separate expedited pipeline for critical hotfixes
- Set clear deployment windows

## Deployment types

### Rolling update

- Default for most deployments — gradually replaces old instances with new
- Configure `maxSurge` and `maxUnavailable` for control over rollout speed

### Blue/green

- Deploy new version alongside old, then switch traffic entirely
- Instantaneous rollback by switching traffic back
- Requires two identical environments and a traffic router

### Canary

- Roll out to a small subset of users (5-10%) first
- Monitor error rates and performance before full rollout
- Implement with service mesh (Istio, Linkerd) or ingress controllers

### Feature flags / dark launch

- Deploy code but keep features hidden until toggled on
- Decouples deployment from release
- Use LaunchDarkly, Split.io, Unleash, or similar

### A/B testing

- Deploy multiple variants concurrently to different user segments
- Compare behavior and business metrics
- Integrate with specialized A/B testing platforms

## Rollback strategies

- **Automated rollbacks** — trigger on monitoring alerts or failed health checks
- **Versioned artifacts** — keep previous successful builds readily deployable
- **Runbooks** — document step-by-step rollback procedures; review regularly
- **Post-incident reviews** — blameless PIRs to identify root cause and prevent recurrence
- **Communication plan** — clear stakeholder communication during incidents

## Post-deployment validation

- Implement automated health checks immediately after deployment
- Trigger rollback if checks fail
- Monitor application metrics (error rates, latency, throughput)
- Set up alerts for anomalies in the first hours after deploy
