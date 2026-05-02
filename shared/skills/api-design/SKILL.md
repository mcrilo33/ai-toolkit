# API Design

REST API design patterns — resource naming, HTTP methods, status codes, pagination,
filtering, versioning, and error responses.

## When to Use

- Designing or reviewing REST API endpoints
- Defining response formats, pagination, or error handling
- User says "design the API for X", "what status code for Y", "add pagination"

**Do NOT use when:**

- Implementing the backend logic (use `backend-patterns`)
- GraphQL-only work (different paradigm)
- Frontend data fetching (use `frontend-patterns`)

## Resource Design

### URL Structure

```
GET    /api/v1/users              → list users
POST   /api/v1/users              → create user
GET    /api/v1/users/:id          → get user
PATCH  /api/v1/users/:id          → update user
DELETE /api/v1/users/:id          → delete user
GET    /api/v1/users/:id/orders   → list user's orders
```

### Naming Rules

- **Plural nouns** for collections: `/users`, `/orders` — never `/user`, `/getUsers`
- **No verbs in URLs** — the HTTP method is the verb
- **Lowercase, hyphen-separated**: `/order-items` — never `/orderItems` or `/order_items`
- **Nest for ownership**: `/users/:id/orders` — max 2 levels deep
- **Actions as sub-resources** when no CRUD fits: `POST /orders/:id/cancel`

## HTTP Methods & Status Codes

### Method Semantics

| Method | Purpose | Idempotent | Body |
|--------|---------|-----------|------|
| `GET` | Read | Yes | No |
| `POST` | Create | No | Yes |
| `PUT` | Full replace | Yes | Yes |
| `PATCH` | Partial update | No* | Yes |
| `DELETE` | Remove | Yes | No |

*PATCH can be made idempotent with proper implementation.

### Status Code Reference

| Code | Meaning | Use When |
|------|---------|----------|
| `200` | OK | Successful GET, PATCH, DELETE |
| `201` | Created | Successful POST (include `Location` header) |
| `204` | No Content | Successful DELETE with no response body |
| `400` | Bad Request | Validation error, malformed input |
| `401` | Unauthorized | Missing or invalid auth token |
| `403` | Forbidden | Valid auth but insufficient permissions |
| `404` | Not Found | Resource doesn't exist |
| `409` | Conflict | Duplicate resource, optimistic lock failure |
| `422` | Unprocessable Entity | Valid JSON but semantic validation failed |
| `429` | Too Many Requests | Rate limited (include `Retry-After`) |
| `500` | Internal Server Error | Unhandled exception |

### Common Mistakes

- Using `200` for creation → use `201`
- Using `404` for authorization failures → use `403`
- Using `500` for validation errors → use `400` or `422`
- Using `200` with `{ "success": false }` → use proper status codes

## Response Format

### Success — Single Resource

```json
{
  "data": {
    "id": "usr_abc123",
    "email": "user@example.com",
    "name": "Alice",
    "created_at": "2025-01-15T10:30:00Z"
  }
}
```

### Success — Collection with Pagination

```json
{
  "data": [
    { "id": "usr_abc123", "name": "Alice" },
    { "id": "usr_def456", "name": "Bob" }
  ],
  "pagination": {
    "total": 142,
    "page": 2,
    "per_page": 20,
    "total_pages": 8
  }
}
```

### Error

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid input",
    "details": [
      { "field": "email", "message": "Must be a valid email address" },
      { "field": "age", "message": "Must be at least 18" }
    ]
  }
}
```

**Rules:**

- Wrap responses in `{ "data": ... }` or `{ "error": ... }` — never raw arrays
- Use ISO 8601 for dates: `2025-01-15T10:30:00Z`
- Use prefixed IDs when possible: `usr_abc123`, `ord_def456`
- Include `created_at` and `updated_at` on all resources
- Never expose internal IDs (auto-increment integers) — use UUIDs or prefixed IDs

## Pagination

### Offset-Based (simple)

```
GET /api/v1/users?page=2&per_page=20
```

**Pros:** Simple, supports "jump to page N".
**Cons:** Inconsistent on inserts/deletes, slow on large offsets.

### Cursor-Based (recommended for large datasets)

```
GET /api/v1/users?cursor=eyJpZCI6MTAwfQ&limit=20
```

Response includes:

```json
{
  "data": [...],
  "pagination": {
    "next_cursor": "eyJpZCI6MTIwfQ",
    "has_more": true
  }
}
```

**Pros:** Consistent, fast regardless of dataset size.
**Cons:** No "jump to page N".

**Use offset for:** Admin panels, dashboards with page numbers.
**Use cursor for:** Feeds, timelines, public APIs, large datasets.

## Filtering, Sorting, and Search

### Filtering

```
GET /api/v1/orders?status=pending&created_after=2025-01-01
GET /api/v1/products?price_min=10&price_max=100&category=electronics
```

### Sorting

```
GET /api/v1/users?sort=created_at        → ascending (default)
GET /api/v1/users?sort=-created_at       → descending (prefix with -)
GET /api/v1/users?sort=-created_at,name  → multi-field
```

### Full-Text Search

```
GET /api/v1/products?q=wireless+headphones
```

### Sparse Fieldsets

```
GET /api/v1/users?fields=id,name,email
```

Reduce payload size for list endpoints. Optional but useful for mobile clients.

## Rate Limiting

Include rate limit headers in every response:

```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 42
X-RateLimit-Reset: 1672531200
```

**Tiers:**

| Tier | Limit | Scope |
|------|-------|-------|
| Unauthenticated | 60/hour | IP |
| Authenticated | 1000/hour | User/API key |
| Internal | No limit | Service-to-service |

## Versioning

### URL Path (recommended for simplicity)

```
/api/v1/users
/api/v2/users
```

### Header (recommended for flexibility)

```
Accept: application/vnd.myapp.v2+json
```

**Rules:**

- Start with `v1` — don't over-engineer versioning before you need it
- Bump version only for breaking changes (field removal, type change)
- Support at least N-1 version for deprecation period
- Additive changes (new fields, new endpoints) don't require version bump

## API Design Checklist

```
- [ ] Resource URLs use plural nouns, no verbs
- [ ] Correct HTTP methods (GET reads, POST creates, etc.)
- [ ] Proper status codes (201 for creation, 404 for missing, etc.)
- [ ] Consistent response envelope ({ data } or { error })
- [ ] Pagination on all list endpoints
- [ ] Input validation with clear error messages
- [ ] Rate limiting with headers
- [ ] Auth on every non-public endpoint
- [ ] ISO 8601 dates
- [ ] No internal IDs or stack traces in responses
- [ ] Versioned URL (/api/v1/...)
```
