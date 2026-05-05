# .gitignore Template

The canonical `.gitignore` lives in `shared/.gitignore` and is synced to target repos by `sync-to-repo.sh`.

When creating or updating a `.gitignore`, use the shared file as the baseline. Add project-specific patterns below the shared ones.

## Mandatory categories

- **Environment** — `.env`, `.env.*` (except `.env.example`)
- **IDE** — `.idea/`, `.vscode/`, swap files
- **Dependencies** — `node_modules/`, `venv/`, `__pycache__/`
- **Build artifacts** — `dist/`, `build/`, `*.egg-info/`
- **OS** — `.DS_Store`, `Thumbs.db`
- **Secrets** — `*.pem`, `*.key`, `*credentials*`, `*secret*`
