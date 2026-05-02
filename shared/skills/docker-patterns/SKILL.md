# Docker Patterns

Production-ready Docker and Docker Compose patterns for local development, multi-stage
builds, networking, volumes, and container security.

## When to Use

- Setting up or reviewing Docker / Docker Compose configurations
- Building multi-stage Dockerfiles for production
- User says "dockerize this", "add Docker Compose", "optimize my Dockerfile"

**Do NOT use when:**

- Kubernetes-specific work (orchestration, Helm charts)
- CI/CD pipeline design (use `deployment-patterns` or `ci-cd-review`)
- Application code changes unrelated to containerization

## Multi-Stage Dockerfile

Always use multi-stage builds for production. Separate build dependencies from runtime.

### Node.js / TypeScript

```dockerfile
# Stage 1: Install dependencies
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts

# Stage 2: Build
FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# Stage 3: Production
FROM node:22-alpine AS runtime
WORKDIR /app
RUN addgroup -S app && adduser -S app -G app
COPY --from=build --chown=app:app /app/dist ./dist
COPY --from=build --chown=app:app /app/node_modules ./node_modules
COPY --from=build --chown=app:app /app/package.json ./
USER app
EXPOSE 3000
CMD ["node", "dist/index.js"]
```

### Python

```dockerfile
# Stage 1: Build
FROM python:3.13-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Production
FROM python:3.13-slim AS runtime
WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app
COPY --from=build /install /usr/local
COPY --chown=app:app . .
USER app
EXPOSE 8000
CMD ["gunicorn", "app:create_app()", "-b", "0.0.0.0:8000"]
```

### Go

```dockerfile
FROM golang:1.23-alpine AS build
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /server ./cmd/server

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=build /server /server
EXPOSE 8080
ENTRYPOINT ["/server"]
```

## Docker Compose — Local Development

```yaml
services:
  app:
    build:
      context: .
      target: deps  # Use deps stage for dev (skip production build)
    volumes:
      - .:/app
      - /app/node_modules  # Anonymous volume — don't overwrite installed deps
    ports:
      - "3000:3000"
    env_file: .env
    depends_on:
      db:
        condition: service_healthy

  db:
    image: postgres:17-alpine
    environment:
      POSTGRES_DB: app_dev
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 3s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

volumes:
  pgdata:
```

### Override Files

Use `docker-compose.override.yml` for dev-specific config (auto-loaded):

```yaml
# docker-compose.override.yml — dev only, not committed
services:
  app:
    command: npm run dev
    environment:
      DEBUG: "true"
```

## Volume Strategies

| Type | Use case | Syntax |
|------|----------|--------|
| Named volume | Persistent data (DB, uploads) | `pgdata:/var/lib/postgresql/data` |
| Bind mount | Source code in dev (hot reload) | `.:/app` |
| Anonymous volume | Protect installed deps from bind mount | `/app/node_modules` |

**Rules:**

- Named volumes for data that must survive `docker compose down`
- Bind mounts for dev only — never in production
- Anonymous volumes to prevent host bind mounts from overwriting container directories

## Networking

```yaml
services:
  api:
    networks: [backend]
  web:
    networks: [backend, frontend]
  db:
    networks: [backend]  # Not accessible from frontend

networks:
  backend:
  frontend:
```

**Rules:**

- Services on the same network resolve each other by service name (`db:5432`)
- Isolate databases and internal services from public-facing networks
- Only expose ports you need — `ports` is host-accessible, `expose` is internal-only

## Container Security

**Dockerfile hardening:**

```
- [ ] Run as non-root user (USER app)
- [ ] Use specific image tags (node:22-alpine, not node:latest)
- [ ] Use slim/alpine base images
- [ ] No secrets in build args or ENV — use runtime secrets
- [ ] COPY specific files — never COPY . . without .dockerignore
- [ ] Set HEALTHCHECK in Dockerfile or Compose
```

**Compose security:**

```
- [ ] read_only: true where possible
- [ ] security_opt: [no-new-privileges:true]
- [ ] mem_limit and cpus set for each service
- [ ] No privileged: true unless absolutely required
```

## .dockerignore

Always include — prevents bloating images and leaking secrets:

```
node_modules
.git
.env
.env.*
*.log
dist
coverage
.vscode
.idea
__pycache__
*.pyc
.mypy_cache
.pytest_cache
```

## Common Anti-Patterns

| Anti-Pattern | Fix |
|---|---|
| `FROM node:latest` | Pin version: `FROM node:22-alpine` |
| `RUN npm install` in production | Use `npm ci` for deterministic installs |
| Running as root | Add `USER` directive |
| `COPY . .` without `.dockerignore` | Create proper `.dockerignore` |
| Secrets in `ENV` or `ARG` | Use runtime env vars or secrets manager |
| Single-stage Dockerfile | Use multi-stage builds |
| No healthcheck | Add `HEALTHCHECK` to Dockerfile or Compose |
