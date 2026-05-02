# Security Review

Interactive security audit — walk through a structured checklist to find vulnerabilities
in application code, configuration, and dependencies.

## When to Use

- Auditing code for security before shipping (pre-PR, pre-deploy)
- Reviewing auth, payments, or PII-handling code
- User says "security review", "audit this for vulnerabilities", "is this secure?"

**Do NOT use when:**

- General code review (use `code-review` agent — it includes basic security checks)
- Writing security rules (the `security` rule handles that passively)
- Pen-testing or runtime scanning (out of scope — use dedicated tools)

## Workflow

### 1. Scope the Review

Identify what to audit:

- **Files changed** — check the diff (`get_changed_files`)
- **Critical paths** — auth, payments, admin, file uploads, API keys
- **Attack surface** — public endpoints, user input, external integrations

### 2. Run the 10-Point Checklist

Work through each item. For every finding, note severity and location.

#### ① Secrets Management

```
- [ ] No hardcoded API keys, passwords, tokens, or connection strings
- [ ] Secrets loaded from environment variables or secrets manager
- [ ] .env files in .gitignore
- [ ] No secrets in logs, error messages, or client-side code
```

**Search for:**

```bash
grep -rn "sk-\|sk_live\|ghp_\|AKIA\|password\s*=\s*['\"]" --include="*.py" --include="*.ts" --include="*.js" .
grep -rn "API_KEY\|SECRET\|TOKEN\|PRIVATE" --include="*.py" --include="*.ts" --include="*.env" .
```

#### ② Input Validation

```
- [ ] All user inputs validated and sanitized at entry points
- [ ] File uploads: type, size, and content validated (not just extension)
- [ ] URL parameters, query strings, and headers validated
- [ ] Schema validation (Zod, Pydantic, Joi) at API boundaries
```

#### ③ SQL Injection

```
- [ ] All queries use parameterized statements or ORM methods
- [ ] No string concatenation or f-strings in SQL queries
- [ ] Raw SQL queries (if any) use bound parameters
```

**Search for:**

```bash
grep -rn "f\".*SELECT\|f\".*INSERT\|f\".*UPDATE\|f\".*DELETE" --include="*.py" .
grep -rn "execute.*\`\|query.*\`.*\$\{" --include="*.ts" --include="*.js" .
```

#### ④ Authentication & Authorization

```
- [ ] Auth required on all non-public endpoints
- [ ] Tokens validated on every request (signature + expiry)
- [ ] Authorization checked per-resource (not just "is logged in")
- [ ] Row-Level Security (RLS) enabled on tables with user data
- [ ] Password hashing uses bcrypt, argon2, or scrypt — never MD5/SHA
- [ ] Rate limiting on login/signup endpoints
```

#### ⑤ XSS Prevention

```
- [ ] User content HTML-escaped before rendering
- [ ] React: no dangerouslySetInnerHTML with user data
- [ ] Content-Security-Policy (CSP) header configured
- [ ] HttpOnly + Secure + SameSite flags on cookies
```

#### ⑥ CSRF Protection

```
- [ ] CSRF tokens on state-changing requests (POST, PUT, DELETE)
- [ ] SameSite=Strict or SameSite=Lax on session cookies
- [ ] Origin/Referer header validated for sensitive actions
```

#### ⑦ Rate Limiting

```
- [ ] Rate limits on all public endpoints
- [ ] Stricter limits on auth endpoints (login, signup, password reset)
- [ ] Rate limits on expensive operations (search, report generation)
- [ ] 429 responses include Retry-After header
```

#### ⑧ Sensitive Data Exposure

```
- [ ] No PII, tokens, or passwords in application logs
- [ ] Error responses don't include stack traces in production
- [ ] API responses don't include fields the user shouldn't see
- [ ] Database queries don't SELECT * — only needed fields
```

#### ⑨ Dependency Security

```
- [ ] Lock files committed (package-lock.json, requirements.txt, go.sum)
- [ ] No known vulnerabilities in dependencies
- [ ] Dependencies audited recently
```

**Check:**

```bash
npm audit                     # Node.js
pip-audit                     # Python
cargo audit                   # Rust
gh api /repos/:owner/:repo/vulnerability-alerts  # GitHub
```

#### ⑩ Infrastructure & Config

```
- [ ] HTTPS enforced — no plain HTTP in production
- [ ] CORS configured with specific origins (not *)
- [ ] Security headers set (X-Content-Type-Options, X-Frame-Options, etc.)
- [ ] Debug mode disabled in production
- [ ] Admin/internal endpoints not publicly accessible
```

### 3. Report Findings

Structure the report by severity:

```markdown
## Security Review: <scope>

### 🔴 Critical (fix before shipping)
- **[FILE:LINE]** Hardcoded API key in source code
- **[FILE:LINE]** SQL injection via string concatenation

### 🟡 Warning (fix soon)
- **[FILE:LINE]** Missing rate limiting on login endpoint
- **[FILE:LINE]** CORS allows all origins

### 🟢 Info (consider improving)
- **[FILE:LINE]** console.log may leak sensitive data in production
- No Content-Security-Policy header configured

### ✅ Passed
- Auth tokens validated on all protected endpoints
- Parameterized queries used throughout
- Secrets loaded from environment variables
```

## Pre-Deployment Security Checklist

Final check before any production deployment:

```
- [ ] No hardcoded secrets in code or config
- [ ] All inputs validated at boundaries
- [ ] Auth + authorization on every protected endpoint
- [ ] SQL injection impossible (parameterized queries only)
- [ ] XSS mitigated (output encoding, CSP)
- [ ] CSRF protection on state-changing endpoints
- [ ] Rate limiting on public and auth endpoints
- [ ] No sensitive data in logs or error responses
- [ ] Dependencies audited — no known CVEs
- [ ] HTTPS enforced, security headers configured
- [ ] CORS restricted to known origins
- [ ] Debug mode off in production
```

## Resources

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/)
- [CWE Top 25](https://cwe.mitre.org/top25/)
