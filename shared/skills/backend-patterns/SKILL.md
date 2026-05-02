# Backend Patterns

Production-ready patterns for API services, database access, caching, auth, and background
processing. Use these as reference implementations when building or reviewing backend code.

## When to Use

- Building or reviewing an API service (REST or GraphQL)
- Designing database access layers, caching, or auth flows
- User says "backend pattern for X", "how should I structure the API", "add caching"

**Do NOT use when:**

- Frontend-only work (use `frontend-patterns`)
- CI/CD or deployment work (use `deployment-patterns`)
- Pure database migration (use `database-migrations`)

## Patterns

### 1. Service Layer + Repository

Separate business logic from data access. Never put queries in route handlers.

```
Route Handler → Service (business logic) → Repository (data access) → Database
```

**Rules:**

- Handlers validate input + call service — nothing else
- Services orchestrate logic, call repositories, enforce invariants
- Repositories execute queries, return typed objects
- Each layer depends only on the layer below it

### 2. Error Handling

Centralize error handling. Never swallow errors silently.

```
                    ┌─ Domain errors (400s) ← Service layer raises
Request → Handler → ┤
                    └─ Infra errors (500s)  ← Repository/external calls
                            ↓
                    Global error middleware → structured JSON response + log
```

**Rules:**

- Define typed error classes: `NotFoundError`, `ValidationError`, `ConflictError`
- Map domain errors → HTTP status codes in ONE place (middleware/handler)
- Always return `{ error: { code, message, details? } }` — never raw stack traces
- Log full context (request ID, user, stack) server-side; return safe message client-side

### 3. Database Access

**N+1 prevention:**

- Use `JOIN` / `include` / `prefetch_related` for associations
- Never query in a loop — batch with `WHERE id IN (...)`
- Use query logging in dev to spot N+1 patterns

**Transactions:**

- Wrap multi-step mutations in a transaction
- Use `SELECT ... FOR UPDATE` when concurrent writes compete for the same row
- Keep transactions short — no HTTP calls or heavy compute inside

**Connection management:**

- Use a connection pool (pgBouncer, built-in pool)
- Set pool size = `(2 × CPU cores) + disk spindles` as a baseline
- Always release connections (use context managers / `finally`)

### 4. Caching

**Cache-aside pattern (most common):**

```
1. Check cache → hit? return cached
2. Miss → query DB → store in cache → return
3. On write → invalidate cache key
```

**Rules:**

- Always set a TTL — never cache forever
- Cache at the service layer, not the repository layer
- Use consistent key naming: `<entity>:<id>:<version>` (e.g., `user:42:v1`)
- Invalidate on writes — stale data is worse than a cache miss
- Consider cache stampede: use lock/singleflight for expensive queries

### 5. Authentication & Authorization

**JWT flow:**

```
Login → validate credentials → issue access token (short-lived) + refresh token
Request → extract token → verify signature + expiry → attach user to context
```

**Rules:**

- Access tokens: 15 min max. Refresh tokens: 7–30 days, rotatable
- Store refresh tokens server-side (DB or Redis), not just in cookies
- Never put sensitive data in JWT payload (it's base64, not encrypted)
- Validate permissions per-request — never trust the client's role claim alone

**Row-Level Security (RLS) when using Supabase/Postgres:**

- Enable RLS on every table with user data
- Policies: `USING (auth.uid() = user_id)` for read, similar for write
- Test RLS by querying as different users

### 6. Rate Limiting

- Apply at the API gateway or middleware level
- Use sliding window or token bucket algorithm
- Different tiers: unauthenticated (strict), authenticated (relaxed), internal (none)
- Return `429 Too Many Requests` with `Retry-After` header
- Log rate-limited requests for abuse detection

### 7. Background Jobs

- Use a job queue (Celery, BullMQ, Sidekiq, Temporal) for:
  - Email/notification sending
  - Report generation
  - Data sync / ETL
  - Webhook delivery with retry
- Jobs must be **idempotent** — safe to retry on failure
- Set timeouts and max retries per job type
- Use dead-letter queues for jobs that exhaust retries

### 8. Structured Logging

```
{ "level": "error", "msg": "payment failed", "request_id": "abc-123",
  "user_id": 42, "amount": 99.99, "error": "card_declined", "ts": "..." }
```

**Rules:**

- Use structured JSON logging (not free-form strings)
- Include: timestamp, level, message, request ID, user context
- Never log secrets, tokens, passwords, or full credit card numbers
- Use correlation IDs across services for distributed tracing
- Log at boundaries: incoming request, outgoing call, error, business event
