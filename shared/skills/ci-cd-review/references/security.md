# CI/CD security reference

## Secret management

- Use GitHub Secrets for all sensitive data (API keys, passwords, tokens)
- Access via `secrets.<SECRET_NAME>` — never hardcode, never log
- Use environment-specific secrets for deployment targets with manual approvals
- Never construct secrets dynamically or print them, even if masked

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: production
      url: https://prod.example.com
    steps:
      - name: Deploy to production
        env:
          PROD_API_KEY: ${{ secrets.PROD_API_KEY }}
        run: ./deploy-script.sh
```

## OpenID Connect (OIDC) for cloud authentication

- Use OIDC for credential-less authentication with AWS, Azure, GCP
- Exchanges a short-lived JWT for temporary cloud credentials — no static keys
- Requires trust policy configuration in your cloud provider
- Always pin OIDC actions to full commit SHA

## Least privilege for `GITHUB_TOKEN`

- Default to `contents: read` at workflow level
- Add write permissions only when strictly needed
- Override per-job for elevated access

```yaml
permissions:
  contents: read
  pull-requests: write  # only if workflow updates PRs

jobs:
  lint:
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
      - run: npm run lint
```

## Dependency review and SCA

- Integrate `dependency-review-action`, Snyk, or Trivy early in the pipeline
- Scan for known vulnerabilities and licensing issues in dependencies
- Set up alerts for new vulnerability findings

## Static application security testing (SAST)

- Integrate CodeQL, SonarQube, Bandit (Python), or ESLint security plugins
- Block PRs on critical findings
- Add security linters to pre-commit hooks for earlier feedback

## Secret scanning and leak prevention

- Enable GitHub's built-in secret scanning
- Use pre-commit hooks (e.g., `git-secrets`) to catch leaks locally
- Pass secrets only to the environment where needed at runtime, never in build artifacts

## Immutable infrastructure and image signing

- Ensure reproducible builds in Dockerfiles
- Sign container images with Cosign or Notary
- Enforce that only signed images can be deployed to production

## Self-hosted runner security

- Harden runner machines and restrict network access
- Manage access controls and ensure timely patching
- Use runner groups for organization
