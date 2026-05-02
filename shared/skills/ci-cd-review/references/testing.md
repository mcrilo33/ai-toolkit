# CI/CD testing reference

## Testing pyramid in CI

Run tests in order of speed and scope:

```text
lint → unit → integration → E2E → performance
```

Each tier should have its own job, running after the previous tier passes.

## Unit tests

- Run on every `push` and `pull_request`
- Use language-specific runners: Jest, Vitest, Pytest, Go testing, JUnit
- Integrate code coverage tools (Istanbul, Coverage.py, JaCoCo)
- Enforce minimum coverage thresholds
- Publish results as artifacts or via GitHub Checks/Annotations
- Parallelize for speed

## Integration tests

- Verify interactions between components using real dependencies
- Provision services via `services` in the workflow:

```yaml
jobs:
  integration:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: test
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
      redis:
        image: redis:7
        ports:
          - 6379:6379
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
      - run: npm ci && npm run test:integration
```

- Plan for test data management — repeatable setup and cleanup
- Run after unit tests, before E2E

## End-to-end (E2E) tests

- Use Cypress, Playwright, or Selenium
- Run against a deployed staging environment when possible
- Mitigate flakiness: explicit waits, `data-testid` selectors, retries
- Capture screenshots and video on failure
- Consider visual regression testing (Applitools, Percy)

## Performance and load testing

- Tools: k6, Locust, JMeter, Gatling, Artillery
- Run less frequently: nightly, weekly, or on significant merges
- Define thresholds (response time, throughput, error rates) and fail on breach
- Compare against established baselines

## Test reporting and visibility

- Publish JUnit XML / HTML reports as artifacts
- Use GitHub Checks/Annotations for inline PR feedback
- Integrate with SonarQube, Codecov, or Allure for trend analysis
- Add workflow status badges to README
