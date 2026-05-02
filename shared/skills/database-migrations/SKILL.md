# Database Migrations

Safe migration patterns for schema changes — zero-downtime strategies, ORM-specific
workflows, and anti-patterns to avoid.

## When to Use

- Adding, modifying, or removing database tables or columns
- Reviewing migration files for safety
- User says "add a migration", "is this migration safe?", "how to rename a column safely"

**Do NOT use when:**

- Writing queries or optimizing DB access (use `backend-patterns`)
- Designing API responses (use `api-design`)
- Docker/infra work (use `docker-patterns` or `deployment-patterns`)

## Core Principles

1. **Migrations are immutable** — never edit a migration that has run in production
2. **Backward compatible** — old code must work with new schema during deployment
3. **Reversible** — every migration should have a rollback (down migration)
4. **One concern per migration** — don't mix table creation with data migration
5. **Test migrations** — run against a production-like dataset before deploying

## Migration Safety Checklist

Before running any migration in production:

```
- [ ] Migration is backward-compatible (old app version works with new schema)
- [ ] Rollback migration tested
- [ ] Large table? Estimated lock time acceptable (< 1s)
- [ ] No data loss — columns being removed are truly unused
- [ ] Indexes created CONCURRENTLY (PostgreSQL)
- [ ] Tested against production-size dataset
```

## PostgreSQL Patterns

### Adding a Column (safe)

```sql
-- Safe: nullable column, no lock
ALTER TABLE users ADD COLUMN avatar_url TEXT;

-- Safe: with default (Postgres 11+ doesn't rewrite table)
ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true;
```

### Adding an Index (safe)

```sql
-- Always use CONCURRENTLY to avoid table lock
CREATE INDEX CONCURRENTLY idx_users_email ON users (email);
```

⚠️ `CONCURRENTLY` cannot run inside a transaction. ORM migrations may need a raw SQL step.

### Renaming a Column (3-phase expand-contract)

**Never rename in one step** — breaks running application code.

**Phase 1: Expand** — add new column, backfill

```sql
ALTER TABLE users ADD COLUMN full_name TEXT;
UPDATE users SET full_name = name;
```

Update application code to write to both `name` and `full_name`.

**Phase 2: Migrate** — switch reads to new column

Update application code to read from `full_name`. Keep writing to both.

**Phase 3: Contract** — drop old column

```sql
ALTER TABLE users DROP COLUMN name;
```

### Removing a Column (2-phase)

**Phase 1:** Remove all application code that references the column. Deploy.

**Phase 2:** Drop the column in a migration. Deploy.

```sql
ALTER TABLE users DROP COLUMN legacy_field;
```

### Large Data Migrations

For tables with millions of rows, batch the update:

```sql
-- Process in batches of 10,000
UPDATE users SET status = 'active'
WHERE id IN (
  SELECT id FROM users WHERE status IS NULL LIMIT 10000
);
-- Repeat until no rows remain
```

**Rules:**

- Never update millions of rows in a single transaction
- Use `LIMIT` + loop or `pg_cron` for background processing
- Monitor lock duration and table bloat during migration

## ORM-Specific Workflows

### Prisma (TypeScript)

```bash
# Generate migration from schema changes
npx prisma migrate dev --name add_user_avatar

# Apply in production
npx prisma migrate deploy

# Reset dev database (destructive)
npx prisma migrate reset
```

**Custom SQL in Prisma migration:**

Edit the generated `.sql` file before applying:

```sql
-- Add CONCURRENTLY index (Prisma doesn't generate this)
CREATE INDEX CONCURRENTLY idx_users_email ON users (email);
```

### Django (Python)

```bash
# Generate migration
python manage.py makemigrations

# Review before applying
python manage.py sqlmigrate app_name 0042

# Apply
python manage.py migrate
```

**Data migration in Django:**

```python
from django.db import migrations

def backfill_full_name(apps, schema_editor):
    User = apps.get_model("users", "User")
    User.objects.filter(full_name__isnull=True).update(full_name=F("name"))

class Migration(migrations.Migration):
    dependencies = [("users", "0041_add_full_name")]
    operations = [migrations.RunPython(backfill_full_name, migrations.RunPython.noop)]
```

### Alembic (Python / SQLAlchemy)

```bash
# Generate migration
alembic revision --autogenerate -m "add user avatar"

# Apply
alembic upgrade head

# Rollback one step
alembic downgrade -1
```

### golang-migrate (Go)

```bash
# Create migration pair
migrate create -ext sql -dir migrations -seq add_user_avatar

# Apply
migrate -path migrations -database "$DATABASE_URL" up

# Rollback
migrate -path migrations -database "$DATABASE_URL" down 1
```

## Zero-Downtime Migration Strategy

Use the **expand-contract** pattern for breaking changes:

```
Phase 1: EXPAND
├── Add new column/table
├── Backfill data
├── App writes to BOTH old and new
└── Deploy app + migration together

Phase 2: MIGRATE
├── Switch reads to new column/table
├── Keep writing to both (safety net)
└── Deploy app change only

Phase 3: CONTRACT
├── Stop writing to old column/table
├── Drop old column/table
└── Deploy migration + app change
```

**Key rule:** Each phase is a separate deployment. Never combine expand + contract.

## Anti-Patterns

| Anti-Pattern | Why It's Bad | Fix |
|---|---|---|
| Rename column in one step | Breaks running code during deploy | 3-phase expand-contract |
| `NOT NULL` without default on existing table | Fails if rows exist | Add with default, or nullable first |
| Index without `CONCURRENTLY` | Locks table for duration | `CREATE INDEX CONCURRENTLY` |
| Data migration in same transaction as DDL | Long lock, blocks traffic | Separate migrations |
| Editing a deployed migration | Inconsistent state across environments | Create a new migration |
| `DROP COLUMN` without code cleanup first | Runtime errors during deploy | 2-phase: remove code → drop column |
